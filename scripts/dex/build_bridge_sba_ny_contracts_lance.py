"""SBA × NY State contracts bridge (Pattern B PUBLISHER).

Pattern B exact-match bridge: SBA 7(a) / 504 borrowers (legal_name_normalized,
filtered to borrstate='NY') × NY State Authority contracts (vendor_name_normalized
filtered to vendor_state='NY'). Normalizer is canonical
scripts._lib.entity_name_normalize on BOTH sides.

Method: legal_name_state_exact_ny v1.0.0  (NEW state-variant — PUBLISHER cycle.
Does NOT overwrite the CA method (PR #464) or the FL method (PR #467);
different method_name, different (method_name, semver) tuple.)

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 5_000 (provisional floor; executor recalibrates at
                          0.5 × probed dry-run yield. Floor reduced from
                          30_000 post-pivot to data.ny.gov narrower-scope
                          construction sources: ehig-g5x3 has 140K NY-vendor
                          rows vs OpenBookNY ALL-state-agencies which would
                          have been larger. CA precedent 152_358 / FL 242_161
                          were against wide state-of-state owner-identity
                          spines, not narrower public-authority procurement.).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance
        (filter pc.field('borrstate') == 'NY')
    s3://dex-raw-landing-zone/polaris-warehouse/nystate/contracts_lance
        (filter pc.field('vendor_state') == 'NY')

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_contracts_lance
    (dual BTREE: sba_legal_name_normalized + contract_id per reviewer
    2026-05-18. BTREE on vendor_name_normalized would be redundant since the
    JOIN equality forces it value-equal to sba_legal_name_normalized on every
    bridge row.)

input_columns_left  = {legal_name_normalized, borrstate}   (SBA shape, mirror CA/FL)
input_columns_right = {vendor_name_normalized}             (NY-pure single-col; mirror CA
                                                            NOT FL which has {state} too)

Source pivot note (2026-05-18):
    The original OpenBookNY OSC ColdFusion source rejected automation. This
    bridge now joins against the data.ny.gov Socrata dataset ehig-g5x3
    "Procurement Report for State Authorities" (which IS the construction-
    aligned target for the original strategic role "fills NY-side of
    construction-targeting roadmap"). RIGHT-side schema accordingly switched
    from OpenBookNY's (contract_number, transaction_amount, start_date,
    end_date, description, transaction_approved_filed_date,
    transaction_type, department_facility) to ehig-g5x3's (contract_id,
    authority_name, vendor_state, procurement_description, type_of_procurement,
    award_process, contract_amount, award_date, end_date).

Provenance lifecycle (idempotent UPSERTs):
    register_match_method      → ops.match_methods           (idempotent on method_name)
    register_match_method_version → ops.match_method_versions (idempotent on (method_name, semver))
    register_bridge            → ops.bridges                 (idempotent)
    start_bridge_run           → ops.bridge_generation_runs  (status=running)
    write Lance + dual BTREE + tier counts
    complete_bridge_run        → status=completed + metrics

Tier rule:
    platinum = 1:1  (sba_fan_out=1 AND ny_fan_out=1)
    gold     = 1:N | N:1  (one side = 1, other > 1, both ≤ COLLISION_THRESHOLD)
    silver   = N:M (both ≤ COLLISION_THRESHOLD)
    rejected = >COLLISION_THRESHOLD on either side

CLI:
    --apply     build and write to Lance
    (default)   dry-run — log fan-out distribution and exit

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/build_bridge_sba_ny_contracts_lance.py [--apply]
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

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sba_ny_contracts"
DATASET_SLUG = "sba_ny_contracts_lance"
METHOD_NAME = "legal_name_state_exact_ny"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Recalibrated from 5_000 → 15_000 after dry-run 2026-05-18: matched 30,409 rows
# (1,212 platinum + 29,197 gold + 25,243 rejected) against 622,794 distinct SBA NY
# names × 140,126 NY-vendor contract rows. Floor at 0.5 × dry-run yield per
# audit pre-flight #4 + CA/FL precedent (PR #464/#467) recalibration ratio.
MIN_ROWS_MATCHED = 15_000

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "nystate_contracts_lance"

LEFT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
)
RIGHT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/nystate/contracts_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_contracts_lance"
)

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# ---------------------------------------------------------------------------
# Materialize inputs
# ---------------------------------------------------------------------------

def _materialize_inputs(storage_options: dict):
    """Load SBA borrowers (borrstate='NY') + NY contracts into Arrow tables.

    SBA side:
      - Read legal_name_normalized + borrstate from borrowers_lance,
        filtered to borrstate='NY' at the Lance scanner.
      - Pre-dedup to distinct legal_name_normalized list (Python-side,
        OOM-resistant — mirrors CA/FL bridge pattern).

    NY contracts side:
      - Read vendor_name_normalized + contract_id + identity cols.
      - Filter is_valid() on vendor_name_normalized.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening sba/borrowers_lance ...")
    sba_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sba_filter = pc.field("borrstate") == "NY"
    sba_tbl = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter=sba_filter,
    ).to_table()
    rows_sba_raw = len(sba_tbl)
    logger.info("  sba borrowers_lance (borrstate=NY): %d rows", rows_sba_raw)

    # Pre-dedup SBA side: distinct legal_name_normalized
    names = sba_tbl.column("legal_name_normalized").to_pylist()
    distinct_sba: set[str] = set()
    for nm in names:
        if not nm:
            continue
        if isinstance(nm, str) and len(nm) >= 2:
            distinct_sba.add(nm)
    del sba_tbl, names

    logger.info(
        "  sba_branded (distinct legal_name_normalized, NY only): %d names",
        len(distinct_sba),
    )

    sba_branded_arrow = pa.table(
        {
            "sba_legal_name_normalized": pa.array(
                sorted(distinct_sba), type=pa.string()
            ),
        }
    )
    del distinct_sba

    logger.info("opening nystate/contracts_lance ...")
    ny_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    # Filter: NY-vendor only (vendor_state='NY') AND vendor_name_normalized non-null.
    ny_filter = (
        (pc.field("vendor_state") == "NY")
        & pc.field("vendor_name_normalized").is_valid()
    )
    ny_tbl = ny_ds.scanner(
        columns=[
            "vendor_name_normalized",
            "contract_id",
            "vendor_name",
            "vendor_state",
            "authority_name",
            "procurement_description",
            "type_of_procurement",
            "award_process",
            "contract_amount",
            "award_date",
            "end_date",
        ],
        filter=ny_filter,
    ).to_table()
    rows_ny = len(ny_tbl)
    logger.info("  nystate contracts_lance (post-filter NY-vendor): %d rows", rows_ny)

    return sba_branded_arrow, ny_tbl, rows_sba_raw, rows_ny


# ---------------------------------------------------------------------------
# Build match table
# ---------------------------------------------------------------------------

def _build_match_table(
    sba_branded_arrow,
    ny_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
):
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba_branded", sba_branded_arrow)
    con.register("ny_contracts", ny_tbl)

    rows_sba_reg = con.execute("SELECT COUNT(*) FROM sba_branded").fetchone()[0]
    rows_ny_reg = con.execute("SELECT COUNT(*) FROM ny_contracts").fetchone()[0]
    logger.info(
        "  registered: sba_branded=%d  ny_contracts=%d",
        rows_sba_reg, rows_ny_reg,
    )

    # 1. Inner join on normalized name; SBA side is already distinct names.
    #    JOIN ON sba_legal_name_normalized = vendor_name_normalized
    #    (NY-pure: RIGHT pre-filtered to vendor_state='NY').
    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            s.sba_legal_name_normalized,
            c.vendor_name_normalized,
            c.contract_id,
            c.vendor_name             AS ny_vendor_name,
            c.vendor_state            AS ny_vendor_state,
            c.authority_name          AS ny_authority_name,
            c.procurement_description AS ny_procurement_description,
            c.type_of_procurement     AS ny_type_of_procurement,
            c.award_process           AS ny_award_process,
            c.contract_amount         AS ny_contract_amount,
            c.award_date              AS ny_award_date,
            c.end_date                AS ny_end_date
        FROM sba_branded s
        JOIN ny_contracts c
          ON s.sba_legal_name_normalized = c.vendor_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts.
    #    SBA side is already distinct (sba_fan_out = 1 per name).
    #    NY fan-out comes from same-normalized-name across multiple contracts
    #    (a vendor can appear on many contracts).
    con.execute(
        """
        CREATE TEMP TABLE ny_fanout AS
        SELECT sba_legal_name_normalized, COUNT(*) AS ny_fan_out
        FROM matched
        GROUP BY 1
        """
    )

    # 3. Tier + provenance columns.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.*,
            '{METHOD_NAME}'              AS match_method,
            m.sba_legal_name_normalized  AS match_value_normalized,
            'NY'                         AS match_state,
            1                            AS sba_fan_out,
            nf.ny_fan_out,
            CASE
                WHEN nf.ny_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN nf.ny_fan_out = 1
                    THEN 'platinum'
                WHEN nf.ny_fan_out <= {COLLISION_THRESHOLD}
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched m
        JOIN ny_fanout nf USING (sba_legal_name_normalized)
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


# ---------------------------------------------------------------------------
# Write bridge Lance
# ---------------------------------------------------------------------------

def _write_bridge_lance(con, storage_options: dict, lance_commit_lock) -> int:
    """Write bridge_match to Lance via Arrow-bridge pattern + dual BTREE.

    Dual BTREE per reviewer 2026-05-18:
      1. sba_legal_name_normalized  — LEFT-side identity (primary BTREE)
      2. contract_id                — RIGHT-side unique identifier (secondary;
                                      mirrors FL's entity_num pattern)

    BTREE on vendor_name_normalized would be REDUNDANT: the JOIN equality
    forces vendor_name_normalized == sba_legal_name_normalized on every
    bridge row, so those two columns are value-equal.
    """
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

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

        # BTREE 1: sba_legal_name_normalized (LEFT-side identity)
        try:
            ds.create_scalar_index("sba_legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on sba_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sba_legal_name_normalized FAILED: %s", e)
            raise

        # BTREE 2: contract_id (RIGHT-side synthetic identifier from data.ny.gov
        # ehig-g5x3; mirrors PR #467 FL entity_num pattern. Native unique key
        # since ehig-g5x3 has no per-row business contract_number.)
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="SBA x NY contracts bridge builder (Pattern B PUBLISHER)"
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="build and write to Lance (default: dry-run only)",
    )
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set in environment", var)
            return 64

    _bridge_database_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    from scripts._lib.entity_name_normalize import (  # noqa: F401
        __version__ as NORMALIZER_VERSION,
    )
    from scripts._lib.lance_commit_lock import lance_commit_lock
    from scripts._lib.match_method_registry import (
        complete_bridge_run,
        fail_bridge_run,
        register_bridge,
        register_match_method,
        register_match_method_version,
        start_bridge_run,
    )

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION,
    )
    logger.info("inputs: %s + %s", LEFT_LANCE_URI, RIGHT_LANCE_URI)
    logger.info("output: %s", BRIDGE_LANCE_URI)
    logger.info("mode: %s", "apply" if args.apply else "dry-run")

    # Provenance: register method + method version + bridge (idempotent UPSERTs).
    # legal_name_state_exact_ny is a NEW state-variant — does NOT overwrite
    # the CA method (PR #464) or the FL method (PR #467).
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "NY state-variant of legal_name_state_exact — exact equality on "
            "legal-entity-name normalization, scoped to NY SBA borrowers × "
            "NY State contracts vendors. _lib/entity_name_normalize "
            f"v{NORMALIZER_VERSION} on both sides. "
            "RIGHT side is NY-pure by source (no state column needed); "
            "mirrors CA shape (single-col input_columns_right) not FL shape."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "symmetric two-sided fan-out; platinum 1:1, gold 1:N|N:1, "
            f"silver N:M ≤{COLLISION_THRESHOLD}×{COLLISION_THRESHOLD}, "
            f"rejected >50 either side"
        ),
        rejection_rule_description=(
            f"COLLISION_THRESHOLD={COLLISION_THRESHOLD}; "
            "either fan-out >50 → rejected (excluded from output)"
        ),
        input_columns_left=["legal_name_normalized", "borrstate"],
        input_columns_right=["vendor_name_normalized"],
        output_value_description=(
            "normalize_entity_name(sba.legal_name_normalized) == "
            "normalize_entity_name(ny.vendor_name_normalized) for borrstate='NY'"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA × NY State contracts bridge — legal-name exact match for "
            "borrstate='NY'. Attaches NY public-contract history to SBA "
            "borrower cohorts. Fills NY-side of construction-targeting roadmap "
            "(capital-supply × NY public-work capital-demand). Dual BTREE on "
            "sba_legal_name_normalized + contract_id per reviewer 2026-05-18 "
            "(contract_id is synthetic sha1[:16] of ehig-g5x3 stable fields)."
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

    try:
        sba_branded_arrow, ny_tbl, rows_left, rows_right = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            sba_branded_arrow, ny_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M <=%d):    %d",
            COLLISION_THRESHOLD, counts["rows_tier3"],
        )
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

        if not args.apply:
            logger.info(
                "DRY RUN complete — rows_matched=%d (floor=%d). "
                "Re-calibrate MIN_ROWS_MATCHED to floor(0.5 × %d) = %d before --apply.",
                counts["rows_matched"],
                MIN_ROWS_MATCHED,
                counts["rows_matched"],
                max(MIN_ROWS_MATCHED, (counts["rows_matched"] // 2 // 1000) * 1000),
            )
            fail_bridge_run(run_uuid, "dry-run")
            return 0

        # --apply path
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

        lance_count = _write_bridge_lance(con, storage_options, lance_commit_lock)
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
            "OK - run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0
        )
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            logger.exception("also failed to mark run as failed")
        raise


if __name__ == "__main__":
    SCRIPT_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(SCRIPT_DIR.parent))
    raise SystemExit(main())
