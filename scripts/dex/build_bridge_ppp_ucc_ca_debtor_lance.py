"""PPP × CA UCC-1 debtor Pattern B Lance bridge.

Pattern B exact-match bridge: SBA PPP borrowers with borrstate='CA'
(from sba/ppp_borrowers_lance, Cycle 1 output — one row per
(legal_name_normalized, borrstate, borrzip) grain)
× CA UCC-1 debtor filings (ucc_ca/debtors_lance, Organization rows
deduped to debtor-name-grain via SELECT DISTINCT debtor_name_normalized).

Method: legal_name_state_exact_ca v1.0.0 (REUSED — the method + version rows
were registered by PR #464's build_bridge_sba_sos_ca_owner_lance.py;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run per L21 — the method-definition
and method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the shared match_method_versions row and corrupt
the provenance trail of all existing reusers).

PPP-side shape: IDENTICAL to build_bridge_ppp_sos_ca_entities_lance.py (Cycle 2).
    - borrstate='CA' scanner filter (SINGLE-column predicate — NOT an OR-predicate;
      no pc.or_() needed).
    - ppp_borrowers_lance is ALREADY at (legal_name_normalized, borrstate, borrzip)
      grain (Cycle 1 output, PR #574) — NO SELECT DISTINCT needed.
    - JOIN legal_name_normalized DIRECTLY — no re-normalize (parity holds 100.0%
      per Cycle 2 validator measurement; _lib v1.0.0 compatible).

UCC-debtor-side shape: ADAPTED from build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py.
    - ucc_ca/debtors_lance URI: s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
    - Scanner filter: DEBTOR_TYPE='Organization' (excludes ~2.2M individual-debtor rows).
    - Read raw ORG_NAME, normalize Python-side via _lib.entity_name_normalize.normalize_entity_name
      (canonical _lib v1.0.0 — the SAME normalizer that produced the PPP legal_name_normalized
      key; this is the validated join-safe path per Footgun 1 resolution).
    - Drop None/empty normalizations.
    - DELIBERATE DIVERGENCE from ucc_ca_debtor_sos_ca_owner precedent:
      ADD `SELECT DISTINCT debtor_name_normalized` dedup BEFORE the join
      (Footgun 2 resolution — mirrors Cycle 6 USAspending SELECT DISTINCT).
      The ucc_ca_debtor_sos_ca_owner precedent kept filing-grain for its downstream
      entity_num→ca_principals chain; this PPP-UCC bridge has no such need.
    - After dedup: ucc_fan_out ≡ 1 for every row → silver is structurally unreachable
      (bridge is platinum + gold only — CORRECT, not a bug).

Normalizer discipline (CRITICAL — Footgun 1 / validator p1):
    ONLY _lib.entity_name_normalize.normalize_entity_name on the UCC side.
    DO NOT import or call anything from _lib/ucc_normalize.py (86.4% divergence
    from _lib on 500-sample — proof it must never join against a _lib key).
    The PPP side is joined DIRECTLY on legal_name_normalized (no re-normalize).
    Anti-grep: no 'ucc_normalize' literal in this script (constraint #11).

Fan-out (CRITICAL — validator tier-rule trap):
    ppp_fan_out = COUNT(*) per legal_name_normalized (PPP rows per name).
    ucc_fan_out = COUNT(DISTINCT debtor_name_normalized) per name (always 1 post-dedup).
    DO NOT use COUNT(*) for both denominators — that collapses gold=0.
    The two denominators MUST be different.

Tier rule (symmetric two-sided, verbatim from Cycle 2):
    platinum = BOTH fan_out == 1
    gold     = EXACTLY ONE side == 1
    silver   = BOTH <= COLLISION_THRESHOLD (structurally unreachable post-dedup)
    rejected = EITHER > COLLISION_THRESHOLD

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 261_000 (validator-calibrated 2026-05-20 — ~70% of measured 373,503)

Expected baseline (validator-measured, deterministic 3× runs):
    rows_matched=373,503 (platinum=207,919; gold=165,584; silver=0; rejected=136)

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/
        (legal_name_normalized, borrstate, borrzip, total_ppp_loans, total_ppp_approval;
        filter: borrstate='CA' AND legal_name_normalized is_valid();
        ALREADY at (name, state, zip) grain — NO SELECT DISTINCT)
    s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
        (raw ORG_NAME; filter: DEBTOR_TYPE='Organization';
        normalize via _lib.entity_name_normalize; SELECT DISTINCT debtor_name_normalized)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_ucc_ca_debtor_lance
    (BTREE on ppp_legal_name_normalized AND ucc_debtor_name_normalized)

Registry (REUSER pattern — L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (register_match_method and register_match_method_version are INTENTIONALLY
    NOT IMPORTED — constraint #14 anti-grep; REUSE not redefine.)

Polaris (constraint #10 — SOFT):
    Deferred: init_polaris_lance_generic.py --namespace bridges --table ppp_ucc_ca_debtor_lance
    Polaris (Railway) is 502-down as of 2026-05-20 (validator-confirmed). Not a blocker.

Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_ppp_ucc_ca_debtor_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_ppp_ucc_ca_debtor_lance.py
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

from scripts._lib.entity_name_normalize import (  # noqa: F401 — __version__ for log provenance
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)
from scripts._lib.address_normalize import (
    __version__ as ADDR_NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# v2.0.0 (address-axis composite) — NEW method
# legal_name_state_exact_ca_with_address_corroboration v1.0.0; register
# method-definition + version-definition rows. Shared idempotently with the
# CA-scoped UCC × SoS / UCC × SAM sibling bridges (PR #805).
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps and constraint checks)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "ppp_ucc_ca_debtor"           # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "ppp_ucc_ca_debtor_lance"    # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ca_with_address_corroboration"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "2.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-20 post full-corpus baseline probe.
# PPP CA borrowers (borrstate='CA') = 1,132,256 rows; deduped UCC debtor names = 1,583,695.
# Observed non-rejected rows = 373,503 (platinum=207,919; gold=165,584; silver=0; rejected=136).
# Floor = 261,000 (~70% of measured 373,503; catches catastrophic failure from
# schema/normalizer regression without false-tripping on clean recipe runs per DATA-FACTORY-LESSONS).
MIN_ROWS_MATCHED = 261_000

SOURCE_LEFT = "ppp_borrowers_lance"
SOURCE_RIGHT = "ucc_ca_debtors_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance"
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_ucc_ca_debtor_lance"
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


def _ensure_db_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load PPP CA-state borrowers + UCC CA debtor names into Arrow tables.

    PPP side (PARITY — join on pre-normalized column directly):
      - Read ppp_borrowers_lance with push-down filter:
        borrstate='CA' AND legal_name_normalized is_valid()
        (SINGLE-column state predicate — no pc.or_() needed)
      - Project: legal_name_normalized, borrstate, borrzip, total_ppp_loans,
        total_ppp_approval
      - NO SELECT DISTINCT — ppp_borrowers_lance is already at (name,state,zip)
        borrower grain (Cycle 1 output, PR #574).
      - DO NOT call normalize_entity_name on legal_name_normalized — parity holds
        (100.0% on 500-sample; the column is already _lib v1.0.0 compatible).

    UCC debtor side (Footgun 1 + Footgun 2 resolutions):
      - Read ucc_ca/debtors_lance with push-down filter: DEBTOR_TYPE='Organization'
        (excludes ~2.2M individual rows).
      - Project ORG_NAME only (raw — the dataset has NO normalized-name column).
      - Normalize ORG_NAME Python-side via _lib.entity_name_normalize.normalize_entity_name
        (canonical _lib v1.0.0 — same normalizer that produced PPP legal_name_normalized).
      - Drop rows where normalized name is None or empty.
      - Return as Arrow table WITH debtor_name_normalized column.
      - The SELECT DISTINCT dedup happens later in DuckDB (Footgun 2 / constraint #8).
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # PPP left side.
    logger.info("opening sba/ppp_borrowers_lance (CA filter, single-column) ...")
    ppp_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    ppp_filter = (
        (pc.field("borrstate") == "CA")
        & pc.field("legal_name_normalized").is_valid()
    )
    ppp_tbl = ppp_ds.scanner(
        columns=[
            "legal_name_normalized",
            "borrstate",
            "borrzip",
            "total_ppp_loans",
            "total_ppp_approval",
            "borrower_address_normalized",
        ],
        filter=ppp_filter,
    ).to_table()
    rows_left = len(ppp_tbl)
    logger.info(
        "  ppp_borrowers_lance CA rows (borrstate='CA', normalized valid): %d",
        rows_left,
    )

    # UCC right side — raw ORG_NAME, normalize Python-side, drop None/empty.
    # Footgun 1: ONLY _lib.entity_name_normalize — NO ucc_normalize.
    logger.info("opening ucc_ca/debtors_lance (DEBTOR_TYPE='Organization') ...")
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

    # Normalize ORG_NAME in Python via _lib (canonical normalizer — constraint #11).
    org_names = ucc_tbl.column("ORG_NAME").to_pylist()
    normalized = [normalize_entity_name(n) for n in org_names]
    ucc_tbl = ucc_tbl.append_column(
        "debtor_name_normalized",
        pa.array(normalized, type=pa.string()),
    )
    # Filter out None/empty (suffix-stripped-to-empty, generic names, etc.).
    valid_mask = pc.is_valid(ucc_tbl.column("debtor_name_normalized"))
    ucc_tbl = ucc_tbl.filter(valid_mask)
    rows_ucc_post_norm = len(ucc_tbl)
    logger.info(
        "  ucc after _lib normalize (debtor_name_normalized is_valid): %d rows",
        rows_ucc_post_norm,
    )
    # Validator-measured post-normalize figure: 3,681,435. Log for reproducibility.
    logger.info(
        "  expected ucc post-norm from validator probe: 3,681,435 (delta: %+d)",
        rows_ucc_post_norm - 3_681_435,
    )

    return ppp_tbl, ucc_tbl, rows_left, rows_ucc_raw


def _build_match_table(
    ppp_tbl,
    ucc_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run dedup + exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Footgun 2 (constraint #8): SELECT DISTINCT debtor_name_normalized dedup on
    the UCC side BEFORE the join — mirrors Cycle 6 USAspending SELECT DISTINCT.
    Assert: COUNT(*) == COUNT(DISTINCT debtor_name_normalized) on the deduped UCC input.

    Join key: ppp.legal_name_normalized = ucc.ucc_debtor_name_normalized
    BOTH are _lib v1.0.0 normalized — join directly, no re-normalize.

    Fan-out (CRITICAL asymmetry — validator tier-rule trap):
        ppp_fan_out = COUNT(*) per legal_name_normalized (# of PPP borrower rows)
        ucc_fan_out = COUNT(DISTINCT debtor_name_normalized) per name (≡ 1 post-dedup)
    DO NOT use COUNT(*) for ucc_fan_out — that collapses gold=0.

    Expected post-dedup result: rows_matched=373,503; silver=0 (correct — ucc_fan_out≡1).
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
    con.register("ucc_filing", ucc_tbl)

    rows_ppp_reg = con.execute("SELECT COUNT(*) FROM ppp").fetchone()[0]
    rows_ucc_filing = con.execute("SELECT COUNT(*) FROM ucc_filing").fetchone()[0]
    logger.info(
        "  registered: ppp=%d  ucc_filing=%d",
        rows_ppp_reg, rows_ucc_filing,
    )

    # Footgun 2 dedup (constraint #8): collapse to debtor-name grain.
    # v2.0.0: also AGGREGATE all distinct UCC-filing addresses per name into a
    # LIST (`ucc_debtor_address_set`). A PPP borrower's address matches if it
    # appears in this set — different UCC filings under the same legal name
    # may file from different addresses, so the per-name address agreement is
    # an OR across the set. Mirrors the SAM × UCC v2.0.0 pattern (PR #805).
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
    # Constraint #8 assertion: COUNT(*) == COUNT(DISTINCT debtor_name_normalized).
    if rows_ucc_deduped != rows_ucc_distinct_check:
        raise RuntimeError(
            f"Constraint #8 VIOLATED: UCC dedup failed — "
            f"COUNT(*) {rows_ucc_deduped} != COUNT(DISTINCT) {rows_ucc_distinct_check}"
        )
    logger.info(
        "  constraint #8 PASS: COUNT(*) == COUNT(DISTINCT) == %d (debtor-name-grain confirmed)",
        rows_ucc_deduped,
    )
    logger.info(
        "  expected ucc distinct from validator probe: 1,583,695 (delta: %+d)",
        rows_ucc_deduped - 1_583_695,
    )

    # 1. Inner JOIN on normalized names; carry PPP borrower address + UCC address set.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            p.legal_name_normalized                AS ppp_legal_name_normalized,
            p.borrstate                            AS ppp_borrstate,
            p.borrzip                              AS ppp_borrzip,
            p.total_ppp_loans                      AS ppp_total_loans,
            p.total_ppp_approval                   AS ppp_total_approval,
            p.borrower_address_normalized          AS ppp_borrower_address_normalized,
            u.ucc_debtor_name_normalized,
            u.ucc_debtor_address_set,
            '{METHOD_NAME}'                        AS match_method,
            p.legal_name_normalized                AS match_value,
            '{BRIDGE_VERSION}'                     AS bridge_version,
            '{bridge_run_id}'                      AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'         AS generated_at
        FROM ppp p
        JOIN ucc u
          ON p.legal_name_normalized = u.ucc_debtor_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (CRITICAL asymmetry — validator tier-rule trap).
    #    ppp_fan_out: # of PPP rows (borrower locations) per normalized name.
    #    ucc_fan_out: # of distinct UCC debtor names per name (≡ 1 post-dedup always).
    #    DO NOT use COUNT(*) for ucc_fan_out — that would collapse gold=0.
    con.execute(
        """
        CREATE TEMP TABLE ppp_fanout AS
        SELECT ppp_legal_name_normalized, COUNT(*) AS ppp_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ucc_fanout AS
        SELECT ppp_legal_name_normalized,
               COUNT(DISTINCT ucc_debtor_name_normalized) AS ucc_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule + address agreement + composite tier (v2.0.0).
    #    silver was previously structurally unreachable post-dedup (ucc_fan_out ≡ 1);
    #    under v2.0.0 silver IS reachable when name=gold (1:N) + no address agreement.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            pf.ppp_fan_out,
            uf.ucc_fan_out,
            -- address agreement: PPP borrower address appears in UCC name's address set
            CASE
              WHEN b.ucc_debtor_address_set IS NULL OR LEN(b.ucc_debtor_address_set) = 0
                THEN FALSE
              WHEN list_contains(b.ucc_debtor_address_set, b.ppp_borrower_address_normalized)
                THEN TRUE
              ELSE FALSE
            END                                                  AS address_agrees,
            CASE
              WHEN b.ppp_borrower_address_normalized IS NOT NULL
                   AND list_contains(b.ucc_debtor_address_set, b.ppp_borrower_address_normalized)
                THEN 'borrower'
              ELSE NULL
            END                                                  AS address_match_path,
            CASE
              WHEN b.ppp_borrower_address_normalized IS NOT NULL
                   AND list_contains(b.ucc_debtor_address_set, b.ppp_borrower_address_normalized)
                THEN b.ppp_borrower_address_normalized
              ELSE NULL
            END                                                  AS address_match_value,
            CASE
                WHEN pf.ppp_fan_out > {COLLISION_THRESHOLD}
                  OR uf.ucc_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN pf.ppp_fan_out = 1 AND uf.ucc_fan_out = 1
                    THEN 'platinum'
                WHEN pf.ppp_fan_out = 1 OR  uf.ucc_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                                  AS name_confidence_tier
        FROM bridge_raw b
        JOIN ppp_fanout pf USING (ppp_legal_name_normalized)
        JOIN ucc_fanout uf USING (ppp_legal_name_normalized)
        """
    )

    # Composite tier — address agreement promotes; absence holds/demotes.
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

    # 4. Filter rejected rows before write. Drop the LIST column
    # (ucc_debtor_address_set) before Lance write — Lance 1.5.x def-buffer cap
    # risk on LIST<VARCHAR> with high fan-out (per L54). Per-name agreement is
    # captured in the boolean + path/value scalar columns.
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
            COUNT(*) FILTER (WHERE address_agrees)
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
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE (constraints #2 + #3).

    BTREE on ppp_legal_name_normalized (constraint #2) and ucc_debtor_name_normalized
    (constraint #3). Both must succeed or the run fails.
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

        # Dual BTREE per constraints #2 and #3.
        try:
            ds.create_scalar_index(
                "ppp_legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on ppp_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on ppp_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index(
                "ucc_debtor_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on ucc_debtor_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on ucc_debtor_name_normalized FAILED: %s", e)
            raise
        # v2.0.0 filter accelerators (non-fatal — not identity keys).
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


def main() -> int:
    """Build the PPP CA borrowers × CA UCC-1 debtors Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description=(
            "PPP CA borrowers × CA UCC-1 debtors Pattern B bridge generator. "
            "Resolves CA-state PPP borrowers against deduped CA UCC-1 debtor names "
            "via legal_name_state_exact_ca (L21 REUSE). "
            "The equipment-finance-lien signal: PPP recipients that pledged collateral."
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
        "bridge: %s  method=%s v%s  name_norm=v%s  addr_norm=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, ADDR_NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)
    logger.info("floor: %d rows_matched", MIN_ROWS_MATCHED)

    # v2.0.0 — NEW composite address-axis method (shared with the
    # CA-scoped UCC × SoS / UCC × SAM sibling bridges per PR #805; idempotent
    # UPSERT keeps the row consistent across writers).
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Composite legal-name + state exact match with address corroboration. "
            "Inner-joins LEFT and RIGHT on exact-equality normalized legal name "
            "(canonical _lib/entity_name_normalize, CA-state constrained); then "
            "checks whether LEFT's normalized physical address (base form, "
            "unit-stripped via _lib/address_normalize) equals ANY of the RIGHT "
            "side's address roles (e.g. SoS principal / principal_in_ca / "
            "mailing for sos_ca_owner; per-name aggregated UCC address set "
            "for sam / ppp bridges). Address agreement promotes the name-axis "
            "fan-out tier: platinum requires BOTH 1:1 name AND address corroboration."
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
            "address_agrees; without address corroboration the name-axis "
            "platinum demotes to gold and gold demotes to silver."
        ),
        rejection_rule_description=(
            "Reject when EITHER side's fan-out under the shared normalized name "
            "exceeds COLLISION_THRESHOLD (50)."
        ),
        input_columns_left=[
            "legal_name_normalized", "borrstate", "borrower_address_normalized",
        ],
        input_columns_right=[
            "debtor_name_normalized (from ORG_NAME)",
            "STATE",
            "address_base_normalized",
        ],
        output_value_description=(
            "(ppp_legal_name_normalized, ucc_debtor_name_normalized) name-grain pair "
            "with boolean address_agrees + address_match_path/value + "
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
            "PPP (SBA Paycheck Protection Program) CA-state borrowers × CA UCC-1 "
            "debtor filings (aggregated to debtor-name-grain with per-name address "
            "set) — v2.0.0 composite name+address axis. Resolves CA-state PPP "
            "recipients that also pledged collateral via a California UCC-1 filing, "
            "promoting the tier when the PPP borrower's normalized address appears "
            "in the UCC debtor name's per-filing address set. Method: "
            "legal_name_state_exact_ca_with_address_corroboration v1.0.0 (NEW; "
            "shared idempotently with PR #805 sibling bridges). PPP side: "
            "borrstate='CA' filter; 1.13M CA PPP borrowers at (name,state,zip) grain. "
            "UCC side: Organization debtors normalized via _lib.entity_name_normalize, "
            "aggregated to debtor-name-grain with address_base_normalized set. "
            "Silver was structurally unreachable in v1.0.0 (ucc_fan_out≡1 post-dedup); "
            "v2.0.0 makes silver reachable when name=gold + no address agreement."
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
        ppp_tbl, ucc_tbl, rows_left, rows_ucc_raw = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            ppp_tbl, ucc_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge composite tier distribution (v2.0.0 — address-axis):")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum:               %d  (name 1:1 + address agrees)", counts["rows_tier1"])
        logger.info("    gold:                   %d  (name 1:1 OR (name 1:N|N:1 + address agrees))", counts["rows_tier2"])
        logger.info("    silver:                 %d  (residual — v2.0.0: name 1:N|N:1 + no address agreement)", counts["rows_tier3"])
        logger.info("  address_agrees:           %d  (PPP borrower address ∈ UCC per-name address set)", counts["rows_address_agrees"])
        logger.info(
            "  rows_collision_rejected:  %d",
            counts["rows_collision_rejected"],
        )

        # HARD FAIL before Lance write if rows_matched < floor.
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,} — check normalizer (Footgun 1) and "
                f"UCC dedup (Footgun 2)"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

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
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        logger.info(
            "Polaris registration DEFERRED (constraint #10 SOFT — Polaris 502-down 2026-05-20):"
        )
        logger.info(
            "  init_polaris_lance_generic.py --namespace bridges "
            "--table ppp_ucc_ca_debtor_lance"
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
