"""Per-state Insurance Producers source-config + schema mapping.

Phase 1 sources — three states whose Departments of Insurance / DFS publish
producer-license bulk data in free public form:

  Texas (TX)        Socrata datasets at data.texas.gov
                      kxv3-diwf  individuals + adjusters (license-grain, ~949K)
                      3yqc-fcdt  agencies / business entities (~55K)
  Florida (FL)      Direct CSV downloads at myfloridacfo.com/downloads/AAS/
                      AllValidLicensesIndividual.csv  (license-grain, ~334 MB)
                      AllValidLicensesBusiness.csv     (license-grain, ~26 MB)
  Illinois (IL)     Socrata dataset at data.illinois.gov
                      serf-cewv  insurance producers (LOA-grain, ~628K rows)

The original directive (TX/FL/NY/IL/CA) was descoped after URL discovery showed
NY publishes producer data via FOIL request only (no bulk endpoint) and CA
gates bulk data behind a $4,417 paid mailing-list product. The operator's
explicit "CA descope is acceptable" language in the directive (along with the
pause-and-ask threshold of >2-of-5 / >40% which 2-of-5 / 40% does NOT exceed)
permits proceeding with TX + FL + IL. NY + CA are surfaced in the cycle report
for operator awareness.

This module is data only — pure config dicts, no I/O. The ingest script
(`run_insurance_producers_r2_ingest.py`) reads the `STREAMS` list and emits
one ZSTD Parquet per (state, stream) tuple per snapshot.

Stream kinds:
  - `socrata`   — pulled via Socrata JSON resource API (`/resource/<id>.json`)
                  with `$order=:id` pagination + `rowsUpdatedAt` freshness signal
                  from the view-metadata endpoint.
  - `csv_url`   — fetched as a single HTTP GET to a static URL; freshness
                  signaled via the `Last-Modified` response header.

Producer-kind rules:
  - `INDIVIDUAL_ONLY` — every row in the stream is an individual producer.
  - `AGENCY_ONLY`     — every row in the stream is an agency / business entity.
  - `MIXED`           — kind is determined per-row from a column (e.g. IL,
                        where `FIRST_NAME` populated → INDIVIDUAL, else AGENCY).

Identity-spine column hints (used by the normalizer / Parquet projection):
  - `npn_column`            — source column carrying the National Producer Number
  - `license_number_column` — source column carrying the state-issued license #
  - `individual_first_column` / `_middle_column` / `_last_column`
                            — split-name source columns (FL has these)
  - `individual_full_name_column` — combined-name source column when split is
                                    not available (FL "Full Name", TX "Name",
                                    IL "LAST_NAME_OR_BUSINESS_NAME"+"FIRST_NAME"
                                    via assemble_individual_name_columns)
  - `agency_name_column`    — single org-name source column (FL business "Full
                              Name", TX agencies "Name", IL when FIRST_NAME is
                              empty)

Address-column tuples are listed in preference order; first-non-NULL wins
during the COALESCE projection.

License lifecycle:
  - `license_status_column` — when present, fed through
                              `classify_license_status`. When None (some FL
                              files only ship VALID, IL has no status), the
                              normalized status defaults to ACTIVE because the
                              source filters to active licenses by URL choice
                              (e.g. FL's "AllValidLicenses..." files).

Lines of authority:
  - `loa_column` — source column carrying the LOA / license-type / qualification
                   string. Fed through `normalize_lines_of_authority` to emit
                   the canonical-enum semicolon-joined set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


# Producer-kind rules.
KIND_RULE_INDIVIDUAL_ONLY: Final = "INDIVIDUAL_ONLY"
KIND_RULE_AGENCY_ONLY: Final = "AGENCY_ONLY"
KIND_RULE_MIXED_BY_FIRST_NAME: Final = "MIXED_BY_FIRST_NAME"
# (More rules can be added as new states surface different conventions.)


@dataclass(frozen=True)
class StreamConfig:
    """One producer-licensing dataset to ingest."""
    state: str                             # 'TX' | 'FL' | 'IL'
    name: str                              # 'individuals' | 'agencies' | 'producers' | 'business'
    source_kind: str                       # 'socrata' | 'csv_url'
    title: str                             # human-readable

    # --- Socrata-specific ---------------------------------------------------
    domain: str | None = None              # e.g. 'data.texas.gov'
    socrata_id: str | None = None          # e.g. 'kxv3-diwf'

    # --- CSV-URL-specific ---------------------------------------------------
    csv_url: str | None = None             # full HTTPS URL
    csv_encoding: str = "utf-8"            # most state CSVs are UTF-8 / ASCII

    # --- Producer-kind logic ------------------------------------------------
    kind_rule: str = KIND_RULE_INDIVIDUAL_ONLY
    # For MIXED rules: source column whose presence determines kind.
    kind_discriminator_column: str | None = None

    # --- Identity-spine source columns --------------------------------------
    # Some columns may be absent in a given state's source — that's OK; the
    # ingest script's projection guards against missing columns.
    npn_column: str | None = None
    license_number_column: str | None = None
    individual_first_column: str | None = None
    individual_middle_column: str | None = None
    individual_last_column: str | None = None
    # Used when the source ships a single combined-name column (TX 'Name',
    # FL 'Full Name'). The normalizer's comma-reverse heuristic handles the
    # 'LAST, FIRST' shape FL uses.
    individual_full_name_column: str | None = None
    agency_name_column: str | None = None

    # --- Address spine ------------------------------------------------------
    # Tuples carry preference order — first-non-NULL wins during COALESCE.
    address_zip_columns: tuple = ()
    address_state_columns: tuple = ()
    address_city_columns: tuple = ()

    # --- License lifecycle --------------------------------------------------
    license_status_column: str | None = None
    license_status_default: str | None = None  # used when source has no col
    license_effective_date_column: str | None = None
    license_expiration_date_column: str | None = None

    # --- Lines of authority -------------------------------------------------
    loa_column: str | None = None
    license_type_column: str | None = None  # secondary descriptor (FL has both
                                            # 'License TYCL' code + 'License
                                            # TYCL Desc' text — use the Desc)

    # --- Cross-licensure ----------------------------------------------------
    home_state_column: str | None = None
    residency_type_column: str | None = None  # 'Resident' / 'Non-Resident'

    # --- Date format hints --------------------------------------------------
    # Date-style columns the projection should TRY_CAST or pass through to the
    # `parse_us_date` normalizer. Order doesn't matter; presence-checked at
    # projection time against actual source columns.
    date_columns: tuple = ()

    @property
    def csv_url_resolved(self) -> str:
        """Bulk-download URL for the stream, regardless of source kind.

        For Socrata streams this is the JSON resource endpoint with
        `$order=:id` pagination handled by the ingest script. For csv_url
        streams it's the static URL verbatim.
        """
        if self.source_kind == "socrata":
            assert self.domain and self.socrata_id
            return (
                f"https://{self.domain}/resource/{self.socrata_id}.json"
            )
        assert self.csv_url
        return self.csv_url

    @property
    def metadata_url(self) -> str | None:
        """Freshness-check URL — Socrata view-metadata or None for csv_url
        (csv_url freshness comes from the HEAD `Last-Modified` header)."""
        if self.source_kind == "socrata":
            assert self.domain and self.socrata_id
            return f"https://{self.domain}/api/views/{self.socrata_id}.json"
        return None


# --------------------------------------------------------------------------- #
# Phase 1 streams — TX (×2) + FL (×2) + IL (×1) = 5 R2 partitions per snapshot.
# --------------------------------------------------------------------------- #

STREAMS: Final[tuple[StreamConfig, ...]] = (
    # ----- Texas: data.texas.gov Socrata datasets ---------------------------
    StreamConfig(
        state="TX",
        name="individuals",
        source_kind="socrata",
        domain="data.texas.gov",
        socrata_id="kxv3-diwf",
        title="Insurance agents, adjusters, and people approved to manage "
              "insurance-related products or claims (Texas)",
        kind_rule=KIND_RULE_INDIVIDUAL_ONLY,
        npn_column="npn",
        license_number_column="license_number",
        individual_full_name_column="name",
        # TX Socrata always serves active licenses; no status column shipped.
        license_status_default="ACTIVE",
        license_effective_date_column="license_issue_date",
        license_expiration_date_column="expiration_date",
        loa_column="qualification",
        license_type_column="license_type",
        address_zip_columns=("pstl_cd",),
        address_state_columns=("state",),
        address_city_columns=("city",),
        date_columns=("license_issue_date", "expiration_date"),
    ),
    StreamConfig(
        state="TX",
        name="agencies",
        source_kind="socrata",
        domain="data.texas.gov",
        socrata_id="3yqc-fcdt",
        title="Insurance agencies and businesses approved to manage "
              "insurance-related products (Texas)",
        kind_rule=KIND_RULE_AGENCY_ONLY,
        npn_column="npn",
        license_number_column="agency_license_number",
        agency_name_column="org_name",
        license_status_default="ACTIVE",
        license_effective_date_column="license_issue_date",
        license_expiration_date_column="expiration_date",
        loa_column="qualification",
        license_type_column="license_type",
        address_zip_columns=("pstl_cd",),
        address_state_columns=("state",),
        address_city_columns=("city",),
        date_columns=("license_issue_date", "expiration_date"),
    ),

    # ----- Florida: myfloridacfo.com direct-CSV downloads -------------------
    StreamConfig(
        state="FL",
        name="individuals",
        source_kind="csv_url",
        csv_url=("https://www.myfloridacfo.com/downloads/AAS/LicenseeSearch/"
                 "AllValidLicensesIndividual.csv"),
        title="Florida DFS — All Valid Individual Insurance Licenses",
        kind_rule=KIND_RULE_INDIVIDUAL_ONLY,
        npn_column="NPN Number",
        license_number_column="License Number",
        individual_first_column="First Name",
        individual_middle_column="Middle Name",
        individual_last_column="Last Name",
        individual_full_name_column="Full Name",
        license_status_column="License Status",
        license_effective_date_column="License Issue Date",
        # Address: prefer Mailing then Business.
        address_zip_columns=("Mailing Zip", "Business Zip"),
        address_state_columns=("Mailing State", "Business State"),
        address_city_columns=("Mailing City", "Business City"),
        loa_column="License TYCL Desc",
        license_type_column="License TYCL",
        residency_type_column="Residency Type",
        date_columns=("License Issue Date",),
    ),
    StreamConfig(
        state="FL",
        name="business",
        source_kind="csv_url",
        csv_url=("https://www.myfloridacfo.com/downloads/AAS/LicenseeSearch/"
                 "AllValidLicensesBusiness.csv"),
        title="Florida DFS — All Valid Business / Agency Insurance Licenses",
        kind_rule=KIND_RULE_AGENCY_ONLY,
        npn_column="NPN Number",
        license_number_column="License Number",
        agency_name_column="Full Name",
        license_status_column="License Status",
        license_effective_date_column="License Issue Date",
        address_zip_columns=("Mailing Zip", "Business Zip"),
        address_state_columns=("Mailing State", "Business State"),
        address_city_columns=("Mailing City", "Business City"),
        loa_column="License TYCL Desc",
        license_type_column="License TYCL",
        residency_type_column="Residency Type",
        date_columns=("License Issue Date",),
    ),

    # ----- Illinois: data.illinois.gov Socrata dataset ----------------------
    StreamConfig(
        state="IL",
        name="producers",
        source_kind="socrata",
        domain="data.illinois.gov",
        socrata_id="serf-cewv",
        title="DOI Insurance Producers (Illinois)",
        # IL ships individuals AND agencies in the same dataset; if FIRST_NAME
        # is populated it's an individual, else the LAST_NAME_OR_BUSINESS_NAME
        # field carries the agency name.
        kind_rule=KIND_RULE_MIXED_BY_FIRST_NAME,
        kind_discriminator_column="first_name",
        # IL Socrata column names (snake_case from Socrata field names).
        individual_first_column="first_name",
        individual_last_column="last_name_or_business_name",
        agency_name_column="last_name_or_business_name",
        license_status_default="ACTIVE",
        # Address: only Mailing block in IL.
        address_zip_columns=("zip",),
        address_state_columns=("mailing_state",),
        address_city_columns=("mailing_city",),
        loa_column="loa_name",
    ),
)


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #


def streams_for_state(code: str) -> tuple[StreamConfig, ...]:
    """Return the streams configured for a given 2-letter state code."""
    return tuple(s for s in STREAMS if s.state == code.upper())


def stream_by_id(name_or_id: str) -> StreamConfig | None:
    """Lookup a stream by its Socrata 4×4 id, or its `STATE/NAME` slug."""
    for s in STREAMS:
        if s.socrata_id == name_or_id:
            return s
        if f"{s.state}/{s.name}" == name_or_id:
            return s
    return None


# --------------------------------------------------------------------------- #
# Sanity self-check (catches typos at module import in dev).
# --------------------------------------------------------------------------- #


def _validate() -> None:
    seen = set()
    for s in STREAMS:
        key = (s.state, s.name)
        if key in seen:
            raise ValueError(f"Duplicate stream key: {key}")
        seen.add(key)
        if s.source_kind == "socrata":
            if not (s.domain and s.socrata_id):
                raise ValueError(
                    f"{s.state}/{s.name}: socrata stream missing domain/id"
                )
        elif s.source_kind == "csv_url":
            if not s.csv_url:
                raise ValueError(
                    f"{s.state}/{s.name}: csv_url stream missing csv_url"
                )
        else:
            raise ValueError(
                f"{s.state}/{s.name}: unknown source_kind={s.source_kind!r}"
            )
        if s.kind_rule == KIND_RULE_INDIVIDUAL_ONLY:
            if not (s.individual_full_name_column or s.individual_last_column):
                raise ValueError(
                    f"{s.state}/{s.name}: INDIVIDUAL_ONLY stream missing "
                    "individual name source columns"
                )
        elif s.kind_rule == KIND_RULE_AGENCY_ONLY:
            if not s.agency_name_column:
                raise ValueError(
                    f"{s.state}/{s.name}: AGENCY_ONLY stream missing "
                    "agency_name_column"
                )
        elif s.kind_rule == KIND_RULE_MIXED_BY_FIRST_NAME:
            if not s.kind_discriminator_column:
                raise ValueError(
                    f"{s.state}/{s.name}: MIXED rule missing discriminator col"
                )
        else:
            raise ValueError(
                f"{s.state}/{s.name}: unknown kind_rule={s.kind_rule!r}"
            )


_validate()
