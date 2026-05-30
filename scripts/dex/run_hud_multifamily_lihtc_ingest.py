#!/usr/bin/env python3
"""HUD Multifamily + LIHTC — ArcGIS FeatureServer ingest.

Four datasets from HUD's ArcGIS Hub backend (services.arcgis.com/VTyQ9soqVukalItT):

  multifamily-pipeline    Multifamily_Properties_and_Pipeline       18,951 rows
  insured                 HUD_Insured_Multifamily_Properties        17,324 rows
  lihtc                   LIHTC                                     50,566 rows
  assisted                MULTIFAMILY_PROPERTIES_ASSISTED           23,781 rows

Source-first per CLAUDE.md (2026-04-16): each layer lands in its own
entities.source_hud_* table. No identity resolution, no canonical merge.

The HUD User portal (huduser.gov) bulk downloads return 202 anti-bot
responses — ArcGIS is the canonical machine-readable path. Auth: none.

Pagination: resultOffset/resultRecordCount, max 2000 per page. Stop when
exceededTransferLimit=false AND a partial page is returned. Geometry is
NOT requested (returnGeometry=false).

Date handling: ArcGIS returns esriFieldTypeDate as epoch milliseconds.
The script coerces to timezone-aware datetime for timestamptz columns.

Idempotency: ON CONFLICT DO UPDATE on the natural key.

Skip-if-unchanged: ArcGIS doesn't expose a per-row last-modified header
or a clean dataset-level timestamp. Falls back to total record count
comparison vs the prior successful run. Imperfect but pragmatic.

Audit: ops.hud_arcgis_ingest_runs.

Recon report: per-dataset stdout block summarizing total rows, named-firm
field population, status distribution (where applicable), top firms, top
states. After all datasets succeed, a cross-dataset summary.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_hud_multifamily_lihtc_ingest.py multifamily-pipeline
  PYTHONPATH=. doppler run -- python3 scripts/run_hud_multifamily_lihtc_ingest.py all
  PYTHONPATH=. doppler run -- python3 scripts/run_hud_multifamily_lihtc_ingest.py all --recon-only
  PYTHONPATH=. doppler run -- python3 scripts/run_hud_multifamily_lihtc_ingest.py insured --dry-run --max-pages 1
  PYTHONPATH=. doppler run -- python3 scripts/run_hud_multifamily_lihtc_ingest.py all --skip-if-unchanged
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
from typing import Any, Iterable

import httpx
import psycopg
from psycopg.types.json import Jsonb

ARCGIS_BASE = (
    "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services"
)
DEFAULT_PAGE_SIZE = 2000   # ArcGIS maxRecordCount
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
USER_AGENT = "data-engine-x/hud-arcgis-ingest"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("hud-arcgis-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# ESRI -> Postgres type coercers (apply to incoming JSON values)
# --------------------------------------------------------------------------- #


def coerce_arcgis_date(value: Any) -> datetime | None:
    """ArcGIS dates are epoch milliseconds (int). Return tz-aware UTC datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s != "" else None


def coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coerce_double(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_natural_key_text(value: Any) -> str | None:
    """Natural-key columns are stored as text. Pipeline's Property_ID is an
    ESRI Double in source (integer values like 800212886.0); strip the
    fractional part before stringifying so it joins cleanly to TIER 2/4
    string keys."""
    if value is None or value == "":
        return None
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    return str(value)


def _esri_to_coercer(esri_type: str):
    if esri_type == "esriFieldTypeDate":
        return coerce_arcgis_date
    if esri_type in ("esriFieldTypeInteger", "esriFieldTypeSmallInteger"):
        return coerce_int
    if esri_type in ("esriFieldTypeDouble", "esriFieldTypeSingle"):
        return coerce_double
    return coerce_text


# --------------------------------------------------------------------------- #
# Per-dataset configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColSpec:
    name: str          # ArcGIS field name (mixed case from source)
    pg_name: str       # lowercase Postgres column name
    coerce: Any        # callable: raw_value -> python value


@dataclass(frozen=True)
class DatasetConfig:
    key: str
    service: str
    layer_id: int
    schema: str
    table: str
    natural_key_field: str        # ArcGIS field name (mixed case)
    natural_key_pg: str           # lowercase Postgres column name
    cols: list[ColSpec] = field(default_factory=list)
    # Recon hints
    firm_cols: list[str] = field(default_factory=list)         # pg col names
    status_col: str | None = None
    state_col: str | None = None
    date_col: str | None = None

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"

    @property
    def query_url(self) -> str:
        return f"{ARCGIS_BASE}/{self.service}/FeatureServer/{self.layer_id}/query"

    @property
    def layer_url(self) -> str:
        return f"{ARCGIS_BASE}/{self.service}/FeatureServer/{self.layer_id}"


def _load_schema_fields(service: str, layer_id: int, client: httpx.Client) -> list[dict[str, Any]]:
    url = f"{ARCGIS_BASE}/{service}/FeatureServer/{layer_id}?f=json"
    r = client.get(url, timeout=30.0)
    r.raise_for_status()
    return r.json()["fields"]


def _build_col_specs(
    fields: list[dict[str, Any]],
    natural_key_field: str,
) -> list[ColSpec]:
    """Build the ColSpec list excluding the OID and natural-key fields. Natural
    key is handled separately; OID is stored in arcgis_object_id."""
    specs: list[ColSpec] = []
    for f in fields:
        name = f["name"]
        if f["type"] == "esriFieldTypeOID":
            continue
        if name == natural_key_field:
            continue
        specs.append(ColSpec(
            name=name,
            pg_name=name.lower(),
            coerce=_esri_to_coercer(f["type"]),
        ))
    return specs


# --------------------------------------------------------------------------- #
# Dataset registry — populated lazily once schemas are fetched
# --------------------------------------------------------------------------- #


DATASET_DEFS: list[dict[str, Any]] = [
    {
        "key": "multifamily-pipeline",
        "service": "Multifamily_Properties_and_Pipeline",
        "layer_id": 0,
        "schema": "entities",
        "table": "source_hud_multifamily_pipeline",
        "natural_key_field": "Property_ID",
        "natural_key_pg": "property_id",
        "firm_cols": [],  # NO named-firm fields — HUD-internal officers only
        "status_col": "production_pipeline_y_n",
        "state_col": "state",
        "date_col": None,
    },
    {
        "key": "insured",
        "service": "HUD_Insured_Multifamily_Properties",
        "layer_id": 0,
        "schema": "entities",
        "table": "source_hud_insured_multifamily_properties",
        "natural_key_field": "PROPERTY_ID",
        "natural_key_pg": "property_id",
        "firm_cols": [
            "mgmt_agent_org_name",
            "project_manager_name_text",
            "mgmt_contact_full_name",
            "client_group_name",
            "property_name_text",
            "soa_name1",
        ],
        "status_col": "has_active_financing_ind",
        "state_col": "state2kx",
        "date_col": "loan_maturity_date",
    },
    {
        "key": "lihtc",
        "service": "LIHTC",
        "layer_id": 0,
        "schema": "entities",
        "table": "source_hud_lihtc_projects",
        "natural_key_field": "HUD_ID",
        "natural_key_pg": "hud_id",
        "firm_cols": [],  # HUD scrubbed COMPANY/CONTACT/CO_*
        "status_col": None,
        "state_col": "proj_st",
        "date_col": None,
    },
    {
        "key": "assisted",
        "service": "MULTIFAMILY_PROPERTIES_ASSISTED",
        "layer_id": 0,
        "schema": "entities",
        "table": "source_hud_multifamily_properties_assisted",
        "natural_key_field": "PROPERTY_ID",
        "natural_key_pg": "property_id",
        "firm_cols": [
            "mgmt_agent_org_name",
            "project_manager_name_text",
            "mgmt_contact_full_name",
            "client_group_name",
            "property_name_text",
            "soa_name1",
        ],
        "status_col": "has_active_assistance_ind",
        "state_col": "state2kx",
        "date_col": "loan_maturity_date",
    },
]


def build_datasets(client: httpx.Client) -> dict[str, DatasetConfig]:
    out: dict[str, DatasetConfig] = {}
    for d in DATASET_DEFS:
        fields = _load_schema_fields(d["service"], d["layer_id"], client)
        cols = _build_col_specs(fields, d["natural_key_field"])
        out[d["key"]] = DatasetConfig(
            key=d["key"],
            service=d["service"],
            layer_id=d["layer_id"],
            schema=d["schema"],
            table=d["table"],
            natural_key_field=d["natural_key_field"],
            natural_key_pg=d["natural_key_pg"],
            cols=cols,
            firm_cols=list(d["firm_cols"]),
            status_col=d["status_col"],
            state_col=d["state_col"],
            date_col=d["date_col"],
        )
    return out


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
# SQL builders
# --------------------------------------------------------------------------- #


def stage_create_sql(ds: DatasetConfig) -> str:
    # All staging columns as text/jsonb-friendly to avoid type-mismatch on COPY;
    # we convert in Python before COPY, so use the actual target types via a LIKE.
    # Use LIKE on the target table to inherit types — simplest correct path.
    return (
        f"CREATE TEMP TABLE IF NOT EXISTS {ds.stage_table} "
        f"(LIKE {ds.fully_qualified} INCLUDING DEFAULTS);"
    )


def truncate_stage_sql(ds: DatasetConfig) -> str:
    return f"TRUNCATE {ds.stage_table};"


def copy_sql(ds: DatasetConfig) -> str:
    cols = (
        [ds.natural_key_pg]
        + [c.pg_name for c in ds.cols]
        + ["arcgis_object_id", "source_service_name", "source_fetched_at"]
    )
    return f"COPY {ds.stage_table} ({', '.join(cols)}) FROM STDIN"


def upsert_sql(ds: DatasetConfig) -> str:
    natural_cols = [c.pg_name for c in ds.cols]
    bookkeeping_cols = ["arcgis_object_id", "source_service_name", "source_fetched_at"]
    target_cols = (
        [ds.natural_key_pg]
        + natural_cols
        + bookkeeping_cols
        + ["ingested_at"]
    )
    select_cols = (
        [ds.natural_key_pg]
        + natural_cols
        + bookkeeping_cols
        + ["now()"]
    )
    update_assigns = ",\n      ".join(
        f"{c} = EXCLUDED.{c}" for c in (natural_cols + bookkeeping_cols)
    ) + ",\n      ingested_at = now()"
    where_clause = " OR ".join(
        f"{ds.fully_qualified}.{c} IS DISTINCT FROM EXCLUDED.{c}"
        for c in (natural_cols + ["arcgis_object_id"])
    )
    # DISTINCT ON dedupes within a single page: HUD's Pipeline dataset has true
    # duplicate rows (same Property_ID, different OBJECTID) — picking the lowest
    # OBJECTID is arbitrary but stable across re-ingests. No-op for datasets
    # where the natural key is genuinely unique.
    return f"""
WITH staged_unique AS (
  SELECT DISTINCT ON ({ds.natural_key_pg}) *
    FROM {ds.stage_table}
   ORDER BY {ds.natural_key_pg}, arcgis_object_id
), upserted AS (
  INSERT INTO {ds.fully_qualified} ({', '.join(target_cols)})
  SELECT {', '.join(select_cols)}
    FROM staged_unique
   ON CONFLICT ({ds.natural_key_pg}) DO UPDATE SET
      {update_assigns}
   WHERE {where_clause}
   RETURNING (xmax = 0) AS inserted
)
SELECT
  count(*) FILTER (WHERE inserted)     AS rows_inserted,
  count(*) FILTER (WHERE NOT inserted) AS rows_updated
FROM upserted;
"""


def row_to_tuple(
    ds: DatasetConfig,
    feat: dict[str, Any],
    *,
    source_service_name: str,
    source_fetched_at: datetime,
) -> tuple[Any, ...]:
    attrs = feat["attributes"]
    nat_key = coerce_natural_key_text(attrs.get(ds.natural_key_field))
    values = [c.coerce(attrs.get(c.name)) for c in ds.cols]
    object_id = coerce_int(attrs.get("OBJECTID"))
    return (
        nat_key,
        *values,
        object_id,
        source_service_name,
        source_fetched_at,
    )


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    ds: DatasetConfig,
    *,
    page_size: int,
    total_records_in_source: int,
    prior_total: int | None,
) -> str:
    sql = """
    INSERT INTO ops.hud_arcgis_ingest_runs (
        service_name, table_name, status, source_url,
        total_records_in_source, prior_total_records_in_source, page_size
    ) VALUES (%s, %s, 'running', %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            ds.service, ds.table, ds.layer_url,
            total_records_in_source, prior_total, page_size,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_total(conn: psycopg.Connection, ds: DatasetConfig) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT total_records_in_source
              FROM ops.hud_arcgis_ingest_runs
             WHERE service_name = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (ds.service,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def write_no_change_run(
    conn: psycopg.Connection,
    ds: DatasetConfig,
    *,
    page_size: int,
    total_records_in_source: int,
    prior_total: int | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.hud_arcgis_ingest_runs (
                service_name, table_name, status, source_url,
                total_records_in_source, prior_total_records_in_source,
                page_size, started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                ds.service, ds.table, ds.layer_url,
                total_records_in_source, prior_total, page_size,
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
    rows_inserted: int,
    rows_updated: int,
    rows_unchanged: int,
    bytes_downloaded: int,
    started_wall: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_wall, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.hud_arcgis_ingest_runs
               SET status = %s, pages_fetched = %s,
                   rows_inserted = %s, rows_updated = %s, rows_unchanged = %s,
                   bytes_downloaded = %s, finished_at = now(),
                   duration_seconds = %s, error_message = %s, notes = %s
             WHERE id = %s;
            """,
            (
                status, pages_fetched,
                rows_inserted, rows_updated, rows_unchanged,
                bytes_downloaded, duration, error_message,
                Jsonb(notes) if notes else None, run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-page work
# --------------------------------------------------------------------------- #


def ensure_stage_table(conn: psycopg.Connection, ds: DatasetConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(stage_create_sql(ds))
    conn.commit()


def upsert_page(
    conn: psycopg.Connection,
    ds: DatasetConfig,
    rows: Iterable[dict[str, Any]],
    *,
    source_service_name: str,
    source_fetched_at: datetime,
) -> tuple[int, int, int]:
    rows_list = list(rows)
    page_size = len(rows_list)
    if page_size == 0:
        return 0, 0, 0
    with conn.cursor() as cur:
        cur.execute(truncate_stage_sql(ds))
        with cur.copy(copy_sql(ds)) as copy:
            for feat in rows_list:
                copy.write_row(row_to_tuple(
                    ds, feat,
                    source_service_name=source_service_name,
                    source_fetched_at=source_fetched_at,
                ))
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
    service: str
    table: str
    total_rows: int = 0
    distinct_natural_keys: int = 0
    firm_field_pops: dict[str, tuple[int, int]] = field(default_factory=dict)
    # per-firm-col: (non_null, distinct)
    top_firms: list[tuple[str, str, int]] = field(default_factory=list)
    # (firm_col, value, count)
    status_distribution: list[tuple[str, int]] = field(default_factory=list)
    top_states: list[tuple[str, int]] = field(default_factory=list)
    date_min: str | None = None
    date_max: str | None = None
    pipeline_y_count: int | None = None
    pipeline_n_count: int | None = None
    project_status_distribution: list[tuple[str, int]] = field(default_factory=list)
    yr_alloc_buckets: list[tuple[str, int]] = field(default_factory=list)


def gather_recon_stats(
    conn: psycopg.Connection,
    ds: DatasetConfig,
) -> ReconStats:
    stats = ReconStats(key=ds.key, service=ds.service, table=ds.table)
    fq = ds.fully_qualified
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {fq};")
        stats.total_rows = int(cur.fetchone()[0])

        cur.execute(f"SELECT count(DISTINCT {ds.natural_key_pg}) FROM {fq};")
        stats.distinct_natural_keys = int(cur.fetchone()[0])

        # firm-field population + distinct
        for col in ds.firm_cols:
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL), "
                f"       count(DISTINCT {col}) FROM {fq};"
            )
            non_null, distinct = cur.fetchone()
            stats.firm_field_pops[col] = (int(non_null or 0), int(distinct or 0))

            # Top 5 firms per col (compact view)
            cur.execute(
                f"SELECT {col}, count(*) AS cnt FROM {fq} "
                f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY cnt DESC LIMIT 5;"
            )
            for v, c in cur.fetchall():
                stats.top_firms.append((col, v, int(c)))

        # Status distribution
        if ds.status_col:
            cur.execute(
                f"SELECT {ds.status_col}, count(*) FROM {fq} "
                f"GROUP BY {ds.status_col} ORDER BY count(*) DESC;"
            )
            stats.status_distribution = [(r[0], int(r[1])) for r in cur.fetchall()]

        # Top states
        if ds.state_col:
            cur.execute(
                f"SELECT {ds.state_col}, count(*) FROM {fq} "
                f"WHERE {ds.state_col} IS NOT NULL "
                f"GROUP BY {ds.state_col} ORDER BY count(*) DESC LIMIT 10;"
            )
            stats.top_states = [(r[0], int(r[1])) for r in cur.fetchall()]

        # Date range
        if ds.date_col:
            cur.execute(
                f"SELECT min({ds.date_col})::text, max({ds.date_col})::text "
                f"FROM {fq};"
            )
            r = cur.fetchone()
            stats.date_min, stats.date_max = r[0], r[1]

        # Pipeline-specific extras
        if ds.key == "multifamily-pipeline":
            cur.execute(
                f"SELECT production_pipeline_y_n, count(*) FROM {fq} "
                f"GROUP BY production_pipeline_y_n;"
            )
            for v, c in cur.fetchall():
                if v == "Y":
                    stats.pipeline_y_count = int(c)
                elif v == "N":
                    stats.pipeline_n_count = int(c)
            cur.execute(
                f"SELECT project_status, count(*) FROM {fq} "
                f"WHERE project_status IS NOT NULL "
                f"GROUP BY project_status ORDER BY count(*) DESC;"
            )
            stats.project_status_distribution = [(r[0], int(r[1])) for r in cur.fetchall()]

        # LIHTC-specific yr_alloc buckets
        if ds.key == "lihtc":
            cur.execute(
                f"SELECT "
                f"  CASE "
                f"    WHEN yr_alloc IS NULL THEN 'NULL' "
                f"    WHEN yr_alloc::int < 1990 THEN '1987-1989' "
                f"    WHEN yr_alloc::int < 2000 THEN '1990-1999' "
                f"    WHEN yr_alloc::int < 2010 THEN '2000-2009' "
                f"    WHEN yr_alloc::int < 2020 THEN '2010-2019' "
                f"    WHEN yr_alloc::int < 9000 THEN '2020+' "
                f"    ELSE 'sentinel(>=9000)' "
                f"  END AS bucket, "
                f"  count(*) "
                f"FROM {fq} "
                f"GROUP BY 1 ORDER BY 1;"
            )
            stats.yr_alloc_buckets = [(r[0], int(r[1])) for r in cur.fetchall()]

    return stats


def print_recon_block(stats: ReconStats) -> None:
    print(f"=== RECON: {stats.service} ({stats.key}) ===")
    print(f"  table:                 entities.{stats.table}")
    print(f"  total rows ingested:   {stats.total_rows}")
    print(f"  distinct natural keys: {stats.distinct_natural_keys} "
          f"({'unique' if stats.distinct_natural_keys == stats.total_rows else 'DUPLICATES PRESENT'})")

    if stats.firm_field_pops:
        print("  named-firm field population:")
        for col, (nn, dist) in stats.firm_field_pops.items():
            pct = (nn / stats.total_rows * 100) if stats.total_rows else 0
            print(f"    {col:35s} non-null: {nn:>6}/{stats.total_rows} ({pct:5.1f}%) distinct: {dist}")
    else:
        print("  named-firm fields:     none in this dataset (HUD-internal officers only or scrubbed)")

    if stats.top_firms:
        print("  top 5 per firm field:")
        last_col = None
        for col, val, cnt in stats.top_firms:
            if col != last_col:
                print(f"    -- {col} --")
                last_col = col
            print(f"      {cnt:5d}  {val}")

    if stats.status_distribution:
        print("  status distribution:")
        for v, c in stats.status_distribution[:10]:
            print(f"    {str(v):40s} {c}")

    if stats.top_states:
        print("  top 10 states:")
        for v, c in stats.top_states:
            print(f"    {v}: {c}")

    if stats.date_min is not None:
        print(f"  date range: {stats.date_min} .. {stats.date_max}")

    if stats.key == "multifamily-pipeline":
        print(f"  Production_Pipeline_Y_N: Y={stats.pipeline_y_count}, N={stats.pipeline_n_count}")
        if stats.project_status_distribution:
            print("  project_status distribution (active pipeline only):")
            for v, c in stats.project_status_distribution:
                print(f"    {v:45s} {c}")

    if stats.key == "lihtc" and stats.yr_alloc_buckets:
        print("  yr_alloc bucketed:")
        for v, c in stats.yr_alloc_buckets:
            print(f"    {v}: {c}")

    print("=== END RECON ===")
    print()


def print_cross_dataset_summary(all_stats: list[ReconStats]) -> None:
    print("=== CROSS-DATASET SUMMARY ===")
    total = 0
    for s in all_stats:
        if s.firm_field_pops:
            populated = sum(1 for nn, _ in s.firm_field_pops.values() if nn > 0)
            nf_label = f"firm fields {populated}/{len(s.firm_field_pops)} populated"
        else:
            nf_label = "no firm fields"
        print(f"  {s.key:25s} {s.total_rows:>7} rows  ({nf_label})")
        total += s.total_rows
    print(f"  {'TOTAL':25s} {total:>7} rows landed")

    # Cross-table top firms (TIER 2 + TIER 4 share field names)
    print()
    print("  Cross-table NOTE: TIER 2 and TIER 4 share PROPERTY_ID; the same")
    print("  property may appear in both with different management firms over")
    print("  the FHA-insurance vs Section-8-PB lifecycle. No deduping happens")
    print("  here — that's a downstream audience-build concern.")
    print("=== END SUMMARY ===")


# --------------------------------------------------------------------------- #
# Per-dataset main
# --------------------------------------------------------------------------- #


def run_recon_only(ds: DatasetConfig) -> ReconStats | None:
    log.info("[%s] RECON-ONLY (service=%s table=%s)",
             ds.key, ds.service, ds.fully_qualified)
    try:
        with psycopg.connect(_database_url()) as conn:
            stats = gather_recon_stats(conn, ds)
    except psycopg.errors.UndefinedTable:
        log.error("[%s] table %s does not exist — apply the migration first.",
                  ds.key, ds.fully_qualified)
        return None
    print_recon_block(stats)
    return stats


def ingest_dataset(
    ds: DatasetConfig,
    client: httpx.Client,
    *,
    page_size: int,
    page_sleep: float,
    max_pages: int | None,
    skip_if_unchanged: bool,
    dry_run: bool,
) -> tuple[int, ReconStats | None]:
    started_wall = time.monotonic()
    fetched_at = datetime.now(timezone.utc)
    log.info("[%s] start (service=%s table=%s)",
             ds.key, ds.service, ds.fully_qualified)

    log.info("[%s] fetching total record count from source", ds.key)
    total_in_source = fetch_total_count(client, ds)
    log.info("[%s] source reports %d total records", ds.key, total_in_source)

    if dry_run:
        log.info("[%s] DRY RUN — first %s page(s), no DB writes",
                 ds.key, max_pages or 1)
        offset = 0
        for page_idx in range(max_pages or 1):
            rows, exceeded, nbytes = fetch_page(
                client, ds, page_size=page_size, offset=offset,
            )
            log.info("[%s]   page %s: %s rows, exceeded=%s, %s bytes",
                     ds.key, page_idx, len(rows), exceeded, nbytes)
            if rows:
                log.info("[%s]   sample attrs: %s", ds.key,
                         json.dumps(rows[0].get("attributes", {}), default=str)[:400])
            if not exceeded or len(rows) < page_size:
                break
            offset += page_size
            time.sleep(page_sleep)
        return 0, None

    with psycopg.connect(_database_url()) as conn:
        prior_total = get_prior_total(conn, ds)
        log.info("[%s] prior successful total_records_in_source: %s",
                 ds.key, prior_total)
        if (
            skip_if_unchanged
            and prior_total is not None
            and prior_total == total_in_source
        ):
            log.info("[%s] total_records_in_source unchanged — recording no_change",
                     ds.key)
            write_no_change_run(
                conn, ds, page_size=page_size,
                total_records_in_source=total_in_source,
                prior_total=prior_total,
            )
            stats = gather_recon_stats(conn, ds)
            print_recon_block(stats)
            return 0, stats

        run_id = insert_run_row(
            conn, ds, page_size=page_size,
            total_records_in_source=total_in_source,
            prior_total=prior_total,
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
                rows, exceeded, nbytes = fetch_page(
                    client, ds, page_size=page_size, offset=offset,
                )
                pages_fetched += 1
                total_bytes += nbytes
                ins, upd, unch = upsert_page(
                    conn, ds, rows,
                    source_service_name=ds.service,
                    source_fetched_at=fetched_at,
                )
                total_inserted += ins
                total_updated += upd
                total_unchanged += unch
                log.info(
                    "[%s] page %s: fetched=%s ins=%s upd=%s unch=%s "
                    "exceeded=%s bytes=%s elapsed=%.1fs",
                    ds.key, pages_fetched, len(rows), ins, upd, unch,
                    exceeded, nbytes, time.monotonic() - page_started,
                )
                if not exceeded or len(rows) < page_size:
                    break
                offset += page_size
                time.sleep(page_sleep)

            finalize_run_row(
                conn, run_id, status="completed",
                pages_fetched=pages_fetched,
                rows_inserted=total_inserted, rows_updated=total_updated,
                rows_unchanged=total_unchanged, bytes_downloaded=total_bytes,
                started_wall=started_wall, error_message=None, notes=None,
            )
            log.info(
                "[%s] DONE — pages=%s ins=%s upd=%s unch=%s bytes=%s wall=%.1fs",
                ds.key, pages_fetched, total_inserted, total_updated,
                total_unchanged, total_bytes,
                time.monotonic() - started_wall,
            )
            stats = gather_recon_stats(conn, ds)
            print_recon_block(stats)
            return 0, stats
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] ingest failed", ds.key)
            # Roll back any aborted transaction so finalize can write the audit row.
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            finalize_run_row(
                conn, run_id, status="failed",
                pages_fetched=pages_fetched,
                rows_inserted=total_inserted, rows_updated=total_updated,
                rows_unchanged=total_unchanged, bytes_downloaded=total_bytes,
                started_wall=started_wall, error_message=str(exc), notes=None,
            )
            return 1, None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


ALL_KEYS = [d["key"] for d in DATASET_DEFS]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "dataset", choices=ALL_KEYS + ["all"],
        help="Dataset key, or 'all' to run every dataset sequentially.",
    )
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--max-pages", type=int, default=None,
                   help="Stop after N pages (smoke test).")
    p.add_argument("--page-sleep-seconds", type=float, default=0.25)
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if total_records_in_source has not advanced "
                        "since the prior successful run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch pages but do not write to the DB.")
    p.add_argument("--recon-only", action="store_true",
                   help="Run the recon-report SELECTs against existing table "
                        "contents only. No HTTP fetches, no DB writes to "
                        "source-data tables.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    keys = ALL_KEYS if args.dataset == "all" else [args.dataset]

    if args.recon_only:
        all_stats: list[ReconStats] = []
        with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
            datasets = build_datasets(client)
        for k in keys:
            ds = datasets[k]
            s = run_recon_only(ds)
            if s is not None:
                all_stats.append(s)
        if len(all_stats) > 1:
            print_cross_dataset_summary(all_stats)
        return 0

    rc = 0
    all_stats = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        log.info("loading ArcGIS schemas for %d dataset(s)", len(keys))
        datasets = build_datasets(client)
        for k in keys:
            ds = datasets[k]
            log.info("[%s] schema: %d non-key/non-OID fields",
                     ds.key, len(ds.cols))
            ds_rc, stats = ingest_dataset(
                ds, client,
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
