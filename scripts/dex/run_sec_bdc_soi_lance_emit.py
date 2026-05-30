#!/usr/bin/env python3
"""SEC BDC Schedule of Investments → Lance emit (Pattern A, custom-DuckDB).

Reads the structured soi.tsv ZSTD Parquet (s3 output) across every release
partition, LEFT JOINs the s4 HTML-parsed maturity/name attributes, builds
typed siblings, and emits a single Lance dataset:

  s3://dex-raw-landing-zone/polaris-warehouse/sec_bdc/soi_lance

WHY A CUSTOM DUCKDB QUERY (NOT a thin LanceEmitConfig wrapper)
--------------------------------------------------------------
`scripts/_lib/lance_emit.py`'s `multi_release` mode does a single-table read
with no JOIN. This emit needs a LEFT JOIN of the structured soi Parquet to
the s4-parsed Parquet, so it uses the runbook §6 inline-DuckDB shape instead.

INPUTS
------
- Structured:  r2://dex-raw-landing-zone/sec-bdc/soi/release=*/data.parquet
               soi.tsv's column count drifts 51 (monthly delta) ↔ 226
               (quarterly snapshot); read with union_by_name=TRUE so the
               drift is absorbed.
- Parsed:      r2://dex-raw-landing-zone/sec-bdc/soi-parsed/release=*/data.parquet
               s4's per-investment maturity_date + portfolio_company_name +
               instrument_type. May be ABSENT for periods s4 has not parsed
               (the LEFT JOIN tolerates that — the typed maturity_date then
               falls back to the structured "Investment Maturity Date").

JOIN KEY
--------
s4's parsed Parquet is keyed (adsh + identifier string + fair value) per the
directive s4 row. The structured soi row's "Investment, Identifier Axis" is
the fused free-text identifier and "Investment Owned, Fair Value" the numeric
disambiguator. Identifier text is fuzzy (company names contain commas), so the
practical join is (adsh, ROUND(fair_value)) — fair value is unique enough
within a filing to disambiguate investment lines, and rounding absorbs the
scale/format differences between the structured TSV cell and the parsed HTML
cell. A structured row with no parsed match keeps NULL parsed columns.

TYPED SIBLINGS (directive s5 row)
---------------------------------
  maturity_date   DATE   — COALESCE(TRY_CAST(parsed s4 date), TRY_CAST(structured))
  fair_value      DOUBLE — TRY_CAST("Investment Owned, Fair Value")
  amortized_cost  DOUBLE — TRY_CAST("Investment Owned, Cost")
  principal       DOUBLE — TRY_CAST("Investment Owned, Balance, Principal Amount")
  portfolio_company_name              — s4-parsed, raw holdings-table cell text
  portfolio_company_name_normalized   — lower/trimmed/punct-stripped (LEGACY:
                                        footnote-contaminated — use _clean below)
  instrument_type                     — s4-parsed
  portfolio_company_name_clean        — _lib/sec_bdc_name_normalize: footnotes,
                                        tranche text + junk stripped; pipe-
                                        delimited for multi-borrower cells
  portfolio_company_dba               — extracted dba / f-k-a brand (nullable)
  portfolio_company_entity_type       — company / clo / cash_or_treasury / fund
                                        / non_entity

DuckDB type names are written as SQL string literals inside TRY_CAST — this
script never imports the DuckDB typing module (runbook §Gotchas #2; the verify
harness negation-greps for that import path).

BTREE scalar indexes: adsh, cik, maturity_date, portfolio_company_name_normalized.

Per CLAUDE.md §"Source ingest invariant" sub-case "bulk-historical Volume-King":
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on the canonical keys
  - No LIST<VARCHAR> — every column is scalar VARCHAR / DOUBLE / DATE
  - mode="overwrite" — full re-emit each cycle (union of all releases)

Polaris registration is NOT done here — it is the discrete s7 step
(`init_polaris_lance_generic.py`), run outside Modal because registration
fails silently from inside Modal (runbook §Gotchas #3).

Usage:
  cd ~/hq-all/apps/data-engine-x && \\
    doppler run --project hq-all --config prd -- \\
    uv run python scripts/run_sec_bdc_soi_lance_emit.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock
from scripts._lib.sec_bdc_name_normalize import normalize as normalize_bdc_name

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants (verify harness greps for these) ─────────────────
R2_BUCKET = "dex-raw-landing-zone"

SOI_PARQUET_GLOB = f"s3://{R2_BUCKET}/sec-bdc/soi/release=*/data.parquet"
PARSED_PARQUET_GLOB = f"s3://{R2_BUCKET}/sec-bdc/soi-parsed/release=*/data.parquet"

LANCE_SOI_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sec_bdc/soi_lance"

TMP_DIR = "/tmp/lance"


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2_secret (
            TYPE s3,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{os.environ["R2_ENDPOINT"].replace("https://", "")}',
            REGION 'us-east-1',
            URL_STYLE 'path'
        )
        """
    )
    return con


def _parsed_glob_has_data(con) -> bool:
    """True iff the s4 soi-parsed glob resolves to >=1 Parquet file. The s4
    parse may not have run for any period yet (s5 can still emit the
    structured-only dataset — maturity_date then comes from the structured
    "Investment Maturity Date" column alone)."""
    try:
        n = con.execute(
            f"SELECT count(*) FROM glob('{PARSED_PARQUET_GLOB}')"
        ).fetchone()[0]
        return bool(n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("soi-parsed glob probe failed (%s) — treating as empty", exc)
        return False


def _build_select_sql(*, with_parsed: bool) -> str:
    """Assemble the emit SELECT.

    The structured soi Parquet is read with union_by_name=TRUE to absorb the
    51↔226 column drift between monthly deltas and quarterly snapshots. Only
    the stable-core columns (present in every period) are projected, plus the
    typed siblings. When the s4 soi-parsed glob has data it is LEFT JOINed on
    (adsh, ROUND(fair_value)); otherwise the parsed columns are emitted as
    typed NULLs so the Lance schema is identical either way.
    """
    # The structured fair value, used both as a typed sibling and as the join
    # disambiguator. ROUND to 0 dp so the structured TSV cell and the parsed
    # HTML cell line up despite formatting differences.
    if with_parsed:
        parsed_cte = f"""
        parsed AS (
            SELECT
                adsh                              AS p_adsh,
                ROUND(TRY_CAST(fair_value AS DOUBLE)) AS p_fv_key,
                ANY_VALUE(portfolio_company_name) AS p_company,
                ANY_VALUE(instrument_type)        AS p_instrument,
                ANY_VALUE(maturity_date)          AS p_maturity
            FROM read_parquet('{PARSED_PARQUET_GLOB}', union_by_name=TRUE)
            WHERE adsh IS NOT NULL
            GROUP BY adsh, ROUND(TRY_CAST(fair_value AS DOUBLE))
        ),"""
        parsed_join = """
        LEFT JOIN parsed
          ON parsed.p_adsh = soi.adsh
         AND parsed.p_fv_key = ROUND(TRY_CAST(soi."Investment Owned, Fair Value" AS DOUBLE))"""
        p_company = "parsed.p_company"
        p_instrument = "parsed.p_instrument"
        p_maturity = "parsed.p_maturity"
    else:
        parsed_cte = ""
        parsed_join = ""
        p_company = "CAST(NULL AS VARCHAR)"
        p_instrument = "CAST(NULL AS VARCHAR)"
        p_maturity = "CAST(NULL AS VARCHAR)"

    return f"""
        WITH {parsed_cte}
        soi AS (
            SELECT * FROM read_parquet('{SOI_PARQUET_GLOB}', union_by_name=TRUE)
        )
        SELECT
            soi.adsh,
            soi.cik,
            soi.name,
            soi.release,
            soi.form,
            soi.filed,
            soi.period,
            soi.inlineurl,
            soi."Investment, Identifier Axis"          AS investment_identifier,
            soi."Investment Type Axis"                 AS investment_type_axis,
            soi."Investment Interest Rate"             AS investment_interest_rate,
            soi."Investment, Basis Spread, Variable Rate" AS basis_spread_variable_rate,
            -- structured maturity (raw, ~18% populated) kept for provenance
            soi."Investment Maturity Date"             AS maturity_date_structured,
            -- s4-parsed columns (NULL when no parsed match / no parse run)
            {p_company}                                AS portfolio_company_name,
            lower(regexp_replace(trim(COALESCE({p_company}, '')),
                  '[^a-z0-9 ]', '', 'gi'))             AS portfolio_company_name_normalized,
            {p_instrument}                             AS instrument_type,
            {p_maturity}                               AS maturity_date_parsed,
            -- typed sibling: prefer the s4-parsed date, else the structured one
            COALESCE(
                TRY_CAST({p_maturity} AS DATE),
                TRY_CAST(soi."Investment Maturity Date" AS DATE)
            )                                          AS maturity_date,
            TRY_CAST(soi."Investment Owned, Fair Value" AS DOUBLE)   AS fair_value,
            TRY_CAST(soi."Investment Owned, Cost" AS DOUBLE)         AS amortized_cost,
            TRY_CAST(soi."Investment Owned, Balance, Principal Amount" AS DOUBLE)
                                                                     AS principal,
            TRY_CAST(soi."Investment Owned, Net Assets, Percentage" AS DOUBLE)
                                                                     AS net_assets_pct
        FROM soi{parsed_join}
    """


def emit() -> int:
    """Emit the BDC SOI Lance dataset. Returns the row count written."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    soi_rows = con.execute(
        f"SELECT count(*) FROM read_parquet('{SOI_PARQUET_GLOB}', union_by_name=TRUE)"
    ).fetchone()[0]
    logger.info("soi: %d structured source rows at %s", soi_rows, SOI_PARQUET_GLOB)
    if soi_rows == 0:
        raise RuntimeError(
            "soi: no rows under sec-bdc/soi/release=*/ — run "
            "run_sec_bdc_soi_r2_ingest.py first"
        )

    with_parsed = _parsed_glob_has_data(con)
    logger.info(
        "soi-parsed glob: %s",
        "present — LEFT JOIN enabled" if with_parsed else "absent — structured-only emit",
    )

    select_sql = _build_select_sql(with_parsed=with_parsed)
    storage_options = _storage_options()

    # Materialise, then apply the deterministic BDC name cleaner — adds
    # portfolio_company_name_clean (footnotes / tranche / junk stripped;
    # pipe-delimited for multi-borrower cells), portfolio_company_dba (extracted
    # dba / f-k-a brand), and portfolio_company_entity_type.
    import pyarrow as pa

    arrow_tbl = con.execute(select_sql).arrow().read_all()
    logger.info("applying BDC name cleaner to %d rows ...", arrow_tbl.num_rows)
    clean, dba, etype = [], [], []
    for raw in arrow_tbl.column("portfolio_company_name").to_pylist():
        ents = normalize_bdc_name(raw)["entities"]
        if ents:
            clean.append("|".join(e["cleaned_name"] for e in ents))
            dba.append(ents[0]["dba"])
            etype.append(ents[0]["entity_type"])
        else:
            clean.append(None)
            dba.append(None)
            etype.append(None)
    arrow_tbl = (
        arrow_tbl
        .append_column("portfolio_company_name_clean", pa.array(clean, pa.string()))
        .append_column("portfolio_company_dba", pa.array(dba, pa.string()))
        .append_column("portfolio_company_entity_type", pa.array(etype, pa.string()))
    )

    with lance_commit_lock("sec_bdc_soi_lance"):
        logger.info("writing BDC SOI Lance dataset to %s ...", LANCE_SOI_URI)
        ds = lance.write_dataset(
            arrow_tbl,
            LANCE_SOI_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=50_000,
        )
        lance_rows = ds.count_rows()
        logger.info("BDC SOI Lance written: %d rows (version %s)", lance_rows, ds.version)

        for col in ("adsh", "cik", "maturity_date", "portfolio_company_name_normalized"):
            logger.info("creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:  # noqa: BLE001
            logger.warning("optimize failed (non-fatal): %s", exc)

    logger.info("BDC SOI emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_SOI_URI)
    return lance_rows


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description="SEC BDC Schedule of Investments → Lance emit"
    ).parse_args(argv)

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set in environment", var)
            return 64

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
