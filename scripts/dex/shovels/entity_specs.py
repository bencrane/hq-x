"""Per-entity typed-column declarations for the 6 Shovels canonical tables.

Each ``EntityIngestSpec`` lists the typed columns projected out of the verbatim
Shovels record — the PK plus every field used downstream for filtering, sorting,
or joining (derived from ``SHOVELS_API_CANONICAL_REFERENCE.md`` §6 field
dictionaries). The full record always also lands in ``raw_json`` (see
``_client.EntityIngestSpec.project``), so nothing is lost — the typed columns are
the queryable/indexable surface, ``raw_json`` is the 1:1 mirror.

Conventions:
  * Money fields (``job_value``, ``fees``, ``*_job_value``, market value) are
    integers as published by Shovels (§2/§6) — mirrored as ``int64`` verbatim.
  * Dates are ``YYYY-MM-DD`` strings (§2) — kept as string, not parsed, so the
    raw fidelity holds; downstream casts in DuckDB as needed.
  * Nested structs/arrays (``tags``, ``geo_ids``, ``address``, ``classification_
    derived``) are stored as compact JSON strings (queryable via DuckDB
    ``json_extract``; dodges the Lance LIST definition-buffer cap per CLAUDE.md
    L54).
  * Residents have no natural id → we synthesize a deterministic ``resident_key``
    (see ``resident_key``); that is the PK + BTREE key.
"""
from __future__ import annotations

import hashlib
from typing import Any

import pyarrow as pa

from scripts.shovels._client import (
    EntityIngestSpec,
    to_int,
    to_float,
    to_json_str,
    to_str,
)


def _g(*keys: str):
    """Extractor for a top-level field via the first present key."""
    def _extract(raw: dict[str, Any]) -> Any:
        for k in keys:
            if k in raw:
                return raw.get(k)
        return None
    return _extract


def _nested(outer: str, inner: str):
    def _extract(raw: dict[str, Any]) -> Any:
        sub = raw.get(outer)
        return sub.get(inner) if isinstance(sub, dict) else None
    return _extract


def _str_of(*keys: str):
    inner = _g(*keys)
    return lambda raw: to_str(inner(raw))


def _int_of(*keys: str):
    inner = _g(*keys)
    return lambda raw: to_int(inner(raw))


def _float_of(*keys: str):
    inner = _g(*keys)
    return lambda raw: to_float(inner(raw))


def _json_of(*keys: str):
    inner = _g(*keys)
    return lambda raw: to_json_str(inner(raw))


def _nested_str(outer: str, inner: str):
    fn = _nested(outer, inner)
    return lambda raw: to_str(fn(raw))


# --------------------------------------------------------------------------- #
# resident deterministic key
# --------------------------------------------------------------------------- #
def resident_key(*, address_geo_id: str, raw: dict[str, Any]) -> str:
    """Deterministic composite PK for a resident row.

    Residents (§6.5) have NO natural id. We key on the address geo_id plus a
    stable hash of the identity tuple (name + personal_emails + phone). Same
    person at same address on a re-fetch ⇒ identical key ⇒ dedup-latest-per-PK
    collapses to one row. Different people at the same address ⇒ distinct keys.

    The hash inputs are normalized (lower/stripped) so trivial whitespace/case
    drift does not fork the key. The raw PII still lands verbatim in raw_json;
    the key itself is a non-reversible digest (no PII recoverable from it alone).
    """
    name = (to_str(raw.get("name")) or "").strip().lower()
    email = (to_str(raw.get("personal_emails")) or "").strip().lower()
    phone = (to_str(raw.get("phone")) or "").strip().lower()
    basis = f"{address_geo_id}|{name}|{email}|{phone}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return f"{address_geo_id}:{digest}"


# --------------------------------------------------------------------------- #
# 1. permits  (PermitsRead, §6.1) — PK = id
# --------------------------------------------------------------------------- #
PERMIT_SPEC = EntityIngestSpec(
    entity="permit",
    r2_entity_dir="permit",
    pk_column="id",
    typed_columns=[
        ("id", pa.string(), _str_of("id")),
        ("number", pa.string(), _str_of("number")),
        ("description", pa.string(), _str_of("description")),
        ("jurisdiction", pa.string(), _str_of("jurisdiction")),
        ("job_value", pa.int64(), _int_of("job_value")),
        ("fees", pa.int64(), _int_of("fees")),
        ("type", pa.string(), _str_of("type")),
        ("subtype", pa.string(), _str_of("subtype")),
        ("status", pa.string(), _str_of("status")),
        ("file_date", pa.string(), _str_of("file_date")),
        ("issue_date", pa.string(), _str_of("issue_date")),
        ("final_date", pa.string(), _str_of("final_date")),
        ("start_date", pa.string(), _str_of("start_date")),
        ("end_date", pa.string(), _str_of("end_date")),
        ("total_duration", pa.int64(), _int_of("total_duration")),
        ("construction_duration", pa.int64(), _int_of("construction_duration")),
        ("approval_duration", pa.int64(), _int_of("approval_duration")),
        ("inspection_pass_rate", pa.float64(), _float_of("inspection_pass_rate")),
        ("contractor_id", pa.string(), _str_of("contractor_id")),
        ("tags", pa.string(), _json_of("tags")),
        ("property_type", pa.string(), _str_of("property_type")),
        ("property_type_detail", pa.string(), _str_of("property_type_detail")),
        ("property_year_built", pa.int64(), _int_of("property_year_built")),
        ("property_building_area", pa.int64(), _int_of("property_building_area")),
        ("property_lot_size", pa.int64(), _int_of("property_lot_size")),
        ("property_unit_count", pa.int64(), _int_of("property_unit_count")),
        ("property_assess_market_value", pa.int64(), _int_of("property_assess_market_value")),
        # geo_ids map (address_id/city_id/county_id/jurisdiction_id) — JSON +
        # the address_id flattened for the residents-leg join.
        ("geo_ids", pa.string(), _json_of("geo_ids")),
        ("address_id", pa.string(), _nested_str("geo_ids", "address_id")),
        ("city_id", pa.string(), _nested_str("geo_ids", "city_id")),
        ("county_id", pa.string(), _nested_str("geo_ids", "county_id")),
        ("jurisdiction_id", pa.string(), _nested_str("geo_ids", "jurisdiction_id")),
        # flattened address fields for cheap geo filtering
        ("address_city", pa.string(), _nested_str("address", "city")),
        ("address_state", pa.string(), _nested_str("address", "state")),
        ("address_zip_code", pa.string(), _nested_str("address", "zip_code")),
        ("address", pa.string(), _json_of("address")),
    ],
)


# --------------------------------------------------------------------------- #
# 2. contractors  (ContractorsRead, §6.3) — PK = id
# --------------------------------------------------------------------------- #
CONTRACTOR_SPEC = EntityIngestSpec(
    entity="contractor",
    r2_entity_dir="contractor",
    pk_column="id",
    typed_columns=[
        ("id", pa.string(), _str_of("id")),
        ("license", pa.string(), _str_of("license")),
        ("name", pa.string(), _str_of("name")),
        ("business_name", pa.string(), _str_of("business_name")),
        ("business_type", pa.string(), _str_of("business_type")),
        ("classification", pa.string(), _json_of("classification")),
        ("classification_derived", pa.string(), _json_of("classification_derived")),
        ("license_issue_date", pa.string(), _str_of("license_issue_date")),
        ("license_exp_date", pa.string(), _str_of("license_exp_date")),
        ("primary_email", pa.string(), _str_of("primary_email")),
        ("primary_phone", pa.string(), _str_of("primary_phone")),
        ("website", pa.string(), _str_of("website")),
        ("dba", pa.string(), _str_of("dba")),
        ("sic", pa.string(), _str_of("sic")),
        ("naics", pa.string(), _str_of("naics")),
        ("linkedin_url", pa.string(), _str_of("linkedin_url")),
        ("revenue", pa.string(), _str_of("revenue")),
        ("employee_count", pa.string(), _str_of("employee_count")),  # RANGE STRING (§13.7)
        ("primary_industry", pa.string(), _str_of("primary_industry")),
        ("review_count", pa.int64(), _int_of("review_count")),
        ("rating", pa.float64(), _float_of("rating")),
        ("permit_count", pa.int64(), _int_of("permit_count")),
        ("avg_job_value", pa.int64(), _int_of("avg_job_value")),
        ("total_job_value", pa.int64(), _int_of("total_job_value")),
        ("avg_construction_duration", pa.int64(), _int_of("avg_construction_duration")),
        ("avg_inspection_pass_rate", pa.float64(), _float_of("avg_inspection_pass_rate")),
        ("first_seen_date", pa.string(), _str_of("first_seen_date")),
        ("status_tally", pa.string(), _json_of("status_tally")),
        ("tag_tally", pa.string(), _json_of("tag_tally")),
        ("address_state", pa.string(), _nested_str("address", "state")),
        ("address", pa.string(), _json_of("address")),
    ],
)


# --------------------------------------------------------------------------- #
# 3. employees  (Employees, §6.4, PII) — PK = id (+ contractor_id)
# --------------------------------------------------------------------------- #
EMPLOYEE_SPEC = EntityIngestSpec(
    entity="employee",
    r2_entity_dir="employee",
    pk_column="id",
    typed_columns=[
        ("id", pa.string(), _str_of("id")),
        ("contractor_id", pa.string(), _str_of("contractor_id")),
        ("name", pa.string(), _str_of("name")),                 # PII
        ("phone", pa.string(), _str_of("phone")),               # PII
        ("email", pa.string(), _str_of("email")),               # PII
        ("business_email", pa.string(), _str_of("business_email")),  # PII
        ("linkedin_url", pa.string(), _str_of("linkedin_url")),
        ("street_no", pa.string(), _str_of("street_no")),
        ("street", pa.string(), _str_of("street")),
        ("city", pa.string(), _str_of("city")),
        ("state", pa.string(), _str_of("state")),
        ("zip_code", pa.string(), _str_of("zip_code")),
        ("gender", pa.string(), _str_of("gender")),
        ("age_range", pa.string(), _str_of("age_range")),
        ("income_range", pa.string(), _str_of("income_range")),
        ("net_worth", pa.string(), _str_of("net_worth")),
        ("homeowner", pa.string(), _str_of("homeowner")),
        ("job_title", pa.string(), _str_of("job_title")),
        ("seniority_level", pa.string(), _str_of("seniority_level")),
        ("department", pa.string(), _str_of("department")),
    ],
)


# --------------------------------------------------------------------------- #
# 4. residents  (ResidentsRead, §6.5, PII) — PK = synthesized resident_key
# --------------------------------------------------------------------------- #
# NOTE: residents have no natural id; the resident_key column + the address
# geo_id are injected by the CLI (it knows which address_geo_id each batch came
# from) via the ``extra`` extractor closures below, which read the key/geo from
# a record dict the CLI augments before handing to the driver.
RESIDENT_SPEC = EntityIngestSpec(
    entity="resident",
    r2_entity_dir="resident",
    pk_column="resident_key",
    typed_columns=[
        # The CLI augments each raw resident with '_resident_key' and
        # '_address_geo_id' before projection (see ingest_residents.py).
        ("resident_key", pa.string(), _str_of("_resident_key")),
        ("address_geo_id", pa.string(), _str_of("_address_geo_id")),
        ("name", pa.string(), _str_of("name")),                 # PII
        ("personal_emails", pa.string(), _str_of("personal_emails")),  # PII (single string, §6.5)
        ("phone", pa.string(), _str_of("phone")),               # PII
        ("linkedin_url", pa.string(), _str_of("linkedin_url")),
        ("net_worth", pa.string(), _str_of("net_worth")),
        ("income_range", pa.string(), _str_of("income_range")),
        ("is_homeowner", pa.string(), _str_of("is_homeowner")),
        ("street_no", pa.string(), _str_of("street_no")),
        ("street", pa.string(), _str_of("street")),
        ("city", pa.string(), _str_of("city")),
        ("state", pa.string(), _str_of("state")),
        ("zip_code", pa.string(), _str_of("zip_code")),
    ],
)


# --------------------------------------------------------------------------- #
# 5. geo dimension  (GeoEntitiesRead + detail, §6.6) — PK = geo_id
# --------------------------------------------------------------------------- #
# The CLI normalizes city/county/jurisdiction/state/zipcode rows into a common
# shape augmented with '_geo_type', '_seed_state', and detail fields before
# projection.
GEO_SPEC = EntityIngestSpec(
    entity="geo",
    r2_entity_dir="geo",
    pk_column="geo_id",
    typed_columns=[
        ("geo_id", pa.string(), _str_of("geo_id")),
        ("geo_type", pa.string(), _str_of("_geo_type")),  # city|county|jurisdiction|state|zipcode
        ("name", pa.string(), _str_of("name")),
        ("state", pa.string(), _str_of("state")),
        ("seed_state", pa.string(), _str_of("_seed_state")),
        # detail fields (present only for city/county/jurisdiction detail pulls)
        ("counties", pa.string(), _json_of("counties")),
        ("jurisdictions", pa.string(), _json_of("jurisdictions")),
        ("zipcodes", pa.string(), _json_of("zipcodes")),
    ],
)


# --------------------------------------------------------------------------- #
# 6. tags  (list/tags, §8) — PK = id (the tag slug)
# --------------------------------------------------------------------------- #
TAG_SPEC = EntityIngestSpec(
    entity="tag",
    r2_entity_dir="tag",
    pk_column="id",
    typed_columns=[
        ("id", pa.string(), _str_of("id")),
        ("description", pa.string(), _str_of("description")),
    ],
)


ALL_SPECS = {
    "permit": PERMIT_SPEC,
    "contractor": CONTRACTOR_SPEC,
    "employee": EMPLOYEE_SPEC,
    "resident": RESIDENT_SPEC,
    "geo": GEO_SPEC,
    "tag": TAG_SPEC,
}
