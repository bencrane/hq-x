"""DuckDB-on-R2 bridge generator: SAM.gov archived Award Notices ↔ SAM
Entity registry, via NORMALIZED AWARDEE NAME (strict fan-out).

The bulk Contract Opportunities CSV does NOT carry UEI. For Award Notices
that have already archived (no longer reachable via the v2 search API per
its "latest active version only" semantics), the only path to the awardee
UEI is fuzzy name normalization. This bridge is that path.

**Why name-only not name+state:** 83% of archived Award Notices have NULL
`pop_state` — SAM doesn't reliably populate place-of-performance state on
historical archives, and the bulk CSV carries no field for the awardee's
own HQ state (only the buyer org's state, which doesn't help). Name-only
match is forced by the data; we mitigate false positives via a strict
collision threshold (5 instead of the usual 50).

Logic (Pattern B per DATA-FACTORY-ARCHITECTURE-PATTERNS.md):
  1. REUSE existing `name_state_exact` v1.0.0 method (registered by FEC×SBA
     bridge — per L21).
  2. Register new bridge `sam_opps_archived_sam_entities_namepopstate`
     v1.0.0 (idempotent UPSERT).
  3. Read R2 archived SAM opps Parquets (filter to Award Notices with
     non-empty awardee).
  4. Read R2 SAM Entity Parquet (latest snapshot under sam-gov/monthly/).
  5. Normalize awardee + legal_business_name via py_normalize_entity UDF
     (per L34).
  6. INNER JOIN on (name_normalized, state) — SAM opps `pop_state` joined
     to SAM Entity `physical_address_province_or_state`. Note: pop_state
     is the place-of-performance, NOT the awardee's HQ state — accept
     state-mismatch noise; downstream consumers filter by tier.
  7. Compute fan-out per match key for confidence tier (per L13):
        platinum (1:1), gold (1:N or N:1), silver (N:M ≤ 50), reject (>50).
  8. Write Parquet → bridges/sam_opps_archived_sam_entities_namepopstate/
     snapshot=YYYY-MM-DD/data.parquet with bridge_run_id per row.
  9. HARD FAIL if rows_matched < 10_000 (validation floor — there should
     be tens of thousands of name-matchable awardees).

Inputs:
  SAM opps archived: r2://dex-raw-landing-zone/sam-gov-opps/archived/fy=*/snapshot=*/*.parquet
  SAM Entity:        r2://dex-raw-landing-zone/sam-gov/monthly/{latest}/part-*.parquet

Output:
  r2://dex-raw-landing-zone/bridges/sam_opps_archived_sam_entities_namepopstate/
    snapshot=YYYY-MM-DD/data.parquet

Usage:
  cd ~/hq-all && doppler run --project hq-all --config prd --command \\
    'uv run --with duckdb --with "psycopg[binary]" --with boto3 python \\
     apps/data-engine-x/scripts/build_bridge_sam_opps_archived_sam_entities.py --dry-run'

  cd ~/hq-all && doppler run --project hq-all --config prd --command \\
    'uv run --with duckdb --with "psycopg[binary]" --with boto3 python \\
     apps/data-engine-x/scripts/build_bridge_sam_opps_archived_sam_entities.py --apply'

See directive ~/Desktop/hq/directives/2026-05-09-sam-gov-contract-opportunities-ingest-plan.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    normalize_entity_name,
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_sam_opps_archived_sam_entities")


# Bridge identity
BRIDGE_NAME = "sam_opps_archived_sam_entities_name_strict"
METHOD_NAME = "name_exact_strict"
METHOD_SEMVER = "1.0.0"
SOURCE_LEFT = "source_sam_opps_archived"
SOURCE_RIGHT = "source_sam_entities"

# R2 layout
R2_BUCKET = "dex-raw-landing-zone"
SAM_OPPS_ARCHIVED_GLOB = "sam-gov-opps/archived/fy=*/snapshot=*/*.parquet"
SAM_ENTITY_LATEST_GLOB = "sam-gov/monthly/2026-05-03/*.parquet"  # latest snapshot
BRIDGE_OUTPUT_PREFIX = "bridges/sam_opps_archived_sam_entities_name_strict"

# Tier thresholds — without state filter, fan-out is more dangerous on the
# B (SAM Entity) side (multiple Entities for same name = collision). Side A
# fan-out (multiple Award Notices for same name) is just "established
# federal contractor" — not a quality signal. Asymmetric thresholds:
#   side A (Award Notices): allow up to 1000 (Boeing has thousands)
#   side B (SAM Entity):    cap at 5 (any name with >5 SAM Entities is ambiguous)
COLLISION_THRESHOLD_LEFT = 1000   # SAM opps notice_count_at_key
COLLISION_THRESHOLD_RIGHT = 5     # SAM Entity uei_count_at_key
MIN_ROWS_MATCHED = 50_000


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    return endpoint.split("//")[-1].split(".")[0]


def _connect_duckdb_to_r2():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id_from_endpoint(os.environ["R2_ENDPOINT"])}'
        );
        """
    )
    return con


def _register(dry_run: bool) -> tuple[str, str, str]:
    """Returns (method_id, version_id, bridge_id) — UUIDs as strings."""
    if dry_run:
        logger.info("DRY-RUN: skipping registry UPSERTs")
        return ("00000000-0000-0000-0000-000000000000",) * 3
    method_id = register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality match on normalized entity name only, with "
            "strict fan-out threshold. For sources lacking reliable state "
            "data on either side."
        ),
    )
    version_id = register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="scripts/_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module=None,
        blacklist_version=None,
        tier_rule_description=(
            f"asymmetric: platinum=A=1+B=1, gold=B=1, silver=B<=2; "
            f"reject A>{COLLISION_THRESHOLD_LEFT} or B>{COLLISION_THRESHOLD_RIGHT}. "
            "B-side strictness reflects name-collision risk in SAM Entity; "
            "A-side looseness reflects 'established federal contractor' bias."
        ),
        rejection_rule_description=(
            f"fan-out > {COLLISION_THRESHOLD_LEFT} on left (SAM opps) "
            f"OR > {COLLISION_THRESHOLD_RIGHT} on right (SAM Entity)"
        ),
        input_columns_left=["awardee"],
        input_columns_right=["legal_business_name"],
        output_value_description=(
            "name_normalized — passed through "
            "_lib/entity_name_normalize.normalize_entity_name v" + NORMALIZER_VERSION
        ),
    )
    bridge_id = register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_OUTPUT_PREFIX + "/",
        description=(
            "SAM.gov archived Award Notices (notice_id grain) joined to SAM "
            "Entity registry (uei grain) via normalized awardee name + "
            "place-of-performance state. Adds UEI / legal_business_name / "
            "cage_code / entity_structure to historical Award Notices that "
            "the SAM API can no longer enrich (since v2 search returns only "
            "active versions)."
        ),
    )
    logger.info(
        "registry: method=%s/%s version=%s bridge=%s",
        METHOD_NAME, METHOD_SEMVER, version_id, bridge_id,
    )
    return (method_id, version_id, bridge_id)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not (args.dry_run or args.apply):
        p.error("specify --dry-run or --apply")

    method_id, version_id, bridge_id = _register(args.dry_run)

    snapshot_date = datetime.now(timezone.utc).date()
    output_key = f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot_date.isoformat()}/data.parquet"

    if args.apply:
        bridge_run_id = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=METHOD_SEMVER,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=output_key,
        )
        logger.info("started bridge_run_id=%s", bridge_run_id)
    else:
        bridge_run_id = "00000000-0000-0000-0000-000000000001"

    try:
        con = _connect_duckdb_to_r2()

        # Register Python normalizer as DuckDB UDF (per L34). null_handling
        # must be 'special' because the normalizer returns None for generic
        # strings (per L33) and DuckDB's default rejects UDFs returning None.
        con.create_function(
            "py_normalize_entity",
            normalize_entity_name,
            ["VARCHAR"],
            "VARCHAR",
            null_handling="special",
        )

        # ── Side A: archived SAM opps Award Notices ──
        logger.info("loading SAM opps archived (Award Notices only)...")
        con.execute(
            f"""
            CREATE TABLE side_a AS
            SELECT
              notice_id,
              award_number,
              award_amount,
              award_date,
              awardee,
              py_normalize_entity(awardee) AS awardee_normalized,
              pop_state,
              naics_code,
              department_agency,
              sub_tier,
              office,
              posted_date
            FROM read_parquet(
              'r2://{R2_BUCKET}/{SAM_OPPS_ARCHIVED_GLOB}',
              union_by_name = TRUE
            )
            WHERE notice_type = 'Award Notice'
              AND awardee IS NOT NULL
              AND TRIM(awardee) <> ''
              AND py_normalize_entity(awardee) IS NOT NULL
            """
        )
        n_a = con.execute("SELECT count(*) FROM side_a").fetchone()[0]
        logger.info("  side_a (archived Award Notices): %s rows", f"{n_a:,}")

        # ── Side B: SAM Entity ──
        logger.info("loading SAM Entity registry...")
        con.execute(
            f"""
            CREATE TABLE side_b AS
            SELECT
              unique_entity_id AS uei,
              legal_business_name,
              py_normalize_entity(legal_business_name) AS name_normalized,
              UPPER(TRIM(physical_address_province_or_state)) AS sam_state,
              cage_code,
              entity_structure,
              physical_address_city,
              physical_address_zippostal_code
            FROM read_parquet('r2://{R2_BUCKET}/{SAM_ENTITY_LATEST_GLOB}')
            WHERE legal_business_name IS NOT NULL
              AND py_normalize_entity(legal_business_name) IS NOT NULL
            """
        )
        n_b = con.execute("SELECT count(*) FROM side_b").fetchone()[0]
        logger.info("  side_b (SAM Entity): %s rows", f"{n_b:,}")

        # ── Fan-out per name_normalized (NAME ONLY — no state) ──
        logger.info("computing fan-out per match key (name only)...")
        con.execute(
            """
            CREATE TABLE fanout AS
            SELECT
              a.awardee_normalized AS name_normalized,
              count(DISTINCT a.notice_id)  AS a_count,
              count(DISTINCT b.uei)        AS b_count
            FROM side_a a JOIN side_b b
              ON a.awardee_normalized = b.name_normalized
            GROUP BY 1
            """
        )
        n_keys = con.execute("SELECT count(*) FROM fanout").fetchone()[0]
        logger.info("  unique match keys: %s", f"{n_keys:,}")

        # ── Final inner-join + tier classify + reject collisions ──
        logger.info("building bridge rows + tier classification...")
        con.execute(
            f"""
            CREATE TABLE bridge_rows AS
            SELECT
              ?::UUID                       AS bridge_run_id,
              a.notice_id,
              b.uei,
              a.awardee                     AS awardee_raw,
              a.awardee_normalized          AS match_value_name,
              a.pop_state                   AS pop_state,
              b.sam_state                   AS sam_state,
              b.legal_business_name         AS sam_legal_business_name,
              b.cage_code                   AS sam_cage_code,
              b.entity_structure            AS sam_entity_structure,
              b.physical_address_city       AS sam_city,
              b.physical_address_zippostal_code AS sam_zip,
              a.award_number,
              a.award_amount,
              a.award_date,
              a.naics_code,
              a.department_agency,
              a.posted_date,
              CASE
                WHEN f.b_count = 1 AND f.a_count = 1 THEN 'platinum'
                WHEN f.b_count = 1                   THEN 'gold'
                WHEN f.b_count <= 2                  THEN 'silver'
                ELSE 'rejected'
              END                           AS confidence_tier,
              f.a_count                     AS notice_count_at_key,
              f.b_count                     AS uei_count_at_key,
              now()                         AS generated_at
            FROM side_a a
            JOIN side_b b
              ON a.awardee_normalized = b.name_normalized
            JOIN fanout f
              ON f.name_normalized = a.awardee_normalized
            WHERE NOT (
              f.a_count > {COLLISION_THRESHOLD_LEFT} OR f.b_count > {COLLISION_THRESHOLD_RIGHT}
            )
            """,
            [bridge_run_id],
        )
        # Metrics
        metrics_row = con.execute(
            """
            SELECT
              count(*)                                 AS rows_matched,
              count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
              count(*) FILTER (WHERE confidence_tier = 'gold')     AS rows_tier2,
              count(*) FILTER (WHERE confidence_tier = 'silver')   AS rows_tier3,
              count(DISTINCT notice_id)                AS distinct_notice_ids,
              count(DISTINCT uei)                      AS distinct_ueis
            FROM bridge_rows
            """
        ).fetchone()
        rows_matched = int(metrics_row[0])
        rows_t1 = int(metrics_row[1])
        rows_t2 = int(metrics_row[2])
        rows_t3 = int(metrics_row[3])
        distinct_notice_ids = int(metrics_row[4])
        distinct_ueis = int(metrics_row[5])

        # Rejected count (rows that hit the JOIN but exceeded collision threshold)
        rejected_row = con.execute(
            f"""
            SELECT count(*)
            FROM side_a a
            JOIN side_b b
              ON a.awardee_normalized = b.name_normalized
            JOIN fanout f
              ON f.name_normalized = a.awardee_normalized
            WHERE f.a_count > {COLLISION_THRESHOLD_LEFT} OR f.b_count > {COLLISION_THRESHOLD_RIGHT}
            """
        ).fetchone()
        rows_rejected = int(rejected_row[0])

        logger.info(
            "matched=%s | tier1=%s tier2=%s tier3=%s | rejected=%s | "
            "distinct_notice_ids=%s distinct_ueis=%s",
            f"{rows_matched:,}", f"{rows_t1:,}", f"{rows_t2:,}", f"{rows_t3:,}",
            f"{rows_rejected:,}", f"{distinct_notice_ids:,}", f"{distinct_ueis:,}",
        )

        if rows_matched < MIN_ROWS_MATCHED:
            raise RuntimeError(
                f"validation floor FAILED: rows_matched={rows_matched} < "
                f"MIN_ROWS_MATCHED={MIN_ROWS_MATCHED}"
            )

        if args.dry_run:
            logger.info("DRY-RUN: not writing Parquet; not completing bridge run")
            return

        # Write Parquet to R2 directly via DuckDB COPY
        out_url = f"r2://{R2_BUCKET}/{output_key}"
        logger.info("writing bridge Parquet to %s", out_url)
        con.execute(
            f"""
            COPY (SELECT * FROM bridge_rows)
            TO '{out_url}' (
              FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 3,
              ROW_GROUP_SIZE 100000
            );
            """
        )
        # Get bytes by re-heading the object
        import boto3
        s3 = boto3.client(
            "s3", endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        head = s3.head_object(Bucket=R2_BUCKET, Key=output_key)
        bytes_out = int(head["ContentLength"])
        logger.info("wrote %s bytes to %s", f"{bytes_out:,}", output_key)

        complete_bridge_run(
            bridge_run_id=bridge_run_id,
            metrics={
                "rows_left": n_a,
                "rows_right": n_b,
                "rows_matched": rows_matched,
                "rows_tier1": rows_t1,
                "rows_tier2": rows_t2,
                "rows_tier3": rows_t3,
                "rows_collision_rejected": rows_rejected,
                "distinct_notice_ids": distinct_notice_ids,
                "distinct_ueis": distinct_ueis,
                "r2_output_key": output_key,
                "r2_output_bytes": bytes_out,
            },
        )
        logger.info("bridge run completed: %s", bridge_run_id)

    except Exception as exc:
        if args.apply:
            try:
                fail_bridge_run(bridge_run_id, error_message=str(exc)[:4000])
            except Exception:
                logger.exception("ALSO failed to mark bridge run as failed")
        raise


if __name__ == "__main__":
    main()
