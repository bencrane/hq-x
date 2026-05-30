#!/usr/bin/env python3
"""EPA NPDES Construction General Permit (CGP) — bulk-CSV ingest from ECHO.

Lands the construction-site subset of the EPA ICIS-NPDES bulk download into
five source tables under the entities schema. Source-first per CLAUDE.md
(2026-04-16): no identity resolution, no canonical merge.

Source: one ZIP at https://echo.epa.gov/files/echodownloads/npdes_downloads.zip
   ICIS_PERMITS.csv             -> entities.source_epa_icis_permits_cgp
   ICIS_FACILITIES.csv          -> entities.source_epa_icis_facilities_cgp
   NPDES_NAICS.csv              -> entities.source_epa_npdes_naics_cgp
   NPDES_SICS.csv               -> entities.source_epa_npdes_sics_cgp
   NPDES_PERM_COMPONENTS.csv    -> entities.source_epa_npdes_perm_components_swc
                                    (SWC subset only — the canonical CGP filter)

CGP filter:
   EXTERNAL_PERMIT_NMBR is in the SWC subset of NPDES_PERM_COMPONENTS
   (COMPONENT_TYPE_CODE = 'SWC' = "Storm Water Construction"). We do a
   single pre-pass over NPDES_PERM_COMPONENTS to load the SWC permit set
   into RAM, then filter every other CSV against that set in-Python so
   the dedicated source tables hold only CGP rows.

Idempotency: ON CONFLICT DO UPDATE on PK. Re-running refreshes any row
EPA has corrected. EPA rebuilds the bundle weekly.

Audit: ops.epa_npdes_ingest_runs.
Skip-if-unchanged: HEAD Last-Modified compared to prior successful run.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_npdes_cgp_ingest.py all
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_npdes_cgp_ingest.py all --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_npdes_cgp_ingest.py all --dry-run --max-rows 1000
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_npdes_cgp_ingest.py perm-components-swc
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_npdes_cgp_ingest.py all --recon-only
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx
import psycopg
from psycopg.types.json import Jsonb


BUNDLE_URL = "https://echo.epa.gov/files/echodownloads/npdes_downloads.zip"
DEFAULT_BATCH_SIZE = 50_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# Some rows in EPA's bulk CSVs contain stray NUL bytes; strip them per-line.
def _denul(stream: Iterable[str]) -> Iterator[str]:
    for line in stream:
        yield line.replace("\x00", "")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("epa-npdes-cgp-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Per-CSV configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DatasetConfig:
    key: str                     # CLI subcommand
    dataset_form: str            # Audit-table value
    csv_name: str                # ZIP member name
    schema: str
    table: str
    csv_cols: list[str]          # uppercased CSV header names to pull
    target_cols: list[str]       # postgres column names (same order, lowercased)
    numeric_cols: set[str]       # postgres columns to coerce '' -> None for numeric
    pk_cols: list[str]           # postgres PK columns (subset of target_cols)
    swc_filter_csv_col: str      # CSV column to filter against the SWC permit set ("" = no filter)

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"


# Column lists — derived from the ECHO bulk CSV headers (recon 2026-05-01).
ICIS_PERMITS_CSV_COLS = [
    "ACTIVITY_ID", "EXTERNAL_PERMIT_NMBR", "VERSION_NMBR",
    "FACILITY_TYPE_INDICATOR", "PERMIT_TYPE_CODE",
    "MAJOR_MINOR_STATUS_FLAG", "PERMIT_STATUS_CODE",
    "TOTAL_DESIGN_FLOW_NMBR", "ACTUAL_AVERAGE_FLOW_NMBR",
    "STATE_WATER_BODY", "STATE_WATER_BODY_NAME",
    "PERMIT_NAME", "AGENCY_TYPE_CODE",
    "ORIGINAL_ISSUE_DATE", "ISSUE_DATE", "ISSUING_AGENCY",
    "EFFECTIVE_DATE", "EXPIRATION_DATE", "RETIREMENT_DATE",
    "TERMINATION_DATE", "PERMIT_COMP_STATUS_FLAG",
    "DMR_NON_RECEIPT_FLAG", "RNC_TRACKING_FLAG",
    "MASTER_EXTERNAL_PERMIT_NMBR", "TMDL_INTERFACE_FLAG",
    "EDMR_AUTHORIZATION_FLAG", "PRETREATMENT_INDICATOR_CODE",
    "RAD_WBD_HUC12S",
]

ICIS_PERMITS = DatasetConfig(
    key="permits",
    dataset_form="ICIS_PERMITS",
    csv_name="ICIS_PERMITS.csv",
    schema="entities",
    table="source_epa_icis_permits_cgp",
    csv_cols=ICIS_PERMITS_CSV_COLS,
    target_cols=[c.lower() for c in ICIS_PERMITS_CSV_COLS],
    numeric_cols={"total_design_flow_nmbr", "actual_average_flow_nmbr"},
    pk_cols=["external_permit_nmbr", "version_nmbr"],
    swc_filter_csv_col="EXTERNAL_PERMIT_NMBR",
)


ICIS_FACILITIES_CSV_COLS = [
    "ICIS_FACILITY_INTEREST_ID", "NPDES_ID", "FACILITY_UIN",
    "FACILITY_TYPE_CODE", "FACILITY_NAME",
    "LOCATION_ADDRESS", "SUPPLEMENTAL_ADDRESS_TEXT",
    "CITY", "COUNTY_CODE", "STATE_CODE", "ZIP",
    "GEOCODE_LATITUDE", "GEOCODE_LONGITUDE", "IMPAIRED_WATERS",
]

# Reorder so PK column (npdes_id) is first in the postgres column list — keeps
# COPY/UPSERT readable. The CSV order is preserved by csv_cols; we map to the
# desired postgres-column-order via target_cols.
ICIS_FACILITIES_TARGET_COLS = [
    "npdes_id", "icis_facility_interest_id", "facility_uin",
    "facility_type_code", "facility_name",
    "location_address", "supplemental_address_text",
    "city", "county_code", "state_code", "zip",
    "geocode_latitude", "geocode_longitude", "impaired_waters",
]
# Map each target_col -> the CSV column we pull it from (uppercased).
ICIS_FACILITIES_CSV_FOR_TARGET = {
    "npdes_id": "NPDES_ID",
    "icis_facility_interest_id": "ICIS_FACILITY_INTEREST_ID",
    "facility_uin": "FACILITY_UIN",
    "facility_type_code": "FACILITY_TYPE_CODE",
    "facility_name": "FACILITY_NAME",
    "location_address": "LOCATION_ADDRESS",
    "supplemental_address_text": "SUPPLEMENTAL_ADDRESS_TEXT",
    "city": "CITY",
    "county_code": "COUNTY_CODE",
    "state_code": "STATE_CODE",
    "zip": "ZIP",
    "geocode_latitude": "GEOCODE_LATITUDE",
    "geocode_longitude": "GEOCODE_LONGITUDE",
    "impaired_waters": "IMPAIRED_WATERS",
}

ICIS_FACILITIES = DatasetConfig(
    key="facilities",
    dataset_form="ICIS_FACILITIES",
    csv_name="ICIS_FACILITIES.csv",
    schema="entities",
    table="source_epa_icis_facilities_cgp",
    csv_cols=[ICIS_FACILITIES_CSV_FOR_TARGET[t] for t in ICIS_FACILITIES_TARGET_COLS],
    target_cols=ICIS_FACILITIES_TARGET_COLS,
    numeric_cols={"geocode_latitude", "geocode_longitude"},
    pk_cols=["npdes_id"],
    swc_filter_csv_col="NPDES_ID",
)


NPDES_NAICS_CSV_COLS = ["NPDES_ID", "NAICS_CODE", "NAICS_DESC", "PRIMARY_INDICATOR_FLAG"]
NPDES_NAICS = DatasetConfig(
    key="naics",
    dataset_form="NPDES_NAICS",
    csv_name="NPDES_NAICS.csv",
    schema="entities",
    table="source_epa_npdes_naics_cgp",
    csv_cols=NPDES_NAICS_CSV_COLS,
    target_cols=["npdes_id", "naics_code", "naics_desc", "primary_indicator_flag"],
    numeric_cols=set(),
    pk_cols=["npdes_id", "naics_code"],
    swc_filter_csv_col="NPDES_ID",
)


NPDES_SICS_CSV_COLS = ["NPDES_ID", "SIC_CODE", "SIC_DESC", "PRIMARY_INDICATOR_FLAG"]
NPDES_SICS = DatasetConfig(
    key="sics",
    dataset_form="NPDES_SICS",
    csv_name="NPDES_SICS.csv",
    schema="entities",
    table="source_epa_npdes_sics_cgp",
    csv_cols=NPDES_SICS_CSV_COLS,
    target_cols=["npdes_id", "sic_code", "sic_desc", "primary_indicator_flag"],
    numeric_cols=set(),
    pk_cols=["npdes_id", "sic_code"],
    swc_filter_csv_col="NPDES_ID",
)


NPDES_PERM_COMPONENTS_CSV_COLS = [
    "EXTERNAL_PERMIT_NMBR", "COMPONENT_TYPE_CODE", "COMPONENT_TYPE_DESC",
]
NPDES_PERM_COMPONENTS_SWC = DatasetConfig(
    key="perm-components-swc",
    dataset_form="NPDES_PERM_COMPONENTS_SWC",
    csv_name="NPDES_PERM_COMPONENTS.csv",
    schema="entities",
    table="source_epa_npdes_perm_components_swc",
    csv_cols=NPDES_PERM_COMPONENTS_CSV_COLS,
    target_cols=["external_permit_nmbr", "component_type_code", "component_type_desc"],
    numeric_cols=set(),
    pk_cols=["external_permit_nmbr", "component_type_code"],
    swc_filter_csv_col="",   # this table builds the SWC set; filter is in-pass
)


DATASETS: dict[str, DatasetConfig] = {
    d.key: d for d in (
        NPDES_PERM_COMPONENTS_SWC,   # first — supplies the SWC set
        ICIS_PERMITS,
        ICIS_FACILITIES,
        NPDES_NAICS,
        NPDES_SICS,
    )
}


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED is not set in the environment.")
    return url


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            cl = int(r.headers.get("content-length", 0)) or None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(lm_raw, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            return cl, lm
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=900.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss",
                                url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed: {last_exc}")


# --------------------------------------------------------------------------- #
# CSV → Postgres COPY pipeline
# --------------------------------------------------------------------------- #


def open_csv_in_zip(zip_path: Path, member_name: str) -> tuple[zipfile.ZipFile, io.TextIOWrapper]:
    z = zipfile.ZipFile(zip_path)
    if member_name not in z.namelist():
        z.close()
        raise RuntimeError(
            f"Member {member_name!r} not found in {zip_path.name}; "
            f"contents: {z.namelist()}"
        )
    fh = io.TextIOWrapper(
        z.open(member_name, "r"),
        encoding="utf-8", errors="replace", newline="",
    )
    return z, fh


def stage_create_sql(cfg: DatasetConfig) -> str:
    cols = ",\n  ".join(
        f"{c} {'numeric' if c in cfg.numeric_cols else 'text'}"
        for c in cfg.target_cols
    )
    return f"""
CREATE TEMP TABLE IF NOT EXISTS {cfg.stage_table} (
  {cols},
  source_file_last_modified timestamptz
);
"""


def truncate_stage_sql(cfg: DatasetConfig) -> str:
    return f"TRUNCATE {cfg.stage_table};"


def copy_sql(cfg: DatasetConfig) -> str:
    cols = list(cfg.target_cols) + ["source_file_last_modified"]
    return f"COPY {cfg.stage_table} ({', '.join(cols)}) FROM STDIN"


def upsert_from_stage_sql(cfg: DatasetConfig) -> str:
    natural_cols = list(cfg.target_cols)
    target_cols = natural_cols + ["source_file_last_modified", "ingested_at"]
    select_cols = natural_cols + ["source_file_last_modified", "now()"]
    pk = ", ".join(cfg.pk_cols)
    update_cols = [c for c in natural_cols if c not in cfg.pk_cols]
    if update_cols:
        update_assigns = ",\n      ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        update_assigns += ",\n      source_file_last_modified = EXCLUDED.source_file_last_modified"
        update_assigns += ",\n      ingested_at = now()"
        where_clause = " OR ".join(
            f"{cfg.fully_qualified}.{c} IS DISTINCT FROM EXCLUDED.{c}"
            for c in update_cols + ["source_file_last_modified"]
        )
        do_clause = f"DO UPDATE SET\n      {update_assigns}\n   WHERE {where_clause}"
    else:
        # No non-PK columns to update — only refresh source_file_last_modified.
        do_clause = (
            "DO UPDATE SET\n      "
            "source_file_last_modified = EXCLUDED.source_file_last_modified,\n      "
            "ingested_at = now()\n   "
            f"WHERE {cfg.fully_qualified}.source_file_last_modified IS DISTINCT FROM EXCLUDED.source_file_last_modified"
        )
    # SELECT DISTINCT ON (pk) collapses duplicates within the same chunk —
    # NPDES_NAICS / NPDES_SICS ship 8K-21K duplicate (npdes_id, code) rows
    # which would otherwise fail ON CONFLICT with CardinalityViolation.
    return f"""
WITH deduped AS (
  SELECT DISTINCT ON ({pk}) {', '.join(select_cols)}
    FROM {cfg.stage_table}
   ORDER BY {pk}, ctid
), upserted AS (
  INSERT INTO {cfg.fully_qualified} ({', '.join(target_cols)})
  SELECT * FROM deduped
   ON CONFLICT ({pk}) {do_clause}
   RETURNING (xmax = 0) AS inserted
)
SELECT
  count(*) FILTER (WHERE inserted)     AS rows_inserted,
  count(*) FILTER (WHERE NOT inserted) AS rows_updated
FROM upserted;
"""


def copy_chunk_to_stage(
    conn: psycopg.Connection,
    cfg: DatasetConfig,
    rows: list[tuple[Any, ...]],
) -> tuple[int, int]:
    if not rows:
        return 0, 0
    with conn.cursor() as cur:
        cur.execute(truncate_stage_sql(cfg))
        with cur.copy(copy_sql(cfg)) as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute(upsert_from_stage_sql(cfg))
        ins, upd = cur.fetchone()
    conn.commit()
    return int(ins), int(upd)


def stream_csv_to_db(
    conn: psycopg.Connection,
    cfg: DatasetConfig,
    csv_fh: io.TextIOWrapper,
    *,
    source_file_last_modified: datetime | None,
    batch_size: int,
    log_prefix: str,
    swc_set: set[str] | None,
    max_rows: int | None,
    perm_components_swc_only: bool,
) -> tuple[int, int, int, int]:
    """Returns (inserted, updated, rows_seen_pre_filter, rows_filtered_post)."""
    reader = csv.reader(_denul(csv_fh))
    try:
        header = next(reader)
    except StopIteration:
        return 0, 0, 0, 0
    header_upper = [h.strip().upper() for h in header]

    expected = set(cfg.csv_cols)
    missing = sorted(expected - set(header_upper))
    extra = sorted(set(header_upper) - expected)
    if missing:
        log.warning("%s CSV missing %d columns expected by migration: %s",
                    log_prefix, len(missing), missing[:10])
    if extra:
        log.info("%s CSV has %d columns not in migration (will be dropped): %s",
                 log_prefix, len(extra), extra[:10])

    csv_indexes = [header_upper.index(c) if c in header_upper else None
                   for c in cfg.csv_cols]
    swc_filter_idx: int | None = None
    if cfg.swc_filter_csv_col and swc_set is not None:
        swc_filter_idx = header_upper.index(cfg.swc_filter_csv_col)

    rows_seen = rows_filtered = total_inserted = total_updated = 0
    chunk: list[tuple[Any, ...]] = []
    page_started = time.monotonic()
    for raw in reader:
        rows_seen += 1
        if max_rows is not None and rows_filtered >= max_rows:
            break
        if perm_components_swc_only:
            # NPDES_PERM_COMPONENTS table — keep only SWC rows.
            try:
                comp_idx = header_upper.index("COMPONENT_TYPE_CODE")
            except ValueError:
                comp_idx = -1
            if comp_idx == -1 or comp_idx >= len(raw) or raw[comp_idx] != "SWC":
                continue
        if swc_filter_idx is not None:
            if swc_filter_idx >= len(raw) or raw[swc_filter_idx] not in swc_set:
                continue
        rows_filtered += 1
        out: list[Any] = []
        for col_name, idx in zip(cfg.target_cols, csv_indexes):
            if idx is None or idx >= len(raw):
                out.append(None)
                continue
            v = raw[idx]
            if v is None or v == "":
                out.append(None)
            else:
                out.append(v)
        out.append(source_file_last_modified)
        chunk.append(tuple(out))
        if len(chunk) >= batch_size:
            ins, upd = copy_chunk_to_stage(conn, cfg, chunk)
            total_inserted += ins
            total_updated += upd
            log.info(
                "%s chunk: rows_seen=%d filtered=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
                log_prefix, rows_seen, rows_filtered, ins, upd,
                total_inserted, total_updated,
                time.monotonic() - page_started,
            )
            chunk.clear()
            page_started = time.monotonic()
    if chunk:
        ins, upd = copy_chunk_to_stage(conn, cfg, chunk)
        total_inserted += ins
        total_updated += upd
        log.info(
            "%s final chunk: rows_seen=%d filtered=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
            log_prefix, rows_seen, rows_filtered, ins, upd,
            total_inserted, total_updated,
            time.monotonic() - page_started,
        )
    return total_inserted, total_updated, rows_seen, rows_filtered


# --------------------------------------------------------------------------- #
# SWC permit-set loader
# --------------------------------------------------------------------------- #


def load_swc_permit_set(zip_path: Path) -> set[str]:
    """Single-pass over NPDES_PERM_COMPONENTS.csv to load SWC permit numbers."""
    log.info("loading SWC permit set from NPDES_PERM_COMPONENTS.csv")
    started = time.monotonic()
    z, fh = open_csv_in_zip(zip_path, "NPDES_PERM_COMPONENTS.csv")
    try:
        with z, fh:
            reader = csv.reader(_denul(fh))
            header = next(reader)
            header_upper = [h.strip().upper() for h in header]
            idx_p = header_upper.index("EXTERNAL_PERMIT_NMBR")
            idx_c = header_upper.index("COMPONENT_TYPE_CODE")
            swc: set[str] = set()
            for row in reader:
                if len(row) <= max(idx_p, idx_c):
                    continue
                if row[idx_c] == "SWC":
                    swc.add(row[idx_p])
    finally:
        pass
    log.info("SWC permit set loaded: %d permits in %.1fs",
             len(swc), time.monotonic() - started)
    return swc


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    cfg: DatasetConfig,
    *,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.epa_npdes_ingest_runs (
        dataset_form, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cfg.dataset_form, url, source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, cfg: DatasetConfig,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.epa_npdes_ingest_runs
             WHERE dataset_form = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (cfg.dataset_form,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    cfg: DatasetConfig,
    *,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.epa_npdes_ingest_runs (
                dataset_form, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                cfg.dataset_form, url, source_last_modified,
                prior_source_last_modified, started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes: int,
    csv_bytes: int,
    rows_in_csv: int,
    rows_filtered: int,
    rows_inserted: int,
    rows_updated: int,
    rows_unchanged: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.epa_npdes_ingest_runs
               SET status = %s, zip_bytes_downloaded = %s,
                   csv_bytes_extracted = %s, rows_in_csv = %s,
                   rows_filtered = %s,
                   rows_inserted = %s, rows_updated = %s, rows_unchanged = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, rows_in_csv, rows_filtered,
            rows_inserted, rows_updated, rows_unchanged,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Recon report
# --------------------------------------------------------------------------- #


@dataclass
class ReconStats:
    key: str
    table_fqn: str
    total_rows: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


def gather_recon_permits(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(key="permits", table_fqn=ICIS_PERMITS.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {ICIS_PERMITS.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT permit_status_code, count(*) c
              FROM {ICIS_PERMITS.fully_qualified}
             GROUP BY permit_status_code ORDER BY c DESC;
        """)
        s.notes["by_status"] = {(r[0] or "(blank)"): int(r[1]) for r in cur.fetchall()}
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE permit_status_code IN ('EFF','ADC')),
              count(DISTINCT external_permit_nmbr),
              count(*) FILTER (WHERE permit_type_code = 'GPC'),
              count(*) FILTER (WHERE permit_type_code = 'NPD'),
              count(*) FILTER (WHERE master_external_permit_nmbr IS NOT NULL)
              FROM {ICIS_PERMITS.fully_qualified};
        """)
        active, distinct_perm, gpc, npd, has_master = cur.fetchone()
        s.notes["active_eff_or_adc"] = int(active)
        s.notes["distinct_external_permit_nmbrs"] = int(distinct_perm)
        s.notes["permit_type_gpc"] = int(gpc)
        s.notes["permit_type_npd_individual"] = int(npd)
        s.notes["with_master_general_permit"] = int(has_master)
        cur.execute(f"""
            SELECT agency_type_code, count(*) c
              FROM {ICIS_PERMITS.fully_qualified}
             GROUP BY agency_type_code ORDER BY c DESC LIMIT 10;
        """)
        s.notes["agency_type_breakdown"] = [
            {"code": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT master_external_permit_nmbr, count(*) c
              FROM {ICIS_PERMITS.fully_qualified}
             WHERE master_external_permit_nmbr IS NOT NULL
             GROUP BY master_external_permit_nmbr ORDER BY c DESC LIMIT 15;
        """)
        s.notes["top_master_general_permits"] = [
            {"master_permit": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
        # Top 15 PERMIT_NAME by row count — the "named operator" sanity check
        # (caveat: these are mostly site/project names, not GC names).
        cur.execute(f"""
            SELECT permit_name, count(*) c
              FROM {ICIS_PERMITS.fully_qualified}
             WHERE permit_name IS NOT NULL AND permit_name <> ''
             GROUP BY permit_name ORDER BY c DESC LIMIT 15;
        """)
        s.notes["top_permit_names"] = [
            {"name": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
    return s


def gather_recon_facilities(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(key="facilities", table_fqn=ICIS_FACILITIES.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {ICIS_FACILITIES.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE facility_name IS NOT NULL AND facility_name <> ''),
              count(*) FILTER (WHERE location_address IS NOT NULL AND location_address <> ''),
              count(*) FILTER (WHERE geocode_latitude IS NOT NULL),
              count(DISTINCT facility_name)
              FROM {ICIS_FACILITIES.fully_qualified};
        """)
        nm, addr, geo, distinct_nm = cur.fetchone()
        s.notes["facility_name_populated"] = int(nm)
        s.notes["location_address_populated"] = int(addr)
        s.notes["geocoded"] = int(geo)
        s.notes["distinct_facility_names"] = int(distinct_nm)
        cur.execute(f"""
            SELECT state_code, count(*) c
              FROM {ICIS_FACILITIES.fully_qualified}
             WHERE state_code IS NOT NULL AND state_code <> ''
             GROUP BY state_code ORDER BY c DESC LIMIT 10;
        """)
        s.notes["top_states"] = [
            {"state": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT count(DISTINCT state_code) FROM {ICIS_FACILITIES.fully_qualified}
              WHERE state_code IS NOT NULL AND state_code <> '';
        """)
        s.notes["distinct_states"] = int(cur.fetchone()[0])
        # Top 15 facility names by NOI count — the named-operator sanity check
        # for the directive deliverable (h).
        cur.execute(f"""
            SELECT facility_name, count(*) c
              FROM {ICIS_FACILITIES.fully_qualified}
             WHERE facility_name IS NOT NULL AND facility_name <> ''
             GROUP BY facility_name ORDER BY c DESC LIMIT 15;
        """)
        s.notes["top_facility_names_by_noi_count"] = [
            {"name": r[0], "noi_count": int(r[1])} for r in cur.fetchall()
        ]
        # Date-range proxy via joined permits table (effective_date as text).
        cur.execute(f"""
            SELECT min(effective_date), max(effective_date)
              FROM {ICIS_PERMITS.fully_qualified}
             WHERE effective_date IS NOT NULL AND effective_date <> '';
        """)
        mn, mx = cur.fetchone()
        s.notes["effective_date_min_max_in_permits"] = {"min": mn, "max": mx}
    return s


def gather_recon_naics(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(key="naics", table_fqn=NPDES_NAICS.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {NPDES_NAICS.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT count(DISTINCT npdes_id),
                   count(*) FILTER (WHERE primary_indicator_flag = 'Y')
              FROM {NPDES_NAICS.fully_qualified};
        """)
        d, p = cur.fetchone()
        s.notes["distinct_permits_with_naics"] = int(d)
        s.notes["primary_naics_rows"] = int(p)
        cur.execute(f"""
            SELECT naics_code, naics_desc, count(*) c
              FROM {NPDES_NAICS.fully_qualified}
             GROUP BY naics_code, naics_desc ORDER BY c DESC LIMIT 15;
        """)
        s.notes["top_naics"] = [
            {"code": r[0], "desc": r[1], "count": int(r[2])} for r in cur.fetchall()
        ]
    return s


def gather_recon_sics(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(key="sics", table_fqn=NPDES_SICS.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {NPDES_SICS.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT count(DISTINCT npdes_id),
                   count(*) FILTER (WHERE primary_indicator_flag = 'Y')
              FROM {NPDES_SICS.fully_qualified};
        """)
        d, p = cur.fetchone()
        s.notes["distinct_permits_with_sic"] = int(d)
        s.notes["primary_sic_rows"] = int(p)
        cur.execute(f"""
            SELECT sic_code, sic_desc, count(*) c
              FROM {NPDES_SICS.fully_qualified}
             GROUP BY sic_code, sic_desc ORDER BY c DESC LIMIT 15;
        """)
        s.notes["top_sic"] = [
            {"code": r[0], "desc": r[1], "count": int(r[2])} for r in cur.fetchall()
        ]
    return s


def gather_recon_perm_components_swc(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(key="perm-components-swc",
                   table_fqn=NPDES_PERM_COMPONENTS_SWC.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {NPDES_PERM_COMPONENTS_SWC.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT count(DISTINCT external_permit_nmbr)
              FROM {NPDES_PERM_COMPONENTS_SWC.fully_qualified};
        """)
        s.notes["distinct_swc_permits"] = int(cur.fetchone()[0])
    return s


RECON_FNS = [
    gather_recon_perm_components_swc,
    gather_recon_permits,
    gather_recon_facilities,
    gather_recon_naics,
    gather_recon_sics,
]


def print_recon(s: ReconStats) -> None:
    print(f"=== RECON: {s.key}  ({s.table_fqn}) ===")
    print(f"  total rows: {s.total_rows:,}")
    for k, v in s.notes.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for kk, vv in v.items():
                if isinstance(vv, int):
                    print(f"      {kk}: {vv:,}")
                else:
                    print(f"      {kk}: {vv}")
        elif isinstance(v, list):
            print(f"  {k}:")
            for item in v:
                print(f"      {item}")
        elif isinstance(v, int):
            print(f"  {k}: {v:,}")
        elif isinstance(v, float):
            print(f"  {k}: {v:,.2f}")
        else:
            print(f"  {k}: {v}")
    print(f"=== END RECON ===\n")


def run_recon_only() -> None:
    with psycopg.connect(_database_url()) as conn:
        for fn in RECON_FNS:
            try:
                s = fn(conn)
                print_recon(s)
            except psycopg.errors.UndefinedTable:
                log.error("Table missing — apply the migration first.")
                return


# --------------------------------------------------------------------------- #
# Per-dataset main
# --------------------------------------------------------------------------- #


def ensure_stage_table(conn: psycopg.Connection, cfg: DatasetConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(stage_create_sql(cfg))
    conn.commit()


def ingest_one(
    cfg: DatasetConfig,
    *,
    zip_path: Path,
    source_last_modified: datetime | None,
    zip_bytes: int,
    swc_set: set[str] | None,
    batch_size: int,
    skip_if_unchanged: bool,
    dry_run: bool,
    max_rows: int | None,
) -> int:
    log_prefix = f"[{cfg.key}]"
    started_wall = time.monotonic()
    log.info("%s start csv=%s", log_prefix, cfg.csv_name)

    if dry_run:
        log.info("%s DRY RUN — inspecting CSV header only", log_prefix)
        z, fh = open_csv_in_zip(zip_path, cfg.csv_name)
        with z, fh:
            header_line = fh.readline()
            cols = header_line.rstrip("\n").split(",")
            log.info("%s CSV cols=%d header=%s", log_prefix, len(cols), cols[:8])
            if max_rows is not None and swc_set is not None:
                # Show the SWC-filter hit rate over a small sample.
                reader = csv.reader(_denul(fh))
                idx_swc = (
                    cols and cfg.swc_filter_csv_col
                    and [c.strip().strip('"').upper() for c in cols].index(cfg.swc_filter_csv_col)
                    if cfg.swc_filter_csv_col else None
                )
                sample_seen = sample_kept = 0
                for row in reader:
                    sample_seen += 1
                    if sample_seen > max_rows: break
                    if idx_swc is not None and idx_swc < len(row) and row[idx_swc] in swc_set:
                        sample_kept += 1
                log.info("%s sample %d rows -> %d would pass SWC filter", log_prefix, sample_seen-1, sample_kept)
        return 0

    with psycopg.connect(_database_url()) as conn:
        prior = get_prior_source_last_modified(conn, cfg)
        log.info("%s prior source_last_modified: %s", log_prefix, prior)
        if (
            skip_if_unchanged
            and prior is not None
            and source_last_modified is not None
            and source_last_modified <= prior
        ):
            log.info("%s source_last_modified unchanged — recording no_change", log_prefix)
            write_no_change_run(
                conn, cfg, url=BUNDLE_URL,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            return 0

        run_id = insert_run_row(
            conn, cfg, url=BUNDLE_URL,
            source_last_modified=source_last_modified,
            prior_source_last_modified=prior,
        )
        log.info("%s run id: %s", log_prefix, run_id)
        ensure_stage_table(conn, cfg)

        try:
            z, fh = open_csv_in_zip(zip_path, cfg.csv_name)
            with z, fh:
                csv_bytes = z.getinfo(cfg.csv_name).file_size
                log.info("%s extracting %s (%d bytes uncompressed)",
                         log_prefix, cfg.csv_name, csv_bytes)
                ins, upd, rows_seen, rows_filtered = stream_csv_to_db(
                    conn, cfg, fh,
                    source_file_last_modified=source_last_modified,
                    batch_size=batch_size,
                    log_prefix=log_prefix,
                    swc_set=swc_set,
                    max_rows=max_rows,
                    perm_components_swc_only=(cfg.key == "perm-components-swc"),
                )

            finalize_run_row(
                conn, run_id, status="completed",
                zip_bytes=zip_bytes, csv_bytes=csv_bytes,
                rows_in_csv=rows_seen, rows_filtered=rows_filtered,
                rows_inserted=ins, rows_updated=upd,
                rows_unchanged=max(0, rows_filtered - ins - upd),
                started_at=started_wall, error_message=None,
                notes={"max_rows": max_rows} if max_rows else None,
            )
            log.info(
                "%s DONE rows_in_csv=%d filtered=%d ins=%d upd=%d unch=%d wall=%.1fs",
                log_prefix, rows_seen, rows_filtered, ins, upd,
                max(0, rows_filtered - ins - upd),
                time.monotonic() - started_wall,
            )
            return 0
        except Exception as exc:
            log.exception("%s ingest failed", log_prefix)
            try:
                conn.rollback()
            except Exception:
                pass
            finalize_run_row(
                conn, run_id, status="failed",
                zip_bytes=zip_bytes, csv_bytes=0, rows_in_csv=0, rows_filtered=0,
                rows_inserted=0, rows_updated=0, rows_unchanged=0,
                started_at=started_wall, error_message=str(exc), notes=None,
            )
            return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dataset", choices=list(DATASETS.keys()) + ["all"],
                   help="Dataset key or 'all'.")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help="Rows per COPY chunk (default: 50000).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if bundle Last-Modified has not advanced "
                        "since the prior successful run for this dataset.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD + download + read CSV header only; no DB writes.")
    p.add_argument("--recon-only", action="store_true",
                   help="Run recon SELECTs against existing table contents and exit.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Smoke-test cap on rows landed PER TABLE (post-filter).")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP downloads "
                        "(default: /tmp/epa_npdes_cgp_ingest).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.recon_only:
        run_recon_only()
        return 0

    datasets = list(DATASETS.values()) if args.dataset == "all" else [DATASETS[args.dataset]]

    workdir = Path(args.workdir or "/tmp/epa_npdes_cgp_ingest")
    workdir.mkdir(parents=True, exist_ok=True)
    zip_path = workdir / "npdes_downloads.zip"

    rc = 0
    with httpx.Client(headers={"User-Agent": "data-engine-x/epa-npdes-cgp-ingest"}) as client:
        try:
            content_length, source_last_modified = head_url(client, BUNDLE_URL)
        except Exception:
            log.exception("HEAD failed")
            return 1
        log.info("HEAD content_length=%s last_modified=%s",
                 content_length, source_last_modified)

        # Skip-if-unchanged early exit: if every selected dataset has a
        # completed run with the same Last-Modified, we don't even need
        # to download. We check per-dataset inside ingest_one anyway, but
        # this saves a 338 MB download when nothing has changed.
        if args.skip_if_unchanged and not args.dry_run and source_last_modified is not None:
            with psycopg.connect(_database_url()) as conn:
                priors = [get_prior_source_last_modified(conn, c) for c in datasets]
            if priors and all(p is not None and source_last_modified <= p for p in priors):
                log.info("All %d datasets already at %s — early skip (no download)",
                         len(datasets), source_last_modified)
                with psycopg.connect(_database_url()) as conn:
                    for c in datasets:
                        write_no_change_run(
                            conn, c, url=BUNDLE_URL,
                            source_last_modified=source_last_modified,
                            prior_source_last_modified=source_last_modified,
                        )
                return 0

        # Download once per invocation; reuse across datasets.
        zip_bytes = 0
        if not zip_path.exists() or zip_path.stat().st_size != (content_length or -1):
            log.info("downloading %s -> %s", BUNDLE_URL, zip_path)
            zip_bytes = download_zip(client, BUNDLE_URL, zip_path)
            log.info("downloaded %d bytes", zip_bytes)
        else:
            zip_bytes = zip_path.stat().st_size
            log.info("reusing already-downloaded %s (%d bytes)", zip_path, zip_bytes)

    # SWC permit set is required by every CSV except perm-components-swc itself.
    needs_swc = any(d.swc_filter_csv_col for d in datasets)
    swc_set: set[str] | None = None
    if needs_swc:
        swc_set = load_swc_permit_set(zip_path)

    for cfg in datasets:
        ds_rc = ingest_one(
            cfg,
            zip_path=zip_path,
            source_last_modified=source_last_modified,
            zip_bytes=zip_bytes,
            swc_set=swc_set,
            batch_size=args.batch_size,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            max_rows=args.max_rows,
        )
        rc = rc or ds_rc

    if not args.dry_run:
        run_recon_only()
    return rc


if __name__ == "__main__":
    sys.exit(main())
