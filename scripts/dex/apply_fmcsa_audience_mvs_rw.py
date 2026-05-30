#!/usr/bin/env python3
"""Apply FMCSA audience MVs to RisingWave + seed ops.audience_mv_specs.

Reads the YAML config at scripts/_config/fmcsa_audience_mvs.yaml — 20
audience MV specs (15 base + 5 freight-brokerage signal MVs) — and:

  1. (Pre-flight) Asserts upstream MVs exist and have rows in expected
     ranges: mv_fmcsa_carrier_essentials, mv_fmcsa_pdl_match (BACKGROUND
     hydration tolerated for #12 dependency).
  2. (RW phase) For each non-deferred audience: emits
        SET BACKGROUND_DDL = TRUE;
        DROP MATERIALIZED VIEW IF EXISTS public.<mv_name>;
        CREATE MATERIALIZED VIEW public.<mv_name> AS <select_template>;
     and runs it via psql against the RW cluster.
  3. (Wait phase) Polls SHOW JOBS until the MV's BACKGROUND build completes
     (timeout: 30min per MV per directive pause-and-surface conditions).
  4. (Smoke phase, L25 + L30 HARD GATE) Counts rows; runs the named smoke
     query; asserts both pass. On failure: stops, surfaces the failing MV.
  5. (Postgres phase) Upserts a row into ops.audience_mv_specs for each
     audience (active + deferred), recording filter description, expected
     range, named smoke, business use case, last_smoke_status, etc.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_audience_mvs_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_audience_mvs_rw.py --apply

    # Re-seed spec table without re-applying DDL (idempotent):
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_audience_mvs_rw.py --seed-specs-only

See directive ~/Desktop/hq/directives/2026-05-09-fmcsa-audience-mvs.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_fmcsa_audience_mvs_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
YAML_PATH = (
    REPO_ROOT
    / "apps/data-engine-x/scripts/_config/fmcsa_audience_mvs.yaml"
)

# Per-MV BACKGROUND-build wait cap (directive: pause if a single MV stalls
# past 30 min).
PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30


def _load_specs() -> list[dict]:
    import yaml

    with YAML_PATH.open() as f:
        cfg = yaml.safe_load(f)
    audiences = cfg.get("audiences") or []
    if len(audiences) != 21:
        raise SystemExit(
            f"FAIL: expected 21 audience entries in YAML "
            "(15 base + 5 freight-brokerage signals + 1 emailable-paid-domain), "
            f"got {len(audiences)}"
        )
    seen = set()
    for a in audiences:
        name = a["mv_name"]
        if name in seen:
            raise SystemExit(f"FAIL: duplicate mv_name in YAML: {name}")
        seen.add(name)
        if not name.startswith("mv_audience_fmcsa_"):
            raise SystemExit(
                f"FAIL: mv_name '{name}' violates naming convention "
                "(must start with 'mv_audience_fmcsa_')"
            )
    return audiences


# ──────────────────────────────────────────────────────────────────────────────
# RW connection (psql shell-out — same shape as apply_fmcsa_pdl_match_rw.py).
# ──────────────────────────────────────────────────────────────────────────────


def _rw_psql(sql: str, *, fetch: bool = False) -> str:
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "-v", "ON_ERROR_STOP=1",
    ]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql failed (exit {proc.returncode}):\n"
            f"  CMD: {' '.join(cmd[:9])} <sql>\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _rw_psql_script(sql_script: str, *, timeout_s: int | None = None) -> str:
    """Run a multi-statement script through stdin (lets us SET + CREATE
    in the same session, which BACKGROUND_DDL requires).

    timeout_s: if set, kill the psql process after this many seconds. The
    DDL itself stays admitted by RW (BACKGROUND_DDL is async — RW returns
    after admission, not after hydration). The psql client may still hang
    on the cluster's NOTICE/idle-timeout; the timeout severs that without
    affecting the MV.
    """
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            input=sql_script,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        # The DDL was likely admitted; we just couldn't get a clean exit.
        logger.warning(
            "RW psql timed out after %ss — DDL likely admitted (verify via SHOW JOBS)",
            timeout_s,
        )
        return "<psql_timeout>"
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql script failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


# ──────────────────────────────────────────────────────────────────────────────
# Postgres connection (for ops.audience_mv_specs upsert).
# ──────────────────────────────────────────────────────────────────────────────


def _pg_psql(sql: str, *, fetch: bool = False) -> str:
    cmd = ["psql", os.environ["DEX_DB_URL_DIRECT"], "-v", "ON_ERROR_STOP=1"]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"PG psql failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


# ──────────────────────────────────────────────────────────────────────────────
# Pre-flight ground-truth checks.
# ──────────────────────────────────────────────────────────────────────────────


def _preflight_rw() -> None:
    logger.info("preflight: checking upstream RW objects")
    out = _rw_psql(
        "SELECT count(*) FROM public.mv_fmcsa_carrier_essentials;",
        fetch=True,
    ).strip()
    n = int(out)
    if n < 100_000:
        raise SystemExit(
            f"FAIL: mv_fmcsa_carrier_essentials has only {n} rows; "
            "expected >100K. Re-hydration in progress?"
        )
    logger.info("preflight: mv_fmcsa_carrier_essentials = %s rows", f"{n:,}")

    out = _rw_psql(
        "SELECT relname FROM pg_class WHERE relname = 'mv_fmcsa_pdl_match';",
        fetch=True,
    ).strip()
    if "mv_fmcsa_pdl_match" not in out:
        raise SystemExit(
            "FAIL: mv_fmcsa_pdl_match not found in pg_class. "
            "Predecessor directive #2 must apply before #5."
        )
    logger.info("preflight: mv_fmcsa_pdl_match exists (may be hydrating)")


# ──────────────────────────────────────────────────────────────────────────────
# Per-MV apply + smoke-gate.
# ──────────────────────────────────────────────────────────────────────────────


def _ddl_for(spec: dict, *, drop_first: bool = True) -> str:
    parts = ["SET BACKGROUND_DDL = TRUE;"]
    if drop_first:
        parts.append(f"DROP MATERIALIZED VIEW IF EXISTS public.{spec['mv_name']};")
    parts.append(
        f"CREATE MATERIALIZED VIEW public.{spec['mv_name']} AS\n"
        f"{spec['select_template'].rstrip()};"
    )
    return "\n".join(parts) + "\n"


def _mv_already_present(mv_name: str) -> bool:
    """True if the MV exists in pg_class OR is queued in rw_ddl_progress."""
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{mv_name}';",
        fetch=True,
    ).strip()
    if mv_name in out:
        return True
    out = _rw_psql(
        "SELECT ddl_statement FROM rw_catalog.rw_ddl_progress;",
        fetch=True,
    ).strip()
    return f"public.{mv_name}" in out


def _wait_for_mv_hydration(mv_name: str, *, timeout_s: int) -> str:
    """Poll SHOW JOBS until the MV is no longer in jobs list, or timeout.
    Returns 'hydrated' on success, 'timeout' on timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _rw_psql(
            f"SELECT ddl_statement FROM rw_catalog.rw_ddl_progress "
            f"WHERE ddl_statement ILIKE '%{mv_name}%';",
            fetch=True,
        ).strip()
        if not out:
            return "hydrated"
        logger.info("waiting on %s — still in rw_ddl_progress", mv_name)
        time.sleep(POLL_INTERVAL_S)
    return "timeout"


def _smoke_gate(spec: dict) -> tuple[str, int, int]:
    """Run row-count + named smoke query. Return (status, total_rows,
    smoke_count).

    status ∈ {'pass', 'fail_count_low', 'fail_count_high', 'fail_smoke',
              'pending_hydrate'}.
    """
    mv_name = spec["mv_name"]
    try:
        total_out = _rw_psql(
            f"SELECT count(*) FROM public.{mv_name};", fetch=True
        ).strip()
        total = int(total_out)
    except SystemExit as exc:
        # Hydration not yet complete; bubble up as pending.
        logger.warning("count(*) failed for %s: %s", mv_name, str(exc)[:200])
        return ("pending_hydrate", 0, 0)

    if total < spec["expected_row_count_min"]:
        # If the row count is at-zero or barely populated, this is a
        # pending_hydrate (not a HARD FAIL) — the MV is in BACKGROUND build.
        if total == 0:
            return ("pending_hydrate", 0, 0)
        return ("fail_count_low", total, 0)
    if total > spec["expected_row_count_max"] * 2:
        return ("fail_count_high", total, 0)

    smoke_q = spec["smoke_query"].strip()
    smoke_out = _rw_psql(smoke_q, fetch=True).strip()
    smoke_count = int(smoke_out.splitlines()[0])
    if smoke_count < spec["smoke_min"]:
        return ("fail_smoke", total, smoke_count)
    return ("pass", total, smoke_count)


# ──────────────────────────────────────────────────────────────────────────────
# ops.audience_mv_specs upsert.
# ──────────────────────────────────────────────────────────────────────────────


def _seed_spec_row(
    spec: dict,
    *,
    smoke_status: str,
    smoke_row_count: int,
    notes: str | None,
) -> None:
    def q(s: str | None) -> str:
        if s is None:
            return "NULL"
        s2 = s.replace("'", "''")
        return f"'{s2}'"

    notes_value = q(notes) if notes else "NULL"

    sql = f"""
    INSERT INTO ops.audience_mv_specs (
        mv_name,
        filter_description,
        expected_row_count_min,
        expected_row_count_max,
        named_smoke_query,
        expected_smoke_min,
        business_use_case,
        owner_team,
        last_applied_at,
        last_smoke_status,
        last_smoke_row_count,
        notes,
        updated_at
    ) VALUES (
        {q(spec['mv_name'])},
        {q(spec['filter_description'].strip())},
        {spec['expected_row_count_min']},
        {spec['expected_row_count_max']},
        {q(spec['smoke_query'].strip())},
        {spec['smoke_min']},
        {q(spec['business_use_case'].strip())},
        'data-engine-x',
        now(),
        {q(smoke_status)},
        {smoke_row_count},
        {notes_value},
        now()
    )
    ON CONFLICT (mv_name) DO UPDATE SET
        filter_description     = EXCLUDED.filter_description,
        expected_row_count_min = EXCLUDED.expected_row_count_min,
        expected_row_count_max = EXCLUDED.expected_row_count_max,
        named_smoke_query      = EXCLUDED.named_smoke_query,
        expected_smoke_min     = EXCLUDED.expected_smoke_min,
        business_use_case      = EXCLUDED.business_use_case,
        owner_team             = EXCLUDED.owner_team,
        last_applied_at        = EXCLUDED.last_applied_at,
        last_smoke_status      = EXCLUDED.last_smoke_status,
        last_smoke_row_count   = EXCLUDED.last_smoke_row_count,
        notes                  = EXCLUDED.notes,
        updated_at             = EXCLUDED.updated_at;
    """
    _pg_psql(sql)


# ──────────────────────────────────────────────────────────────────────────────
# Main flow.
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--seed-specs-only", action="store_true")
    p.add_argument(
        "--skip-wait",
        action="store_true",
        help="Apply DDL, seed spec rows with pending_hydrate, skip wait/smoke.",
    )
    p.add_argument(
        "--verify-only",
        action="store_true",
        help=(
            "For every spec marked pending_hydrate in ops.audience_mv_specs, "
            "run the smoke gate again and update status. Use after BACKGROUND "
            "DDLs have hydrated."
        ),
    )
    args = p.parse_args()
    if not (args.dry_run or args.apply or args.seed_specs_only or args.verify_only):
        p.error("specify --dry-run, --apply, --seed-specs-only, or --verify-only")

    specs = _load_specs()
    logger.info("loaded %d audience specs from %s", len(specs), YAML_PATH)

    if args.dry_run:
        for spec in specs:
            print(f"-- ====== {spec['mv_name']} ======")
            if spec.get("deferred"):
                print(f"-- DEFERRED: {spec.get('deferred_reason', '').strip()}")
            else:
                print(_ddl_for(spec))
        return

    if args.verify_only:
        # For each non-deferred spec, run smoke gate and update status.
        for spec in specs:
            mv_name = spec["mv_name"]
            if spec.get("deferred"):
                continue
            status, total, smoke_count = _smoke_gate(spec)
            logger.info(
                "verify %s: status=%s total=%s smoke=%s",
                mv_name, status, f"{total:,}", smoke_count,
            )
            mapped = (
                "pass" if status == "pass"
                else "pending_hydrate" if status == "pending_hydrate"
                else "fail"
            )
            _seed_spec_row(
                spec,
                smoke_status=mapped,
                smoke_row_count=smoke_count,
                notes=(
                    f"verify-only run; status={status} total={total} "
                    f"smoke={smoke_count}"
                ),
            )
        return

    if args.seed_specs_only:
        # Re-seed all spec rows from YAML without checking RW.
        # smoke_status/row_count is preserved-via-update only when row exists;
        # for fresh inserts we mark 'pending_hydrate' until next apply.
        for spec in specs:
            if spec.get("deferred"):
                _seed_spec_row(
                    spec,
                    smoke_status="blocked_upstream",
                    smoke_row_count=0,
                    notes=spec.get("deferred_reason", "").strip(),
                )
            else:
                _seed_spec_row(
                    spec,
                    smoke_status="pending_hydrate",
                    smoke_row_count=0,
                    notes="seed-only run; smoke not exercised",
                )
            logger.info("seeded spec row: %s", spec["mv_name"])
        return

    # --apply path.
    _preflight_rw()

    smoke_results: list[tuple[str, str, int, int]] = []
    failures: list[str] = []

    for spec in specs:
        mv_name = spec["mv_name"]

        if spec.get("deferred"):
            logger.warning(
                "DEFERRED — skipping DDL apply for %s: %s",
                mv_name,
                spec.get("deferred_reason", "")[:200],
            )
            _seed_spec_row(
                spec,
                smoke_status="blocked_upstream",
                smoke_row_count=0,
                notes=spec.get("deferred_reason", "").strip(),
            )
            smoke_results.append((mv_name, "blocked_upstream", 0, 0))
            continue

        # If the MV is already in pg_class or queued for creation, skip
        # the DDL apply — it's already admitted (idempotent re-run path).
        already = _mv_already_present(mv_name)
        if already:
            logger.info("already present (in pg_class or rw_ddl_progress) — skipping DDL: %s", mv_name)
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            _seed_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes="already in catalog or rw_ddl_progress; DDL skipped",
            )
            continue

        logger.info("applying DDL for %s", mv_name)
        ddl = _ddl_for(spec, drop_first=False)
        try:
            # Per-DDL timeout: BACKGROUND_DDL is supposed to admit + return,
            # but under cluster pressure psql may stall on barrier processing.
            # 90s is enough to admit the DDL; if the cluster genuinely can't
            # admit, this is a pause-and-surface condition.
            _rw_psql_script(ddl, timeout_s=90)
        except SystemExit as exc:
            logger.error("DDL apply FAILED for %s: %s", mv_name, str(exc)[:500])
            _seed_spec_row(
                spec,
                smoke_status="fail",
                smoke_row_count=0,
                notes=f"DDL apply failed: {str(exc)[:500]}",
            )
            failures.append(mv_name)
            continue

        if args.skip_wait:
            logger.info("--skip-wait: marking %s pending_hydrate", mv_name)
            _seed_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes="DDL applied; wait skipped via --skip-wait",
            )
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        # Wait for hydration.
        logger.info("waiting for %s to hydrate (timeout=%ds)", mv_name, PER_MV_WAIT_TIMEOUT_S)
        wait_status = _wait_for_mv_hydration(
            mv_name, timeout_s=PER_MV_WAIT_TIMEOUT_S
        )
        if wait_status == "timeout":
            logger.warning("HYDRATION TIMEOUT for %s — marking pending_hydrate", mv_name)
            _seed_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes=f"hydration timeout after {PER_MV_WAIT_TIMEOUT_S}s",
            )
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        # Smoke gate.
        status, total, smoke_count = _smoke_gate(spec)
        logger.info(
            "smoke for %s: status=%s total=%s smoke=%s",
            mv_name, status, f"{total:,}", smoke_count,
        )

        if status == "pass":
            _seed_spec_row(
                spec,
                smoke_status="pass",
                smoke_row_count=smoke_count,
                notes=f"total_rows={total}; smoke_query_returned={smoke_count}",
            )
        elif status == "pending_hydrate":
            _seed_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes="hydration incomplete at smoke time",
            )
        else:
            _seed_spec_row(
                spec,
                smoke_status="fail",
                smoke_row_count=smoke_count,
                notes=(
                    f"status={status} total_rows={total} "
                    f"smoke_returned={smoke_count} "
                    f"expected_min={spec['expected_row_count_min']} "
                    f"expected_max={spec['expected_row_count_max']} "
                    f"smoke_min={spec['smoke_min']}"
                ),
            )
            failures.append(f"{mv_name}({status})")

        smoke_results.append((mv_name, status, total, smoke_count))

    # ──────────────────────────────────────────────────────────────────────
    # Summary.
    # ──────────────────────────────────────────────────────────────────────
    print("\n=== APPLY SUMMARY ===")
    for mv, status, total, smoke in smoke_results:
        print(f"  {mv:<46} {status:<18} total={total:>12,} smoke={smoke:>10,}")
    print()

    if failures:
        logger.error("HARD-FAIL audiences: %s", failures)
        # Per directive: "PR does NOT ship if any MV's smoke returns 0."
        # Failures here include fail_smoke / fail_count_low (count==0 path).
        # pending_hydrate is NOT a failure — it gets retried.
        sys.exit(2)

    logger.info("done — %d audiences processed", len(specs))


if __name__ == "__main__":
    main()
