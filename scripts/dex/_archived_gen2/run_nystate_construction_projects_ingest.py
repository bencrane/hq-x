#!/usr/bin/env python3
"""NY State Construction Projects (Socrata, data.ny.gov) — multi-dataset ingest.

Five feeds covering DASNY (Dormitory Authority — state-facility construction PM)
and NYSTA (Thruway Authority — capital program):

  dasny-active-projects         ekci-x6aq  DASNY Active Construction Projects
  nysta-active-capital          f9da-b8zj  NYSTA Active Capital Projects
  nysta-planned-capital         4kng-zbqe  NYSTA Planned Capital Projects
  nysta-capital-programs-2005   n5iq-rskv  NYSTA Capital Projects, Capital Programs: Beginning 2005
  nysta-completed-2005          t7xk-vv89  NYSTA Completed Capital Projects: Beginning 2005

Source-first per CLAUDE.md (2026-04-16): each feed lands in its own
entities.source_* table. No identity resolution, no canonical merge.

Auth: Basic Auth via SOCRATA_API_KEY_ID + SOCRATA_API_KEY_SECRET (preferred,
already in Doppler), falling back to SOCRATA_APP_TOKEN (X-App-Token), and
finally to unauthenticated access. NY State feeds are small (~1k rows total)
so unauthenticated is acceptable — divergence from the NYC ingest, which raises.

Pagination: $limit + $offset, $order=:id, $select=*,:id,:created_at,:updated_at.
Idempotency: PK on socrata_id (Socrata :id), ON CONFLICT DO UPDATE.
Skip-if-unchanged: compare metadata.rowsUpdatedAt to prior successful run.
Audit: ops.nystate_opendata_ingest_runs.

Recon report: after each successful ingest, emits a stdout block summarizing
total rows, named-contractor field population, status distinct values, and
date range. Drives the decision on whether to promote to a Trigger.dev refresh
task.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_nystate_construction_projects_ingest.py nysta-active-capital
  PYTHONPATH=. doppler run -- python3 scripts/run_nystate_construction_projects_ingest.py all
  PYTHONPATH=. doppler run -- python3 scripts/run_nystate_construction_projects_ingest.py all --recon-only
  PYTHONPATH=. doppler run -- python3 scripts/run_nystate_construction_projects_ingest.py dasny-active-projects --dry-run --max-pages 1
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
    return logging.getLogger("nystate-construction-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Type coercers (from raw Socrata JSON values)
# --------------------------------------------------------------------------- #


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
    """ISO 8601 calendar_date string (e.g. '2012-12-12T00:00:00.000') -> date."""
    if value is None or value == "":
        return None
    return str(value)[:10]


def coerce_jsonb(value: Any) -> Jsonb | None:
    """Socrata 'point' / 'location' types come back as JSON objects.
    Store verbatim as jsonb so both shapes survive without a custom mapping."""
    if value is None or value == "":
        return None
    return Jsonb(value)


def coerce_tstz(value: Any) -> datetime | None:
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
    pg_type: str
    coerce: Callable[[Any], Any]


@dataclass(frozen=True)
class DatasetConfig:
    key: str
    fourxfour: str
    name: str
    schema: str
    table: str
    cols: list[ColSpec] = field(default_factory=list)
    # GTM-relevant column hints for the recon report
    contractor_col: str | None = None  # column most useful as a "named firm"
    secondary_contractor_col: str | None = None
    status_col: str | None = None
    date_col: str | None = None        # primary date for time-window framing

    @property
    def resource_url(self) -> str:
        return f"https://data.ny.gov/resource/{self.fourxfour}.json"

    @property
    def metadata_url(self) -> str:
        return f"https://data.ny.gov/api/views/{self.fourxfour}.json"

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"


# --------------------------------------------------------------------------- #
# Dataset definitions
# --------------------------------------------------------------------------- #

DASNY_ACTIVE = DatasetConfig(
    key="dasny-active-projects",
    fourxfour="ekci-x6aq",
    name="DASNY Active Construction Projects",
    schema="entities",
    table="source_dasny_active_construction_projects",
    cols=[
        ColSpec("snapshotdate",                 "date",          coerce_date),
        ColSpec("institution",                  "text",          coerce_text),
        ColSpec("projectid",                    "text",          coerce_text),
        ColSpec("description",                  "text",          coerce_text),
        ColSpec("county",                       "text",          coerce_text),
        ColSpec("project_budget",               "numeric(14,2)", coerce_numeric),
        ColSpec("construction_start_date",      "text",          coerce_text),
        ColSpec("construction_completion_date", "text",          coerce_text),
        ColSpec("architect",                    "text",          coerce_text),
        ColSpec("construction_manager",         "text",          coerce_text),
    ],
    contractor_col="architect",
    secondary_contractor_col="construction_manager",
    status_col=None,
    date_col="snapshotdate",
)

# Shared NYSTA base column list (Active / Planned / Completed all use this).
_NYSTA_BASE_COLS: list[ColSpec] = [
    ColSpec("source",                       "text",          coerce_text),
    ColSpec("status",                       "text",          coerce_text),
    ColSpec("program_year",                 "text",          coerce_text),
    ColSpec("id",                           "text",          coerce_text),
    ColSpec("division",                     "text",          coerce_text),
    ColSpec("description",                  "text",          coerce_text),
    ColSpec("estimated_letting_date",       "text",          coerce_text),
    ColSpec("letting_date",                 "date",          coerce_date),
    ColSpec("estimated_completion_date",    "text",          coerce_text),
    ColSpec("completion_date",              "date",          coerce_date),
    ColSpec("contractor",                   "text",          coerce_text),
    ColSpec("contract_no",                  "text",          coerce_text),
    ColSpec("low_bid_amount",               "numeric(14,2)", coerce_numeric),
    ColSpec("approved_construction_amount", "numeric(14,2)", coerce_numeric),
    ColSpec("construction_amount",          "numeric(14,2)", coerce_numeric),
    ColSpec("latitude",                     "text",          coerce_text),
    ColSpec("longitude",                    "text",          coerce_text),
    ColSpec("location_1",                   "jsonb",         coerce_jsonb),
]


NYSTA_ACTIVE = DatasetConfig(
    key="nysta-active-capital",
    fourxfour="f9da-b8zj",
    name="NYSTA Active Capital Projects",
    schema="entities",
    table="source_nysta_active_capital_projects",
    cols=list(_NYSTA_BASE_COLS),
    contractor_col="contractor",
    status_col="status",
    date_col="letting_date",
)

NYSTA_PLANNED = DatasetConfig(
    key="nysta-planned-capital",
    fourxfour="4kng-zbqe",
    name="NYSTA Planned Capital Projects",
    schema="entities",
    table="source_nysta_planned_capital_projects",
    cols=list(_NYSTA_BASE_COLS),
    contractor_col="contractor",  # exists but expected null on every row
    status_col="status",
    date_col="letting_date",
)

NYSTA_2005 = DatasetConfig(
    key="nysta-capital-programs-2005",
    fourxfour="n5iq-rskv",
    name="NYSTA Capital Projects, Capital Programs: Beginning 2005",
    schema="entities",
    table="source_nysta_capital_projects_beginning_2005",
    cols=_NYSTA_BASE_COLS + [
        ColSpec("georeference", "jsonb", coerce_jsonb),
    ],
    contractor_col="contractor",
    status_col="status",
    date_col="letting_date",
)

NYSTA_COMPLETED = DatasetConfig(
    key="nysta-completed-2005",
    fourxfour="t7xk-vv89",
    name="NYSTA Completed Capital Projects: Beginning 2005",
    schema="entities",
    table="source_nysta_completed_capital_projects_beginning_2005",
    cols=list(_NYSTA_BASE_COLS),
    contractor_col="contractor",
    status_col="status",
    date_col="completion_date",
)

DATASETS: dict[str, DatasetConfig] = {
    ds.key: ds for ds in (
        DASNY_ACTIVE,
        NYSTA_ACTIVE,
        NYSTA_PLANNED,
        NYSTA_2005,
        NYSTA_COMPLETED,
    )
}


# --------------------------------------------------------------------------- #
# Auth + DB helpers
# --------------------------------------------------------------------------- #


def _resolve_auth() -> tuple[httpx.Auth | None, dict[str, str], str]:
    key_id = os.environ.get("SOCRATA_API_KEY_ID")
    key_secret = os.environ.get("SOCRATA_API_KEY_SECRET")
    app_token = (
        os.environ.get("SOCRATA_APP_TOKEN")
        or os.environ.get("SOCRATA_API_KEY")
    )
    if key_id and key_secret:
        return httpx.BasicAuth(key_id, key_secret), {}, "basic"
    if app_token:
        return None, {"X-App-Token": app_token}, "app_token"
    log.warning(
        "No Socrata credentials in env (SOCRATA_API_KEY_ID/_SECRET or "
        "SOCRATA_APP_TOKEN). Falling back to unauthenticated access — "
        "rate limits are tighter but acceptable for these small NY State feeds."
    )
    return None, {}, "none"


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
    INSERT INTO ops.nystate_opendata_ingest_runs (
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
              FROM ops.nystate_opendata_ingest_runs
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
            INSERT INTO ops.nystate_opendata_ingest_runs (
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
            UPDATE ops.nystate_opendata_ingest_runs
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
# Recon report
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
    contractor_samples: list[str] = field(default_factory=list)
    null_contractor_socrata_ids: list[str] = field(default_factory=list)
    secondary_contractor_field: str | None = None
    secondary_contractor_non_null: int = 0
    secondary_contractor_distinct: int = 0
    status_field: str | None = None
    status_distinct_values: list[str] = field(default_factory=list)
    date_field: str | None = None
    date_min: str | None = None
    date_max: str | None = None


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
        status_field=ds.status_col,
        date_field=ds.date_col,
    )
    fq = ds.fully_qualified
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {fq};")
        stats.total_rows = int(cur.fetchone()[0])

        if ds.contractor_col is not None:
            col = ds.contractor_col
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL), "
                f"       count(DISTINCT {col}) FROM {fq};"
            )
            non_null, distinct = cur.fetchone()
            stats.contractor_non_null = int(non_null or 0)
            stats.contractor_distinct = int(distinct or 0)
            stats.contractor_present = stats.contractor_non_null > 0

            cur.execute(
                f"SELECT DISTINCT {col} FROM {fq} "
                f"WHERE {col} IS NOT NULL ORDER BY {col} LIMIT 5;"
            )
            stats.contractor_samples = [r[0] for r in cur.fetchall()]

            cur.execute(
                f"SELECT socrata_id FROM {fq} "
                f"WHERE {col} IS NULL ORDER BY socrata_id LIMIT 3;"
            )
            stats.null_contractor_socrata_ids = [r[0] for r in cur.fetchall()]

        if ds.secondary_contractor_col is not None:
            col = ds.secondary_contractor_col
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL), "
                f"       count(DISTINCT {col}) FROM {fq};"
            )
            non_null, distinct = cur.fetchone()
            stats.secondary_contractor_non_null = int(non_null or 0)
            stats.secondary_contractor_distinct = int(distinct or 0)

        if ds.status_col is not None:
            cur.execute(
                f"SELECT DISTINCT {ds.status_col} FROM {fq} "
                f"WHERE {ds.status_col} IS NOT NULL ORDER BY {ds.status_col};"
            )
            stats.status_distinct_values = [r[0] for r in cur.fetchall()]

        if ds.date_col is not None:
            cur.execute(
                f"SELECT min({ds.date_col})::text, max({ds.date_col})::text FROM {fq};"
            )
            d_min, d_max = cur.fetchone()
            stats.date_min = d_min
            stats.date_max = d_max

    return stats


def print_recon_block(stats: ReconStats) -> None:
    print(f"=== RECON: {stats.name} ({stats.fourxfour}) ===")
    print(f"  total rows ingested:     {stats.total_rows}")
    print(f"  dataset_rows_updated_at: {stats.dataset_rows_updated_at}")
    if stats.contractor_field is None:
        print("  named contractor field:  NOT FOUND — manual review required")
    else:
        if stats.contractor_present:
            print(
                f"  named contractor field:  {stats.contractor_field}  "
                f"(non-null: {stats.contractor_non_null}/{stats.total_rows}, "
                f"distinct: {stats.contractor_distinct})"
            )
        else:
            print(
                f"  named contractor field:  {stats.contractor_field}  "
                f"(declared in schema, but ZERO non-null rows in this feed — "
                f"by design for this status)"
            )
    if stats.secondary_contractor_field is not None:
        print(
            f"  secondary firm field:    {stats.secondary_contractor_field}  "
            f"(non-null: {stats.secondary_contractor_non_null}/{stats.total_rows}, "
            f"distinct: {stats.secondary_contractor_distinct})"
        )
    if stats.status_field is not None:
        print(
            f"  status field:            {stats.status_field}  "
            f"(distinct values: {stats.status_distinct_values})"
        )
    else:
        print("  status field:            n/a (single-status feed)")
    if stats.date_field is not None:
        print(
            f"  date range:              {stats.date_min} .. {stats.date_max}  "
            f"(column: {stats.date_field})"
        )
    if stats.null_contractor_socrata_ids:
        print(
            f"  null contractor sample:  {stats.null_contractor_socrata_ids}"
        )
    if stats.contractor_samples:
        print(f"  named contractor sample: {stats.contractor_samples}")
    print("=== END RECON ===")
    print()


def print_cross_dataset_summary(all_stats: list[ReconStats]) -> None:
    print("=== CROSS-DATASET SUMMARY ===")
    total = 0
    for s in all_stats:
        present = "MISSING"
        if s.contractor_field is not None and s.contractor_present:
            present = "present"
        elif s.contractor_field is not None and not s.contractor_present:
            present = "declared/empty"
        label = f"  {s.key}:".ljust(38)
        print(f"{label}{s.total_rows} rows, contractor field {present}")
        total += s.total_rows
    print(f"  total rows landed:                   {total}")
    print("=== END SUMMARY ===")


# --------------------------------------------------------------------------- #
# Per-dataset main
# --------------------------------------------------------------------------- #


def run_recon_only(
    ds: DatasetConfig,
    *,
    page_size: int,
) -> ReconStats | None:
    """Hit metadata + first page (no DB writes), then run the recon SELECTs
    against the existing table contents (assumes a prior ingest landed
    something — if not, total_rows will be 0 and we say so)."""
    auth, headers, auth_method = _resolve_auth()
    headers = {**headers, "User-Agent": "data-engine-x/nystate-construction-ingest"}
    log.info("[%s] RECON-ONLY (4x4=%s table=%s)", ds.key, ds.fourxfour, ds.fully_qualified)

    with httpx.Client(auth=auth, headers=headers) as client:
        metadata = fetch_metadata(client, ds)
        rows_updated_at = _ts_from_unix(metadata.get("rowsUpdatedAt"))
        log.info("[%s] auth=%s rowsUpdatedAt=%s", ds.key, auth_method, rows_updated_at)
        # Light first-page poke to verify the resource endpoint responds.
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


def ingest_dataset(
    ds: DatasetConfig,
    *,
    page_size: int,
    page_sleep: float,
    max_pages: int | None,
    skip_if_unchanged: bool,
    dry_run: bool,
) -> tuple[int, ReconStats | None]:
    auth, headers, auth_method = _resolve_auth()
    headers = {**headers, "User-Agent": "data-engine-x/nystate-construction-ingest"}
    started_wall = time.monotonic()
    log.info("[%s] start (4x4=%s table=%s auth=%s)",
             ds.key, ds.fourxfour, ds.fully_qualified, auth_method)

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
            return 0, None

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
                stats = gather_recon_stats(conn, ds, metadata)
                print_recon_block(stats)
                return 0, stats

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
                stats = gather_recon_stats(conn, ds, metadata)
                print_recon_block(stats)
                return 0, stats
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] ingest failed", ds.key)
                finalize_run_row(
                    conn, run_id, status="failed",
                    pages_fetched=pages_fetched,
                    rows_inserted=total_inserted, rows_updated=total_updated,
                    rows_unchanged=total_unchanged, bytes_downloaded=total_bytes,
                    started_at=started_wall, error_message=str(exc), notes=None,
                )
                return 1, None


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
                   help="Hit metadata + first page, then run the recon-report "
                        "SELECTs against the existing table contents. No "
                        "writes to the source-data tables.")
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
        if len(all_stats) > 1:
            print_cross_dataset_summary(all_stats)
        return 0

    rc = 0
    all_stats = []
    for k in keys:
        ds = DATASETS[k]
        ds_rc, stats = ingest_dataset(
            ds,
            page_size=args.page_size,
            page_sleep=args.page_sleep_seconds,
            max_pages=args.max_pages,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
        )
        rc = rc or ds_rc
        if stats is not None:
            all_stats.append(stats)
    if len(all_stats) > 1:
        print_cross_dataset_summary(all_stats)
    return rc


if __name__ == "__main__":
    sys.exit(main())
