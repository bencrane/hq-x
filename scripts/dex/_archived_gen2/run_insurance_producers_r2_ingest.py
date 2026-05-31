#!/usr/bin/env python3
"""Insurance Producers → R2 Fuel Tank ingest.

Phase 1 sources from state insurance-department free public bulk endpoints
(per `_lib/insurance_producers_state_schema_map.py`):

  TX  data.texas.gov Socrata     individuals (kxv3-diwf, ~949K)
                                 agencies   (3yqc-fcdt, ~55K)
  FL  myfloridacfo.com direct    individuals (AllValidLicensesIndividual.csv)
                                 business    (AllValidLicensesBusiness.csv)
  IL  data.illinois.gov Socrata  producers  (serf-cewv, ~628K)

Each invocation writes ONE ZSTD Parquet per (state, stream) tuple at
`s3://dex-raw-landing-zone/insurance-producers/state=ST/stream=NAME/
snapshot=YYYY-MM-DD/data.parquet`.

The Parquet preserves all source columns as VARCHAR (raw fidelity), adds typed
DATE casts on lifecycle date columns, adds the normalization-spine canonical
column set (producer_kind_normalized, producer_first/last_normalized,
agency_name_normalized, npn_normalized, license_number_normalized,
license_status_normalized, lines_of_authority_set, is_life/health/p_and_c/
surplus_writer flags, address_zip5 + state + city normalized,
home_state_normalized), and adds partition metadata
(producer_state_filing, producer_stream, producer_snapshot_date).

RisingWave wiring is DEFERRED to a follow-up directive — this script lands
canonical R2 Parquet only.

Audit ledger: `ops.insurance_producers_r2_ingest_runs`. Idempotency: HEAD
`Last-Modified` (csv_url) or Socrata `rowsUpdatedAt` (socrata); skip-if-
unchanged short-circuits.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_insurance_producers_r2_ingest.py TX/individuals
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_insurance_producers_r2_ingest.py FL/business --max-rows 5000
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_insurance_producers_r2_ingest.py --all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_insurance_producers_r2_ingest.py --all --skip-if-unchanged

A stream identifier can be `STATE`, `STATE/NAME`, or the Socrata 4×4 id.

See directive
~/Desktop/hq/directives/2026-05-08-insurance-producers-phase-1-top-5-states-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

from scripts._lib.insurance_producers_normalize import (
    KIND_AGENCY,
    KIND_INDIVIDUAL,
    STATUS_ACTIVE,
    classify_license_status,
    derive_loa_flags,
    normalize_agency_name,
    normalize_city,
    normalize_lines_of_authority,
    normalize_license_number,
    normalize_npn,
    normalize_producer_name,
    normalize_state_code,
    parse_us_date,
    strip_excel_protect,
    zip5,
)
from scripts._lib.insurance_producers_state_schema_map import (
    KIND_RULE_AGENCY_ONLY,
    KIND_RULE_INDIVIDUAL_ONLY,
    KIND_RULE_MIXED_BY_FIRST_NAME,
    STREAMS,
    StreamConfig,
    stream_by_id,
    streams_for_state,
)


R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
USER_AGENT = "data-engine-x/insurance-producers-r2-ingest"

# Socrata recommends max 50_000 rows / page on the JSON resource endpoint.
SOCRATA_PAGE_SIZE = 50_000


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("insurance-producers-r2-ingest")


log = _logger()


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
# Freshness — Socrata view-metadata + HTTP HEAD for direct-CSV streams
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceFreshness:
    last_modified: datetime | None
    extra: dict[str, Any]


def fetch_freshness(
    client: httpx.Client, stream: StreamConfig,
) -> SourceFreshness:
    """Get the last-modified timestamp for the source.

    Socrata: GET `/api/views/<id>.json` — read `rowsUpdatedAt` (UTC epoch).
    csv_url: HEAD on the file URL — read `Last-Modified` header.
    """
    if stream.source_kind == "socrata":
        url = stream.metadata_url
        assert url
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = client.get(url, follow_redirects=True, timeout=30.0)
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("metadata GET %s -> %s; retry in %ss",
                                url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                j = r.json()
                rows_updated = j.get("rowsUpdatedAt")
                view_last_mod = j.get("viewLastModified")
                ts: datetime | None = None
                if rows_updated:
                    try:
                        ts = datetime.fromtimestamp(
                            int(rows_updated), tz=timezone.utc,
                        )
                    except (TypeError, ValueError):
                        ts = None
                return SourceFreshness(
                    last_modified=ts,
                    extra={
                        "rows_updated_at": rows_updated,
                        "view_last_modified": view_last_mod,
                    },
                )
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 30)
                log.warning("metadata %s error (%s); retry in %ss",
                            url, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"metadata fetch failed: {last_exc}")
    elif stream.source_kind == "csv_url":
        url = stream.csv_url_resolved
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = client.head(url, follow_redirects=True, timeout=30.0)
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("HEAD %s -> %s; retry in %ss",
                                url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                lm_header = r.headers.get("last-modified")
                length_header = r.headers.get("content-length")
                ts: datetime | None = None
                if lm_header:
                    try:
                        ts = parsedate_to_datetime(lm_header)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        ts = None
                return SourceFreshness(
                    last_modified=ts,
                    extra={
                        "last_modified_header": lm_header,
                        "content_length": length_header,
                    },
                )
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s error (%s); retry in %ss",
                            url, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"HEAD fetch failed: {last_exc}")
    else:
        raise ValueError(f"unknown source_kind: {stream.source_kind!r}")


# --------------------------------------------------------------------------- #
# Source download — Socrata pagination (NDJSON) + direct-CSV (streamed)
# --------------------------------------------------------------------------- #


def _socrata_page(
    client: httpx.Client, base_url: str, *, limit: int, offset: int,
) -> list[dict]:
    """One page of Socrata JSON resource data, with `$order=:id`."""
    last_exc: Exception | None = None
    params = {"$limit": str(limit), "$offset": str(offset), "$order": ":id"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(base_url, params=params, timeout=120.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("page GET %s offset=%d -> %s; retry in %ss",
                            base_url, offset, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("page GET %s offset=%d error (%s); retry in %ss",
                        base_url, offset, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Socrata page fetch failed: {last_exc}")


def download_socrata_to_jsonl(
    client: httpx.Client, stream: StreamConfig, dest_jsonl: Path,
    *, max_rows: int | None,
) -> tuple[int, int]:
    """Paginate the Socrata JSON resource endpoint, write NDJSON to dest_jsonl.

    Returns (rows_written, bytes_written).
    """
    base_url = stream.csv_url_resolved
    target = max_rows if max_rows is not None else None
    page_size = (
        min(SOCRATA_PAGE_SIZE, target) if target is not None else SOCRATA_PAGE_SIZE
    )
    rows_written = 0
    bytes_written = 0
    offset = 0
    last_log = time.monotonic()
    with dest_jsonl.open("w", encoding="utf-8") as f:
        while True:
            page = _socrata_page(client, base_url, limit=page_size, offset=offset)
            if not page:
                break
            for row in page:
                row.pop(":id", None)
                line = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
                f.write(line)
                f.write("\n")
                bytes_written += len(line) + 1
            rows_written += len(page)
            offset += len(page)
            now = time.monotonic()
            if now - last_log >= 10.0:
                log.info("  socrata pagination: %s rows, %.1f MB",
                         f"{rows_written:,}", bytes_written / (1 << 20))
                last_log = now
            if target is not None and rows_written >= target:
                break
            if len(page) < page_size:
                break
    return rows_written, bytes_written


def download_csv_url(
    client: httpx.Client, stream: StreamConfig, dest_csv: Path,
) -> tuple[int, int]:
    """Stream the source CSV to disk. Returns (rows_estimated, bytes_written).

    The row count is estimated by counting newlines after download. We don't
    parse here — the canonical row count comes from DuckDB after CSV read.
    """
    url = stream.csv_url_resolved
    bytes_written = 0
    last_log = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with client.stream(
                "GET", url, follow_redirects=True, timeout=600.0,
            ) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("stream GET %s -> %s; retry in %ss",
                                url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest_csv.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        bytes_written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 10.0:
                            log.info("  csv download: %.1f MB",
                                     bytes_written / (1 << 20))
                            last_log = now
            break
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("stream GET %s error (%s); retry in %ss",
                        url, exc, wait)
            time.sleep(wait)
    else:
        raise RuntimeError(f"CSV download failed after retries: {last_exc}")
    # Newline-count estimate (cheap, one pass through the file).
    with dest_csv.open("rb") as f:
        rows_estimated = sum(1 for _ in f) - 1  # subtract header
    return max(rows_estimated, 0), bytes_written


# --------------------------------------------------------------------------- #
# DuckDB transform — read source into raw VARCHAR view + apply Python
# normalizers via post-pass on Pandas, write ZSTD Parquet.
# --------------------------------------------------------------------------- #


def _build_raw_view(
    con: duckdb.DuckDBPyConnection,
    *,
    stream: StreamConfig,
    source_path: Path,
    log_prefix: str,
) -> tuple[list[str], dict[str, str]]:
    """CREATE VIEW raw AS ... over the source file.

    Returns (column_names_in_load_order, column_name_to_type_map).
    """
    if stream.source_kind == "socrata":
        # Socrata JSON resource pagination yielded NDJSON. read_json_auto with
        # union_by_name handles missing-fields-on-some-rows.
        con.execute(f"""
            CREATE VIEW raw AS
            SELECT * FROM read_json_auto(
              '{source_path}',
              format='newline_delimited',
              union_by_name=true,
              maximum_object_size=33554432
            );
        """)
    elif stream.source_kind == "csv_url":
        # FL CSVs use header row, double-quote, comma. all_varchar holds raw
        # fidelity (FL Excel-protect wrappers are stripped at the normalizer
        # layer). ignore_errors keeps occasional malformed rows from blowing
        # the entire ingest.
        con.execute(f"""
            CREATE VIEW raw AS
            SELECT * FROM read_csv_auto(
              '{source_path}',
              header=true,
              all_varchar=true,
              ignore_errors=true,
              quote='"',
              escape='"',
              delim=','
            );
        """)
    else:
        raise ValueError(f"unknown source_kind: {stream.source_kind!r}")

    raw_describe = con.execute("DESCRIBE raw;").fetchall()
    raw_cols = [r[0] for r in raw_describe]
    raw_types = {r[0]: str(r[1]) for r in raw_describe}
    log.info("%s   raw columns (%d): %s",
             log_prefix, len(raw_cols),
             ", ".join(raw_cols[:8]) + (", …" if len(raw_cols) > 8 else ""))
    return raw_cols, raw_types


def _project_raw_to_arrow(
    con: duckdb.DuckDBPyConnection,
    *,
    raw_cols: list[str],
    raw_types: dict[str, str],
    max_rows: int | None,
) -> pa.Table:
    """Select all raw columns as VARCHAR (preserving raw fidelity) → Arrow Table.

    DuckDB STRUCT/MAP/LIST columns serialize via to_json; everything else
    casts cleanly to VARCHAR. We don't apply normalizers in SQL — the Python
    pass below handles those.
    """
    select_parts: list[str] = []
    for col in raw_cols:
        t = raw_types.get(col, "VARCHAR").upper()
        if t.startswith("STRUCT") or t.startswith("MAP") or t.startswith("LIST"):
            expr = f"to_json(\"{col}\")"
        elif t == "VARCHAR":
            expr = f"\"{col}\""
        else:
            expr = f"CAST(\"{col}\" AS VARCHAR)"
        # Per-state column lookups in `_apply_normalizers` operate over the
        # source-config column names (original header), so we keep them as-is.
        select_parts.append(f"{expr} AS \"{col}\"")
    sql = "SELECT " + ", ".join(select_parts) + " FROM raw"
    if max_rows is not None:
        sql += f" LIMIT {max_rows}"
    return con.execute(sql).to_arrow_table()


def _coalesce_first_nonempty(values: list[Any]) -> str | None:
    """Return the first non-empty stripped string from values; else None."""
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _apply_normalizers(
    tbl: pa.Table,
    *,
    stream: StreamConfig,
    snapshot_date: date,
) -> pa.Table:
    """Row-by-row apply Python normalizers; return PyArrow Table with the
    canonical column set appended. The original raw columns are preserved.
    """
    n = tbl.num_rows
    cols = set(tbl.column_names)

    def _list(name: str | None) -> list[Any] | None:
        if not name or name not in cols:
            return None
        return tbl.column(name).to_pylist()

    # --- Per-row producer-kind classifier ---------------------------------
    if stream.kind_rule == KIND_RULE_INDIVIDUAL_ONLY:
        kinds = [KIND_INDIVIDUAL] * n
    elif stream.kind_rule == KIND_RULE_AGENCY_ONLY:
        kinds = [KIND_AGENCY] * n
    elif stream.kind_rule == KIND_RULE_MIXED_BY_FIRST_NAME:
        disc_col = stream.kind_discriminator_column
        if disc_col not in cols:
            raise RuntimeError(
                f"{stream.state}/{stream.name}: discriminator column "
                f"'{disc_col}' missing from source"
            )
        disc = tbl.column(disc_col).to_pylist()
        kinds = [
            KIND_INDIVIDUAL if (v is not None and str(v).strip()) else KIND_AGENCY
            for v in disc
        ]
    else:
        raise ValueError(f"unknown kind_rule: {stream.kind_rule!r}")

    # --- NPN + license-number ---------------------------------------------
    npn_list = _list(stream.npn_column)
    npn_normalized = (
        [normalize_npn(v) for v in npn_list] if npn_list is not None
        else [None] * n
    )
    license_list = _list(stream.license_number_column)
    license_number_normalized = (
        [normalize_license_number(v) for v in license_list]
        if license_list is not None else [None] * n
    )

    # --- Producer first / middle / last (individuals) --------------------
    first_list = _list(stream.individual_first_column)
    middle_list = _list(stream.individual_middle_column)
    last_list = _list(stream.individual_last_column)
    full_list = _list(stream.individual_full_name_column)

    producer_first: list[str | None] = []
    producer_middle: list[str | None] = []
    producer_last: list[str | None] = []
    for i in range(n):
        if kinds[i] != KIND_INDIVIDUAL:
            producer_first.append(None)
            producer_middle.append(None)
            producer_last.append(None)
            continue
        # First name: prefer explicit first column, else first token of
        # normalized full-name.
        f = None
        if first_list is not None:
            v = strip_excel_protect(first_list[i])
            if v:
                f = v.lower().strip() or None
        if not f and full_list is not None:
            full = normalize_producer_name(full_list[i])
            if full:
                f = full.split(" ", 1)[0]
        producer_first.append(f)
        # Middle name: prefer explicit middle column, none otherwise.
        m = None
        if middle_list is not None:
            v = strip_excel_protect(middle_list[i])
            if v:
                m = v.lower().strip() or None
        producer_middle.append(m)
        # Last name: prefer explicit last column, else final token of
        # normalized full-name.
        l = None
        if last_list is not None:
            v = strip_excel_protect(last_list[i])
            if v:
                l = v.lower().strip() or None
        if not l and full_list is not None:
            full = normalize_producer_name(full_list[i])
            if full:
                parts = full.split(" ")
                if parts:
                    l = parts[-1]
        producer_last.append(l)

    # --- Agency name -------------------------------------------------------
    agency_list = _list(stream.agency_name_column)
    agency_normalized: list[str | None] = []
    for i in range(n):
        if kinds[i] != KIND_AGENCY:
            agency_normalized.append(None)
            continue
        agency_normalized.append(
            normalize_agency_name(agency_list[i])
            if agency_list is not None else None
        )

    # --- Address spine (preference-ordered) -------------------------------
    zip_pref_lists = [
        tbl.column(c).to_pylist()
        for c in stream.address_zip_columns if c in cols
    ]
    state_pref_lists = [
        tbl.column(c).to_pylist()
        for c in stream.address_state_columns if c in cols
    ]
    city_pref_lists = [
        tbl.column(c).to_pylist()
        for c in stream.address_city_columns if c in cols
    ]

    def _coalesce_at(lists: list[list[Any]], i: int) -> str | None:
        for lst in lists:
            v = lst[i]
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
        return None

    address_zip5 = [zip5(_coalesce_at(zip_pref_lists, i)) for i in range(n)]
    address_state_normalized = [
        normalize_state_code(_coalesce_at(state_pref_lists, i))
        for i in range(n)
    ]
    address_city_normalized = [
        normalize_city(_coalesce_at(city_pref_lists, i)) for i in range(n)
    ]

    # --- License lifecycle ------------------------------------------------
    status_list = _list(stream.license_status_column)
    if status_list is not None:
        license_status_normalized = [classify_license_status(v) for v in status_list]
    else:
        license_status_normalized = [
            stream.license_status_default or STATUS_ACTIVE
        ] * n

    eff_list = _list(stream.license_effective_date_column)
    license_effective_date = (
        [parse_us_date(v) for v in eff_list]
        if eff_list is not None else [None] * n
    )
    exp_list = _list(stream.license_expiration_date_column)
    license_expiration_date = (
        [parse_us_date(v) for v in exp_list]
        if exp_list is not None else [None] * n
    )

    # --- Lines of authority + writer flags --------------------------------
    loa_list = _list(stream.loa_column)
    lines_of_authority_set = (
        [normalize_lines_of_authority(v) for v in loa_list]
        if loa_list is not None else [None] * n
    )
    flags_per_row = [derive_loa_flags(s) for s in lines_of_authority_set]
    is_life_writer = [f["is_life_writer"] for f in flags_per_row]
    is_health_writer = [f["is_health_writer"] for f in flags_per_row]
    is_p_and_c_writer = [f["is_p_and_c_writer"] for f in flags_per_row]
    is_surplus_writer = [f["is_surplus_writer"] for f in flags_per_row]

    # --- Home / residency state -------------------------------------------
    home_list = _list(stream.home_state_column)
    if home_list is not None:
        home_state_normalized = [normalize_state_code(v) for v in home_list]
    elif (stream.residency_type_column
          and stream.residency_type_column in cols):
        residency = tbl.column(stream.residency_type_column).to_pylist()
        home_state_normalized = [
            stream.state
            if (v is not None and str(v).strip().lower() == "resident")
            else None
            for v in residency
        ]
    else:
        home_state_normalized = [None] * n

    # --- Partition metadata (always emitted) ------------------------------
    new_columns = {
        "producer_kind_normalized": kinds,
        "npn_normalized": npn_normalized,
        "license_number_normalized": license_number_normalized,
        "producer_first_normalized": producer_first,
        "producer_middle_normalized": producer_middle,
        "producer_last_normalized": producer_last,
        "agency_name_normalized": agency_normalized,
        "address_zip5": address_zip5,
        "address_state_normalized": address_state_normalized,
        "address_city_normalized": address_city_normalized,
        "license_status_normalized": license_status_normalized,
        "lines_of_authority_set": lines_of_authority_set,
        "is_life_writer": is_life_writer,
        "is_health_writer": is_health_writer,
        "is_p_and_c_writer": is_p_and_c_writer,
        "is_surplus_writer": is_surplus_writer,
        "home_state_normalized": home_state_normalized,
        "producer_state_filing": [stream.state] * n,
        "producer_stream": [stream.name] * n,
        "producer_kind_rule": [stream.kind_rule] * n,
    }
    snapshot_iso = snapshot_date  # Python date is fine for PyArrow date32.

    out = tbl
    for name, values in new_columns.items():
        # PyArrow infers the right type per column.
        if name in {
            "is_life_writer", "is_health_writer",
            "is_p_and_c_writer", "is_surplus_writer",
        }:
            arr = pa.array(values, type=pa.bool_())
        else:
            arr = pa.array(values)
        out = out.append_column(name, arr)
    out = out.append_column(
        "license_effective_date",
        pa.array(license_effective_date, type=pa.date32()),
    )
    out = out.append_column(
        "license_expiration_date",
        pa.array(license_expiration_date, type=pa.date32()),
    )
    out = out.append_column(
        "insurance_producers_snapshot_date",
        pa.array([snapshot_iso] * n, type=pa.date32()),
    )

    return out


def transform_to_parquet(
    source_path: Path,
    parquet_path: Path,
    *,
    stream: StreamConfig,
    snapshot_date: date,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """Read source → project + normalize → write ZSTD Parquet.

    Returns (rows_in, rows_pq, null_rates_dict).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")

    raw_cols, raw_types = _build_raw_view(
        con, stream=stream, source_path=source_path, log_prefix=log_prefix,
    )

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   raw rows: %s", log_prefix, f"{rows_in:,}")

    arrow_tbl = _project_raw_to_arrow(
        con, raw_cols=raw_cols, raw_types=raw_types, max_rows=max_rows,
    )
    log.info("%s   loaded %s rows into Arrow Table",
             log_prefix, f"{arrow_tbl.num_rows:,}")

    arrow_tbl = _apply_normalizers(
        arrow_tbl, stream=stream, snapshot_date=snapshot_date,
    )

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    pq.write_table(
        arrow_tbl, str(parquet_path),
        compression="zstd", compression_level=9,
        row_group_size=100_000,
    )
    log.info("%s   parquet write: %.1f MB in %.1fs",
             log_prefix,
             parquet_path.stat().st_size / (1 << 20),
             time.monotonic() - t0)

    # Null-rate sanity on the spine columns.
    rates: dict[str, float] = {}
    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE producer_first_normalized IS NULL) AS first_null,
          count(*) FILTER (WHERE producer_last_normalized IS NULL) AS last_null,
          count(*) FILTER (WHERE agency_name_normalized IS NULL) AS agency_null,
          count(*) FILTER (WHERE license_status_normalized IS NULL) AS status_null,
          count(*) FILTER (WHERE lines_of_authority_set IS NULL) AS loa_null,
          count(*) FILTER (WHERE address_zip5 IS NULL) AS zip_null,
          count(*) FILTER (WHERE is_life_writer = TRUE) AS life,
          count(*) FILTER (WHERE is_health_writer = TRUE) AS health,
          count(*) FILTER (WHERE is_p_and_c_writer = TRUE) AS p_and_c,
          count(*) FILTER (WHERE is_surplus_writer = TRUE) AS surplus
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rows_pq = total
    if total > 0 and rates_row is not None:
        rates = {
            "producer_first_normalized_null_pct":
                round(100.0 * int(rates_row[1]) / total, 4),
            "producer_last_normalized_null_pct":
                round(100.0 * int(rates_row[2]) / total, 4),
            "agency_name_normalized_null_pct":
                round(100.0 * int(rates_row[3]) / total, 4),
            "license_status_normalized_null_pct":
                round(100.0 * int(rates_row[4]) / total, 4),
            "lines_of_authority_set_null_pct":
                round(100.0 * int(rates_row[5]) / total, 4),
            "address_zip5_null_pct":
                round(100.0 * int(rates_row[6]) / total, 4),
            "is_life_writer_pct":
                round(100.0 * int(rates_row[7]) / total, 4),
            "is_health_writer_pct":
                round(100.0 * int(rates_row[8]) / total, 4),
            "is_p_and_c_writer_pct":
                round(100.0 * int(rates_row[9]) / total, 4),
            "is_surplus_writer_pct":
                round(100.0 * int(rates_row[10]) / total, 4),
        }
        log.info(
            "%s   parquet rows: %s; null-rate first=%.2f%% last=%.2f%% "
            "agency=%.2f%% loa=%.2f%% zip=%.2f%%; LOA-flags life=%.2f%% "
            "health=%.2f%% p_and_c=%.2f%% surplus=%.2f%%",
            log_prefix, f"{rows_pq:,}",
            rates["producer_first_normalized_null_pct"],
            rates["producer_last_normalized_null_pct"],
            rates["agency_name_normalized_null_pct"],
            rates["lines_of_authority_set_null_pct"],
            rates["address_zip5_null_pct"],
            rates["is_life_writer_pct"], rates["is_health_writer_pct"],
            rates["is_p_and_c_writer_pct"], rates["is_surplus_writer_pct"],
        )
    con.close()
    return rows_in, rows_pq, rates


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def get_prior_source_last_modified(
    conn: psycopg.Connection, stream: StreamConfig,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.insurance_producers_r2_ingest_runs
             WHERE producer_state_filing = %s
               AND producer_stream = %s
               AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (stream.state, stream.name),
        )
        row = cur.fetchone()
    return row[0] if row else None


def insert_run_row(
    conn: psycopg.Connection,
    stream: StreamConfig,
    snapshot_date: date,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.insurance_producers_r2_ingest_runs (
        producer_state_filing, producer_stream, producer_snapshot_date, status,
        source_url, source_kind, socrata_dataset_id, kind_rule,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            stream.state, stream.name, snapshot_date,
            stream.csv_url_resolved, stream.source_kind, stream.socrata_id,
            stream.kind_rule,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def write_no_change_run(
    conn: psycopg.Connection,
    stream: StreamConfig,
    snapshot_date: date,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.insurance_producers_r2_ingest_runs (
                producer_state_filing, producer_stream, producer_snapshot_date,
                status, source_url, source_kind, socrata_dataset_id, kind_rule,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, %s, 'no_change', %s, %s, %s, %s, %s, %s,
                      %s, %s, 0, %s);
            """,
            (
                stream.state, stream.name, snapshot_date,
                stream.csv_url_resolved, stream.source_kind, stream.socrata_id,
                stream.kind_rule,
                source_last_modified, prior_source_last_modified,
                started, started,
                Jsonb({"reason":
                       "source_last_modified unchanged since prior completed run"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    raw_bytes: int,
    raw_rows: int,
    parquet_rows: int,
    parquet_bytes: int,
    parquet_columns: int,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_object_key: str | None,
    r2_total_bytes: int,
    null_rates: dict[str, float] | None,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    rates = null_rates or {}
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.insurance_producers_r2_ingest_runs
               SET status = %s,
                   raw_bytes_downloaded = %s,
                   raw_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   producer_first_normalized_null_pct = %s,
                   producer_last_normalized_null_pct = %s,
                   agency_name_normalized_null_pct = %s,
                   license_status_normalized_null_pct = %s,
                   lines_of_authority_set_null_pct = %s,
                   address_zip5_null_pct = %s,
                   is_life_writer_pct = %s,
                   is_health_writer_pct = %s,
                   is_p_and_c_writer_pct = %s,
                   is_surplus_writer_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, raw_bytes, raw_rows,
            parquet_rows, parquet_bytes, parquet_columns,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            rates.get("producer_first_normalized_null_pct"),
            rates.get("producer_last_normalized_null_pct"),
            rates.get("agency_name_normalized_null_pct"),
            rates.get("license_status_normalized_null_pct"),
            rates.get("lines_of_authority_set_null_pct"),
            rates.get("address_zip5_null_pct"),
            rates.get("is_life_writer_pct"),
            rates.get("is_health_writer_pct"),
            rates.get("is_p_and_c_writer_pct"),
            rates.get("is_surplus_writer_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-stream main
# --------------------------------------------------------------------------- #


def ingest_stream(
    stream: StreamConfig,
    *,
    snapshot_date: date,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[{stream.state}/{stream.name}]"
    started_wall = time.monotonic()
    log.info("%s start source=%s url=%s",
             log_prefix, stream.source_kind, stream.csv_url_resolved)

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        try:
            freshness = fetch_freshness(client, stream)
        except Exception:
            log.exception("%s freshness fetch failed", log_prefix)
            return 1
        log.info("%s last_modified=%s",
                 log_prefix, freshness.last_modified)

        if dry_run:
            log.info("%s DRY RUN — exiting after freshness fetch", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, stream)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and freshness.last_modified is not None
                and freshness.last_modified <= prior
            ):
                log.info("%s source last_modified unchanged — recording no_change",
                         log_prefix)
                write_no_change_run(
                    conn, stream, snapshot_date,
                    source_last_modified=freshness.last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, stream, snapshot_date,
                source_last_modified=freshness.last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            stem = f"insurance_producers_{stream.state}_{stream.name}"
            source_path = workdir / (
                f"{stem}.jsonl" if stream.source_kind == "socrata"
                else f"{stem}.csv"
            )
            parquet_path = workdir / f"{stem}.parquet"

            try:
                if stream.source_kind == "socrata":
                    rows_pulled, raw_bytes = download_socrata_to_jsonl(
                        client, stream, source_path, max_rows=max_rows,
                    )
                else:
                    rows_pulled, raw_bytes = download_csv_url(
                        client, stream, source_path,
                    )
                log.info("%s downloaded %s rows / %.1f MB",
                         log_prefix, f"{rows_pulled:,}",
                         raw_bytes / (1 << 20))

                rows_in, rows_pq, null_rates = transform_to_parquet(
                    source_path, parquet_path,
                    stream=stream, snapshot_date=snapshot_date,
                    log_prefix=log_prefix, max_rows=max_rows,
                )

                # Row-count parity check (skipped on max_rows path).
                if max_rows is None and rows_in > 0:
                    variance = abs(rows_pq - rows_in) / rows_in
                    if variance > 0.001:
                        raise RuntimeError(
                            f"row-count variance {variance:.4%} > 0.1% "
                            f"(in={rows_in:,} pq={rows_pq:,})"
                        )

                target_prefix = r2_prefix_override or (
                    f"insurance-producers/state={stream.state}/"
                    f"stream={stream.name}/snapshot={snapshot_date.isoformat()}/"
                )
                target_key = target_prefix.rstrip("/") + "/data.parquet"
                uploaded = upload_to_r2(
                    parquet_path, bucket=R2_BUCKET, key=target_key,
                )
                log.info(
                    "%s uploaded → s3://%s/%s (%.1f MB)",
                    log_prefix, R2_BUCKET, target_key, uploaded / (1 << 20),
                )

                # Determine actual parquet column count via DuckDB.
                con = duckdb.connect(":memory:")
                con.execute("PRAGMA threads=1;")
                col_row = con.execute(
                    f"SELECT count(*) FROM "
                    f"(DESCRIBE SELECT * FROM read_parquet('{parquet_path}'));"
                ).fetchone()
                column_count = int(col_row[0]) if col_row else 0
                con.close()

                notes = {
                    "max_rows": max_rows,
                    "r2_prefix_override": r2_prefix_override,
                    "source_kind": stream.source_kind,
                    "socrata_id": stream.socrata_id,
                    "title": stream.title,
                    "kind_rule": stream.kind_rule,
                    "freshness_extra": freshness.extra,
                    "raw_bytes_downloaded": raw_bytes,
                    "rows_pulled_from_source": rows_pulled,
                }
                finalize_run_row(
                    conn, run_id, status="completed",
                    raw_bytes=raw_bytes, raw_rows=rows_in,
                    parquet_rows=rows_pq, parquet_bytes=uploaded,
                    parquet_columns=column_count,
                    r2_bucket=R2_BUCKET, r2_prefix=target_prefix,
                    r2_object_key=target_key, r2_total_bytes=uploaded,
                    null_rates=null_rates,
                    started_at=started_wall, error_message=None,
                    notes=notes,
                )
                log.info(
                    "%s DONE rows=%s upload=%.1f MB wall=%.1fs",
                    log_prefix, f"{rows_pq:,}",
                    uploaded / (1 << 20),
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    raw_bytes=0, raw_rows=0,
                    parquet_rows=0, parquet_bytes=0, parquet_columns=0,
                    r2_bucket=None, r2_prefix=None, r2_object_key=None,
                    r2_total_bytes=0,
                    null_rates=None,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                try:
                    source_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    parquet_path.unlink(missing_ok=True)
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def resolve_stream_targets(tokens: list[str]) -> list[StreamConfig]:
    out: list[StreamConfig] = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if "/" in tok:
            state, name = tok.split("/", 1)
            matched = [s for s in STREAMS
                       if s.state == state.upper() and s.name == name]
        elif len(tok) <= 2:
            matched = list(streams_for_state(tok))
        else:
            byid = stream_by_id(tok)
            matched = [byid] if byid else []
        if not matched:
            raise SystemExit(f"unknown stream target: {tok!r}")
        out.extend(matched)
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("targets", nargs="*",
                   help="One or more stream identifiers: STATE (e.g. 'TX'), "
                        "STATE/NAME (e.g. 'TX/individuals'), Socrata 4×4 id, "
                        "or 'STATE/STREAM' slug.")
    p.add_argument("--all", action="store_true",
                   help="Ingest every stream in STREAMS (TX×2 + FL×2 + IL×1).")
    p.add_argument("--snapshot-date", default=None,
                   help="Override the snapshot partition date (YYYY-MM-DD). "
                        "Default: today (UTC).")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical insurance-producers/state=ST/stream=…"
                        "/snapshot=… prefix (smoke-test use).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.targets:
        log.error("--all and explicit targets are mutually exclusive")
        return 2
    if not args.all and not args.targets:
        log.error("must pass at least one target or --all")
        return 2

    targets = list(STREAMS) if args.all else resolve_stream_targets(args.targets)
    snapshot_date = (
        date.fromisoformat(args.snapshot_date)
        if args.snapshot_date else datetime.now(timezone.utc).date()
    )
    workdir = Path(args.workdir or "/tmp/insurance_producers_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    log.info("Insurance Producers R2 ingest — %d streams, snapshot=%s",
             len(targets), snapshot_date)

    rc = 0
    for stream in targets:
        log.info("=" * 70)
        log.info("=== INGEST: %s/%s (kind_rule=%s) ===",
                 stream.state, stream.name, stream.kind_rule)
        log.info("=" * 70)
        rc_one = ingest_stream(
            stream,
            snapshot_date=snapshot_date,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("%s/%s failed; continuing with remaining streams",
                      stream.state, stream.name)

    try:
        if workdir.exists() and not any(workdir.iterdir()):
            shutil.rmtree(workdir)
    except Exception:
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
