#!/usr/bin/env python3
"""NYC Property R2 ingest — 4 sources × snapshot-date partition (no RisingWave).

Mirrors four NYC public property datasets into R2 as ZSTD-compressed Parquet,
partitioned by snapshot date. Pulled fresh on every run from the NYC Open Data
SODA API:

  source                  | dataset 4x4   | rows (~) | identity column
  ------------------------+---------------+----------+----------------------
  dof_sales               | w2pb-icbu     | 846K     | bbl + property_zip5
  pluto                   | 64uk-42ks     | 859K     | owner_name_normalized
  dof_condo_units         | eguu-7ie3     | 306K     | unit_bbl → base_bbl
  dof_condo_associations  | p8u6-a6it     | 12K      | condo_name_normalized

Layout per snapshot:

  nyc-property/source=dof_sales/snapshot=YYYY-MM-DD/data.parquet
  nyc-property/source=pluto/snapshot=YYYY-MM-DD/data.parquet
  nyc-property/source=dof_condo_units/snapshot=YYYY-MM-DD/data.parquet
  nyc-property/source=dof_condo_associations/snapshot=YYYY-MM-DD/data.parquet

Audit ledger: ops.nyc_property_r2_ingest_runs (one row per (snapshot_date, source)).

Schema-reality divergence from directive 2026-05-08-nyc-property-r2-ingest.md:

  * The directive expected DOF Sales to carry SELLER NAME + BUYER NAME columns.
    The actual public SODA feed (w2pb-icbu) does NOT. Owner identity for NYC
    property lives in PLUTO (OwnerName) and ACRIS (out of scope here); DOF
    Sales is parcel-grain transactional history without parties.
  * The directive expected an "owner-named" Condo Units dataset. The actual
    public DOF condo data (eguu-7ie3 + p8u6-a6it) is BBL-bridge metadata —
    unit BBL → base BBL → condo name. No owners.
  * SODA's w2pb-icbu covers 2016-2025 only (~846K rows). Pre-2016 history
    lives in per-borough XLSX files at nyc.gov/site/finance — out of scope
    here.
  * Validation floors are calibrated to the actual dataset sizes (700K / 700K
    / 250K / 8K), not the directive's stated floors which assumed buyer/seller
    coverage.

NO RisingWave wiring — that's a follow-up directive after R2 is populated.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nyc_property_r2_ingest.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nyc_property_r2_ingest.py --max-rows 50000 \\
                                                          --r2-prefix-override 'nyc-property/_smoke/'
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nyc_property_r2_ingest.py --source pluto --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import boto3
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

from scripts._lib.nyc_property_normalize import (
    compute_bbl,
    normalize_borough_code,
    normalize_owner_name,
)


R2_BUCKET = "dex-raw-landing-zone"
SOCRATA_HOST = "https://data.cityofnewyork.us"

DEFAULT_PAGE_SIZE = 50_000
DEFAULT_PAGE_SLEEP = 0.25
HTTP_TIMEOUT_SECONDS = 120.0
HTTP_RETRY_LIMIT = 5
HTTP_RETRY_BASE_BACKOFF_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# Source registry — drives the per-source SODA / projection / normalization
# logic. Order matters: smallest first so failures surface fast.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceSpec:
    name: str                  # CHECK constraint value in audit table.
    socrata_id: str            # 4x4.
    dataset_name: str          # human-readable.

    # Per-source projection — the column adapter is dispatched on this name.
    # See _project_<name>_row helpers below.

    def soda_url(self) -> str:
        return f"{SOCRATA_HOST}/resource/{self.socrata_id}.json"

    def metadata_url(self) -> str:
        return f"{SOCRATA_HOST}/api/views/{self.socrata_id}.json"


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        name="dof_condo_associations",
        socrata_id="p8u6-a6it",
        dataset_name="Digital Tax Map: Condominiums",
    ),
    SourceSpec(
        name="dof_condo_units",
        socrata_id="eguu-7ie3",
        dataset_name="Digital Tax Map: Condominium Units",
    ),
    SourceSpec(
        name="dof_sales",
        socrata_id="w2pb-icbu",
        dataset_name="NYC Citywide Annualized Calendar Sales Update",
    ),
    SourceSpec(
        name="pluto",
        socrata_id="64uk-42ks",
        dataset_name="Primary Land Use Tax Lot Output (PLUTO)",
    ),
)


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("nyc-property-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Env helpers.
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError(
            "neither DEX_DB_URL_DIRECT nor DEX_DB_URL_POOLED is set — "
            "are you running under `doppler run`?"
        )
    return url


def _socrata_app_token() -> str | None:
    return os.environ.get("NYC_OPEN_DATA_APP_TOKEN") or None


# --------------------------------------------------------------------------- #
# Audit-row helpers.
# --------------------------------------------------------------------------- #


@dataclass
class RunRow:
    run_id: str
    snapshot_date: date
    source: str
    started_monotonic: float


def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot_date: date,
    source: str,
    source_url: str,
    socrata_dataset_id: str,
    source_last_modified: datetime | None,
    num_found_at_run_start: int | None,
    started_monotonic: float,
) -> RunRow:
    """started_monotonic is the wall-clock origin for duration_seconds — pass
    in the monotonic timestamp captured BEFORE the SODA pull / parquet write
    so the audit ledger reflects the full operation, not just the DB-write
    window.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.nyc_property_r2_ingest_runs (
                snapshot_date, source, status,
                source_url, socrata_dataset_id, source_last_modified,
                num_found_at_run_start
            ) VALUES (%s, %s, 'running', %s, %s, %s, %s)
            RETURNING id;
            """,
            (snapshot_date, source, source_url, socrata_dataset_id,
             source_last_modified, num_found_at_run_start),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return RunRow(
        run_id=str(row_id),
        snapshot_date=snapshot_date,
        source=source,
        started_monotonic=started_monotonic,
    )


def finalize_run_row(
    conn: psycopg.Connection,
    rr: RunRow,
    *,
    status: str,
    pages_fetched: int | None,
    bytes_downloaded: int | None,
    parquet_bytes_written: int | None,
    parquet_row_count: int | None,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_key: str | None,
    r2_object_bytes: int | None,
    num_found_at_run_end: int | None,
    error_message: str | None,
    error_class: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - rr.started_monotonic, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.nyc_property_r2_ingest_runs SET
                status = %s,
                pages_fetched = %s, bytes_downloaded = %s,
                parquet_bytes_written = %s, parquet_row_count = %s,
                r2_bucket = %s, r2_prefix = %s, r2_key = %s, r2_object_bytes = %s,
                num_found_at_run_end = %s,
                finished_at = now(), duration_seconds = %s,
                error_message = %s, error_class = %s, notes = %s
              WHERE id = %s;
            """,
            (
                status, pages_fetched, bytes_downloaded,
                parquet_bytes_written, parquet_row_count,
                r2_bucket, r2_prefix, r2_key, r2_object_bytes,
                num_found_at_run_end,
                duration, error_message, error_class,
                Jsonb(notes) if notes else None, rr.run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# SODA HTTP helpers.
# --------------------------------------------------------------------------- #


def _http_headers() -> dict[str, str]:
    h = {
        "User-Agent": "data-engine-x nyc-property-r2-ingest",
        "Accept": "application/json",
    }
    tok = _socrata_app_token()
    if tok:
        h["X-App-Token"] = tok
    return h


def fetch_dataset_metadata(client: httpx.Client, source: SourceSpec) -> dict[str, Any]:
    r = _retry_get(client, source.metadata_url(), params=None)
    return r.json()


def fetch_count(client: httpx.Client, source: SourceSpec) -> int:
    r = _retry_get(
        client, source.soda_url(),
        params={"$query": "SELECT count(*) AS c"},
    )
    body = r.json()
    if not body or "c" not in body[0]:
        return 0
    return int(body[0]["c"])


def fetch_page(
    client: httpx.Client, source: SourceSpec,
    *, limit: int, offset: int,
) -> tuple[list[dict[str, Any]], int]:
    r = _retry_get(
        client, source.soda_url(),
        params={"$limit": str(limit), "$offset": str(offset), "$order": ":id"},
    )
    return r.json(), len(r.content)


def _retry_get(
    client: httpx.Client, url: str, *, params: dict[str, str] | None,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(HTTP_RETRY_LIMIT):
        try:
            r = client.get(
                url, params=params, headers=_http_headers(),
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            if r.status_code == 429 or 500 <= r.status_code < 600:
                raise httpx.HTTPStatusError(
                    f"retryable status={r.status_code}", request=r.request, response=r,
                )
            r.raise_for_status()
            return r
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc
            backoff = HTTP_RETRY_BASE_BACKOFF_SECONDS * (2 ** attempt)
            log.warning(
                "GET %s attempt %d/%d failed: %s — retrying in %.1fs",
                url, attempt + 1, HTTP_RETRY_LIMIT, exc, backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(f"GET {url} failed after {HTTP_RETRY_LIMIT} attempts: {last_exc}")


# --------------------------------------------------------------------------- #
# SODA pagination.
# --------------------------------------------------------------------------- #


@dataclass
class SodaPull:
    rows: list[dict[str, Any]]
    pages_fetched: int
    bytes_downloaded: int
    num_found_at_run_start: int
    num_found_at_run_end: int
    source_last_modified: datetime | None


def pull_dataset(
    source: SourceSpec, *,
    page_size: int, page_sleep: float, max_rows: int | None,
) -> SodaPull:
    rows: list[dict[str, Any]] = []
    pages_fetched = 0
    bytes_downloaded = 0

    with httpx.Client(follow_redirects=True) as client:
        meta = fetch_dataset_metadata(client, source)
        last_mod_unix = meta.get("rowsUpdatedAt") or meta.get("dataUpdatedAt")
        last_mod = (
            datetime.fromtimestamp(int(last_mod_unix), tz=timezone.utc)
            if last_mod_unix is not None else None
        )
        num_start = fetch_count(client, source)
        log.info(
            "[%s] dataset_name=%s rowsUpdatedAt=%s rowCount=%s",
            source.name, source.dataset_name, last_mod, num_start,
        )

        offset = 0
        while True:
            limit = page_size
            if max_rows is not None:
                remaining = max_rows - len(rows)
                if remaining <= 0:
                    break
                limit = min(limit, remaining)

            page, nbytes = fetch_page(
                client, source, limit=limit, offset=offset,
            )
            pages_fetched += 1
            bytes_downloaded += nbytes
            rows.extend(page)
            log.info(
                "[%s] page %d offset=%d got=%d total=%d (%.1f KB)",
                source.name, pages_fetched, offset, len(page), len(rows),
                nbytes / 1024,
            )
            if len(page) < limit:
                break
            offset += limit
            if max_rows is not None and len(rows) >= max_rows:
                break
            time.sleep(page_sleep)

        num_end = fetch_count(client, source)
        if num_end != num_start:
            log.warning(
                "[%s] count drifted mid-run: start=%s end=%s",
                source.name, num_start, num_end,
            )

    return SodaPull(
        rows=rows, pages_fetched=pages_fetched, bytes_downloaded=bytes_downloaded,
        num_found_at_run_start=num_start, num_found_at_run_end=num_end,
        source_last_modified=last_mod,
    )


# --------------------------------------------------------------------------- #
# Per-source row projection.
#
# Each projector takes the raw Socrata dict and returns a dict whose keys
# match the per-source pyarrow schema. Raw source fields are preserved as
# strings; normalized fields (`bbl`, `property_zip5`, `*_normalized`) are
# computed from them. Snapshot date is added by the caller.
# --------------------------------------------------------------------------- #


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return str(v)


def _zip5(v: Any) -> str | None:
    s = _str(v)
    if s is None:
        return None
    if "-" in s:
        s = s.split("-", 1)[0]
    s = s.strip()
    if len(s) >= 5 and s[:5].isdigit():
        return s[:5]
    return s if s else None


def _project_dof_sales(row: dict[str, Any]) -> dict[str, Any]:
    """w2pb-icbu — 29 columns from metadata; SODA truncates several names.

    NO SELLER/BUYER name — those fields are not in the public feed (the
    NYC Open Data DOF Sales dataset is parcel-grain transactional history,
    not buyer/seller-grain). Owner identity for NYC property lives in PLUTO
    or ACRIS (the latter is out of scope here).

    Column-name truncations observed at smoke time (2026-05-08):
      building_class_as_of_final_roll  → building_class_as_of_final
      building_class_at_time_of_sale   → building_class_at_time_of
      neighborhood_tabulation_area_*   → nta
      ease-ment                        → ease_ment (snake-case rewrite)
    """
    borough_raw = row.get("borough")
    block_raw = row.get("block")
    lot_raw = row.get("lot")
    return {
        "borough_raw": _str(borough_raw),
        "neighborhood": _str(row.get("neighborhood")),
        "building_class_category": _str(row.get("building_class_category")),
        "tax_class_as_of_final_roll": _str(row.get("tax_class_as_of_final_roll")),
        "block": _str(block_raw),
        "lot": _str(lot_raw),
        "easement": _str(row.get("ease_ment")),
        "building_class_as_of_final": _str(row.get("building_class_as_of_final")),
        "address": _str(row.get("address")),
        "apartment_number": _str(row.get("apartment_number")),
        "zip_code": _str(row.get("zip_code")),
        "residential_units": _str(row.get("residential_units")),
        "commercial_units": _str(row.get("commercial_units")),
        "total_units": _str(row.get("total_units")),
        "land_square_feet": _str(row.get("land_square_feet")),
        "gross_square_feet": _str(row.get("gross_square_feet")),
        "year_built": _str(row.get("year_built")),
        "tax_class_at_time_of_sale": _str(row.get("tax_class_at_time_of_sale")),
        "building_class_at_time_of": _str(row.get("building_class_at_time_of")),
        "sale_price": _str(row.get("sale_price")),
        "sale_date": _str(row.get("sale_date")),
        "latitude": _str(row.get("latitude")),
        "longitude": _str(row.get("longitude")),
        "community_board": _str(row.get("community_board")),
        "council_district": _str(row.get("council_district")),
        "bin": _str(row.get("bin")),
        "bbl_socrata": _str(row.get("bbl")),
        "census_tract_2020": _str(row.get("census_tract_2020")),
        "nta": _str(row.get("nta")),
        # Normalized columns.
        "borough_code": normalize_borough_code(borough_raw),
        "bbl": compute_bbl(borough_raw, block_raw, lot_raw),
        "property_zip5": _zip5(row.get("zip_code")),
    }


def _project_pluto(row: dict[str, Any]) -> dict[str, Any]:
    """64uk-42ks — 101 columns. ownername is the canonical donor identity field.

    PLUTO's 'borough' column is a 2-letter code (MN/BX/BK/QN/SI). 'block' and
    'lot' are the 'tax_block' / 'tax_lot' columns (Socrata uses the 'tax block'
    / 'tax lot' display names which become snake-case 'tax_block' / 'tax_lot'
    via SODA's identifier rewrite).
    """
    borough_raw = row.get("borough")
    block_raw = row.get("tax_block") or row.get("block")
    lot_raw = row.get("tax_lot") or row.get("lot")
    owner = row.get("ownername")
    # Preserve every PLUTO column as raw_<name> string. PLUTO's ~101 columns
    # are wide and we want to keep the verbatim Socrata payload accessible.
    out = {f"raw_{k}": _str(v) for k, v in row.items() if not k.startswith(":@")}
    out.update({
        # Hot columns surfaced for predicate pushdown / RW source DDL clarity.
        "borough_raw": _str(borough_raw),
        "block": _str(block_raw),
        "lot": _str(lot_raw),
        "address": _str(row.get("address")),
        "zip_code": _str(row.get("postcode") or row.get("zipcode")),
        "ownername": _str(owner),
        "ownertype": _str(row.get("ownertype")),
        "lotarea": _str(row.get("lotarea")),
        "bldgarea": _str(row.get("bldgarea")),
        "yearbuilt": _str(row.get("yearbuilt")),
        "bldgclass": _str(row.get("bldgclass")),
        "landuse": _str(row.get("landuse")),
        "condono": _str(row.get("condono")),
        "version": _str(row.get("version")),
        "bbl_pluto": _str(row.get("bbl")),
        # Normalized.
        "borough_code": normalize_borough_code(borough_raw),
        "bbl": compute_bbl(borough_raw, block_raw, lot_raw),
        "property_zip5": _zip5(row.get("postcode") or row.get("zipcode")),
        "owner_name_normalized": normalize_owner_name(_str(owner)),
    })
    return out


def _project_dof_condo_units(row: dict[str, Any]) -> dict[str, Any]:
    """eguu-7ie3 — 16 columns. BBL bridge: unit_bbl → condo_base_bbl."""
    base_boro = row.get("condo_base_boro")
    unit_boro = row.get("unit_boro")
    return {
        "condo_base_boro": _str(base_boro),
        "condo_base_block": _str(row.get("condo_base_block")),
        "condo_base_lot": _str(row.get("condo_base_lot")),
        "condo_base_bbl": _str(row.get("condo_base_bbl")),
        "condo_base_bbl_key": _str(row.get("condo_base_bbl_key")),
        "condo_number": _str(row.get("condo_number")),
        "condo_key": _str(row.get("condo_key")),
        "unit_boro": _str(unit_boro),
        "unit_block": _str(row.get("unit_block")),
        "unit_lot": _str(row.get("unit_lot")),
        "unit_bbl": _str(row.get("unit_bbl")),
        "unit_designation": _str(row.get("unit_designation")),
        "floor_text": _str(row.get("floor_text")),
        "model": _str(row.get("model")),
        "geometry_type": _str(row.get("geometry_type")),
        "effective_tax_year": _str(row.get("effective_tax_year")),
        # Normalized.
        "borough_code": normalize_borough_code(base_boro),
        "unit_bbl_normalized": compute_bbl(
            unit_boro, row.get("unit_block"), row.get("unit_lot"),
        ),
        "base_bbl_normalized": compute_bbl(
            base_boro, row.get("condo_base_block"), row.get("condo_base_lot"),
        ),
    }


def _project_dof_condo_associations(row: dict[str, Any]) -> dict[str, Any]:
    """p8u6-a6it — 9 columns. Maps base BBL → billing BBL via condo association."""
    base_boro = row.get("condo_base_boro")
    name = row.get("condo_name")
    return {
        "condo_base_boro": _str(base_boro),
        "condo_base_block": _str(row.get("condo_base_block")),
        "condo_base_lot": _str(row.get("condo_base_lot")),
        "condo_base_bbl": _str(row.get("condo_base_bbl")),
        "condo_base_bbl_key": _str(row.get("condo_base_bbl_key")),
        "condo_key": _str(row.get("condo_key")),
        "condo_number": _str(row.get("condo_number")),
        "condo_name": _str(name),
        "condo_billing_bbl": _str(row.get("condo_billing_bbl")),
        # Normalized.
        "borough_code": normalize_borough_code(base_boro),
        "base_bbl_normalized": compute_bbl(
            base_boro, row.get("condo_base_block"), row.get("condo_base_lot"),
        ),
        "association_name_normalized": normalize_owner_name(_str(name)),
    }


PROJECTORS = {
    "dof_sales": _project_dof_sales,
    "pluto": _project_pluto,
    "dof_condo_units": _project_dof_condo_units,
    "dof_condo_associations": _project_dof_condo_associations,
}


# --------------------------------------------------------------------------- #
# Parquet writer.
# --------------------------------------------------------------------------- #


def project_rows(
    source: SourceSpec, raw_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    proj = PROJECTORS[source.name]
    return [proj(r) for r in raw_rows]


def _build_table(
    rows: list[dict[str, Any]], snapshot_date: date, source_name: str,
) -> pa.Table:
    """Build a pyarrow Table with a stable column union across rows.

    Columns where the projector emits `borough_code` (SMALLINT-ish) are typed
    int8; everything else is string. A `snapshot_date` and `source_name`
    column are appended to every row.
    """
    if not rows:
        # Empty table — emit a single column so Parquet write doesn't fail.
        return pa.table({
            "snapshot_date": pa.array([], type=pa.date32()),
            "nyc_property_source": pa.array([], type=pa.string()),
        })

    # Stable column union across all rows (preserves first-seen order).
    seen: dict[str, None] = {}
    for r in rows:
        for k in r.keys():
            seen.setdefault(k, None)
    columns = list(seen.keys())

    # Build per-column arrays.
    arrays: list[pa.Array] = []
    field_types: list[pa.Field] = []
    for col in columns:
        values = [r.get(col) for r in rows]
        if col == "borough_code":
            arr = pa.array(values, type=pa.int8())
        else:
            # Force string for every non-borough column. compute_bbl returns
            # zero-padded strings; raw_* fields are already strings.
            arr = pa.array(
                [None if v is None else str(v) for v in values],
                type=pa.string(),
            )
        arrays.append(arr)
        field_types.append(pa.field(col, arr.type))

    # Append snapshot_date + nyc_property_source.
    arrays.append(pa.array([snapshot_date] * len(rows), type=pa.date32()))
    field_types.append(pa.field("snapshot_date", pa.date32()))
    arrays.append(pa.array([source_name] * len(rows), type=pa.string()))
    field_types.append(pa.field("nyc_property_source", pa.string()))

    return pa.Table.from_arrays(arrays, schema=pa.schema(field_types))


def write_parquet(
    rows: list[dict[str, Any]], *,
    snapshot_date: date, source_name: str, out_path: Path,
) -> tuple[int, int]:
    table = _build_table(rows, snapshot_date, source_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table, out_path,
        compression="zstd", compression_level=9,
        row_group_size=50_000,
    )
    return table.num_rows, out_path.stat().st_size


# --------------------------------------------------------------------------- #
# R2 upload.
# --------------------------------------------------------------------------- #


def upload_to_r2(
    parquet_path: Path, *, bucket: str, key: str, log_prefix: str,
) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    log.info(
        "%s uploading %s (%.1f MB) → s3://%s/%s",
        log_prefix, parquet_path, file_bytes / (1 << 20), bucket, key,
    )
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    log.info("%s upload done", log_prefix)
    return file_bytes


# --------------------------------------------------------------------------- #
# Per-source orchestration.
# --------------------------------------------------------------------------- #


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPError):
        return "download_failure"
    if isinstance(exc, psycopg.Error):
        return "db_failure"
    if isinstance(exc, (TimeoutError, OSError)):
        return "timeout"
    return "unknown"


def _validation_floor(source_name: str) -> int:
    return {
        "dof_sales":              700_000,
        "pluto":                  700_000,
        "dof_condo_units":        250_000,
        "dof_condo_associations":   8_000,
    }[source_name]


@dataclass
class SourceResult:
    source: str
    rows: int
    parquet_bytes: int
    r2_key: str | None
    duration_s: float
    status: str
    error: str | None


def run_one_source(
    source: SourceSpec, *,
    snapshot_date: date,
    r2_prefix: str,
    workdir: Path,
    page_size: int,
    page_sleep: float,
    max_rows: int | None,
    dry_run: bool,
    skip_db: bool,
) -> SourceResult:
    started = time.monotonic()
    log.info(
        "[%s] starting (snapshot=%s dataset=%s)",
        source.name, snapshot_date, source.socrata_id,
    )
    pull = pull_dataset(
        source, page_size=page_size, page_sleep=page_sleep, max_rows=max_rows,
    )
    log.info(
        "[%s] SODA pull complete — pages=%d rows=%d bytes=%.1f MB",
        source.name, pull.pages_fetched, len(pull.rows),
        pull.bytes_downloaded / (1 << 20),
    )

    if dry_run:
        log.info("[%s] DRY RUN — sample row: %s", source.name,
                 pull.rows[0] if pull.rows else "(none)")
        return SourceResult(
            source=source.name, rows=len(pull.rows), parquet_bytes=0,
            r2_key=None, duration_s=time.monotonic() - started,
            status="dry_run", error=None,
        )

    projected = project_rows(source, pull.rows)
    parquet_path = workdir / f"{source.name}.parquet"
    rows_written, parquet_bytes = write_parquet(
        projected, snapshot_date=snapshot_date,
        source_name=source.name, out_path=parquet_path,
    )
    log.info(
        "[%s] wrote %s — rows=%d bytes=%.1f MB",
        source.name, parquet_path, rows_written, parquet_bytes / (1 << 20),
    )

    r2_key = r2_prefix + f"source={source.name}/snapshot={snapshot_date.isoformat()}/data.parquet"
    uploaded = upload_to_r2(
        parquet_path, bucket=R2_BUCKET, key=r2_key,
        log_prefix=f"[{source.name}]",
    )

    if not skip_db:
        with psycopg.connect(_database_url()) as conn:
            rr = insert_run_row(
                conn,
                snapshot_date=snapshot_date, source=source.name,
                source_url=source.soda_url(),
                socrata_dataset_id=source.socrata_id,
                source_last_modified=pull.source_last_modified,
                num_found_at_run_start=pull.num_found_at_run_start,
                started_monotonic=started,
            )
            try:
                finalize_run_row(
                    conn, rr, status="completed",
                    pages_fetched=pull.pages_fetched,
                    bytes_downloaded=pull.bytes_downloaded,
                    parquet_bytes_written=parquet_bytes,
                    parquet_row_count=rows_written,
                    r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                    r2_object_bytes=uploaded,
                    num_found_at_run_end=pull.num_found_at_run_end,
                    error_message=None, error_class=None,
                    notes={
                        "dataset_name": source.dataset_name,
                        "max_rows": max_rows,
                        "page_size": page_size,
                    },
                )
            except Exception as exc:
                log.exception("[%s] failed to finalize run row", source.name)
                finalize_run_row(
                    conn, rr, status="failed",
                    pages_fetched=pull.pages_fetched,
                    bytes_downloaded=pull.bytes_downloaded,
                    parquet_bytes_written=parquet_bytes,
                    parquet_row_count=rows_written,
                    r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                    r2_object_bytes=uploaded,
                    num_found_at_run_end=pull.num_found_at_run_end,
                    error_message=str(exc), error_class=_classify_error(exc),
                    notes=None,
                )
                raise

    # Cleanup local parquet.
    try:
        parquet_path.unlink(missing_ok=True)
    except Exception:
        pass

    return SourceResult(
        source=source.name, rows=rows_written, parquet_bytes=parquet_bytes,
        r2_key=r2_key, duration_s=time.monotonic() - started,
        status="completed", error=None,
    )


# --------------------------------------------------------------------------- #
# Validation gate.
# --------------------------------------------------------------------------- #


def validate_results(
    results: list[SourceResult], *, max_rows: int | None,
) -> tuple[bool, list[str]]:
    """Apply per-source row-count floors. Returns (passed, failures)."""
    failures: list[str] = []
    if max_rows is not None:
        # Smoke-test mode — skip floors.
        return True, failures
    for r in results:
        if r.status != "completed":
            failures.append(f"{r.source}: status={r.status} error={r.error}")
            continue
        floor = _validation_floor(r.source)
        if r.rows < floor:
            failures.append(
                f"{r.source}: row_count={r.rows} below floor={floor}",
            )
    return len(failures) == 0, failures


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #


@dataclass
class IngestArgs:
    page_size: int
    page_sleep: float
    max_rows: int | None
    dry_run: bool
    workdir: Path
    r2_prefix_override: str | None
    sources: tuple[SourceSpec, ...]
    skip_db: bool


def run_ingest(args: IngestArgs) -> int:
    snapshot_date = datetime.now(timezone.utc).date()
    r2_prefix = args.r2_prefix_override or "nyc-property/"
    if not r2_prefix.endswith("/"):
        r2_prefix += "/"
    log.info(
        "starting NYC Property R2 ingest snapshot=%s prefix=%s sources=%s dry_run=%s",
        snapshot_date, r2_prefix, [s.name for s in args.sources], args.dry_run,
    )

    results: list[SourceResult] = []
    rc = 0
    for source in args.sources:
        try:
            res = run_one_source(
                source,
                snapshot_date=snapshot_date,
                r2_prefix=r2_prefix,
                workdir=args.workdir,
                page_size=args.page_size,
                page_sleep=args.page_sleep,
                max_rows=args.max_rows,
                dry_run=args.dry_run,
                skip_db=args.skip_db,
            )
            results.append(res)
            log.info(
                "[%s] DONE rows=%d bytes=%.1f MB duration=%.1fs",
                res.source, res.rows, res.parquet_bytes / (1 << 20), res.duration_s,
            )
        except Exception as exc:
            log.exception("[%s] failed", source.name)
            results.append(SourceResult(
                source=source.name, rows=0, parquet_bytes=0, r2_key=None,
                duration_s=0.0, status="failed", error=str(exc),
            ))
            rc = 1

    if args.dry_run:
        log.info("DRY RUN — skipping validation gate")
        return rc

    passed, failures = validate_results(results, max_rows=args.max_rows)
    if passed:
        log.info("validation gate PASSED — %d sources completed", len(results))
    else:
        log.error("validation gate FAILED:")
        for f in failures:
            log.error("  - %s", f)
        rc = max(rc, 1)
    return rc


def parse_args(argv: list[str] | None = None) -> IngestArgs:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--page-sleep", type=float, default=DEFAULT_PAGE_SLEEP)
    p.add_argument(
        "--max-rows", type=int, default=None,
        help="Cap rows pulled per source — useful for smoke tests",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Pull SODA, project, but write no Parquet / no R2 / no DB rows",
    )
    p.add_argument(
        "--workdir", default=None,
        help="Local Parquet scratch dir (default: /tmp/nyc_property_r2)",
    )
    p.add_argument(
        "--r2-prefix-override", default=None,
        help="Override the nyc-property/ prefix (e.g. for smoke runs)",
    )
    p.add_argument(
        "--source", action="append", choices=[s.name for s in SOURCES],
        help="Only ingest the named source(s). Repeatable. Default: all 4.",
    )
    p.add_argument(
        "--skip-db", action="store_true",
        help="Skip audit ledger writes (smoke / dev only)",
    )
    a = p.parse_args(argv)
    workdir = Path(a.workdir or "/tmp/nyc_property_r2")
    workdir.mkdir(parents=True, exist_ok=True)
    if a.source:
        sources = tuple(s for s in SOURCES if s.name in a.source)
    else:
        sources = SOURCES
    return IngestArgs(
        page_size=a.page_size, page_sleep=a.page_sleep,
        max_rows=a.max_rows, dry_run=a.dry_run,
        workdir=workdir, r2_prefix_override=a.r2_prefix_override,
        sources=sources, skip_db=a.skip_db,
    )


def main(argv: list[str] | None = None) -> int:
    return run_ingest(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
