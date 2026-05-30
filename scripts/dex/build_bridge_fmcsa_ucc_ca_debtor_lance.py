"""FMCSA CA carrier × CA UCC-1 debtor Pattern B Lance bridge.

Pattern B exact-match bridge: FMCSA motor carriers registered in California
(fmcsa/carrier_essentials_lance, phy_state='CA') × CA UCC-1 debtor filings
(ucc_ca/debtors_lance, DEBTOR_TYPE='Organization', STATE='CA'), joined on
normalized legal_name. Payoff: identify which CA motor carriers carry UCC liens
(equipment loans, asset-based lending, factoring) — a capital / lender-intelligence
layer over the CA carrier base.

Method: legal_name_state_exact_ca v1.0.0 (REUSED — the method + version rows
were registered by PR #464's build_bridge_sba_sos_ca_owner_lance.py;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run — the method-definition and
method-version-definition helpers are INTENTIONALLY OMITTED.
Calling register_match_method / register_match_method_version would UPSERT
over the shared match_method_versions row and corrupt the provenance trail of
all 12+ existing reusers: sba_sos_ca_owner, ucc_ca_debtor_sos_ca_owner,
ppp_ucc_ca_debtor, sam_ucc_ca_debtor, usaspending_ucc_ca_debtor, etc.)
Precedent: build_bridge_sam_pdl_domain_lance.py and
build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py.

FMCSA side:
  - scanner filter: phy_state='CA' (directive C8, validator-confirmed lowercase columns)
  - raw legal_name normalized Python-side via _lib.entity_name_normalize.normalize_entity_name
    (carrier_essentials_lance ALSO carries a pre-materialized legal_name_normalized
    column built with the same canonical v1.0.0 normalizer; we re-normalize fresh
    here so both sides share one identical Python code path — C3 parity).
  - columns: dot_number, legal_name, phy_state + normalized output.

UCC debtor side (mirrors build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py):
  - scanner filter: DEBTOR_TYPE='Organization' (validator-confirmed exact value;
    distinct DEBTOR_TYPE set is {'Organization','Individual'}; C8 org-to-org only)
    AND STATE='CA' (v1 scope — CA-symmetric join, directive C8).
  - raw ORG_NAME normalized Python-side via _lib.entity_name_normalize.normalize_entity_name
    (ONLY _lib — the legacy ucc_normalize.py diverges at 86.4% on 500-sample).
  - columns: UCC1_NUM, UCC3_NUM, DEBTOR_TYPE, ORG_NAME, ADDR1, CITY, STATE, POSTAL_CODE.
  - Filing-grain (no SELECT DISTINCT) — mirrors ucc_ca_debtor_sos_ca_owner precedent;
    the UCC filing-level detail (UCC1_NUM, UCC3_NUM, address) is meaningful downstream.

Normalizer discipline (C3):
  ONLY scripts._lib.entity_name_normalize.normalize_entity_name on BOTH sides.
  The module docstring warns against the "DuckDB SQL approximation of the Python rule"
  drift trap (28-entry generic-string blacklist + multi-token suffix regex cannot be
  faithfully inlined as SQL). Canonical reference: ucc_ca_debtor_sos_ca_owner_lance.py
  and the validator notes for this directive.

Fan-out (symmetric two-sided, validator-fixed bug):
  fmcsa_fan_out = COUNT(DISTINCT dot_number) per normalized name.
  ucc_fan_out   = COUNT(DISTINCT UCC1_NUM) per normalized name (filing grain retained).
  DO NOT use COUNT(*) over the join product — that makes both denominators identical
  and collapses the gold tier to 0 (validator's first benchmark draft bug, documented).

Tier rule (standard, directive C5):
  platinum = BOTH fan_out == 1 (1:1)
  gold     = EXACTLY ONE side == 1 (1:N or N:1)
  silver   = BOTH <= COLLISION_THRESHOLD (N:M, both ≤ 50)
  rejected = EITHER > COLLISION_THRESHOLD

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 300_000 (validator-calibrated 2026-05-21 — ~55% of measured
    est_rows_matched=545,143; conservative end of 55-60% band because name+state
    matching is fuzzier than domain/UEI joins. Hard fail if below floor — C9.)

Validator-measured estimate (benchmark run 3×, deterministic 0% stddev):
    rows_matched=545,143 (platinum=49,607; gold=346,921; silver=148,615; rejected=46,385)

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance
        (filter phy_state='CA'; normalize legal_name via _lib Python pre-pass)
    s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
        (filter DEBTOR_TYPE='Organization' AND STATE='CA';
         normalize ORG_NAME via _lib Python pre-pass)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_ucc_ca_debtor_lance
    (dual BTREE on carrier_name_normalized AND debtor_name_normalized)

Polaris (Out of scope — known/broken non-blocker):
    Polaris Generic Table registration is explicitly descoped to a non-blocking
    follow-up per the directive ## Out of scope. Do NOT block on it.

Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_fmcsa_ucc_ca_debtor_lance.py --apply

Dry-run (no Lance write; counts + tier breakdown only):
    doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_fmcsa_ucc_ca_debtor_lance.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 pattern — allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
# CRITICAL (C2 / validator P1 / L21): the method-definition and
# method-version-definition helpers are INTENTIONALLY OMITTED.
# legal_name_state_exact_ca v1.0.0 is REUSED from PR #464. Calling
# register_match_method / register_match_method_version would UPSERT over
# the shared match_method_versions row and corrupt the provenance trail of
# 12+ existing consumers. Precedent: build_bridge_sam_pdl_domain_lance.py.
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps and constraint checks C2–C9)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "fmcsa_ucc_ca_debtor"           # naked (ops.bridges convention)
DATASET_SLUG = "fmcsa_ucc_ca_debtor_lance"    # R2/lance suffix
METHOD_NAME = "legal_name_state_exact_ca"     # REUSED — registered by PR #464
METHOD_SEMVER = "1.0.0"                       # REUSED — version row from PR #464
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50  # >50 fan-out on either side → rejected (C5)
# Validator-calibrated 2026-05-21. ~55% of est_rows_matched=545,143.
# HARD FAIL if rows_matched < MIN_ROWS_MATCHED before any Lance write (C9).
MIN_ROWS_MATCHED = 300_000

SOURCE_LEFT = "fmcsa"
SOURCE_RIGHT = "ucc_ca_debtor"

FMCSA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
UCC_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance"
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_ucc_ca_debtor_lance"
)

TMP_DIR = "/tmp/lance"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_fmcsa_ucc_ca_debtor_lance")


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
    """Load FMCSA CA carriers + UCC CA org debtors into Arrow tables.

    FMCSA left side (C8 — CA filter, C3 — Python normalizer):
      - Scanner filter: phy_state='CA' (validator-confirmed lowercase col name;
        value is the literal string 'CA').
      - Project: dot_number, legal_name, phy_state.
      - Normalize legal_name Python-side via _lib.entity_name_normalize
        (fresh re-normalize for C3 code-path parity; the pre-materialized
        legal_name_normalized col is NOT used — single consistent path).
      - Drop rows where normalized name is None or empty.

    UCC right side (C8 — org filter + CA, C3 — Python normalizer):
      - Scanner filter: DEBTOR_TYPE='Organization' AND STATE='CA'
        (validator-confirmed: DEBTOR_TYPE distinct set is exactly
        {'Organization','Individual'}; STATE='CA' pins to CA-filed debtors).
      - Project: UCC1_NUM, UCC3_NUM, DEBTOR_TYPE, ORG_NAME, ADDR1, CITY,
        STATE, POSTAL_CODE (filing-grain — matches ucc_ca_debtor_sos_ca_owner).
      - Normalize ORG_NAME Python-side via _lib.entity_name_normalize.
      - Drop rows where normalized name is None or empty.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # FMCSA left side.
    logger.info("opening fmcsa/carrier_essentials_lance (phy_state='CA') ...")
    fmcsa_ds = lance.dataset(FMCSA_LANCE_URI, storage_options=storage_options)
    fmcsa_total = fmcsa_ds.count_rows()
    fmcsa_tbl = fmcsa_ds.scanner(
        columns=["dot_number", "legal_name", "phy_state"],
        filter=pc.field("phy_state") == "CA",
    ).to_table()
    rows_fmcsa_ca = len(fmcsa_tbl)
    logger.info(
        "  fmcsa carrier_essentials_lance total=%d  CA carriers=%d",
        fmcsa_total, rows_fmcsa_ca,
    )

    # Normalize legal_name in Python (canonical normalizer — C3, NOT DuckDB SQL).
    fmcsa_legal = fmcsa_tbl.column("legal_name").to_pylist()
    fmcsa_norm = [normalize_entity_name(n) for n in fmcsa_legal]
    fmcsa_tbl = fmcsa_tbl.append_column(
        "carrier_name_normalized",
        pa.array(fmcsa_norm, type=pa.string()),
    )
    # Drop rows where normalized name is None or empty.
    valid_mask = (
        pc.is_valid(fmcsa_tbl.column("carrier_name_normalized"))
    )
    fmcsa_tbl = fmcsa_tbl.filter(valid_mask)
    rows_fmcsa_post_norm = len(fmcsa_tbl)
    logger.info(
        "  fmcsa after normalization (carrier_name_normalized is_valid): %d rows",
        rows_fmcsa_post_norm,
    )

    # UCC right side (filing-grain, mirrors ucc_ca_debtor_sos_ca_owner pattern).
    logger.info(
        "opening ucc_ca/debtors_lance "
        "(DEBTOR_TYPE='Organization', STATE='CA') ..."
    )
    ucc_ds = lance.dataset(UCC_LANCE_URI, storage_options=storage_options)
    ucc_total = ucc_ds.count_rows()
    ucc_tbl = ucc_ds.scanner(
        columns=[
            "UCC1_NUM",
            "UCC3_NUM",
            "DEBTOR_TYPE",
            "ORG_NAME",
            "ADDR1",
            "CITY",
            "STATE",
            "POSTAL_CODE",
        ],
        filter=(
            (pc.field("DEBTOR_TYPE") == "Organization")
            & (pc.field("STATE") == "CA")
        ),
    ).to_table()
    rows_ucc_ca_org = len(ucc_tbl)
    logger.info(
        "  ucc debtors_lance total=%d  CA org debtors=%d",
        ucc_total, rows_ucc_ca_org,
    )

    # Normalize ORG_NAME in Python (canonical normalizer — C3).
    org_names = ucc_tbl.column("ORG_NAME").to_pylist()
    ucc_norm = [normalize_entity_name(n) for n in org_names]
    ucc_tbl = ucc_tbl.append_column(
        "debtor_name_normalized",
        pa.array(ucc_norm, type=pa.string()),
    )
    # Drop rows where normalized name is None or empty.
    valid_mask_ucc = pc.is_valid(ucc_tbl.column("debtor_name_normalized"))
    ucc_tbl = ucc_tbl.filter(valid_mask_ucc)
    rows_ucc_post_norm = len(ucc_tbl)
    logger.info(
        "  ucc after normalization (debtor_name_normalized is_valid): %d rows",
        rows_ucc_post_norm,
    )

    return (
        fmcsa_tbl,
        ucc_tbl,
        rows_fmcsa_ca,
        rows_ucc_ca_org,
    )


def _build_match_table(
    fmcsa_tbl,
    ucc_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: carrier_name_normalized = debtor_name_normalized
    (both sides are _lib v1.0.0 canonical-normalizer output — direct equality).

    Fan-out (symmetric two-sided — validator's corrected benchmark formula):
      fmcsa_fan_out = COUNT(DISTINCT dot_number) per normalized name.
      ucc_fan_out   = COUNT(DISTINCT UCC1_NUM) per normalized name.
      DO NOT use COUNT(*) over the join product — that makes both sides
      identical (= join-row count) and collapses the gold tier to 0.

    DuckDB settings: threads=4 (local), memory=12GB, temp to /tmp/lance.
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='200GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("fmcsa", fmcsa_tbl)
    con.register("ucc", ucc_tbl)

    rows_fmcsa_reg = con.execute("SELECT COUNT(*) FROM fmcsa").fetchone()[0]
    rows_ucc_reg = con.execute("SELECT COUNT(*) FROM ucc").fetchone()[0]
    logger.info(
        "  registered: fmcsa=%d  ucc=%d",
        rows_fmcsa_reg, rows_ucc_reg,
    )

    # 1. Inner JOIN on normalized legal name (both sides CA-pinned at scanner).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            f.dot_number,
            f.legal_name                        AS carrier_legal_name_raw,
            f.phy_state                         AS carrier_phy_state,
            f.carrier_name_normalized,
            u.UCC1_NUM                          AS ucc1_num,
            u.UCC3_NUM                          AS ucc3_num,
            u.DEBTOR_TYPE                       AS debtor_type,
            u.ORG_NAME                          AS debtor_org_name_raw,
            u.debtor_name_normalized,
            u.ADDR1                             AS debtor_addr1,
            u.CITY                              AS debtor_city,
            u.STATE                             AS debtor_state,
            u.POSTAL_CODE                       AS debtor_postal_code,
            '{METHOD_NAME}'                     AS match_method,
            f.carrier_name_normalized           AS match_value,
            '{BRIDGE_VERSION}'                  AS bridge_version,
            '{bridge_run_id}'                   AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'      AS generated_at
        FROM fmcsa f
        JOIN ucc u
          ON f.carrier_name_normalized = u.debtor_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Per-side fan-out counts (C5, validator-corrected formula).
    #    fmcsa_fan_out = # of distinct FMCSA carriers per normalized name.
    #    ucc_fan_out   = # of distinct UCC filings (UCC1_NUM) per normalized name.
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_fanout AS
        SELECT carrier_name_normalized,
               COUNT(DISTINCT dot_number) AS fmcsa_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ucc_fanout AS
        SELECT debtor_name_normalized,
               COUNT(DISTINCT ucc1_num) AS ucc_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (C5 standard: platinum/gold/silver/rejected).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            ff.fmcsa_fan_out,
            uf.ucc_fan_out,
            CASE
                WHEN ff.fmcsa_fan_out > {COLLISION_THRESHOLD}
                  OR uf.ucc_fan_out   > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.fmcsa_fan_out = 1 AND uf.ucc_fan_out = 1
                    THEN 'platinum'
                WHEN ff.fmcsa_fan_out = 1 OR  uf.ucc_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN fmcsa_fanout ff USING (carrier_name_normalized)
        JOIN ucc_fanout   uf USING (debtor_name_normalized)
        """
    )

    # 4. Filter rejected rows.
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
    """Write bridge_match to Lance inside commit lock + dual BTREE (C7).

    Dual BTREE on carrier_name_normalized AND debtor_name_normalized,
    both must succeed or the run fails.
    compact_files + cleanup_old_versions(7d) per template.
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

        # Dual BTREE (C7 — both must succeed).
        try:
            ds.create_scalar_index(
                "carrier_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on carrier_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on carrier_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index(
                "debtor_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on debtor_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on debtor_name_normalized FAILED: %s", e)
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
    """Build the FMCSA CA carriers × CA UCC-1 debtors Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description=(
            "FMCSA CA carrier × CA UCC-1 debtor Pattern B bridge. "
            "Matches CA motor carriers to CA UCC-1 debtor filings on normalized "
            "legal_name — identifies carriers carrying UCC liens (equipment finance, "
            "factoring). Method: legal_name_state_exact_ca v1.0.0 (L21 REUSE)."
        )
    )
    mode_grp = parser.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument(
        "--apply",
        action="store_true",
        help="Full pipeline: write Lance + BTREE + complete registry rows.",
    )
    mode_grp.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Count-only mode: produces match estimate + tier breakdown "
            "without writing Lance dataset or completing registry rows (C10)."
        ),
    )
    args = parser.parse_args()

    # Env validation.
    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        _ensure_db_url()
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    _ensure_db_url()
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED — L21)  normalizer=v%s  mode=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION,
        "dry-run" if args.dry_run else "apply",
    )
    logger.info("left:  %s", FMCSA_LANCE_URI)
    logger.info("right: %s", UCC_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)
    logger.info("floor: %d rows_matched (C9)", MIN_ROWS_MATCHED)
    logger.info(
        "estimate: 545,143 rows_matched "
        "(platinum=49,607; gold=346,921; silver=148,615; rejected=46,385)"
    )

    # Dry-run mode (C10): compute counts only; no registry writes, no Lance write.
    if args.dry_run:
        try:
            fmcsa_tbl, ucc_tbl, rows_left, rows_right = _materialize_inputs(
                storage_options,
            )
            con, counts = _build_match_table(
                fmcsa_tbl,
                ucc_tbl,
                bridge_run_id="00000000-0000-0000-0000-000000000000",
                generated_at_iso=started_at.isoformat(),
            )

            logger.info("-" * 60)
            logger.info("DRY-RUN tier distribution:")
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

            floor_met = counts["rows_matched"] >= MIN_ROWS_MATCHED
            result = {
                "status": "dry-run",
                "bridge_name": BRIDGE_NAME,
                "rows_matched": counts["rows_matched"],
                "rows_tier1_platinum": counts["rows_tier1"],
                "rows_tier2_gold": counts["rows_tier2"],
                "rows_tier3_silver": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "min_rows_matched_floor": MIN_ROWS_MATCHED,
                "floor_met": floor_met,
                "elapsed_s": round(time.time() - t0, 1),
            }
            print(json.dumps(result, indent=2))

            if not floor_met:
                logger.error(
                    "HARD FAIL (dry-run): rows_matched=%d < floor=%d",
                    counts["rows_matched"], MIN_ROWS_MATCHED,
                )
                return 1

            logger.info(
                "DRY-RUN OK — floor met (%d >= %d). duration=%.1fs",
                counts["rows_matched"], MIN_ROWS_MATCHED, time.time() - t0,
            )
            return 0

        except Exception as exc:
            logger.exception("dry-run failed")
            return 1

    # --- Full apply path ---

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per C2 / L21).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "FMCSA CA motor carriers × CA UCC-1 debtor filings (Organization-type) — "
            "normalized legal_name exact match. Identifies which CA motor carriers "
            "carry UCC liens (equipment loans, asset-based lending, factoring). "
            "Method: legal_name_state_exact_ca v1.0.0 (L21 REUSE — 13th+ consumer). "
            "FMCSA side: phy_state='CA' CA carriers from carrier_essentials_lance. "
            "UCC side: DEBTOR_TYPE='Organization' AND STATE='CA' filing-grain from "
            "ucc_ca/debtors_lance. Dual BTREE on carrier_name_normalized + "
            "debtor_name_normalized. Standalone name+state bridge (no UEI/federal "
            "identifier on UCC debtor side). Polaris registration deferred — "
            "known/broken non-blocker per directive ## Out of scope."
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
        fmcsa_tbl, ucc_tbl, rows_left, rows_right = _materialize_inputs(
            storage_options,
        )
        con, counts = _build_match_table(
            fmcsa_tbl,
            ucc_tbl,
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

        # C9 hard-fail floor — must check BEFORE any Lance write.
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,} — check phy_state/DEBTOR_TYPE/STATE "
                f"filters and normalizer (C8/C3)"
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
            "OK — bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        logger.info("output: %s", BRIDGE_LANCE_URI)
        logger.info(
            "NOTE: Polaris Generic Table registration DEFERRED — "
            "known/broken non-blocker per directive ## Out of scope. "
            "Dataset readable directly from R2 via PyLance regardless of catalog state."
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
