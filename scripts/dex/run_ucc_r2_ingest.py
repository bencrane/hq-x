#!/usr/bin/env python3
"""UCC-1 Filings → R2 Fuel Tank ingest.

Phase 1 sources from Socrata-style state open-data portals (free public bulk
access; no login, no per-query cap). Per `_lib/ucc_state_schema_map.py`:

  CO data.colorado.gov  filings + debtors + secured_parties + collateral
  CT data.ct.gov        liens (denormalized)
  OR data.oregon.gov    secured_parties + filings_last_month

Each invocation writes ONE ZSTD Parquet per (state, stream) tuple at
`s3://dex-raw-landing-zone/ucc/state=ST/stream=NAME/snapshot=YYYY-MM-DD/data.parquet`.

The Parquet preserves all source columns as VARCHAR (raw fidelity), adds
typed DATE casts on Socrata Calendar Date columns, adds normalization-spine
columns (`debtor_name_normalized` + `debtor_zip5` + `debtor_state_normalized`
for debtor parties, secured-party analogues for secured-party parties, both
for denormalized streams), and adds partition metadata (`ucc_state`,
`ucc_stream`, `ucc_snapshot_date`).

RisingWave wiring is DEFERRED to a follow-up directive — this script lands
canonical R2 Parquet only.

Audit ledger: `ops.ucc_r2_ingest_runs`. Idempotency: HEAD via the Socrata
view-metadata API's `rowsUpdatedAt` field; skip-if-unchanged short-circuits.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ucc_r2_ingest.py CO/filings
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ucc_r2_ingest.py CT/liens --max-rows 50000
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ucc_r2_ingest.py --all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ucc_r2_ingest.py --all --skip-if-unchanged

A stream identifier can be `STATE`, `STATE/NAME`, or the Socrata 4×4 id.

See directive
~/Desktop/hq/directives/2026-05-08-ucc-1-filings-phase-1-top-5-states-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb

from scripts._lib.ucc_state_schema_map import (
    STREAMS,
    StreamConfig,
    stream_by_id,
    streams_for_state,
)


R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
USER_AGENT = "data-engine-x/ucc-r2-ingest"

# Socrata recommends max 50_000 rows / page on the JSON resource endpoint;
# `$order=:id` gives stable pagination via the row-internal system id.
SOCRATA_PAGE_SIZE = 50_000


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("ucc-r2-ingest")


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
# HTTP layer — Socrata view-metadata for HEAD-equivalent + bulk CSV download
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceFreshness:
    rows_updated_at: datetime | None
    view_last_updated_at: datetime | None
    row_count: int | None


def fetch_freshness(client: httpx.Client, stream: StreamConfig) -> SourceFreshness:
    """Hit the Socrata `/api/views/{id}.json` metadata endpoint to learn when
    the dataset was last updated. The CSV-export URL doesn't expose
    `Last-Modified` reliably (Content-Length: 0 on HEAD), so the metadata API
    is the canonical freshness signal.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(stream.metadata_url, follow_redirects=True, timeout=30.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("metadata GET %s -> %s; retry in %ss",
                            stream.metadata_url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            j = r.json()

            def _epoch_to_utc(v: Any) -> datetime | None:
                if v is None:
                    return None
                try:
                    return datetime.fromtimestamp(int(v), tz=timezone.utc)
                except (TypeError, ValueError):
                    return None

            rows_updated = _epoch_to_utc(j.get("rowsUpdatedAt"))
            view_last_updated = _epoch_to_utc(j.get("viewLastModified"))
            # Socrata exposes view-level row count under `columns[*].cachedContents.cardinality`
            # for some categories; the simpler `rowsUpdatedAt + actuallyExposedColumns` is
            # what we need for freshness. Row count comes from the dataset itself
            # via a $select=count(*) query later.
            return SourceFreshness(
                rows_updated_at=rows_updated,
                view_last_updated_at=view_last_updated,
                row_count=None,
            )
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("metadata %s error (%s); retry in %ss",
                        stream.metadata_url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"metadata fetch failed: {last_exc}")


def _socrata_page(
    client: httpx.Client, base_url: str, *, limit: int, offset: int,
) -> list[dict]:
    """One page of Socrata JSON resource data, with `$order=:id` for stability."""
    last_exc: Exception | None = None
    params = {
        "$limit": str(limit),
        "$offset": str(offset),
        "$order": ":id",
    }
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


def download_json_pages(
    client: httpx.Client, stream: StreamConfig, dest_jsonl: Path,
    *, max_rows: int | None,
) -> tuple[int, int]:
    """Paginate the Socrata JSON resource endpoint, write NDJSON to dest_jsonl.

    Field names in the resource JSON are the Socrata fieldName (snake_case) —
    matching `_lib/ucc_state_schema_map.py`. The CSV-export endpoint uses
    DISPLAY column labels (uppercase + spaces) that don't match the schema map,
    so the JSON path is canonical.

    Returns (rows_written, bytes_written).
    """
    base_url = f"https://{stream.domain}/resource/{stream.socrata_id}.json"
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
            import json as _json
            for row in page:
                # Drop the system id field from the payload — we're keeping it
                # for ordering only, not for downstream Parquet columns.
                row.pop(":id", None)
                line = _json.dumps(row, separators=(",", ":"), ensure_ascii=False)
                f.write(line)
                f.write("\n")
                bytes_written += len(line) + 1
            rows_written += len(page)
            offset += len(page)
            now = time.monotonic()
            if now - last_log >= 10.0:
                log.info("  JSON pagination: %s rows, %.1f MB",
                         f"{rows_written:,}", bytes_written / (1 << 20))
                last_log = now
            if target is not None and rows_written >= target:
                break
            if len(page) < page_size:
                break
    return rows_written, bytes_written


# --------------------------------------------------------------------------- #
# DuckDB transform — CSV → typed/normalized columns → ZSTD Parquet
# --------------------------------------------------------------------------- #


_NORMALIZE_MACROS_SQL = r"""
-- Pure-SQL mirror of scripts/_lib/ucc_normalize.py. Vectorized at plan time;
-- no Python UDF overhead. Rule changes must update both halves; the SQL form
-- here is the canonical ingest path.

-- Strip ONE trailing org-suffix word + collapse whitespace + collapse N.A.
-- variants. Mirrors `normalize_party_name`.
CREATE MACRO ucc_normalize_party(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE NULLIF(
      (
        WITH s0 AS (
          -- Lowercase first.
          SELECT lower(raw) AS s
        ), s1 AS (
          -- Collapse N.A. variants (N.A., N. A., n.a., etc.) → "na".
          SELECT regexp_replace(
            regexp_replace(s, '\bn\.a\.?\b', 'na', 'g'),
            '\bn\s*\.\s*a\s*\.?\b', 'na', 'g'
          ) AS s FROM s0
        ), s2 AS (
          -- Comma-reverse heuristic for "LAST, FIRST" individuals.
          -- Two guards mirror scripts/_lib/ucc_normalize.normalize_party_name:
          --   1. Head (pre-comma) must be a SINGLE word — multi-word heads
          --      like "kubota credit corporation, u.s.a." don't reverse.
          --   2. Tail's first word (sans periods) must not be a known org
          --      suffix marker (na, inc, llc, etc.).
          SELECT
            CASE
              WHEN strpos(s, ',') > 0
                AND length(string_split(rtrim(substr(s, 1, strpos(s, ',') - 1)), ' ')) = 1
                AND lower(replace(regexp_replace(
                      ltrim(substr(s, strpos(s, ',') + 1)),
                      '^\W*(\w+).*$', '\1', 'g'
                    ), '.', '')) NOT IN (
                  'llc','inc','incorporated','corp','corporation','ltd','limited',
                  'lp','llp','pc','pa','pllc','co','company','na','fsb','fa'
                )
              THEN ltrim(substr(s, strpos(s, ',') + 1)) || ' '
                   || rtrim(substr(s, 1, strpos(s, ',') - 1))
              ELSE s
            END AS s FROM s1
        ), s3 AS (
          -- Strip the punctuation set used by the Python normalizer.
          SELECT regexp_replace(s, '[,.&''\"]+', ' ', 'g') AS s FROM s2
        ), s4 AS (
          -- Collapse whitespace and trim.
          SELECT trim(regexp_replace(s, '\s+', ' ', 'g')) AS s FROM s3
        ), parts AS (
          SELECT s, string_split(s, ' ') AS p FROM s4
        )
        -- Strip ONE trailing org suffix if present.
        SELECT CASE
          WHEN length(p) >= 2 AND p[length(p)] IN
               ('llc','inc','incorporated','corp','corporation','ltd','limited',
                'lp','llp','pc','pa','pllc','co','company','na','fsb','fa')
          THEN array_to_string(p[1:length(p)-1], ' ')
          ELSE s
        END FROM parts
      ),
      ''
    )
  END
);

-- Mirrors `zip5`. First five numeric chars; NULL when fewer than five digits.
CREATE MACRO ucc_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

-- Mirrors `normalize_state_code`. Uppercase 2-letter, validated.
-- NB: validation list mirrors _STATE_CODES.
CREATE MACRO ucc_state_norm(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(trim(raw)) <> 2 THEN NULL
    WHEN upper(trim(raw)) IN (
      'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA',
      'HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
      'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
      'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
      'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
      'DC','PR','VI','GU','AS','MP'
    ) THEN upper(trim(raw))
    ELSE NULL
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_NORMALIZE_MACROS_SQL)


def _coalesce_columns_sql(cols: tuple) -> str:
    """Emit a SQL COALESCE expression over a tuple of DuckDB-quoted column
    names; returns NULL if the tuple is empty."""
    if not cols:
        return "CAST(NULL AS VARCHAR)"
    quoted = ", ".join(f'NULLIF(trim("{c}"), \'\')' for c in cols)
    return f"COALESCE({quoted})"


def _concat_individual_name_sql(cols: tuple) -> str:
    """Emit `first middle last suffix` concat (whitespace-joined, NULL-safe)
    over the given source columns. Empty tuple → CAST(NULL AS VARCHAR)."""
    if not cols:
        return "CAST(NULL AS VARCHAR)"
    parts = [f"COALESCE(NULLIF(trim(\"{c}\"), ''), '')" for c in cols]
    sep = " || ' ' || "
    body = sep.join(parts)
    return (
        f"NULLIF(trim(regexp_replace({body}, '\\s+', ' ', 'g')), '')"
    )


def json_to_parquet(
    jsonl_path: Path,
    parquet_path: Path,
    *,
    stream: StreamConfig,
    snapshot_date: date,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """Read NDJSON pages as VARCHAR, project + normalize, write ZSTD Parquet.

    Returns (rows_in, rows_pq, null_rates).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    # Socrata JSON resource fields are snake_case (matching the schema map).
    # `read_json_auto` infers types per-field; we coerce everything to VARCHAR
    # at the SELECT layer for the raw projection so behaviour matches the
    # FEC pattern (raw fidelity preserved). `format='newline_delimited'`
    # treats each line as one record. `union_by_name=true` handles the
    # rare case where a row drops an optional field.
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_json_auto(
          '{jsonl_path}',
          format='newline_delimited',
          union_by_name=true,
          maximum_object_size=33554432
        );
    """)

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   json raw rows: %s", log_prefix, f"{rows_in:,}")

    raw_describe = con.execute("DESCRIBE raw;").fetchall()
    raw_cols = [r[0] for r in raw_describe]
    raw_types = {r[0]: str(r[1]) for r in raw_describe}
    log.info("%s   json columns (%d): %s",
             log_prefix, len(raw_cols),
             ", ".join(raw_cols[:8]) + (", …" if len(raw_cols) > 8 else ""))

    def _to_varchar(col: str) -> str:
        """Coerce one source column to VARCHAR for the raw projection.

        Socrata location/struct fields (e.g. OR's `image_link`) come back as
        STRUCT — those serialize via `to_json` for fidelity. Everything else
        casts cleanly via VARCHAR.
        """
        t = raw_types.get(col, "VARCHAR").upper()
        if t.startswith("STRUCT") or t.startswith("MAP") or t.startswith("LIST"):
            return f"to_json(\"{col}\")"
        if t == "VARCHAR":
            return f"\"{col}\""
        return f"CAST(\"{col}\" AS VARCHAR)"

    # Build the projection: lowercased raw columns + typed date casts +
    # normalization spine + partition metadata.
    select_parts: list[str] = []
    raw_lc_set: set[str] = set()

    for col in raw_cols:
        lc = col.lower().replace(" ", "_")
        # Avoid duplicate output column names if the source column renames
        # collide (rare on Socrata, but possible with case-insensitive collisions).
        candidate = lc
        i = 2
        while candidate in raw_lc_set:
            candidate = f"{lc}_{i}"
            i += 1
        raw_lc_set.add(candidate)
        if col in stream.date_columns:
            select_parts.append(
                f"TRY_CAST({_to_varchar(col)} AS DATE) AS {candidate}_date"
            )
            # Also preserve the raw VARCHAR under the original lowercased name
            # so consumers can see the raw form when the typed cast nulled.
            select_parts.append(f"{_to_varchar(col)} AS {candidate}_raw")
        else:
            select_parts.append(f"{_to_varchar(col)} AS {candidate}")

    # Normalization spine — depends on stream kind.
    # Filter every schema-map column against `raw_cols` because Socrata's
    # JSON resource API omits absent fields entirely (a column with no
    # populated rows in the dataset never appears in the JSON payload, so
    # `read_json_auto` doesn't infer it). Without this guard, the projection
    # references a non-existent column and DuckDB raises BinderException.
    raw_cols_lc = {c.lower() for c in raw_cols}

    def _present(col: str | None) -> str | None:
        return col if (col and col.lower() in raw_cols_lc) else None

    def _present_tuple(cols: tuple) -> tuple:
        return tuple(c for c in cols if c.lower() in raw_cols_lc)

    spine_columns: list[str] = []
    role_partition_column = "CAST(NULL AS VARCHAR)"

    if stream.kind == "party":
        # Single-role stream — emit role-specific normalized columns.
        # Role is either static (party_role) or dynamic (party_role_column +
        # party_role_map).
        if stream.party_role:
            role_partition_column = f"CAST('{stream.party_role}' AS VARCHAR)"
        else:
            assert stream.party_role_column and stream.party_role_map
            if stream.party_role_column.lower() not in raw_cols_lc:
                raise RuntimeError(
                    f"{stream.state}/{stream.name}: dynamic party_role_column "
                    f"'{stream.party_role_column}' missing from JSON payload"
                )
            map_cases = " ".join(
                f"WHEN \"{stream.party_role_column}\" = '{src}' "
                f"THEN '{dst}'"
                for src, dst in stream.party_role_map.items()
            )
            role_partition_column = f"CASE {map_cases} ELSE NULL END"

        # Name input: prefer org-name columns, fall back to first/middle/last.
        org_sql = _coalesce_columns_sql(_present_tuple(stream.name_columns))
        ind_sql = _concat_individual_name_sql(
            _present_tuple(stream.individual_name_columns))
        name_input = f"COALESCE({org_sql}, {ind_sql})"
        zip_col = _present(stream.zip_column)
        state_col = _present(stream.state_column)
        spine_columns.extend([
            f"ucc_normalize_party({name_input}) AS party_name_normalized",
            (f'ucc_zip5("{zip_col}") AS party_zip5'
             if zip_col else "CAST(NULL AS VARCHAR) AS party_zip5"),
            (f'ucc_state_norm("{state_col}") AS party_state_normalized'
             if state_col else
             "CAST(NULL AS VARCHAR) AS party_state_normalized"),
        ])

    elif stream.kind == "denormalized":
        # Both parties live in the same row.
        d_org = _coalesce_columns_sql(_present_tuple(stream.debtor_name_columns))
        d_ind = _concat_individual_name_sql(
            _present_tuple(stream.debtor_individual_name_columns))
        d_input = f"COALESCE({d_org}, {d_ind})"
        sp_org = _coalesce_columns_sql(
            _present_tuple(stream.secured_party_name_columns))
        sp_ind = _concat_individual_name_sql(
            _present_tuple(stream.secured_party_individual_name_columns))
        sp_input = f"COALESCE({sp_org}, {sp_ind})"
        d_zip = _present(stream.debtor_zip_column)
        d_state = _present(stream.debtor_state_column)
        sp_zip = _present(stream.secured_party_zip_column)
        sp_state = _present(stream.secured_party_state_column)
        spine_columns.extend([
            f"ucc_normalize_party({d_input}) AS debtor_name_normalized",
            (f'ucc_zip5("{d_zip}") AS debtor_zip5'
             if d_zip else "CAST(NULL AS VARCHAR) AS debtor_zip5"),
            (f'ucc_state_norm("{d_state}") AS debtor_state_normalized'
             if d_state else "CAST(NULL AS VARCHAR) AS debtor_state_normalized"),
            f"ucc_normalize_party({sp_input}) AS secured_party_name_normalized",
            (f'ucc_zip5("{sp_zip}") AS secured_party_zip5'
             if sp_zip else "CAST(NULL AS VARCHAR) AS secured_party_zip5"),
            (f'ucc_state_norm("{sp_state}") AS secured_party_state_normalized'
             if sp_state else
             "CAST(NULL AS VARCHAR) AS secured_party_state_normalized"),
        ])

    elif stream.kind == "collateral":
        coll_col = _present(stream.collateral_description_column)
        if coll_col:
            spine_columns.append(
                f"lower(trim(regexp_replace("
                f'"{coll_col}", \'\\s+\', \' \', \'g\'))) '
                f"AS collateral_description_normalized"
            )

    # Partition metadata (always present).
    spine_columns.extend([
        f"CAST('{stream.state}' AS VARCHAR) AS ucc_state",
        f"CAST('{stream.name}' AS VARCHAR) AS ucc_stream",
        f"CAST('{stream.kind}' AS VARCHAR) AS ucc_stream_kind",
        f"CAST('{snapshot_date.isoformat()}' AS DATE) AS ucc_snapshot_date",
    ])
    if stream.kind == "party":
        spine_columns.append(f"({role_partition_column}) AS party_role")

    select_sql = (
        "SELECT "
        + ", ".join(select_parts + spine_columns)
        + " FROM raw"
        + (f" LIMIT {max_rows}" if max_rows is not None else "")
    )

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info(
        "%s   parquet write: %.1f MB in %.1fs",
        log_prefix,
        parquet_path.stat().st_size / (1 << 20),
        time.monotonic() - t0,
    )

    # Null-rate sanity check on the spine columns.
    rates: dict[str, float] = {}
    if stream.kind == "party":
        rates_row = con.execute(f"""
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE party_name_normalized IS NULL) AS name_null,
              count(*) FILTER (WHERE party_zip5 IS NULL) AS zip_null
            FROM read_parquet('{parquet_path}');
        """).fetchone()
        total = int(rates_row[0]) if rates_row else 0
        if total > 0 and rates_row is not None:
            rates = {
                "party_name_normalized_null_pct":
                    round(100.0 * int(rates_row[1]) / total, 4),
                "party_zip5_null_pct":
                    round(100.0 * int(rates_row[2]) / total, 4),
            }
    elif stream.kind == "denormalized":
        rates_row = con.execute(f"""
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE debtor_name_normalized IS NULL) AS d_name_null,
              count(*) FILTER (WHERE debtor_zip5 IS NULL) AS d_zip_null,
              count(*) FILTER (WHERE secured_party_name_normalized IS NULL) AS sp_name_null,
              count(*) FILTER (WHERE secured_party_zip5 IS NULL) AS sp_zip_null
            FROM read_parquet('{parquet_path}');
        """).fetchone()
        total = int(rates_row[0]) if rates_row else 0
        if total > 0 and rates_row is not None:
            rates = {
                "debtor_name_normalized_null_pct":
                    round(100.0 * int(rates_row[1]) / total, 4),
                "debtor_zip5_null_pct":
                    round(100.0 * int(rates_row[2]) / total, 4),
                "secured_party_name_normalized_null_pct":
                    round(100.0 * int(rates_row[3]) / total, 4),
                "secured_party_zip5_null_pct":
                    round(100.0 * int(rates_row[4]) / total, 4),
            }
    rates_row2 = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}');"
    ).fetchone()
    rows_pq = int(rates_row2[0]) if rates_row2 else 0
    if rates:
        rates_str = ", ".join(f"{k.replace('_null_pct','')}={v:.2f}%"
                              for k, v in rates.items())
        log.info("%s   parquet rows: %s; null-rate %s",
                 log_prefix, f"{rows_pq:,}", rates_str)
    else:
        log.info("%s   parquet rows: %s", log_prefix, f"{rows_pq:,}")
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
              FROM ops.ucc_r2_ingest_runs
             WHERE ucc_state = %s AND ucc_stream = %s AND status = 'completed'
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
    INSERT INTO ops.ucc_r2_ingest_runs (
        ucc_state, ucc_stream, ucc_snapshot_date, status,
        source_url, socrata_dataset_id, stream_kind,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            stream.state, stream.name, snapshot_date,
            stream.csv_url, stream.socrata_id, stream.kind,
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
            INSERT INTO ops.ucc_r2_ingest_runs (
                ucc_state, ucc_stream, ucc_snapshot_date, status,
                source_url, socrata_dataset_id, stream_kind,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, %s, 'no_change', %s, %s, %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                stream.state, stream.name, snapshot_date,
                stream.csv_url, stream.socrata_id, stream.kind,
                source_last_modified, prior_source_last_modified,
                started, started,
                Jsonb({"reason": "rowsUpdatedAt unchanged since prior completed run"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    csv_bytes: int,
    csv_rows: int,
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
            UPDATE ops.ucc_r2_ingest_runs
               SET status = %s,
                   csv_bytes_downloaded = %s,
                   csv_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   debtor_name_normalized_null_pct = %s,
                   debtor_zip5_null_pct = %s,
                   secured_party_name_normalized_null_pct = %s,
                   secured_party_zip5_null_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, csv_bytes, csv_rows,
            parquet_rows, parquet_bytes, parquet_columns,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            # For PARTY-kind streams the spine is single-role; map onto
            # whichever side the role is. The audit columns in the schema
            # cover both debtor + secured_party, with the unused side NULL.
            rates.get("debtor_name_normalized_null_pct")
                or (rates.get("party_name_normalized_null_pct")
                    if notes and notes.get("party_role") == "debtor" else None),
            rates.get("debtor_zip5_null_pct")
                or (rates.get("party_zip5_null_pct")
                    if notes and notes.get("party_role") == "debtor" else None),
            rates.get("secured_party_name_normalized_null_pct")
                or (rates.get("party_name_normalized_null_pct")
                    if notes and notes.get("party_role") == "secured_party" else None),
            rates.get("secured_party_zip5_null_pct")
                or (rates.get("party_zip5_null_pct")
                    if notes and notes.get("party_role") == "secured_party" else None),
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
    log.info("%s start csv_url=%s", log_prefix, stream.csv_url)

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        try:
            freshness = fetch_freshness(client, stream)
        except Exception:
            log.exception("%s metadata fetch failed", log_prefix)
            return 1
        log.info("%s rowsUpdatedAt=%s viewLastModified=%s",
                 log_prefix, freshness.rows_updated_at,
                 freshness.view_last_updated_at)

        if dry_run:
            log.info("%s DRY RUN — exiting after metadata fetch", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, stream)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and freshness.rows_updated_at is not None
                and freshness.rows_updated_at <= prior
            ):
                log.info("%s rowsUpdatedAt unchanged — recording no_change",
                         log_prefix)
                write_no_change_run(
                    conn, stream, snapshot_date,
                    source_last_modified=freshness.rows_updated_at,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, stream, snapshot_date,
                source_last_modified=freshness.rows_updated_at,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            jsonl_path = workdir / f"ucc_{stream.state}_{stream.name}.jsonl"
            parquet_path = workdir / f"ucc_{stream.state}_{stream.name}.parquet"

            try:
                rows_pulled, jsonl_bytes = download_json_pages(
                    client, stream, jsonl_path, max_rows=max_rows,
                )
                log.info("%s downloaded %s rows / %.1f MB JSONL",
                         log_prefix, f"{rows_pulled:,}",
                         jsonl_bytes / (1 << 20))

                rows_in, rows_pq, null_rates = json_to_parquet(
                    jsonl_path, parquet_path,
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
                    f"ucc/state={stream.state}/stream={stream.name}/"
                    f"snapshot={snapshot_date.isoformat()}/"
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
                    "socrata_id": stream.socrata_id,
                    "stream_kind": stream.kind,
                    "title": stream.title,
                    "party_role": stream.party_role,
                    "party_role_column": stream.party_role_column,
                    "jsonl_bytes_downloaded": jsonl_bytes,
                    "rows_pulled_from_socrata": rows_pulled,
                }
                finalize_run_row(
                    conn, run_id, status="completed",
                    csv_bytes=jsonl_bytes, csv_rows=rows_in,
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
                    csv_bytes=0, csv_rows=0,
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
                    jsonl_path.unlink(missing_ok=True)
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
                   help="One or more stream identifiers: STATE (e.g., 'CO'), "
                        "STATE/NAME (e.g., 'CO/filings'), or Socrata 4×4 id.")
    p.add_argument("--all", action="store_true",
                   help="Ingest every stream in STREAMS (CO×4 + CT×1 + OR×2).")
    p.add_argument("--snapshot-date", default=None,
                   help="Override the snapshot partition date (YYYY-MM-DD). "
                        "Default: today (UTC).")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical ucc/state=ST/stream=NAME/snapshot=… "
                        "prefix (smoke-test use).")
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
    workdir = Path(args.workdir or "/tmp/ucc_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    log.info("UCC R2 ingest — %d streams, snapshot=%s",
             len(targets), snapshot_date)

    rc = 0
    for stream in targets:
        log.info("=" * 70)
        log.info("=== INGEST: %s/%s (kind=%s) ===",
                 stream.state, stream.name, stream.kind)
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

    # Best-effort cleanup of the workdir if empty.
    try:
        if workdir.exists() and not any(workdir.iterdir()):
            shutil.rmtree(workdir)
    except Exception:
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
