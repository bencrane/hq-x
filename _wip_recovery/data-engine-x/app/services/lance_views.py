"""Register Lance datasets as DuckDB views via the Arrow bridge.

Companion to ``duckdb_views.py`` (which loads .sql files). Lance datasets
cannot yet be referenced from a single SQL file because the DuckDB
``lance`` community extension is not stable for DuckDB 1.5.x on osx_arm64
as of 2026-05-12 (community-extensions repo returns 404 for the
``osx_arm64/lance.duckdb_extension.gz`` artifact). Once that extension
publishes for our DuckDB build, this file becomes obsolete: drop in
``views/fmcsa/carrier_latest_lance.sql`` instead.

Until then, the Lance read path goes:

    lance.dataset(uri, storage_options=...)  →  .scanner(...).to_table()  →
        DuckDB.register('lance_view_name', arrow_table)  →  SQL queries

This works but materializes the projected columns up front. For a 4.4M-row
dataset with 53 columns that's ~500 MB in process memory. Acceptable for
the canary; the eventual lance-duckdb extension will fix this with
push-down predicate evaluation.

Each entry below corresponds to one Lance dataset registered as a DuckDB
view. The view name MUST match the convention
``<source>_<table>_<format>`` (e.g. ``fmcsa_carrier_latest_lance``).

Per-DOT random access on these views still goes through Lance — call
``lance.dataset(uri).scanner(filter=...)`` directly in code paths that
care about latency. The DuckDB view is for SQL-ergonomic full-table or
aggregate workloads.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    import duckdb

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class LanceView:
    """Declarative spec for a Lance dataset → DuckDB view registration."""

    name: str
    """View name as it appears in DuckDB SQL."""

    uri: str
    """Lance dataset URI (s3://...)."""

    description: str = ""
    """Doc for the view; not exposed to DuckDB but useful for inspection."""

    register_at_boot: bool = True
    """If False, this view is NOT eagerly materialized at FastAPI boot.

    The Arrow-bridge pattern fully materializes the projected columns into
    process memory at registration time. For datasets > a few hundred MB,
    the boot-time cost is prohibitive. Set to False; the view becomes
    on-demand via ``register_lance_view_lazy(con, name, filter=...)`` — a
    bounding Lance scanner filter is required to keep the Arrow load
    within container memory.
    """


# The canonical set of Lance views the typed_audiences runtime should register
# at FastAPI boot. Add entries as new Lance datasets land.
#
# Naming convention: the materialized Arrow-table registration gets a "_raw"
# suffix. The SQL view at views/<namespace>/<name>.sql renames + projects
# on top of the _raw table and becomes the public-facing name.
LANCE_VIEWS: list[LanceView] = [
    LanceView(
        name="fmcsa_carrier_essentials_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance",
        description=(
            "FMCSA carrier essentials, latest snapshot per refresh, Lance format. "
            "Raw Arrow-table materialization of the Lance dataset. The public-facing "
            "SQL view fmcsa_carrier_latest_lance (views/fmcsa/carrier_latest_lance.sql) "
            "renames + projects on top of this. Per-DOT lookups should go through "
            "lance.dataset(uri).scanner(filter=...) directly, not via this view."
        ),
    ),
    # Wave 1 sweep — sibling FMCSA cohorts. Same R2 layout, same operational
    # discipline as the canary. BTREE on dot_number.
    LanceView(
        name="fmcsa_crash_essentials_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/crash_essentials_lance",
        description=(
            "FMCSA crash essentials, latest snapshot per refresh, Lance format. "
            "Per-DOT crash history; public SQL view at "
            "views/fmcsa/crash_latest_lance.sql."
        ),
    ),
    LanceView(
        name="fmcsa_authhist_essentials_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/authhist_essentials_lance",
        description=(
            "FMCSA authority-history essentials, latest snapshot per refresh, "
            "Lance format. Per-DOT authority history; public SQL view at "
            "views/fmcsa/authhist_latest_lance.sql."
        ),
    ),
    # Wave 1 sweep — SAM.gov opps active feed. BTREE on notice_id.
    LanceView(
        name="sam_gov_opps_active_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/opps_active_lance",
        description=(
            "SAM.gov opportunities active feed, latest snapshot per refresh, "
            "Lance format. Per-notice lookups; public SQL view at "
            "views/sam_gov/opps_active_latest_lance.sql."
        ),
    ),
    # Phase 4 vector layer — embeddings of fmcsa_carrier_essentials, used
    # by the audience-spec similar_to + semantic_match primitives. The
    # vector_index lives in-Lance (IVF_PQ); per-DOT lookups + ANN search
    # go through lance.dataset(uri).scanner(...) directly, NOT this view.
    # The DuckDB view exists for batch/aggregate SQL ergonomics (e.g.
    # "how many carriers have embeddings vs source rows").
    #
    # register_at_boot=False — small today (~2M rows × 384-or-1536-dim) but
    # the Arrow-bridge materialization can balloon memory on cold start.
    # The vector primitives in the evaluator open Lance directly.
    LanceView(
        name="fmcsa_carrier_essentials_embeddings_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/"
            "carrier_essentials_embeddings_lance"
        ),
        description=(
            "FMCSA carrier essentials embeddings (Phase 4 vector layer). "
            "Rows = (dot_number, embedding_vector, content_hash, "
            "profile_text, embedded_at, model_version). The evaluator's "
            "similar_to + semantic_match primitives open Lance directly; "
            "this view is for SQL-only aggregate workloads."
        ),
        register_at_boot=False,
    ),
    # Wave 1 sweep — USAspending contracts (multi-year, currently 2024-2026).
    # BTREE on recipient_uei for "show me everything awarded to UEI X" lookups.
    #
    # register_at_boot=False — the Arrow bridge fully materializes the dataset
    # at registration time; USAspending contracts at ~30M rows × ~298 columns
    # is too large for FastAPI process memory. Per-UEI lookups go through
    # lance.dataset(uri).scanner(filter='recipient_uei = ...') directly. The
    # SQL view exists so that batch / aggregate workloads have an ergonomic
    # path via on-demand registration (``register_lance_view_lazy(...)``).
    LanceView(
        name="usaspending_contracts_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance",
        description=(
            "USAspending federal contracts, currently fiscal years 2024-2026, "
            "Lance format. Per-UEI lookups; public SQL view at "
            "views/usaspending/contracts_latest_lance.sql. NOT registered at "
            "boot — too large for in-process materialization; lookup via "
            "lance.dataset(uri).scanner(filter=...) directly."
        ),
        register_at_boot=False,
    ),
    # Wave 2 sweep — CMS Open Payments General 2024+ (normalized 15-col
    # schema). BTREE on record_id. ~15.4M rows; register_at_boot=False to
    # keep FastAPI cold-start memory bounded.
    LanceView(
        name="cms_open_payments_general_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/"
            "cms_open_payments/general_payments_lance"
        ),
        description=(
            "CMS Open Payments General feed (drug/biological/device payments "
            "to physicians and teaching hospitals), 2024 normalized 15-col "
            "schema. Per-record_id lookups; public SQL view at "
            "views/cms_open_payments/general_payments_latest_lance.sql. NOT "
            "registered at boot — ~15.4M rows is too large for in-process "
            "materialization; lookup via lance.dataset(uri).scanner(...) "
            "directly."
        ),
        register_at_boot=False,
    ),
    # Wave 2 sweep — CMS Open Payments Research 2024+ (same 15-col normalized
    # schema as General). BTREE on record_id. ~756K rows; small enough to
    # register at boot.
    LanceView(
        name="cms_open_payments_research_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/"
            "cms_open_payments/research_payments_lance"
        ),
        description=(
            "CMS Open Payments Research feed (industry payments tied to "
            "clinical research), 2024 normalized 15-col schema. "
            "Per-record_id lookups; public SQL view at "
            "views/cms_open_payments/research_payments_latest_lance.sql."
        ),
    ),
    # Wave 2 sweep — GLEIF LEI records (universal legal-entity spine). BTREE
    # on lei. ~3.3M rows × 24 cols. Small enough to register at boot for
    # cross-source matching ergonomics.
    LanceView(
        name="gleif_lei_records_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/"
            "gleif/lei_records_lance"
        ),
        description=(
            "GLEIF LEI records (every LEI-registered legal entity worldwide), "
            "weekly snapshot, Lance format. Per-LEI lookups; public SQL view "
            "at views/gleif/lei_records_latest_lance.sql. The canonical "
            "legal-entity identity spine for cross-source matching."
        ),
    ),
    # carrier-detail Lance migration (fmcsa-carrier-detail-lance-v1) — 4 new
    # cohorts added to support the get_carrier_detail hot path. All
    # register_at_boot=False — the hot path goes through fmcsa_mv_detail.py's
    # direct lance.dataset(uri).scanner(filter=...) calls, not the Arrow-bridge
    # bulk materialization that register_lance_views performs. The DuckDB view
    # entries here exist for SQL-ergonomic aggregate / batch workloads only.
    LanceView(
        name="fmcsa_insurance_active_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/insurance_active_lance",
        description=(
            "FMCSA insurance active+pending (actpendinsur_essentials), daily snapshot, "
            "Lance format. BTREE on dot_number. Per-DOT lookups via "
            "lance.dataset(uri).scanner(filter=...) directly (see fmcsa_mv_detail.py). "
            "NOT registered at boot — bulk materialization is too large for hot-path use."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="fmcsa_insurance_history_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/insurance_history_lance",
        description=(
            "FMCSA insurance history (inshist_essentials), daily snapshot, Lance format. "
            "BTREE on dot_number. ~7.4M rows. Per-DOT lookups via lance.dataset(uri).scanner(). "
            "NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="fmcsa_safety_basics_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/safety_basics_lance",
        description=(
            "FMCSA SMS safety basics (sms_ab_pass — has _PCT/_BASIC_ALERT columns the "
            "LTH dashboard renders; matches safety_basics_latest.sql). Monthly snapshot, "
            "Lance format. BTREE on dot_number (lowercase; uppercase DOT_NUMBER renamed "
            "at emit time). Per-DOT lookups via lance.dataset(uri).scanner(). "
            "NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="fmcsa_inspections_recent_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/inspections_recent_lance",
        description=(
            "FMCSA vehicle inspections (vehicle_inspection_essentials), daily snapshot, "
            "Lance format. BTREE on dot_number. ~8.18M rows — heaviest FMCSA cohort. "
            "Per-DOT lookups via lance.dataset(uri).scanner(). NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # CA SoS Master Unload ingest — 3 source datasets + 1 bridge.
    # All register_at_boot=False — multi-million-row Lance datasets; per-key
    # reads go through lance.dataset(uri).scanner(filter=...) directly. DuckDB
    # view exists for SQL-ergonomic aggregate / batch workloads via lazy
    # registration (register_lance_view_lazy).
    LanceView(
        name="sos_ca_entities_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance",
        description=(
            "CA Secretary of State Master Unload — entities (Filings.csv release "
            "2026-05-16). BTREE on entity_num + entity_name_normalized. ~4.5M+ "
            "rows; NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sos_ca_principals_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_principals_lance",
        description=(
            "CA Secretary of State Master Unload — principals (Principals.csv "
            "release 2026-05-16). BTREE on entity_num + entity_name_normalized. "
            "~6M+ rows; the CA LLC owner-identity spine. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sos_ca_agents_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_agents_lance",
        description=(
            "CA Secretary of State Master Unload — agents (Agents.csv release "
            "2026-05-16). BTREE on entity_num + entity_name_normalized. ~4M+ "
            "rows; NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="bridges_sba_sos_ca_owner_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_ca_owner_lance",
        description=(
            "SBA × CA SoS principals owner-identity bridge — legal_name_normalized "
            "exact match (CA borrowers). BTREE on sba_legal_name_normalized. "
            "Method=legal_name_state_exact_ca v1.0.0; bridge_version=1.0.0. "
            "Per-row provenance: bridge_run_id, match_method, confidence_tier, "
            "sba_fan_out, sos_fan_out, generated_at. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="bridges_ucc_ca_lender_sos_ca_owner_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_lender_sos_ca_owner_lance",
        description=(
            "UCC CA secured-party (Organization) × CA SoS entities — "
            "legal_name_normalized exact match (CA). BTREE on "
            "secured_party_name_normalized + entity_num. "
            "Method=legal_name_state_exact_ca v1.0.0 (REUSED from PR #464 "
            "SBA × SoS bridge); bridge_version=1.0.0. Per-row provenance: "
            "bridge_run_id, match_method, confidence_tier, ucc_fan_out, "
            "sos_fan_out, generated_at. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # FL Sunbiz Quarterly ingest — 3 source datasets + 1 bridge.
    # All register_at_boot=False — multi-million-row Lance datasets; per-key
    # reads go through lance.dataset(uri).scanner(filter=...) directly.
    LanceView(
        name="fl_entities_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance",
        description=(
            "FL Sunbiz Quarterly — entities (cordata.zip release 2026-05-16). "
            "79-field fixed-width FL COR layout; BTREE on entity_num + "
            "entity_name_normalized. ~12M+ rows; NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="fl_officers_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_officers_lance",
        description=(
            "FL Sunbiz Quarterly — officers (extracted from inline cordata "
            "officer slots 1-6; release 2026-05-16). BTREE on entity_num + "
            "entity_name_normalized + full_name_normalized. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="fl_events_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_events_lance",
        description=(
            "FL Sunbiz Quarterly — events (corevt.txt release 2026-05-16). "
            "25-field fixed-width FL COR-EVENT layout; BTREE on "
            "event_doc_number + event_effective_date. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sba_sos_fl_owner_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_fl_owner_lance",
        description=(
            "SBA × FL Sunbiz entities owner-identity bridge — "
            "legal_name_state_exact_fl v1.0.0 (NEW state-variant; does NOT "
            "overwrite the legal_name_state_exact_ca row from PR #464). "
            "BTREE on sba_legal_name_normalized + entity_num. Floor 242,161 "
            "rows. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sam_entity_pocs_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entity_pocs_lance",
        description=(
            "SAM.gov entity POCs — long-form explode of 6 POC kinds from "
            "sam_gov.entities_lance (govt_bus, alt_govt_bus, past_perf, "
            "alt_past_perf, elec_bus, alt_elec_bus); one row per "
            "(uei, poc_kind) where at least one of (first_name, "
            "middle_initial, last_name, title) is non-null. BTREE on "
            "uei + poc_kind + full_name_normalized. Floor 1,885,939. "
            "NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sam_pdl_usaspending_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_usaspending_lance",
        description=(
            "SAM x PDL x USAspending 3-way Pattern A enriched-cohort emit at "
            "entity grain (1 row per UEI in SAM x PDL intersection). SAM core "
            "(~18 cols) + PDL enrichment (~7 cols) + USAspending rollup (8 cols "
            "incl. lifetime/active contract count + total_obligated + "
            "has_active_award). LEFT JOIN USAspending preserves all SAM x PDL "
            "UEIs (NULL rollup for ~81% without contract history). BTREE on "
            "uei + naics_primary_2digit + legal_business_name_normalized. "
            "Floor 247,876. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="usaspending_contract_subawards_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance",
        description=(
            "USAspending contract subawards (first-tier FSRS subaward reporting "
            "on prime federal contracts). Pattern A pull-through from CSV-bulk "
            "R2 parquet (usaspending/contract_subawards/year=2026/data.parquet, "
            "ingested 2026-05-09). BTREE on prime_award_unique_key + subaward_number "
            "+ subawardee_uei + prime_awardee_uei. ~16K rows. 22 trap VARCHARs "
            "(*_amount, *_date, *_fiscal_year) TRY_CAST at emit-time. Per-key "
            "lookups via lance.dataset(uri).scanner(filter=...) directly. "
            "NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="usaspending_assistance_subawards_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance",
        description=(
            "USAspending assistance subawards (first-tier FSRS subaward reporting "
            "on prime federal grants / cooperative agreements). Pattern A "
            "pull-through from CSV-bulk R2 parquet (usaspending/"
            "assistance_subawards/year=2026/data.parquet, ingested 2026-05-09). "
            "BTREE on prime_award_unique_key + subaward_number + subawardee_uei "
            "+ prime_awardee_uei. ~54K rows. 21 trap VARCHARs (*_amount, *_date, "
            "*_fiscal_year) TRY_CAST at emit-time. Per-key lookups via "
            "lance.dataset(uri).scanner(filter=...) directly. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="contracts_with_subawards_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance",
        description=(
            "Federal contracts (prime grain) × rolled-up FSRS contract subawards — "
            "Pattern A enriched-cohort emit at prime_award_unique_key grain. "
            "Prime side from usaspending.contracts_lance (latest action per prime "
            "via ROW_NUMBER dedup); subaward rollup LEFT JOIN from "
            "usaspending.contract_subawards_lance (count, total_amount, "
            "earliest/latest action date, distinct UEIs/names/POP states). "
            "BTREE on prime_award_unique_key + prime_awardee_uei + prime_naics_code. "
            "Floor 11,829,546 rows. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # Phase 1 AE platform supply substrate — Clay Find People AE/US ingest.
    # BTREE on linkedin_url_normalized, domain, latest_experience_start_date,
    # _snapshot_date (validator §6c). Per-snapshot reads go through
    # lance.dataset(uri).scanner(filter='_snapshot_date = ...') directly.
    # register_at_boot=False — per-cycle dataset; not a hot-path bulk view.
    LanceView(
        name="clay_find_people_ae_us_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/clay/find_people_ae_us_lance",
        description=(
            "Clay Find People output for US-based Account Executive roles "
            "(Phase 1 AE platform supply substrate). Pattern A direct source "
            "hydration from entities.source_clay_find_people. BTREE on "
            "linkedin_url_normalized, domain, latest_experience_start_date, "
            "_snapshot_date. PK=(linkedin_url_normalized, _snapshot_date) "
            "preserves multi-snapshot history. source_provider=clay_find_people. "
            "Per-snapshot lookups via lance.dataset(uri).scanner(filter=...) "
            "directly. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="jsearch_jobs_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/jsearch/jobs_lance",
        description=(
            "JSearch raw jobs (Pattern A from Postgres entities.source_jsearch_search "
            "via daily R2 snapshot). BTREE on job_id + employer_name_normalized + "
            "employer_domain_normalized + job_country + job_state + job_publisher. "
            "Per-publisher syndication preserved (source ingest invariant — raw stays raw; "
            "canonicalization is downstream in ae_postings_lance). Floor 1,161. "
            "NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="jsearch_pdl_employer_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/jsearch_pdl_employer_lance",
        description=(
            "JSearch employer × PDL company identity bridge (Pattern B; domain-match "
            "primary via domain_exact v1.0.0 REUSED — shared with FMCSA-PDL / SAM-PDL / "
            "UCC-PDL; name-match fallback for NULL-website rows). BTREE on job_id + "
            "pdl_id + match_method. 4-tier confidence per match_method (platinum / "
            "gold / silver / rejected). bridge_version=1.0.0. Floor 407. NOT registered "
            "at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="ae_postings_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/ae_postings_lance",
        description=(
            "AE-canonical postings (Pattern A enriched-cohort; AE-filtered "
            "jsearch.jobs_lance × jsearch_pdl_employer_lance; per-publisher dedup at "
            "(pdl_id, title, city, posted_date_day) grain — one row per canonical "
            "posting). Includes job_publishers_array + cluster_size + role_canonical "
            "(Account Executive / Enterprise AE / Mid-Market AE / SMB AE / Strategic "
            "AE / Senior AE) + seniority_band + is_active. BTREE on pdl_id + "
            "role_canonical + seniority_band + job_state + is_active. bridge_version=1.0.0. "
            "Floor 232. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # Phase 2 AE platform supply substrate — Clay Enriched Person (two datasets).
    # Both register_at_boot=False: flat dataset carries nested struct columns
    # (not DuckDB-Arrow-bridge-compatible at register time); work_history is the
    # long-form explode whose per-URL reads go through lance.dataset(uri).scanner()
    # directly. On-demand via register_lance_view_lazy.
    LanceView(
        name="clay_enriched_person_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/clay/enriched_person_lance",
        description=(
            "Clay enriched person profiles — Phase 2 AE platform supply substrate "
            "(one row per LinkedIn URL). Carries identity (linkedin_url_normalized, "
            "slug, name, profile_id), headline + summary text, current-state pointers "
            "(org, title, country, location_name, last_refresh, jobs_count, "
            "connections, num_followers), flat latest_experience.* fields, "
            "and Arrow nested struct columns for small per-person arrays "
            "(education, certifications, languages, awards, volunteering, "
            "current_experience). raw_source_row column carries the full Clay "
            "body as serialized JSON string. BTREE on linkedin_url_normalized + "
            "slug + profile_id + _snapshot_date + latest_experience_company_domain. "
            "NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="clay_enriched_person_work_history_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/clay/enriched_person_work_history_lance",
        description=(
            "Clay enriched person work history — long-form explode of experience[] "
            "(one row per (linkedin_url_normalized, experience_idx)); mirror shape of "
            "sam_gov.entity_pocs_lance (PR #468 long-form POC explode). Carries "
            "per-job: linkedin_url_normalized (FK to enriched_person_lance), "
            "experience_idx (0 = newest per Clay ordering), company, company_domain, "
            "company_url (LinkedIn company URL), org_id (bigint), title, start_date, "
            "end_date, is_current, locality, summary, _snapshot_date. BTREE on "
            "linkedin_url_normalized + company_domain + org_id + start_date + "
            "_snapshot_date. C13: every work_history row's linkedin_url_normalized "
            "matches a row in enriched_person_lance (sibling URI under "
            "polaris-warehouse/clay/). NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # Real-Time Glassdoor Data substrate v1 — 4 endpoints + bridge.
    # All register_at_boot=False — operator-fired ingest surface, per-id reads
    # go through lance.dataset(uri).scanner(filter=...) directly.
    LanceView(
        name="glassdoor_company_search_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_search_lance",
        description=(
            "Glassdoor /company-search response rows, one per (input_query, "
            "glassdoor_company_id) tuple. Per-id lookups should go through "
            "lance.dataset(uri).scanner(filter='glassdoor_company_id = ...') directly. "
            "Boot-lean: register_at_boot=False — Arrow-bridge materialization "
            "cost not justified for an operator-fired resolution surface."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="glassdoor_company_overview_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_overview_lance",
        description=(
            "Glassdoor /company-overview response rows, one per glassdoor_company_id. "
            "Per-id lookups should go through lance.dataset(uri).scanner(filter=...) "
            "directly. Boot-lean: register_at_boot=False."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="glassdoor_company_salaries_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_lance",
        description=(
            "Glassdoor /company-salaries response rows, one per (company × title × "
            "location × location_type). Per-id lookups through scanner(filter=...). "
            "Boot-lean: register_at_boot=False."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="glassdoor_company_salaries_v2_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_v2_lance",
        description=(
            "Glassdoor /company-salaries-v2 response rows (flattened salaries[] "
            "array), one per (company × job_title_id × page). Per-id lookups "
            "through scanner(filter=...). Boot-lean: register_at_boot=False."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="bridges_pdl_glassdoor_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_glassdoor_lance",
        description=(
            "PDL company × Glassdoor company_id identity bridge (domain_exact v1.0.0 "
            "REUSED; name-match fallback). One row per (pdl_id, glassdoor_company_id, "
            "match_method) tuple. Per-pdl_id lookups through scanner(filter=...). "
            "Boot-lean: register_at_boot=False."
        ),
        register_at_boot=False,
    ),
    # TXDOT Construction Letting bid-tabulations — txdot-letting-substrate (2026-05-18).
    # Source: Socrata data.texas.gov/de7b-7dna (24-month rolling, ~1.03M rows).
    # Grain: one row per (bid item × project × contractor); both winning and losing bids.
    # register_at_boot=False because 1M+ rows — per-key reads go via
    # lance.dataset(uri).scanner(filter=...) directly;
    # full materialization via register_lance_view_lazy only on demand.
    LanceView(
        name="txdot_letting_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/txstate/txdot_letting_lance",
        register_at_boot=False,
        description=(
            "TXDOT bid tabulations — one row per (bid item × project × contractor); "
            "both winning and losing bids. Lance format. ~1.03M rows (24-mo rolling). "
            "Source: Socrata data.texas.gov/de7b-7dna. Per-key reads via "
            "lance.dataset(uri).scanner(filter=...)."
        ),
    ),
    # --- SEC DERA Form D — Pattern A Lance datasets (cycle sec-dera-form-d-ingest) ---
    LanceView(
        name="sec_dera_form_d_submission_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_submission_lance",
        description=(
            "SEC DERA Form D submission table (1 row per accessionnumber). "
            "BTREE on accessionnumber. ~1.1M rows historical. "
            "R2 source: sec-dera/form-d/release=YYYYqQ/submission.parquet."
        ),
        register_at_boot=True,  # ~1.1M rows; within boot budget.
    ),
    LanceView(
        name="sec_dera_form_d_issuers_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_issuers_lance",
        description=(
            "SEC DERA Form D issuers (1+ per filing; composite key accessionnumber + "
            "issuer_seq_key). BTREE on accessionnumber + cik. ~1.2M rows historical."
        ),
        register_at_boot=True,  # ~1.2M rows.
    ),
    LanceView(
        name="sec_dera_form_d_offering_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_offering_lance",
        description=(
            "SEC DERA Form D offering payload (TOTALOFFERINGAMOUNT, TOTALAMOUNTSOLD, "
            "SALEDATE, ISEQUITYTYPE, ISDEBTTYPE — private capital inflection signal). "
            "BTREE on accessionnumber. ~1.1M rows historical."
        ),
        register_at_boot=True,  # ~1.1M rows.
    ),
    LanceView(
        name="sec_dera_form_d_recipients_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_recipients_lance",
        description=(
            "SEC DERA Form D placement-agent recipients (composite key accessionnumber + "
            "recipient_seq_key). BTREE on accessionnumber + recipientcrdnumber "
            "(Stage 6 bridge anchor to ADV via CRD). ~550K rows historical."
        ),
        register_at_boot=True,  # ~550K rows.
    ),
    LanceView(
        name="sec_dera_form_d_related_persons_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_related_persons_lance",
        description=(
            "SEC DERA Form D related persons (~3.9M rows historical; largest Form D dataset). "
            "BTREE on accessionnumber. register_at_boot=True — 3.9M rows is within the "
            "anti-pattern threshold (gleif_lei_records_lance_raw at ~3.3M is precedent; "
            "cms_open_payments_general_lance_raw at ~15.4M is False)."
        ),
        register_at_boot=True,  # ~3.9M rows — within boot budget per reviewer §special-check 6.
    ),
    LanceView(
        name="sec_dera_form_d_signatures_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/form_d_signatures_lance",
        description=(
            "SEC DERA Form D signatures. BTREE on accessionnumber. ~1.2M rows historical."
        ),
        register_at_boot=True,  # ~1.2M rows.
    ),
    # --- SEC DERA FSDS — Pattern A Lance datasets (cycle sec-dera-fsds-ingest) ---
    LanceView(
        name="sec_dera_fsds_sub_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_sub_lance",
        description=(
            "SEC DERA FSDS submission table (1 row per adsh — 10-K/10-Q filer-period). "
            "BTREE on adsh + cik. ~415K rows historical. "
            "R2 source: sec-dera/fsds/release=YYYYqQ/sub.parquet."
        ),
        register_at_boot=True,   # ~415K rows; within boot budget.
    ),
    LanceView(
        name="sec_dera_fsds_tag_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_tag_lance",
        description=(
            "SEC DERA FSDS XBRL tag dictionary (composite logical key tag + version; "
            "heavy cross-quarter overlap as filers reuse the us-gaap taxonomy). "
            "BTREE on tag. ~5M rows historical (reference-table semantics)."
        ),
        register_at_boot=True,   # ~5M rows; reference-table read pattern.
    ),
    LanceView(
        name="sec_dera_fsds_pre_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_pre_lance",
        description=(
            "SEC DERA FSDS presentation linkbase (statement+line ordering per filing). "
            "Composite key (adsh, report, line). BTREE on adsh. ~50M rows historical. "
            "register_at_boot=False — too large for boot-time DuckDB Arrow bridge per "
            "ARCHITECTURE-PATTERNS anti-pattern §\"Materialize a Pattern A Lance view "
            "via the boot-time DuckDB Arrow bridge\"."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sec_dera_fsds_num_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_dera/fsds_num_lance",
        description=(
            "SEC DERA FSDS numeric facts — every XBRL-tagged numeric value across all "
            "10-K/10-Q filings (the \"Refinancing / BDC Target Matrix\" substrate). "
            "Composite key (adsh, tag, version, ddate, qtrs, uom, coreg). BTREE on adsh + tag. "
            "~200-350M rows historical — largest Lance dataset to date. "
            "register_at_boot=False — per-key reads via lance.dataset(uri).scanner(filter=...). "
            "ARCHITECTURE-PATTERNS anti-pattern §\"Materialize a Pattern A Lance view via "
            "the boot-time DuckDB Arrow bridge\"."
        ),
        register_at_boot=False,
    ),
    # CA Cal eProcure archived (cycle ca-cal-eprocure-archived-ingest 2026-05-19).
    # Two datasets: PO historical (FY 2012-2015, one-shot, 344K rows) + NCB monthly
    # (Special Category ≥$1M, 469 rows). Both register_at_boot=True — combined
    # 344K+469 rows is well within per-process Arrow-bridge budget.
    LanceView(
        name="cal_eprocure_archived_po_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/castate/"
            "cal_eprocure_archived_po_lance"
        ),
        description=(
            "CA DGS Purchase Order Data (archived FY 2012-2015), one-shot historical "
            "backfill, 344K rows. BTREE on purchase_order_number + "
            "department_name_normalized + purchase_date_typed. Namespace: castate. "
            "Source: data.ca.gov CKAN resource bb82edc5-9c78-44e2-8947-68ece26197c5."
        ),
    ),
    LanceView(
        name="cal_eprocure_archived_ncb_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/castate/"
            "cal_eprocure_archived_ncb_lance"
        ),
        description=(
            "CA DGS Non-Competitive Bids Special Category ≥$1M, monthly refresh, "
            "~469 rows. BTREE on ncb_number + requesting_organization_normalized + "
            "approved_on_typed. Namespace: castate. "
            "Source: data.ca.gov CKAN resource 14932789-485b-481b-910a-dafb40d3471c."
        ),
    ),
    # ── Caltrans CCOP active bid solicitations (daily refresh, ~96-100 rows) ──
    # Open opportunities for CA highway/bridge construction. Lead-magnet payload
    # for equipment-financing newsletter — pairs with caltrans/awards_lance (past)
    # and cslb/licensees_lance (contractor identity by license class).
    LanceView(
        name="caltrans_ccop_active_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/caltrans/"
            "ccop_active_lance"
        ),
        description=(
            "Caltrans Contracting Opportunities Portal — active highway/bridge bid "
            "solicitations, daily refresh, ~96-100 rows. BTREE on project_id + "
            "county + license_class_normalized + bid_date_typed. Namespace: caltrans. "
            "Source: server-rendered HTML at https://ccop.dot.ca.gov/allProjects."
        ),
    ),
    # ── CSCR (Cal eProcure Event Search) events (one-shot manual ingest) ──
    # CA state-agency-wide active bid events from the public Event Search page.
    # No Modal / no Cron — operator re-downloads + re-runs ingest when needed.
    LanceView(
        name="castate_cscr_events_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/castate/"
            "cscr_events_lance"
        ),
        description=(
            "California State Contracts Register — active bid events from the "
            "public Cal eProcure Event Search Download button. BTREE on "
            "event_id + department + end_date_typed + buyer_email_normalized. "
            "Namespace: castate. One-shot manual ingest (no Cron). "
            "Source: https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx."
        ),
    ),
    # ── OPSC School Facility Program Funding (weekly refresh, ~14,200 rows) ──
    # CA DGS / Office of Public School Construction SFP funding awards by
    # district, school, program, and SAB action date. K-12 facility construction
    # lead-magnet payload — pairs with caltrans_ccop_active (current bids) and
    # cslb/licensees_lance (contractor identity by license class).
    LanceView(
        name="opsc_school_facility_funding_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/castate/"
            "opsc_school_facility_funding_lance"
        ),
        description=(
            "CA OPSC School Facility Program funding awards, weekly refresh, "
            "~14,200 rows. BTREE on application_number + county + "
            "applicant_normalized + last_sab_date_typed. Namespace: castate. "
            "Source: data.ca.gov CKAN resource 8080bb19-a63b-47e3-82d3-7451d119e27f."
        ),
    ),
    # ── AZ APP Public RFP Browse (daily refresh, ~150 rows) ──────────────────
    # Arizona Procurement Portal (Ivalua) public-RFP grid at app.az.gov. All
    # open AZ-state-agency solicitations: RFPs, IFBs, sole-source notices,
    # demo / auction listings. First entry in the `azstate` namespace.
    LanceView(
        name="az_app_rfp_public_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/azstate/"
            "app_rfp_public_lance"
        ),
        description=(
            "Arizona Procurement Portal — public RFP / IFB / sole-source "
            "solicitations, daily refresh, ~150 rows per snapshot. BTREE on "
            "code + rfp_id + agency_normalized + end_typed. Namespace: azstate. "
            "Source: Ivalua HTML grid at https://app.az.gov/page.aspx/en/rfp/request_browse_public."
        ),
    ),
    # ── FDOT SCOC active + historical contracts (one-shot manual ingest, ~1,590 rows) ──
    # FL DOT State Construction Office contract roster — every awarded FDOT
    # construction contract (active + completed) with winning contractor
    # (vendor_name + vendor_id) AND the FDOT-side Project Engineer/Manager.
    # Lead-magnet payload for the FL heavy-iron equipment-financing audience.
    # No Modal / no Cron yet — operator re-downloads + re-runs ingest when needed
    # (Cloudflare bot-protection on scoc.fdot.gov; future cycle wires automation).
    LanceView(
        name="fdot_scoc_active_contracts_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/fdot/"
            "scoc_active_contracts_lance"
        ),
        description=(
            "Florida DOT State Construction Office active + historical contracts, "
            "~1,590 rows. BTREE on contract_id + vendor_id + "
            "vendor_name_normalized + project_engineer_manager_normalized + "
            "letting_date_typed. Namespace: fdot. One-shot manual ingest (no Cron). "
            "Source: https://scoc.fdot.gov/ (Cloudflare-protected SPA Export button)."
        ),
    ),
    # ── SAM construction contractors cohort (Pattern A enriched-cohort, ~73,572 rows) ──
    # UEI-grain rollup of every SAM.gov entity registered with construction primary
    # NAICS (23xxxx), enriched with USAspending prime-contract history flags.
    # The EquipmentWork heavy-iron newsletter audience cohort. Downstream slices:
    #   is_heavy_iron_naics AND NOT has_ever_won_prime → ~13,755 UEI sub-proxy
    #   is_heavy_iron_naics AND has_ever_won_prime     → ~2,943 UEI prime cohort
    #   NOT has_ever_won_prime (any 23xxxx)            → ~61,242 UEI broad sub-proxy
    LanceView(
        name="sam_construction_contractors_cohort_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "sam_construction_contractors_lance"
        ),
        description=(
            "SAM.gov construction contractors enriched cohort, ~73,572 rows. "
            "BTREE on uei + physical_address_state + is_heavy_iron_naics + "
            "has_ever_won_prime. Namespace: bridges. Pattern A enriched-cohort "
            "emit (not an identity bridge). Build script: "
            "scripts/build_bridge_sam_construction_contractors_lance.py. "
            "Inputs: sam_gov.entities_lance + usaspending.contracts_lance."
        ),
    ),
    LanceView(
        name="sam_sos_ca_entities_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "sam_sos_ca_entities_lance"
        ),
        description=(
            "SAM × CA SoS entities bridge (Pattern B). Resolves CA-state SAM "
            "entities against sos.ca_entities_lance via "
            "legal_name_state_exact_ca. BTREE on sam_uei + sos_entity_num."
        ),
    ),
    LanceView(
        name="sam_sos_fl_entities_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "sam_sos_fl_entities_lance"
        ),
        description=(
            "SAM × FL Sunbiz entities bridge (Pattern B). Resolves FL-state "
            "SAM entities against sos.fl_entities_lance via "
            "legal_name_state_exact_fl. BTREE on sam_uei + sos_entity_num. "
            "FL mirror of bridges.sam_sos_ca_entities_lance (PR #560)."
        ),
    ),
    LanceView(
        name="ppp_sos_ca_entities_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "ppp_sos_ca_entities_lance"
        ),
        description=(
            "PPP × CA SoS entities bridge (Pattern B). Resolves CA-state PPP "
            "borrowers against sos.ca_entities_lance via "
            "legal_name_state_exact_ca. BTREE on ppp_legal_name_normalized + "
            "sos_entity_num."
        ),
    ),
    LanceView(
        name="ppp_sos_fl_entities_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "ppp_sos_fl_entities_lance"
        ),
        description=(
            "PPP × FL Sunbiz entities bridge (Pattern B). Resolves FL-state "
            "PPP borrowers against sos.fl_entities_lance via "
            "legal_name_state_exact_fl. BTREE on ppp_legal_name_normalized + "
            "sos_entity_num."
        ),
    ),
    LanceView(
        name="usaspending_sos_fl_owner_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "usaspending_sos_fl_owner_lance"
        ),
        description=(
            "USAspending × FL Sunbiz entities bridge (Pattern B). Resolves "
            "FL-recipient USAspending contractors against "
            "sos.fl_entities_lance via legal_name_state_exact_fl. BTREE on "
            "recipient_uei + sos_entity_num. Closes the SoS x {SBA, "
            "USAspending, SAM} matrix."
        ),
    ),
    LanceView(
        name="ppp_sos_ny_entities_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "ppp_sos_ny_entities_lance"
        ),
        description=(
            "PPP × NY SoS active corporations bridge (Pattern B). Resolves "
            "NY-state PPP borrowers against sos.ny_active_corporations_lance "
            "via legal_name_state_exact_ny. Carries inline ceo_name (NY CEO "
            "unlock). BTREE on ppp_legal_name_normalized + sos_dos_id."
        ),
    ),
    LanceView(
        name="sba_sos_ny_owner_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "sba_sos_ny_owner_lance"
        ),
        description=(
            "SBA × NY SoS active corporations bridge (Pattern B). Resolves "
            "NY-state SBA borrowers against sos.ny_active_corporations_lance "
            "via legal_name_state_exact_ny. Carries inline ceo_name (NY CEO "
            "unlock). BTREE on sba_legal_name_normalized + sos_dos_id."
        ),
    ),
    LanceView(
        name="sam_sos_fl_officers_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "sam_sos_fl_officers_lance"
        ),
        description=(
            "SAM × FL Sunbiz officers cohort (Pattern A enriched-cohort). "
            "Joins bridges.sam_sos_fl_entities_lance to sos.fl_officers_lance "
            "for corporate-officer identity layer on SAM-registered FL entities. "
            "FL mirror of bridges.sam_sos_ca_principals_lance (PR #561). "
            "BTREE on sam_uei + sos_entity_num + officer_full_name_normalized. "
            "Provenance: inherited entities_bridge_run_id + fresh "
            "cohort_bridge_run_id (L28)."
        ),
    ),
    # ── NY State Active Corporations (DoS Beginning 1800, ~4.2M rows) ────────
    # Daily Socrata refresh (Cron 0 14 * * * UTC). Pattern A direct Lance emit.
    # BTREE on dos_id (canonical PK) + entity_name_normalized +
    # initial_dos_filing_date_typed. register_at_boot=False — 4.2M rows exceeds
    # the per-process Arrow-bridge boot budget; per-key reads go through
    # lance.dataset(uri).scanner(filter=...) directly.
    LanceView(
        name="ny_active_corporations_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/sos/"
            "ny_active_corporations_lance"
        ),
        description=(
            "NY State Active Corporations (DoS Beginning 1800) — 4.2M-row "
            "corporate-registry Lance dataset, daily snapshot refresh. "
            "BTREE on dos_id + entity_name_normalized + initial_dos_filing_date_typed. "
            "Namespace: sos. "
            "Source: https://data.ny.gov/api/views/n9v6-gdp6."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sam_sos_ny_entities_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "sam_sos_ny_entities_lance"
        ),
        description=(
            "SAM × NY SoS Active Corporations Pattern B Lance bridge "
            "(REUSER of legal_name_state_exact_ny v1.0.0 from PR #513). "
            "Dual-BTREE on sam_uei + sos_dos_id. Output column shape mirrors "
            "sam_sos_ca_entities_lance (PR #560) + sam_sos_fl_entities_lance (PR #563). "
            "Namespace: bridges."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="usaspending_sos_ny_owner_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "usaspending_sos_ny_owner_lance"
        ),
        description=(
            "USAspending × NY SoS Active Corporations Pattern B Lance bridge "
            "(REUSER #8 of legal_name_state_exact_ny v1.0.0 from PR #513). "
            "Dual-BTREE on recipient_uei + sos_dos_id. Output column shape "
            "mirrors usaspending_sos_ca_owner_lance (PR #487) + sam_sos_ny_entities_lance "
            "(PR #569). Namespace: bridges."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="construction_opps_sized_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "construction_opps_sized_lance"
        ),
        description=(
            "SAM open construction opportunities banded by expected award size "
            "(Pattern A enriched-cohort). Open SAM construction opps (naics 23%, "
            "response_deadline>now()) enriched with an expected-award-size band "
            "derived from the USAspending construction base-award historical "
            "distribution (T2..T5 fallback hierarchy). PK notice_id; BTREE on "
            "notice_id + pop_state + naics_code + size_band. Namespace: bridges. "
            "Build script: scripts/run_sam_construction_opps_sized_emit.py."
        ),
        register_at_boot=False,
    ),
    # ── SBA PPP borrowers — Pattern A one-shot borrower-grain rollup ─────────
    # 10.2M-row PPP-only borrower-grain rollup of sba/loans_lance (11.47M PPP
    # loan rows) filtered to program='ppp'. One row per
    # (legal_name_normalized, borrstate, borrzip). BTREE on legal_name_normalized.
    # PPP FOIA is frozen (SBA last refresh 2024-09-30) — no Modal cron.
    # Prerequisite for PPP × SoS bridge cycles (ppp-bridge-batch Cycles 2-4).
    # register_at_boot=False — 10.2M rows is too large for in-process
    # materialization at FastAPI boot (anti-pattern threshold: >~4M rows).
    LanceView(
        name="ppp_borrowers_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/sba/"
            "ppp_borrowers_lance"
        ),
        description=(
            "PPP (Paycheck Protection Program) borrowers — borrower-grain "
            "rollup of sba/loans_lance filtered to program='ppp'. One row per "
            "(legal_name_normalized, borrstate, borrzip). BTREE on "
            "legal_name_normalized. Bridge input for PPP × SoS cycles "
            "(ppp-bridge-batch Cycles 2-4). PPP FOIA frozen 2024-09-30. "
            "~10.2M rows. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # openFDA Medical Device ingest — 510k + PMA + classification.
    # All 3 datasets are far below the ~3.9M-row boot budget (510k ~175K,
    # pma ~56K, classification ~7K rows); register_at_boot=True (default).
    # BTREE on canonical PKs: k_number / pma_number / product_code.
    LanceView(
        name="device_510k_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_510k_lance",
        description="openFDA Medical Device 510(k) clearances, full snapshot, Lance format. BTREE on k_number. Namespace: openfda. Source: https://api.fda.gov/download.json.",
    ),
    LanceView(
        name="device_pma_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_pma_lance",
        description="openFDA Medical Device PMA approvals (one row per (pma_number, supplement_number)), full snapshot, Lance format. BTREE on pma_number. Namespace: openfda.",
    ),
    LanceView(
        name="device_classification_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_classification_lance",
        description="openFDA Medical Device classification, full snapshot, Lance format. BTREE on product_code. Namespace: openfda.",
    ),
    LanceView(
        name="warn_notices_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/warn/notices_lance",
        description=(
            "WARN Act layoff + closure notices, all states (40 + DC), "
            "consolidated daily from Big Local News warn-transformer "
            "integrated.csv. BTREE on hash_id, company_normalized, postal_code, "
            "notice_date_typed. Namespace: warn."
        ),
    ),
    # SBIR + STTR awards — federal R&D grants to US small businesses (Phase
    # I/II, 11 agencies). One row per award, ~219K rows. Self-reported firm
    # + PI contact info from the grant proposal — the only Lance-resident
    # source with both contact_email and pi_email at firm scale.
    # Small enough to register at boot.
    LanceView(
        name="sbir_awards_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sbir/awards_lance",
        description=(
            "SBIR + STTR awards (Pattern A, award-grain). One row per federal "
            "Small Business Innovation Research / Small Business Technology "
            "Transfer award (~219K). Self-reported firm + PI contact info: "
            "company_website, contact_email, pi_email, contact_phone, pi_phone. "
            "BTREE on uei, company_website, contact_email, pi_email, "
            "agency_tracking_number. Namespace: sbir. Source: "
            "data.www.sbir.gov bulk CSV (monthly Modal cron via "
            "run_sbir_awards_r2_ingest.py)."
        ),
    ),
    # Canonical federal-recipient spine — one row per distinct UEI across
    # SAM-registered entities + USAspending prime contract recipients +
    # contract subawardees + assistance subawardees. ~896K rows. Role flags
    # + per-role activity dates + obligated totals + physical/mailing
    # addresses (raw + normalized following the address_normalize v1.0.0
    # convention). Small enough to register at boot.
    LanceView(
        name="sam_recipients_spine_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_recipients_lance",
        description=(
            "Canonical federal-recipient spine — one row per distinct UEI "
            "across the four federal-contracting populations: SAM-registered "
            "entities (sam_gov/entities_lance), prime contract recipients "
            "(usaspending/contracts_lance), contract subawardees "
            "(usaspending/contract_subawards_lance), assistance subawardees "
            "(usaspending/assistance_subawards_lance). Role flags + per-role "
            "first/last activity dates + obligated totals. Identity coalesced "
            "(SAM > contracts > subawards). Physical + mailing addresses "
            "with raw + normalized columns (physical_address_base_normalized "
            "/ _zip5 / _state_normalized; mailing same shape) matching the "
            "convention every existing SAM address bridge expects. SAM "
            "addresses pre-baked; contracts + subawards normalized at "
            "spine-build via address_normalize v1.0.0. BTREE on uei, "
            "legal_business_name_normalized, corporate_website, "
            "physical_address_base_normalized, physical_address_zip5, "
            "physical_address_state_normalized, "
            "mailing_address_base_normalized, mailing_address_zip5. "
            "Namespace: spines."
        ),
    ),
    # ClinicalTrials.gov device-intervention studies (all statuses), weekly
    # Modal refresh, latest-snapshot emit. ~87,546 rows; small enough to
    # register at boot.
    LanceView(
        name="clinicaltrials_device_studies_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/clinicaltrials/"
            "device_studies_lance"
        ),
        description=(
            "ClinicalTrials.gov device-intervention studies (all statuses) — "
            "the pre-clearance pipeline leg of the medtech regulatory-lifecycle "
            "tracker. BTREE on nct_id + lead_sponsor_name_normalized. "
            "Namespace: clinicaltrials. Source: "
            "https://clinicaltrials.gov/api/v2/studies (query.intr=device)."
        ),
    ),
    LanceView(
        name="bridges_openfda_device_pdl_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/bridges/openfda_device_pdl_lance",
        description=(
            "openFDA Medical Device applicants (510k + PMA) x PDL companies — "
            "normalized name+state exact match. Carries pdl_website per row. "
            "BTREE on applicant_name_normalized. Method=company_name_state_exact "
            "v1.0.0 (REUSED — shared with ucc_pdl); bridge_version=1.0.0. Per-row "
            "provenance: bridge_run_id, match_method, confidence_tier, "
            "openfda_fan_out, pdl_fan_out, generated_at. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="ppp_ucc_ca_debtor_lance",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
            "ppp_ucc_ca_debtor_lance"
        ),
        description=(
            "PPP × CA UCC-1 debtor bridge (Pattern B). Resolves CA-state PPP "
            "borrowers against ucc_ca debtor filings (deduped to debtor-name "
            "grain) via legal_name_state_exact_ca — the equipment-finance-lien "
            "signal. BTREE on ppp_legal_name_normalized + "
            "ucc_debtor_name_normalized."
        ),
    ),
    # SEC BDC Schedule of Investments — one row per portfolio investment per
    # filing period, union of all BDC Data Set releases. register_at_boot=False
    # — the dataset is ~450K-1M rows and the boot-time Arrow bridge fully
    # materializes it (ARCHITECTURE-PATTERNS anti-pattern); call
    # register_lance_view_lazy(con, "sec_bdc_soi_lance_raw") to load on demand.
    LanceView(
        name="sec_bdc_soi_lance_raw",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/sec_bdc/soi_lance",
        description=(
            "SEC BDC Schedule of Investments — one row per portfolio "
            "investment per filing period; typed maturity_date (DATE) "
            "from the s4 Inline-XBRL HTML parse + cleaned "
            "portfolio_company_name. BTREE on adsh, cik, maturity_date, "
            "portfolio_company_name_normalized. Namespace: sec_bdc. "
            "Source: https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets."
        ),
    ),
    # Lane WHO Phase 1 — SAM.gov historical longitudinal Lance emits.
    # Two datasets split by schema era (v2 153-col / pre-v2 131-col schemas differ
    # materially; cross-era reconciliation deferred to Phase 2).
    # Both register_at_boot=False — 10.7M and 7.7M rows respectively; per-UEI
    # time-travel reads go through lance.dataset(uri).scanner(filter=...) directly.
    # BTREE on canonical PK + snapshot_date; row identity = (PK, snapshot_date).
    # Directive: 2026-05-22-sam-historical-longitudinal-lance-emit.md
    LanceView(
        name="sam_entities_longitudinal_v2_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/"
            "entities_longitudinal_v2_lance"
        ),
        description=(
            "SAM.gov entity history — v2 era (2020-11-30..2026-05-03), 13 semiannual "
            "+ monthly snapshots, 153-col schema. One row per (unique_entity_id, "
            "snapshot_date); the longitudinal identity spine for per-UEI time-travel. "
            "BTREE on unique_entity_id + snapshot_date. ~10.7M rows. "
            "NOT registered at boot — per-UEI reads via "
            "lance.dataset(uri).scanner(filter='unique_entity_id = ...') directly."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="sam_entities_longitudinal_pre_v2_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/"
            "entities_longitudinal_pre_v2_lance"
        ),
        description=(
            "SAM.gov entity history — pre-v2 era (2014-11-30..2020-05-31), 12 "
            "semiannual snapshots, 131-col schema. One row per (cage_code, "
            "snapshot_date); PK=cage_code (DUNS 100% redacted to literal string "
            "'No longer available' across all 12 snapshots — unusable as index key). "
            "BTREE on cage_code + snapshot_date. ~7.7M rows. "
            "NOT registered at boot — per-entity reads via "
            "lance.dataset(uri).scanner(filter='cage_code = ...') directly."
        ),
        register_at_boot=False,
    ),
    # --- USAspending db-dump Phase 2 — per-table Lance datasets ---
    # Emitted from s3://dex-raw-landing-zone/usaspending/db-dump/{table}/release=2026-05-07/*.parquet
    # via scripts/run_usaspending_db_dump_lance_emit_sweep.py (cycle usaspending-db-dump-lance-emits).
    # Polaris registration is deferred; operator registers retroactively via init_polaris_lance_generic.py.
    # All 10 datasets: register_at_boot=False — large tables (awards ~180M, transaction_fpds ~108M,
    # transaction_fabs ~132M) would exceed boot-time Arrow-bridge budget; per-key reads via
    # lance.dataset(uri).scanner(filter=...) directly.
    LanceView(
        name="usaspending_awards_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_lance",
        description=(
            "USAspending db-dump awards table (~182M rows). Pattern A raw Lance emit. "
            "BTREE on award_id (PK) + generated_unique_award_id + recipient_uei. "
            "Source: usaspending/db-dump/awards/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Identity spine: recipient_uei."
        ),
    ),
    LanceView(
        name="usaspending_transaction_fpds_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fpds_lance",
        description=(
            "USAspending db-dump transaction_fpds table (~108M rows, federal contract transactions). "
            "Pattern A raw Lance emit. BTREE on transaction_id (PK) + recipient_uei + naics_code. "
            "Source: usaspending/db-dump/transaction_fpds/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Identity spine: recipient_uei."
        ),
    ),
    LanceView(
        name="usaspending_transaction_fabs_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fabs_lance",
        description=(
            "USAspending db-dump transaction_fabs table (~132M rows, federal financial assistance transactions). "
            "Pattern A raw Lance emit. BTREE on transaction_id (PK) + recipient_uei + cfda_number. "
            "Source: usaspending/db-dump/transaction_fabs/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Identity spine: recipient_uei."
        ),
    ),
    LanceView(
        name="usaspending_subaward_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/subaward_lance",
        description=(
            "USAspending db-dump subaward table (~10M rows, FSRS first-tier subaward reporting). "
            "Pattern A raw Lance emit. BTREE on sub_id (PK) + broker_subaward_id + "
            "sub_awardee_or_recipient_uei + unique_award_key + subaward_recipient_hash. "
            "Source: usaspending/db-dump/subaward/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Identity spine: sub_awardee_or_recipient_uei."
        ),
    ),
    LanceView(
        name="usaspending_recipient_lookup_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_lookup_lance",
        description=(
            "USAspending db-dump recipient_lookup table (~18M rows). "
            "Pattern A raw Lance emit. BTREE on id (PK) + uei + legal_business_name. "
            "Source: usaspending/db-dump/recipient_lookup/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Identity spine: uei."
        ),
    ),
    LanceView(
        name="usaspending_recipient_profile_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_profile_lance",
        description=(
            "USAspending db-dump recipient_profile table (~18M rows). "
            "Pattern A raw Lance emit. BTREE on recipient_hash (PK) + uei + recipient_level. "
            "Source: usaspending/db-dump/recipient_profile/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Identity spine: uei."
        ),
    ),
    LanceView(
        name="usaspending_references_cfda_lance",
        register_at_boot=True,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/references_cfda_lance",
        description=(
            "USAspending db-dump references_cfda table (~4K rows, CFDA program reference). "
            "Pattern A raw Lance emit. BTREE on id (PK) + program_number. "
            "Source: usaspending/db-dump/references_cfda/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Dim table — no UEI column."
        ),
    ),
    LanceView(
        name="usaspending_subtier_agency_lance",
        register_at_boot=True,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/subtier_agency_lance",
        description=(
            "USAspending db-dump subtier_agency table (~1.5K rows). "
            "Pattern A raw Lance emit. BTREE on subtier_agency_id (PK) + subtier_code + toptier_agency_id. "
            "Source: usaspending/db-dump/subtier_agency/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Dim table — no UEI column."
        ),
    ),
    LanceView(
        name="usaspending_toptier_agency_lance",
        register_at_boot=True,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/toptier_agency_lance",
        description=(
            "USAspending db-dump toptier_agency table (~200 rows). "
            "Pattern A raw Lance emit. BTREE on toptier_agency_id (PK) + toptier_code. "
            "Source: usaspending/db-dump/toptier_agency/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Dim table — no UEI column."
        ),
    ),
    LanceView(
        name="usaspending_agency_lance",
        register_at_boot=True,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/agency_lance",
        description=(
            "USAspending db-dump agency table (~1.5K rows). "
            "Pattern A raw Lance emit. BTREE on id (PK) + toptier_agency_id + subtier_agency_id. "
            "Source: usaspending/db-dump/agency/release=2026-05-07/*.parquet. "
            "Namespace: usaspending. Dim table — no UEI column."
        ),
    ),
    # Stale / inert entries from earlier cycles — NOT in R2 target set for this cycle.
    # References_location and transaction_normalized were absent from the db-dump;
    # these entries are kept for metadata completeness but back no data.
    LanceView(
        name="usaspending_transaction_normalized_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_normalized_lance",
        description=(
            "USAspending transaction_normalized — NOT in db-dump (consolidated into "
            "transaction_search_* upstream). Inert entry; no Lance data exists. "
            "Kept to avoid breaking any code that references this name."
        ),
    ),
    LanceView(
        name="usaspending_references_location_lance",
        register_at_boot=False,
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/references_location_lance",
        description=(
            "USAspending references_location — NOT in db-dump (split into multiple ref_* tables "
            "upstream). Inert entry; no Lance data exists. "
            "Kept to avoid breaking any code that references this name."
        ),
    ),
    # ── usaspending-derived-views-daily cycle (Phase 3) ─────────────────────
    # Four derived Lance views pre-materialized daily at 08:30 UTC by
    # usaspending_derived_views_daily_app.py. All register_at_boot=False
    # (accessed via lance.dataset(...).scanner() directly in service modules).
    LanceView(
        name="usaspending_winners_recent_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/winners_recent_lance",
        description=(
            "USAspending winners (FPDS ∪ FABS) over rolling 90-day window, "
            "SAM-entity envelope inlined (8 cols), pocs_count summary inlined. "
            "Daily refresh via usaspending_derived_views_daily_app at 08:30 UTC. "
            "BTREE on recipient_uei + action_date + naics_code + "
            "awarding_toptier_agency_name + kind. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="usaspending_awards_by_agency_month_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_by_agency_month_lance",
        description=(
            "USAspending awards rollup by (awarding_toptier_agency_name, "
            "action_month, kind). All years in source. Aggregates: count(*), "
            "sum(federal_action_obligation), count(distinct recipient_uei). "
            "Daily refresh via usaspending_derived_views_daily_app at 08:30 UTC. "
            "BTREE on awarding_toptier_agency_name + action_month. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="usaspending_awards_by_naics_month_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_by_naics_month_lance",
        description=(
            "USAspending awards rollup by (naics_code, action_month, kind). "
            "All years in source. Aggregates: count(*), sum(obligation), "
            "count(distinct recipient_uei). Daily refresh at 08:30 UTC. "
            "BTREE on naics_code + action_month. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="usaspending_awards_by_state_month_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_by_state_month_lance",
        description=(
            "USAspending awards rollup by (recipient_state, action_month, kind). "
            "All years in source. Aggregates: count(*), sum(obligation), "
            "count(distinct recipient_uei). Daily refresh at 08:30 UTC. "
            "BTREE on recipient_state + action_month. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # ── grants.gov daily open-opportunities ingest (Pattern A × 2) ──────────
    # Both register_at_boot=False — per-key reads go through
    # lance.dataset(uri).scanner(filter=opportunity_id IN (...)) directly
    # per c9 + DATA-FACTORY-ARCHITECTURE-PATTERNS §"Anti-pattern: Materialize
    # a Pattern A Lance view via the boot-time DuckDB Arrow bridge".
    # Directive: 2026-05-22-hq-all-grants-gov-daily-ingest.md
    LanceView(
        name="grants_gov_opportunity_synopsis_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/"
            "opportunity_synopsis_lance"
        ),
        description=(
            "grants.gov open opportunities — synopsis grain (posted + archived "
            "cumulative snapshot). Daily refresh from GrantsDBExtract ZIP. "
            "BTREE on opportunity_id (INT8). Floor 75,000 rows. Pipe-delimited "
            "VARCHAR for eligible_applicants and category_of_funding_activity "
            "(L54 multi-value encoding). NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    LanceView(
        name="grants_gov_opportunity_forecast_lance_raw",
        uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/"
            "opportunity_forecast_lance"
        ),
        description=(
            "grants.gov open opportunities — forecast grain (upcoming "
            "opportunities, v2 enhancement). Daily refresh from GrantsDBExtract "
            "ZIP. BTREE on opportunity_id (INT8). Floor 1,000 rows. Forecast-only "
            "date fields: estimated_synopsis_post_date, estimated_synopsis_close_date, "
            "estimated_award_date, estimated_project_start_date. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
    # FEC individual contributions — canonical transaction spine (281.7M rows,
    # 24 cycles 1980-2026, PK sub_id). register_at_boot=False — far too large
    # for in-process boot materialization; per-key reads go through
    # lance.dataset(uri).scanner(filter=...). The canonical join axis for FEC
    # person/employer bridges.
    LanceView(
        name="fec_individual_contributions_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/spines/fec_individual_contributions_lance",
        description=(
            "FEC itemized individual contributions 1980-2026 (transaction grain, "
            "PK sub_id). Source columns verbatim + parsed person-name components "
            "(first/middle/last/suffix/title/nickname) + person_key. BTREE on "
            "sub_id, cmte_id, name_last_key, name_first_key, name_normalized, "
            "employer_normalized, occupation_normalized, zip5, state, person_key, "
            "transaction_dt, cycle_year. NOT registered at boot — 281.7M rows; "
            "lookups via lance.dataset(uri).scanner(filter=...) directly."
        ),
        register_at_boot=False,
    ),
    # FEC donor rolodex — person grain (PK person_key), aggregated from the
    # transaction spine. Convenience surface; NOT the bridge join axis.
    LanceView(
        name="fec_donors_lance_raw",
        uri="s3://dex-raw-landing-zone/polaris-warehouse/spines/fec_donors_lance",
        description=(
            "FEC donor rolodex (person grain, PK person_key) — one row per "
            "individual donor aggregated from fec_individual_contributions_lance. "
            "Latest name/employer/occupation/geo + giving rollups. BTREE on "
            "person_key, name_last_key, name_first_key, state, zip5_latest, "
            "employer_normalized_latest. NOT registered at boot."
        ),
        register_at_boot=False,
    ),
]


def _lance_storage_options() -> dict:
    """S3-protocol options for Lance to talk to Cloudflare R2.

    Matches the form used by ``scripts/run_fmcsa_carrier_essentials_lance_emit.py``
    — kept in sync there because Lance's R2 access semantics are subtle.
    """
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def register_lance_views(
    con: "duckdb.DuckDBPyConnection",
    views: list[LanceView] | None = None,
    include_lazy: bool = False,
) -> list[str]:
    """Register each ``views[i]`` as a DuckDB view on ``con``.

    The view is created via ``con.register(name, arrow_table)`` where
    ``arrow_table`` is the FULL materialized table (no filter pushdown
    at registration). Callers that need per-key random access should
    call ``lance.dataset(uri).scanner(filter=...)`` directly.

    By default, only views with ``register_at_boot=True`` are registered.
    Set ``include_lazy=True`` (e.g. in a batch job) to force registration
    of the large datasets too.

    Returns the list of view names registered.
    """
    if views is None:
        views = LANCE_VIEWS
    use_native = get_settings().use_native_lance
    registered: list[str] = []
    if use_native:
        for v in views:
            if not v.register_at_boot and not include_lazy:
                LOG.info(
                    "skipping Lance view %r at boot (register_at_boot=False, "
                    "include_lazy=False) — call register_lance_view_lazy(con, %r, "
                    "filter=...) to load on demand",
                    v.name, v.name,
                )
                continue
            LOG.info(
                "registering Lance view %r ← %s (native lance_scan)",
                v.name, v.uri,
            )
            con.execute(
                f"CREATE OR REPLACE VIEW {v.name} AS "
                f"SELECT * FROM lance_scan('{v.uri}')"
            )
            registered.append(v.name)
        return registered

    # Arrow-bridge fallback (macOS-compatible). DO NOT modify.
    import lance

    storage_options = _lance_storage_options()
    for v in views:
        if not v.register_at_boot and not include_lazy:
            LOG.info(
                "skipping Lance view %r at boot (register_at_boot=False, "
                "include_lazy=False) — call register_lance_view_lazy(con, %r, "
                "filter=...) to load on demand",
                v.name, v.name,
            )
            continue
        LOG.info("registering Lance view %r ← %s", v.name, v.uri)
        ds = lance.dataset(v.uri, storage_options=storage_options)
        arrow_tbl = ds.to_table()
        con.register(v.name, arrow_tbl)
        registered.append(v.name)
    return registered


def register_lance_view_lazy(
    con: "duckdb.DuckDBPyConnection",
    name: str,
    *,
    filter: str,
    columns: list[str] | None = None,
) -> None:
    """Register a single Lance view by name on demand. REQUIRES a bounding
    Lance scanner filter.

    `register_at_boot=False` views are the massive spines (USAspending
    contracts, SBA, grants.gov forecast, etc.); a raw ``ds.to_table()`` on
    them materializes tens of millions of rows into process memory and OOMs
    the container. The filter MUST narrow at the Lance layer before Arrow
    materialization. Optional ``columns`` further trims the projection.

    Example:
        register_lance_view_lazy(
            con,
            "usaspending_contracts_lance_raw",
            filter="recipient_uei IN ('ABC123…', 'DEF456…')",
            columns=["recipient_uei", "naics_code", "total_obligated_amount"],
        )
    """
    if not filter or not str(filter).strip():
        raise ValueError(
            f"register_lance_view_lazy({name!r}): non-empty Lance filter is "
            "required — raw .to_table() on a register_at_boot=False spine "
            "is the OOM vector this guardrail exists to prevent"
        )

    view = next((v for v in LANCE_VIEWS if v.name == name), None)
    if view is None:
        raise ValueError(f"Lance view {name!r} not declared in LANCE_VIEWS")

    if get_settings().use_native_lance:
        # Native path — DuckDB lance_scan() over the R2 URI. The Lance filter
        # the caller passed is enforced by the caller's downstream WHERE
        # against this view; DuckDB's planner pushes projection + predicate
        # into lance_scan, so the per-key OOM guard is preserved.
        LOG.info(
            "registering Lance view %r on demand (uri=%s, native lance_scan, "
            "filter=%r, columns=%r — applied by caller WHERE)",
            view.name, view.uri, filter, columns,
        )
        con.execute(
            f"CREATE OR REPLACE VIEW {view.name} AS "
            f"SELECT * FROM lance_scan('{view.uri}')"
        )
        return

    # Arrow-bridge fallback (macOS-compatible). DO NOT modify.
    import lance

    storage_options = _lance_storage_options()
    LOG.info(
        "registering Lance view %r on demand (uri=%s, filter=%r, columns=%r)",
        view.name, view.uri, filter, columns,
    )
    ds = lance.dataset(view.uri, storage_options=storage_options)
    scanner_kwargs: dict = {"filter": filter}
    if columns is not None:
        scanner_kwargs["columns"] = columns
    arrow_tbl = ds.scanner(**scanner_kwargs).to_table()
    con.register(view.name, arrow_tbl)
