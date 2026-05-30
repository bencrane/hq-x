"""Per-state Socrata UCC dataset config + schema mapping.

Phase 1 sources — three states whose Secretary-of-State equivalents publish
UCC filing data on Socrata-style state open-data portals (free public access,
no login, no per-query rate-cap).

  Colorado  (data.colorado.gov)  4 datasets — normalized: filings + debtors
                                              + secured-parties + collateral
                                              joined via `fileid`
  Connecticut (data.ct.gov)      1 dataset  — denormalized: one row per lien
                                              with debtor + secured party
                                              already joined
  Oregon    (data.oregon.gov)    2 datasets — secured-parties cumulative +
                                              filings-entered-last-month
                                              (rolling current month)

The original directive (TX/FL/OH/PA/DE) was descoped after URL discovery
showed 5 of 5 states paywall their UCC bulk; the operator chose to re-scope
to states with confirmed free public bulk. See the cycle report
`reports/2026-05-08-scope-ucc-phase1-top5-r2-ingest-blocked-public-bulk-paywalled-5-of-5-states.md`
for the full access matrix and decision context.

This module is data only — pure config dicts, no I/O. The ingest script
(`run_ucc_r2_ingest.py`) reads the `STREAMS` list and emits one ZSTD Parquet
per (state, stream) tuple per snapshot.

Stream kinds:
  - `filing`         — filing-level metadata, no party identity
  - `party`          — debtor OR secured-party records, one party per row
  - `collateral`     — collateral-description records
  - `denormalized`   — one row carries filing + debtor + secured-party

Party-role resolution for PARTY streams:
  - Static: `party_role` is set to "debtor" or "secured_party" — every row in
    the stream has that role.
  - Dynamic: `party_role` is None AND `party_role_column` + `party_role_map`
    are set — the role is read per row from the named source column and
    translated through the map (e.g., OR snfi-f79b: party_type "DB" → "debtor",
    "SP" → "secured_party").

Identity-spine column hints (used by the normalizer layer):
  - `*_name_columns`   — list of source columns to coalesce into the
                          normalization input for the matching role's
                          name field. Order matters: first non-NULL wins.
  - `*_zip_column`     — source column for postal code (raw — `zip5()` is
                          applied at ingest)
  - `*_state_column`   — source column for state code (raw — normalized at
                          ingest)

Date columns:
  - `date_columns` maps a canonical date alias → source column name. The
    ingest layer attempts ISO-8601 cast (Socrata Calendar Date format).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True)
class StreamConfig:
    """One Socrata-backed UCC dataset to ingest."""
    state: str                              # "CO" | "CT" | "OR"
    name: str                               # "filings" | "debtors" | …
    domain: str                             # "data.colorado.gov"
    socrata_id: str                         # "wffy-3uut"
    title: str                              # human-readable
    kind: str                               # filing | party | collateral | denormalized

    # --- Party-role resolution (PARTY-kind streams only) -------------------
    party_role: str | None = None           # "debtor" | "secured_party" — static
    party_role_column: str | None = None    # source column carrying the role — dynamic
    party_role_map: dict = field(default_factory=dict)
    # e.g. {"DB": "debtor", "SP": "secured_party"}

    # --- Identity-spine hints — single-role streams (PARTY) ----------------
    # name_columns is a list of source columns coalesced first-non-NULL-wins,
    # e.g. ["organizationname", "lastname"] tries org first then individual.
    name_columns: tuple = ()
    # individual_name_columns assemble first/middle/last when no org name is
    # present. Order: first, middle, last, suffix.
    individual_name_columns: tuple = ()
    zip_column: str | None = None
    state_column: str | None = None

    # --- Identity-spine hints — DENORMALIZED streams -----------------------
    debtor_name_columns: tuple = ()
    debtor_individual_name_columns: tuple = ()
    debtor_zip_column: str | None = None
    debtor_state_column: str | None = None
    secured_party_name_columns: tuple = ()
    secured_party_individual_name_columns: tuple = ()
    secured_party_zip_column: str | None = None
    secured_party_state_column: str | None = None

    # --- Collateral-stream identity hint -----------------------------------
    collateral_description_column: str | None = None

    # --- Date columns to attempt typed DATE cast at projection -------------
    # Socrata Calendar Date columns serialize as 'YYYY-MM-DDTHH:MM:SS.000'.
    date_columns: tuple = ()

    @property
    def csv_url(self) -> str:
        """Socrata bulk CSV-export URL (full dataset, no pagination)."""
        return f"https://{self.domain}/api/views/{self.socrata_id}/rows.csv?accessType=DOWNLOAD"

    @property
    def metadata_url(self) -> str:
        """Socrata view-metadata API — used for HEAD-equivalent freshness check."""
        return f"https://{self.domain}/api/views/{self.socrata_id}.json"


# --------------------------------------------------------------------------- #
# Phase 1 streams
# --------------------------------------------------------------------------- #

STREAMS: Final[tuple[StreamConfig, ...]] = (
    # ----- Colorado (4 datasets, normalized via fileid) --------------------
    StreamConfig(
        state="CO",
        name="filings",
        domain="data.colorado.gov",
        socrata_id="wffy-3uut",
        title="Uniform Commercial Code (UCC) Filing Information in Colorado",
        kind="filing",
        date_columns=("filingdate", "lapsedate"),
    ),
    StreamConfig(
        state="CO",
        name="debtors",
        domain="data.colorado.gov",
        socrata_id="8upq-58vz",
        title="Uniform Commercial Code (UCC) Debtor Information in Colorado",
        kind="party",
        party_role="debtor",
        name_columns=("organizationname",),
        individual_name_columns=("firstname", "middlename", "lastname", "suffix"),
        zip_column="zipcode",
        state_column="state",
    ),
    StreamConfig(
        state="CO",
        name="secured_parties",
        domain="data.colorado.gov",
        socrata_id="ap62-sav4",
        title="Secured Party Information in Colorado",
        kind="party",
        party_role="secured_party",
        name_columns=("organizationname",),
        individual_name_columns=("firstname", "middlename", "lastname", "suffix"),
        zip_column="zipcode",
        state_column="state",
    ),
    StreamConfig(
        state="CO",
        name="collateral",
        domain="data.colorado.gov",
        socrata_id="4am6-w6u4",
        title="Uniform Commercial Code (UCC) Collateral Information in Colorado",
        kind="collateral",
        collateral_description_column="collateraldescription",
    ),

    # ----- Connecticut (1 dataset, denormalized) ---------------------------
    StreamConfig(
        state="CT",
        name="liens",
        domain="data.ct.gov",
        socrata_id="xfev-8smz",
        title="Uniform Commercial Code (UCC) Lien Filings",
        kind="denormalized",
        debtor_name_columns=("debtor_nm_bus",),
        debtor_individual_name_columns=(
            "debtor_nm_first", "debtor_nm_mid", "debtor_nm_last", "debtor_nm_suff",
        ),
        debtor_zip_column="debtor_ad_zip",
        debtor_state_column="debtor_ad_state",
        secured_party_name_columns=("sec_party_nm_bus",),
        secured_party_individual_name_columns=(
            "sec_party_nm_first", "sec_party_nm_mid", "sec_party_nm_last", "sec_party_nm_suff",
        ),
        secured_party_zip_column="sec_party_ad_zip",
        secured_party_state_column="sec_party_ad_state",
        date_columns=("dt_accept", "dt_lapse"),
    ),

    # ----- Oregon (2 datasets) ---------------------------------------------
    StreamConfig(
        state="OR",
        name="secured_parties",
        domain="data.oregon.gov",
        socrata_id="2kf7-i54h",
        title="UCC Secured Parties List (Oregon)",
        kind="party",
        party_role="secured_party",
        # Single combined `secured_party` column carries org names — there is
        # no separate first/last for individual filings here. The normalizer
        # will treat it like a party_name input.
        name_columns=("secured_party",),
        zip_column="postalcode",
        state_column="state",
        date_columns=("filing_date", "lapse_date"),
    ),
    StreamConfig(
        state="OR",
        name="filings_last_month",
        domain="data.oregon.gov",
        socrata_id="snfi-f79b",
        title="UCC List of Filings Entered Last Month (Oregon)",
        # Per-row carries one party (DB or SP) attached to a filing. We split
        # by party_type at ingest into two roles via party_role_map, treating
        # the stream as kind=party (single-role per row).
        kind="party",
        party_role_column="party_type",
        party_role_map={
            "DB": "debtor",
            "SP": "secured_party",
        },
        name_columns=("entity",),
        zip_column="zip_code_txt",
        state_column="st_cd_txt",
        date_columns=("filing_date", "lapse_date"),
    ),
)


def streams_for_state(code: str) -> tuple[StreamConfig, ...]:
    """Return the streams configured for a given state code."""
    return tuple(s for s in STREAMS if s.state == code.upper())


def stream_by_id(socrata_id: str) -> StreamConfig | None:
    """Lookup a stream by its Socrata 4×4 id."""
    for s in STREAMS:
        if s.socrata_id == socrata_id:
            return s
    return None


# Sanity self-check (catches typos at module import in dev).
def _validate() -> None:
    seen = set()
    for s in STREAMS:
        key = (s.state, s.name)
        if key in seen:
            raise ValueError(f"Duplicate stream key: {key}")
        seen.add(key)
        if s.kind == "party" and s.party_role is None and s.party_role_column is None:
            raise ValueError(
                f"{s.state}/{s.name}: PARTY-kind stream missing both party_role "
                "and party_role_column"
            )
        if s.kind == "party" and s.party_role_column and not s.party_role_map:
            raise ValueError(
                f"{s.state}/{s.name}: dynamic party_role_column without party_role_map"
            )
        if s.kind == "denormalized" and not (
            s.debtor_name_columns or s.debtor_individual_name_columns
        ):
            raise ValueError(
                f"{s.state}/{s.name}: DENORMALIZED stream missing debtor name columns"
            )


_validate()
