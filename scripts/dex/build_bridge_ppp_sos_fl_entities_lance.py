"""PPP × FL Sunbiz entities Pattern B Lance bridge.

Pattern B exact-match bridge: SBA PPP borrowers with borrstate='FL'
(from sba/ppp_borrowers_lance, Cycle 1 output — one row per
(legal_name_normalized, borrstate, borrzip) grain)
× FL Sunbiz entities (entity_name_normalized pre-normalized at PR #467).

Method: legal_name_state_exact_fl v1.0.0 (REUSED — the method + version rows
were registered by PR #467's build_bridge_sba_sos_fl_owner_lance.py;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run per L21 — the method-definition
and method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the shared input_columns_left config and corrupt
the sam_sos_fl_entities, sba_sos_fl_owner, sba_fl_cilb, and fl_cilb_sunbiz
bridges' provenance trail).
This is the FIFTH REUSER of this method (prior reusers: fl_cilb_sunbiz PR #467,
sba_fl_cilb PR #467, sba_sos_fl_owner PR #467 publisher, sam_sos_fl_entities PR #563;
per L21 — mirror of the CA cycle REUSER chain).

PPP-side grain: (legal_name_normalized, borrstate, borrzip) — one row per
PPP borrower location. ppp_borrowers_lance is ALREADY at this grain (Cycle 1 output,
PR #574); NO SELECT DISTINCT is needed, unlike the SAM precedent which had to
dedup raw SAM rows to (uei, legal_business_name).

PPP-side filter: borrstate='FL' (SINGLE-column predicate — NOT an OR-predicate;
no pc.or_() needed, unlike the SAM-FL cycle which required OR across two state
columns; mirror of the PPP-CA cycle which used a single borrstate='CA' predicate).

Normalizer (CRITICAL — join on the pre-normalized columns DIRECTLY):
    PARITY HOLDS (validator-measured 2026-05-20 via 500-row sample; 500/500 = 100.0%
    agreement between _lib.normalize_entity_name and stored entity_name_normalized;
    0 mismatches on FL Sunbiz side; PPP side legal_name_normalized confirmed 100%
    _lib-parity at Cycle 2).
    FL Sunbiz entity_name_normalized was produced upstream by PR #467 publisher using
    _lib.normalize_entity_name; ppp_borrowers_lance.legal_name_normalized was produced by
    emit_sba_loans_lance.py's _normalize_entity_sql() — a SQL transliteration of
    _lib.entity_name_normalize with IDENTICAL suffix tokens and IDENTICAL regex order.
    JOIN: FROM ppp p JOIN fl c ON p.legal_name_normalized = c.entity_name_normalized.
    DO NOT call normalize_entity_name on either join key. DO NOT copy the SAM-FL
    precedent's L229-243 Python-normalize block — that block exists because SAM's
    pre-supplied column diverges at ~8.4% from _lib; PPP's column does NOT have
    that divergence.

FL Sunbiz status column: 'status' (NOT 'entity_status' as CA SoS has). Taxonomy is
2-value: 'A' (Active, 3,933,815 rows), 'I' (Inactive, 8,673,643 rows). Build script
renames in DuckDB SELECT: f.status AS sos_entity_status (mirror SAM-FL precedent L314).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected — N:M up to 50×50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 360_000 (validator-calibrated 2026-05-20 post full-corpus
probe: 513,021 non-rejected matches — 237,058 platinum + 119,521 gold + 156,442
silver + 4,092 rejected. 360K floor (~70%) catches catastrophic failure from
schema/normalizer regression without false-tripping on clean recipe runs;
per DATA-FACTORY-LESSONS.md floor convention).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/
        (legal_name_normalized, borrstate, borrzip, total_ppp_loans, total_ppp_approval;
        filter: borrstate='FL' AND legal_name_normalized is_valid();
        ALREADY at (name, state, zip) grain — NO SELECT DISTINCT)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance
        (entity_name_normalized pre-normalized at PR #467; entity_num PK;
        status col is 'status' NOT 'entity_status'; filter: entity_name_normalized is_valid())

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_sos_fl_entities_lance
    (BTREE on ppp_legal_name_normalized AND sos_entity_num; dual-BTREE per
    contract §C2/C3)

Match method REUSE (validator p3 / L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 / validator p3 — REUSER pattern mirrors PPP-CA PR #575.)

Tier rule (symmetric two-sided per PPP-CA precedent L316-342):
    platinum = BOTH fan_out == 1 (1:1 exact)
    gold     = ONE side fan_out == 1 (1:N | N:1)
    silver   = BOTH fan_out <= 50 (N:M below collision threshold)
    rejected = EITHER fan_out >  50 (collision)

Deferred Polaris registration (constraint #9 soft — Polaris/Railway down 2026-05-20):
    python apps/data-engine-x/scripts/init_polaris_lance_generic.py \\
        --namespace bridges --table ppp_sos_fl_entities_lance \\
        --doc "PPP x FL Sunbiz entities Pattern B bridge (legal_name_state_exact_fl)"

Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_ppp_sos_fl_entities_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_ppp_sos_fl_entities_lance.py
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

from scripts._lib.entity_name_normalize import (  # noqa: F401 — __version__ for log provenance only
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL validator p3 / L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle REUSES legal_name_state_exact_fl
# v1.0.0 registered by PR #467 (sba_sos_fl_owner publisher). Calling those helpers
# would UPSERT over the shared match_method_versions row and corrupt the provenance
# trail of sam_sos_fl_entities, sba_sos_fl_owner, sba_fl_cilb, and fl_cilb_sunbiz.
# This script is a REUSER — the method-definition imports are absent by design (L21).
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps in verify constraints)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "ppp_sos_fl_entities"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "ppp_sos_fl_entities_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_fl"    # REUSED — registered by PR #467
METHOD_SEMVER = "1.0.0"                      # REUSED — version row from PR #467
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-20 post full-corpus baseline probe.
# PPP FL borrowers (borrstate='FL') = 858,173 rows; distinct names 735,838.
# Join yields 513,021 non-rejected (237,058 platinum + 119,521 gold + 156,442
# silver + 4,092 rejected). Max fan-out PPP 192, SoS 31.
# Floor = 360,000 (~70% of measured; catches catastrophic failure from
# schema/normalizer regression without false-tripping on clean recipe runs).
MIN_ROWS_MATCHED = 360_000

SOURCE_LEFT = "ppp_borrowers_lance"
SOURCE_RIGHT = "sos_fl_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_sos_fl_entities_lance"

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
    """Load PPP FL-state borrowers + FL Sunbiz entities into Arrow tables.

    PPP side (PARITY — join on pre-normalized columns directly):
      - Read ppp_borrowers_lance with push-down filter:
        borrstate='FL' AND legal_name_normalized is_valid()
        (SINGLE-column state predicate — no pc.or_() needed; mirror PPP-CA L201-214).
      - Project: legal_name_normalized, borrstate, borrzip, total_ppp_loans,
        total_ppp_approval
      - NO SELECT DISTINCT — ppp_borrowers_lance is already at (name,state,zip)
        borrower grain (Cycle 1 output, PR #574).
      - DO NOT call normalize_entity_name on legal_name_normalized — parity holds
        (100.0% on 500-sample; the column is already _lib v1.0.0 compatible via
        emit_sba_loans_lance.py's _normalize_entity_sql()).

    FL Sunbiz side (mirror SAM-FL L245-260):
      - Read entity_num + entity_name_normalized + status (FL column — NOT entity_status).
      - Filter to rows where entity_name_normalized is_valid().
      - Status column is 'status' ('A'/'I') — renamed in DuckDB SELECT (see _build_match_table).
    """
    import lance
    import pyarrow.compute as pc

    logger.info("opening sba/ppp_borrowers_lance (FL filter, single-column) ...")
    ppp_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)

    # Single-column push-down filter: borrstate='FL' AND legal_name_normalized valid.
    # ppp_borrowers_lance is already at (name,state,zip) grain — NO SELECT DISTINCT.
    # CRITICAL: do NOT call normalize_entity_name on the join key (parity holds,
    # validator p1 — both sides _lib v1.0.0 parity, join pre-normalized directly).
    ppp_filter = (
        (pc.field("borrstate") == "FL")
        & pc.field("legal_name_normalized").is_valid()
    )
    ppp_tbl = ppp_ds.scanner(
        columns=[
            "legal_name_normalized",
            "borrstate",
            "borrzip",
            "total_ppp_loans",
            "total_ppp_approval",
        ],
        filter=ppp_filter,
    ).to_table()
    rows_left = len(ppp_tbl)
    logger.info("  ppp_borrowers_lance FL rows (borrstate='FL', normalized valid): %d", rows_left)

    logger.info("opening sos/fl_entities_lance ...")
    fl_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    fl_filter = pc.field("entity_name_normalized").is_valid()
    # FL Sunbiz columns: entity_num (PK), entity_name_normalized (join key),
    # status (NOT entity_status — FL Sunbiz has no entity_status column).
    # Mirror SAM-FL L245-260.
    fl_tbl = fl_ds.scanner(
        columns=[
            "entity_num",
            "entity_name_normalized",
            "status",
        ],
        filter=fl_filter,
    ).to_table()
    rows_fl = len(fl_tbl)
    logger.info("  sos fl_entities_lance (entity_name_normalized is_valid): %d rows", rows_fl)

    return ppp_tbl, fl_tbl, rows_left, rows_fl


def _build_match_table(
    ppp_tbl,
    fl_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: ppp.legal_name_normalized = fl.entity_name_normalized
    BOTH are pre-normalized (_lib v1.0.0 compatible) — join directly, no re-normalize.
    (Validator p1: both sides measured 100% _lib parity; no Python-normalize block needed.)

    Fan-out: separate ppp_fan_out (# of PPP rows per normalized name) and
    sos_fan_out (# of distinct FL entity_nums per normalized name).
    Mirrors PPP-CA precedent at L267-342.

    FL Sunbiz 'status' renamed to 'sos_entity_status' to match the PPP-CA bridge
    output column shape (mirror SAM-FL precedent L314 which does s.status AS entity_status;
    the PPP-CA output column is sos_entity_status so we land it directly that way).
    Validator p2: FL has no entity_status column — reading it directly is a build error;
    must project f.status AS sos_entity_status.
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("ppp", ppp_tbl)
    con.register("fl", fl_tbl)

    rows_ppp_reg = con.execute("SELECT COUNT(*) FROM ppp").fetchone()[0]
    rows_fl_reg = con.execute("SELECT COUNT(*) FROM fl").fetchone()[0]
    logger.info("  registered: ppp=%d  fl=%d", rows_ppp_reg, rows_fl_reg)

    # 1. Inner JOIN on pre-normalized name (CRITICAL: join the two pre-normalized
    #    columns directly — ppp.legal_name_normalized = fl.entity_name_normalized).
    #    PPP left grain is (legal_name_normalized, borrstate, borrzip) — all three
    #    identity columns carried per contract §6 (name alone is not unique across states).
    #    FL 'status' renamed to 'sos_entity_status' (mirror PPP-CA output column shape;
    #    FL Sunbiz has no entity_status column — validator p2 confirmed).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            p.legal_name_normalized          AS ppp_legal_name_normalized,
            p.borrstate                      AS ppp_borrstate,
            p.borrzip                        AS ppp_borrzip,
            p.total_ppp_loans                AS ppp_total_loans,
            p.total_ppp_approval             AS ppp_total_approval,
            f.entity_num                     AS sos_entity_num,
            f.entity_name_normalized         AS sos_entity_name_normalized,
            f.status                         AS sos_entity_status,
            '{METHOD_NAME}'                  AS match_method,
            p.legal_name_normalized          AS match_value,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'   AS generated_at
        FROM ppp p
        JOIN fl f
          ON p.legal_name_normalized = f.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided, mirrors PPP-CA L297-313).
    #    ppp_fan_out: # of PPP rows (borrower locations) sharing this normalized name.
    #    sos_fan_out: # of distinct FL entity_nums for this normalized name.
    con.execute(
        """
        CREATE TEMP TABLE ppp_fanout AS
        SELECT ppp_legal_name_normalized, COUNT(*) AS ppp_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT ppp_legal_name_normalized,
               COUNT(DISTINCT sos_entity_num) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided per PPP-CA precedent L316-342):
    #    platinum = BOTH fan_out == 1
    #    gold     = ONE side fan_out == 1
    #    silver   = BOTH <= COLLISION_THRESHOLD (else)
    #    rejected = EITHER > COLLISION_THRESHOLD
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            pf.ppp_fan_out,
            uf.sos_fan_out,
            CASE
                WHEN pf.ppp_fan_out > {COLLISION_THRESHOLD}
                  OR uf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN pf.ppp_fan_out = 1 AND uf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN pf.ppp_fan_out = 1 OR  uf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN ppp_fanout pf USING (ppp_legal_name_normalized)
        JOIN sos_fanout uf USING (ppp_legal_name_normalized)
        """
    )

    # 4. Filter rejected rows before write (mirrors PPP-CA L344-350).
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
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE.

    BTREE on ppp_legal_name_normalized (constraint #2) and sos_entity_num
    (constraint #3). Mirrors PPP-CA precedent at L376-430.
    """
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

        # Dual BTREE per contract §C2/C3 (mirrors PPP-CA L407-420).
        try:
            ds.create_scalar_index("ppp_legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on ppp_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on ppp_legal_name_normalized FAILED: %s", e)
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
    """Build the PPP FL borrowers × FL Sunbiz entities Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description="PPP FL borrowers × FL Sunbiz entities Pattern B bridge generator."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to Lance + register ops. Without this flag runs in dry-run mode.",
    )
    args = parser.parse_args()

    _ensure_db_url()
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #467, 5th REUSER)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per L21/p3).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "PPP (SBA Paycheck Protection Program) FL-state borrowers × FL Sunbiz entities "
            "— legal-name exact match. "
            "Reuses legal_name_state_exact_fl v1.0.0 method registered by PR #467 "
            "(sba_sos_fl_owner publisher). Fifth REUSER (prior: fl_cilb_sunbiz, sba_fl_cilb, "
            "sba_sos_fl_owner PR #467; sam_sos_fl_entities PR #563). "
            "FL variant of bridges.ppp_sos_ca_entities_lance (Cycle 2, PR #575). "
            "Filter: borrstate='FL' (single-column predicate); "
            "858K FL PPP borrowers matched against 12.6M FL Sunbiz entities. "
            "Normalizer parity holds (100.0% on 500-sample 2026-05-20 both sides): "
            "joins ppp_borrowers_lance.legal_name_normalized directly to "
            "fl_entities_lance.entity_name_normalized — both _lib v1.0.0 compatible. "
            "Left grain: (legal_name_normalized, borrstate, borrzip) per PR #574 Cycle 1. "
            "BTREE on ppp_legal_name_normalized + sos_entity_num."
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
        ppp_tbl, fl_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            ppp_tbl, fl_tbl,
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
