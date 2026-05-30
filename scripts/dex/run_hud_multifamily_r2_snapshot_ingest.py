#!/usr/bin/env python3
"""HUD Multifamily + LIHTC ArcGIS → R2 Fuel Tank snapshot ingest.

Mirrors four HUD ArcGIS FeatureServer datasets into R2 as ZSTD Parquet,
date-stamped by snapshot day. ~110K rows total across:

  multifamily-pipeline  Multifamily_Properties_and_Pipeline      ~19K rows
  insured               HUD_Insured_Multifamily_Properties       ~17K rows
  lihtc                 LIHTC                                    ~51K rows
  assisted              MULTIFAMILY_PROPERTIES_ASSISTED          ~24K rows

Fills the multifamily borrower-name gap HMDA leaves anonymized: HMDA shows
multifamily mortgage *flow* by census tract; HUD Multifamily shows the
*stock* of FHA-insured + HUD-assisted + LIHTC properties with property-level
addresses + named management/sponsor entities.

Pipeline (per dataset, sequential):

  1. ArcGIS schema fetch — discover the layer's published fields.
  2. Total-record-count probe → skip-if-unchanged short-circuit (pragmatic
     fallback: ArcGIS doesn't expose per-row last-modified; total count is
     a best-effort change signal, same as the existing Postgres ingest).
  3. Paginate the FeatureServer query (resultOffset + resultRecordCount=2000;
     stop when exceededTransferLimit=false AND partial page returned). HTTP
     layer + retry logic + epoch-ms date coercion lifted from
     scripts/run_hud_multifamily_lihtc_ingest.py.
  4. Project per-feature attributes into a typed-Python row, preserving every
     raw field 1:1, plus four normalized identity-spine columns:
       owner_name_normalized, property_zip5, property_state_normalized,
       property_city_normalized
     (per-table owner-field cascade is in
      scripts/_lib/hud_multifamily_normalize.py — HUD's actual ArcGIS
      schemas do NOT have OWNER_NAME / MORTGAGEE_NAME / MGR_NAME /
      LIHTC_OWNER_NAME columns; see that module's docstring.)
     and one partition column: hud_snapshot_date (DATE).
  5. pyarrow Table → ZSTD Parquet → R2 at
     s3://dex-raw-landing-zone/hud-multifamily/snapshot={YYYY-MM-DD}/{key}.parquet.
  6. Audit row → ops.hud_multifamily_r2_ingest_runs.

The Postgres `entities.source_hud_*` ingests (run by the existing scraper)
stay UNTOUCHED — both paths share the upstream API and write to disjoint
destinations. RisingWave wiring is DEFERRED to a follow-up directive.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_hud_multifamily_r2_snapshot_ingest.py insured
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_hud_multifamily_r2_snapshot_ingest.py all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_hud_multifamily_r2_snapshot_ingest.py insured \\
        --max-pages 1 --r2-prefix-override smoke/hud-multifamily
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_hud_multifamily_r2_snapshot_ingest.py all \\
        --skip-if-unchanged

See directive
~/Desktop/hq/directives/2026-05-08-hud-multifamily-r2-snapshot.md.
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
from typing import Any

import boto3
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

# Add scripts/_lib to sys.path so the normalizer can be imported as a sibling.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _lib import hud_multifamily_normalize as N  # noqa: E402


R2_BUCKET = "dex-raw-landing-zone"
ARCGIS_BASE = (
    "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services"
)
DEFAULT_PAGE_SIZE = 2000   # ArcGIS maxRecordCount
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
USER_AGENT = "data-engine-x/hud-multifamily-r2-snapshot"


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("hud-mf-r2")


log = _logger()


# --------------------------------------------------------------------------- #
# Per-dataset configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DatasetConfig:
    key: str                # 'insured' | 'assisted' | 'lihtc' | 'multifamily-pipeline'
    service: str            # ArcGIS service name
    layer_id: int
    natural_key_field: str  # ArcGIS field name (mixed case)
    # Per-table address candidate fields (ArcGIS field name → internal attr).
    # First non-empty wins; falls back through the cascade.
    state_fields: tuple[str, ...]
    zip_fields: tuple[str, ...]
    city_fields: tuple[str, ...]

    @property
    def query_url(self) -> str:
        return f"{ARCGIS_BASE}/{self.service}/FeatureServer/{self.layer_id}/query"

    @property
    def layer_url(self) -> str:
        return f"{ARCGIS_BASE}/{self.service}/FeatureServer/{self.layer_id}"


# Address-field cascades per table. Looked up empirically from each layer's
# published schema — the four datasets use different naming conventions:
#   pipeline: lowercase State / Zip_Code / City.
#   insured / assisted: STD_ST / STD_ZIP5 / STD_CITY (USPS-standardized)
#                       with STATE2KX as fallback for state.
#   lihtc:    STATE2KX / STD_ZIP5 / STD_CITY (no plain STATE).
DATASETS: tuple[DatasetConfig, ...] = (
    DatasetConfig(
        key="multifamily-pipeline",
        service="Multifamily_Properties_and_Pipeline",
        layer_id=0,
        natural_key_field="Property_ID",
        state_fields=("State",),
        zip_fields=("Zip_Code",),
        city_fields=("City",),
    ),
    DatasetConfig(
        key="insured",
        service="HUD_Insured_Multifamily_Properties",
        layer_id=0,
        natural_key_field="PROPERTY_ID",
        state_fields=("STD_ST", "STATE2KX"),
        zip_fields=("STD_ZIP5", "STD_ZIP9"),
        city_fields=("STD_CITY",),
    ),
    DatasetConfig(
        # LIHTC's STATE2KX is the FIPS code with leading zero stripped (e.g.
        # '2' for Alaska's FIPS 02), so it consistently fails the 2-letter
        # invariant. PROJ_ST is the actual 2-letter postal code and is
        # populated on every row. Cascade order matters — PROJ_ST must be
        # first. (insured / assisted use STD_ST which is the 2-letter code,
        # so STD_ST → STATE2KX is the correct order there.)
        key="lihtc",
        service="LIHTC",
        layer_id=0,
        natural_key_field="HUD_ID",
        state_fields=("PROJ_ST", "STATE2KX"),
        zip_fields=("STD_ZIP5", "PROJ_ZIP"),
        city_fields=("STD_CITY", "PROJ_CTY"),
    ),
    DatasetConfig(
        key="assisted",
        service="MULTIFAMILY_PROPERTIES_ASSISTED",
        layer_id=0,
        natural_key_field="PROPERTY_ID",
        state_fields=("STD_ST", "STATE2KX"),
        zip_fields=("STD_ZIP5", "STD_ZIP9"),
        city_fields=("STD_CITY",),
    ),
)


DATASETS_BY_KEY: dict[str, DatasetConfig] = {d.key: d for d in DATASETS}


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# HTTP layer (lifted from run_hud_multifamily_lihtc_ingest.py)
# --------------------------------------------------------------------------- #


def fetch_layer_schema(
    client: httpx.Client, ds: DatasetConfig,
) -> list[dict[str, Any]]:
    url = f"{ds.layer_url}?f=json"
    r = client.get(url, timeout=30.0)
    r.raise_for_status()
    return r.json()["fields"]


def fetch_total_count(client: httpx.Client, ds: DatasetConfig) -> int:
    params = {"where": "1=1", "returnCountOnly": "true", "f": "json"}
    r = client.get(ds.query_url, params=params, timeout=60.0)
    r.raise_for_status()
    return int(r.json()["count"])


def fetch_page(
    client: httpx.Client,
    ds: DatasetConfig,
    *,
    page_size: int,
    offset: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Return (features_list, exceeded_transfer_limit, response_bytes)."""
    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
        "resultOffset": str(offset),
        "resultRecordCount": str(page_size),
        "orderByFields": f"{ds.natural_key_field} ASC",
    }
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(ds.query_url, params=params, timeout=180.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning(
                    "[%s] HTTP %s; retry in %ss (%s/%s)",
                    ds.key, r.status_code, wait, attempt, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return (
                data.get("features", []),
                bool(data.get("exceededTransferLimit", False)),
                len(r.content),
            )
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning(
                "[%s] page fetch error (%s); retry in %ss (%s/%s)",
                ds.key, exc, wait, attempt, MAX_RETRIES,
            )
            time.sleep(wait)
    raise RuntimeError(
        f"[{ds.key}] failed to fetch page after {MAX_RETRIES} retries; last: {last_exc}"
    )


# --------------------------------------------------------------------------- #
# Per-feature projection + Parquet write
# --------------------------------------------------------------------------- #


_DATE_TYPE = "esriFieldTypeDate"
_INT_TYPES = {"esriFieldTypeInteger", "esriFieldTypeSmallInteger", "esriFieldTypeOID"}
_FLOAT_TYPES = {"esriFieldTypeDouble", "esriFieldTypeSingle"}


def _coerce_value(esri_type: str, raw: Any) -> Any:
    """Coerce an ArcGIS attribute value to a Python-native value suitable
    for pyarrow inference. Dates → tz-aware datetime; ints/floats → numeric;
    everything else → string-or-None."""
    if raw is None:
        return None
    if esri_type == _DATE_TYPE:
        return N.coerce_arcgis_epoch_ms_to_dt(raw)
    if esri_type in _INT_TYPES:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if esri_type in _FLOAT_TYPES:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    s = str(raw).strip()
    return s if s else None


def _first_present(attrs: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for f in fields:
        v = attrs.get(f)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def features_to_arrow_table(
    features: list[dict[str, Any]],
    *,
    ds: DatasetConfig,
    schema_fields: list[dict[str, Any]],
    snapshot_date: date,
) -> pa.Table:
    """Project a list of ArcGIS features into a pyarrow Table preserving
    every raw attribute 1:1 (typed) and adding the four normalized identity
    columns plus hud_snapshot_date.

    Columns emitted:
      arcgis_object_id (int64)            — OBJECTID, surfaced as a sibling
      <every other raw field, lowercased> — typed per esri_type
      owner_name_normalized (string)
      property_zip5 (string)
      property_state_normalized (string)
      property_city_normalized (string)
      hud_snapshot_date (date32)
    """
    # Ordered list of (esri_field_name, lowercase_pg_name, esri_type),
    # excluding OBJECTID — that becomes arcgis_object_id below.
    raw_cols: list[tuple[str, str, str]] = []
    for f in schema_fields:
        if f["type"] == "esriFieldTypeOID":
            continue
        raw_cols.append((f["name"], f["name"].lower(), f["type"]))

    # Build one column-per-field as a Python list. pyarrow will infer types
    # from the list contents — datetimes as timestamp(us, tz=UTC), ints,
    # floats, strings.
    cols: dict[str, list[Any]] = {"arcgis_object_id": []}
    for _esri_name, pg_name, _ in raw_cols:
        cols[pg_name] = []
    cols["owner_name_normalized"] = []
    cols["property_zip5"] = []
    cols["property_state_normalized"] = []
    cols["property_city_normalized"] = []
    cols["hud_snapshot_date"] = []

    for feat in features:
        attrs = feat.get("attributes", {})

        # OBJECTID (always present per ArcGIS contract).
        oid = attrs.get("OBJECTID")
        try:
            cols["arcgis_object_id"].append(int(oid) if oid is not None else None)
        except (TypeError, ValueError):
            cols["arcgis_object_id"].append(None)

        # All other raw fields, typed per esri_type.
        for esri_name, pg_name, esri_type in raw_cols:
            cols[pg_name].append(_coerce_value(esri_type, attrs.get(esri_name)))

        # Normalized identity columns.
        cols["owner_name_normalized"].append(
            N.normalize_owner_name(N.pick_owner_field(ds.key, attrs))
        )
        cols["property_zip5"].append(
            N.normalize_zip5(_first_present(attrs, ds.zip_fields))
        )
        cols["property_state_normalized"].append(
            N.normalize_state(_first_present(attrs, ds.state_fields))
        )
        cols["property_city_normalized"].append(
            N.normalize_city(_first_present(attrs, ds.city_fields))
        )
        cols["hud_snapshot_date"].append(snapshot_date)

    # Build pyarrow arrays. For mostly-empty / all-None columns pyarrow
    # would otherwise default to null type — force string for those so the
    # Parquet schema is stable across runs.
    arrays: list[pa.Array] = []
    names: list[str] = []
    for col_name, values in cols.items():
        if col_name == "arcgis_object_id":
            arrays.append(pa.array(values, type=pa.int64()))
        elif col_name == "hud_snapshot_date":
            arrays.append(pa.array(values, type=pa.date32()))
        elif col_name in {
            "owner_name_normalized",
            "property_zip5",
            "property_state_normalized",
            "property_city_normalized",
        }:
            arrays.append(pa.array(values, type=pa.string()))
        else:
            # Match raw esri_type to pyarrow type via the matching raw_col
            # tuple — ensures fields like LAST_UPDT_DTTM stay timestamptz
            # even when every value in this batch is null.
            esri_type = next(
                (t for (_, pg, t) in raw_cols if pg == col_name), None,
            )
            if esri_type == _DATE_TYPE:
                arrays.append(pa.array(
                    values, type=pa.timestamp("us", tz="UTC"),
                ))
            elif esri_type in _INT_TYPES:
                arrays.append(pa.array(values, type=pa.int64()))
            elif esri_type in _FLOAT_TYPES:
                arrays.append(pa.array(values, type=pa.float64()))
            else:
                arrays.append(pa.array(values, type=pa.string()))
        names.append(col_name)
    return pa.Table.from_arrays(arrays, names=names)


def write_parquet(table: pa.Table, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table, dest,
        compression="zstd",
        compression_level=9,
        row_group_size=100000,
    )
    return dest.stat().st_size


def upload_to_r2(parquet_path: Path, *, key: str, log_prefix: str) -> int:
    s3 = _r2_client()
    n_bytes = parquet_path.stat().st_size
    log.info("%s uploading %.1f MB → s3://%s/%s",
             log_prefix, n_bytes / (1 << 20), R2_BUCKET, key)
    s3.upload_file(
        str(parquet_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return n_bytes


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot_date: date,
    ds: DatasetConfig,
    total_records_in_source: int,
    prior_total: int | None,
) -> str:
    sql = """
    INSERT INTO ops.hud_multifamily_r2_ingest_runs (
        snapshot_date, dataset_key, status,
        source_service, source_layer_url,
        total_records_in_source, prior_total_records_in_source
    ) VALUES (%s, %s, 'running', %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            snapshot_date, ds.key,
            ds.service, ds.layer_url,
            total_records_in_source, prior_total,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_total(
    conn: psycopg.Connection, ds: DatasetConfig,
) -> int | None:
    """Most recent successful total_records_in_source for this dataset_key
    across ALL prior snapshots — re-ingestion is gated by source-side
    record count, not by snapshot label."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT total_records_in_source
              FROM ops.hud_multifamily_r2_ingest_runs
             WHERE dataset_key = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
        """, (ds.key,))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    snapshot_date: date,
    ds: DatasetConfig,
    total_records_in_source: int,
    prior_total: int | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.hud_multifamily_r2_ingest_runs (
                snapshot_date, dataset_key, status,
                source_service, source_layer_url,
                total_records_in_source, prior_total_records_in_source,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                snapshot_date, ds.key,
                ds.service, ds.layer_url,
                total_records_in_source, prior_total,
                started, started,
                Jsonb({"reason": "total_records_in_source unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    pages_fetched: int,
    bytes_downloaded: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    parquet_column_count: int,
    r2_key: str | None,
    r2_total_bytes: int,
    owner_null_pct: float | None,
    zip5_ok_pct: float | None,
    state_ok_pct: float | None,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.hud_multifamily_r2_ingest_runs
               SET status = %s,
                   pages_fetched = %s,
                   bytes_downloaded = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_key = %s, r2_total_bytes = %s,
                   owner_name_null_pct = %s,
                   zip5_ok_pct = %s,
                   state_ok_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
        """, (
            status, pages_fetched, bytes_downloaded,
            parquet_row_count, parquet_bytes_written, parquet_column_count,
            R2_BUCKET if r2_key else None, r2_key, r2_total_bytes,
            owner_null_pct, zip5_ok_pct, state_ok_pct,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-dataset ingest
# --------------------------------------------------------------------------- #


def ingest_dataset(
    ds: DatasetConfig,
    client: httpx.Client,
    *,
    snapshot_date: date,
    page_size: int,
    page_sleep: float,
    max_pages: int | None,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[{ds.key} {snapshot_date}]"
    started_wall = time.monotonic()
    log.info("%s start service=%s", log_prefix, ds.service)

    schema_fields = fetch_layer_schema(client, ds)
    non_oid_cols = [f for f in schema_fields if f["type"] != "esriFieldTypeOID"]
    log.info("%s schema: %d non-OID fields", log_prefix, len(non_oid_cols))

    total_in_source = fetch_total_count(client, ds)
    log.info("%s source reports %d total records", log_prefix, total_in_source)

    if dry_run:
        log.info("%s DRY RUN — fetching %s page(s), no DB writes / R2 upload",
                 log_prefix, max_pages or 1)
        offset = 0
        for page_idx in range(max_pages or 1):
            rows, exceeded, nbytes = fetch_page(
                client, ds, page_size=page_size, offset=offset,
            )
            log.info("%s   page %s: rows=%s exceeded=%s bytes=%s",
                     log_prefix, page_idx, len(rows), exceeded, nbytes)
            if not exceeded or len(rows) < page_size:
                break
            offset += page_size
            time.sleep(page_sleep)
        return 0

    with psycopg.connect(_database_url()) as conn:
        prior_total = get_prior_total(conn, ds)
        log.info("%s prior successful total_records_in_source: %s",
                 log_prefix, prior_total)
        if (
            skip_if_unchanged
            and prior_total is not None
            and prior_total == total_in_source
        ):
            log.info("%s total_records_in_source unchanged — recording no_change",
                     log_prefix)
            write_no_change_run(
                conn,
                snapshot_date=snapshot_date, ds=ds,
                total_records_in_source=total_in_source,
                prior_total=prior_total,
            )
            return 0

        run_id = insert_run_row(
            conn,
            snapshot_date=snapshot_date, ds=ds,
            total_records_in_source=total_in_source,
            prior_total=prior_total,
        )
        log.info("%s run id: %s", log_prefix, run_id)

        try:
            # Paginate. Buffer all features in memory — these datasets are
            # small (max ~51K rows × ~120 cols for LIHTC ≈ 100MB working set,
            # well within a 6GB limit).
            all_features: list[dict[str, Any]] = []
            offset = 0
            pages_fetched = 0
            total_bytes = 0
            while True:
                if max_pages is not None and pages_fetched >= max_pages:
                    log.info("%s max-pages limit hit", log_prefix)
                    break
                page_started = time.monotonic()
                rows, exceeded, nbytes = fetch_page(
                    client, ds, page_size=page_size, offset=offset,
                )
                pages_fetched += 1
                total_bytes += nbytes
                all_features.extend(rows)
                log.info(
                    "%s page %s: rows=%s buffered=%s exceeded=%s bytes=%s elapsed=%.1fs",
                    log_prefix, pages_fetched, len(rows), len(all_features),
                    exceeded, nbytes, time.monotonic() - page_started,
                )
                if not exceeded or len(rows) < page_size:
                    break
                offset += page_size
                time.sleep(page_sleep)

            log.info("%s buffered %d features; building Arrow table",
                     log_prefix, len(all_features))
            table = features_to_arrow_table(
                all_features,
                ds=ds,
                schema_fields=schema_fields,
                snapshot_date=snapshot_date,
            )
            parquet_row_count = table.num_rows
            parquet_column_count = table.num_columns
            log.info("%s arrow table: rows=%s cols=%s",
                     log_prefix, parquet_row_count, parquet_column_count)

            # Compute validation-gate rates inline (cheaper than re-reading
            # parquet). These mirror the directive's §5 sanity checks.
            owner_col = table.column("owner_name_normalized")
            zip_col = table.column("property_zip5")
            state_col = table.column("property_state_normalized")
            owner_null_pct = (
                100.0 * owner_col.null_count / parquet_row_count
                if parquet_row_count else 0.0
            )
            zip5_ok_pct = (
                100.0 * (parquet_row_count - zip_col.null_count) / parquet_row_count
                if parquet_row_count else 0.0
            )
            state_ok_pct = (
                100.0 * (parquet_row_count - state_col.null_count) / parquet_row_count
                if parquet_row_count else 0.0
            )
            log.info(
                "%s validation: owner_name_null=%.2f%% zip5_ok=%.2f%% state_ok=%.2f%%",
                log_prefix, owner_null_pct, zip5_ok_pct, state_ok_pct,
            )

            workdir.mkdir(parents=True, exist_ok=True)
            parquet_path = workdir / f"{ds.key}_{snapshot_date.isoformat()}.parquet"
            t0 = time.monotonic()
            parquet_bytes = write_parquet(table, parquet_path)
            log.info(
                "%s parquet write: %.1f MB in %.1fs",
                log_prefix, parquet_bytes / (1 << 20), time.monotonic() - t0,
            )

            # R2 upload.
            target_prefix = (
                r2_prefix_override
                or f"hud-multifamily/snapshot={snapshot_date.isoformat()}"
            )
            target_key = target_prefix.rstrip("/") + f"/{ds.key}.parquet"
            try:
                uploaded = upload_to_r2(
                    parquet_path, key=target_key, log_prefix=log_prefix,
                )
            finally:
                # Best-effort local cleanup; full ingest of all 4 datasets
                # would otherwise leave ~50MB in /tmp.
                try:
                    parquet_path.unlink(missing_ok=True)
                except Exception:
                    pass

            finalize_run_row(
                conn, run_id, status="completed",
                pages_fetched=pages_fetched,
                bytes_downloaded=total_bytes,
                parquet_row_count=parquet_row_count,
                parquet_bytes_written=parquet_bytes,
                parquet_column_count=parquet_column_count,
                r2_key=target_key,
                r2_total_bytes=uploaded,
                owner_null_pct=round(owner_null_pct, 4),
                zip5_ok_pct=round(zip5_ok_pct, 4),
                state_ok_pct=round(state_ok_pct, 4),
                started_at=started_wall, error_message=None,
                notes={
                    "page_size": page_size,
                    "max_pages": max_pages,
                    "natural_key_field": ds.natural_key_field,
                    "r2_prefix_override": r2_prefix_override,
                },
            )
            log.info(
                "%s DONE rows=%s parquet=%.1f MB wall=%.1fs",
                log_prefix, f"{parquet_row_count:,}",
                parquet_bytes / (1 << 20),
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
                pages_fetched=0,
                bytes_downloaded=0,
                parquet_row_count=0,
                parquet_bytes_written=0,
                parquet_column_count=0,
                r2_key=None, r2_total_bytes=0,
                owner_null_pct=None,
                zip5_ok_pct=None,
                state_ok_pct=None,
                started_at=started_wall,
                error_message=str(exc), notes=None,
            )
            return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


ALL_KEYS = [d.key for d in DATASETS]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "dataset", choices=ALL_KEYS + ["all"],
        help="Dataset key, or 'all' to run every dataset sequentially.",
    )
    p.add_argument("--snapshot-date", default=None,
                   help="Snapshot label, YYYY-MM-DD. Default: today UTC.")
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--max-pages", type=int, default=None,
                   help="Stop after N pages (smoke test).")
    p.add_argument("--page-sleep-seconds", type=float, default=0.25)
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Skip ingest if total_records_in_source unchanged "
                        "vs prior successful run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch pages but do NOT write to DB / R2 / parquet.")
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override the canonical R2 prefix "
                        "(default: hud-multifamily/snapshot={YYYY-MM-DD}).")
    return p.parse_args()


def _parse_snapshot_date(raw: str | None) -> date:
    if raw is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(raw)


def main() -> int:
    args = parse_args()
    snapshot_date = _parse_snapshot_date(args.snapshot_date)
    workdir = Path(args.workdir or "/tmp/hud_mf_r2_ingest")

    keys = ALL_KEYS if args.dataset == "all" else [args.dataset]

    rc = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        for k in keys:
            ds = DATASETS_BY_KEY[k]
            log.info("=" * 70)
            log.info("=== INGEST: dataset=%s snapshot=%s ===", k, snapshot_date)
            log.info("=" * 70)
            ds_rc = ingest_dataset(
                ds, client,
                snapshot_date=snapshot_date,
                page_size=args.page_size,
                page_sleep=args.page_sleep_seconds,
                max_pages=args.max_pages,
                skip_if_unchanged=args.skip_if_unchanged,
                dry_run=args.dry_run,
                workdir=workdir,
                r2_prefix_override=args.r2_prefix_override,
            )
            if ds_rc != 0:
                rc = ds_rc
                log.error("dataset %s failed; continuing with remaining datasets", k)
    return rc


if __name__ == "__main__":
    sys.exit(main())
