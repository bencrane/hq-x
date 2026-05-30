#!/usr/bin/env python3
"""Rebuild source_usaspending_contracts_historical with typed source DDL.

Why this exists:

    The Parquets at usaspending/contracts/year=*/data.parquet are TYPED
    (action_date: DATE, total_dollars_obligated: DOUBLE, etc., 100%
    populated for action_date). The original CREATE SOURCE declared every
    column as CHARACTER VARYING per the L2 "Parquet=VARCHAR everywhere"
    convention. RW's S3-Parquet connector then silently NULLs columns
    when source-type ≠ Parquet-type — so all 23M historical contract
    rows had NULL action_date and NULL total_dollars_obligated.

    Result: mv_sam_usaspending_aggregated's first_award_date /
    latest_award_date were bounded by the working subaward streams
    only (~1 fiscal year), making mv_audience_fed_contractors_long_tenure
    (5+ year tenure filter) impossible to populate.

    Same root cause + same fix pattern as IRS 990 (PR #273):
        - Introspect Parquet schema via DuckDB
        - Map types via _duckdb_type_to_rw
        - Emit CREATE SOURCE with proper types
        - Update mv_sam_usaspending_aggregated to direct-project from
          the historical stream (drop the regex CASE WHEN guard)

Run:
    doppler run -p hq-all -c prd -- \\
        uv run --with 'psycopg[binary]' --with duckdb --with boto3 python \\
        apps/data-engine-x/scripts/rebuild_usaspending_historical_typed.py --apply

Idempotent: re-runs will DROP+CREATE again. --dry-run prints DDL only.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("rebuild_usaspending_historical")

REPO_ROOT = Path(__file__).resolve().parents[3]


# Lifted from apply_irs_990_rw_wiring.py — keep in sync if the canonical
# version evolves.
def _duckdb_type_to_rw(duck_type: str) -> str:
    t = duck_type.upper()
    base = t.split("(")[0]
    mapping = {
        "VARCHAR": "CHARACTER VARYING",
        "TEXT": "CHARACTER VARYING",
        "STRING": "CHARACTER VARYING",
        "BIGINT": "BIGINT",
        "INTEGER": "INTEGER",
        "INT": "INTEGER",
        "SMALLINT": "SMALLINT",
        "TINYINT": "SMALLINT",
        "DOUBLE": "DOUBLE PRECISION",
        "FLOAT": "REAL",
        "REAL": "REAL",
        "DECIMAL": "NUMERIC",
        "NUMERIC": "NUMERIC",
        "BOOLEAN": "BOOLEAN",
        "BOOL": "BOOLEAN",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
        "UUID": "CHARACTER VARYING",
    }
    return mapping.get(base, "CHARACTER VARYING")


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"FAIL: required env var {name} is not set")
    return v


def _rw_psql(sql: str, *, fetch: bool = False, timeout_s: int = 600) -> str:
    cmd = [
        "psql",
        "-h", _required_env("RISINGWAVE_HOST"),
        "-p", _required_env("RISINGWAVE_PORT"),
        "-U", _required_env("RISINGWAVE_USER"),
        "-d", _required_env("RISINGWAVE_DATABASE"),
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    cmd += ["-tAc", sql] if fetch else ["-c", sql]
    env = {**os.environ, "PGPASSWORD": _required_env("RISINGWAVE_PASSWORD")}
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          check=False, timeout=timeout_s)
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql failed (exit {proc.returncode}):\n"
            f"  STDERR: {proc.stderr}\n  STDOUT: {proc.stdout}"
        )
    return proc.stdout


def _introspect_parquet(s3_uri: str) -> list[tuple[str, str]]:
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint_full = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_region='auto';")
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{s3_uri}');").fetchall()
    return [(r[0], r[1]) for r in rows]


def _build_create_source_ddl(columns: list[tuple[str, str]]) -> str:
    cols_decl = ",\n    ".join(
        f'"{name}" {_duckdb_type_to_rw(t)}' for name, t in columns
    )
    return f"""\
CREATE SOURCE source_usaspending_contracts_historical (
    {cols_decl}
) WITH (
    connector = 's3',
    s3.region_name = 'us-east-1',
    s3.bucket_name = 'dex-raw-landing-zone',
    s3.endpoint_url = '{_required_env("R2_ENDPOINT")}',
    s3.credentials.access = '{_required_env("R2_ACCESS_KEY_ID")}',
    s3.credentials.secret = '{_required_env("R2_SECRET_ACCESS_KEY")}',
    match_pattern = 'usaspending/contracts/year=*/data.parquet'
) FORMAT PLAIN ENCODE PARQUET;"""


# Updated aggregated MV SQL — Stream 2 (historical) now direct-projects
# action_date / total_dollars_obligated since the source returns them typed.
# Streams 1 / 3 / 4 unchanged (Stream 1's source is already typed but empty;
# Streams 3-4 still have VARCHAR action_dates in the Parquet).
AGGREGATED_MV_DDL = """
SET BACKGROUND_DDL = TRUE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_sam_usaspending_aggregated;
CREATE MATERIALIZED VIEW public.mv_sam_usaspending_aggregated AS
WITH all_usaspending AS (
    SELECT
        'contracts' AS stream,
        recipient_uei,
        total_dollars_obligated AS amount,
        action_date,
        awarding_agency_name,
        naics_code
    FROM public.source_usaspending_contracts
    WHERE recipient_uei IS NOT NULL
    UNION ALL
    SELECT
        'contracts_historical' AS stream,
        recipient_uei,
        CAST(total_dollars_obligated AS NUMERIC) AS amount,
        action_date,
        awarding_agency_name,
        naics_code
    FROM public.source_usaspending_contracts_historical
    WHERE recipient_uei IS NOT NULL
    UNION ALL
    SELECT
        'contract_subawards' AS stream,
        subawardee_uei AS recipient_uei,
        CASE WHEN subaward_amount ~ '^-?[0-9]+(\\.[0-9]+)?$'
             THEN subaward_amount::NUMERIC ELSE NULL END AS amount,
        CASE WHEN subaward_action_date ~ '^\\d{4}-\\d{2}-\\d{2}'
             THEN subaward_action_date::DATE ELSE NULL END AS action_date,
        prime_award_awarding_agency_name AS awarding_agency_name,
        prime_award_naics_code AS naics_code
    FROM public.source_usaspending_contract_subawards
    WHERE subawardee_uei IS NOT NULL
    UNION ALL
    SELECT
        'assistance_subawards' AS stream,
        subawardee_uei AS recipient_uei,
        CASE WHEN subaward_amount ~ '^-?[0-9]+(\\.[0-9]+)?$'
             THEN subaward_amount::NUMERIC ELSE NULL END AS amount,
        CASE WHEN subaward_action_date ~ '^\\d{4}-\\d{2}-\\d{2}'
             THEN subaward_action_date::DATE ELSE NULL END AS action_date,
        prime_award_awarding_agency_name AS awarding_agency_name,
        NULL AS naics_code
    FROM public.source_usaspending_assistance_subawards
    WHERE subawardee_uei IS NOT NULL
),
agg AS (
    SELECT
        recipient_uei,
        count(*) AS total_award_actions,
        sum(amount) AS total_obligated_dollars,
        min(action_date) AS first_award_date,
        max(action_date) AS latest_award_date,
        count(DISTINCT awarding_agency_name) AS distinct_agencies_count,
        count(DISTINCT naics_code) FILTER (WHERE naics_code IS NOT NULL) AS distinct_naics_count,
        count(*) FILTER (WHERE stream IN ('contracts', 'contracts_historical')) AS contract_count,
        count(*) FILTER (WHERE stream IN ('contract_subawards', 'assistance_subawards')) AS subaward_count
    FROM all_usaspending
    GROUP BY recipient_uei
)
SELECT
    s.unique_entity_id AS uei,
    s.legal_business_name,
    s.dba_name,
    s.entity_url,
    s.physical_address_line_1,
    s.physical_address_line_2,
    s.physical_address_city,
    s.physical_address_province_or_state AS physical_state,
    s.physical_address_zippostal_code AS physical_zip,
    s.mailing_address_line_1,
    s.mailing_address_city,
    s.mailing_address_state_or_province AS mailing_state,
    s.mailing_address_zippostal_code AS mailing_zip,
    s.primary_naics,
    s.entity_structure,
    s.cage_code,
    s.govt_bus_poc_first_name,
    s.govt_bus_poc_middle_initial,
    s.govt_bus_poc_last_name,
    s.govt_bus_poc_title,
    s.govt_bus_poc_st_add_1 AS poc_address_line_1,
    s.govt_bus_poc_city AS poc_city,
    s.govt_bus_poc_state_or_province AS poc_state,
    s.govt_bus_poc_zippostal_code AS poc_zip,
    s.elec_bus_poc_first_name,
    s.elec_bus_poc_last_name,
    s.elec_bus_poc_title,
    a.total_award_actions,
    a.total_obligated_dollars,
    a.first_award_date,
    a.latest_award_date,
    a.distinct_agencies_count,
    a.distinct_naics_count,
    a.contract_count,
    a.subaward_count,
    CAST('97269e73-ec53-4a22-bd59-63bfe0583999' AS CHARACTER VARYING) AS bridge_run_id
FROM public.source_sam_entities_pocs AS s
JOIN agg AS a ON a.recipient_uei = s.unique_entity_id
WHERE s.govt_bus_poc_first_name IS NOT NULL;
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print DDL but don't apply.")
    p.add_argument("--apply", action="store_true",
                   help="Drop + rebuild source + aggregated MV.")
    args = p.parse_args()

    if not (args.dry_run or args.apply):
        p.error("specify --dry-run or --apply")

    # Step 1: introspect Parquet to get the column list + types.
    sample_uri = "s3://dex-raw-landing-zone/usaspending/contracts/year=2008/data.parquet"
    logger.info("Introspecting %s", sample_uri)
    cols = _introspect_parquet(sample_uri)
    typed_cnt = sum(1 for _, t in cols if t.upper() not in ("VARCHAR", "TEXT", "STRING"))
    logger.info("Got %d columns (%d typed)", len(cols), typed_cnt)

    source_ddl = _build_create_source_ddl(cols)

    if args.dry_run:
        print("--- CREATE SOURCE DDL ---")
        print(source_ddl)
        print("\n--- AGGREGATED MV DDL ---")
        print(AGGREGATED_MV_DDL)
        return

    # Step 2: drop dependents (cascade through source).
    logger.info("Dropping mv_audience_fed_contractors_long_tenure (was deferred)")
    _rw_psql("DROP MATERIALIZED VIEW IF EXISTS public.mv_audience_fed_contractors_long_tenure;")

    logger.info("Dropping source_usaspending_contracts_historical CASCADE")
    _rw_psql("DROP SOURCE IF EXISTS public.source_usaspending_contracts_historical CASCADE;")

    # Step 3: recreate source with typed DDL.
    logger.info("Creating source_usaspending_contracts_historical with typed DDL")
    _rw_psql(source_ddl)

    # Step 4: recreate aggregated MV.
    logger.info("Recreating mv_sam_usaspending_aggregated (BACKGROUND_DDL)")
    # Use stdin to handle the multi-statement script.
    cmd = [
        "psql",
        "-h", _required_env("RISINGWAVE_HOST"),
        "-p", _required_env("RISINGWAVE_PORT"),
        "-U", _required_env("RISINGWAVE_USER"),
        "-d", _required_env("RISINGWAVE_DATABASE"),
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    env = {**os.environ, "PGPASSWORD": _required_env("RISINGWAVE_PASSWORD")}
    proc = subprocess.run(cmd, env=env, input=AGGREGATED_MV_DDL,
                          capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode != 0:
        raise SystemExit(
            f"Aggregated MV DDL failed: {proc.stderr}\n{proc.stdout}"
        )
    logger.info("Aggregated MV DDL admitted (BACKGROUND build)")

    logger.info("Done. Run apply_usaspending_audience_mvs_rw.py --apply --skip-wait next "
                "to recreate the 8 audience MVs (long_tenure now un-deferred via YAML).")


if __name__ == "__main__":
    main()
