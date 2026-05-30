"""SBA NY borrowers × NY MTA Procurements — Pattern B REUSER bridge (Lance).

Pattern B exact-match bridge: SBA NY borrowers (borrstate='NY') × MTA
procurements (vendor_state='NY') on normalized name.

Method: legal_name_state_exact_ny v1.0.0 (REUSED). FOURTH REUSER. Per L21,
register_match_method[_version] helpers INTENTIONALLY OMITTED.

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 100 (MTA scope is narrower than other NY contracting bodies;
expect ~few-hundred matches given ~107K MTA rows, of which NY-vendor subset
is typically half-or-less).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance
        (borrstate='NY'; DISTINCT legal_name_normalized.)
    s3://dex-raw-landing-zone/polaris-warehouse/nystate/mta_procurements_lance
        (filter vendor_state='NY' AND vendor_name_normalized is_valid.)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_mta_lance
    Dual BTREE: sba_legal_name_normalized + contract_id.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import __version__ as NORMALIZER_VERSION
from scripts._lib.lance_commit_lock import lance_commit_lock
# L21 REUSER: NO register_match_method[_version].
from scripts._lib.match_method_registry import (
    complete_bridge_run, fail_bridge_run, register_bridge, start_bridge_run,
)

BRIDGE_NAME = "sba_ny_mta"
DATASET_SLUG = "sba_ny_mta_lance"
METHOD_NAME = "legal_name_state_exact_ny"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 100

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "ny_mta_procurements_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/nystate/mta_procurements_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_mta_lance"

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout)


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _ensure_db_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict) -> tuple:
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening sba/borrowers_lance (borrstate=NY) ...")
    sba_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sba_raw = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter=pc.field("borrstate") == "NY",
    ).to_table()
    logger.info("  sba borrowers_lance (NY): %d rows", len(sba_raw))

    names = sba_raw.column("legal_name_normalized").to_pylist()
    distinct_sba: set[str] = {n for n in names if isinstance(n, str) and len(n) >= 2}
    del sba_raw, names

    sba_branded_arrow = pa.table({
        "sba_legal_name_normalized": pa.array(sorted(distinct_sba), type=pa.string()),
    })
    rows_sba_distinct = len(sba_branded_arrow)
    del distinct_sba
    logger.info("  sba_branded (distinct NY names): %d", rows_sba_distinct)

    logger.info("opening nystate/mta_procurements_lance (vendor_state=NY) ...")
    mta_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    mta_filter = (
        (pc.field("state") == "NY")
        & pc.field("vendor_name_normalized").is_valid()
    )
    mta_tbl = mta_ds.scanner(
        columns=[
            "vendor_name_normalized",
            "contract_id",
            "transaction_number",
            "fiscal_year_end_date",
            "procurement_description",
            "type_of_procurement",
            "award_process",
            "award_date",
            "end_date",
            "contract_amount",
            "vendor_is_a_mwbe",
        ],
        filter=mta_filter,
    ).to_table()
    logger.info("  mta_procurements_lance (vendor_state=NY): %d rows", len(mta_tbl))

    return sba_branded_arrow, mta_tbl, rows_sba_distinct, len(mta_tbl)


def _build_match_table(sba_tbl, mta_tbl, *, bridge_run_id: str, generated_at_iso: str) -> tuple:
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba", sba_tbl)
    con.register("mta", mta_tbl)
    logger.info("  registered: sba=%d  mta=%d",
                con.execute("SELECT COUNT(*) FROM sba").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM mta").fetchone()[0])

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            sba.sba_legal_name_normalized,
            mta.contract_id,
            mta.transaction_number                   AS mta_transaction_number,
            mta.fiscal_year_end_date                 AS mta_fiscal_year_end_date,
            mta.procurement_description              AS mta_procurement_description,
            mta.type_of_procurement                  AS mta_type_of_procurement,
            mta.award_process                        AS mta_award_process,
            mta.award_date                           AS mta_award_date,
            mta.end_date                             AS mta_end_date,
            mta.contract_amount                      AS mta_contract_amount,
            mta.vendor_is_a_mwbe                     AS mta_vendor_is_a_mwbe,
            '{METHOD_NAME}'                          AS match_method,
            sba.sba_legal_name_normalized            AS match_value_normalized,
            '{BRIDGE_VERSION}'                       AS bridge_version,
            '{bridge_run_id}'                        AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'           AS generated_at
        FROM sba
        JOIN mta
          ON sba.sba_legal_name_normalized = mta.vendor_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    con.execute(
        """
        CREATE TEMP TABLE sba_fanout AS
        SELECT sba_legal_name_normalized, COUNT(*) AS sba_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE mta_fanout AS
        SELECT sba_legal_name_normalized,
               COUNT(DISTINCT contract_id) AS mta_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sba_fan_out,
            mf.mta_fan_out,
            CASE
                WHEN sf.sba_fan_out > {COLLISION_THRESHOLD}
                  OR mf.mta_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND mf.mta_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR  mf.mta_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN sba_fanout sf USING (sba_legal_name_normalized)
        JOIN mta_fanout mf USING (sba_legal_name_normalized)
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
        """
    )

    counts_row = con.execute(
        """
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
               COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
               COUNT(*) FILTER (WHERE confidence_tier = 'silver')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    return con, {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_collision_rejected": rejected,
    }


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(reader, BRIDGE_LANCE_URI, mode="overwrite", storage_options=storage_options)
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, time.time() - t0, ds.version)

        try:
            ds.create_scalar_index("sba_legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on sba_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sba_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("contract_id", index_type="BTREE", replace=True)
            logger.info("BTREE on contract_id: OK")
        except Exception as e:
            logger.error("BTREE on contract_id FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def main() -> int:
    parser = argparse.ArgumentParser(description="SBA NY × NY MTA Procurements Pattern B REUSER bridge.")
    parser.add_argument("--apply", action="store_true", default=False)
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #513)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )

    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA NY borrowers x NY MTA Procurements (vendor_state='NY') — "
            "legal-name exact match. Reuses legal_name_state_exact_ny v1.0.0 "
            "(publisher PR #513). FOURTH REUSER of method. Dual BTREE on "
            "sba_legal_name_normalized + contract_id."
        ),
    )
    run_uuid = start_bridge_run(
        bridge_name=BRIDGE_NAME, method_semver=METHOD_SEMVER, bridge_version=BRIDGE_VERSION,
        source_left=SOURCE_LEFT, source_right=SOURCE_RIGHT,
        match_method=METHOD_NAME, r2_output_key=BRIDGE_LANCE_URI,
    )
    bridge_run_id = str(run_uuid)
    logger.info("bridge_run_id=%s", bridge_run_id)

    if not args.apply:
        msg = "dry-run; no Lance write (pass --apply to execute)"
        logger.info("DRY-RUN: %s", msg)
        fail_bridge_run(run_uuid, msg)
        return 0

    try:
        sba_tbl, mta_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sba_tbl, mta_tbl, bridge_run_id=bridge_run_id, generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info("    silver   (N:M <= %d):   %d", COLLISION_THRESHOLD, counts["rows_tier3"])
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = f"HARD FAIL: rows_matched={counts['rows_matched']:,} < floor={MIN_ROWS_MATCHED:,}"
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(run_uuid, metrics={
            "rows_left": rows_left, "rows_right": rows_right,
            "rows_matched": counts["rows_matched"],
            "rows_tier1": counts["rows_tier1"], "rows_tier2": counts["rows_tier2"],
            "rows_tier3": counts["rows_tier3"],
            "rows_collision_rejected": counts["rows_collision_rejected"],
            "lance_rows": lance_count,
        })
        logger.info("OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
                    bridge_run_id, lance_count, time.time() - t0)
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
