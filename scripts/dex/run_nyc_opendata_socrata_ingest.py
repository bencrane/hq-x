#!/usr/bin/env python3
"""NYC Open Data (Socrata) — multi-dataset ingest.

Seven datasets bundled in one runner — they share Socrata's auth,
pagination, audit, and refresh patterns.

  hpd-registrations               tesw-yqqr  HPD Multiple Dwelling Registrations
  hpd-contacts                    feu5-w2e2  HPD Registration Contacts
  hpd-violations                  wvxf-dwi5  HPD Housing Maintenance Code Violations
  dob-ecb-violations              6bgk-3dad  DOB ECB Violations
  dob-permits                     ic3t-wcy2  DOB Job Application Filings (legacy BIS — DEAD at 2020-05)
  dob-now-approved-permits        rbx6-tga4  DOB NOW: Build – Approved Permits
  dob-now-job-application-filings w9ak-ipjd  DOB NOW: Build – Job Application Filings

Auth: Basic Auth via SOCRATA_API_KEY_ID:SOCRATA_API_KEY_SECRET (Doppler).
Pagination: $limit + $offset, $order=:id, $select=*,:id,:created_at,:updated_at.
Idempotency: PK on socrata_id (Socrata :id), ON CONFLICT DO UPDATE.
Skip-if-unchanged: compare metadata.rowsUpdatedAt to prior successful run.
Audit: ops.nyc_opendata_ingest_runs.

Recon: --recon-only hits metadata + first page (no writes), then runs
analytical queries against the existing tables. Prints a per-dataset block
summarizing row count, named-firm field population, status distribution,
date range, and top contractor/owner samples — useful as a pre-promote
gate or a post-ingest sanity check.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_nyc_opendata_socrata_ingest.py hpd-registrations
  PYTHONPATH=. doppler run -- python3 scripts/run_nyc_opendata_socrata_ingest.py all
  PYTHONPATH=. doppler run -- python3 scripts/run_nyc_opendata_socrata_ingest.py hpd-violations --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_nyc_opendata_socrata_ingest.py dob-permits --dry-run --max-pages 1
  PYTHONPATH=. doppler run -- python3 scripts/run_nyc_opendata_socrata_ingest.py dob-now-approved-permits --recon-only
  PYTHONPATH=. doppler run -- python3 scripts/run_nyc_opendata_socrata_ingest.py all --recon-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import httpx
import psycopg
from psycopg.types.json import Jsonb

DEFAULT_PAGE_SIZE = 50_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
SELECT_CLAUSE = "*,:id,:created_at,:updated_at"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("nyc-opendata-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Type coercers (from raw Socrata JSON values)
# --------------------------------------------------------------------------- #


def coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coerce_numeric(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def coerce_date(value: Any) -> str | None:
    """ISO 8601 calendar_date string (e.g. '2026-03-19T00:00:00.000') -> date."""
    if value is None or value == "":
        return None
    return str(value)[:10]


def coerce_tstz(value: Any) -> datetime | None:
    """Socrata ISO 8601 timestamp -> datetime."""
    if value is None or value == "":
        return None
    s = str(value)
    cleaned = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _ts_from_unix(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Per-dataset configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColSpec:
    name: str           # Socrata field name == Postgres column name
    pg_type: str        # Postgres type for the temp staging table
    coerce: Callable[[Any], Any]


@dataclass(frozen=True)
class DatasetConfig:
    key: str            # CLI subcommand
    fourxfour: str
    name: str           # Human-readable
    schema: str         # 'entities'
    table: str
    cols: list[ColSpec] = field(default_factory=list)
    # change_cols: list of natural-key & high-cardinality cols whose change
    # triggers an UPDATE. Empty = always update on conflict.
    change_cols: list[str] = field(default_factory=list)
    # Recon-report hints (consumed by --recon-only). Optional; datasets
    # without these set still get a basic recon block (row count + date
    # range from socrata_updated_at).
    contractor_col: str | None = None        # primary "named firm" column
    secondary_contractor_col: str | None = None
    owner_col: str | None = None             # property-owner business name (separate from contractor)
    license_type_col: str | None = None      # GC/P/F/etc. classifier
    status_col: str | None = None            # filing/permit status
    date_col: str | None = None              # primary time-window column
    natural_key_col: str | None = None       # job filing number / similar (for repro IDs)

    @property
    def resource_url(self) -> str:
        return f"https://data.cityofnewyork.us/resource/{self.fourxfour}.json"

    @property
    def metadata_url(self) -> str:
        return f"https://data.cityofnewyork.us/api/views/{self.fourxfour}.json"

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"


# --------------------------------------------------------------------------- #
# Dataset definitions
# --------------------------------------------------------------------------- #

HPD_REGISTRATIONS = DatasetConfig(
    key="hpd-registrations",
    fourxfour="tesw-yqqr",
    name="HPD Multiple Dwelling Registrations",
    schema="entities",
    table="hpd_registrations",
    cols=[
        ColSpec("registrationid",       "bigint",  coerce_int),
        ColSpec("buildingid",           "bigint",  coerce_int),
        ColSpec("boroid",               "smallint", coerce_int),
        ColSpec("boro",                 "text",    coerce_text),
        ColSpec("housenumber",          "text",    coerce_text),
        ColSpec("lowhousenumber",       "text",    coerce_text),
        ColSpec("highhousenumber",      "text",    coerce_text),
        ColSpec("streetname",           "text",    coerce_text),
        ColSpec("streetcode",           "int",     coerce_int),
        ColSpec("zip",                  "text",    coerce_text),
        ColSpec("block",                "int",     coerce_int),
        ColSpec("lot",                  "int",     coerce_int),
        ColSpec("bin",                  "bigint",  coerce_int),
        ColSpec("communityboard",       "smallint", coerce_int),
        ColSpec("lastregistrationdate", "date",    coerce_date),
        ColSpec("registrationenddate",  "date",    coerce_date),
    ],
)

HPD_CONTACTS = DatasetConfig(
    key="hpd-contacts",
    fourxfour="feu5-w2e2",
    name="HPD Registration Contacts",
    schema="entities",
    table="hpd_registration_contacts",
    cols=[
        ColSpec("registrationcontactid", "bigint", coerce_int),
        ColSpec("registrationid",        "bigint", coerce_int),
        ColSpec("type",                  "text",   coerce_text),
        ColSpec("contactdescription",    "text",   coerce_text),
        ColSpec("corporationname",       "text",   coerce_text),
        ColSpec("title",                 "text",   coerce_text),
        ColSpec("firstname",             "text",   coerce_text),
        ColSpec("middleinitial",         "text",   coerce_text),
        ColSpec("lastname",              "text",   coerce_text),
        ColSpec("businesshousenumber",   "text",   coerce_text),
        ColSpec("businessstreetname",    "text",   coerce_text),
        ColSpec("businessapartment",     "text",   coerce_text),
        ColSpec("businesscity",          "text",   coerce_text),
        ColSpec("businessstate",         "text",   coerce_text),
        ColSpec("businesszip",           "text",   coerce_text),
    ],
)

HPD_VIOLATIONS = DatasetConfig(
    key="hpd-violations",
    fourxfour="wvxf-dwi5",
    name="HPD Housing Maintenance Code Violations",
    schema="entities",
    table="hpd_violations",
    cols=[
        ColSpec("violationid",            "bigint", coerce_int),
        ColSpec("buildingid",             "bigint", coerce_int),
        ColSpec("registrationid",         "bigint", coerce_int),
        ColSpec("boroid",                 "smallint", coerce_int),
        ColSpec("boro",                   "text",   coerce_text),
        ColSpec("housenumber",            "text",   coerce_text),
        ColSpec("lowhousenumber",         "text",   coerce_text),
        ColSpec("highhousenumber",        "text",   coerce_text),
        ColSpec("streetname",             "text",   coerce_text),
        ColSpec("streetcode",             "text",   coerce_text),
        ColSpec("zip",                    "text",   coerce_text),
        ColSpec("apartment",              "text",   coerce_text),
        ColSpec("story",                  "text",   coerce_text),
        ColSpec("block",                  "int",    coerce_int),
        ColSpec("lot",                    "int",    coerce_int),
        ColSpec("class",                  "text",   coerce_text),
        ColSpec("inspectiondate",         "date",   coerce_date),
        ColSpec("approveddate",           "date",   coerce_date),
        ColSpec("originalcertifybydate",  "date",   coerce_date),
        ColSpec("originalcorrectbydate",  "date",   coerce_date),
        ColSpec("newcertifybydate",       "date",   coerce_date),
        ColSpec("newcorrectbydate",       "date",   coerce_date),
        ColSpec("certifieddate",          "date",   coerce_date),
        ColSpec("ordernumber",            "text",   coerce_text),
        ColSpec("novid",                  "bigint", coerce_int),
        ColSpec("novdescription",         "text",   coerce_text),
        ColSpec("novissueddate",          "date",   coerce_date),
        ColSpec("currentstatusid",        "smallint", coerce_int),
        ColSpec("currentstatus",          "text",   coerce_text),
        ColSpec("currentstatusdate",      "date",   coerce_date),
        ColSpec("novtype",                "text",   coerce_text),
        ColSpec("violationstatus",        "text",   coerce_text),
        ColSpec("rentimpairing",          "text",   coerce_text),
        ColSpec("latitude",               "text",   coerce_text),
        ColSpec("longitude",              "text",   coerce_text),
        ColSpec("communityboard",         "text",   coerce_text),
        ColSpec("councildistrict",        "text",   coerce_text),
        ColSpec("censustract",            "text",   coerce_text),
        ColSpec("bin",                    "text",   coerce_text),
        ColSpec("bbl",                    "text",   coerce_text),
        ColSpec("nta",                    "text",   coerce_text),
    ],
)

DOB_ECB_VIOLATIONS = DatasetConfig(
    key="dob-ecb-violations",
    fourxfour="6bgk-3dad",
    name="DOB ECB Violations",
    schema="entities",
    table="dob_ecb_violations",
    cols=[
        ColSpec("isn_dob_bis_extract",       "text",          coerce_text),
        ColSpec("ecb_violation_number",      "text",          coerce_text),
        ColSpec("ecb_violation_status",      "text",          coerce_text),
        ColSpec("dob_violation_number",      "text",          coerce_text),
        ColSpec("bin",                       "text",          coerce_text),
        ColSpec("boro",                      "text",          coerce_text),
        ColSpec("block",                     "text",          coerce_text),
        ColSpec("lot",                       "text",          coerce_text),
        ColSpec("hearing_date",              "text",          coerce_text),
        ColSpec("hearing_time",              "text",          coerce_text),
        ColSpec("served_date",               "text",          coerce_text),
        ColSpec("issue_date",                "text",          coerce_text),
        ColSpec("severity",                  "text",          coerce_text),
        ColSpec("violation_type",            "text",          coerce_text),
        ColSpec("respondent_name",           "text",          coerce_text),
        ColSpec("respondent_house_number",   "text",          coerce_text),
        ColSpec("respondent_street",         "text",          coerce_text),
        ColSpec("respondent_city",           "text",          coerce_text),
        ColSpec("respondent_zip",            "text",          coerce_text),
        ColSpec("violation_description",     "text",          coerce_text),
        ColSpec("penality_imposed",          "numeric(14,2)", coerce_numeric),
        ColSpec("amount_paid",               "numeric(14,2)", coerce_numeric),
        ColSpec("balance_due",               "numeric(14,2)", coerce_numeric),
        ColSpec("infraction_code1",          "text",          coerce_text),
        ColSpec("section_law_description1",  "text",          coerce_text),
        ColSpec("infraction_code2",          "text",          coerce_text),
        ColSpec("section_law_description2",  "text",          coerce_text),
        ColSpec("infraction_code3",          "text",          coerce_text),
        ColSpec("section_law_description3",  "text",          coerce_text),
        ColSpec("infraction_code4",          "text",          coerce_text),
        ColSpec("section_law_description4",  "text",          coerce_text),
        ColSpec("infraction_code5",          "text",          coerce_text),
        ColSpec("section_law_description5",  "text",          coerce_text),
        ColSpec("infraction_code6",          "text",          coerce_text),
        ColSpec("section_law_description6",  "text",          coerce_text),
        ColSpec("infraction_code7",          "text",          coerce_text),
        ColSpec("section_law_description7",  "text",          coerce_text),
        ColSpec("infraction_code8",          "text",          coerce_text),
        ColSpec("section_law_description8",  "text",          coerce_text),
        ColSpec("infraction_code9",          "text",          coerce_text),
        ColSpec("section_law_description9",  "text",          coerce_text),
        ColSpec("infraction_code10",         "text",          coerce_text),
        ColSpec("section_law_description10", "text",          coerce_text),
        ColSpec("aggravated_level",          "text",          coerce_text),
        ColSpec("hearing_status",            "text",          coerce_text),
        ColSpec("certification_status",      "text",          coerce_text),
    ],
)

DOB_PERMITS = DatasetConfig(
    key="dob-permits",
    fourxfour="ic3t-wcy2",
    name="DOB Job Application Filings",
    schema="entities",
    table="dob_job_application_filings",
    cols=[
        ColSpec("job__",                          "text",    coerce_text),
        ColSpec("doc__",                          "text",    coerce_text),
        ColSpec("borough",                        "text",    coerce_text),
        ColSpec("house__",                        "text",    coerce_text),
        ColSpec("street_name",                    "text",    coerce_text),
        ColSpec("block",                          "text",    coerce_text),
        ColSpec("lot",                            "text",    coerce_text),
        ColSpec("bin__",                          "text",    coerce_text),
        ColSpec("job_type",                       "text",    coerce_text),
        ColSpec("job_status",                     "text",    coerce_text),
        ColSpec("job_status_descrp",              "text",    coerce_text),
        ColSpec("latest_action_date",             "text",    coerce_text),
        ColSpec("building_type",                  "text",    coerce_text),
        ColSpec("community___board",              "text",    coerce_text),
        ColSpec("cluster",                        "text",    coerce_text),
        ColSpec("landmarked",                     "text",    coerce_text),
        ColSpec("adult_estab",                    "text",    coerce_text),
        ColSpec("loft_board",                     "text",    coerce_text),
        ColSpec("city_owned",                     "text",    coerce_text),
        ColSpec("little_e",                       "text",    coerce_text),
        ColSpec("pc_filed",                       "text",    coerce_text),
        ColSpec("efiling_filed",                  "text",    coerce_text),
        ColSpec("plumbing",                       "text",    coerce_text),
        ColSpec("mechanical",                     "text",    coerce_text),
        ColSpec("boiler",                         "text",    coerce_text),
        ColSpec("fuel_burning",                   "text",    coerce_text),
        ColSpec("fuel_storage",                   "text",    coerce_text),
        ColSpec("standpipe",                      "text",    coerce_text),
        ColSpec("sprinkler",                      "text",    coerce_text),
        ColSpec("fire_alarm",                     "text",    coerce_text),
        ColSpec("equipment",                      "text",    coerce_text),
        ColSpec("fire_suppression",               "text",    coerce_text),
        ColSpec("curb_cut",                       "text",    coerce_text),
        ColSpec("other",                          "text",    coerce_text),
        ColSpec("other_description",              "text",    coerce_text),
        ColSpec("applicant_s_first_name",         "text",    coerce_text),
        ColSpec("applicant_s_last_name",          "text",    coerce_text),
        ColSpec("applicant_professional_title",   "text",    coerce_text),
        ColSpec("applicant_license__",            "text",    coerce_text),
        ColSpec("professional_cert",              "text",    coerce_text),
        ColSpec("pre__filing_date",               "text",    coerce_text),
        ColSpec("paid",                           "text",    coerce_text),
        ColSpec("fully_paid",                     "text",    coerce_text),
        ColSpec("assigned",                       "text",    coerce_text),
        ColSpec("approved",                       "text",    coerce_text),
        ColSpec("fully_permitted",                "text",    coerce_text),
        ColSpec("initial_cost",                   "text",    coerce_text),
        ColSpec("total_est__fee",                 "text",    coerce_text),
        ColSpec("fee_status",                     "text",    coerce_text),
        ColSpec("existing_zoning_sqft",           "numeric", coerce_numeric),
        ColSpec("proposed_zoning_sqft",           "numeric", coerce_numeric),
        ColSpec("horizontal_enlrgmt",             "text",    coerce_text),
        ColSpec("vertical_enlrgmt",               "text",    coerce_text),
        ColSpec("enlargement_sq_footage",         "numeric", coerce_numeric),
        ColSpec("street_frontage",                "numeric", coerce_numeric),
        ColSpec("existingno_of_stories",          "numeric", coerce_numeric),
        ColSpec("proposed_no_of_stories",         "numeric", coerce_numeric),
        ColSpec("existing_height",                "numeric", coerce_numeric),
        ColSpec("proposed_height",                "numeric", coerce_numeric),
        ColSpec("existing_dwelling_units",        "text",    coerce_text),
        ColSpec("proposed_dwelling_units",        "text",    coerce_text),
        ColSpec("existing_occupancy",             "text",    coerce_text),
        ColSpec("proposed_occupancy",             "text",    coerce_text),
        ColSpec("site_fill",                      "text",    coerce_text),
        ColSpec("zoning_dist1",                   "text",    coerce_text),
        ColSpec("zoning_dist2",                   "text",    coerce_text),
        ColSpec("zoning_dist3",                   "text",    coerce_text),
        ColSpec("special_district_1",             "text",    coerce_text),
        ColSpec("special_district_2",             "text",    coerce_text),
        ColSpec("owner_type",                     "text",    coerce_text),
        ColSpec("non_profit",                     "text",    coerce_text),
        ColSpec("owner_s_first_name",             "text",    coerce_text),
        ColSpec("owner_s_last_name",              "text",    coerce_text),
        ColSpec("owner_s_business_name",          "text",    coerce_text),
        ColSpec("owner_s_house_number",           "text",    coerce_text),
        ColSpec("owner_shouse_street_name",       "text",    coerce_text),
        ColSpec("city_",                          "text",    coerce_text),
        ColSpec("state",                          "text",    coerce_text),
        ColSpec("zip",                            "text",    coerce_text),
        ColSpec("owner_sphone__",                 "text",    coerce_text),
        ColSpec("job_description",                "text",    coerce_text),
        ColSpec("dobrundate",                     "text",    coerce_text),
        ColSpec("job_s1_no",                      "text",    coerce_text),
        ColSpec("total_construction_floor_area",  "text",    coerce_text),
        ColSpec("withdrawal_flag",                "text",    coerce_text),
        ColSpec("signoff_date",                   "text",    coerce_text),
        ColSpec("special_action_status",          "text",    coerce_text),
        ColSpec("special_action_date",            "text",    coerce_text),
        ColSpec("building_class",                 "text",    coerce_text),
        ColSpec("job_no_good_count",              "text",    coerce_text),
        ColSpec("gis_latitude",                   "text",    coerce_text),
        ColSpec("gis_longitude",                  "text",    coerce_text),
        ColSpec("gis_council_district",           "text",    coerce_text),
        ColSpec("gis_census_tract",               "text",    coerce_text),
        ColSpec("gis_nta_name",                   "text",    coerce_text),
        ColSpec("gis_bin",                        "text",    coerce_text),
    ],
)

DOB_NOW_APPROVED_PERMITS = DatasetConfig(
    key="dob-now-approved-permits",
    fourxfour="rbx6-tga4",
    name="DOB NOW: Build – Approved Permits",
    schema="entities",
    table="source_dob_now_approved_permits",
    contractor_col="applicant_business_name",
    secondary_contractor_col="filing_representative_business_name",
    owner_col="owner_business_name",
    license_type_col="permittee_s_license_type",
    status_col="permit_status",
    date_col="issued_date",
    natural_key_col="job_filing_number",
    cols=[
        ColSpec("job_filing_number",                    "text", coerce_text),
        ColSpec("work_permit",                          "text", coerce_text),
        ColSpec("sequence_number",                      "text", coerce_text),
        ColSpec("filing_reason",                        "text", coerce_text),
        ColSpec("house_no",                             "text", coerce_text),
        ColSpec("street_name",                          "text", coerce_text),
        ColSpec("borough",                              "text", coerce_text),
        ColSpec("lot",                                  "text", coerce_text),
        ColSpec("bin",                                  "text", coerce_text),
        ColSpec("block",                                "text", coerce_text),
        ColSpec("c_b_no",                               "text", coerce_text),
        ColSpec("apt_condo_no_s",                       "text", coerce_text),
        ColSpec("work_on_floor",                        "text", coerce_text),
        ColSpec("work_type",                            "text", coerce_text),
        ColSpec("permittee_s_license_type",             "text", coerce_text),
        ColSpec("applicant_license",                    "text", coerce_text),
        ColSpec("applicant_first_name",                 "text", coerce_text),
        ColSpec("applicant_middle_name",                "text", coerce_text),
        ColSpec("applicant_last_name",                  "text", coerce_text),
        ColSpec("applicant_business_name",              "text", coerce_text),
        ColSpec("applicant_business_address",           "text", coerce_text),
        ColSpec("filing_representative_first_name",     "text", coerce_text),
        ColSpec("filing_representative_middle_initial", "text", coerce_text),
        ColSpec("filing_representative_last_name",      "text", coerce_text),
        ColSpec("filing_representative_business_name",  "text", coerce_text),
        ColSpec("approved_date",                        "date", coerce_date),
        ColSpec("issued_date",                          "date", coerce_date),
        ColSpec("expired_date",                         "date", coerce_date),
        ColSpec("job_description",                      "text", coerce_text),
        ColSpec("estimated_job_costs",                  "text", coerce_text),
        ColSpec("owner_business_name",                  "text", coerce_text),
        ColSpec("owner_name",                           "text", coerce_text),
        # owner_street_address/city/state/zip_code are present in the source
        # schema but redacted (0 non-null in the live feed). Kept in the
        # ingest list for forward-compat if DOB un-redacts.
        ColSpec("owner_street_address",                 "text", coerce_text),
        ColSpec("owner_city",                           "text", coerce_text),
        ColSpec("owner_state",                          "text", coerce_text),
        ColSpec("owner_zip_code",                       "text", coerce_text),
        ColSpec("permit_status",                        "text", coerce_text),
        ColSpec("tracking_number",                      "text", coerce_text),
        ColSpec("zip_code",                             "text", coerce_text),
        # Stored as text to match the existing NYC ingest pattern (preserves
        # precision; keeps joins simple even though Socrata declares numeric).
        ColSpec("latitude",                             "text", coerce_text),
        ColSpec("longitude",                            "text", coerce_text),
        ColSpec("community_board",                      "text", coerce_text),
        ColSpec("council_district",                     "text", coerce_text),
        ColSpec("bbl",                                  "text", coerce_text),
        ColSpec("census_tract",                         "text", coerce_text),
        ColSpec("nta",                                  "text", coerce_text),
    ],
)

DOB_NOW_JOB_APPLICATION_FILINGS = DatasetConfig(
    key="dob-now-job-application-filings",
    fourxfour="w9ak-ipjd",
    name="DOB NOW: Build – Job Application Filings",
    schema="entities",
    table="source_dob_now_job_application_filings",
    # No applicant_business_name on this feed — owner is the primary firm
    # signal, with filing_representative as secondary.
    contractor_col="owner_s_business_name",
    secondary_contractor_col="filing_representative_business_name",
    owner_col="owner_s_business_name",
    license_type_col="applicant_professional_title",
    status_col="filing_status",
    date_col="filing_date",
    natural_key_col="job_filing_number",
    cols=[
        ColSpec("job_filing_number",                        "text", coerce_text),
        ColSpec("filing_status",                            "text", coerce_text),
        ColSpec("house_no",                                 "text", coerce_text),
        ColSpec("street_name",                              "text", coerce_text),
        ColSpec("borough",                                  "text", coerce_text),
        ColSpec("block",                                    "text", coerce_text),
        ColSpec("lot",                                      "text", coerce_text),
        ColSpec("bin",                                      "text", coerce_text),
        # source typo: triple-m
        ColSpec("commmunity_board",                         "text", coerce_text),
        ColSpec("work_on_floor",                            "text", coerce_text),
        ColSpec("apt_condo_no_s",                           "text", coerce_text),
        ColSpec("applicant_professional_title",             "text", coerce_text),
        ColSpec("applicant_license",                        "text", coerce_text),
        ColSpec("applicant_first_name",                     "text", coerce_text),
        # source typo: plural-s
        ColSpec("applicants_middle_initial",                "text", coerce_text),
        ColSpec("applicant_last_name",                      "text", coerce_text),
        ColSpec("owner_s_business_name",                    "text", coerce_text),
        ColSpec("owner_s_street_name",                      "text", coerce_text),
        ColSpec("city",                                     "text", coerce_text),
        ColSpec("state",                                    "text", coerce_text),
        ColSpec("zip",                                      "text", coerce_text),
        ColSpec("filing_representative_first_name",         "text", coerce_text),
        ColSpec("filing_representative_middle_initial",     "text", coerce_text),
        ColSpec("filing_representative_last_name",          "text", coerce_text),
        ColSpec("filing_representative_business_name",      "text", coerce_text),
        ColSpec("filing_representative_street_name",        "text", coerce_text),
        ColSpec("filing_representative_city",               "text", coerce_text),
        ColSpec("filing_representative_state",              "text", coerce_text),
        ColSpec("filing_representative_zip",                "text", coerce_text),
        ColSpec("sprinkler_work_type",                      "text", coerce_text),
        ColSpec("plumbing_work_type",                       "text", coerce_text),
        ColSpec("initial_cost",                             "text", coerce_text),
        ColSpec("total_construction_floor_area",            "text", coerce_text),
        ColSpec("review_building_code",                     "text", coerce_text),
        ColSpec("little_e",                                 "text", coerce_text),
        ColSpec("unmapped_cco_street",                      "text", coerce_text),
        ColSpec("request_legalization",                     "text", coerce_text),
        ColSpec("includes_permanent_removal",               "text", coerce_text),
        ColSpec("in_compliance_with_nycecc",                "text", coerce_text),
        ColSpec("exempt_from_nycecc",                       "text", coerce_text),
        ColSpec("building_type",                            "text", coerce_text),
        ColSpec("existing_stories",                         "text", coerce_text),
        ColSpec("existing_height",                          "text", coerce_text),
        ColSpec("existing_dwelling_units",                  "text", coerce_text),
        ColSpec("proposed_no_of_stories",                   "text", coerce_text),
        ColSpec("proposed_height",                          "text", coerce_text),
        ColSpec("proposed_dwelling_units",                  "text", coerce_text),
        ColSpec("specialinspectionrequirement",             "text", coerce_text),
        ColSpec("special_inspection_agency_number",         "text", coerce_text),
        ColSpec("progressinspectionrequirement",            "text", coerce_text),
        ColSpec("built_1_information_value",                "text", coerce_text),
        ColSpec("built_2_information_value",                "text", coerce_text),
        ColSpec("built_2_a_information_value",              "text", coerce_text),
        ColSpec("built_2_b_information_value",              "text", coerce_text),
        ColSpec("standpipe",                                "text", coerce_text),
        ColSpec("antenna",                                  "text", coerce_text),
        ColSpec("curb_cut",                                 "text", coerce_text),
        ColSpec("sign",                                     "text", coerce_text),
        ColSpec("fence",                                    "text", coerce_text),
        ColSpec("scaffold",                                 "text", coerce_text),
        ColSpec("shed",                                     "text", coerce_text),
        ColSpec("postcode",                                 "text", coerce_text),
        ColSpec("latitude",                                 "text", coerce_text),
        ColSpec("longitude",                                "text", coerce_text),
        ColSpec("council_district",                         "text", coerce_text),
        ColSpec("census_tract",                             "text", coerce_text),
        ColSpec("bbl",                                      "text", coerce_text),
        ColSpec("nta",                                      "text", coerce_text),
        ColSpec("filing_date",                              "date", coerce_date),
        ColSpec("current_status_date",                      "date", coerce_date),
        ColSpec("first_permit_date",                        "date", coerce_date),
        # source typo: trailing underscore on 11 *_work_type_ flag columns
        ColSpec("boiler_equipment_work_type_",              "text", coerce_text),
        ColSpec("earth_work_work_type_",                    "text", coerce_text),
        ColSpec("foundation_work_type_",                    "text", coerce_text),
        ColSpec("general_construction_work_type_",          "text", coerce_text),
        ColSpec("mechanical_systems_work_type_",            "text", coerce_text),
        ColSpec("place_of_assembly_work_type_",             "text", coerce_text),
        ColSpec("protection_mechanical_methods_work_type_", "text", coerce_text),
        ColSpec("sidewalk_shed_work_type_",                 "text", coerce_text),
        ColSpec("structural_work_type_",                    "text", coerce_text),
        ColSpec("support_of_excavation_work_type_",         "text", coerce_text),
        ColSpec("temporary_place_of_assembly_work_type_",   "text", coerce_text),
        ColSpec("job_type",                                 "text", coerce_text),
        ColSpec("approved_date",                            "date", coerce_date),
        ColSpec("signoff_date",                             "date", coerce_date),
    ],
)

DATASETS: dict[str, DatasetConfig] = {
    ds.key: ds for ds in (
        HPD_REGISTRATIONS,
        HPD_CONTACTS,
        HPD_VIOLATIONS,
        DOB_ECB_VIOLATIONS,
        DOB_PERMITS,
        DOB_NOW_APPROVED_PERMITS,
        DOB_NOW_JOB_APPLICATION_FILINGS,
    )
}


# --------------------------------------------------------------------------- #
# Auth + DB helpers
# --------------------------------------------------------------------------- #


def _resolve_auth() -> tuple[httpx.Auth | None, dict[str, str], str]:
    key_id = os.environ.get("SOCRATA_API_KEY_ID")
    key_secret = os.environ.get("SOCRATA_API_KEY_SECRET")
    app_token = os.environ.get("SOCRATA_API_KEY")
    if key_id and key_secret:
        return httpx.BasicAuth(key_id, key_secret), {}, "basic"
    if app_token:
        return None, {"X-App-Token": app_token}, "app_token"
    raise RuntimeError(
        "Neither SOCRATA_API_KEY_ID/SOCRATA_API_KEY_SECRET nor SOCRATA_API_KEY "
        "is set. Cannot authenticate to Socrata."
    )


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED is not set in the environment.")
    return url


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def fetch_metadata(client: httpx.Client, ds: DatasetConfig) -> dict[str, Any]:
    r = client.get(ds.metadata_url, timeout=30.0)
    r.raise_for_status()
    return r.json()


def fetch_page(
    client: httpx.Client,
    ds: DatasetConfig,
    *,
    page_size: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    params = {
        "$limit": str(page_size),
        "$offset": str(offset),
        "$order": ":id",
        "$select": SELECT_CLAUSE,
    }
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(ds.resource_url, params=params, timeout=180.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("[%s] HTTP %s; retry in %ss (%s/%s)",
                            ds.key, r.status_code, wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json(), len(r.content)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("[%s] page fetch error (%s); retry in %ss (%s/%s)",
                        ds.key, exc, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
    raise RuntimeError(
        f"Failed to fetch page after {MAX_RETRIES} retries; last: {last_exc}"
    )


# --------------------------------------------------------------------------- #
# SQL builders (per-dataset, derived from ColSpecs)
# --------------------------------------------------------------------------- #


def stage_create_sql(ds: DatasetConfig) -> str:
    cols = ",\n  ".join(f"{c.name} {c.pg_type}" for c in ds.cols)
    return f"""
CREATE TEMP TABLE IF NOT EXISTS {ds.stage_table} (
  {cols},
  socrata_id              text,
  socrata_created_at      timestamptz,
  socrata_updated_at      timestamptz,
  dataset_rows_updated_at timestamptz
);
"""


def copy_sql(ds: DatasetConfig) -> str:
    cols = [c.name for c in ds.cols] + [
        "socrata_id", "socrata_created_at", "socrata_updated_at",
        "dataset_rows_updated_at",
    ]
    return f"COPY {ds.stage_table} ({', '.join(cols)}) FROM STDIN"


def upsert_sql(ds: DatasetConfig) -> str:
    natural_cols = [c.name for c in ds.cols]
    target_cols = natural_cols + [
        "socrata_id", "socrata_created_at", "socrata_updated_at",
        "dataset_rows_updated_at", "ingested_at",
    ]
    select_cols = natural_cols + [
        "socrata_id", "socrata_created_at", "socrata_updated_at",
        "dataset_rows_updated_at", "now()",
    ]
    update_assigns = ",\n      ".join(
        f"{c} = EXCLUDED.{c}" for c in natural_cols
    )
    update_assigns += ",\n      socrata_created_at = EXCLUDED.socrata_created_at"
    update_assigns += ",\n      socrata_updated_at = EXCLUDED.socrata_updated_at"
    update_assigns += ",\n      dataset_rows_updated_at = EXCLUDED.dataset_rows_updated_at"
    update_assigns += ",\n      ingested_at = now()"

    where_clause = " OR ".join(
        f"{ds.fully_qualified}.{c} IS DISTINCT FROM EXCLUDED.{c}"
        for c in (natural_cols + ["socrata_updated_at"])
    )

    return f"""
WITH upserted AS (
  INSERT INTO {ds.fully_qualified} ({', '.join(target_cols)})
  SELECT {', '.join(select_cols)}
    FROM {ds.stage_table}
   ON CONFLICT (socrata_id) DO UPDATE SET
      {update_assigns}
   WHERE {where_clause}
   RETURNING (xmax = 0) AS inserted
)
SELECT
  count(*) FILTER (WHERE inserted)     AS rows_inserted,
  count(*) FILTER (WHERE NOT inserted) AS rows_updated
FROM upserted;
"""


def truncate_stage_sql(ds: DatasetConfig) -> str:
    return f"TRUNCATE {ds.stage_table};"


def row_to_tuple(
    ds: DatasetConfig,
    row: dict[str, Any],
    dataset_rows_updated_at: datetime | None,
) -> tuple[Any, ...]:
    values = tuple(c.coerce(row.get(c.name)) for c in ds.cols)
    return values + (
        row.get(":id"),
        coerce_tstz(row.get(":created_at")),
        coerce_tstz(row.get(":updated_at")),
        dataset_rows_updated_at,
    )


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    ds: DatasetConfig,
    *,
    auth_method: str,
    page_size: int,
    metadata: dict[str, Any],
    prior_rows_updated_at: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.nyc_opendata_ingest_runs (
        dataset_4x4, dataset_name, status, source_url, auth_method, page_size,
        dataset_rows_updated_at, dataset_rows_created_at,
        dataset_view_last_modified, prior_dataset_rows_updated_at
    ) VALUES (%s, %s, 'running', %s, %s, %s, %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            ds.fourxfour, ds.name, ds.resource_url, auth_method, page_size,
            _ts_from_unix(metadata.get("rowsUpdatedAt")),
            _ts_from_unix(metadata.get("rowsCreatedAt")),
            _ts_from_unix(metadata.get("viewLastModified")),
            prior_rows_updated_at,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_rows_updated_at(
    conn: psycopg.Connection, ds: DatasetConfig
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dataset_rows_updated_at
              FROM ops.nyc_opendata_ingest_runs
             WHERE dataset_4x4 = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (ds.fourxfour,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    ds: DatasetConfig,
    *,
    auth_method: str,
    page_size: int,
    metadata: dict[str, Any],
    prior_rows_updated_at: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.nyc_opendata_ingest_runs (
                dataset_4x4, dataset_name, status, source_url, auth_method, page_size,
                dataset_rows_updated_at, dataset_rows_created_at,
                dataset_view_last_modified, prior_dataset_rows_updated_at,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s);
            """, (
            ds.fourxfour, ds.name, ds.resource_url, auth_method, page_size,
            _ts_from_unix(metadata.get("rowsUpdatedAt")),
            _ts_from_unix(metadata.get("rowsCreatedAt")),
            _ts_from_unix(metadata.get("viewLastModified")),
            prior_rows_updated_at, started, started,
            Jsonb({"reason": "rowsUpdatedAt unchanged"}),
        ))
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    pages_fetched: int,
    rows_inserted: int,
    rows_updated: int,
    rows_unchanged: int,
    bytes_downloaded: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.nyc_opendata_ingest_runs
               SET status = %s, pages_fetched = %s,
                   rows_inserted = %s, rows_updated = %s, rows_unchanged = %s,
                   bytes_downloaded = %s, finished_at = now(),
                   duration_seconds = %s, error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, pages_fetched, rows_inserted, rows_updated, rows_unchanged,
            bytes_downloaded, duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-page work
# --------------------------------------------------------------------------- #


def ensure_stage_table(conn: psycopg.Connection, ds: DatasetConfig) -> None:
    """Create the per-dataset staging temp table once for the connection.

    Without ON COMMIT DROP — survives across per-page transactions.
    """
    with conn.cursor() as cur:
        cur.execute(stage_create_sql(ds).replace(" ON COMMIT DROP", ""))
    conn.commit()


def upsert_page(
    conn: psycopg.Connection,
    ds: DatasetConfig,
    rows: Iterable[dict[str, Any]],
    *,
    dataset_rows_updated_at: datetime | None,
) -> tuple[int, int, int]:
    rows_list = list(rows)
    page_size = len(rows_list)
    with conn.cursor() as cur:
        cur.execute(truncate_stage_sql(ds))
        with cur.copy(copy_sql(ds)) as copy:
            for raw in rows_list:
                copy.write_row(row_to_tuple(ds, raw, dataset_rows_updated_at))
        cur.execute(upsert_sql(ds))
        ins, upd = cur.fetchone()
    conn.commit()
    return int(ins), int(upd), page_size - int(ins) - int(upd)


# --------------------------------------------------------------------------- #
# Recon-only support
# --------------------------------------------------------------------------- #


@dataclass
class ReconStats:
    key: str
    name: str
    fourxfour: str
    total_rows: int = 0
    dataset_rows_updated_at: datetime | None = None
    contractor_field: str | None = None
    contractor_present: bool = False
    contractor_non_null: int = 0
    contractor_distinct: int = 0
    contractor_samples: list[tuple[str, int]] = field(default_factory=list)  # (name, count)
    secondary_contractor_field: str | None = None
    secondary_contractor_non_null: int = 0
    secondary_contractor_distinct: int = 0
    owner_field: str | None = None
    owner_non_null: int = 0
    owner_distinct: int = 0
    owner_samples: list[tuple[str, int]] = field(default_factory=list)
    license_type_field: str | None = None
    license_type_distribution: list[tuple[str, int]] = field(default_factory=list)
    status_field: str | None = None
    status_distribution: list[tuple[str, int]] = field(default_factory=list)
    date_field: str | None = None
    date_min: str | None = None
    date_max: str | None = None
    natural_key_field: str | None = None
    natural_key_non_null: int = 0
    natural_key_distinct: int = 0


def _quoted(col: str) -> str:
    """Quote an identifier for SQL — defensive against typo-preserved
    column names like 'commmunity_board' that happen to be safe but the
    pattern is good hygiene for the trailing-underscore work_type_ flags."""
    return '"' + col.replace('"', '""') + '"'


def gather_recon_stats(
    conn: psycopg.Connection,
    ds: DatasetConfig,
    metadata: dict[str, Any],
) -> ReconStats:
    stats = ReconStats(
        key=ds.key,
        name=ds.name,
        fourxfour=ds.fourxfour,
        dataset_rows_updated_at=_ts_from_unix(metadata.get("rowsUpdatedAt")),
        contractor_field=ds.contractor_col,
        secondary_contractor_field=ds.secondary_contractor_col,
        owner_field=ds.owner_col,
        license_type_field=ds.license_type_col,
        status_field=ds.status_col,
        date_field=ds.date_col,
        natural_key_field=ds.natural_key_col,
    )
    fq = ds.fully_qualified
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {fq};")
        stats.total_rows = int(cur.fetchone()[0])

        if ds.contractor_col is not None:
            col = _quoted(ds.contractor_col)
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL), "
                f"       count(DISTINCT {col}) FROM {fq};"
            )
            non_null, distinct = cur.fetchone()
            stats.contractor_non_null = int(non_null or 0)
            stats.contractor_distinct = int(distinct or 0)
            stats.contractor_present = stats.contractor_non_null > 0

            cur.execute(
                f"SELECT {col}, count(*) AS c FROM {fq} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY c DESC LIMIT 15;"
            )
            stats.contractor_samples = [(r[0], int(r[1])) for r in cur.fetchall()]

        if ds.secondary_contractor_col is not None:
            col = _quoted(ds.secondary_contractor_col)
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL), "
                f"       count(DISTINCT {col}) FROM {fq};"
            )
            non_null, distinct = cur.fetchone()
            stats.secondary_contractor_non_null = int(non_null or 0)
            stats.secondary_contractor_distinct = int(distinct or 0)

        if ds.owner_col is not None and ds.owner_col != ds.contractor_col:
            col = _quoted(ds.owner_col)
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL), "
                f"       count(DISTINCT {col}) FROM {fq};"
            )
            non_null, distinct = cur.fetchone()
            stats.owner_non_null = int(non_null or 0)
            stats.owner_distinct = int(distinct or 0)

            cur.execute(
                f"SELECT {col}, count(*) AS c FROM {fq} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY c DESC LIMIT 10;"
            )
            stats.owner_samples = [(r[0], int(r[1])) for r in cur.fetchall()]

        if ds.license_type_col is not None:
            col = _quoted(ds.license_type_col)
            cur.execute(
                f"SELECT {col}, count(*) AS c FROM {fq} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY c DESC LIMIT 20;"
            )
            stats.license_type_distribution = [(r[0], int(r[1])) for r in cur.fetchall()]

        if ds.status_col is not None:
            col = _quoted(ds.status_col)
            cur.execute(
                f"SELECT {col}, count(*) AS c FROM {fq} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY c DESC LIMIT 20;"
            )
            stats.status_distribution = [(r[0], int(r[1])) for r in cur.fetchall()]

        if ds.date_col is not None:
            col = _quoted(ds.date_col)
            cur.execute(
                f"SELECT min({col})::text, max({col})::text FROM {fq};"
            )
            d_min, d_max = cur.fetchone()
            stats.date_min = d_min
            stats.date_max = d_max

        if ds.natural_key_col is not None:
            col = _quoted(ds.natural_key_col)
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL), "
                f"       count(DISTINCT {col}) FROM {fq};"
            )
            non_null, distinct = cur.fetchone()
            stats.natural_key_non_null = int(non_null or 0)
            stats.natural_key_distinct = int(distinct or 0)

    return stats


def print_recon_block(stats: ReconStats) -> None:
    print(f"=== RECON: {stats.name} ({stats.fourxfour}) ===")
    print(f"  total rows ingested:     {stats.total_rows:,}")
    print(f"  dataset_rows_updated_at: {stats.dataset_rows_updated_at}")
    if stats.natural_key_field is not None:
        print(
            f"  natural key field:       {stats.natural_key_field}  "
            f"(non-null: {stats.natural_key_non_null:,}/{stats.total_rows:,}, "
            f"distinct: {stats.natural_key_distinct:,})"
        )
    if stats.date_field is not None:
        print(
            f"  date range:              {stats.date_min} .. {stats.date_max}  "
            f"(column: {stats.date_field})"
        )
    if stats.contractor_field is None:
        print("  contractor field:        not declared for this dataset")
    elif stats.contractor_present:
        print(
            f"  contractor field:        {stats.contractor_field}  "
            f"(non-null: {stats.contractor_non_null:,}/{stats.total_rows:,}, "
            f"distinct: {stats.contractor_distinct:,})"
        )
    else:
        print(
            f"  contractor field:        {stats.contractor_field}  "
            f"(declared but ZERO non-null rows in this feed)"
        )
    if stats.secondary_contractor_field is not None:
        print(
            f"  secondary firm field:    {stats.secondary_contractor_field}  "
            f"(non-null: {stats.secondary_contractor_non_null:,}/{stats.total_rows:,}, "
            f"distinct: {stats.secondary_contractor_distinct:,})"
        )
    if stats.owner_field is not None and stats.owner_field != stats.contractor_field:
        print(
            f"  owner field:             {stats.owner_field}  "
            f"(non-null: {stats.owner_non_null:,}/{stats.total_rows:,}, "
            f"distinct: {stats.owner_distinct:,})"
        )
    if stats.license_type_field is not None and stats.license_type_distribution:
        print(f"  license-type distribution ({stats.license_type_field}):")
        for v, c in stats.license_type_distribution:
            print(f"      {v!r:35s} {c:>10,}")
    if stats.status_field is not None and stats.status_distribution:
        print(f"  status distribution ({stats.status_field}):")
        for v, c in stats.status_distribution[:15]:
            print(f"      {v!r:50s} {c:>10,}")
        if len(stats.status_distribution) > 15:
            print(f"      ... ({len(stats.status_distribution) - 15} more states)")
    if stats.contractor_samples:
        print(f"  top 15 by {stats.contractor_field}:")
        for name, count in stats.contractor_samples:
            print(f"      {name!r:55s} {count:>10,}")
    if stats.owner_samples and stats.owner_field != stats.contractor_field:
        print(f"  top 10 by {stats.owner_field}:")
        for name, count in stats.owner_samples:
            print(f"      {name!r:55s} {count:>10,}")
    print("=== END RECON ===")
    print()


def print_cross_dataset_summary(all_stats: list[ReconStats]) -> None:
    print("=== CROSS-DATASET SUMMARY ===")
    total = 0
    for s in all_stats:
        if s.contractor_field is None:
            present = "n/a"
        elif s.contractor_present:
            present = f"{s.contractor_field} ({s.contractor_non_null:,}/{s.total_rows:,})"
        else:
            present = f"{s.contractor_field} (declared/empty)"
        label = f"  {s.key}:".ljust(40)
        print(f"{label}{s.total_rows:>10,} rows, contractor: {present}")
        total += s.total_rows
    print(f"  total rows across datasets:           {total:>10,}")
    print("=== END SUMMARY ===")


def run_recon_only(
    ds: DatasetConfig,
    *,
    page_size: int,
) -> ReconStats | None:
    """Hit metadata + first page (no DB writes), then run the recon
    SELECTs against the existing table contents (assumes a prior ingest
    landed something — if not, total_rows will be 0 and we say so)."""
    auth, headers, auth_method = _resolve_auth()
    headers = {**headers, "User-Agent": "data-engine-x/nyc-opendata-ingest"}
    log.info("[%s] RECON-ONLY (4x4=%s table=%s)", ds.key, ds.fourxfour, ds.fully_qualified)

    with httpx.Client(auth=auth, headers=headers) as client:
        metadata = fetch_metadata(client, ds)
        rows_updated_at = _ts_from_unix(metadata.get("rowsUpdatedAt"))
        log.info("[%s] auth=%s rowsUpdatedAt=%s", ds.key, auth_method, rows_updated_at)
        rows, nbytes = fetch_page(client, ds, page_size=min(page_size, 5), offset=0)
        log.info("[%s] first-page sample: %s rows, %s bytes",
                 ds.key, len(rows), nbytes)

    try:
        with psycopg.connect(_database_url()) as conn:
            stats = gather_recon_stats(conn, ds, metadata)
    except psycopg.errors.UndefinedTable:
        log.error(
            "[%s] table %s does not exist — apply the migration first.",
            ds.key, ds.fully_qualified,
        )
        return None
    print_recon_block(stats)
    return stats


# --------------------------------------------------------------------------- #
# Per-dataset main
# --------------------------------------------------------------------------- #


def ingest_dataset(
    ds: DatasetConfig,
    *,
    page_size: int,
    page_sleep: float,
    max_pages: int | None,
    skip_if_unchanged: bool,
    dry_run: bool,
) -> int:
    auth, headers, auth_method = _resolve_auth()
    headers = {**headers, "User-Agent": "data-engine-x/nyc-opendata-ingest"}
    started_wall = time.monotonic()
    log.info("[%s] start (4x4=%s table=%s)", ds.key, ds.fourxfour, ds.fully_qualified)

    with httpx.Client(auth=auth, headers=headers) as client:
        log.info("[%s] fetch metadata", ds.key)
        metadata = fetch_metadata(client, ds)
        rows_updated_at = _ts_from_unix(metadata.get("rowsUpdatedAt"))
        log.info("[%s] dataset rowsUpdatedAt: %s", ds.key, rows_updated_at)

        if dry_run:
            log.info("[%s] DRY RUN — first %s page(s), no DB writes",
                     ds.key, max_pages or 1)
            offset = 0
            for page_idx in range(max_pages or 1):
                rows, nbytes = fetch_page(client, ds, page_size=page_size, offset=offset)
                log.info("[%s]   page %s: %s rows, %s bytes",
                         ds.key, page_idx, len(rows), nbytes)
                if rows:
                    log.info("[%s]   sample: %s", ds.key,
                             json.dumps(rows[0], default=str)[:300])
                if len(rows) < page_size:
                    break
                offset += page_size
                time.sleep(page_sleep)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_rows_updated_at(conn, ds)
            log.info("[%s] prior successful rowsUpdatedAt: %s", ds.key, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and rows_updated_at is not None
                and rows_updated_at <= prior
            ):
                log.info("[%s] rowsUpdatedAt unchanged — recording no_change", ds.key)
                write_no_change_run(
                    conn, ds, auth_method=auth_method, page_size=page_size,
                    metadata=metadata, prior_rows_updated_at=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, ds, auth_method=auth_method, page_size=page_size,
                metadata=metadata, prior_rows_updated_at=prior,
            )
            log.info("[%s] run id: %s", ds.key, run_id)
            ensure_stage_table(conn, ds)

            total_inserted = total_updated = total_unchanged = 0
            total_bytes = pages_fetched = 0
            try:
                offset = 0
                while True:
                    if max_pages is not None and pages_fetched >= max_pages:
                        log.info("[%s] max-pages limit hit", ds.key)
                        break
                    page_started = time.monotonic()
                    rows, nbytes = fetch_page(client, ds, page_size=page_size, offset=offset)
                    pages_fetched += 1
                    total_bytes += nbytes
                    ins, upd, unch = upsert_page(
                        conn, ds, rows, dataset_rows_updated_at=rows_updated_at,
                    )
                    total_inserted += ins
                    total_updated += upd
                    total_unchanged += unch
                    log.info(
                        "[%s] page %s: fetched=%s ins=%s upd=%s unch=%s bytes=%s elapsed=%.1fs",
                        ds.key, pages_fetched, len(rows), ins, upd, unch,
                        nbytes, time.monotonic() - page_started,
                    )
                    if len(rows) < page_size:
                        break
                    offset += page_size
                    time.sleep(page_sleep)

                finalize_run_row(
                    conn, run_id, status="completed",
                    pages_fetched=pages_fetched,
                    rows_inserted=total_inserted, rows_updated=total_updated,
                    rows_unchanged=total_unchanged, bytes_downloaded=total_bytes,
                    started_at=started_wall, error_message=None, notes=None,
                )
                log.info(
                    "[%s] DONE — pages=%s ins=%s upd=%s unch=%s bytes=%s wall=%.1fs",
                    ds.key, pages_fetched, total_inserted, total_updated,
                    total_unchanged, total_bytes, time.monotonic() - started_wall,
                )
                return 0
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] ingest failed", ds.key)
                finalize_run_row(
                    conn, run_id, status="failed",
                    pages_fetched=pages_fetched,
                    rows_inserted=total_inserted, rows_updated=total_updated,
                    rows_unchanged=total_unchanged, bytes_downloaded=total_bytes,
                    started_at=started_wall, error_message=str(exc), notes=None,
                )
                return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

ALL_KEYS = list(DATASETS.keys())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "dataset", choices=ALL_KEYS + ["all"],
        help="Dataset key, or 'all' to run every dataset sequentially.",
    )
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--max-pages", type=int, default=None,
                   help="Stop after N pages (smoke test).")
    p.add_argument("--page-sleep-seconds", type=float, default=1.0)
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if metadata.rowsUpdatedAt has not advanced "
                        "since the prior successful run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch pages but do not write to the DB.")
    p.add_argument("--recon-only", action="store_true",
                   help="Hit metadata + first page (no DB writes), then run "
                        "analytical queries against the existing table and "
                        "print a per-dataset recon block. With 'all', also "
                        "prints a cross-dataset summary.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    keys = ALL_KEYS if args.dataset == "all" else [args.dataset]

    if args.recon_only:
        all_stats: list[ReconStats] = []
        for k in keys:
            ds = DATASETS[k]
            s = run_recon_only(ds, page_size=args.page_size)
            if s is not None:
                all_stats.append(s)
        if args.dataset == "all" and all_stats:
            print_cross_dataset_summary(all_stats)
        return 0

    rc = 0
    for k in keys:
        ds = DATASETS[k]
        ds_rc = ingest_dataset(
            ds,
            page_size=args.page_size,
            page_sleep=args.page_sleep_seconds,
            max_pages=args.max_pages,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
        )
        rc = rc or ds_rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
