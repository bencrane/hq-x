#!/usr/bin/env python3
"""USAspending recipient-features Lance emit v2 (Pattern A derived, FULL OUTER JOIN).

v2 extends v1 (PR #558/#559) to include subaward winners via FULL OUTER JOIN of:
  - prime-side aggregates (GROUP BY recipient_uei from contracts_lance)
  - subaward-side aggregates (GROUP BY subawardee_uei from contract_subawards_lance)

New columns added (subaward-side + derived combined columns):
    subaward_count                     -- COUNT(*) of subaward rows per UEI
    subaward_total_dollars             -- SUM(subaward_amount)
    subaward_min_dollars               -- MIN(subaward_amount)
    subaward_median_dollars            -- MEDIAN(subaward_amount)
    subaward_max_dollars               -- MAX(subaward_amount)
    subaward_p95_dollars               -- P95(subaward_amount)
    subaward_pop_states                -- pipe-delimited state codes (L54)
    subaward_pop_primary_state         -- MODE of subaward PoP state
    subaward_pop_state_count           -- COUNT(DISTINCT subaward PoP state)
    subaward_top_primes                -- pipe top-3 prime contractors by SUM(amount)
    subaward_top_naics                 -- pipe top-5 NAICS by SUM(amount)
    subaward_top_naics_primary         -- top-1 NAICS by SUM(amount)
    subaward_earliest_date             -- MIN(subaward_action_date)
    subaward_latest_date               -- MAX(subaward_action_date)
    subaward_count_last_12mo           -- COUNT(*) WHERE action_date >= NOW()-365d
    subaward_dollars_last_12mo         -- SUM(amount) WHERE action_date >= NOW()-365d
    subaward_count_last_5y             -- COUNT(*) WHERE action_date >= NOW()-5y
    is_subawardee_only                 -- TRUE if UEI appears only as subawardee
    combined_total_dollars             -- COALESCE(lifetime_total_obligated,0) + COALESCE(subaward_total_dollars,0)
    roles                              -- 'prime|subaward' / 'prime' / 'subaward'

Subaward NAICS source: prime_award_naics_code (USAspending convention — subawards
inherit NAICS from the parent prime contract; no subaward-specific NAICS field exists).

v1 prime-side columns survive verbatim (narrow-subaward-additions per audit P1 resolution):
    recipient_uei, pop_primary_state, distinct_pop_states, distinct_naics_codes,
    distinct_psc_codes, distinct_agencies, lifetime_contract_count,
    lifetime_total_obligated, max_single_award_obligation, latest_action_date,
    max_pop_end_date, contract_count_365d, total_obligated_365d,
    emit_run_id, generated_at, feature_version

Volume floor: 130,854 (= floor(0.95 * 137,742 UNION distinct UEIs;
validator measured prime=134,841 + subawardee-non-null=7,197 → UNION=137,742
at 2026-05-19T20:40:07Z).

Closest precedent: scripts/build_bridge_sam_pdl_usaspending_lance.py (PR #469)
— same Arrow-bridge + DuckDB 4-way join + lance write under commit_lock.

Source: directive 2026-05-19-usaspending-recipient-features-include-subawards.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from scripts/ dir or from project root.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

R2_BUCKET = "dex-raw-landing-zone"
CONTRACTS_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/contracts_lance"
SUBAWARDS_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/contract_subawards_lance"
OUTPUT_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/recipient_features_lance"
DATASET_SLUG = "usaspending_recipient_features_lance"
MIN_ROW_FLOOR = 130_854  # validator 2026-05-19T20:40:07Z (floor(0.95 * 137,742 UNION distinct UEIs))
EMIT_VERSION = "2.0.0"
TMP_DIR = "/tmp/lance"

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _connect_pg():
    import psycopg
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise EnvironmentError("DEX_DB_URL_DIRECT is required")
    return psycopg.connect(url, autocommit=True)


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------


def _record_run_start() -> tuple[str, str | None]:
    """Insert a running row; return (ingest_run_id, None).

    The second tuple element is a legacy slot kept for caller-signature stability
    (the catalog row has no surrogate id — its PK is source_slug).
    """
    run_id = str(uuid.uuid4())
    try:
        with _connect_pg() as conn:
            conn.execute(
                """
                INSERT INTO ops.usaspending_recipient_features_emit_runs
                    (ingest_run_id, started_at, status, feature_version)
                VALUES (%s, NOW(), 'running', %s)
                """,
                (run_id, EMIT_VERSION),
            )
    except Exception as exc:
        LOG.warning("ledger start failed (non-fatal): %s", exc)
    return run_id, None


def _record_run_complete(
    run_id: str,
    *,
    status: str,
    rows_emitted: int = 0,
    error_message: str | None = None,
    upstream_version: str | None = None,
) -> None:
    try:
        with _connect_pg() as conn:
            conn.execute(
                """
                UPDATE ops.usaspending_recipient_features_emit_runs
                SET status        = %s,
                    completed_at  = NOW(),
                    rows_emitted  = %s,
                    rows_ingested = %s,
                    error_message = %s,
                    upstream_contracts_lance_version = %s
                WHERE ingest_run_id = %s
                """,
                (status, rows_emitted, rows_emitted, error_message, upstream_version, run_id),
            )
    except Exception as exc:
        LOG.warning("ledger complete failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Core emit
# ---------------------------------------------------------------------------


def emit(apply: bool = False) -> dict:
    """Run the recipient-features emit. Returns a metrics dict.

    Args:
        apply: if False (dry-run), compute row count but do NOT write Lance
               or touch the ledger.
    """
    # LANCE_BYPASS_SPILLING and TMPDIR must be set before any lance import.
    os.environ["TMPDIR"] = TMP_DIR
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    Path(f"{TMP_DIR}/duckdb").mkdir(parents=True, exist_ok=True)

    import duckdb
    import lance

    storage = _storage_options()
    t0 = time.time()
    emit_run_id = str(uuid.uuid4())
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    LOG.info("scanning contracts_lance at %s ...", CONTRACTS_URI)

    # 1a. PyLance scanner — prime contracts: project only the columns needed for features.
    #     TRY_CAST every USAspending VARCHAR date/numeric column per
    #     CLAUDE.md §"USAspending pipeline" — action_date and
    #     period_of_performance_current_end_date arrive as VARCHAR in
    #     contracts_lance (the bulk Parquet ingest preserves source types).
    #     federal_action_obligation and total_dollars_obligated are also VARCHAR.
    #     DO NOT use duckdb.typing module — that path is documented as broken on
    #     the current duckdb pin (run_govcontract_match_subset.py precedent).
    contracts_table = lance.dataset(
        CONTRACTS_URI, storage_options=storage
    ).scanner(
        columns=[
            "recipient_uei",
            "primary_place_of_performance_state_code",
            "primary_place_of_performance_zip_4",
            "naics_code",
            "product_or_service_code",
            "awarding_agency_name",
            "action_date",
            "period_of_performance_current_end_date",
            "federal_action_obligation",
            "total_dollars_obligated",
        ],
        filter="recipient_uei IS NOT NULL AND recipient_uei != ''",
    ).to_table()

    LOG.info("contracts_lance scan complete: %d rows", contracts_table.num_rows)

    # 1b. PyLance scanner — subawards: project identity + value + geography + NAICS + prime name.
    #     subaward_amount is already DOUBLE in source; TRY_CAST is defensive per L29 convention.
    #     subaward_action_date is already date32[day] in source; TRY_CAST is defensive.
    #     subaward NAICS comes from prime_award_naics_code (P3 — no subaward-specific NAICS exists;
    #     subawards inherit NAICS from the parent prime contract per USAspending convention).
    LOG.info("scanning contract_subawards_lance at %s ...", SUBAWARDS_URI)
    subawards_table = lance.dataset(
        SUBAWARDS_URI, storage_options=storage
    ).scanner(
        columns=[
            "subawardee_uei",
            "subaward_amount",
            "subaward_action_date",
            "subaward_primary_place_of_performance_state_code",
            "prime_award_naics_code",
            "prime_awardee_name",
            "prime_awardee_uei",
        ],
        filter="subawardee_uei IS NOT NULL AND subawardee_uei != ''",
    ).to_table()

    LOG.info("contract_subawards_lance scan complete: %d rows", subawards_table.num_rows)

    # 2. DuckDB: register Arrow tables, then build prime CTE → subaward CTE → FULL OUTER JOIN.
    con = duckdb.connect()
    con.register("contracts", contracts_table)
    con.register("subawards", subawards_table)
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{TMP_DIR}/duckdb'")

    # 2a. Prime-side typed temp table — TRY_CAST per CLAUDE.md §"USAspending pipeline".
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE typed AS
        SELECT
            recipient_uei,
            primary_place_of_performance_state_code AS pop_primary_state,
            primary_place_of_performance_zip_4       AS pop_primary_zip_4,
            naics_code,
            product_or_service_code                 AS psc_code,
            awarding_agency_name,
            TRY_CAST(action_date AS DATE)                             AS action_date_typed,
            TRY_CAST(period_of_performance_current_end_date AS DATE)  AS popoe_date,
            TRY_CAST(federal_action_obligation AS DOUBLE)             AS obligation_dbl,
            TRY_CAST(total_dollars_obligated   AS DOUBLE)             AS total_obligated_dbl
        FROM contracts
        WHERE recipient_uei IS NOT NULL AND recipient_uei != ''
    """)

    # 2b. Prime-side aggregate CTE — v1 shape (all 13 non-provenance prime columns).
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE features AS
        SELECT
            recipient_uei,
            MODE() WITHIN GROUP (ORDER BY pop_primary_state)        AS pop_primary_state,
            COUNT(DISTINCT pop_primary_state)                        AS distinct_pop_states,
            COUNT(DISTINCT naics_code)                               AS distinct_naics_codes,
            COUNT(DISTINCT psc_code)                                 AS distinct_psc_codes,
            COUNT(DISTINCT awarding_agency_name)                     AS distinct_agencies,
            COUNT(*)                                                 AS lifetime_contract_count,
            SUM(COALESCE(total_obligated_dbl, 0))                   AS lifetime_total_obligated,
            MAX(obligation_dbl)                                      AS max_single_award_obligation,
            MAX(action_date_typed)                                   AS latest_action_date,
            MAX(popoe_date)                                          AS max_pop_end_date,
            SUM(CASE WHEN action_date_typed >= CURRENT_DATE - INTERVAL '365 days'
                     THEN 1 ELSE 0 END)                              AS contract_count_365d,
            SUM(CASE WHEN action_date_typed >= CURRENT_DATE - INTERVAL '365 days'
                     THEN COALESCE(total_obligated_dbl, 0) ELSE 0 END) AS total_obligated_365d
        FROM typed
        GROUP BY recipient_uei
    """)

    # 2c. Subaward-side typed temp table — TRY_CAST (defensive; source types already match).
    #     Rename subawardee_uei → recipient_uei here so the FULL OUTER JOIN key is uniform
    #     and COALESCE(p.recipient_uei, s.recipient_uei) is clean (P2 FULL OUTER JOIN fix).
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE subaward_typed AS
        SELECT
            subawardee_uei                                            AS recipient_uei,
            TRY_CAST(subaward_amount AS DOUBLE)                       AS sub_amount_dbl,
            TRY_CAST(subaward_action_date AS DATE)                    AS sub_action_date_typed,
            subaward_primary_place_of_performance_state_code          AS sub_pop_state,
            prime_award_naics_code                                    AS sub_naics_code,
            prime_awardee_name                                        AS sub_prime_name
        FROM subawards
        WHERE subawardee_uei IS NOT NULL AND subawardee_uei != ''
    """)

    # 2d. Subaward-side aggregates — one row per subawardee_uei.
    #     Top-N via ranked subquery: top-3 prime contractors (by SUM(amount)), top-5 NAICS.
    #     Pipe-delimited per L54: array_to_string(list_distinct(list(col)) FILTER (...), '|').
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE subaward_features AS
        WITH
        sub_top_primes AS (
            SELECT recipient_uei,
                   array_to_string(
                     list(sub_prime_name ORDER BY total_amt DESC) FILTER (WHERE sub_prime_name IS NOT NULL),
                     '|'
                   ) AS subaward_top_primes
            FROM (
                SELECT recipient_uei, sub_prime_name, SUM(sub_amount_dbl) AS total_amt,
                       ROW_NUMBER() OVER (PARTITION BY recipient_uei ORDER BY SUM(sub_amount_dbl) DESC NULLS LAST) AS rn
                FROM subaward_typed
                GROUP BY recipient_uei, sub_prime_name
            )
            WHERE rn <= 3
            GROUP BY recipient_uei
        ),
        sub_top_naics AS (
            SELECT recipient_uei,
                   array_to_string(
                     list(sub_naics_code ORDER BY total_amt DESC) FILTER (WHERE sub_naics_code IS NOT NULL),
                     '|'
                   ) AS subaward_top_naics,
                   (array_agg(sub_naics_code ORDER BY total_amt DESC))[1] AS subaward_top_naics_primary
            FROM (
                SELECT recipient_uei, sub_naics_code, SUM(sub_amount_dbl) AS total_amt,
                       ROW_NUMBER() OVER (PARTITION BY recipient_uei ORDER BY SUM(sub_amount_dbl) DESC NULLS LAST) AS rn
                FROM subaward_typed
                GROUP BY recipient_uei, sub_naics_code
            )
            WHERE rn <= 5
            GROUP BY recipient_uei
        )
        SELECT
            s.recipient_uei,
            COUNT(*)                                                       AS subaward_count,
            SUM(COALESCE(s.sub_amount_dbl, 0))                             AS subaward_total_dollars,
            MIN(s.sub_amount_dbl)                                          AS subaward_min_dollars,
            MEDIAN(s.sub_amount_dbl)                                       AS subaward_median_dollars,
            MAX(s.sub_amount_dbl)                                          AS subaward_max_dollars,
            QUANTILE_CONT(s.sub_amount_dbl, 0.95)                          AS subaward_p95_dollars,
            array_to_string(
                list_distinct(list(s.sub_pop_state) FILTER (WHERE s.sub_pop_state IS NOT NULL)),
                '|'
            )                                                              AS subaward_pop_states,
            MODE() WITHIN GROUP (ORDER BY s.sub_pop_state)                 AS subaward_pop_primary_state,
            COUNT(DISTINCT s.sub_pop_state)                                AS subaward_pop_state_count,
            ANY_VALUE(tp.subaward_top_primes)                              AS subaward_top_primes,
            ANY_VALUE(tn.subaward_top_naics)                               AS subaward_top_naics,
            ANY_VALUE(tn.subaward_top_naics_primary)                       AS subaward_top_naics_primary,
            MIN(s.sub_action_date_typed)                                   AS subaward_earliest_date,
            MAX(s.sub_action_date_typed)                                   AS subaward_latest_date,
            SUM(CASE WHEN s.sub_action_date_typed >= CURRENT_DATE - INTERVAL '365 days' THEN 1 ELSE 0 END)
                                                                           AS subaward_count_last_12mo,
            SUM(CASE WHEN s.sub_action_date_typed >= CURRENT_DATE - INTERVAL '365 days'
                     THEN COALESCE(s.sub_amount_dbl, 0) ELSE 0 END)        AS subaward_dollars_last_12mo,
            SUM(CASE WHEN s.sub_action_date_typed >= CURRENT_DATE - INTERVAL '5 years' THEN 1 ELSE 0 END)
                                                                           AS subaward_count_last_5y
        FROM subaward_typed s
        LEFT JOIN sub_top_primes tp ON s.recipient_uei = tp.recipient_uei
        LEFT JOIN sub_top_naics  tn ON s.recipient_uei = tn.recipient_uei
        GROUP BY s.recipient_uei
    """)

    # 2e. FULL OUTER JOIN: prime features × subaward features.
    #     P2 fix: subawardee_uei already renamed to recipient_uei in subaward_typed,
    #     so subaward_features.recipient_uei is the clean join key.
    #     COALESCE(p.recipient_uei, s.recipient_uei) captures subaward-only UEIs.
    #     All 16 v1 prime columns propagate verbatim (NULL for subaward-only UEIs).
    #     Derived columns use literal v1 column names (P1 resolution):
    #       is_subawardee_only: lifetime_contract_count IS NULL OR = 0 (NOT award_count)
    #       combined_total_dollars: lifetime_total_obligated (NOT award_total_dollars)
    #       combined_state_count: DROPPED (no multi-value pop_states in v1 to union)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE features_v2 AS
        SELECT
            COALESCE(p.recipient_uei, s.recipient_uei)                     AS recipient_uei,
            -- 16 v1 prime-side columns propagate verbatim (NULL for subaward-only UEIs)
            p.pop_primary_state,
            p.distinct_pop_states,
            p.distinct_naics_codes,
            p.distinct_psc_codes,
            p.distinct_agencies,
            p.lifetime_contract_count,
            p.lifetime_total_obligated,
            p.max_single_award_obligation,
            p.latest_action_date,
            p.max_pop_end_date,
            p.contract_count_365d,
            p.total_obligated_365d,
            -- subaward-side columns (NULL for prime-only UEIs)
            s.subaward_count,
            s.subaward_total_dollars,
            s.subaward_min_dollars,
            s.subaward_median_dollars,
            s.subaward_max_dollars,
            s.subaward_p95_dollars,
            s.subaward_pop_states,
            s.subaward_pop_primary_state,
            s.subaward_pop_state_count,
            s.subaward_top_primes,
            s.subaward_top_naics,
            s.subaward_top_naics_primary,
            s.subaward_earliest_date,
            s.subaward_latest_date,
            s.subaward_count_last_12mo,
            s.subaward_dollars_last_12mo,
            s.subaward_count_last_5y,
            -- derived combined columns (literal v1 column names per P1 narrow-subaward-additions)
            ((p.lifetime_contract_count IS NULL OR p.lifetime_contract_count = 0)
                AND s.subaward_count IS NOT NULL AND s.subaward_count > 0)  AS is_subawardee_only,
            COALESCE(p.lifetime_total_obligated, 0)
                + COALESCE(s.subaward_total_dollars, 0)                     AS combined_total_dollars,
            CASE
                WHEN (p.lifetime_contract_count IS NOT NULL AND p.lifetime_contract_count > 0)
                 AND (s.subaward_count IS NOT NULL AND s.subaward_count > 0)
                    THEN 'prime|subaward'
                WHEN (p.lifetime_contract_count IS NOT NULL AND p.lifetime_contract_count > 0)
                    THEN 'prime'
                ELSE 'subaward'
            END                                                              AS roles,
            -- provenance (overwrite v1's emit_run_id / generated_at / feature_version)
            CAST('{emit_run_id}' AS VARCHAR)                                 AS emit_run_id,
            TIMESTAMP '{generated_at}'                                       AS generated_at,
            '{EMIT_VERSION}'                                                 AS feature_version
        FROM features p
        FULL OUTER JOIN subaward_features s ON p.recipient_uei = s.recipient_uei
    """)

    rows_planned = con.execute("SELECT COUNT(*) FROM features_v2").fetchone()[0]
    LOG.info("features_v2 computed: %d rows (floor=%d)", rows_planned, MIN_ROW_FLOOR)

    # Forensic: log subaward-only gold cohort count and prime-side UEIs.
    distinct_typed = con.execute(
        "SELECT COUNT(DISTINCT recipient_uei) FROM typed"
    ).fetchone()[0]
    subaward_only_count = con.execute(
        "SELECT COUNT(*) FROM features_v2 WHERE is_subawardee_only = TRUE"
    ).fetchone()[0]
    LOG.info(
        "prime_ueis=%d  total_v2_rows=%d  subaward_only_gold_cohort=%d",
        distinct_typed, rows_planned, subaward_only_count,
    )

    if not apply:
        LOG.info("dry-run: pass --apply to write Lance")
        return {"status": "dry-run", "rows_planned": rows_planned}

    # 3. Ledger: insert 'running' row.
    run_id, _source_id = _record_run_start()

    # 4. Volume floor check BEFORE write.
    if rows_planned < MIN_ROW_FLOOR:
        msg = f"FAIL: rows_planned={rows_planned} below floor={MIN_ROW_FLOOR}"
        LOG.error(msg)
        _record_run_complete(run_id, status="failed", error_message=msg)
        raise RuntimeError(msg)

    # 5. Lance write inside commit_lock.
    upstream_version: str | None = None
    try:
        with lance_commit_lock(DATASET_SLUG):
            LOG.info("writing Lance dataset v2 (mode=overwrite) to %s ...", OUTPUT_URI)
            t_write = time.time()
            reader = con.execute("SELECT * FROM features_v2").to_arrow_reader(
                batch_size=50_000
            )
            ds = lance.write_dataset(
                reader, OUTPUT_URI, mode="overwrite", storage_options=storage
            )
            rows_written = ds.count_rows()
            upstream_version = str(ds.version)
            write_dur = round(time.time() - t_write, 1)
            LOG.info(
                "wrote %d rows in %.1fs (lance version=%s)",
                rows_written, write_dur, ds.version,
            )

            if rows_written < MIN_ROW_FLOOR:
                msg = f"FAIL: rows_written={rows_written} below floor={MIN_ROW_FLOOR}"
                LOG.error(msg)
                _record_run_complete(run_id, status="failed", error_message=msg)
                raise RuntimeError(msg)

            # 6. BTREE scalar indexes on recipient_uei and pop_primary_state (P5 — preserve v1 BTREEs).
            #    LANCE_BYPASS_SPILLING must be set before index creation.
            os.environ["LANCE_BYPASS_SPILLING"] = "true"
            LOG.info("creating BTREE scalar index on recipient_uei ...")
            ds.create_scalar_index("recipient_uei", index_type="BTREE", replace=True)
            LOG.info("creating BTREE scalar index on pop_primary_state ...")
            ds.create_scalar_index("pop_primary_state", index_type="BTREE", replace=True)

            # Sanity: pop_primary_state cardinality should be in [40, 60] (US states + territories).
            distinct_states = con.execute(
                "SELECT COUNT(DISTINCT pop_primary_state) FROM features_v2"
            ).fetchone()[0]
            LOG.info(
                "pop_primary_state cardinality: %d distinct values (expected 40-60)",
                distinct_states,
            )

            # Sanity: gold cohort (is_subawardee_only=TRUE) must be > 0.
            gold_cohort = con.execute(
                "SELECT COUNT(*) FROM features_v2 WHERE is_subawardee_only = TRUE"
            ).fetchone()[0]
            LOG.info(
                "is_subawardee_only=TRUE count: %d (validator measured 2,901 in source; "
                "load-bearing assertion: must be > 0)",
                gold_cohort,
            )
            if gold_cohort == 0:
                msg = "FAIL: gold cohort sanity gate: count(is_subawardee_only=TRUE) = 0; subaward leg silently dropped"
                LOG.error(msg)
                _record_run_complete(run_id, status="failed", error_message=msg)
                raise RuntimeError(msg)

            # 7. Compact + cleanup old versions.
            LOG.info("optimize: compact + cleanup_older_than=7d ...")
            try:
                stats = ds.optimize.compact_files()
                LOG.info("  compact_files: %s", stats)
            except Exception as exc:
                LOG.warning("  compact_files failed (non-fatal): %s", exc)
            try:
                cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
                LOG.info("  cleanup_old_versions: %s", cleanup)
            except Exception as exc:
                LOG.warning("  cleanup_old_versions failed (non-fatal): %s", exc)

    except Exception as exc:
        _record_run_complete(
            run_id, status="failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    total_dur = round(time.time() - t0, 1)
    _record_run_complete(
        run_id, status="completed",
        rows_emitted=rows_written,
        upstream_version=upstream_version,
    )

    metrics = {
        "status": "succeeded",
        "dataset_slug": DATASET_SLUG,
        "rows_emitted": rows_written,
        "lance_version": upstream_version,
        "write_seconds": write_dur,
        "total_seconds": total_dur,
        "prime_ueis_in_source": distinct_typed,
        "subaward_only_gold_cohort": subaward_only_count,
        "distinct_states": distinct_states,
        "gold_cohort_rows": gold_cohort,
        "emit_run_id": emit_run_id,
    }
    LOG.info("OK — metrics: %s", metrics)
    return metrics


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit USAspending recipient-features Lance dataset"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually run the emit (dry-run otherwise)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    if not args.apply:
        LOG.info("dry-run: pass --apply to execute")
        result = emit(apply=False)
        print(json.dumps(result, indent=2, default=str))
        return 0

    result = emit(apply=True)
    print(f"OK — metrics: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
