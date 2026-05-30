"""SEC BDC Schedule-of-Investments portfolio companies × CO Secretary-of-State entities
Pattern B Lance bridge.

Pattern B exact-match bridge: SEC BDC SOI portfolio companies
(from sec_bdc/soi_lance: portfolio_company_name_clean and
portfolio_company_dba; entity_type='company') × CO SoS registered entities
(sos/co_entities_lance: entity_name_normalized, entityid, entitystatus).

NAME-ONLY match — the BDC side has no state, ZIP, domain, or officer
field. Match nationally against the full ~3.05M-entity CO SoS corpus.

Method: company_name_exact_nostate v1.0.0 (REUSED — the method + version rows
were registered by build_bridge_sec_bdc_ppp_lance.py / PR #627;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run per L21 — the method-definition
and method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the shared match_method_versions row and corrupt
the sec_bdc_ppp bridge's provenance trail).
This is the FOURTH REUSER of company_name_exact_nostate v1.0.0
(publisher: PR #627 build_bridge_sec_bdc_ppp_lance.py;
first reuser: PR #630 build_bridge_sec_bdc_sos_ca_entities_lance.py;
second reuser: PR #631 build_bridge_sec_bdc_sos_fl_entities_lance.py;
third reuser: PR #633 build_bridge_sec_bdc_sos_ny_entities_lance.py).

BDC left side (DISTINCT normalized-name grain — same as BDC×PPP, PR #627,
BDC×CA-SoS, PR #630, BDC×FL-SoS, PR #631, and BDC×NY-SoS, PR #633):
  - Scan soi_lance company-typed rows.
  - For each row: pipe-split portfolio_company_name_clean on '|', take
    portfolio_company_dba.
  - Exclude raw portfolio_company_name_clean cells in the 21-string
    blacklist OR ending with ' Total' (applied to the RAW cell BEFORE
    pipe-split; the ' Total' suffix rule is NOT a blanket %total check).
  - Fold each piece through entity_name_normalize.normalize_entity_name.
  - Collect DISTINCT non-None results. bdc_fan_out = 1 by construction.

CO SoS right side:
  - Full sos/co_entities_lance (3,049,389 rows — ONE-ROW-PER-ENTITY,
    validator-confirmed 2026-05-21: COUNT(*) = COUNT(DISTINCT entityid)
    = 3,049,389, ratio 1.00000, zero null entityid).
  - entity_name_normalized is ALREADY _lib-normalized — join directly.
  - *** CRITICAL (validator prediction p4): do NOT re-normalize the CO side
    from raw entityname. co_entities_lance.entity_name_normalized already
    has a CO-ingest pre-step that strips a trailing registration-status
    suffix (", <Status> <Month> <day>, <year>") from entityname BEFORE
    normalizing. ~75% of CO rows carry such a suffix; re-normalizing from
    raw entityname would break matching for those rows. Join the stored
    entity_name_normalized directly. ***
  - CO entity PK = entityid (CO Secretary-of-State entity ID — NOT dos_id
    as NY uses; NOT entity_num as CA/FL use).
  - CO HAS a real status column entitystatus (free-text: 'Good Standing',
    'Delinquent', 'Voluntarily Dissolved', 'Administratively Dissolved',
    etc.). Carry it verbatim as sos_entity_status — do NOT project literal
    'A' (that was an NY-only hack; CO has a real entitystatus).
  - No state filter — NAME-ONLY, national match.

*** GRAIN — CO-SPECIFIC (validator prediction p3) ***
co_entities_lance is ONE-ROW-PER-ENTITY (3,049,389 rows / 3,049,389
distinct entityid; ratio 1.00000; zero null entityid). Unlike NY (exact 2×
duplicated dump), CO has NO source row duplication. The DISTINCT-pair
collapse is still mandated as a forward-safe contract (a no-op on today's
CO data but mandatory per directive). The bridge:
  (a) Emits DISTINCT (bdc_name_normalized, sos_entity_id) pairs — SELECT
      DISTINCT on the joined pair (no-op on CO's clean PK, but the contract).
  (b) Counts CO-side fan-out as COUNT(DISTINCT entityid) per bdc_name_normalized
      (mirrors the NY/FL/CA BDC-SoS pattern — defense-in-depth).
Validator floor-calibration probe (3× deterministic, identical every run):
  Raw INNER JOIN COUNT(*) = 3,534
  DISTINCT (bdc_name, entityid) pairs = 3,534 (equal — confirms one-row-per-entity)
  Non-rejected matched rows (DISTINCT pairs) = 3,534
  Platinum / gold / silver / rejected = 1,867 / 1,667 / 0 / 0
  Max fan-out: BDC=1, CO=17

COLLISION_THRESHOLD = 50.
MIN_ROWS_MATCHED = 2_400 (validator-calibrated 2026-05-21; full-corpus probe:
3,534 non-rejected DISTINCT pairs = 1,867 platinum + 1,667 gold + 0 silver + 0
rejected. Floor = 2,400 / 3,534 = 67.9% — the ~70% convention per
DATA-FACTORY-LESSONS.md, rounded to clean integer below 70%, mirroring
FL's 3,500/5,087 = 68.8% and NY's 1,600/2,323 = 68.9%).

Smoke target (validator-confirmed 2026-05-21):
  'Iron Mountain Information Management, LLC'
  → normalize → 'iron mountain information management'
  → CO SoS sos_entity_id='19971169145' (entitystatus 'Good Standing', platinum tier).
  CO fan-out = 1, BDC fan-out = 1 → platinum.

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sos_co_entities_lance
    (BTREE on bdc_name_normalized AND sos_entity_id — dual BTREE per contract;
    sos_entity_id is the CO PK, NOT sos_dos_id as NY uses, NOT sos_entity_num as CA/FL use)

Match method REUSE (validator p5 / L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    [build_match_table + DISTINCT-pair collapse + floor check BEFORE write]
    write Lance + BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 / validator p5 — REUSER pattern mirrors PR #633.)

Bridge version: 1.0.0
Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_sec_bdc_sos_co_entities_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_sec_bdc_sos_co_entities_lance.py
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

from scripts._lib.entity_name_normalize import (  # noqa: F401 — __version__ for log provenance only
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL validator p5 / L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle is the FOURTH REUSER of
# company_name_exact_nostate v1.0.0 (publisher: PR #627 build_bridge_sec_bdc_ppp_lance.py;
# first reuser: PR #630 build_bridge_sec_bdc_sos_ca_entities_lance.py;
# second reuser: PR #631 build_bridge_sec_bdc_sos_fl_entities_lance.py;
# third reuser: PR #633 build_bridge_sec_bdc_sos_ny_entities_lance.py).
# Calling those helpers would UPSERT over the shared match_method_versions row and
# corrupt the sec_bdc_ppp bridge's provenance trail. REUSER pattern: call ONLY
# register_bridge, start_bridge_run, complete_bridge_run, fail_bridge_run.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps in verify constraints)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sec_bdc_sos_co_entities"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "sec_bdc_sos_co_entities_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "company_name_exact_nostate"        # REUSED — registered by PR #627
METHOD_SEMVER = "1.0.0"                           # REUSED — version row from PR #627
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-21 — full-corpus probe (3× deterministic):
# BDC distinct normalized names: 13,459; distinct-entity probe:
# 3,534 non-rejected DISTINCT (bdc_name, entityid) pairs
# = 1,867 platinum + 1,667 gold + 0 silver + 0 rejected.
# (Raw INNER JOIN COUNT(*) = 3,534 — equal to DISTINCT-pair count; CO one-row-per-entity.)
# Floor = 2,400 (~67.9% of 3,534; catches catastrophic failure from
# schema/normalizer/grain regression without false-tripping on clean recipe runs).
MIN_ROWS_MATCHED = 2_400

SOURCE_LEFT = "soi_lance"
SOURCE_RIGHT = "sos_co_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sec_bdc/soi_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/co_entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sos_co_entities_lance"

TMP_DIR = "/tmp/lance"

# ---------------------------------------------------------------------------
# Left-side hygiene — sector-header blacklist (verbatim from PR #627 / PR #630 / PR #631 / PR #633)
# Validator prediction p6: copy SECTOR_HEADER_BLACKLIST, _is_sector_header,
# and _build_bdc_name_set EXACTLY from build_bridge_sec_bdc_sos_ny_entities_lance.py.
# Applied to the RAW portfolio_company_name_clean cell (pre-pipe-split)
# for company-typed rows.
# ---------------------------------------------------------------------------

# Rule 1: 21-string exact-match blacklist (32,694 company-typed rows).
# Hard-coded verbatim from directive Constraints §"Left-side hygiene".
SECTOR_HEADER_BLACKLIST: frozenset[str] = frozenset({
    "Software",
    "Business Services",
    "Healthcare",
    "Company",
    "Portfolio Company",
    "Consumer Services",
    "Financial Services",
    "Education",
    "Packaging",
    "Energy",
    "Consumer Products",
    "Business Products",
    "Food & Beverage",
    "Healthcare Services",
    "Information Technology",
    "Distribution & Logistics",
    "Specialty Chemicals & Materials",
    "Financial Services & Technology",
    "Net Lease",
    "Non-Control/Non-Affiliate Investments",
    "Unfunded Loan Commitments",
})


def _is_sector_header(raw_clean: str | None) -> bool:
    """Return True if the raw portfolio_company_name_clean cell is a sector
    header and should be excluded before matching.

    Two rules (validator prediction p6):
    1. Exact-match against the 21-string blacklist.
    2. ' Total' word-suffix rule: cell.strip().endswith(' Total').
       This catches 'Aerospace & Defense Total', 'Grand Total', etc.
       NOTE: do NOT use endswith('total') — that wrongly catches real-company
       '…Subtotal' rows (e.g. 'Axiado Corporation Subtotal').
    """
    if raw_clean is None:
        return False
    stripped = raw_clean.strip()
    # Rule 1: exact-match blacklist
    if stripped in SECTOR_HEADER_BLACKLIST:
        return True
    # Rule 2: ' Total' word-suffix (space-prefixed; NOT generic 'total' suffix)
    if stripped.endswith(" Total"):
        return True
    return False


# ---------------------------------------------------------------------------
# Smoke target (validator-confirmed 2026-05-21)
# ---------------------------------------------------------------------------

SMOKE_TARGET_RAW = "Iron Mountain Information Management, LLC"
SMOKE_TARGET_EXPECTED_ENTITY_ID = "19971169145"

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


def _build_bdc_name_set(storage_options: dict) -> tuple[set[str], int]:
    """Build the DISTINCT set of normalize_entity_name outputs from BDC SOI.

    Copied VERBATIM from build_bridge_sec_bdc_sos_ny_entities_lance.py (PR #633) per
    validator prediction p6: the BDC left side MUST be a DISTINCT normalized-name
    set (not at SOI-row grain). bdc_fan_out = 1 by construction.

    Steps:
    - Scan soi_lance for portfolio_company_entity_type='company'.
    - For each row:
      a. Check the raw portfolio_company_name_clean cell against the blacklist
         (BEFORE pipe-split).
      b. If not blacklisted: pipe-split portfolio_company_name_clean on '|',
         normalize each piece via normalize_entity_name.
      c. Take portfolio_company_dba, normalize via normalize_entity_name.
    - Collect DISTINCT non-None results.

    Returns (bdc_name_set, raw_company_rows_scanned).
    """
    import lance
    import pyarrow.compute as pc

    logger.info("opening sec_bdc/soi_lance (entity_type='company') ...")
    soi_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)

    soi_filter = pc.field("portfolio_company_entity_type") == "company"
    soi_tbl = soi_ds.scanner(
        columns=[
            "portfolio_company_name_clean",
            "portfolio_company_dba",
            "portfolio_company_entity_type",
        ],
        filter=soi_filter,
    ).to_table()

    raw_company_rows = len(soi_tbl)
    logger.info("  soi_lance company-typed rows: %d", raw_company_rows)

    clean_col = soi_tbl.column("portfolio_company_name_clean").to_pylist()
    dba_col = soi_tbl.column("portfolio_company_dba").to_pylist()

    bdc_names: set[str] = set()
    blacklisted_rows = 0
    for raw_clean, raw_dba in zip(clean_col, dba_col):
        # Rule: apply blacklist to RAW cell BEFORE pipe-split.
        if _is_sector_header(raw_clean):
            blacklisted_rows += 1
            continue
        # Pipe-split portfolio_company_name_clean on '|'.
        if raw_clean:
            for piece in raw_clean.split("|"):
                piece = piece.strip()
                if piece:
                    normed = normalize_entity_name(piece)
                    if normed:
                        bdc_names.add(normed)
        # Also take portfolio_company_dba.
        if raw_dba:
            raw_dba = raw_dba.strip()
            if raw_dba:
                normed_dba = normalize_entity_name(raw_dba)
                if normed_dba:
                    bdc_names.add(normed_dba)

    logger.info(
        "  blacklisted company rows (sector headers + Total): %d", blacklisted_rows
    )
    logger.info(
        "  distinct normalized BDC names (after blacklist + normalize): %d",
        len(bdc_names),
    )
    return bdc_names, raw_company_rows


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load BDC DISTINCT name set + CO SoS entities into Arrow tables.

    BDC side (DISTINCT normalized-name grain):
      - Built by _build_bdc_name_set — a DISTINCT set of normalized names.
      - bdc_fan_out = 1 by construction.

    CO SoS side (validator predictions p1, p2, p4):
      - Full sos/co_entities_lance (no state filter — NAME-ONLY, national).
      - Filter: entity_name_normalized is_valid().
      - Project: entityid (CO PK — NOT dos_id as NY; NOT entity_num as CA/FL; p1),
        entity_name_normalized, entitystatus (real CO status column — p2).
        *** CRITICAL prediction p1: CO uses entityid NOT dos_id / entity_num ***
        *** CRITICAL prediction p2: CO HAS a real entitystatus column — scan it ***
        *** CRITICAL prediction p4: join entity_name_normalized DIRECTLY — DO NOT
            re-normalize from raw entityname (CO ingest strips status suffix before
            normalizing; re-normalizing would break ~75% of CO rows) ***
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    bdc_names, raw_company_rows = _build_bdc_name_set(storage_options)

    # Convert DISTINCT BDC name set to an Arrow table for DuckDB registration.
    bdc_tbl = pa.table({"bdc_name": pa.array(sorted(bdc_names), type=pa.string())})
    logger.info("  bdc DISTINCT name table: %d rows", len(bdc_tbl))

    logger.info("opening sos/co_entities_lance (entity_name_normalized is_valid) ...")
    sos_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    # *** CRITICAL prediction p1 ***
    # Scan 'entityid' (the CO Secretary-of-State PK) — NOT 'dos_id' (NY) and NOT
    # 'entity_num' (CA/FL SoS column).
    # *** CRITICAL prediction p2 ***
    # Scan 'entitystatus' (CO has a real status column — free-text values:
    # 'Good Standing', 'Delinquent', 'Voluntarily Dissolved', etc.).
    # *** CRITICAL prediction p4 ***
    # Scan 'entity_name_normalized' and join it DIRECTLY — do NOT re-normalize
    # from raw entityname. The stored column already strips the CO-ingest
    # registration-status suffix (", <Status> <Month> <day>, <year>") before
    # normalizing; re-normalizing from raw entityname would break ~75% of CO rows.
    sos_tbl = sos_ds.scanner(
        columns=[
            "entityid",
            "entity_name_normalized",
            "entitystatus",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info(
        "  sos co_entities_lance (entity_name_normalized is_valid): %d rows"
        " (one-row-per-entity per validator; DISTINCT pairs are a no-op on CO's clean PK)",
        rows_sos,
    )

    return bdc_tbl, sos_tbl, raw_company_rows, rows_sos


def _check_smoke_target(con, *, bridge_run_id: str) -> bool:
    """Check that the smoke target resolves through the bridge to sos_entity_id='19971169145'.

    Validator-confirmed 2026-05-21 smoke target:
      'Iron Mountain Information Management, LLC'
      → normalize_entity_name →
      'iron mountain information management'
      → CO SoS sos_entity_id='19971169145' (entitystatus 'Good Standing', platinum tier).

    CO is one-row-per-entity so the DISTINCT step is a no-op for this target
    (1 raw CO row → 1 bridge pair).
    """
    smoke_normed = normalize_entity_name(SMOKE_TARGET_RAW)
    logger.info(
        "smoke target: '%s' → normalize → '%s'", SMOKE_TARGET_RAW, smoke_normed
    )
    if smoke_normed is None:
        logger.error("smoke target normalized to None — normalizer failure")
        return False

    rows = con.execute(
        "SELECT sos_entity_id, confidence_tier FROM bridge_match WHERE bdc_name_normalized = ? LIMIT 10",
        [smoke_normed],
    ).fetchall()
    entity_ids = [r[0] for r in rows]
    tiers = [r[1] for r in rows]
    logger.info(
        "  smoke target '%s' bridge rows sos_entity_id: %s  tiers: %s",
        smoke_normed, entity_ids, tiers,
    )
    if SMOKE_TARGET_EXPECTED_ENTITY_ID in entity_ids:
        logger.info(
            "  SMOKE OK: found sos_entity_id='%s'", SMOKE_TARGET_EXPECTED_ENTITY_ID
        )
        return True
    logger.error(
        "  SMOKE FAIL: expected sos_entity_id='%s', got entity_ids=%s",
        SMOKE_TARGET_EXPECTED_ENTITY_ID,
        entity_ids,
    )
    return False


def _build_match_table(
    bdc_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + DISTINCT-pair collapse + fan-out tiering in DuckDB.

    Join key: bdc.bdc_name = sos.entity_name_normalized.
    BDC side is the DISTINCT normalized-name set — bdc_fan_out=1 by construction.
    CO SoS side is joined directly — entity_name_normalized is already
    _lib-normalized at emit (join directly; do NOT re-normalize per directive/p4).

    *** GRAIN — CO one-row-per-entity (validator prediction p3) ***
    After the INNER JOIN, materialize bridge_raw as
        SELECT DISTINCT bdc_name_normalized, sos_entity_id, sos_entity_status
    The DISTINCT step is a no-op on CO's clean one-row-per-entity data but
    is mandated as a forward-safe contract (per directive GRAIN section).
    Without it, a future CO re-ingest dup would silently double bridge rows.

    CO-side fan-out is counted as COUNT(DISTINCT entityid) per bdc_name_normalized
    (NOT raw COUNT(*)) — mirrors the NY/FL/CA BDC-SoS pattern.

    *** prediction p2: CO HAS a real entitystatus column ***
    Project n.entitystatus AS sos_entity_status — carry the real CO status value.
    Do NOT project literal 'A' (that was an NY-only hack; CO has free-text statuses).

    *** prediction p1: CO PK is entityid ***
    Project n.entityid AS sos_entity_id. Do NOT reference dos_id / entity_num.

    Tier rule (symmetric two-sided):
        platinum = BOTH fan_out == 1
        gold     = ONE side fan_out == 1
        silver   = BOTH <= COLLISION_THRESHOLD
        rejected = EITHER > COLLISION_THRESHOLD

    Returns (duckdb_connection, counts_dict).
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("bdc", bdc_tbl)
    con.register("sos", sos_tbl)

    rows_bdc_reg = con.execute("SELECT COUNT(*) FROM bdc").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos").fetchone()[0]
    logger.info("  registered: bdc=%d  sos=%d (one-row-per-entity)", rows_bdc_reg, rows_sos_reg)

    # 1. INNER JOIN on normalized name, then DISTINCT (bdc_name_normalized, sos_entity_id)
    #    forward-safe collapse (a no-op on CO's clean one-row-per-entity data — p3).
    #    BDC: bdc.bdc_name (already DISTINCT normalized names — bdc_fan_out=1
    #         by construction).
    #    SoS: sos.entity_name_normalized (already _lib-normalized at emit —
    #         join directly; do NOT re-normalize per directive/p4).
    #    *** prediction p1: project n.entityid AS sos_entity_id (NOT dos_id / entity_num) ***
    #    *** prediction p2: carry n.entitystatus AS sos_entity_status (real CO status) ***
    #    *** prediction p3: SELECT DISTINCT (bdc_name_normalized, sos_entity_id, entitystatus)
    #        — entitystatus is functionally dependent on entityid so it does not change
    #        the (bdc_name, entityid) pair grain ***
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT DISTINCT
            b.bdc_name                       AS bdc_name_normalized,
            n.entityid                       AS sos_entity_id,
            n.entitystatus                   AS sos_entity_status
        FROM bdc b
        JOIN sos n
          ON b.bdc_name = n.entity_name_normalized
        """
    )
    rows_matched_raw_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info(
        "  bridge_raw (DISTINCT bdc_name_normalized, sos_entity_id pairs): %d rows"
        " (CO one-row-per-entity so raw JOIN count == DISTINCT-pair count)",
        rows_matched_raw_pre,
    )

    # 2. Materialize full bridge row with provenance columns.
    #    Built from the DISTINCT (bdc_name_normalized, sos_entity_id) pairs.
    #    *** prediction p2: sos_entity_status from bridge_raw (real CO status) ***
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_full AS
        SELECT
            r.bdc_name_normalized,
            r.sos_entity_id,
            r.bdc_name_normalized            AS sos_entity_name_normalized,
            r.sos_entity_status,
            '{METHOD_NAME}'                  AS match_method,
            r.bdc_name_normalized            AS match_value,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'   AS generated_at
        FROM bridge_raw r
        """
    )

    # 3. Fan-out counts (symmetric two-sided, on DISTINCT pairs).
    #    bdc_fan_out: # of distinct BDC normalized names matching this key.
    #    sos_fan_out: # of distinct CO entityids for this normalized name.
    #    *** GRAIN: both computed on bridge_raw which already holds DISTINCT pairs ***
    #    *** fan-out on COUNT(DISTINCT sos_entity_id) per p3 (mirrors BDC-SoS precedents) ***
    con.execute(
        """
        CREATE TEMP TABLE bdc_fanout AS
        SELECT bdc_name_normalized,
               COUNT(DISTINCT bdc_name_normalized) AS bdc_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT bdc_name_normalized,
               COUNT(DISTINCT sos_entity_id) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 4. Tier rule (symmetric two-sided):
    #    platinum = BOTH fan_out == 1
    #    gold     = ONE side fan_out == 1
    #    silver   = BOTH <= COLLISION_THRESHOLD
    #    rejected = EITHER > COLLISION_THRESHOLD
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            bf.bdc_fan_out,
            sf.sos_fan_out,
            CASE
                WHEN bf.bdc_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN bf.bdc_fan_out = 1 AND sf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN bf.bdc_fan_out = 1 OR  sf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_full b
        JOIN bdc_fanout bf USING (bdc_name_normalized)
        JOIN sos_fanout sf USING (bdc_name_normalized)
        """
    )

    # 5. Filter rejected rows before write.
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
    max_bdc_fo = con.execute("SELECT MAX(bdc_fan_out) FROM bridge_all").fetchone()[0]
    max_sos_fo = con.execute("SELECT MAX(sos_fan_out) FROM bridge_all").fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_collision_rejected": rejected,
        "max_bdc_fan_out": max_bdc_fo or 0,
        "max_sos_fan_out": max_sos_fo or 0,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE.

    BTREE on bdc_name_normalized (BDC join key) and sos_entity_id
    (CO SoS PK — directive Constraint; NOT sos_dos_id as NY uses;
    NOT sos_entity_num as CA/FL use; NOT ppp_legal_name_normalized as PPP×NY).
    Dual BTREE per contract. Wraps lance.write_dataset in lance_commit_lock.
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

        # Dual BTREE per contract (prediction p7):
        # BTREE on bdc_name_normalized + sos_entity_id (CO PK).
        # NOTE: do NOT use sos_dos_id (NY PK — does not exist in CO schema).
        # NOTE: do NOT use sos_entity_num (CA/FL SoS key — does not exist in CO schema).
        try:
            ds.create_scalar_index("bdc_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on bdc_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on bdc_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sos_entity_id", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_entity_id: OK")
        except Exception as e:
            logger.error("BTREE on sos_entity_id FAILED: %s", e)
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
    """Build the SEC BDC SOI portfolio companies × CO SoS entities Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description="SEC BDC SOI portfolio companies × CO SoS entities Pattern B bridge generator."
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
        "bridge: %s  method=%s v%s (REUSED from PR #627, 4th REUSER)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)
    logger.info(
        "GRAIN: CO dataset is one-row-per-entity (validator-confirmed) — "
        "emitting DISTINCT (bdc_name, entityid) pairs (no-op on CO's clean PK, but the contract); "
        "fan-out counted on COUNT(DISTINCT entityid)"
    )

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per L21/p5).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SEC BDC Schedule-of-Investments portfolio companies × Colorado Secretary-of-State "
            "registered business entities — name-only national exact match. "
            "Reuses company_name_exact_nostate v1.0.0 method registered by PR #627 "
            "(sec_bdc_ppp publisher). Fourth REUSER (first: PR #630 BDC×CA-SoS; "
            "second: PR #631 BDC×FL-SoS; third: PR #633 BDC×NY-SoS). "
            "BDC side: DISTINCT normalize_entity_name(portfolio_company_name_clean pipe-split + dba) "
            "from soi_lance company-typed rows, sector-header blacklist applied (21-string exact + "
            "' Total' suffix rule). ~13,459 distinct normalized names. "
            "CO SoS side: full co_entities_lance (3,049,389 rows — one-row-per-entity, "
            "validator-confirmed 2026-05-21). entity_name_normalized joined directly "
            "(already _lib-normalized with CO-ingest status-suffix strip; do NOT re-normalize). "
            "GRAIN: bridge emits DISTINCT (bdc_name_normalized, sos_entity_id) pairs "
            "(no-op on CO one-row-per-entity, but forward-safe contract); "
            "fan-out counted on COUNT(DISTINCT entityid) per name. "
            "CO entity PK: entityid (CO Secretary-of-State ID) → sos_entity_id. "
            "Status: real CO entitystatus column (free-text: Good Standing / Delinquent / "
            "Voluntarily Dissolved / etc.) → sos_entity_status. "
            "No state filter — NAME-ONLY national match. "
            "BTREE on bdc_name_normalized + sos_entity_id. "
            "Floor: MIN_ROWS_MATCHED=2400 (validator-calibrated 2026-05-21; "
            "full-corpus probe=3,534 DISTINCT pairs). "
            "Carries sos_entity_id + sos_entity_status from CO SoS."
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
        bdc_tbl, sos_tbl, rows_left_raw, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            bdc_tbl, sos_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution (DISTINCT bdc_name+sos_entity_id pairs):")
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
        logger.info("  max_bdc_fan_out:          %d", counts["max_bdc_fan_out"])
        logger.info("  max_sos_fan_out:          %d (COUNT(DISTINCT entityid) per name)", counts["max_sos_fan_out"])

        # Smoke target check (always run — even in dry-run mode).
        smoke_ok = _check_smoke_target(con, bridge_run_id=bridge_run_id)

        # Hard-fail floor check — BEFORE Lance write (per directive Constraints).
        # Floor is on DISTINCT (bdc_name_normalized, sos_entity_id) pairs.
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,} (DISTINCT pairs)"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

        if not args.apply:
            # Dry-run: mark as failed-dry-run so the run is not left orphaned.
            smoke_note = "smoke=OK" if smoke_ok else "smoke=FAIL"
            msg = f"dry-run; no Lance write (pass --apply to execute). {smoke_note}"
            logger.info("DRY-RUN: %s", msg)
            fail_bridge_run(run_uuid, msg)
            logger.info("bridge_run marked failed-dry-run (run_id=%s)", bridge_run_id)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left_raw,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
            },
        )
        logger.info(
            "OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        if not smoke_ok:
            logger.warning("WARNING: smoke target check FAILED after write — verify manually")
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
