#!/usr/bin/env python3
"""Idempotent seed for ops.data_sources + ops.data_source_slas.

Populates the source registry from the canonical inventory:
  ~/Desktop/hq/inventory/AUDIENCE-SPEC-R2-RW-INVENTORY-2026-05-11.md

Sources are defined inline (not parsed from markdown — fragile) and match
the inventory at the time of the audit pass (2026-05-12T04:30Z). The
hardcoded list covers:
  - 25 R2 top-level prefixes
  - 14 fmcsa-derived sub-prefixes (audience MVs)
  - 5 Iceberg fmcsa.* catalog tables (plus fmcsa-carrier-essentials)
  - ~22 RW MV outputs

SLA defaults (per directive §"Validator notes §Verification gate"):
  - daily snapshot sources  → 86400 s (24 h)
  - weekly                  → 604800 s (7 d)
  - quarterly               → 7889400 s (~91 d)
  - anomaly prefixes        → NULL + status='needs_triage'

Idempotent: INSERT ... ON CONFLICT (display_name) DO UPDATE ... WHERE ...
IS DISTINCT FROM EXCLUDED ... — re-runs are safe and produce no new rows
when the source list is unchanged.

Usage:
  doppler run --project hq-all --config prd -- \\
    python3 apps/data-engine-x/scripts/seed_observability_sources.py

Doppler scope: hq-all/prd (NOT data-engine-x — that project no longer
exists per doppler_architecture.md memory, 2026-05-09).
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------
# Each entry: (display_name, storage_uri, format, owner_app, status,
#              sla_freshness_seconds, sla_basis, sla_notes)
# status: 'active' | 'needs_triage' | 'retired'
# sla_freshness_seconds: int | None (None => anomaly, no SLA)
# ---------------------------------------------------------------------------

DAILY = 86400       # 24 h
WEEKLY = 604800     # 7 d
QUARTERLY = 7889400  # ~91 d
NO_SLA = None

R2_BUCKET = "s3://dex-raw-landing-zone"

# ── R2 top-level prefixes (25) ──────────────────────────────────────────────
R2_TOP_LEVEL: list[dict[str, Any]] = [
    # normal / active sources
    dict(display_name="bls_oews",          storage_uri=f"{R2_BUCKET}/bls-oews/",          format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="BLS Occupational Employment & Wage Statistics"),
    dict(display_name="cms_open_payments",  storage_uri=f"{R2_BUCKET}/cms-open-payments/", format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="CMS Open Payments; iceberg also wired"),
    dict(display_name="cms_pecos",          storage_uri=f"{R2_BUCKET}/cms-pecos/",         format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="CMS PECOS provider enrollment"),
    dict(display_name="dol_5500",           storage_uri=f"{R2_BUCKET}/dol-5500/",          format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=QUARTERLY,  basis="last_ingested", notes="DOL Form 5500 ERISA filings, quarterly"),
    dict(display_name="epa_npdes_cgp",      storage_uri=f"{R2_BUCKET}/epa-npdes-cgp/",     format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="EPA NPDES Construction General Permit"),
    dict(display_name="fdic",               storage_uri=f"{R2_BUCKET}/fdic/",              format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="FDIC bank data"),
    dict(display_name="fec",                storage_uri=f"{R2_BUCKET}/fec/",               format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="FEC campaign finance"),
    dict(display_name="fmcsa",              storage_uri=f"{R2_BUCKET}/fmcsa/",             format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=DAILY,      basis="last_ingested", notes="FMCSA raw carrier/inspection/crash files"),
    dict(display_name="fmcsa_carrier_essentials", storage_uri=f"{R2_BUCKET}/fmcsa-carrier-essentials/", format="r2_parquet", owner_app="data-engine-x", status="active", sla=WEEKLY, basis="last_ingested", notes="FMCSA carrier essentials (legacy prefix)"),
    dict(display_name="franchisors",        storage_uri=f"{R2_BUCKET}/franchisors/",       format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="Franchisor/franchisee registry"),
    dict(display_name="gleif",              storage_uri=f"{R2_BUCKET}/gleif/",             format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="GLEIF LEI-keyed entity data"),
    dict(display_name="google_maps",        storage_uri=f"{R2_BUCKET}/google-maps/",       format="r2_parquet",  owner_app="data-engine-x", status="needs_triage", sla=NO_SLA,     basis="last_ingested", notes="Google Maps data — origin unclear; needs triage"),
    dict(display_name="hmda",               storage_uri=f"{R2_BUCKET}/hmda/",              format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=QUARTERLY,  basis="last_ingested", notes="HMDA LAR + panel (CFPB quarterly)"),
    dict(display_name="hud_multifamily",    storage_uri=f"{R2_BUCKET}/hud-multifamily/",   format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="HUD Multifamily housing data"),
    dict(display_name="sam_gov_opps",       storage_uri=f"{R2_BUCKET}/sam-gov-opps/",      format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=DAILY,      basis="last_ingested", notes="SAM.gov contract opportunities, daily snapshots"),
    dict(display_name="sba",               storage_uri=f"{R2_BUCKET}/sba/",               format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="SBA loan + PPP data"),
    dict(display_name="sec_adv",            storage_uri=f"{R2_BUCKET}/sec-adv/",           format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=WEEKLY,     basis="last_ingested", notes="SEC Form ADV investment advisor data"),
    dict(display_name="usaspending",        storage_uri=f"{R2_BUCKET}/usaspending/",       format="r2_parquet",  owner_app="data-engine-x", status="active",       sla=DAILY,      basis="last_ingested", notes="USAspending API delta, daily cadence"),
    # iceberg test / infra prefixes — needs_triage / no SLA
    dict(display_name="iceberg_test",       storage_uri=f"{R2_BUCKET}/iceberg-test/",      format="r2_parquet",  owner_app="data-engine-x", status="needs_triage", sla=NO_SLA,     basis="last_ingested", notes="Iceberg test prefix — infra only, not user-visible"),
    dict(display_name="iceberg_warehouse",  storage_uri=f"{R2_BUCKET}/iceberg-warehouse/", format="parquet_iceberg", owner_app="data-engine-x", status="needs_triage", sla=NO_SLA, basis="last_ingested", notes="Iceberg warehouse root — not a discrete audience"),
    # anomaly prefixes — needs_triage, no SLA
    dict(display_name="bridges",            storage_uri=f"{R2_BUCKET}/bridges/",           format="r2_parquet",  owner_app="data-engine-x", status="needs_triage", sla=NO_SLA,     basis="last_ingested", notes="Origin unclear — triage required"),
    dict(display_name="dfpi",               storage_uri=f"{R2_BUCKET}/dfpi/",              format="r2_parquet",  owner_app="data-engine-x", status="needs_triage", sla=NO_SLA,     basis="last_ingested", notes="DFPI California regulator — origin unclear"),
    dict(display_name="epiq",               storage_uri=f"{R2_BUCKET}/epiq/",              format="r2_parquet",  owner_app="data-engine-x", status="needs_triage", sla=NO_SLA,     basis="last_ingested", notes="Epiq data — origin unclear"),
    dict(display_name="federal",            storage_uri=f"{R2_BUCKET}/federal/",           format="r2_parquet",  owner_app="data-engine-x", status="needs_triage", sla=NO_SLA,     basis="last_ingested", notes="Generic federal prefix — needs triage"),
    # CA UCC — pre-staged for operator-supplied bulk drop (directive 2026-05-12).
    # status='needs_triage' until first ingest; flip to 'active' + set SLA
    # (likely 604800s / weekly) once cadence is known. See
    # apps/data-engine-x/docs/ucc_ca_ingest.md for the operator's drop flow.
    dict(display_name="ucc_ca_filings",     storage_uri=f"{R2_BUCKET}/ucc/state=CA/",      format="r2_parquet",  owner_app="data-engine-x", status="needs_triage", sla=NO_SLA,     basis="last_ingested", notes="CA UCC bulk (Master Unload + Weekly Data) — awaiting operator bulk-data drop; flip to active + set SLA on first ingest"),
]

# ── fmcsa-derived sub-prefixes (14 audience MVs) ────────────────────────────
FMCSA_DERIVED_NAMES = [
    "_factory_poc_carrier_essentials",
    "actpendinsur_essentials",
    "authhist_essentials",
    "boc3_awh",
    "carrier_essentials",
    "carrier_inspection_state_footprint",
    "carrier_latest",
    "carrier_registrations_essentials",
    "crash_essentials",
    "email_attributed",       # BREACH TEST CASE — stale >36h per inventory line 386
    "inshist_essentials",
    "insur_awh",
    "officer_normalized",     # BREACH TEST CASE — stale >38h per inventory line 387
    "rejected_essentials",
]

FMCSA_DERIVED_SOURCES: list[dict[str, Any]] = [
    dict(
        display_name=f"fmcsa_derived_{name}",
        storage_uri=f"{R2_BUCKET}/fmcsa-derived/{name}/",
        format="r2_parquet",
        owner_app="data-engine-x",
        status="active",
        sla=DAILY,
        basis="last_ingested",
        notes=f"FMCSA derived audience: {name}; daily snapshot cadence",
    )
    for name in FMCSA_DERIVED_NAMES
]

# ── Iceberg catalog tables (fmcsa.*) ────────────────────────────────────────
ICEBERG_FMCSA_TABLES = [
    "authhist",
    "carrier",
    "company_census_file",
    "crash_file",
    "inspections_and_citations",
]

ICEBERG_SOURCES: list[dict[str, Any]] = [
    dict(
        display_name=f"iceberg_fmcsa_{table}",
        storage_uri=f"{R2_BUCKET}/iceberg-warehouse/fmcsa/{table}/",
        format="parquet_iceberg",
        owner_app="data-engine-x",
        status="active",
        sla=DAILY,
        basis="last_ingested",
        notes=f"Iceberg table fmcsa.{table}; daily refresh",
    )
    for table in ICEBERG_FMCSA_TABLES
]

# ── RisingWave MV outputs ────────────────────────────────────────────────────
RW_MV_SOURCES: list[dict[str, Any]] = [
    # Pharma / CMS
    dict(display_name="mv_pharma_spend_analysis",           storage_uri="risingwave://prod/public/mv_pharma_spend_analysis",           format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="CMS Open Payments pharma spend analysis MV"),
    dict(display_name="mv_cms_open_payments_general_2024_silver",  storage_uri="risingwave://prod/public/mv_cms_open_payments_general_2024_silver",  format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY, basis="last_ingested", notes="CMS Open Payments General 2024 silver"),
    dict(display_name="mv_cms_open_payments_research_2024_silver", storage_uri="risingwave://prod/public/mv_cms_open_payments_research_2024_silver", format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY, basis="last_ingested", notes="CMS Open Payments Research 2024 silver"),
    # PPP / SBA
    dict(display_name="mv_ppp_identity_unmasking",          storage_uri="risingwave://prod/public/mv_ppp_identity_unmasking",          format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="PPP loan identity unmasking MV"),
    dict(display_name="mv_sba_ppp_silver",                  storage_uri="risingwave://prod/public/mv_sba_ppp_silver",                  format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SBA PPP silver MV"),
    dict(display_name="mv_sba_historical_survivability",    storage_uri="risingwave://prod/public/mv_sba_historical_survivability",    format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SBA historical survivability MV"),
    # Federal contracts
    dict(display_name="mv_federal_contract_leads",          storage_uri="risingwave://prod/public/mv_federal_contract_leads",          format="rw_mv", owner_app="data-engine-x", status="active", sla=DAILY,     basis="last_ingested", notes="USAspending federal contract leads MV"),
    dict(display_name="mv_usaspending_contracts_typed",     storage_uri="risingwave://prod/public/mv_usaspending_contracts_typed",     format="rw_mv", owner_app="data-engine-x", status="active", sla=DAILY,     basis="last_ingested", notes="USAspending contracts typed MV"),
    # SEC ADV
    dict(display_name="mv_sec_adv_investment_advisors",     storage_uri="risingwave://prod/public/mv_sec_adv_investment_advisors",     format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SEC Form ADV investment advisors"),
    dict(display_name="mv_sec_adv_fund_managers",           storage_uri="risingwave://prod/public/mv_sec_adv_fund_managers",           format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SEC Form ADV fund managers"),
    dict(display_name="mv_sec_adv_fiduciaries",             storage_uri="risingwave://prod/public/mv_sec_adv_fiduciaries",             format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SEC Form ADV fiduciaries"),
    dict(display_name="mv_sec_adv_schedule_a",              storage_uri="risingwave://prod/public/mv_sec_adv_schedule_a",              format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SEC Form ADV Schedule A direct owners"),
    dict(display_name="mv_sec_adv_schedule_b",              storage_uri="risingwave://prod/public/mv_sec_adv_schedule_b",              format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SEC Form ADV Schedule B indirect owners"),
    dict(display_name="mv_sec_adv_dba_names",               storage_uri="risingwave://prod/public/mv_sec_adv_dba_names",               format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SEC Form ADV DBA trade names"),
    dict(display_name="mv_sec_adv_ria_state_registrations", storage_uri="risingwave://prod/public/mv_sec_adv_ria_state_registrations", format="rw_mv", owner_app="data-engine-x", status="active", sla=WEEKLY,    basis="last_ingested", notes="SEC Form ADV state registrations"),
    # DOL 5500
    dict(display_name="mv_dol_5500_employers",              storage_uri="risingwave://prod/public/mv_dol_5500_employers",              format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="DOL 5500 employer-level targeting"),
    dict(display_name="mv_dol_5500_plans",                  storage_uri="risingwave://prod/public/mv_dol_5500_plans",                  format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="DOL 5500 plan-level data"),
    dict(display_name="mv_dol_5500_fiduciaries",            storage_uri="risingwave://prod/public/mv_dol_5500_fiduciaries",            format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="DOL 5500 plan fiduciaries"),
    dict(display_name="mv_employer_stability_signals",      storage_uri="risingwave://prod/public/mv_employer_stability_signals",      format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="Employer stability signals cross-source"),
    # HMDA
    dict(display_name="mv_hmda_lar_unified",                storage_uri="risingwave://prod/public/mv_hmda_lar_unified",                format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="HMDA LAR multi-year unified MV"),
    dict(display_name="mv_market_map_credit_supply",        storage_uri="risingwave://prod/public/mv_market_map_credit_supply",        format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="HMDA market map credit supply"),
    dict(display_name="mv_market_map_lender_concentration", storage_uri="risingwave://prod/public/mv_market_map_lender_concentration", format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="HMDA market map lender concentration"),
    dict(display_name="mv_lending_stability_history",       storage_uri="risingwave://prod/public/mv_lending_stability_history",       format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="HMDA lending stability history"),
    dict(display_name="hmda_market_map",                    storage_uri="risingwave://prod/public/source_hmda_lar_r2",                 format="rw_mv", owner_app="data-engine-x", status="active", sla=QUARTERLY, basis="last_ingested", notes="HMDA market map base table (multi-year LAR)"),
]

# ── DEX Postgres-resident MVs / tables (cycle outputs that aren't R2/Lance/RW) ──
DEX_POSTGRES_SOURCES: list[dict[str, Any]] = [
    dict(
        display_name="entities_pdl_to_sba_borrowers_fuzzy_v1",
        storage_uri="postgres://dex/entities/mv_pdl_to_sba_borrowers_fuzzy_v1",
        format="postgres_mv",
        owner_app="data-engine-x",
        status="active",
        sla=DAILY,
        basis="last_ingested",
        notes="PDL × SBA fuzzy match v1 — embedding-cosine sibling to "
              "entities.mv_pdl_to_sba_borrowers. Populated by "
              "scripts/run_pdl_sba_fuzzy_match_emit.py "
              "(sentence-transformers/all-MiniLM-L6-v2). Cycle "
              "hq-all-pdl-sba-fuzzy-match-v1.",
    ),
]

ALL_SOURCES: list[dict[str, Any]] = (
    R2_TOP_LEVEL + FMCSA_DERIVED_SOURCES + ICEBERG_SOURCES + RW_MV_SOURCES
    + DEX_POSTGRES_SOURCES
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED (or DEX_DB_URL_DIRECT) must be set via Doppler")
    return url


def seed(conn: psycopg.Connection) -> None:
    inserted = 0
    updated = 0
    sla_upserted = 0

    for src in ALL_SOURCES:
        display_name = src["display_name"]
        storage_uri = src["storage_uri"]
        fmt = src["format"]
        owner_app = src["owner_app"]
        status = src["status"]
        sla = src["sla"]
        basis = src["basis"]
        notes = src.get("notes")

        # Upsert data_sources
        result = conn.execute(
            """
            INSERT INTO ops.data_sources
                (display_name, storage_uri, format, owner_app, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (display_name) DO UPDATE
                SET storage_uri = EXCLUDED.storage_uri,
                    format      = EXCLUDED.format,
                    owner_app   = EXCLUDED.owner_app,
                    status      = EXCLUDED.status
                WHERE ops.data_sources.storage_uri IS DISTINCT FROM EXCLUDED.storage_uri
                   OR ops.data_sources.format      IS DISTINCT FROM EXCLUDED.format
                   OR ops.data_sources.owner_app   IS DISTINCT FROM EXCLUDED.owner_app
                   OR ops.data_sources.status      IS DISTINCT FROM EXCLUDED.status
            RETURNING source_id,
                (xmax = 0) AS was_inserted
            """,
            (display_name, storage_uri, fmt, owner_app, status),
        ).fetchone()

        if result is None:
            # No change — conflict + nothing updated
            source_id_result = conn.execute(
                "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
                (display_name,),
            ).fetchone()
            assert source_id_result is not None
            source_id = source_id_result[0]
        else:
            source_id, was_inserted = result
            if was_inserted:
                inserted += 1
            else:
                updated += 1

        # Upsert data_source_slas
        conn.execute(
            """
            INSERT INTO ops.data_source_slas
                (source_id, sla_freshness_seconds, sla_basis, notes)
            VALUES (%s, %s, %s::data_source_sla_basis, %s)
            ON CONFLICT (source_id) DO UPDATE
                SET sla_freshness_seconds = EXCLUDED.sla_freshness_seconds,
                    sla_basis             = EXCLUDED.sla_basis,
                    notes                 = EXCLUDED.notes,
                    updated_at            = NOW()
                WHERE ops.data_source_slas.sla_freshness_seconds IS DISTINCT FROM EXCLUDED.sla_freshness_seconds
                   OR ops.data_source_slas.sla_basis             IS DISTINCT FROM EXCLUDED.sla_basis
                   OR ops.data_source_slas.notes                 IS DISTINCT FROM EXCLUDED.notes
            """,
            (source_id, sla, basis, notes),
        )
        sla_upserted += 1

    conn.commit()

    total = len(ALL_SOURCES)
    log.info(
        "seed complete: total=%d inserted=%d updated=%d sla_rows=%d",
        total, inserted, updated, sla_upserted,
    )

    # ---------------------------------------------------------------------------
    # Retire unmapped shadow rows
    #
    # A prior seed run created bulk_ingest_unmapped_* rows via a fallback
    # name-resolver path that no longer exists. These are duplicates of
    # already-active feeds and were confirmed as such in the anomaly-triage
    # doc (ANOMALY-PREFIX-TRIAGE-2026-05-12.md). Retire them here idempotently
    # so the verification gate `COUNT(*) ... = 0` passes on every re-run.
    # ---------------------------------------------------------------------------
    retired_count = conn.execute(
        """
        UPDATE ops.data_sources
        SET status = 'retired'
        WHERE display_name LIKE 'bulk_ingest_unmapped_%'
          AND status != 'retired'
        RETURNING display_name
        """
    ).fetchall()
    if retired_count:
        for row in retired_count:
            log.info("retired unmapped shadow row: %s", row[0])
    conn.commit()

    # Post-seed summary
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'needs_triage') AS needs_triage,
            COUNT(*) FILTER (WHERE display_name IN (
                'fmcsa_derived_email_attributed',
                'fmcsa_derived_officer_normalized'
            )) AS breach_test_cases,
            COUNT(*) FILTER (WHERE display_name LIKE 'bulk_ingest_unmapped_%'
                                AND status != 'retired') AS unmapped_active
        FROM ops.data_sources
        """
    ).fetchone()
    if counts:
        log.info(
            "ops.data_sources: total=%d needs_triage=%d breach_test_cases=%d unmapped_active=%d",
            counts[0], counts[1], counts[2], counts[3],
        )
        if counts[3] > 0:
            log.error("SEED FAIL: %d unmapped shadow rows still active after retire pass", counts[3])


def main() -> int:
    db_url = get_db_url()
    log.info("connecting to DB (%s...)", db_url[:30])
    with psycopg.connect(db_url) as conn:
        seed(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
