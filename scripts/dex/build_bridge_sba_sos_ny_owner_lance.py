"""SBA × NY SoS active corporations Pattern B Lance bridge (owner-identity).

Pattern B exact-match bridge: SBA borrowers with borrstate='NY'
(from sba/borrowers_lance — 12M-row program-agnostic rollup; NO 'program' column,
intentional — covers ALL SBA programs, not PPP-only)
× NY SoS active corporations (entity_name_normalized pre-normalized;
NY DoS uses dos_id as PK, NOT entity_num as used by CA/FL SoS).

Method: legal_name_state_exact_ny v1.0.0 (REUSED — the method + version rows
were registered by a prior build script; this script ONLY calls
register_bridge + start_bridge_run + complete_bridge_run + fail_bridge_run
per L21 — the method-definition and method-version-definition helpers are
INTENTIONALLY OMITTED; calling them would UPSERT over the shared
input_columns_left config and corrupt the sam_sos_ny_entities,
usaspending_sos_ny_owner, ppp_sos_ny_entities, sba_ny_sam
bridges' provenance trail).

SBA-side grain: one row per (legal_name_normalized, borrstate, borrzip) — the
program-agnostic borrower rollup already deduped at that grain.
sba/borrowers_lance is ALREADY borrower-grain (not loan-grain); NO SELECT DISTINCT
is needed (mirrors ppp_borrowers_lance behaviour in Cycle 4 PPP-NY build).

SBA-side filter: borrstate='NY' (SINGLE-column predicate — NOT an OR-predicate;
no pc.or_() needed; constraint #17).

Normalizer (CRITICAL — join on the pre-normalized columns DIRECTLY):
    PARITY HOLDS (validator-measured 2026-05-20 via 500-row sample; 500/500 =
    100.0% agreement on BOTH sides: sba/borrowers_lance.legal_name_normalized vs
    _lib.normalize_entity_name(borrname_sample), and
    sos/ny_active_corporations_lance.entity_name_normalized vs
    _lib.normalize_entity_name(current_entity_name); _lib v1.0.0; 0 mismatches).
    JOIN: FROM sba s JOIN ny c ON s.legal_name_normalized = c.entity_name_normalized.
    DO NOT call normalize_entity_name on either join key. DO NOT copy the SAM-NY
    precedent's Python-normalize block — that block exists because SAM's
    pre-supplied column diverges at ~8% from _lib; sba/borrowers_lance does NOT
    have that divergence (same normalization lineage as ppp_borrowers_lance).

NY SoS PK column: 'dos_id' (NY Department of State ID — NOT 'entity_num' as used
by CA/FL SoS; validator p2). Bridge output column is 'sos_dos_id'
(mirroring bridges/ppp_sos_ny_entities_lance and bridges/sam_sos_ny_entities_lance).

NY SoS status: ny_active_corporations_lance has NO status column — it is
ACTIVE-only by construction. Bridge projects the literal 'A' AS sos_entity_status
(mirror Cycle 4 ppp_sos_ny_entities_lance which does the same).

NY CEO bonus (the "owner" in the bridge name / constraint #8 / validator p3):
sos/ny_active_corporations_lance carries six inline CEO columns on the entity row:
ceo_name, ceo_address_1, ceo_address_2, ceo_city, ceo_state, ceo_zip.
The SBA-NY bridge MUST project all six — delivers owner-identity unlock.
Validator measured 237,529/371,664 = 63.9% of matched rows carry non-null ceo_name.
NOTE: sba_sos_fl_owner (FL) carries NO CEO column (FL keeps officers in a separate
roster dataset); do NOT copy the FL output shape here.

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected — N:M up to 50×50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 260_000 (validator-calibrated 2026-05-20 post full-corpus
baseline probe: 371,664 non-rejected matches — 183,521 platinum + 179,861 gold +
8,282 silver + 722 rejected. Floor ~70% of baseline; catches catastrophic failure
from schema/normalizer regression without false-tripping on clean recipe runs).

SBA-left column naming follows the sba_sos_fl_owner convention ('sba_*' prefix):
    sba_legal_name_normalized, sba_borrstate, sba_borrzip, sba_total_loans,
    sba_total_gross_approval.
Structural template follows Cycle 4 ppp_sos_ny_entities (on origin/main):
    JOIN, fan-out, tier rule, L21-reuser registry pattern, dual-BTREE,
    NY-right shape (dos_id → sos_dos_id, 'A' status, 6 ceo_* cols).
No list<string> columns projected (constraint #12 / L54): sba/borrowers_lance has
franchise_brands_set, naics_codes_set, lender_set as list<string> — NOT projected.

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/
        (legal_name_normalized, borrstate, borrzip, total_loans, total_gross_approval;
        filter: borrstate='NY' AND legal_name_normalized is_valid();
        borrower-grain rollup — NO SELECT DISTINCT)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ny_active_corporations_lance
        (entity_name_normalized, dos_id PK, ceo_name + 5 ceo_address_* cols;
        NO status column — active-only; filter: entity_name_normalized is_valid())

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_ny_owner_lance
    (BTREE on sba_legal_name_normalized AND sos_dos_id; dual-BTREE per
    contract §C2/C3; sos_dos_id mirrors ppp_sos_ny_entities_lance)

Match method REUSE (validator p4 / L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 / validator p4 — REUSER pattern mirrors Cycle 4
    ppp_sos_ny_entities_lance on origin/main.)

Tier rule (symmetric two-sided per Cycle 4 PPP-NY precedent):
    platinum = BOTH fan_out == 1 (1:1 exact)
    gold     = ONE side fan_out == 1 (1:N | N:1)
    silver   = BOTH fan_out <= 50 (N:M below collision threshold)
    rejected = EITHER fan_out >  50 (collision)

Deferred Polaris registration (constraint #10 soft — Polaris/Railway down 2026-05-20):
    doppler run --project hq-all --config prd -- \\
        uv run --project . python3 scripts/init_polaris_lance_generic.py \\
        --namespace bridges --table sba_sos_ny_owner_lance \\
        --doc "SBA x NY SoS active corporations Pattern B bridge (legal_name_state_exact_ny)"

Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_sba_sos_ny_owner_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_sba_sos_ny_owner_lance.py
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
# CRITICAL validator p4 / L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle REUSES legal_name_state_exact_ny
# v1.0.0 registered by a prior publisher. Calling those helpers would UPSERT
# over the shared match_method_versions row and corrupt the provenance trail of
# sam_sos_ny_entities, usaspending_sos_ny_owner, ppp_sos_ny_entities, sba_ny_sam.
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

BRIDGE_NAME = "sba_sos_ny_owner"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "sba_sos_ny_owner_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ny" # REUSED — registered by prior publisher
METHOD_SEMVER = "1.0.0"                   # REUSED — existing version row
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-20 post full-corpus baseline probe.
# SBA NY-state borrowers (borrstate='NY') = 776,941 rows; distinct names 622,805.
# Join yields 371,664 non-rejected (183,521 platinum + 179,861 gold + 8,282
# silver + 722 rejected). Max fan-out SBA 171, NY SoS 30.
# Floor = 260,000 (~70% of measured; catches catastrophic failure from
# schema/normalizer regression without false-tripping on clean recipe runs).
MIN_ROWS_MATCHED = 260_000

SOURCE_LEFT = "sba_borrowers_lance"
# Validator-confirmed: source_right for ppp_sos_ny_entities uses
# 'sos_ny_active_corporations_lance' — SBA-NY bridge uses the identical string.
SOURCE_RIGHT = "sos_ny_active_corporations_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ny_active_corporations_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_ny_owner_lance"

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
    """Load SBA NY-state borrowers + NY SoS active corporations into Arrow tables.

    SBA side (PARITY — join on pre-normalized columns directly):
      - Read sba/borrowers_lance with push-down filter:
        borrstate='NY' AND legal_name_normalized is_valid()
        (SINGLE-column state predicate — constraint #17; no pc.or_() needed).
      - Project: legal_name_normalized, borrstate, borrzip, total_loans,
        total_gross_approval.
      - NO list<string> columns — franchise_brands_set, naics_codes_set, lender_set
        are NOT projected (constraint #12 / L54).
      - NO SELECT DISTINCT — sba/borrowers_lance is already at borrower grain.
      - DO NOT call normalize_entity_name on legal_name_normalized — parity holds
        (100.0% on 500-sample; validator p1).

    NY SoS side (validator p2 — PK is dos_id NOT entity_num):
      - Read dos_id (PK) + entity_name_normalized (join key) + six CEO columns.
      - Filter to rows where entity_name_normalized is_valid().
      - ny_active_corporations_lance has NO status column — ACTIVE-only.
      - CEO columns confirmed live 2026-05-20: ceo_name, ceo_address_1, ceo_address_2,
        ceo_city, ceo_state, ceo_zip (all string).
    """
    import lance
    import pyarrow.compute as pc

    logger.info("opening sba/borrowers_lance (NY filter, single-column) ...")
    sba_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)

    # Single-column push-down filter: borrstate='NY' AND legal_name_normalized valid.
    # CRITICAL: do NOT call normalize_entity_name on the join key (parity holds,
    # validator p1 — both sides _lib v1.0.0 parity, join pre-normalized directly).
    # Do NOT use pc.or_() — constraint #17 (single-column state predicate).
    sba_filter = (
        (pc.field("borrstate") == "NY")
        & pc.field("legal_name_normalized").is_valid()
    )
    sba_tbl = sba_ds.scanner(
        columns=[
            "legal_name_normalized",
            "borrstate",
            "borrzip",
            "total_loans",
            "total_gross_approval",
        ],
        filter=sba_filter,
    ).to_table()
    rows_left = len(sba_tbl)
    logger.info(
        "  sba/borrowers_lance NY rows (borrstate='NY', normalized valid): %d",
        rows_left,
    )

    logger.info("opening sos/ny_active_corporations_lance ...")
    ny_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    ny_filter = pc.field("entity_name_normalized").is_valid()
    # NY SoS columns: dos_id (PK — NOT entity_num; NY uses Department of State ID;
    # validator p2), entity_name_normalized (join key),
    # six inline CEO columns (validator p3 / constraint #8 — confirmed live 2026-05-20).
    # NO status column — ny_active_corporations_lance is ACTIVE-only by construction.
    ny_tbl = ny_ds.scanner(
        columns=[
            "dos_id",
            "entity_name_normalized",
            "ceo_name",
            "ceo_address_1",
            "ceo_address_2",
            "ceo_city",
            "ceo_state",
            "ceo_zip",
        ],
        filter=ny_filter,
    ).to_table()
    rows_ny = len(ny_tbl)
    logger.info(
        "  sos ny_active_corporations_lance (entity_name_normalized is_valid): %d rows",
        rows_ny,
    )

    return sba_tbl, ny_tbl, rows_left, rows_ny


def _build_match_table(
    sba_tbl,
    ny_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: sba.legal_name_normalized = ny.entity_name_normalized
    BOTH are pre-normalized (_lib v1.0.0 compatible) — join directly, no re-normalize.
    (Validator p1: both sides measured 100% _lib parity; no Python-normalize block needed.)

    Fan-out: separate sba_fan_out (# of SBA rows per normalized name) and
    sos_fan_out (# of distinct NY dos_ids per normalized name).
    Mirrors Cycle 4 PPP-NY precedent for the symmetric two-sided fan-out.

    NY status hardcoded 'A': ny_active_corporations_lance has no status column
    (ACTIVE-only); bridge projects literal 'A' AS sos_entity_status (mirror
    ppp_sos_ny_entities_lance which does the same).

    Validator p2: NY PK is dos_id → bridge output column sos_dos_id.
    Validator p3: project six inline ceo_* columns from the NY side (the "owner"
    in the bridge name — not carried by sba_sos_fl_owner which uses a separate
    officers dataset; NY has it inline).

    SBA-left column naming follows sba_sos_fl_owner convention ('sba_*' prefix):
    sba_legal_name_normalized, sba_borrstate, sba_total_loans, sba_total_gross_approval.
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba", sba_tbl)
    con.register("ny", ny_tbl)

    rows_sba_reg = con.execute("SELECT COUNT(*) FROM sba").fetchone()[0]
    rows_ny_reg = con.execute("SELECT COUNT(*) FROM ny").fetchone()[0]
    logger.info("  registered: sba=%d  ny=%d", rows_sba_reg, rows_ny_reg)

    # 1. Inner JOIN on pre-normalized name (CRITICAL: join the two pre-normalized
    #    columns directly — sba.legal_name_normalized = ny.entity_name_normalized).
    #    SBA left grain carries borrstate + borrzip + loan aggregates.
    #    NY status hardcoded 'A' — ny_active_corporations_lance has no status column.
    #    NY PK dos_id → bridge output column sos_dos_id (mirrors ppp_sos_ny_entities).
    #    Six ceo_* columns projected verbatim from the NY side (constraint #8 / p3).
    #    No list<string> columns — franchise_brands_set etc. NOT projected (constraint #12).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            s.legal_name_normalized          AS sba_legal_name_normalized,
            s.borrstate                      AS sba_borrstate,
            s.borrzip                        AS sba_borrzip,
            s.total_loans                    AS sba_total_loans,
            s.total_gross_approval           AS sba_total_gross_approval,
            n.dos_id                         AS sos_dos_id,
            n.entity_name_normalized         AS sos_entity_name_normalized,
            'A'                              AS sos_entity_status,
            n.ceo_name,
            n.ceo_address_1,
            n.ceo_address_2,
            n.ceo_city,
            n.ceo_state,
            n.ceo_zip,
            '{METHOD_NAME}'                  AS match_method,
            s.legal_name_normalized          AS match_value,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'   AS generated_at
        FROM sba s
        JOIN ny n
          ON s.legal_name_normalized = n.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided, mirrors Cycle 4 PPP-NY precedent).
    #    sba_fan_out: # of SBA rows sharing this normalized name.
    #    sos_fan_out: # of distinct NY dos_ids for this normalized name.
    con.execute(
        """
        CREATE TEMP TABLE sba_fanout AS
        SELECT sba_legal_name_normalized, COUNT(*) AS sba_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT sba_legal_name_normalized,
               COUNT(DISTINCT sos_dos_id) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided per Cycle 4 PPP-NY precedent):
    #    platinum = BOTH fan_out == 1
    #    gold     = ONE side fan_out == 1
    #    silver   = BOTH <= COLLISION_THRESHOLD (else)
    #    rejected = EITHER > COLLISION_THRESHOLD
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sba_fan_out,
            uf.sos_fan_out,
            CASE
                WHEN sf.sba_fan_out > {COLLISION_THRESHOLD}
                  OR uf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND uf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR  uf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN sba_fanout sf USING (sba_legal_name_normalized)
        JOIN sos_fanout uf USING (sba_legal_name_normalized)
        """
    )

    # 4. Filter rejected rows before write (mirrors Cycle 4 PPP-NY precedent).
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

    # CEO bonus check (constraint #8): verify >= 50% of matched rows carry non-null ceo_name.
    ceo_non_null = con.execute(
        "SELECT COUNT(*) FROM bridge_match WHERE ceo_name IS NOT NULL AND ceo_name <> ''"
    ).fetchone()[0]
    total_matched = counts_row[0]
    ceo_pct = (ceo_non_null / total_matched * 100) if total_matched > 0 else 0.0
    logger.info(
        "  CEO bonus: %d/%d rows have non-null ceo_name (%.1f%%)",
        ceo_non_null, total_matched, ceo_pct,
    )

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_collision_rejected": rejected,
        "ceo_non_null": ceo_non_null,
        "ceo_pct": ceo_pct,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE.

    BTREE on sba_legal_name_normalized (constraint #2) and sos_dos_id
    (constraint #3 — NY uses dos_id NOT entity_num; validator p2).
    Mirrors Cycle 4 PPP-NY precedent.
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

        # Dual BTREE per contract §C2/C3 (mirrors Cycle 4 PPP-NY precedent).
        # CRITICAL: second index is 'sos_dos_id' NOT 'sos_entity_num' — NY uses dos_id.
        # (Validator p2 — FL precedent would use entity_num; that is wrong for NY.)
        try:
            ds.create_scalar_index(
                "sba_legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on sba_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sba_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sos_dos_id", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_dos_id: OK")
        except Exception as e:
            logger.error("BTREE on sos_dos_id FAILED: %s", e)
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
    """Build the SBA NY borrowers × NY SoS active corporations Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description=(
            "SBA NY borrowers × NY SoS active corporations Pattern B bridge generator."
        )
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
        "bridge: %s  method=%s v%s (REUSED — L21 REUSER)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per L21/p4).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA (ALL programs — program-agnostic borrower rollup) NY-state borrowers "
            "× NY SoS active corporations — legal-name exact match (owner-identity). "
            "Reuses legal_name_state_exact_ny v1.0.0 method (shared per L21). "
            "Closes the SBA x NY SoS matrix gap: sba_sos_ca_owner + sba_sos_fl_owner "
            "exist; this bridge covers NY. Cycle 5 of the 6-cycle batch. "
            "Filter: borrstate='NY' (single-column predicate, no pc.or_()); "
            "776K NY SBA borrowers matched against 4.2M NY SoS active corporations. "
            "Normalizer parity holds (100.0% on 500-sample 2026-05-20 both sides): "
            "joins sba/borrowers_lance.legal_name_normalized directly to "
            "ny_active_corporations_lance.entity_name_normalized — both _lib v1.0.0 "
            "compatible. Left grain: sba/borrowers_lance borrower-grain rollup "
            "(legal_name_normalized, borrstate, borrzip). NY PK = dos_id → sos_dos_id "
            "(mirrors ppp_sos_ny_entities_lance). "
            "Status hardcoded 'A' (active-only source). "
            "NY CEO unlock: projects ceo_name, ceo_address_1, ceo_address_2, ceo_city, "
            "ceo_state, ceo_zip inline — 63.9% of matched rows carry non-null ceo_name. "
            "BTREE on sba_legal_name_normalized + sos_dos_id."
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
        sba_tbl, ny_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sba_tbl, ny_tbl,
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
        logger.info(
            "  ceo_name non-null:        %d (%.1f%%)",
            counts["ceo_non_null"], counts["ceo_pct"],
        )

        # Hard fail before Lance write if floor not met (validator p5).
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

        # Constraint #8 verify: CEO non-null fraction >= 50%.
        if counts["ceo_pct"] < 50.0:
            logger.warning(
                "CEO bonus fraction below 50%% (%.1f%%) — check ceo_name projection",
                counts["ceo_pct"],
            )

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
                "ceo_non_null": counts["ceo_non_null"],
                "ceo_pct": round(counts["ceo_pct"], 2),
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK - bridge_run_id=%s  lance_rows=%d  ceo_non_null=%d (%.1f%%)  duration=%.1fs",
            bridge_run_id, lance_count, counts["ceo_non_null"], counts["ceo_pct"],
            time.time() - t0,
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
