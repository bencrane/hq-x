#!/usr/bin/env python3
"""Apply the SAM ↔ USAspending UEI aggregation MV (directive #3, surface 3).

Builds two RW objects (sections (1) + (2) of risingwave/tier_a_bridges.sql):

    source_sam_entities_pocs       — parallel thin source over SAM monthly Parquet
                                     glob, declaring POC + identity + normalized
                                     state cols that source_sam_entities omits
    mv_sam_usaspending_aggregated  — UNION-ALL of 4 USAspending streams
                                     (contracts, contracts_historical,
                                     contract_subawards, assistance_subawards)
                                     joined to SAM via UEI, with bridge_run_id
                                     embedded as a literal from the registry.

Trivial-bridge pattern (L20): bridge_run_id is resolved at apply-time
from ops.bridge_generation_runs (latest completed run for
sam_usaspending_uei_contracts) and substituted as a literal in the MV
SQL. The MV cannot query ops.* directly because RW and Postgres are
separate databases — the literal-substitution path is the workaround.
The 4 SAM-USAspending bridges share the same uei_exact method, so one
representative bridge_run_id covers the MV.

BACKGROUND_DDL=TRUE per L24 — UNION-ALL of 4 streams + JOIN to 884K SAM
stalls in foreground even with bypass_cluster_limits=true.

Registry preflight: asserts uei_exact method + 4 SAM-USAspending bridges
exist in ops.* before applying. Asserts 1+ completed run for
sam_usaspending_uei_contracts exists (so the bridge_run_id subquery is
non-empty). Stamps a fresh `running` bridge_generation_run row at apply
time and `completed` it on success — gives the MV a fresh trace key.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_sam_usaspending_aggregated_rw.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_sam_usaspending_aggregated_rw.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    start_bridge_run,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_sam_usaspending_aggregated_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
SQL_FILE = REPO_ROOT / "apps/data-engine-x/risingwave/tier_a_bridges.sql"

BRIDGE_NAMES = [
    "sam_usaspending_uei_contracts",
    "sam_usaspending_uei_contracts_historical",
    "sam_usaspending_uei_contract_subawards",
    "sam_usaspending_uei_assistance_subawards",
]


def _verify_registry() -> None:
    """Assert all 4 SAM-USAspending bridges + uei_exact method registered."""
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.bridge_name, m.method_name, v.semver
                  FROM ops.bridges b
                  JOIN ops.match_methods m USING (match_method_id)
                  JOIN ops.match_method_versions v
                    ON v.match_method_id = m.match_method_id
                 WHERE b.bridge_name = ANY(%s)
                """,
                (BRIDGE_NAMES,),
            )
            rows = cur.fetchall()
    found = {r[0] for r in rows}
    missing = set(BRIDGE_NAMES) - found
    if missing:
        raise SystemExit(
            f"FAIL: SAM-USAspending bridges not registered: {sorted(missing)}. "
            "Run scripts/seed_bridge_registry_tier_a.py --apply first."
        )
    methods = {r[1] for r in rows}
    if "uei_exact" not in methods:
        raise SystemExit("FAIL: uei_exact method not registered.")
    logger.info(
        f"  registry OK — {len(found)} bridges via {sorted(methods)}"
    )


def _stamp_bridge_runs() -> dict[str, str]:
    """Start one bridge_generation_run per SAM-USAspending bridge.

    Returns dict {bridge_name: run_id}. The MV's bridge_run_id literal
    is the run_id for sam_usaspending_uei_contracts (representative for
    all 4 since they share the uei_exact method).
    """
    run_ids: dict[str, str] = {}
    for bridge_name in BRIDGE_NAMES:
        run_id = start_bridge_run(
            bridge_name=bridge_name,
            method_semver="1.0.0",
            bridge_version="1.0.0",
            source_left="source_sam_entities",
            source_right=_source_right_for(bridge_name),
            match_method="uei_exact",
            r2_output_key="trivial",  # no Parquet artifact for trivial-bridges
        )
        run_ids[bridge_name] = str(run_id)
        logger.info(f"  started run {run_id} for bridge={bridge_name}")
    return run_ids


def _source_right_for(bridge_name: str) -> str:
    return {
        "sam_usaspending_uei_contracts": "source_usaspending_contracts",
        "sam_usaspending_uei_contracts_historical": "source_usaspending_contracts_historical",
        "sam_usaspending_uei_contract_subawards": "source_usaspending_contract_subawards",
        "sam_usaspending_uei_assistance_subawards": "source_usaspending_assistance_subawards",
    }[bridge_name]


def _complete_bridge_runs(run_ids: dict[str, str], rows_matched: int) -> None:
    """Complete all 4 bridge_generation_runs. rows_matched applies to all."""
    import uuid as _uuid
    for rid in run_ids.values():
        complete_bridge_run(
            _uuid.UUID(rid),
            metrics={"rows_matched": rows_matched},
        )


def _fail_bridge_runs(run_ids: dict[str, str], err: str) -> None:
    import uuid as _uuid
    for rid in run_ids.values():
        try:
            fail_bridge_run(_uuid.UUID(rid), err)
        except Exception:
            logger.exception(f"failed to mark run {rid} as failed")


def _extract_section(sql: str) -> str:
    """Extract sections (1) + (2) from the doc — drop everything from
    `(3) source_bridge_sam_pdl_domain` onward.
    """
    marker = "-- (3) source_bridge_sam_pdl_domain"
    idx = sql.find(marker)
    if idx == -1:
        raise SystemExit(f"FAIL: marker {marker!r} not found in SQL doc")
    # Walk back to the section header line start
    lines = sql.splitlines(keepends=True)
    section_start = -1
    for i, line in enumerate(lines):
        if marker in line:
            section_start = i
            break
    if section_start == -1:
        raise SystemExit(f"FAIL: marker line not found")
    # Everything before the section header (including the preceding `-- ───` ruler)
    keep_until = section_start
    for j in range(section_start - 1, -1, -1):
        if "─" in lines[j]:
            keep_until = j
            break
    return "".join(lines[:keep_until])


def _substitute_secrets(sql: str, bridge_run_id: str) -> str:
    return (
        sql.replace("__R2_ENDPOINT__", os.environ["R2_ENDPOINT"])
        .replace("__R2_ACCESS_KEY_ID__", os.environ["R2_ACCESS_KEY_ID"])
        .replace("__R2_SECRET_ACCESS_KEY__", os.environ["R2_SECRET_ACCESS_KEY"])
        .replace("__SAM_USASPENDING_BRIDGE_RUN_ID__", bridge_run_id)
    )


def _apply_to_rw(sql: str) -> None:
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
    proc = subprocess.run(
        cmd, input=sql, env=env, check=False, capture_output=True, text=True
    )
    if proc.stdout:
        logger.info(proc.stdout.rstrip())
    if proc.stderr:
        logger.info(proc.stderr.rstrip())
    if proc.returncode != 0:
        raise SystemExit(f"psql exited {proc.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply DDL + stamp runs")
    parser.add_argument("--dry-run", action="store_true", help="print substituted SQL, no apply")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    required = (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "RISINGWAVE_HOST", "RISINGWAVE_PORT", "RISINGWAVE_USER",
        "RISINGWAVE_PASSWORD", "RISINGWAVE_DATABASE",
        "DEX_DB_URL_DIRECT",
    )
    for var in required:
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    if not SQL_FILE.is_file():
        raise SystemExit(f"FAIL: SQL doc missing: {SQL_FILE}")

    _verify_registry()

    sql = SQL_FILE.read_text()
    section_sql = _extract_section(sql)

    if args.dry_run:
        # For dry-run, use a sentinel UUID for the bridge_run_id placeholder
        substituted = _substitute_secrets(section_sql, "00000000-0000-0000-0000-000000000000")
        masked = (
            substituted
            .replace(os.environ["R2_ACCESS_KEY_ID"], "<R2_ACCESS_KEY_ID>")
            .replace(os.environ["R2_SECRET_ACCESS_KEY"], "<R2_SECRET_ACCESS_KEY>")
        )
        print(masked)
        logger.info("DRY RUN — no DDL applied, no runs stamped")
        return 0

    # Stamp 4 runs (one per SAM-USAspending bridge). The MV's bridge_run_id
    # literal is the run_id for sam_usaspending_uei_contracts.
    run_ids = _stamp_bridge_runs()
    representative_run_id = run_ids["sam_usaspending_uei_contracts"]
    substituted = _substitute_secrets(section_sql, representative_run_id)

    t0 = time.time()
    try:
        logger.info("applying SAM-USAspending DDL to RisingWave …")
        _apply_to_rw(substituted)
        # Mark all runs complete; rows_matched is unknown yet (MV is BACKGROUND
        # creating). Caller validation queries get the actual count later.
        _complete_bridge_runs(run_ids, rows_matched=0)
        logger.info(f"OK — DDL applied. duration={time.time()-t0:.1f}s")
        logger.info(f"     representative bridge_run_id={representative_run_id}")
        logger.info(f"     run_ids={run_ids}")
        return 0
    except Exception as exc:
        logger.exception("apply failed")
        _fail_bridge_runs(run_ids, str(exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
