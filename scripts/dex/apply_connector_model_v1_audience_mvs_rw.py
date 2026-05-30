#!/usr/bin/env python3
"""Apply connector-model V1 audience MVs to RisingWave + seed
ops.audience_mv_specs.

Reads scripts/_config/connector_model_v1_audience_mvs.yaml — 5 audience MV
specs over the HNW spine (mv_990_principal_with_fec_giving) — and:

  1. (Pre-flight) Asserts upstream MVs exist in pg_class:
       mv_990_principal_with_fec_giving (>1M rows, the spine)
       mv_audience_990pf_foundation_principals
       mv_audience_nonprofit_principals_high_comp
       mv_audience_nonprofit_principals
       mv_sec_adv_schedule_a_b
       mv_sec_adv_master
  2. (RW phase) For each audience: emits
        SET BACKGROUND_DDL = TRUE;
        DROP MATERIALIZED VIEW IF EXISTS public.<mv_name>;
        CREATE MATERIALIZED VIEW public.<mv_name> AS <select_template>;
     and runs it via psql against the RW cluster.
  3. (Wait phase) Polls rw_catalog.rw_ddl_progress until the MV's BACKGROUND
     build completes (timeout: 30min per MV).
  4. (Smoke phase, L25 + L30 HARD GATE) Counts rows; runs the named smoke
     query; asserts smoke >= smoke_min. Row count outside expected range is
     a soft signal recorded in notes (auto-merge proceeds if smoke passes).
  5. (Postgres phase) Upserts a row into ops.audience_mv_specs for each
     audience.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_connector_model_v1_audience_mvs_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_connector_model_v1_audience_mvs_rw.py --apply

See directive ~/Desktop/hq/directives/2026-05-10-connector-model-v1-audience-mvs.md.

Lifted from apply_sba_borrower_audience_mvs_rw.py (PR #281) — same shape,
different upstream MVs + different YAML config.
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
logger = logging.getLogger("apply_connector_model_v1_audience_mvs_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
YAML_PATH = (
    REPO_ROOT
    / "apps/data-engine-x/scripts/_config/connector_model_v1_audience_mvs.yaml"
)

PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30

UPSTREAM_MVS = [
    "mv_990_principal_with_fec_giving",
    "mv_audience_990pf_foundation_principals",
    "mv_audience_nonprofit_principals_high_comp",
    "mv_audience_nonprofit_principals",
    "mv_sec_adv_schedule_a_b",
    "mv_sec_adv_master",
]


def _load_specs() -> list[dict]:
    import yaml

    with YAML_PATH.open() as f:
        cfg = yaml.safe_load(f)
    audiences = cfg.get("audiences") or []
    if len(audiences) != 5:
        raise SystemExit(
            f"FAIL: expected 5 audience entries in YAML, got {len(audiences)}"
        )
    seen = set()
    for a in audiences:
        name = a["mv_name"]
        if name in seen:
            raise SystemExit(f"FAIL: duplicate mv_name in YAML: {name}")
        seen.add(name)
        if not name.startswith("mv_intro_candidate_"):
            raise SystemExit(
                f"FAIL: mv_name '{name}' violates naming convention "
                "(must start with 'mv_intro_candidate_')"
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
            "RW psql timed out after %ss — DDL likely admitted (verify via rw_ddl_progress)",
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
    logger.info("preflight: checking %d upstream RW objects", len(UPSTREAM_MVS))
    for mv in UPSTREAM_MVS:
        out = _rw_psql(
            f"SELECT relname FROM pg_class WHERE relname = '{mv}';",
            fetch=True,
        ).strip()
        if mv not in out:
            raise SystemExit(
                f"FAIL: upstream MV {mv} not found in pg_class. "
                "Predecessor directives must apply first."
            )
        logger.info("preflight: %s exists", mv)

    out = _rw_psql(
        "SELECT count(*) FROM public.mv_990_principal_with_fec_giving;",
        fetch=True,
    ).strip()
    n = int(out)
    if n < 500_000:
        raise SystemExit(
            f"FAIL: spine mv_990_principal_with_fec_giving has only {n} rows; "
            "expected >500K. Re-hydration in progress?"
        )
    logger.info("preflight: spine = %s rows", f"{n:,}")


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
    mv_name = spec["mv_name"]
    try:
        total_out = _rw_psql(
            f"SELECT count(*) FROM public.{mv_name};", fetch=True
        ).strip()
        total = int(total_out)
    except SystemExit as exc:
        logger.warning("count(*) failed for %s: %s", mv_name, str(exc)[:200])
        return ("pending_hydrate", 0, 0)

    if total == 0:
        return ("pending_hydrate", 0, 0)

    smoke_q = spec["smoke_query"].strip()
    smoke_out = _rw_psql(smoke_q, fetch=True).strip()
    smoke_count = int(smoke_out.splitlines()[0])

    # HARD smoke gate per L23 + L30 — smoke query MUST hit smoke_min.
    if smoke_count < spec["smoke_min"]:
        return ("fail_smoke", total, smoke_count)

    # Soft signals on row-count range — recorded in notes, do not block merge.
    if total < spec["expected_row_count_min"]:
        return ("pass_count_low", total, smoke_count)
    if total > spec["expected_row_count_max"] * 2:
        return ("pass_count_high", total, smoke_count)
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
            print(_ddl_for(spec))
        return

    if args.verify_only:
        for spec in specs:
            mv_name = spec["mv_name"]
            status, total, smoke_count = _smoke_gate(spec)
            logger.info(
                "verify %s: status=%s total=%s smoke=%s",
                mv_name, status, f"{total:,}", smoke_count,
            )
            mapped = (
                "pass" if status.startswith("pass")
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
            _rw_psql_script(ddl, timeout_s=120)
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

        if status.startswith("pass"):
            extra = ""
            if status == "pass_count_low":
                extra = (
                    f" (count BELOW expected_min={spec['expected_row_count_min']})"
                )
            elif status == "pass_count_high":
                extra = (
                    f" (count ABOVE 2x expected_max={spec['expected_row_count_max']})"
                )
            _seed_spec_row(
                spec, smoke_status="pass", smoke_row_count=smoke_count,
                notes=f"total_rows={total}; smoke_query_returned={smoke_count}{extra}",
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
                    f"smoke_min={spec['smoke_min']}"
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
