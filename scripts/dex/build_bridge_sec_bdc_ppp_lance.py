"""SEC BDC Schedule-of-Investments portfolio companies × SBA PPP borrowers
Pattern B Lance bridge.

Pattern B exact-match bridge: SEC BDC SOI portfolio companies
(from sec_bdc/soi_lance: portfolio_company_name_clean and
portfolio_company_dba; entity_type='company') × SBA PPP borrowers
(sba/ppp_borrowers_lance: legal_name_normalized, borrstate, borrzip).

NAME-ONLY match — the BDC side has no state, ZIP, domain, or officer
field. Match nationally against the full 10.18M-row PPP corpus.

Method: company_name_exact_nostate v1.0.0 (NEW — registered here).
Because this is a NEW method_name, this script calls BOTH
register_match_method AND register_match_method_version per validator
prediction p1 (the L21 prohibition on the version-definition helper
applies only to REUSERS of a shared method row — there is no shared row
to corrupt here).

BDC left side (DISTINCT normalized-name grain — validator prediction p2):
  - Scan soi_lance company-typed rows.
  - For each row: pipe-split portfolio_company_name_clean on '|', take
    portfolio_company_dba.
  - Exclude raw portfolio_company_name_clean cells in the 21-string
    blacklist OR ending with ' Total' (validator prediction p3 — applied
    to the RAW cell BEFORE pipe-split; the ' Total' suffix rule is NOT
    a blanket %total check).
  - Fold each piece through entity_name_normalize.normalize_entity_name
    to produce the join key.
  - Collect DISTINCT non-None results. bdc_fan_out = 1 by construction.

PPP right side:
  - Full sba/ppp_borrowers_lance, 10,177,716 rows.
  - legal_name_normalized is ALREADY _lib-normalized (0 NULL, 0 empty).
  - JOIN DIRECTLY — do NOT re-normalize (validator prediction p4).
  - Carry borrstate + borrzip on every output row.

COLLISION_THRESHOLD = 50.
MIN_ROWS_MATCHED = 2_200 (validator-calibrated 2026-05-21; probe: 3,188
non-rejected = 1,102 platinum + 2,086 gold + 0 silver + 0 rejected).
Floor derivation: 2200 / 3188 = 69.0% — the ~70% convention per
DATA-FACTORY-LESSONS.md, rounded to a clean integer below 70% to absorb
minor SOI monthly-re-emit name drift without false-tripping a clean run.

Smoke target (validator-substituted 2026-05-21):
  'Accommodations Plus Technologies Holdings LLC' → normalize →
  'accommodations plus technologies holdings' → PPP row borrstate='NY'.
  (Kaseya rejected — kaseya is absent from ppp_borrowers_lance.)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_ppp_lance
    (BTREE on bdc_name_normalized AND ppp_legal_name_normalized —
    dual BTREE per contract)

New method registration:
    register_match_method(method_name='company_name_exact_nostate', ...)
    register_match_method_version(method_name='company_name_exact_nostate',
        semver='1.0.0', input_columns_left=['portfolio_company_name_clean',
        'portfolio_company_dba'],
        input_columns_right=['legal_name_normalized'], ...)
    register_bridge(bridge_name='sec_bdc_ppp', ...)

Bridge version: 1.0.0
Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_sec_bdc_ppp_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_sec_bdc_ppp_lance.py
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
# NEW method registration: this script is the PUBLISHER of company_name_exact_nostate.
# Per validator prediction p1, BOTH register_match_method AND
# register_match_method_version are called here (L21 prohibition applies only
# to REUSERS of a shared method; this method_name is brand-new).
# Do NOT reuse company_name_exact (SBA-lender/bank shaped) or name_exact_strict
# (USAspending-awardee/SAM shaped, asymmetric reject rule).
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps in verify constraints)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sec_bdc_ppp"                    # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "sec_bdc_ppp_lance"             # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "company_name_exact_nostate"     # NEW — registered by this script
METHOD_SEMVER = "1.0.0"                        # NEW version row
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-21 post full-corpus probe.
# BDC distinct normalized names: 13,459; probe: 3,188 non-rejected matched rows
# = 1,102 platinum + 2,086 gold + 0 silver + 0 rejected.
# Floor = 2,200 (~69% of probe; catches catastrophic failure from
# schema/normalizer regression without false-tripping on clean recipe runs).
MIN_ROWS_MATCHED = 2_200

SOURCE_LEFT = "soi_lance"
SOURCE_RIGHT = "ppp_borrowers_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sec_bdc/soi_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_ppp_lance"

TMP_DIR = "/tmp/lance"

# ---------------------------------------------------------------------------
# Left-side hygiene — sector-header blacklist (validator-derived 2026-05-21)
# Validator prediction p3: apply BOTH rules to the RAW portfolio_company_name_clean
# cell (pre-pipe-split) for company-typed rows.
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

    Two rules (validator prediction p3):
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
# Smoke target (validator-substituted 2026-05-21)
# ---------------------------------------------------------------------------

SMOKE_TARGET_RAW = "Accommodations Plus Technologies Holdings LLC"
SMOKE_TARGET_EXPECTED_STATE = "NY"

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

    Validator prediction p2: the BDC left side MUST be a DISTINCT normalized-name
    set (not at SOI-row grain). bdc_fan_out = 1 by construction.

    Steps:
    - Scan soi_lance for portfolio_company_entity_type='company'.
    - For each row:
      a. Check the raw portfolio_company_name_clean cell against the blacklist
         (BEFORE pipe-split — validator prediction p3).
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
        # Rule: apply blacklist to RAW cell BEFORE pipe-split (prediction p3).
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
    """Load BDC DISTINCT name set + PPP borrowers into Arrow tables.

    BDC side (DISTINCT normalized-name grain):
      - Built by _build_bdc_name_set — a DISTINCT set of normalized names.
      - bdc_fan_out = 1 by construction (prediction p2).
      - NO re-normalization of the PPP side (prediction p4).

    PPP side:
      - Full ppp_borrowers_lance (no state filter — NAME-ONLY, national).
      - Filter: legal_name_normalized is_valid().
      - Project: legal_name_normalized, borrstate, borrzip.
      - DO NOT call normalize_entity_name on legal_name_normalized — it is
        already _lib-normalized (0 NULL, 0 empty per validator probe).
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    bdc_names, raw_company_rows = _build_bdc_name_set(storage_options)

    # Convert DISTINCT BDC name set to an Arrow table for DuckDB registration.
    bdc_tbl = pa.table({"bdc_name": pa.array(sorted(bdc_names), type=pa.string())})
    logger.info("  bdc DISTINCT name table: %d rows", len(bdc_tbl))

    logger.info("opening sba/ppp_borrowers_lance (national, legal_name_normalized valid) ...")
    ppp_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    ppp_filter = pc.field("legal_name_normalized").is_valid()
    ppp_tbl = ppp_ds.scanner(
        columns=[
            "legal_name_normalized",
            "borrstate",
            "borrzip",
        ],
        filter=ppp_filter,
    ).to_table()
    rows_ppp = len(ppp_tbl)
    logger.info(
        "  ppp_borrowers_lance (legal_name_normalized is_valid): %d rows", rows_ppp
    )

    return bdc_tbl, ppp_tbl, raw_company_rows, rows_ppp


def _check_smoke_target(con, *, bridge_run_id: str) -> bool:
    """Check that the smoke target resolves through the bridge to borrstate='NY'.

    Validator prediction p5 smoke target:
      'Accommodations Plus Technologies Holdings LLC'
      → normalize_entity_name →
      'accommodations plus technologies holdings'
      → PPP row borrstate='NY'.
    """
    smoke_normed = normalize_entity_name(SMOKE_TARGET_RAW)
    logger.info(
        "smoke target: '%s' → normalize → '%s'", SMOKE_TARGET_RAW, smoke_normed
    )
    if smoke_normed is None:
        logger.error("smoke target normalized to None — normalizer failure")
        return False

    rows = con.execute(
        "SELECT ppp_borrstate FROM bridge_match WHERE bdc_name_normalized = ? LIMIT 5",
        [smoke_normed],
    ).fetchall()
    states = [r[0] for r in rows]
    logger.info("  smoke target '%s' bridge rows borrstate: %s", smoke_normed, states)
    if SMOKE_TARGET_EXPECTED_STATE in states:
        logger.info(
            "  SMOKE OK: found borrstate='%s'", SMOKE_TARGET_EXPECTED_STATE
        )
        return True
    logger.error(
        "  SMOKE FAIL: expected borrstate='%s', got states=%s",
        SMOKE_TARGET_EXPECTED_STATE,
        states,
    )
    return False


def _build_match_table(
    bdc_tbl,
    ppp_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: bdc.bdc_name = ppp.legal_name_normalized.
    BDC side is the DISTINCT normalized-name set — bdc_fan_out=1 by construction
    (prediction p2). The two-sided CASE is kept anyway (cheap, matches precedent
    shape, future-proof).

    Tier rule (symmetric two-sided):
        platinum = BOTH fan_out == 1
        gold     = ONE side fan_out == 1
        silver   = BOTH <= COLLISION_THRESHOLD
        rejected = EITHER > COLLISION_THRESHOLD

    PPP side is joined directly — legal_name_normalized is NOT re-normalized
    (prediction p4; it is already _lib-normalized).

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
    con.register("ppp", ppp_tbl)

    rows_bdc_reg = con.execute("SELECT COUNT(*) FROM bdc").fetchone()[0]
    rows_ppp_reg = con.execute("SELECT COUNT(*) FROM ppp").fetchone()[0]
    logger.info("  registered: bdc=%d  ppp=%d", rows_bdc_reg, rows_ppp_reg)

    # 1. INNER JOIN on normalized name.
    #    BDC: bdc.bdc_name (already DISTINCT normalized names).
    #    PPP: ppp.legal_name_normalized (already _lib-normalized — join directly;
    #         do NOT re-normalize prediction p4).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            b.bdc_name                       AS bdc_name_normalized,
            p.legal_name_normalized          AS ppp_legal_name_normalized,
            p.borrstate                      AS ppp_borrstate,
            p.borrzip                        AS ppp_borrzip,
            '{METHOD_NAME}'                  AS match_method,
            b.bdc_name                       AS match_value,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'   AS generated_at
        FROM bdc b
        JOIN ppp p
          ON b.bdc_name = p.legal_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided).
    #    bdc_fan_out: # of distinct BDC normalized names matching this key.
    #    ppp_fan_out: # of PPP rows (borrower locations) sharing this normalized name.
    #    Because BDC is a DISTINCT set, bdc_fan_out = 1 by construction.
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
        CREATE TEMP TABLE ppp_fanout AS
        SELECT bdc_name_normalized, COUNT(*) AS ppp_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided, mirrors PPP×SoS-CA precedent):
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
            pf.ppp_fan_out,
            CASE
                WHEN bf.bdc_fan_out > {COLLISION_THRESHOLD}
                  OR pf.ppp_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN bf.bdc_fan_out = 1 AND pf.ppp_fan_out = 1
                    THEN 'platinum'
                WHEN bf.bdc_fan_out = 1 OR  pf.ppp_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN bdc_fanout bf USING (bdc_name_normalized)
        JOIN ppp_fanout pf USING (bdc_name_normalized)
        """
    )

    # 4. Filter rejected rows before write (mirrors PPP×SoS-CA L344-349).
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
    max_ppp_fo = con.execute("SELECT MAX(ppp_fan_out) FROM bridge_all").fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_collision_rejected": rejected,
        "max_bdc_fan_out": max_bdc_fo or 0,
        "max_ppp_fan_out": max_ppp_fo or 0,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE.

    BTREE on bdc_name_normalized (BDC join key) and ppp_legal_name_normalized
    (PPP join key). Dual BTREE per contract.
    Wraps lance.write_dataset in lance_commit_lock (prediction p6).
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

        # Dual BTREE per contract §C2/C3 (prediction p6).
        try:
            ds.create_scalar_index("bdc_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on bdc_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on bdc_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("ppp_legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on ppp_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on ppp_legal_name_normalized FAILED: %s", e)
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
    """Build the SEC BDC SOI portfolio companies × SBA PPP borrowers Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description="SEC BDC SOI portfolio companies × SBA PPP borrowers Pattern B bridge generator."
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
        "bridge: %s  method=%s v%s (NEW — registered here)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Register new method + version + bridge (all idempotent UPSERTs).
    # Prediction p1: NEW method — call BOTH register_match_method AND
    # register_match_method_version (not just register_bridge as a reuser would).
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on normalized legal/entity name only, no state filter — "
            "for source pairs where one side has no usable state/geography column. "
            "scripts/_lib/entity_name_normalize.py v1.0.0 applied to the side(s) that "
            "need normalization. The right side (PPP legal_name_normalized) is already "
            "_lib-normalized and is joined directly."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="scripts/_lib/entity_name_normalize.py",
        normalizer_version="1.0.0",
        blacklist_module=None,
        blacklist_version=None,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M <=50; rejected=>50"
        ),
        rejection_rule_description=(
            "Either side fan-out > 50 (COLLISION_THRESHOLD=50); rows dropped before write."
        ),
        input_columns_left=["portfolio_company_name_clean", "portfolio_company_dba"],
        input_columns_right=["legal_name_normalized"],
        output_value_description=(
            "normalize_entity_name(portfolio_company_name_clean piece | portfolio_company_dba) "
            "= legal_name_normalized on the PPP side. DISTINCT normalized BDC names "
            "joined nationally against full ppp_borrowers_lance."
        ),
        tier_rule_config={
            "collision_threshold": COLLISION_THRESHOLD,
            "left_grain": "DISTINCT normalized names from pipe-split portfolio_company_name_clean + portfolio_company_dba",
            "right_grain": "(legal_name_normalized, borrstate, borrzip) borrower row",
        },
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SEC BDC Schedule-of-Investments portfolio companies × SBA PPP borrowers "
            "— name-only national exact match. "
            "New method company_name_exact_nostate v1.0.0 (publisher: this script). "
            "BDC side: DISTINCT normalize_entity_name(portfolio_company_name_clean pipe-split + dba) "
            "from soi_lance company-typed rows, sector-header blacklist applied (21-string exact + "
            "' Total' suffix rule). "
            "PPP side: full ppp_borrowers_lance (10.18M rows, all states), "
            "legal_name_normalized joined directly (already _lib-normalized). "
            "BTREE on bdc_name_normalized + ppp_legal_name_normalized. "
            "Floor: MIN_ROWS_MATCHED=2200 (validator-calibrated 2026-05-21; probe=3,188). "
            "Geography acquisition: carries borrstate + borrzip from PPP borrower."
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
        bdc_tbl, ppp_tbl, rows_left_raw, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            bdc_tbl, ppp_tbl,
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
        logger.info("  max_bdc_fan_out:          %d", counts["max_bdc_fan_out"])
        logger.info("  max_ppp_fan_out:          %d", counts["max_ppp_fan_out"])

        # Smoke target check (always run — even in dry-run mode).
        smoke_ok = _check_smoke_target(con, bridge_run_id=bridge_run_id)

        # Hard-fail floor check — BEFORE Lance write (prediction p5).
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
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
