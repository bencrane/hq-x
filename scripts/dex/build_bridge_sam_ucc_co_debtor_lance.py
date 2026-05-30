"""SAM.gov entity × CO UCC-1 debtor Pattern B Lance bridge.

CO parity port of build_bridge_sam_ucc_ca_debtor_lance.py (the CA edition).

Pattern B exact-match bridge: SAM.gov registered entities (national —
sam_gov/entities_lance, legal_business_name + unique_entity_id, NO state
pre-filter) × CO UCC-1 debtor filings (ucc_co/debtors_lance, Organization
rows deduped to debtor-name-grain via SELECT DISTINCT debtor_name_normalized).

Method: legal_name_state_exact_co v1.0.0 (REUSED — defined by the
ppp_ucc_co_debtor bridge). This script ONLY calls register_bridge — the
method-definition helpers are intentionally not imported (reuse, not redefine).

SAM-side shape (national — NO state pre-filter):
    - Read sam_gov/entities_lance: unique_entity_id (UEI) + legal_business_name.
    - NO push-down state filter — an out-of-state SAM entity can still be a CO
      UCC debtor.
    - Normalize legal_business_name Python-side via
      _lib.entity_name_normalize.normalize_entity_name (canonical _lib v1.0.0).
    - Drop None/empty normalizations.

UCC-debtor-side shape:
    - ucc_co/debtors_lance; scanner filter DEBTOR_TYPE='Organization'.
    - Read raw ORG_NAME, normalize Python-side via _lib.entity_name_normalize.
    - Drop None/empty; SELECT DISTINCT debtor_name_normalized dedup before join.
    - After dedup ucc_fan_out ≡ 1 → silver structurally unreachable.

Fan-out asymmetry:
    sam_fan_out = COUNT(*) per normalized name (SAM entity rows per name).
    ucc_fan_out = COUNT(DISTINCT debtor_name_normalized) per name (≡ 1 post-dedup).

Tier rule: platinum=both 1; gold=exactly one 1; silver=both ≤50; rejected=either >50.

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ucc_co_debtor_lance
    (BTREE on sam_legal_name_normalized AND ucc_debtor_name_normalized)

Floor: MIN_ROWS_MATCHED — calibrated from --dry-run.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ucc_co_debtor_lance.py --apply
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

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)
from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

BRIDGE_NAME = "sam_ucc_co_debtor"
DATASET_SLUG = "sam_ucc_co_debtor_lance"
METHOD_NAME = "legal_name_state_exact_co_with_address_corroboration"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "2.0.0"

COLLISION_THRESHOLD = 50
# Calibrated from --dry-run: natural matched-row count 20,984 (14,641
# platinum + 6,343 gold; silver 0 by construction). Floor 14,000 (~67%
# of measured).
MIN_ROWS_MATCHED = 14_000

SOURCE_LEFT = "sam_gov_entities_lance"
SOURCE_RIGHT = "ucc_co_debtors_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/debtors_lance"
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ucc_co_debtor_lance"
)

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


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load SAM national entities + CO UCC debtor names into Arrow tables."""
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # SAM left side — national, no state pre-filter.
    logger.info("opening sam_gov/entities_lance (national — no state filter) ...")
    sam_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sam_tbl = sam_ds.scanner(
        columns=[
            "unique_entity_id",
            "legal_business_name",
            "physical_address_base_normalized",
            "mailing_address_base_normalized",
        ],
        filter=pc.field("legal_business_name").is_valid(),
    ).to_table()
    rows_sam_raw = len(sam_tbl)
    logger.info(
        "  sam entities_lance (legal_business_name is_valid): %d rows",
        rows_sam_raw,
    )

    sam_names = sam_tbl.column("legal_business_name").to_pylist()
    sam_normalized = [normalize_entity_name(n) for n in sam_names]
    sam_tbl = sam_tbl.append_column(
        "sam_legal_name_normalized",
        pa.array(sam_normalized, type=pa.string()),
    )
    sam_valid_mask = pc.is_valid(sam_tbl.column("sam_legal_name_normalized"))
    sam_tbl = sam_tbl.filter(sam_valid_mask)
    rows_left = len(sam_tbl)
    logger.info(
        "  sam after _lib normalize (sam_legal_name_normalized is_valid): %d rows",
        rows_left,
    )

    # UCC right side — raw ORG_NAME, normalize Python-side, drop None/empty.
    logger.info("opening ucc_co/debtors_lance (DEBTOR_TYPE='Organization') ...")
    ucc_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    ucc_tbl = ucc_ds.scanner(
        columns=["ORG_NAME", "address_base_normalized"],
        filter=pc.field("DEBTOR_TYPE") == "Organization",
    ).to_table()
    rows_ucc_raw = len(ucc_tbl)
    logger.info(
        "  ucc debtors_lance (DEBTOR_TYPE=Organization): %d rows",
        rows_ucc_raw,
    )

    org_names = ucc_tbl.column("ORG_NAME").to_pylist()
    normalized = [normalize_entity_name(n) for n in org_names]
    ucc_tbl = ucc_tbl.append_column(
        "debtor_name_normalized",
        pa.array(normalized, type=pa.string()),
    )
    valid_mask = pc.is_valid(ucc_tbl.column("debtor_name_normalized"))
    ucc_tbl = ucc_tbl.filter(valid_mask)
    logger.info(
        "  ucc after _lib normalize (debtor_name_normalized is_valid): %d rows",
        len(ucc_tbl),
    )

    return sam_tbl, ucc_tbl, rows_left, rows_ucc_raw


def _build_match_table(
    sam_tbl,
    ucc_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run dedup + exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sam", sam_tbl)
    con.register("ucc_filing", ucc_tbl)

    rows_sam_reg = con.execute("SELECT COUNT(*) FROM sam").fetchone()[0]
    rows_ucc_filing = con.execute("SELECT COUNT(*) FROM ucc_filing").fetchone()[0]
    logger.info("  registered: sam=%d  ucc_filing=%d", rows_sam_reg, rows_ucc_filing)

    # v2.0.0: collapse to debtor-name grain AND aggregate per-name UCC-address set.
    # SAM's physical/mailing is checked against this set for the address-axis tier.
    con.execute(
        """
        CREATE TEMP TABLE ucc AS
        SELECT
            debtor_name_normalized AS ucc_debtor_name_normalized,
            LIST(DISTINCT address_base_normalized) FILTER (
                WHERE address_base_normalized IS NOT NULL
            ) AS ucc_debtor_address_set
        FROM ucc_filing
        WHERE debtor_name_normalized IS NOT NULL
          AND debtor_name_normalized <> ''
        GROUP BY 1
        """
    )
    rows_ucc_deduped = con.execute("SELECT COUNT(*) FROM ucc").fetchone()[0]
    rows_ucc_distinct_check = con.execute(
        "SELECT COUNT(DISTINCT ucc_debtor_name_normalized) FROM ucc"
    ).fetchone()[0]
    logger.info(
        "  ucc after SELECT DISTINCT: %d rows (distinct check: %d)",
        rows_ucc_deduped, rows_ucc_distinct_check,
    )
    if rows_ucc_deduped != rows_ucc_distinct_check:
        raise RuntimeError(
            f"UCC dedup failed — COUNT(*) {rows_ucc_deduped} != "
            f"COUNT(DISTINCT) {rows_ucc_distinct_check}"
        )

    # Inner JOIN on normalized names; carry SAM addresses + UCC address set.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            s.unique_entity_id               AS sam_unique_entity_id,
            s.legal_business_name            AS sam_legal_business_name,
            s.sam_legal_name_normalized,
            s.physical_address_base_normalized AS sam_physical_address_base_normalized,
            s.mailing_address_base_normalized  AS sam_mailing_address_base_normalized,
            u.ucc_debtor_name_normalized,
            u.ucc_debtor_address_set,
            '{METHOD_NAME}'                  AS match_method,
            s.sam_legal_name_normalized      AS match_value,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'   AS generated_at
        FROM sam s
        JOIN ucc u
          ON s.sam_legal_name_normalized = u.ucc_debtor_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # Fan-out counts (CRITICAL asymmetry).
    #   sam_fan_out: # of SAM entity rows per normalized name.
    #   ucc_fan_out: # of distinct UCC debtor names per name (≡ 1 post-dedup).
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sam_legal_name_normalized, COUNT(*) AS sam_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ucc_fanout AS
        SELECT sam_legal_name_normalized,
               COUNT(DISTINCT ucc_debtor_name_normalized) AS ucc_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # Tier rule + address agreement + composite tier (v2.0.0).
    # silver was structurally unreachable in v1.0.0 (ucc_fan_out ≡ 1 post-dedup);
    # under v2.0.0 silver IS reachable when name=gold (1:N) + no address agreement.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sam_fan_out,
            uf.ucc_fan_out,
            CASE
              WHEN b.ucc_debtor_address_set IS NULL OR LEN(b.ucc_debtor_address_set) = 0
                THEN FALSE
              WHEN list_contains(b.ucc_debtor_address_set, b.sam_physical_address_base_normalized)
                OR list_contains(b.ucc_debtor_address_set, b.sam_mailing_address_base_normalized)
                THEN TRUE
              ELSE FALSE
            END                                                  AS address_agrees,
            NULLIF(
              CONCAT_WS('|',
                CASE WHEN b.sam_physical_address_base_normalized IS NOT NULL
                       AND list_contains(b.ucc_debtor_address_set, b.sam_physical_address_base_normalized)
                     THEN 'physical' END,
                CASE WHEN b.sam_mailing_address_base_normalized IS NOT NULL
                       AND list_contains(b.ucc_debtor_address_set, b.sam_mailing_address_base_normalized)
                     THEN 'mailing' END
              ),
              ''
            )                                                    AS address_match_path,
            CASE
              WHEN list_contains(b.ucc_debtor_address_set, b.sam_physical_address_base_normalized)
                THEN b.sam_physical_address_base_normalized
              WHEN list_contains(b.ucc_debtor_address_set, b.sam_mailing_address_base_normalized)
                THEN b.sam_mailing_address_base_normalized
              ELSE NULL
            END                                                  AS address_match_value,
            CASE
                WHEN sf.sam_fan_out > {COLLISION_THRESHOLD}
                  OR uf.ucc_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sam_fan_out = 1 AND uf.ucc_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sam_fan_out = 1 OR  uf.ucc_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                                  AS name_confidence_tier
        FROM bridge_raw b
        JOIN sam_fanout sf USING (sam_legal_name_normalized)
        JOIN ucc_fanout uf USING (sam_legal_name_normalized)
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_tiered AS
        SELECT
            b.*,
            CASE
                WHEN b.name_confidence_tier = 'rejected'                                  THEN 'rejected'
                WHEN b.name_confidence_tier = 'platinum' AND b.address_agrees             THEN 'platinum'
                WHEN b.name_confidence_tier = 'platinum' AND NOT b.address_agrees         THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND b.address_agrees             THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND NOT b.address_agrees         THEN 'silver'
                ELSE 'silver'
            END AS composite_confidence_tier
        FROM bridge_all b
        """
    )

    # Drop the LIST col before Lance write (Lance 1.5.x def-buffer cap risk per L54).
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            * EXCLUDE (ucc_debtor_address_set),
            composite_confidence_tier AS confidence_tier
        FROM bridge_tiered
        WHERE composite_confidence_tier <> 'rejected'
        """
    )

    counts_row = con.execute(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver'),
            COUNT(*) FILTER (WHERE address_agrees),
            COUNT(*) FILTER (WHERE address_match_path = 'physical'),
            COUNT(*) FILTER (WHERE address_match_path = 'mailing'),
            COUNT(*) FILTER (WHERE address_match_path = 'mailing|physical' OR address_match_path = 'physical|mailing')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_tiered WHERE composite_confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_address_agrees": counts_row[4],
        "rows_addr_physical_only": counts_row[5],
        "rows_addr_mailing_only": counts_row[6],
        "rows_addr_both": counts_row[7],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        try:
            ds.create_scalar_index(
                "sam_legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on sam_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sam_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index(
                "ucc_debtor_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on ucc_debtor_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on ucc_debtor_name_normalized FAILED: %s", e)
            raise
        for col in ("composite_confidence_tier", "address_agrees"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE on %s: OK", col)
            except Exception as e:
                logger.warning("BTREE on %s failed (non-fatal): %s", col, e)

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def _ensure_registry() -> None:
    # v2.0.0 — CO-scoped composite method
    # legal_name_state_exact_co_with_address_corroboration v1.0.0.
    # Shared idempotently with ppp_ucc_co_debtor; either script's UPSERT is safe.
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Composite legal-name + state exact match with address corroboration. "
            "CO-state-scoped sibling of legal_name_state_exact_ca_with_address_"
            "corroboration. Inner-joins on exact-equality normalized legal name "
            "(CO-state constrained via input selection); then checks whether "
            "LEFT's normalized physical address (base form, unit-stripped via "
            "_lib/address_normalize) equals ANY of RIGHT's address roles. "
            "Address agreement promotes the name-axis fan-out tier: platinum "
            "requires BOTH 1:1 name AND address corroboration."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py + _lib/address_normalize.py",
        normalizer_version=f"name v{NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        blacklist_module="(same as component normalizers)",
        blacklist_version=f"name v{NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        tier_rule_description=(
            "Name fan-out tier (1:1=platinum, 1:N|N:1=gold, N:M<=50=silver, "
            ">50=rejected) preserved as `name_confidence_tier`. Composite tier "
            "(in canonical `confidence_tier`): platinum REQUIRES name 1:1 AND "
            "address_agrees; absence demotes the name-axis platinum to gold "
            "and gold to silver."
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=[
            "sam_legal_name_normalized",
            "physical_address_base_normalized",
            "mailing_address_base_normalized",
        ],
        input_columns_right=[
            "ucc_debtor_name_normalized", "address_base_normalized",
        ],
        output_value_description=(
            "(sam_unique_entity_id, ucc_debtor_name_normalized) name-grain pair "
            "with boolean address_agrees + address_match_path (which SAM-side "
            "address role matched against the UCC per-name address set) + "
            "composite_confidence_tier."
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SAM.gov registered entities (national — no state pre-filter) × CO UCC-1 "
            "debtor filings (aggregated to debtor-name-grain with per-name address "
            "set) — v2.0.0 composite name+address axis. CO sibling of "
            "sam_ucc_ca_debtor_lance (PR #805). SAM's physical or mailing "
            "normalized address must appear in the UCC debtor name's per-filing "
            "address set for the address axis to agree."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + ledger row")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s  name_norm=v%s  addr_norm=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, ADDR_NORMALIZER_VERSION,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        sam_tbl, ucc_tbl, rows_left, rows_ucc_raw = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sam_tbl, ucc_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge composite tier distribution (v2.0.0 — address-axis):")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum:               %d  (name 1:1 + address agrees)", counts["rows_tier1"])
        logger.info("    gold:                   %d  (name 1:1 OR (name 1:N|N:1 + address agrees))", counts["rows_tier2"])
        logger.info("    silver:                 %d  (residual — v2.0.0: name 1:N|N:1 + no address agreement)", counts["rows_tier3"])
        logger.info("  address_agrees:           %d  (any-role agreement)", counts["rows_address_agrees"])
        logger.info("    physical only:          %d", counts["rows_addr_physical_only"])
        logger.info("    mailing only:           %d", counts["rows_addr_mailing_only"])
        logger.info("    both:                   %d", counts["rows_addr_both"])
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info("DRY RUN — no Lance / Postgres writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_ucc_raw,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_address_agrees": counts["rows_address_agrees"],
                "rows_addr_physical_only": counts["rows_addr_physical_only"],
                "rows_addr_mailing_only": counts["rows_addr_mailing_only"],
                "rows_addr_both": counts["rows_addr_both"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK — run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        logger.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, repr(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
