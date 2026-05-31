"""s1 - CSLB licensees × CA SoS entities owner-identity bridge (Pattern B).

Pattern B exact-match bridge: CSLB licensees (business_name_normalized
produced by predecessor cycle PR #480) × CA SoS entities
(entity_name_normalized produced by PR #464). Both sides are pre-normalized
— no in-script normalization required.

Method: legal_name_state_exact_ca v1.0.0 (REUSED — the method + version rows
were registered by PR #464's ``build_bridge_sba_sos_ca_owner_lance.py``;
this script ONLY calls ``register_bridge`` + ``start_bridge_run`` +
``complete_bridge_run`` + ``fail_bridge_run`` per L21 — the method-definition
and method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the SBA-shape ``input_columns_left`` config and
corrupt the SBA × SoS bridge's provenance trail). This is the
second REUSER of this method (first: PR #466 UCC CA × CA SoS).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected — N:M up to 50×50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 30_000 (conservative floor — validator-calibrated
2026-05-17 post full-corpus probe: 215,261 raw row-level matches, 118,066
distinct CSLB normalized names matched; actual post-fan-out yield ~150K-200K;
30K floor catches catastrophic failure without false-tripping).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/cslb/licensees_lance
        (business_name_normalized pre-normalized at PR #480; LicenseNo PK)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance
        (entity_name_normalized pre-normalized at PR #464; entity_num PK;
        no jurisdiction filter — precedent PR #464/#466 omits it; dataset
        name is CA-implicit; foreign LLCs registered in CA appear here and
        are included intentionally for the downstream owner-identity chain)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_owner_lance
    (BTREE on cslb_license_no AND sos_entity_num; dual-BTREE per validator p6
    / precedent PR #466)

Normalizer (validator p1 — PR #459/#460 root cause):
    ONLY ``scripts._lib.entity_name_normalize`` version imported for
    __version__ correlation. Both sides already carry pre-normalized columns
    (business_name_normalized + entity_name_normalized) so no per-row
    normalization is needed; import is for normalizer_version provenance only.

Match method REUSE (validator p2 / L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 / validator p2 — REUSER pattern mirrors PR #466.)

Tier rule (symmetric two-sided per PR #466):
    platinum = BOTH fan_out == 1 (1:1 exact)
    gold     = ONE side fan_out == 1 (1:N | N:1)
    silver   = BOTH fan_out <= 50 (N:M below collision threshold)
    rejected = EITHER fan_out >  50 (collision)

Plain Python (path-a) — NOT Modal:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_cslb_sos_ca_owner_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run python scripts/build_bridge_cslb_sos_ca_owner_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 pattern — allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL validator p2 / L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle is the second REUSER of
# legal_name_state_exact_ca v1.0.0 (first REUSER: PR #466 UCC CA × CA SoS).
# Calling those helpers would UPSERT over the SBA-shape input_columns_left config
# (legal_name_normalized + borrstate) and corrupt the SBA × SoS bridge.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps in verify-s1.sh)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "cslb_sos_ca_owner"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "cslb_sos_ca_owner_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ca"  # REUSED — registered by PR #464
METHOD_SEMVER = "1.0.0"                    # REUSED — version row from PR #464
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-17 post full-corpus baseline probe.
# CSLB licensees = 243,948; raw row-level matches (no jurisdiction filter) = 215,261;
# distinct CSLB normalized names matched = 118,066. Floor = 30,000 (conservative;
# per directive §Volume floors; actual post-fan-out yield ~150K-200K).
MIN_ROWS_MATCHED = 30_000

SOURCE_LEFT = "cslb_licensees_lance"
SOURCE_RIGHT = "sos_ca_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/cslb/licensees_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_owner_lance"

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


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load CSLB licensees + CA SoS entities into Arrow tables.

    CSLB side:
      - Read LicenseNo + business_name_normalized + BusinessType columns.
      - Both sides are pre-normalized — no in-script normalization.
      - Filter to rows where business_name_normalized is_valid().

    SoS side:
      - Read entity_num + entity_name_normalized + entity metadata columns.
      - Filter to rows where entity_name_normalized is_valid().
      - NO jurisdiction filter — precedent PR #464/#466 omits it; the dataset
        name sos.ca_entities_lance is CA-implicit; foreign LLCs registered in
        CA appear here and are included intentionally for downstream chain.
    """
    import lance
    import pyarrow.compute as pc

    logger.info("opening cslb/licensees_lance ...")
    cslb_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    cslb_filter = pc.field("business_name_normalized").is_valid()
    cslb_tbl = cslb_ds.scanner(
        columns=[
            "LicenseNo",
            "BusinessName",
            "BusinessType",
            "business_name_normalized",
        ],
        filter=cslb_filter,
    ).to_table()
    rows_cslb = len(cslb_tbl)
    logger.info("  cslb licensees_lance (business_name_normalized is_valid): %d rows", rows_cslb)

    logger.info("opening sos/ca_entities_lance ...")
    sos_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    sos_tbl = sos_ds.scanner(
        columns=[
            "entity_num",
            "entity_name",
            "entity_name_normalized",
            "entity_status",
            "standing_sos",
            "entity_type",
            "llc_management_structure",
            "initial_filing_date",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info("  sos ca_entities_lance (entity_name_normalized is_valid): %d rows", rows_sos)

    return cslb_tbl, sos_tbl, rows_cslb, rows_sos


def _build_match_table(
    cslb_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: business_name_normalized (CSLB) = entity_name_normalized (SoS).
    Name-only — state is implicit from the CA-SoS dataset name.
    Fan-out: separate cslb_fan_out (per normalized name across all CSLB rows)
    and sos_fan_out (per normalized name across distinct SoS entity_nums).
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("cslb", cslb_tbl)
    con.register("sos", sos_tbl)

    rows_cslb_reg = con.execute("SELECT COUNT(*) FROM cslb").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos").fetchone()[0]
    logger.info("  registered: cslb=%d  sos=%d", rows_cslb_reg, rows_sos_reg)

    # 1. Inner JOIN on normalized name (name-only composite key; state-implicit).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            c."LicenseNo"                        AS cslb_license_no,
            c."BusinessName"                     AS cslb_business_name_raw,
            c."BusinessType"                     AS cslb_business_type,
            c.business_name_normalized,
            s.entity_num                         AS sos_entity_num,
            s.entity_name                        AS sos_entity_name,
            s.entity_name_normalized,
            s.entity_status,
            s.standing_sos,
            s.entity_type,
            s.llc_management_structure,
            s.initial_filing_date,
            '{METHOD_NAME}'                      AS match_method,
            c.business_name_normalized           AS match_value_normalized,
            '{BRIDGE_VERSION}'                   AS bridge_version,
            '{bridge_run_id}'                    AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'       AS generated_at
        FROM cslb c
        JOIN sos s
          ON c.business_name_normalized = s.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided per PR #466 pattern).
    #    cslb_fan_out: # of CSLB licensee rows sharing this normalized name.
    #    sos_fan_out: # of distinct SoS entity_nums for this normalized name.
    con.execute(
        """
        CREATE TEMP TABLE cslb_fanout AS
        SELECT business_name_normalized, COUNT(*) AS cslb_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT business_name_normalized,
               COUNT(DISTINCT sos_entity_num) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided per PR #466):
    #    platinum = BOTH fan_out == 1
    #    gold     = ONE side fan_out == 1
    #    silver   = BOTH <= COLLISION_THRESHOLD (else)
    #    rejected = EITHER > COLLISION_THRESHOLD
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            cf.cslb_fan_out,
            sf.sos_fan_out,
            CASE
                WHEN cf.cslb_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sos_fan_out  > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN cf.cslb_fan_out = 1 AND sf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN cf.cslb_fan_out = 1 OR  sf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN cslb_fanout cf USING (business_name_normalized)
        JOIN sos_fanout  sf USING (business_name_normalized)
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
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE (cslb_license_no, sos_entity_num)."""
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

        # Dual BTREE per validator p6 / precedent PR #466.
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
    """Build the CSLB × CA SoS owner-identity bridge."""
    parser = argparse.ArgumentParser(
        description="CSLB licensees × CA SoS entities Pattern B fuzzy bridge generator."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to Lance + register Polaris. Without this flag runs in dry-run mode.",
    )
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #464)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "CSLB licensees × CA SoS entities — legal-name exact match. "
            "Reuses legal_name_state_exact_ca v1.0.0 method registered by PR #464. "
            "Second REUSER after PR #466 (UCC CA × CA SoS). "
            "Joins CSLB contractor licensees to CA-registered entities; downstream "
            "chain via sos_entity_num → sos.ca_principals_lance gives owner identity."
        ),
    )
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

    if not args.apply:
        # Dry-run: mark as failed so the run is not left orphaned.
        msg = "dry-run; no Lance write (pass --apply to execute)"
        logger.info("DRY-RUN: %s", msg)
        fail_bridge_run(run_uuid, msg)
        logger.info("bridge_run marked failed-dry-run (run_id=%s)", bridge_run_id)
        return 0

    try:
        cslb_tbl, sos_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            cslb_tbl, sos_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M <= %d):   %d",
            COLLISION_THRESHOLD, counts["rows_tier3"],
        )
        logger.info(
            "  rows_collision_rejected:  %d",
            counts["rows_collision_rejected"],
        )

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
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
    sys.exit(main())
