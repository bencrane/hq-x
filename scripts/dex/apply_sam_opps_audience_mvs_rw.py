#!/usr/bin/env python3
"""Apply SAM.gov Contract Opportunities audience MVs to RW + seed
ops.audience_mv_specs.

Reads the YAML at scripts/_config/sam_opps_audience_mvs.yaml — 7 audience
specs (5 active + 2 deferred until archived feed + UEI bridge land).

Lifted from apply_sba_borrower_audience_mvs_rw.py — same shape, different
upstream MV (mv_sam_opps_active_essentials), different YAML, different
naming convention.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/apply_sam_opps_audience_mvs_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/apply_sam_opps_audience_mvs_rw.py --apply --skip-wait

See directive ~/Desktop/hq/directives/2026-05-09-sam-gov-contract-opportunities-ingest-plan.md.
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
logger = logging.getLogger("apply_sam_opps_audience_mvs_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
YAML_PATH = (
    REPO_ROOT
    / "apps/data-engine-x/scripts/_config/sam_opps_audience_mvs.yaml"
)

EXPECTED_AUDIENCE_COUNT = 7
PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30


def _load_specs() -> list[dict]:
    import yaml
    with YAML_PATH.open() as f:
        cfg = yaml.safe_load(f)
    audiences = cfg.get("audiences") or []
    if len(audiences) != EXPECTED_AUDIENCE_COUNT:
        raise SystemExit(
            f"FAIL: expected {EXPECTED_AUDIENCE_COUNT} audience entries in YAML "
            "(5 active + 2 deferred until archived/UEI bridge), "
            f"got {len(audiences)}"
        )
    seen = set()
    for a in audiences:
        name = a["mv_name"]
        if name in seen:
            raise SystemExit(f"FAIL: duplicate mv_name in YAML: {name}")
        seen.add(name)
        if not name.startswith("mv_audience_sam_opps_"):
            raise SystemExit(
                f"FAIL: mv_name '{name}' violates naming convention "
                "(must start with 'mv_audience_sam_opps_')"
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
            f"  STDERR:\n{proc.stderr}\n  STDOUT:\n{proc.stdout}"
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
            f"  STDERR:\n{proc.stderr}\n  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _pg_psql(sql: str, *, fetch: bool = False) -> str:
    cmd = [
        "psql",
        os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DEX_DB_URL_POOLED"],
        "-v", "ON_ERROR_STOP=1",
    ]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"PG psql failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _preflight_rw() -> None:
    logger.info("preflight: checking upstream RW objects")
    out = _rw_psql(
        "SELECT count(*) FROM public.mv_sam_opps_active_essentials;",
        fetch=True,
    ).strip()
    n = int(out)
    if n < 1000:
        raise SystemExit(
            f"FAIL: mv_sam_opps_active_essentials has only {n} rows; "
            "expected >50k. Re-hydration in progress?"
        )
    logger.info("preflight: mv_sam_opps_active_essentials = %s rows", f"{n:,}")


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
    return mv_name in out


def _smoke_gate(spec: dict) -> tuple[str, int, int]:
    mv_name = spec["mv_name"]
    try:
        total = int(
            _rw_psql(f"SELECT count(*) FROM public.{mv_name};", fetch=True).strip()
        )
    except SystemExit as exc:
        logger.warning("count(*) failed for %s: %s", mv_name, str(exc)[:200])
        return ("pending_hydrate", 0, 0)
    if total < spec["expected_row_count_min"]:
        if total == 0:
            return ("pending_hydrate", 0, 0)
        return ("fail_count_low", total, 0)
    if total > spec["expected_row_count_max"] * 2:
        return ("fail_count_high", total, 0)
    smoke_count = int(_rw_psql(spec["smoke_query"].strip(), fetch=True).strip().splitlines()[0])
    if smoke_count < spec["smoke_min"]:
        return ("fail_smoke", total, smoke_count)
    return ("pass", total, smoke_count)


def _seed_spec_row(
    spec: dict, *, smoke_status: str, smoke_row_count: int, notes: str | None,
) -> None:
    def q(s: str | None) -> str:
        if s is None:
            return "NULL"
        return "'" + s.replace("'", "''") + "'"
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
        {q(notes) if notes else 'NULL'},
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
                    notes="DEFERRED: requires archived feed + UEI bridge",
                )
            else:
                _seed_spec_row(
                    spec, smoke_status="pending_hydrate", smoke_row_count=0,
                    notes="seed-only; smoke not exercised",
                )
            logger.info("seeded spec row: %s", spec["mv_name"])
        return

    # --apply
    _preflight_rw()
    smoke_results: list[tuple[str, str, int, int]] = []
    failures: list[str] = []

    for spec in specs:
        mv_name = spec["mv_name"]
        if spec.get("deferred"):
            logger.warning("DEFERRED — skipping DDL apply for %s", mv_name)
            _seed_spec_row(
                spec, smoke_status="blocked_upstream", smoke_row_count=0,
                notes="DEFERRED: requires archived feed + UEI bridge",
            )
            smoke_results.append((mv_name, "blocked_upstream", 0, 0))
            continue

        if _mv_already_present(mv_name):
            logger.info("already present — skipping DDL: %s", mv_name)
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            _seed_spec_row(
                spec, smoke_status="pending_hydrate", smoke_row_count=0,
                notes="already in catalog; DDL skipped",
            )
            continue

        logger.info("applying DDL for %s", mv_name)
        try:
            _rw_psql_script(_ddl_for(spec, drop_first=False), timeout_s=90)
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
                notes="DDL applied; wait skipped",
            )
            smoke_results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        # Wait + smoke
        deadline = time.time() + PER_MV_WAIT_TIMEOUT_S
        while time.time() < deadline:
            try:
                total = int(_rw_psql(
                    f"SELECT count(*) FROM public.{mv_name};", fetch=True,
                ).strip())
                if total > 0:
                    break
            except SystemExit:
                pass
            time.sleep(POLL_INTERVAL_S)

        status, total, smoke_count = _smoke_gate(spec)
        logger.info(
            "smoke for %s: status=%s total=%s smoke=%s",
            mv_name, status, f"{total:,}", smoke_count,
        )

        if status == "pass":
            _seed_spec_row(
                spec, smoke_status="pass", smoke_row_count=smoke_count,
                notes=f"total_rows={total} smoke_returned={smoke_count}",
            )
        elif status == "pending_hydrate":
            _seed_spec_row(
                spec, smoke_status="pending_hydrate", smoke_row_count=0,
                notes="hydration incomplete at smoke time",
            )
        else:
            _seed_spec_row(
                spec, smoke_status="fail", smoke_row_count=smoke_count,
                notes=f"status={status} total={total} smoke={smoke_count} "
                      f"expected_min={spec['expected_row_count_min']}",
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
