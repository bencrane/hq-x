#!/usr/bin/env python3
"""s5 — Build the derived lenders_lance aggregation from UCC CO data.

Reads (all from R2 Lance datasets, written by s2, s3, s1):
  - ucc_co/secured_parties_lance  (s3 output)
  - ucc_co/filings_lance          (s1 output)
  - ucc_co/debtors_lance          (s2 output, for top_debtor_states/cities join)

Writes:
  s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/lenders_lance/

Schema (exact parity with ucc_ca/lenders_lance — 10 columns):
  lender_name_normalized      text (PK — uppercased + trimmed)
  total_filings               int64
  active_filings              int64  (LAPSE_DATE > NOW_ISO)
  first_filing_date           timestamp
  last_filing_date            timestamp
  top_debtor_states           text (JSON array of top-5 debtor states by count)
  top_debtor_cities           text (JSON array of top-10 debtor cities by count)
  bank_classification         text ∈ {bank, credit_union, non_bank, unknown}
  category_inferred_from_name text
  address_sample              text (most recent filing's ADDR1, CITY, STATE, POSTAL_CODE)

CO-specific adaptations vs CA build script:
  1. Reads Lance datasets (not Parquet) — uses lance.dataset(uri).to_table() + con.register()
  2. No CO amendments stream → terminated_set is empty (no UCC-3 termination data in CO source)
  3. Active filings computed solely from LAPSE_DATE > NOW_ISO (same formula as CA)
  4. Classifier applied VERBATIM — no CO-specific tuning (per directive ## Out of scope item 3)
     Expected unknown rate: 75-85% (CO skews agricultural; acceptable per directive)

BTREE index on lender_name_normalized after Lance write.
Commit-lock via Postgres advisory lock (lance_commit_lock constraint).

MIN_ROW_FLOOR = 35_000 — HARD FAIL gate fires BEFORE lance.write_dataset().
Expected output: ~43,800 rows (CA ratio 2.13% × CO 2.06M secured_parties rows).
Floor of 35K = ~80% of expected (raised from directive's 20K per validator p3 recommendation).

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/build_ucc_co_lenders_lance.py \\
            --apply
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.ucc_ca_classifier import (  # noqa: E402
    classify_category,
    classify_lender,
    _load_corpora,
    _Corpora,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

# Input Lance datasets (all written by s1–s3)
SP_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/secured_parties_lance/"
FILINGS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/filings_lance/"
DEBTORS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/debtors_lance/"

LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/lenders_lance/"
DATASET_SLUG = "ucc_co_lenders_lance"

NOW_ISO = "2026-05-08"  # snapshot evaluation date for "active" calculation (CO snapshot date)

# HARD FAIL gate — fires BEFORE lance.write_dataset()
# Raised from directive's 20K to 35K per validator p3 recommendation (~80% of expected ~43.8K)
MIN_ROW_FLOOR = 35_000


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _connect_duckdb():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _load_lance_table(uri: str, storage_options: dict, columns: list[str] | None = None):
    """Load a Lance dataset into an Arrow table for DuckDB registration.

    Pattern: lance.dataset(uri).to_table() + con.register() — no read_lance UDF needed.
    """
    import lance

    ds = lance.dataset(uri, storage_options=storage_options)
    tbl = ds.to_table(columns=columns)
    LOG.info("Loaded %d rows from %s", len(tbl), uri)
    return tbl


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build UCC CO lenders_lance (s5)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    import pyarrow as pa

    storage_options = _storage_options()
    con = _connect_duckdb()

    # ── Load Lance datasets and register with DuckDB ──────────────────────── #
    LOG.info("Loading CO Lance datasets ...")

    sp_tbl = _load_lance_table(SP_LANCE_URI, storage_options)
    con.register("sp", sp_tbl)

    filings_tbl = _load_lance_table(
        FILINGS_LANCE_URI, storage_options,
        columns=["UCC1_NUM", "FILING_DATE", "LAPSE_DATE", "ACTION_TYPE"],
    )
    con.register("filings", filings_tbl)

    debtors_tbl = _load_lance_table(
        DEBTORS_LANCE_URI, storage_options,
        columns=["UCC1_NUM", "DEBTOR_TYPE", "STATE", "CITY"],
    )
    con.register("debtors", debtors_tbl)

    # ── Step 1: No amendments stream for CO → terminated_set is empty ─────── #
    # CA's build script loads UCC-3 termination amendments here; CO has no amendments
    # stream (validator confirmed only 4 CO streams in raw R2). Active filings are
    # determined solely by LAPSE_DATE > NOW_ISO.
    terminated_set: set[str] = set()
    LOG.info("CO has no amendments stream — terminated_set is empty (active computed via LAPSE_DATE only)")

    # ── Step 2: Aggregate by normalized secured-party ORG_NAME ──────────── #
    LOG.info("Running lender aggregation query ...")

    agg_query = """
        WITH sp_org AS (
            SELECT
                sp.UCC1_NUM,
                sp.UCC3_NUM,
                UPPER(TRIM(COALESCE(sp.ORG_NAME, ''))) AS lender_name_raw,
                sp.ADDR1,
                sp.ADDR2,
                sp.CITY,
                sp.STATE AS sp_state,
                sp.POSTAL_CODE
            FROM sp
            WHERE sp.SECURED_PARTY_TYPE = 'Organization'
              AND sp.ORG_NAME IS NOT NULL
              AND TRIM(sp.ORG_NAME) <> ''
        ),
        filings_lien AS (
            SELECT
                UCC1_NUM,
                TRY_CAST(FILING_DATE AS TIMESTAMP) AS FILING_DATE,
                TRY_CAST(LAPSE_DATE AS TIMESTAMP) AS LAPSE_DATE,
                ACTION_TYPE
            FROM filings
            WHERE ACTION_TYPE = 'Lien Financing Stmt'
               OR UPPER(ACTION_TYPE) LIKE '%UCC-1%'
               OR UPPER(ACTION_TYPE) LIKE '%FINANCING%'
        )
        SELECT
            sp_org.lender_name_raw,
            COUNT(DISTINCT sp_org.UCC1_NUM) AS total_filings,
            MIN(f.FILING_DATE) AS first_filing_date,
            MAX(f.FILING_DATE) AS last_filing_date,
            LAST(sp_org.ADDR1 ORDER BY f.FILING_DATE) AS addr1_sample,
            LAST(sp_org.CITY ORDER BY f.FILING_DATE) AS city_sample,
            LAST(sp_org.sp_state ORDER BY f.FILING_DATE) AS state_sample,
            LAST(sp_org.POSTAL_CODE ORDER BY f.FILING_DATE) AS postal_sample
        FROM sp_org
        JOIN filings_lien f ON sp_org.UCC1_NUM = f.UCC1_NUM
        GROUP BY sp_org.lender_name_raw
        ORDER BY total_filings DESC
    """
    lender_rows = con.execute(agg_query).fetchall()
    LOG.info("Lender aggregation: %d distinct lenders", len(lender_rows))

    # ── Step 3: Active filings (LAPSE_DATE > NOW_ISO, no termination filter needed) ─ #
    LOG.info("Computing active_filings via LAPSE_DATE gate ...")

    active_query = f"""
        WITH sp_org AS (
            SELECT
                sp.UCC1_NUM,
                UPPER(TRIM(COALESCE(sp.ORG_NAME, ''))) AS lender_name_raw
            FROM sp
            WHERE sp.SECURED_PARTY_TYPE = 'Organization'
              AND sp.ORG_NAME IS NOT NULL
              AND TRIM(sp.ORG_NAME) <> ''
        ),
        active_filings AS (
            SELECT f.UCC1_NUM
            FROM filings f
            WHERE TRY_CAST(f.LAPSE_DATE AS TIMESTAMP) > TIMESTAMP '{NOW_ISO}'
        )
        SELECT
            sp_org.lender_name_raw,
            COUNT(DISTINCT sp_org.UCC1_NUM) AS active_count
        FROM sp_org
        JOIN active_filings af ON sp_org.UCC1_NUM = af.UCC1_NUM
        GROUP BY sp_org.lender_name_raw
    """
    active_map: dict[str, int] = {}
    for row in con.execute(active_query).fetchall():
        active_map[row[0]] = row[1]
    LOG.info("Active-filings map: %d entries", len(active_map))

    # ── Step 4: Top debtor states + cities per lender ────────────────────── #
    LOG.info("Computing top debtor states + cities ...")

    debtor_agg_query = """
        WITH sp_org AS (
            SELECT
                sp.UCC1_NUM,
                UPPER(TRIM(COALESCE(sp.ORG_NAME, ''))) AS lender_name_raw
            FROM sp
            WHERE sp.SECURED_PARTY_TYPE = 'Organization'
              AND sp.ORG_NAME IS NOT NULL
              AND TRIM(sp.ORG_NAME) <> ''
        ),
        deb AS (
            SELECT
                d.UCC1_NUM,
                UPPER(TRIM(COALESCE(d.STATE, ''))) AS debtor_state,
                UPPER(TRIM(COALESCE(d.CITY, ''))) AS debtor_city
            FROM debtors d
            WHERE d.DEBTOR_TYPE = 'Organization'
        )
        SELECT
            sp_org.lender_name_raw,
            deb.debtor_state,
            deb.debtor_city,
            COUNT(*) AS cnt
        FROM sp_org
        JOIN deb ON sp_org.UCC1_NUM = deb.UCC1_NUM
        WHERE deb.debtor_state IS NOT NULL AND deb.debtor_state <> ''
        GROUP BY sp_org.lender_name_raw, deb.debtor_state, deb.debtor_city
    """
    debtor_rows = con.execute(debtor_agg_query).fetchall()
    LOG.info("Debtor aggregation rows: %d", len(debtor_rows))

    from collections import defaultdict

    lender_state_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    lender_city_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for lender_name_raw, debtor_state, debtor_city, cnt in debtor_rows:
        if debtor_state:
            lender_state_counts[lender_name_raw][debtor_state] += cnt
        if debtor_city:
            lender_city_counts[lender_name_raw][debtor_city] += cnt

    def _top_k(counts: dict[str, int], k: int) -> str:
        return json.dumps(
            [s for s, _ in sorted(counts.items(), key=lambda x: -x[1])[:k]]
        )

    # ── Step 5: Load classifier corpora once ─────────────────────────────── #
    LOG.info("Loading classifier corpora (verbatim CA classifier — no CO-specific tuning) ...")
    try:
        corpora = _load_corpora()
    except Exception as e:
        LOG.warning("Corpus load failed (heuristics only): %s", e)
        corpora = _Corpora(fdic_names=[], ncua_names=[])

    # ── Step 6: Build final rows ──────────────────────────────────────────── #
    LOG.info("Building %d lender rows with classification ...", len(lender_rows))

    if args.dry_run:
        LOG.info("DRY RUN — exiting after aggregation summary (%d lenders)", len(lender_rows))
        return 0

    final_rows = []
    for lender_name_raw, total_filings, first_filing_date, last_filing_date, \
            addr1_sample, city_sample, state_sample, postal_sample in lender_rows:

        lender_name_normalized = lender_name_raw  # already uppercased + trimmed above

        active = active_map.get(lender_name_raw, 0)

        top_states = _top_k(lender_state_counts.get(lender_name_raw, {}), 5)
        top_cities = _top_k(lender_city_counts.get(lender_name_raw, {}), 10)

        # classify_lender verbatim from _lib/ucc_ca_classifier.py — no CO-specific tuning
        bank_class = classify_lender(lender_name_normalized, corpora=corpora)
        category = classify_category(lender_name_normalized)

        addr_parts = [
            p for p in [addr1_sample, city_sample, state_sample, postal_sample]
            if p and p.strip()
        ]
        address_sample = ", ".join(addr_parts) if addr_parts else None

        final_rows.append({
            "lender_name_normalized": lender_name_normalized,
            "total_filings": total_filings,
            "active_filings": active,
            "first_filing_date": first_filing_date,
            "last_filing_date": last_filing_date,
            "top_debtor_states": top_states,
            "top_debtor_cities": top_cities,
            "bank_classification": bank_class,
            "category_inferred_from_name": category,
            "address_sample": address_sample,
        })

    LOG.info("Final lenders: %d rows", len(final_rows))

    # ── HARD FAIL gate — fires BEFORE lance.write_dataset() ──────────────── #
    if len(final_rows) < MIN_ROW_FLOOR:
        LOG.error(
            "FAIL: aggregation produced %d rows, below MIN_ROW_FLOOR %d — aborting (no Lance write)",
            len(final_rows), MIN_ROW_FLOOR,
        )
        return 1

    # ── Step 7: Write to Lance ────────────────────────────────────────────── #
    import lance

    tbl = pa.Table.from_pylist(final_rows)
    storage_options = _storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        ds = lance.write_dataset(
            tbl,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        LOG.info("Wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        LOG.info("Building BTREE index on lender_name_normalized ...")
        try:
            os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
            ds.create_scalar_index("lender_name_normalized", index_type="BTREE", replace=True)
            LOG.info("BTREE index on lender_name_normalized: OK")
        except Exception as e:
            LOG.error(
                "BTREE index on lender_name_normalized FAILED: %s — HALT with blocked-btree-memory if retry fails",
                e,
            )
            raise

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions()
        except Exception as e:
            LOG.warning("Optimize failed (non-fatal): %s", e)

    # Log bank classification distribution for observability
    # NOTE: CO classifier unknown rate of 75-85% is EXPECTED and ACCEPTABLE per directive
    # ## Out of scope item 3 — CO skews agricultural; classifier tuning is a follow-up cycle.
    from collections import Counter
    bank_dist = Counter(r["bank_classification"] for r in final_rows)
    total_classified = len(final_rows)
    unknown_count = bank_dist.get("unknown", 0)
    unknown_pct = 100.0 * unknown_count / total_classified if total_classified else 0
    LOG.info(
        "Bank classification distribution: %s (unknown: %d/%d = %.1f%% — CO expected 75-85%%)",
        dict(bank_dist), unknown_count, total_classified, unknown_pct,
    )

    LOG.info("s5 complete: %d lender rows emitted to ucc_co/lenders_lance", lance_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
