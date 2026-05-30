#!/usr/bin/env python3
"""Derive per-borrower UCC-history profile Lance dataset.

Cycle: scorer-enrichment-borrower-ucc-history (s1).

Reads (Arrow-bridge pattern — NOT the lance-duckdb extension):
  - polaris-warehouse/bridges/ucc_sba_borrower_lance/   (1.7M rows)
  - polaris-warehouse/ucc_ca/filings_lance/             (7.75M rows)
  - polaris-warehouse/ucc_ca/filing_amendments_lance/   (3.3M rows)
  - polaris-warehouse/ucc_ca/secured_parties_lance/     (4.7M rows)
  - polaris-warehouse/ucc_ca/lenders_lance/             (101K rows canonical)

Output namespace: borrowers.ucc_profile_lance (NOT sba.* — borrower-entity
abstraction, future-extensible to non-SBA-sourced borrower pools).

Output schema:
  borrower_entity_ref            string   — sba.borrowers_lance|<name>|<state>|<zip>
  ucc_filing_count               int64    — distinct UCC1_NUMs joined to borrower
  ucc_active_lien_count          int64    — UCC1_NUMs NOT terminated by an amendment
  ucc_first_filing_date          date32   — earliest FILING_DATE across filings
  ucc_last_filing_date           date32   — latest FILING_DATE across filings
  recent_secured_parties         string   — JSON array of last-5 secured parties
                                            (name, filing_date, bank_classification)
  recent_secured_party_categories string  — JSON array of distinct bank_classifications

Termination logic:
  - A UCC1_NUM is "terminated" if filing_amendments_lance has a row with
    ACTION_TYPE = 'Termination' (exact). 'Erroneous Termination' is excluded
    (it's a clerical reversal, NOT a real termination).

Entity-ref composition:
  - Bridge already carries legal_name_normalized, borrstate, borrzip — no
    join-back to sba.borrowers_lance needed.
  - ENTITY_REF_COLUMNS[("sba","borrowers_lance")] = [legal_name_normalized, borrstate, borrzip]
  - Format: 'sba.borrowers_lance|<legal_name_normalized>|<borrstate>|<borrzip>'

Volume floor: ≥ 400,000 rows (per validator audit; directive floor of 1M was
recalibrated to 400K based on 499,620 distinct entity-ref tuples in bridge).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_borrowers_ucc_profile_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_borrowers_ucc_profile_lance.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_borrowers_ucc_profile_lance")

DATASET_SLUG = "borrowers_ucc_profile_lance"
BRIDGE_LANCE_URI  = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_sba_borrower_lance"
FILINGS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/filings_lance"
AMENDS_LANCE_URI  = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/filing_amendments_lance"
SPTS_LANCE_URI    = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance"
LENDERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lenders_lance"
OUT_LANCE_URI     = "s3://dex-raw-landing-zone/polaris-warehouse/borrowers/ucc_profile_lance"

MIN_ROWS = 400_000
TMP_DIR = "/tmp/lance"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance output")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    import duckdb
    import lance
    import pyarrow.compute as pc

    storage_options = _lance_storage_options()

    # ── Stage 0: load all datasets via Arrow-bridge ──────────────────────── #
    logger.info("opening bridges/ucc_sba_borrower_lance via Arrow-bridge ...")
    bridge_ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=storage_options)
    bridge_arrow = bridge_ds.scanner(
        columns=["legal_name_normalized", "borrstate", "borrzip",
                 "debtor_name_normalized", "state"],
        filter=(
            (pc.field("legal_name_normalized").is_valid())
            & (pc.field("borrstate").is_valid())
            & (pc.field("borrzip").is_valid())
        ),
    ).to_table()
    logger.info("  bridge rows (non-null entity-ref cols): %d", len(bridge_arrow))

    logger.info("opening ucc_ca/filings_lance via Arrow-bridge ...")
    filings_ds = lance.dataset(FILINGS_LANCE_URI, storage_options=storage_options)
    # Only initial lien financing statements; we use ACTION_TYPE filter
    filings_arrow = filings_ds.scanner(
        columns=["UCC1_NUM", "FILING_DATE", "ACTION_TYPE"],
        filter=(pc.field("ACTION_TYPE") == "Lien Financing Stmt"),
    ).to_table()
    logger.info("  filings_lance (Lien Financing Stmt): %d rows", len(filings_arrow))

    logger.info("opening ucc_ca/filing_amendments_lance via Arrow-bridge ...")
    amends_ds = lance.dataset(AMENDS_LANCE_URI, storage_options=storage_options)
    # Only actual terminations (not 'Erroneous Termination')
    amends_arrow = amends_ds.scanner(
        columns=["UCC1_NUM", "ACTION_TYPE"],
        filter=(pc.field("ACTION_TYPE") == "Termination"),
    ).to_table()
    logger.info("  filing_amendments_lance (Termination only): %d rows", len(amends_arrow))

    logger.info("opening ucc_ca/secured_parties_lance via Arrow-bridge ...")
    spts_ds = lance.dataset(SPTS_LANCE_URI, storage_options=storage_options)
    spts_arrow = spts_ds.scanner(
        columns=["UCC1_NUM", "ORG_NAME", "LAST_NAME", "FIRST_NAME"],
    ).to_table()
    logger.info("  secured_parties_lance: %d rows", len(spts_arrow))

    logger.info("opening ucc_ca/lenders_lance via Arrow-bridge ...")
    lenders_ds = lance.dataset(LENDERS_LANCE_URI, storage_options=storage_options)
    lenders_arrow = lenders_ds.scanner(
        columns=["lender_name_normalized", "bank_classification"],
    ).to_table()
    logger.info("  lenders_lance: %d rows", len(lenders_arrow))

    # ── Stage 1: aggregate in DuckDB ─────────────────────────────────────── #
    con = duckdb.connect()
    con.register("bridge", bridge_arrow)
    con.register("filings", filings_arrow)
    con.register("amends", amends_arrow)
    con.register("spts", spts_arrow)
    con.register("lenders", lenders_arrow)

    # Normalized secured-party name: use ORG_NAME when present (lowercase + strip);
    # fall back to LAST_NAME || ' ' || FIRST_NAME for individuals.
    con.execute("""
        CREATE TEMP TABLE spts_with_name AS
        SELECT
            s.UCC1_NUM,
            CASE
                WHEN s.ORG_NAME IS NOT NULL AND trim(s.ORG_NAME) <> ''
                    THEN lower(trim(s.ORG_NAME))
                WHEN s.LAST_NAME IS NOT NULL AND trim(s.LAST_NAME) <> ''
                    THEN lower(trim(coalesce(s.FIRST_NAME, '') || ' ' || s.LAST_NAME))
                ELSE NULL
            END AS secured_party_name_normalized
        FROM spts s
    """)

    # Join lenders for bank_classification
    con.execute("""
        CREATE TEMP TABLE spts_classified AS
        SELECT
            s.UCC1_NUM,
            s.secured_party_name_normalized,
            coalesce(l.bank_classification, 'unknown') AS bank_classification
        FROM spts_with_name s
        LEFT JOIN lenders l
          ON s.secured_party_name_normalized = l.lender_name_normalized
    """)

    # Bridge × filings: join on debtor_name_normalized = lowercase(debtor_name)
    # The bridge carries debtor_name_normalized (already lower+stripped per cycle-2).
    # filings_lance has no direct debtor join — we must go through the debtors
    # dimension. However, ucc_ca/filings_lance carries ACTION_TYPE and UCC1_NUM
    # but NOT a debtor-name column directly. The join is via the bridge's
    # debtor_name_normalized to the debtors_lance.ORG_NAME (lowercased).
    #
    # HOWEVER: we have a simpler path: the bridge already deduplicates which
    # (debtor_name_normalized, state) pairs correspond to which SBA borrower.
    # The filings_lance has UCC1_NUM + FILING_DATE but no debtor column.
    #
    # The correct join key path: bridge has debtor_name_normalized (= normalized
    # ORG_NAME from UCC debtors table). We need to join to ucc_ca/debtors_lance
    # to get UCC1_NUM. Since the audit confirmed debtors_lance schema:
    # (UCC1_NUM, UCC3_NUM, SECURED_PARTY_TYPE, ORG_NAME, LAST_NAME, ...), but
    # wait — that is secured_parties_lance, not debtors_lance.
    #
    # From the directive: debtors_lance ENTITY_REF_COLUMNS = ["UCC1_NUM", "ORG_NAME", "STATE"]
    # So debtors_lance has UCC1_NUM + ORG_NAME. The join path is:
    #   bridge.debtor_name_normalized == lower(debtors_lance.ORG_NAME)
    #   + bridge.state == debtors_lance.STATE (debtor-side state)
    #
    # Since we loaded filings_lance (initial Lien Financing Stmt UCC1_NUMs),
    # and debtors_lance was NOT loaded (not in scope for this Arrow-bridge read),
    # we need to load debtors_lance. Let's load it now inline.
    logger.info("opening ucc_ca/debtors_lance via Arrow-bridge ...")
    debtors_ds = lance.dataset(
        "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance",
        storage_options=storage_options,
    )
    # Filter to Organization type only — matches how build_bridge_ucc_sba_borrower_lance.py
    # constructs debtor_name_normalized (it also filters DEBTOR_TYPE=Organization).
    debtors_arrow = debtors_ds.scanner(
        columns=["UCC1_NUM", "ORG_NAME", "STATE"],
        filter=(pc.field("DEBTOR_TYPE") == "Organization"),
    ).to_table()
    logger.info("  debtors_lance (DEBTOR_TYPE=Organization): %d rows", len(debtors_arrow))
    con.register("debtors", debtors_arrow)

    # Normalized debtors — apply the same suffix-stripping normalization that
    # build_bridge_ucc_sba_borrower_lance.py uses (_normalize_entity_sql equivalent):
    # strip common legal suffixes (llc, inc, corp, etc.) + punctuation collapse + trim.
    # This ensures debtor_name_normalized here matches bridge.debtor_name_normalized.
    _LEGAL_SUFFIXES = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    con.execute(f"""
        CREATE TEMP TABLE debtors_norm AS
        SELECT
            UCC1_NUM,
            NULLIF(
              trim(
                regexp_replace(
                  regexp_replace(
                    regexp_replace(
                      lower(trim(ORG_NAME)),
                      '\\b({_LEGAL_SUFFIXES})\\b\\.?', ' ', 'g'
                    ),
                    '[^\\w\\s]+', ' ', 'g'
                  ),
                  '\\s+', ' ', 'g'
                )
              ), ''
            )                                        AS debtor_name_normalized,
            lower(trim(coalesce(STATE, '')))         AS debtor_state
        FROM debtors
        WHERE ORG_NAME IS NOT NULL AND trim(ORG_NAME) <> ''
    """)

    # Deduplicated bridge: one row per (legal_name_normalized, borrstate, borrzip)
    con.execute("""
        CREATE TEMP TABLE bridge_dedup AS
        SELECT DISTINCT
            legal_name_normalized,
            borrstate,
            borrzip,
            debtor_name_normalized,
            lower(trim(coalesce(state, ''))) AS debtor_state
        FROM bridge
        WHERE legal_name_normalized IS NOT NULL
          AND borrstate IS NOT NULL
          AND borrzip IS NOT NULL
    """)

    # bridge × debtors_norm → UCC1_NUMs per borrower-entity
    con.execute("""
        CREATE TEMP TABLE filings_joined AS
        SELECT
            b.legal_name_normalized,
            b.borrstate,
            b.borrzip,
            d.UCC1_NUM,
            f.FILING_DATE
        FROM bridge_dedup b
        JOIN debtors_norm d
          ON d.debtor_name_normalized = b.debtor_name_normalized
         AND d.debtor_state = b.debtor_state
        JOIN filings f
          ON f.UCC1_NUM = d.UCC1_NUM
    """)

    fj_count = con.execute("SELECT COUNT(*) FROM filings_joined").fetchone()[0]
    logger.info("filings_joined (bridge × debtors × filings): %d rows", fj_count)

    # Active liens: UCC1_NUMs NOT terminated
    con.execute("""
        CREATE TEMP TABLE terminated_uccs AS
        SELECT DISTINCT UCC1_NUM FROM amends
    """)

    con.execute("""
        CREATE TEMP TABLE active_filings AS
        SELECT fj.legal_name_normalized, fj.borrstate, fj.borrzip, fj.UCC1_NUM
        FROM filings_joined fj
        LEFT JOIN terminated_uccs t ON t.UCC1_NUM = fj.UCC1_NUM
        WHERE t.UCC1_NUM IS NULL
    """)

    # Secured party rollup: latest 5 per borrower-entity (by max FILING_DATE)
    con.execute("""
        CREATE TEMP TABLE secured_party_rollup AS
        SELECT
            fj.legal_name_normalized,
            fj.borrstate,
            fj.borrzip,
            sc.secured_party_name_normalized,
            fj.FILING_DATE,
            sc.bank_classification,
            ROW_NUMBER() OVER (
                PARTITION BY fj.legal_name_normalized, fj.borrstate, fj.borrzip
                ORDER BY fj.FILING_DATE DESC
            ) AS rn
        FROM filings_joined fj
        JOIN spts_classified sc ON sc.UCC1_NUM = fj.UCC1_NUM
        WHERE sc.secured_party_name_normalized IS NOT NULL
    """)

    # ── Stage 2: final aggregation ───────────────────────────────────────── #
    con.execute("""
        CREATE TEMP TABLE result AS
        SELECT
            'sba.borrowers_lance|' || fj.legal_name_normalized
                || '|' || fj.borrstate
                || '|' || fj.borrzip                           AS borrower_entity_ref,
            COUNT(DISTINCT fj.UCC1_NUM)                        AS ucc_filing_count,
            COUNT(DISTINCT af.UCC1_NUM)                        AS ucc_active_lien_count,
            MIN(fj.FILING_DATE)::DATE                          AS ucc_first_filing_date,
            MAX(fj.FILING_DATE)::DATE                          AS ucc_last_filing_date,
            to_json(
                list(
                    STRUCT_PACK(
                        name := coalesce(sp5.secured_party_name_normalized, ''),
                        filing_date := sp5.FILING_DATE::DATE::VARCHAR,
                        bank_classification := coalesce(sp5.bank_classification, 'unknown')
                    )
                    ORDER BY sp5.FILING_DATE DESC
                )
                FILTER (WHERE sp5.secured_party_name_normalized IS NOT NULL)
            )::VARCHAR                                          AS recent_secured_parties,
            to_json(
                list(DISTINCT coalesce(sp5.bank_classification, 'unknown'))
                FILTER (WHERE sp5.secured_party_name_normalized IS NOT NULL AND sp5.rn <= 5)
            )::VARCHAR                                          AS recent_secured_party_categories
        FROM filings_joined fj
        LEFT JOIN active_filings af
          ON af.legal_name_normalized = fj.legal_name_normalized
         AND af.borrstate = fj.borrstate
         AND af.borrzip = fj.borrzip
         AND af.UCC1_NUM = fj.UCC1_NUM
        LEFT JOIN secured_party_rollup sp5
          ON sp5.legal_name_normalized = fj.legal_name_normalized
         AND sp5.borrstate = fj.borrstate
         AND sp5.borrzip = fj.borrzip
         AND sp5.rn <= 5
        GROUP BY
            fj.legal_name_normalized,
            fj.borrstate,
            fj.borrzip
    """)

    row_count = con.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    logger.info("result row count: %d", row_count)

    # Sample distribution
    sample = con.execute(
        "SELECT ucc_filing_count, ucc_active_lien_count FROM result ORDER BY ucc_filing_count DESC LIMIT 5"
    ).fetchall()
    logger.info("top-5 by ucc_filing_count: %s", sample)

    if row_count < MIN_ROWS:
        msg = f"HARD FAIL: row_count={row_count:,} < floor={MIN_ROWS:,}"
        logger.error(msg)
        return 1

    if args.dry_run:
        logger.info("DRY RUN — no Lance writes. row_count=%d >= floor=%d", row_count, MIN_ROWS)
        return 0

    # ── Stage 3: write Lance ─────────────────────────────────────────────── #
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing to Lance at %s ...", OUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM result").to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            OUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        try:
            ds.create_scalar_index("borrower_entity_ref", index_type="BTREE", replace=True)
        except Exception as e:
            logger.warning("BTREE index (borrower_entity_ref) failed (non-fatal): %s", e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("OK — %d rows written to %s", lance_count, OUT_LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
