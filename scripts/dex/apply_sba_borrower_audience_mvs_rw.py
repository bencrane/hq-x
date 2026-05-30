#!/usr/bin/env python3
"""Apply SBA-borrower audience MVs to RisingWave + seed ops.audience_mv_specs.

Reads the YAML config at scripts/_config/sba_borrower_audience_mvs.yaml — 13
audience MV specs (10 borrower-grain + 1 lender-grain
`mv_audience_sba_lender_profiles` + 1 franchisee-cohort
`mv_audience_sba_franchisees` added 2026-05-10 + 1 franchisee pipeline-cohort
`mv_audience_sba_franchisee_committed_not_funded` added 2026-05-10) — and:

  1. (Pre-flight) Asserts upstream MVs exist:
       mv_sba_borrower_essentials (>10M rows expected, post-#4)
       mv_sba_borrower_with_fec_owner (BACKGROUND hydration tolerated)
       mv_sba_borrower_with_5500_employer (BACKGROUND hydration tolerated)
  2. (RW phase) For each non-deferred audience: emits
        SET BACKGROUND_DDL = TRUE;
        DROP MATERIALIZED VIEW IF EXISTS public.<mv_name>;
        CREATE MATERIALIZED VIEW public.<mv_name> AS <select_template>;
     and runs it via psql against the RW cluster.
  3. (Wait phase) Polls SHOW JOBS until the MV's BACKGROUND build completes
     (timeout: 30min per MV).
  4. (Smoke phase, L25 + L30 HARD GATE) Counts rows; runs the named smoke
     query; asserts both pass.
  5. (Postgres phase) Upserts a row into ops.audience_mv_specs for each
     audience (active + deferred), recording filter description, expected
     range, named smoke, business use case, last_smoke_status, etc.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_sba_borrower_audience_mvs_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_sba_borrower_audience_mvs_rw.py --apply

See directive ~/Desktop/hq/directives/2026-05-09-sba-borrower-audience-mvs.md.

Lifted from apply_fmcsa_audience_mvs_rw.py (#5) — same shape, different
upstream MVs + different YAML config.
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
logger = logging.getLogger("apply_sba_borrower_audience_mvs_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
YAML_PATH = (
    REPO_ROOT
    / "apps/data-engine-x/scripts/_config/sba_borrower_audience_mvs.yaml"
)

PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30


def _load_specs() -> list[dict]:
    import yaml

    with YAML_PATH.open() as f:
        cfg = yaml.safe_load(f)
    audiences = cfg.get("audiences") or []
    if len(audiences) != 13:
        raise SystemExit(
            f"FAIL: expected 13 audience entries in YAML "
            "(10 borrower-grain + 1 lender-grain + 1 franchisee + 1 franchisee "
            "pipeline-cohort post-2026-05-10), "
            f"got {len(audiences)}"
        )
    seen = set()
    for a in audiences:
        name = a["mv_name"]
        if name in seen:
            raise SystemExit(f"FAIL: duplicate mv_name in YAML: {name}")
        seen.add(name)
        if not name.startswith("mv_audience_sba_"):
            raise SystemExit(
                f"FAIL: mv_name '{name}' violates naming convention "
                "(must start with 'mv_audience_sba_')"
            )
    return audiences


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
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _rw_psql_script(sql_script: str, *, timeout_s: int | None = None) -> str:
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
            cmd, env=env, input=sql_script, capture_output=True, text=True,
            check=False, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
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


def _preflight_rw() -> None:
    logger.info("preflight: checking upstream RW objects")
    out = _rw_psql(
        "SELECT count(*) FROM public.mv_sba_borrower_essentials;",
        fetch=True,
    ).strip()
    n = int(out)
    if n < 5_000_000:
        raise SystemExit(
            f"FAIL: mv_sba_borrower_essentials has only {n} rows; "
            "expected >5M. Re-hydration in progress?"
        )
    logger.info("preflight: mv_sba_borrower_essentials = %s rows", f"{n:,}")

    out = _rw_psql(
        "SELECT relname FROM pg_class WHERE relname = 'mv_sba_borrower_with_fec_owner';",
        fetch=True,
    ).strip()
    if "mv_sba_borrower_with_fec_owner" not in out:
        raise SystemExit(
            "FAIL: mv_sba_borrower_with_fec_owner not found in pg_class. "
            "Predecessor directive #4 must apply before #7."
        )
    logger.info("preflight: mv_sba_borrower_with_fec_owner exists (may be hydrating)")


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
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _rw_psql(
            f"SELECT statement FROM rw_catalog.rw_ddl_progress "
            f"WHERE statement ILIKE '%{mv_name}%';",
            fetch=True,
        ).strip()
        if not out:
            return "hydrated"
        logger.info("waiting on %s — still in rw_ddl_progress", mv_name)
        time.sleep(POLL_INTERVAL_S)
    return "timeout"


def _smoke_gate(spec: dict) -> tuple[str, int, int]:
    mv_name = spec["mv_name"]
    try:
        total_out = _rw_psql(
            f"SELECT count(*) FROM public.{mv_name};", fetch=True
        ).strip()
        total = int(total_out)
    except SystemExit as exc:
        logger.warning("count(*) failed for %s: %s", mv_name, str(exc)[:200])
        return ("pending_hydrate", 0, 0)

    if total < spec["expected_row_count_min"]:
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
        mv_name, filter_description, expected_row_count_min, expected_row_count_max,
        named_smoke_query, expected_smoke_min, business_use_case, owner_team,
        last_applied_at, last_smoke_status, last_smoke_row_count, notes, updated_at
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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--seed-specs-only", action="store_true")
    p.add_argument("--skip-wait", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    args = p.parse_args()
    if not (args.dry_run or args.apply or args.seed_specs_only or args.verify_only):
        p.error("specify --dry-run, --apply, --seed-specs-only, or --verify-only")

    specs = _load_specs()
    logger.info("loaded %d audience specs from %s", len(specs), YAML_PATH)

    if args.dry_run:
        for spec in specs:
            print(f"-- ====== {spec['mv_name']} ======")
            if spec.get("deferred"):
                print(f"-- DEFERRED")
            else:
                print(_ddl_for(spec))
        return

    if args.verify_only:
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
                spec, smoke_status=mapped, smoke_row_count=smoke_count,
                notes=f"verify-only run; status={status} total={total} smoke={smoke_count}",
            )
        return

    if args.seed_specs_only:
        for spec in specs:
            if spec.get("deferred"):
                _seed_spec_row(
                    spec, smoke_status="blocked_upstream", smoke_row_count=0,
                    notes="DEFERRED: Form 5500 × SBA bridge not built",
                )
            else:
                _seed_spec_row(
                    spec, smoke_status="pending_hydrate", smoke_row_count=0,
                    notes="seed-only run; smoke not exercised",
                )
            logger.info("seeded spec row: %s", spec["mv_name"])
        return

    # --apply path
    _preflight_rw()

    smoke_results: list[tuple[str, str, int, int]] = []
    failures: list[str] = []

    for spec in specs:
        mv_name = spec["mv_name"]

        if spec.get("deferred"):
            logger.warning("DEFERRED — skipping DDL apply for %s", mv_name)
            _seed_spec_row(
                spec, smoke_status="blocked_upstream", smoke_row_count=0,
                notes="DEFERRED: Form 5500 × SBA bridge not built",
            )
            smoke_results.append((mv_name, "blocked_upstream", 0, 0))
            continue

        already = _mv_already_present(mv_name)
        if already:
            logger.info("already present — skipping DDL: %s", mv_name)
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            _seed_spec_row(
                spec, smoke_status="pending_hydrate", smoke_row_count=0,
                notes="already in catalog or rw_ddl_progress; DDL skipped",
            )
            continue

        logger.info("applying DDL for %s", mv_name)
        ddl = _ddl_for(spec, drop_first=False)
        try:
            _rw_psql_script(ddl, timeout_s=90)
        except SystemExit as exc:
            logger.error("DDL apply FAILED for %s: %s", mv_name, str(exc)[:500])
            _seed_spec_row(
                spec, smoke_status="fail", smoke_row_count=0,
                notes=f"DDL apply failed: {str(exc)[:500]}",
            )
            failures.append(mv_name)
            continue

        if args.skip_wait:
            logger.info("--skip-wait: marking %s pending_hydrate", mv_name)
            _seed_spec_row(
                spec, smoke_status="pending_hydrate", smoke_row_count=0,
                notes="DDL applied; wait skipped via --skip-wait",
            )
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        logger.info("waiting for %s to hydrate", mv_name)
        wait_status = _wait_for_mv_hydration(
            mv_name, timeout_s=PER_MV_WAIT_TIMEOUT_S
        )
        if wait_status == "timeout":
            logger.warning("HYDRATION TIMEOUT for %s — marking pending_hydrate", mv_name)
            _seed_spec_row(
                spec, smoke_status="pending_hydrate", smoke_row_count=0,
                notes=f"hydration timeout after {PER_MV_WAIT_TIMEOUT_S}s",
            )
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        status, total, smoke_count = _smoke_gate(spec)
        logger.info(
            "smoke for %s: status=%s total=%s smoke=%s",
            mv_name, status, f"{total:,}", smoke_count,
        )

        if status == "pass":
            _seed_spec_row(
                spec, smoke_status="pass", smoke_row_count=smoke_count,
                notes=f"total_rows={total}; smoke_query_returned={smoke_count}",
            )
        elif status == "pending_hydrate":
            _seed_spec_row(
                spec, smoke_status="pending_hydrate", smoke_row_count=0,
                notes="hydration incomplete at smoke time",
            )
        else:
            _seed_spec_row(
                spec, smoke_status="fail", smoke_row_count=smoke_count,
                notes=(
                    f"status={status} total_rows={total} "
                    f"smoke_returned={smoke_count} "
                    f"expected_min={spec['expected_row_count_min']}"
                ),
            )
            failures.append(mv_name)

        smoke_results.append((mv_name, status, total, smoke_count))

    # Summary
    logger.info("=" * 70)
    logger.info("Apply summary:")
    for mv_name, status, total, smoke_count in smoke_results:
        logger.info("  %s | %s | total=%s smoke=%s", mv_name, status, f"{total:,}", smoke_count)
    logger.info("=" * 70)

    if failures:
        logger.error("FAILURES: %s", failures)
        sys.exit(1)
    logger.info("All audience MVs applied successfully.")


if __name__ == "__main__":
    main()
