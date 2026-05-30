"""CSLB × CA SoS principal cohort — Pattern A enriched-cohort emit (Lance).

JOIN bridges.cslb_sos_ca_owner_lance (PR #482) × sos.ca_principals_lance (PR #464)
on sos_entity_num = entity_num, producing per-row (CSLB licensee × matched SoS
entity × principal of that entity). Silver tier INCLUDED — consumers filter by
cslb_sos_match_tier at query time.

NOT a new identity bridge — per DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A
enriched-cohort emit" + PR #469 precedent (sam_pdl_usaspending_lance):
  - Pattern A: no match_method registry calls (enriched-cohort discipline)
  - Pattern A: no ops.bridges row, no ops.bridge_generation_runs row
  - YES ops.data_sources row (Lance dataset still catalogs as a data source)
  - YES per-row provenance: inherited cslb_sos_bridge_run_id (from upstream bridge's
    per-row bridge_run_id column) + this emit's own bridge_run_id UUID

Pattern A structural template: PR #469 (build_bridge_sam_pdl_usaspending_lance.py)
Local-runner CLI template: PR #482 (build_bridge_cslb_sos_ca_owner_lance.py)

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_owner_lance
        (PR #482, 201,716 rows; bridge_run_id column = canonical provenance source)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_principals_lance
        (PR #464, 18,670,722 rows; entity_num = join key)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_lance
    (~617K expected; dual BTREE on cslb_license_no + sos_entity_num)

Validator probe (2026-05-17T19:55Z):
    Total INNER JOIN (all 3 tiers): 616,622
    platinum: 344,123  gold: 29,789  silver: 242,710

Silver tier INCLUDED (audit decision):
    All 3 tiers emitted. Per-row cslb_sos_match_tier + cslb_fan_out + sos_fan_out
    propagated so downstream consumers filter at query time. Excluding silver would
    lose ~242,710 rows (39%) of optionality.

Run (apply):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_cslb_sos_ca_principal_lance.py --apply

Dry-run (print row count + tier breakdown only):
    uv run python scripts/build_bridge_cslb_sos_ca_principal_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 fix — allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants (load-bearing — greps in verify-s1.sh must match exactly)
# ---------------------------------------------------------------------------

DATASET_SLUG = "cslb_sos_ca_principal_lance"
BRIDGE_VERSION = "1.0.0"

# Validator probe 2026-05-17T19:55Z: 616,622 INNER JOIN rows (all 3 tiers):
#   platinum=344,123  gold=29,789  silver=242,710
# Floor at 500,000 gives ~19% headroom below probe (catches catastrophic data
# loss without false-tripping on small upstream drift). Audit decision: include
# silver; MIN_ROWS_MATCHED = 500_000 (per verify-s1.sh L24 + verify-s3.sh L46).
MIN_ROWS_MATCHED = 500_000

UPSTREAM_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_owner_lance"
UPSTREAM_PRINCIPALS_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_principals_lance"
OUTPUT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_lance"

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
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def main() -> int:
    """CSLB × CA SoS principal cohort Pattern A enriched-cohort emit."""
    parser = argparse.ArgumentParser(
        description="CSLB × CA SoS principal cohort — Pattern A enriched-cohort emit (Lance)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the Lance dataset. Without this flag runs in dry-run mode (row-count only).",
    )
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TMPDIR", TMP_DIR)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")

    import duckdb
    import lance
    import pyarrow.compute as pc

    storage_options = _storage_options()

    # Per-emit provenance (validator p2 — own UUID separate from inherited upstream).
    BRIDGE_RUN_ID = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc)
    generated_at_iso = generated_at.isoformat()

    logger.info(
        "emit %s starting at %s (output=%s) apply=%s",
        BRIDGE_RUN_ID, generated_at_iso, OUTPUT_LANCE_URI, args.apply,
    )

    # ---- Step 1: PyLance scanners ---- #

    logger.info("opening bridges/cslb_sos_ca_owner_lance ...")
    ds_bridge = lance.dataset(UPSTREAM_BRIDGE_URI, storage_options=storage_options)
    bridge_arrow = ds_bridge.scanner(
        columns=[
            "cslb_license_no",
            "sos_entity_num",
            "confidence_tier",
            "cslb_fan_out",
            "sos_fan_out",
            "bridge_run_id",
        ],
        filter=pc.field("confidence_tier").isin(["platinum", "gold", "silver"]),
    ).to_table()
    logger.info(
        "  bridge: %d rows (platinum+gold+silver — silver INCLUDED per audit decision)",
        bridge_arrow.num_rows,
    )

    logger.info("opening sos/ca_principals_lance ...")
    ds_principals = lance.dataset(UPSTREAM_PRINCIPALS_URI, storage_options=storage_options)
    principals_arrow = ds_principals.scanner(
        columns=[
            "entity_num",
            "entity_name_normalized",
            "org_name",
            "org_name_normalized",
            "first_name",
            "middle_name",
            "last_name",
            "full_name_normalized",
            "position_type",
            "address1",
            "address2",
            "address3",
            "city",
            "state",
            "country",
            "postal_code",
        ],
    ).to_table()
    logger.info("  principals: %d rows", principals_arrow.num_rows)

    # ---- Step 2: DuckDB INNER JOIN ---- #

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("br", bridge_arrow)
    con.register("pr", principals_arrow)

    # Validator p2: rename inherited bridge_run_id → cslb_sos_bridge_run_id;
    # emit own UUID as bridge_run_id. Mirrors PR #469 line 366/389 exactly.
    # Audit p1: confidence_tier renamed → cslb_sos_match_tier for output clarity.
    con.execute(
        f"""
        CREATE TEMP TABLE cohort AS
        SELECT
            br.cslb_license_no,
            br.sos_entity_num,
            br.confidence_tier                                    AS cslb_sos_match_tier,
            br.cslb_fan_out,
            br.sos_fan_out,
            pr.entity_name_normalized                             AS sos_entity_name_normalized,
            pr.org_name                                           AS principal_org_name,
            pr.org_name_normalized                                AS principal_org_name_normalized,
            pr.first_name                                         AS principal_first_name,
            pr.middle_name                                        AS principal_middle_name,
            pr.last_name                                          AS principal_last_name,
            pr.full_name_normalized                               AS principal_full_name_normalized,
            pr.position_type                                      AS principal_position_type,
            pr.address1                                           AS principal_address1,
            pr.address2                                           AS principal_address2,
            pr.address3                                           AS principal_address3,
            pr.city                                               AS principal_city,
            pr.state                                              AS principal_state,
            pr.country                                            AS principal_country,
            pr.postal_code                                        AS principal_postal_code,
            br.bridge_run_id                                      AS cslb_sos_bridge_run_id,
            '{BRIDGE_RUN_ID}'                                     AS bridge_run_id,
            '{BRIDGE_VERSION}'                                    AS bridge_version,
            TIMESTAMP '{generated_at_iso}'                        AS generated_at
        FROM br
        INNER JOIN pr ON br.sos_entity_num = pr.entity_num
        """
    )

    rows_matched = con.execute("SELECT count(*) FROM cohort").fetchone()[0]
    tier_counts = dict(
        con.execute(
            "SELECT cslb_sos_match_tier, count(*) FROM cohort GROUP BY cslb_sos_match_tier ORDER BY count(*) DESC"
        ).fetchall()
    )
    logger.info("cohort: %d rows  tiers=%s", rows_matched, tier_counts)

    if rows_matched < MIN_ROWS_MATCHED:
        logger.error(
            "HARD FAIL: rows_matched=%d < floor=%d", rows_matched, MIN_ROWS_MATCHED
        )
        return 1

    if not args.apply:
        logger.info(
            "DRY-RUN: would write %d rows (tiers=%s). Pass --apply to write.",
            rows_matched, tier_counts,
        )
        return 0

    # ---- Step 3: Lance write inside commit lock + dual BTREE ---- #

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM cohort").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        # Dual BTREE per directive + PR #482/#466 precedent.
        try:
            ds.create_scalar_index("cslb_license_no", index_type="BTREE", replace=True)
            logger.info("BTREE on cslb_license_no: OK")
        except Exception as e:
            logger.error("BTREE on cslb_license_no FAILED: %s", e)
            raise

        try:
            ds.create_scalar_index("sos_entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_entity_num: OK")
        except Exception as e:
            logger.error("BTREE on sos_entity_num FAILED: %s", e)
            raise

        # compact + cleanup per PR #469 precedent (non-fatal on failure).
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info(
        "OK: bridges.cslb_sos_ca_principal_lance written (%d rows; tiers=%s; bridge_run_id=%s)",
        lance_count, tier_counts, BRIDGE_RUN_ID,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
