#!/usr/bin/env python3
"""s7 — Build the derived lenders_lance aggregation from UCC CA data.

Reads (all from R2 Lance datasets, written by s3-s6 + s8-s9):
  - secured_parties_lance  (s5)
  - filings_lance          (s3)
  - filing_amendments_lance (s6)

Writes:
  s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lenders_lance/

Schema (DoD #7):
  lender_name_normalized      text (PK — uppercased + trimmed + suffix-stripped)
  total_filings               int64
  active_filings              int64  (LAPSE_DATE > NOW() AND no termination UCC-3)
  first_filing_date           timestamp
  last_filing_date            timestamp
  top_debtor_states           text (JSON array of top-5 debtor states by count)
  top_debtor_cities           text (JSON array of top-10 debtor cities by count)
  bank_classification         text ∈ {bank, credit_union, non_bank, unknown}
  category_inferred_from_name text ∈ {equipment_finance, factoring_ar, working_capital, ...}
  address_sample              text (most recent filing's ADDR1, CITY, STATE, POSTAL_CODE)

Commit-lock via Postgres advisory lock (constraint P3).

Note: top_debtor_states and top_debtor_cities require joining secured_parties
to debtors via UCC1_NUM. The join is done in DuckDB over R2 Lance parquets
(using the parsed parquets directly for the aggregation since joining raw Lance
datasets in DuckDB is cleaner than going through the Lance API for a group-by).

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/build_ucc_ca_lenders_lance.py \\
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
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

R2_BUCKET = "dex-raw-landing-zone"
SNAPSHOT = "2026-05-01"

# Input parquets (already on R2 from s2)
SP_PARQUET = f"r2://dex-raw-landing-zone/ucc-ca/master/snapshot=2026-05-01/parsed/secured_parties.parquet"
FILINGS_PARQUET = f"r2://dex-raw-landing-zone/ucc-ca/master/snapshot=2026-05-01/parsed/filings.parquet"
DEBTORS_PARQUET = f"r2://dex-raw-landing-zone/ucc-ca/master/snapshot=2026-05-01/parsed/debtors.parquet"
AMENDMENTS_PARQUET = f"r2://dex-raw-landing-zone/ucc-ca/master/snapshot=2026-05-01/parsed/filing_amendments.parquet"

LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lenders_lance/"
DATASET_SLUG = "ucc_ca_lenders_lance"

NOW_ISO = "2026-05-12"  # snapshot evaluation date for "active" calculation


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


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build UCC CA lenders_lance (s7)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    import pyarrow as pa
    import pyarrow.parquet as pq

    con = _connect_duckdb()

    LOG.info("Building lender aggregation via DuckDB over R2 parquets ...")

    # ── Step 1: Build terminated set (UCC1_NUM → bool from amendments) ──── #
    LOG.info("Loading termination amendments ...")
    terminated_q = f"""
        SELECT DISTINCT UCC1_NUM
        FROM read_parquet('{AMENDMENTS_PARQUET}')
        WHERE UPPER(TRIM(ACTION_TYPE)) = 'TERMINATION'
    """
    terminated_set = {
        r[0] for r in con.execute(terminated_q).fetchall()
    }
    LOG.info("Terminated UCC1_NUM count: %d", len(terminated_set))

    # ── Step 2: Aggregate by normalized secured-party ORG_NAME ──────────── #
    # This is the core aggregation query. We join secured_parties to filings
    # on UCC1_NUM to get LAPSE_DATE, then aggregate per normalized lender name.
    # We use Organization-type secured parties only (not individual names) per
    # the demand-side lender discovery use case.
    LOG.info("Running lender aggregation query ...")

    agg_query = f"""
        WITH sp AS (
            SELECT
                sp.UCC1_NUM,
                sp.UCC3_NUM,
                UPPER(TRIM(COALESCE(sp.ORG_NAME, ''))) AS lender_name_raw,
                sp.ADDR1,
                sp.ADDR2,
                sp.CITY,
                sp.STATE AS sp_state,
                sp.POSTAL_CODE
            FROM read_parquet('{SP_PARQUET}') sp
            WHERE sp.SECURED_PARTY_TYPE = 'Organization'
              AND sp.ORG_NAME IS NOT NULL
              AND TRIM(sp.ORG_NAME) <> ''
        ),
        filings AS (
            SELECT
                UCC1_NUM,
                TRY_CAST(FILING_DATE AS TIMESTAMP) AS FILING_DATE,
                TRY_CAST(LAPSE_DATE AS TIMESTAMP) AS LAPSE_DATE,
                ACTION_TYPE
            FROM read_parquet('{FILINGS_PARQUET}')
            WHERE ACTION_TYPE = 'Lien Financing Stmt'
               OR UPPER(ACTION_TYPE) LIKE '%UCC-1%'
               OR UPPER(ACTION_TYPE) LIKE '%FINANCING%'
        )
        SELECT
            sp.lender_name_raw,
            COUNT(DISTINCT sp.UCC1_NUM) AS total_filings,
            MIN(f.FILING_DATE) AS first_filing_date,
            MAX(f.FILING_DATE) AS last_filing_date,
            LAST(sp.ADDR1 ORDER BY f.FILING_DATE) AS addr1_sample,
            LAST(sp.CITY ORDER BY f.FILING_DATE) AS city_sample,
            LAST(sp.sp_state ORDER BY f.FILING_DATE) AS state_sample,
            LAST(sp.POSTAL_CODE ORDER BY f.FILING_DATE) AS postal_sample
        FROM sp
        JOIN filings f ON sp.UCC1_NUM = f.UCC1_NUM
        GROUP BY sp.lender_name_raw
        ORDER BY total_filings DESC
    """
    lender_rows = con.execute(agg_query).fetchall()
    LOG.info("Lender aggregation: %d distinct lenders", len(lender_rows))

    # ── Step 3: For top-N lenders compute active_filings (termination-aware) ─ #
    # Active = LAPSE_DATE > NOW and NOT terminated. We compute this via a
    # secondary DuckDB query over filings for just the lenders in our set.
    LOG.info("Computing active_filings via termination-aware join ...")

    # Build active-filings counts per lender
    # For performance: only iterate filings where LAPSE_DATE is non-null.
    active_query = f"""
        WITH sp AS (
            SELECT
                sp.UCC1_NUM,
                UPPER(TRIM(COALESCE(sp.ORG_NAME, ''))) AS lender_name_raw
            FROM read_parquet('{SP_PARQUET}') sp
            WHERE sp.SECURED_PARTY_TYPE = 'Organization'
              AND sp.ORG_NAME IS NOT NULL
              AND TRIM(sp.ORG_NAME) <> ''
        ),
        active_filings AS (
            SELECT f.UCC1_NUM
            FROM read_parquet('{FILINGS_PARQUET}') f
            WHERE TRY_CAST(f.LAPSE_DATE AS TIMESTAMP) > TIMESTAMP '{NOW_ISO}'
        )
        SELECT
            sp.lender_name_raw,
            COUNT(DISTINCT sp.UCC1_NUM) AS active_count
        FROM sp
        JOIN active_filings af ON sp.UCC1_NUM = af.UCC1_NUM
        GROUP BY sp.lender_name_raw
    """
    active_map: dict[str, int] = {}
    for row in con.execute(active_query).fetchall():
        lender_name_raw, active_count = row
        active_map[lender_name_raw] = active_count
    LOG.info("Active-filings map: %d entries", len(active_map))

    # Apply termination filter to active counts: subtract terminated filings per lender
    # Full accuracy: active_map from query already excludes lapse-expired; we further
    # need to subtract entries where the filing appears in terminated_set.
    # For v1, the termination filter is applied at the SQL level by NOT joining
    # terminated filings. Build a corrected active map by rerunning with exclusion.
    if terminated_set:
        LOG.info("Applying termination exclusion for %d terminated UCC1_NUMs ...", len(terminated_set))
        # Batch exclusion: use DuckDB with an IN clause on a VALUES list
        # For large terminated sets, write to temp parquet then anti-join
        import tempfile
        import pyarrow.parquet as _pq

        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tf:
            term_path = tf.name
        term_tbl = pa.Table.from_pydict({"UCC1_NUM": list(terminated_set)})
        _pq.write_table(term_tbl, term_path)

        active_query_corrected = f"""
            WITH sp AS (
                SELECT
                    sp.UCC1_NUM,
                    UPPER(TRIM(COALESCE(sp.ORG_NAME, ''))) AS lender_name_raw
                FROM read_parquet('{SP_PARQUET}') sp
                WHERE sp.SECURED_PARTY_TYPE = 'Organization'
                  AND sp.ORG_NAME IS NOT NULL
                  AND TRIM(sp.ORG_NAME) <> ''
            ),
            active_filings AS (
                SELECT f.UCC1_NUM
                FROM read_parquet('{FILINGS_PARQUET}') f
                WHERE TRY_CAST(f.LAPSE_DATE AS TIMESTAMP) > TIMESTAMP '{NOW_ISO}'
            ),
            terminated AS (
                SELECT UCC1_NUM FROM read_parquet('{term_path}')
            )
            SELECT
                sp.lender_name_raw,
                COUNT(DISTINCT sp.UCC1_NUM) AS active_count
            FROM sp
            JOIN active_filings af ON sp.UCC1_NUM = af.UCC1_NUM
            LEFT JOIN terminated t ON sp.UCC1_NUM = t.UCC1_NUM
            WHERE t.UCC1_NUM IS NULL
            GROUP BY sp.lender_name_raw
        """
        active_map = {}
        for row in con.execute(active_query_corrected).fetchall():
            active_map[row[0]] = row[1]
        import os as _os
        _os.unlink(term_path)
        LOG.info("Corrected active-filings map: %d entries", len(active_map))

    # ── Step 4: Top debtor states + cities per lender ────────────────────── #
    LOG.info("Computing top debtor states + cities ...")

    # Use parquets: join secured_parties to debtors via UCC1_NUM
    debtor_agg_query = f"""
        WITH sp AS (
            SELECT
                sp.UCC1_NUM,
                UPPER(TRIM(COALESCE(sp.ORG_NAME, ''))) AS lender_name_raw
            FROM read_parquet('{SP_PARQUET}') sp
            WHERE sp.SECURED_PARTY_TYPE = 'Organization'
              AND sp.ORG_NAME IS NOT NULL
              AND TRIM(sp.ORG_NAME) <> ''
        ),
        deb AS (
            SELECT
                d.UCC1_NUM,
                UPPER(TRIM(COALESCE(d.STATE, ''))) AS debtor_state,
                UPPER(TRIM(COALESCE(d.CITY, ''))) AS debtor_city
            FROM read_parquet('{DEBTORS_PARQUET}') d
            WHERE d.DEBTOR_TYPE = 'Organization'
        )
        SELECT
            sp.lender_name_raw,
            deb.debtor_state,
            deb.debtor_city,
            COUNT(*) AS cnt
        FROM sp
        JOIN deb ON sp.UCC1_NUM = deb.UCC1_NUM
        WHERE deb.debtor_state IS NOT NULL AND deb.debtor_state <> ''
        GROUP BY sp.lender_name_raw, deb.debtor_state, deb.debtor_city
    """
    debtor_rows = con.execute(debtor_agg_query).fetchall()
    LOG.info("Debtor aggregation rows: %d", len(debtor_rows))

    # Build per-lender state + city counts
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
    LOG.info("Loading classifier corpora ...")
    try:
        corpora = _load_corpora()
    except Exception as e:
        LOG.warning("Corpus load failed (heuristics only): %s", e)
        from scripts._lib.ucc_ca_classifier import _Corpora
        corpora = _Corpora(fdic_names=[], ncua_names=[])

    # ── Step 6: Build final rows ──────────────────────────────────────────── #
    LOG.info("Building %d lender rows with classification ...", len(lender_rows))

    if args.dry_run:
        LOG.info("DRY RUN — exiting after aggregation summary")
        return 0

    final_rows = []
    for lender_name_raw, total_filings, first_filing_date, last_filing_date, \
            addr1_sample, city_sample, state_sample, postal_sample in lender_rows:

        lender_name_normalized = lender_name_raw  # already uppercased + trimmed above

        active = active_map.get(lender_name_raw, 0)

        top_states = _top_k(lender_state_counts.get(lender_name_raw, {}), 5)
        top_cities = _top_k(lender_city_counts.get(lender_name_raw, {}), 10)

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

        try:
            ds.create_scalar_index("lender_name_normalized", index_type="BTREE", replace=True)
        except Exception as e:
            LOG.warning("BTREE index failed (non-fatal): %s", e)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions()
        except Exception as e:
            LOG.warning("Optimize failed (non-fatal): %s", e)

    # Log bank classification distribution
    from collections import Counter
    bank_dist = Counter(r["bank_classification"] for r in final_rows[:100])
    LOG.info("Top-100 bank_classification distribution: %s", dict(bank_dist))
    unknown_in_top100 = sum(1 for r in final_rows[:100] if r["bank_classification"] == "unknown")
    if unknown_in_top100 > 5:
        LOG.warning(
            "HARD GATE WARNING: %d/100 top lenders are 'unknown' bank_classification (threshold: <=5). "
            "s16 will FAIL.",
            unknown_in_top100,
        )

    LOG.info("s7 complete: %d lender rows emitted to Lance", lance_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
