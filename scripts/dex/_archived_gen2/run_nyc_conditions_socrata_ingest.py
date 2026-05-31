#!/usr/bin/env python3
"""NYC DOB + HPD + FDNY building conditions & events ingest from Socrata.

Self-contained sibling of scripts/run_nyc_opendata_socrata_ingest.py
(landed by the parallel HPD+DOB executor). The SocrataClient class is
intentionally duplicated here to avoid touching the HPD+DOB script
during parallel execution. A follow-up cleanup directive will DRY both
into scripts/lib/socrata_client.py post-merge.

Both scripts write to the same `ops.nyc_opendata_ingest_runs` audit
table — that is the intentional integration point.

Datasets:

    dob-fisp                 xubg-57si  DOB NOW: Safety – Facades Compliance Filings
    dob-stalled              i296-73x5  DOB Stalled Construction Sites      (SWO substitute)
    fdny-vacates             n5xc-7jfa  NYC Fire Department Building Vacate List (DOB Vacate substitute)
    hpd-vacates              tb8q-a3ar  HPD Order to Repair / Vacate Orders
    dob-boiler               52dp-yji6  DOB NOW: Safety Boiler
    dob-elevator             e5aq-a4j2  DOB NOW Elevator Safety Compliance
    fdny-dispatches          8m42-w767  Fire Incident Dispatch Data         (largest, ~12M rows)
    nyc-ll84-energy          5zyy-y8am  NYC Building Energy & Water Disclosure for LL84 2023+

Usage:

    PYTHONPATH=. doppler run -- python3 scripts/run_nyc_conditions_socrata_ingest.py dob-fisp
    PYTHONPATH=. doppler run -- python3 scripts/run_nyc_conditions_socrata_ingest.py all

    # Smoke test (first page only, no DB writes):
    PYTHONPATH=. doppler run -- python3 scripts/run_nyc_conditions_socrata_ingest.py dob-fisp --dry-run --max-pages 1
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from psycopg import sql
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("nyc-conditions-ingest")


# --------------------------------------------------------------------------- #
# SocrataClient — inlined per parallel-execution coordination (see docstring). #
# --------------------------------------------------------------------------- #


class SocrataClient:
    """Minimal Socrata SODA v2 client with paginated fetch + retry/backoff.

    Auth precedence:
      1. Basic Auth via SOCRATA_API_KEY_ID + SOCRATA_API_KEY_SECRET (preferred,
         higher rate limits).
      2. App-token via SOCRATA_API_KEY (legacy fallback).

    Pagination uses $limit + $offset with $order=:id for stable ordering and
    $select=*,:id,:created_at,:updated_at to capture Socrata system columns.
    """

    DEFAULT_PAGE_SIZE = 50000
    DEFAULT_MAX_RETRIES = 5
    DEFAULT_TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        domain: str = "data.cityofnewyork.us",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        page_polite_sleep_seconds: float = 1.0,
    ) -> None:
        self.domain = domain
        self.page_polite_sleep_seconds = page_polite_sleep_seconds
        self._auth_method, headers = self._build_auth_headers()
        self._client = httpx.Client(
            base_url=f"https://{domain}",
            headers=headers,
            timeout=timeout,
        )

    @property
    def auth_method(self) -> str:
        return self._auth_method

    def _build_auth_headers(self) -> tuple[str, dict[str, str]]:
        key_id = os.environ.get("SOCRATA_API_KEY_ID")
        key_secret = os.environ.get("SOCRATA_API_KEY_SECRET")
        app_token = os.environ.get("SOCRATA_API_KEY")
        if key_id and key_secret:
            blob = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
            return "basic", {"Authorization": f"Basic {blob}"}
        if app_token:
            return "app_token", {"X-App-Token": app_token}
        log.warning("no SOCRATA credentials in env — using anonymous (low rate limit)")
        return "app_token", {}

    def fetch_metadata(self, dataset_4x4: str) -> dict[str, Any]:
        """Returns the dataset metadata (rowsUpdatedAt, viewLastModified, etc.)."""
        r = self._request_with_retry("GET", f"/api/views/{dataset_4x4}.json")
        return r.json()

    def fetch_pages(
        self,
        dataset_4x4: str,
        chunk_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int | None = None,
    ) -> Iterable[tuple[int, list[dict[str, Any]], int]]:
        """Yield (page_index, rows, byte_count) tuples until exhausted."""
        offset = 0
        page_index = 0
        while True:
            if max_pages is not None and page_index >= max_pages:
                return
            params = {
                "$limit": chunk_size,
                "$offset": offset,
                "$order": ":id",
                "$select": "*,:id,:created_at,:updated_at",
            }
            r = self._request_with_retry(
                "GET",
                f"/resource/{dataset_4x4}.json",
                params=params,
            )
            body = r.content
            try:
                rows = json.loads(body)
            except json.JSONDecodeError:
                log.error("non-JSON response on page %d: %s", page_index, body[:200])
                raise
            yield (page_index, rows, len(body))
            if len(rows) < chunk_size:
                return
            offset += chunk_size
            page_index += 1
            if self.page_polite_sleep_seconds > 0:
                time.sleep(self.page_polite_sleep_seconds)

    def _request_with_retry(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> httpx.Response:
        attempt = 0
        backoff = 2.0
        while True:
            attempt += 1
            try:
                r = self._client.request(method, path, params=params)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt > max_retries:
                    log.error("network error after %d attempts: %s", attempt - 1, e)
                    raise
                log.warning("network error attempt %d/%d: %s — backoff %.1fs",
                            attempt, max_retries, e, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt > max_retries:
                    log.error("HTTP %d after %d attempts", r.status_code, attempt - 1)
                    r.raise_for_status()
                log.warning(
                    "HTTP %d attempt %d/%d on %s — backoff %.1fs",
                    r.status_code, attempt, max_retries, path, backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            r.raise_for_status()
            return r

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Per-dataset registry + DB write helpers.                                    #
# --------------------------------------------------------------------------- #


# Three LL84 columns exceed Postgres's 63-char identifier limit and are
# truncated at CREATE TABLE time. The ingest writes rows by source field
# name; map long Socrata names -> truncated Postgres column names here.
LL84_COLUMN_RENAMES = {
    "aggregate_meter_s_district_steam_number_of_individual_meters_included":
        "aggregate_meter_s_district_steam_number_of_individual_meters_in",
    "aggregate_meter_s_natural_gas_number_of_individual_meters_included":
        "aggregate_meter_s_natural_gas_number_of_individual_meters_inclu",
}


@dataclass
class DatasetSpec:
    name: str               # CLI subcommand name
    dataset_4x4: str
    display_name: str       # for audit table
    target_schema: str
    target_table: str
    pk_columns: tuple[str, ...]
    chunk_size: int = SocrataClient.DEFAULT_PAGE_SIZE
    column_renames: dict[str, str] | None = None  # source name -> column name


REGISTRY: dict[str, DatasetSpec] = {
    "dob-fisp": DatasetSpec(
        name="dob-fisp",
        dataset_4x4="xubg-57si",
        display_name="DOB NOW: Safety – Facades Compliance Filings",
        target_schema="entities",
        target_table="dob_fisp_filings",
        pk_columns=("tr6_no",),
    ),
    "dob-stalled": DatasetSpec(
        name="dob-stalled",
        dataset_4x4="i296-73x5",
        display_name="DOB Stalled Construction Sites",
        target_schema="entities",
        target_table="dob_stalled_construction_sites",
        pk_columns=("complaint_number", "dobrundate"),
    ),
    "fdny-vacates": DatasetSpec(
        name="fdny-vacates",
        dataset_4x4="n5xc-7jfa",
        display_name="NYC Fire Department Building Vacate List",
        target_schema="entities",
        target_table="fdny_building_vacate_list",
        pk_columns=("socrata_id",),
    ),
    "hpd-vacates": DatasetSpec(
        name="hpd-vacates",
        dataset_4x4="tb8q-a3ar",
        display_name="HPD Order to Repair / Vacate Orders",
        target_schema="entities",
        target_table="hpd_vacate_orders",
        pk_columns=("vacate_order_number",),
    ),
    "dob-boiler": DatasetSpec(
        name="dob-boiler",
        dataset_4x4="52dp-yji6",
        display_name="DOB NOW: Safety Boiler",
        target_schema="entities",
        target_table="dob_boiler_inspections",
        pk_columns=("tracking_number",),
    ),
    "dob-elevator": DatasetSpec(
        name="dob-elevator",
        dataset_4x4="e5aq-a4j2",
        display_name="DOB NOW Elevator Safety Compliance",
        target_schema="entities",
        target_table="dob_elevator_inspections",
        pk_columns=("device_number",),
    ),
    "fdny-dispatches": DatasetSpec(
        name="fdny-dispatches",
        dataset_4x4="8m42-w767",
        display_name="FDNY Fire Incident Dispatch Data",
        target_schema="entities",
        target_table="fdny_fire_incident_dispatches",
        pk_columns=("starfire_incident_id",),
        chunk_size=25000,  # large dataset, smaller pages = lower memory pressure
    ),
    "nyc-ll84-energy": DatasetSpec(
        name="nyc-ll84-energy",
        dataset_4x4="5zyy-y8am",
        display_name="NYC Building Energy & Water Disclosure for LL84 2023+",
        target_schema="entities",
        target_table="nyc_ll84_energy_disclosure",
        pk_columns=("report_year", "property_id"),
        chunk_size=10000,  # 265 cols wide, smaller pages keep payloads sane
        column_renames=LL84_COLUMN_RENAMES,
    ),
}


_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
        if not url:
            raise RuntimeError("DEX_DB_URL_DIRECT or DEX_DB_URL_POOLED must be set")
        _pool = ConnectionPool(conninfo=url, min_size=1, max_size=4, open=True)
    return _pool


def get_table_columns(spec: DatasetSpec) -> list[str]:
    """Returns the ordered column list of the target table (from information_schema).

    Excludes generated columns — they cannot appear in INSERT column lists.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
              AND is_generated = 'NEVER'
            ORDER BY ordinal_position
            """,
            (spec.target_schema, spec.target_table),
        )
        return [r[0] for r in cur.fetchall()]


def _coerce_for_copy(raw: Any) -> str:
    """Render a value for COPY ... FROM STDIN (text format)."""
    if raw is None or raw == "":
        return r"\N"
    if isinstance(raw, (dict, list)):
        s = json.dumps(raw)
    else:
        s = str(raw)
    # Escape backslash, then tab/newline/carriage return for COPY text format.
    s = s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    return s


def upsert_rows(
    spec: DatasetSpec,
    rows: list[dict[str, Any]],
    table_columns: list[str],
    dataset_rows_updated_at: datetime | None,
) -> tuple[int, int]:
    """Bulk upsert a page via COPY into a temp table, then INSERT ... SELECT ...
    ON CONFLICT into the target. Returns (rows_inserted, rows_updated).
    """
    if not rows:
        return (0, 0)

    renames = spec.column_renames or {}
    insert_cols = [c for c in table_columns if c != "ingested_at"]
    update_cols = [c for c in insert_cols if c not in spec.pk_columns]

    fq_table = sql.SQL("{}.{}").format(
        sql.Identifier(spec.target_schema),
        sql.Identifier(spec.target_table),
    )
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in insert_cols)
    pk_idents = sql.SQL(", ").join(sql.Identifier(c) for c in spec.pk_columns)
    update_set = sql.SQL(", ").join(
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
        for c in update_cols
    )

    # Build all rows' tab-separated payloads first so we can stream via COPY.
    # Rows with NULL in any PK column are skipped (PK NOT NULL would block COPY);
    # we count them as skipped and log a warning so the gap is visible.
    rendered: list[bytes] = []
    skipped_null_pk = 0
    rendered_dt = (
        dataset_rows_updated_at.isoformat() if dataset_rows_updated_at else r"\N"
    )
    for raw_row in rows:
        renamed: dict[str, Any] = {}
        for k, v in raw_row.items():
            renamed[renames.get(k, k)] = v
        # Skip if any PK column is missing or empty.
        if any(renamed.get(pk) in (None, "") for pk in spec.pk_columns):
            skipped_null_pk += 1
            continue
        cells: list[str] = []
        for col in insert_cols:
            if col == "socrata_id":
                cells.append(_coerce_for_copy(renamed.get(":id")))
            elif col == "socrata_created_at":
                cells.append(_coerce_for_copy(renamed.get(":created_at")))
            elif col == "socrata_updated_at":
                cells.append(_coerce_for_copy(renamed.get(":updated_at")))
            elif col == "dataset_rows_updated_at":
                cells.append(rendered_dt)
            else:
                cells.append(_coerce_for_copy(renamed.get(col)))
        rendered.append(("\t".join(cells) + "\n").encode("utf-8"))
    if skipped_null_pk:
        log.warning(
            "[%s] skipped %d rows with NULL PK %s in this page",
            spec.name, skipped_null_pk, spec.pk_columns,
        )
    if not rendered:
        return (0, 0)

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        # Temp table mirrors the target's column types (LIKE ... INCLUDING DEFAULTS
        # would resurrect the ingested_at default, so spell out the columns we care
        # about by creating an empty staging table from the target's structure).
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE _stage (LIKE {tbl} INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            ).format(tbl=fq_table)
        )
        # ingested_at is in the target via DEFAULT now() — keep it; the INSERT
        # below names columns explicitly so the default applies on insert.
        copy_sql = sql.SQL(
            "COPY _stage ({cols}) FROM STDIN WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
        ).format(cols=col_idents)
        with cur.copy(copy_sql) as cp:
            for line in rendered:
                cp.write(line)

        # Datasets sometimes ship duplicate rows on the same PK within a single
        # page (Socrata system-row revisions, multi-cycle filings appearing twice,
        # etc). DISTINCT ON keeps the most recently updated revision; without
        # this the upsert raises CardinalityViolation.
        upsert_sql = sql.SQL(
            "WITH ins AS ("
            "  INSERT INTO {tbl} ({cols}) "
            "  SELECT DISTINCT ON ({pk}) {cols} FROM _stage "
            "  ORDER BY {pk}, socrata_updated_at DESC NULLS LAST "
            "  ON CONFLICT ({pk}) DO UPDATE SET {upd} "
            "  RETURNING (xmax = 0) AS inserted"
            ") SELECT "
            "  COUNT(*) FILTER (WHERE inserted) AS ins_count, "
            "  COUNT(*) FILTER (WHERE NOT inserted) AS upd_count "
            "FROM ins"
        ).format(
            tbl=fq_table,
            cols=col_idents,
            pk=pk_idents,
            upd=update_set,
        )
        cur.execute(upsert_sql)
        ins_count, upd_count = cur.fetchone() or (0, 0)
        conn.commit()
    return (int(ins_count or 0), int(upd_count or 0))


def epoch_to_dt(epoch: int | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def insert_run_start(
    spec: DatasetSpec,
    auth_method: str,
    chunk_size: int,
    metadata: dict[str, Any],
    prior_rows_updated_at: datetime | None,
) -> uuid.UUID:
    pool = get_pool()
    run_id = uuid.uuid4()
    src_url = f"https://data.cityofnewyork.us/resource/{spec.dataset_4x4}.json"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.nyc_opendata_ingest_runs (
                id, dataset_4x4, dataset_name, status, source_url,
                auth_method, page_size,
                dataset_rows_updated_at, dataset_rows_created_at,
                dataset_view_last_modified, prior_dataset_rows_updated_at,
                started_at
            ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                str(run_id),
                spec.dataset_4x4,
                spec.display_name,
                src_url,
                auth_method,
                chunk_size,
                epoch_to_dt(metadata.get("rowsUpdatedAt")),
                epoch_to_dt(metadata.get("rowsCreatedAt")),
                epoch_to_dt(metadata.get("viewLastModified")),
                prior_rows_updated_at,
            ),
        )
        conn.commit()
    return run_id


def finish_run(
    run_id: uuid.UUID,
    status: str,
    pages_fetched: int,
    rows_inserted: int,
    rows_updated: int,
    rows_unchanged: int,
    bytes_downloaded: int,
    duration_seconds: float,
    error_message: str | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.nyc_opendata_ingest_runs SET
                status = %s,
                finished_at = now(),
                pages_fetched = %s,
                rows_inserted = %s,
                rows_updated = %s,
                rows_unchanged = %s,
                bytes_downloaded = %s,
                duration_seconds = %s,
                error_message = %s,
                notes = %s
            WHERE id = %s
            """,
            (
                status,
                pages_fetched,
                rows_inserted,
                rows_updated,
                rows_unchanged,
                bytes_downloaded,
                duration_seconds,
                error_message,
                json.dumps(notes) if notes else None,
                str(run_id),
            ),
        )
        conn.commit()


def get_prior_rows_updated_at(spec: DatasetSpec) -> datetime | None:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT dataset_rows_updated_at
            FROM ops.nyc_opendata_ingest_runs
            WHERE dataset_4x4 = %s AND status IN ('completed', 'no_change')
            ORDER BY started_at DESC LIMIT 1
            """,
            (spec.dataset_4x4,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def log_no_change(
    spec: DatasetSpec,
    auth_method: str,
    chunk_size: int,
    metadata: dict[str, Any],
    prior_rows_updated_at: datetime | None,
) -> None:
    """Insert a 'no_change' audit row when rowsUpdatedAt has not advanced."""
    rid = insert_run_start(spec, auth_method, chunk_size, metadata, prior_rows_updated_at)
    finish_run(rid, "no_change", 0, 0, 0, 0, 0, 0.0)


# --------------------------------------------------------------------------- #
# Per-dataset ingest driver.                                                  #
# --------------------------------------------------------------------------- #


def ingest_dataset(
    spec: DatasetSpec,
    dry_run: bool = False,
    max_pages: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    client = SocrataClient()
    started = time.monotonic()
    log.info(
        "[%s] starting ingest 4x4=%s table=%s.%s chunk_size=%d",
        spec.name, spec.dataset_4x4, spec.target_schema,
        spec.target_table, spec.chunk_size,
    )

    metadata = client.fetch_metadata(spec.dataset_4x4)
    rows_updated_at = epoch_to_dt(metadata.get("rowsUpdatedAt"))
    log.info(
        "[%s] dataset metadata: rowsUpdatedAt=%s viewLastModified=%s",
        spec.name, rows_updated_at, epoch_to_dt(metadata.get("viewLastModified")),
    )

    prior = None if dry_run else get_prior_rows_updated_at(spec)
    if (not force and not dry_run and prior is not None
            and rows_updated_at is not None and rows_updated_at <= prior):
        log.info("[%s] no change since prior run (rowsUpdatedAt=%s); logging no_change row",
                 spec.name, rows_updated_at)
        log_no_change(spec, client.auth_method, spec.chunk_size, metadata, prior)
        client.close()
        return {"status": "no_change"}

    if dry_run:
        run_id = None
    else:
        run_id = insert_run_start(spec, client.auth_method, spec.chunk_size, metadata, prior)

    table_columns: list[str] = [] if dry_run else get_table_columns(spec)

    pages = 0
    inserted = updated = bytes_total = 0
    try:
        for page_idx, rows, byte_count in client.fetch_pages(
            spec.dataset_4x4, chunk_size=spec.chunk_size, max_pages=max_pages,
        ):
            pages += 1
            bytes_total += byte_count
            log.info("[%s] page %d: %d rows (%.2f KB)",
                     spec.name, page_idx, len(rows), byte_count / 1024.0)
            if dry_run:
                if rows:
                    log.info("[%s] dry-run sample keys: %s",
                             spec.name, sorted(rows[0].keys())[:8])
                continue
            ins, upd = upsert_rows(spec, rows, table_columns, rows_updated_at)
            inserted += ins
            updated += upd
        duration = time.monotonic() - started
        log.info("[%s] done in %.1fs: pages=%d inserted=%d updated=%d bytes=%d",
                 spec.name, duration, pages, inserted, updated, bytes_total)
        if not dry_run and run_id is not None:
            finish_run(run_id, "completed", pages, inserted, updated, 0,
                       bytes_total, duration)
        return {
            "status": "completed",
            "pages": pages,
            "rows_inserted": inserted,
            "rows_updated": updated,
            "bytes_downloaded": bytes_total,
            "duration_seconds": duration,
        }
    except Exception as e:
        duration = time.monotonic() - started
        log.exception("[%s] failed after %.1fs", spec.name, duration)
        if not dry_run and run_id is not None:
            finish_run(run_id, "failed", pages, inserted, updated, 0,
                       bytes_total, duration, error_message=str(e)[:500])
        raise
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# CLI.                                                                        #
# --------------------------------------------------------------------------- #


# Order for `all` subcommand: smallest first to fail fast on any 4x4 / schema
# mismatches. FDNY dispatches last because it dominates wall-clock.
ALL_ORDER = [
    "fdny-vacates",      # 357
    "hpd-vacates",       # 8.5K
    "dob-fisp",          # 86K
    "nyc-ll84-energy",   # 103K
    "dob-elevator",      # 120K
    "dob-boiler",        # 850K
    "dob-stalled",       # 1.4M
    "fdny-dispatches",   # 12M (last)
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset",
                        choices=list(REGISTRY.keys()) + ["all"],
                        help="Subcommand: dataset name or 'all'")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch only; no DB writes; no audit row.")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Stop after N pages (smoke test).")
    parser.add_argument("--force", action="store_true",
                        help="Skip the rowsUpdatedAt watermark gate; always re-pull.")
    args = parser.parse_args()

    try:
        if args.dataset == "all":
            results = {}
            for name in ALL_ORDER:
                spec = REGISTRY[name]
                results[name] = ingest_dataset(
                    spec, dry_run=args.dry_run, max_pages=args.max_pages, force=args.force,
                )
            log.info("ALL summary:\n%s", json.dumps(results, default=str, indent=2))
            return 0

        spec = REGISTRY[args.dataset]
        result = ingest_dataset(
            spec, dry_run=args.dry_run, max_pages=args.max_pages, force=args.force,
        )
        log.info("%s result: %s", args.dataset, json.dumps(result, default=str, indent=2))
        return 0
    finally:
        global _pool
        if _pool is not None:
            _pool.close()
            _pool = None


if __name__ == "__main__":
    sys.exit(main())
