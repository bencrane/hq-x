"""SBA NY borrowers × NYC Recent Contract Awards — Pattern B REUSER bridge (Lance).

Pattern B exact-match bridge: SBA 7(a)/504 borrowers (legal_name_normalized,
filtered to borrstate='NY') × NYC awarded contracts (filtered to
type_of_notice_description='Award'). State-pure on LEFT (SBA's borrstate filter);
NYC contracts can have non-NY vendors, but the bridge only retains rows whose
name matches an SBA NY borrower's legal_name_normalized — the state-implicit
constraint flows through the LEFT spine.

Method: legal_name_state_exact_ny v1.0.0 (REUSED — publisher: PR #513). This
is the THIRD REUSER (after sba_ny_usaspending + sba_ny_sam in PR #514). Per
L21, the register_match_method[_version] helpers are INTENTIONALLY OMITTED.

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (symmetric two-sided per PR #466/#482/#487).
MIN_ROWS_MATCHED = 200 (conservative — NYC awards dataset is ~52K rows total,
fewer after Award-only filter; expect ~few-hundred matches given the ~50K
distinct NYC contract vendors vs 622K SBA NY borrowers via name-state-exact).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance
        (filter borrstate='NY'; DISTINCT legal_name_normalized post-filter
        Python-side; OOM-resistant.)
    s3://dex-raw-landing-zone/polaris-warehouse/nyc/contract_awards_lance
        (filter type_of_notice_description='Award' on Lance scanner; both
        sides will share normalized name keys.)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_nyc_contracts_lance
    (BTREE on sba_legal_name_normalized AND contract_id; dual-BTREE per
    PR #466/#482/#487 precedent.)

Match method REUSE (L21):
    register_bridge                          -> ops.bridges
    start_bridge_run                         -> ops.bridge_generation_runs (running)
    write Lance + dual BTREE + tier counts
    complete_bridge_run                      -> status=completed
    fail_bridge_run (on error or dry-run)    -> status=failed

Tier rule (symmetric two-sided):
    platinum = BOTH fan_out == 1
    gold     = ONE side fan_out == 1
    silver   = BOTH fan_out <= 50
    rejected = EITHER fan_out >  50

Plain Python (NOT Modal):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_sba_ny_nyc_contracts_lance.py --apply
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

from scripts._lib.entity_name_normalize import (
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL L21: method + method-version helpers INTENTIONALLY OMITTED (REUSER).
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

BRIDGE_NAME = "sba_ny_nyc_contracts"
DATASET_SLUG = "sba_ny_nyc_contracts_lance"
METHOD_NAME = "legal_name_state_exact_ny"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 200

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "nyc_contract_awards_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/nyc/contract_awards_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_nyc_contracts_lance"

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


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
    """Load SBA NY borrowers + NYC awarded contracts into Arrow tables.

    SBA side: read [legal_name_normalized, borrstate]; filter borrstate='NY';
    Python-side distinct.

    NYC side: read [vendor_name_normalized, contract_id, pin, agency_name,
    type_of_notice_description, category_description, short_title,
    contract_amount, start_date, end_date]; filter
    type_of_notice_description='Award' AND vendor_name_normalized is_valid.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening sba/borrowers_lance (borrstate=NY filter) ...")
    sba_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sba_raw = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter=pc.field("borrstate") == "NY",
    ).to_table()
    logger.info("  sba borrowers_lance (borrstate=NY): %d rows", len(sba_raw))

    names = sba_raw.column("legal_name_normalized").to_pylist()
    distinct_sba: set[str] = {n for n in names if isinstance(n, str) and len(n) >= 2}
    del sba_raw, names

    sba_branded_arrow = pa.table({
        "sba_legal_name_normalized": pa.array(sorted(distinct_sba), type=pa.string()),
    })
    rows_sba_distinct = len(sba_branded_arrow)
    del distinct_sba
    logger.info("  sba_branded (distinct legal_name_normalized, NY): %d names", rows_sba_distinct)

    logger.info("opening nyc/contract_awards_lance (Award filter) ...")
    nyc_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    nyc_filter = (
        (pc.field("type_of_notice_description") == "Award")
        & pc.field("vendor_name_normalized").is_valid()
    )
    nyc_tbl = nyc_ds.scanner(
        columns=[
            "vendor_name_normalized",
            "contract_id",
            "pin",
            "agency_name",
            "type_of_notice_description",
            "category_description",
            "short_title",
            "contract_amount",
            "start_date",
            "end_date",
        ],
        filter=nyc_filter,
    ).to_table()
    logger.info("  nyc contract_awards_lance (Award; valid name): %d rows", len(nyc_tbl))

    return sba_branded_arrow, nyc_tbl, rows_sba_distinct, len(nyc_tbl)


def _build_match_table(
    sba_tbl, nyc_tbl, *, bridge_run_id: str, generated_at_iso: str,
) -> tuple:
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba", sba_tbl)
    con.register("nyc", nyc_tbl)

    logger.info("  registered: sba=%d  nyc=%d",
                con.execute("SELECT COUNT(*) FROM sba").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM nyc").fetchone()[0])

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            sba.sba_legal_name_normalized,
            nyc.contract_id,
            nyc.pin                                  AS nyc_pin,
            nyc.agency_name                          AS nyc_agency_name,
            nyc.category_description                 AS nyc_category_description,
            nyc.short_title                          AS nyc_short_title,
            nyc.contract_amount                      AS nyc_contract_amount,
            nyc.start_date                           AS nyc_start_date,
            nyc.end_date                             AS nyc_end_date,
            '{METHOD_NAME}'                          AS match_method,
            sba.sba_legal_name_normalized            AS match_value_normalized,
            '{BRIDGE_VERSION}'                       AS bridge_version,
            '{bridge_run_id}'                        AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'           AS generated_at
        FROM sba
        JOIN nyc
          ON sba.sba_legal_name_normalized = nyc.vendor_name_normalized
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
        CREATE TEMP TABLE nyc_fanout AS
        SELECT sba_legal_name_normalized,
               COUNT(DISTINCT contract_id) AS nyc_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sba_fan_out,
            nf.nyc_fan_out,
            CASE
                WHEN sf.sba_fan_out > {COLLISION_THRESHOLD}
                  OR nf.nyc_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND nf.nyc_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR  nf.nyc_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN sba_fanout sf USING (sba_legal_name_normalized)
        JOIN nyc_fanout nf USING (sba_legal_name_normalized)
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
        ds = lance.write_dataset(
            reader, BRIDGE_LANCE_URI, mode="overwrite", storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

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
    parser = argparse.ArgumentParser(
        description="SBA NY × NYC Recent Contract Awards Pattern B REUSER bridge."
    )
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
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA NY borrowers x NYC Recent Contract Awards (filter "
            "type_of_notice_description='Award') — legal-name exact match. "
            "Reuses legal_name_state_exact_ny v1.0.0 (publisher PR #513). "
            "THIRD REUSER of method (after sba_ny_usaspending + sba_ny_sam). "
            "Dual BTREE on sba_legal_name_normalized + contract_id "
            "(synthetic sha1[:16] derived from pin+agency+vendor+amount+start_date)."
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
        sba_tbl, nyc_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sba_tbl, nyc_tbl, bridge_run_id=bridge_run_id, generated_at_iso=started_at.isoformat(),
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
        logger.info(
            "OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
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
