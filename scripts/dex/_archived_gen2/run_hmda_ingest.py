#!/usr/bin/env python3
"""HMDA — bulk-CSV ingest from FFIEC Snapshot National Loan-Level Dataset.

Two datasets × seven snapshot years. Source-first per CLAUDE.md (2026-04-16):
each dataset lands in its own entities.source_hmda_* table. No identity
resolution, no canonical merge. LAR (Loan Application Register) is
intentionally OUT OF SCOPE for this first pass — per-lender volume already
lives on TS as `lar_count`; landing 20M+ borrower-level rows offers no marginal
GTM value.

  ts        TS    -> entities.source_hmda_transmittal_sheet
  panel     PANEL -> entities.source_hmda_panel

Source URL pattern (Akamai NetStorage CDN, anonymous read):
  https://files.ffiec.cfpb.gov/static-data/snapshot/{year}/{year}_public_{ts|panel}_csv.zip

URL discovery: read directly from the cfpb/hmda-frontend GitHub source at
src/data-publication/constants/snapshot-dataset.jsx — the React data-publication
SPA pulls these paths from there at runtime.

Idempotency: PK=(lei, dataset_year) on both tables, ON CONFLICT DO UPDATE.

Audit: ops.hmda_ingest_runs.
Skip-if-unchanged: HEAD Last-Modified compared to prior successful run.

2024 Panel is not published by CFPB ("Reporter Panel Unavailable" override).
The script HEADs the URL first; if it returns 404 the run is recorded as a
'failed' row with an explicit error_message rather than retrying or hard-aborting
the whole `panel all` invocation.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_hmda_ingest.py ts 2024
  PYTHONPATH=. doppler run -- python3 scripts/run_hmda_ingest.py all all
  PYTHONPATH=. doppler run -- python3 scripts/run_hmda_ingest.py all all --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_hmda_ingest.py ts 2024 --dry-run
  PYTHONPATH=. doppler run -- python3 scripts/run_hmda_ingest.py all all --recon-only
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
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


SUPPORTED_YEARS = (2018, 2019, 2020, 2021, 2022, 2023, 2024)
DEFAULT_BATCH_SIZE = 25_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("hmda-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Column lists — order matches the migration table definitions exactly.
# Schema columns excluded from these lists (managed separately):
#   lei (PK part), dataset_year (PK part, derived from filename),
#   source_file_last_modified, ingested_at.
# --------------------------------------------------------------------------- #

TS_COLS: list[str] = [
    "activity_year",
    "calendar_quarter",
    "tax_id",
    "agency_code",
    "respondent_name",
    "respondent_state",
    "respondent_city",
    "respondent_zip_code",
    "lar_count",
]

# Only lar_count is a true number; the rest stay text.
TS_NUMERIC_COLS = {"lar_count"}

PANEL_COLS: list[str] = [
    "activity_year",
    "tax_id",
    "agency_code",
    "id_2017",
    "arid_2017",       # only present in 2018-2019 source files; NULL otherwise.
    "respondent_rssd",
    "respondent_name",
    "respondent_state",
    "respondent_city",
    "assets",          # text (preserves -1 sentinel; FFIEC docs say thousands USD)
    "other_lender_code",
    "parent_rssd",
    "parent_name",
    "topholder_rssd",
    "topholder_name",
]

PANEL_NUMERIC_COLS: set[str] = set()  # everything stays text per migration


# --------------------------------------------------------------------------- #
# Per-form configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormConfig:
    key: str                # CLI subcommand
    dataset_form: str       # Audit-table value
    schema: str             # 'entities'
    table: str
    cols: list[str]         # Columns excluding PK + audit timestamps
    numeric_cols: set[str]
    pk_cols: tuple[str, ...]    # ('lei', 'dataset_year')
    csv_basename_prefix: str    # E.g. "{year}_public_ts_csv" → CSV is "{year}_public_ts_csv.csv"

    def url(self, year: int) -> str:
        return (
            "https://files.ffiec.cfpb.gov/static-data/snapshot/"
            f"{year}/{year}_public_{self.csv_basename_prefix}_csv.zip"
        )

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"


TS_FORM = FormConfig(
    key="ts",
    dataset_form="TS",
    schema="entities",
    table="source_hmda_transmittal_sheet",
    cols=TS_COLS,
    numeric_cols=TS_NUMERIC_COLS,
    pk_cols=("lei", "dataset_year"),
    csv_basename_prefix="ts",
)

PANEL_FORM = FormConfig(
    key="panel",
    dataset_form="PANEL",
    schema="entities",
    table="source_hmda_panel",
    cols=PANEL_COLS,
    numeric_cols=PANEL_NUMERIC_COLS,
    pk_cols=("lei", "dataset_year"),
    csv_basename_prefix="panel",
)

FORMS: dict[str, FormConfig] = {f.key: f for f in (TS_FORM, PANEL_FORM)}

# Years known not to be published.
KNOWN_MISSING: set[tuple[str, int]] = {("PANEL", 2024)}


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


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None, int]:
    """Returns (content_length, last_modified, http_status). last_exc raised
    only if all retries exhausted with no usable status."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code == 404:
                return None, None, 404
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
            return cl, lm, r.status_code
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    """Download ZIP to dest, returning bytes written."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=600.0) as r:
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


def open_csv_in_zip(zip_path: Path) -> tuple[zipfile.ZipFile, io.TextIOWrapper, str]:
    """Open the bundled CSV (single-CSV archive) for streaming read."""
    z = zipfile.ZipFile(zip_path)
    target_name = None
    for name in z.namelist():
        if name.lower().endswith(".csv"):
            target_name = name
            break
    if target_name is None:
        z.close()
        raise RuntimeError(
            f"No CSV found in {zip_path.name}; contents: {z.namelist()}"
        )
    f = io.TextIOWrapper(z.open(target_name, "r"), encoding="utf-8", errors="replace", newline="")
    return z, f, target_name


def stage_create_sql(cfg: FormConfig) -> str:
    cols = ",\n  ".join(
        f"{c} {'numeric' if c in cfg.numeric_cols else 'text'}"
        for c in cfg.cols
    )
    return f"""
CREATE TEMP TABLE IF NOT EXISTS {cfg.stage_table} (
  lei text,
  dataset_year smallint,
  {cols},
  source_file_last_modified timestamptz
);
"""


def truncate_stage_sql(cfg: FormConfig) -> str:
    return f"TRUNCATE {cfg.stage_table};"


def copy_sql(cfg: FormConfig) -> str:
    cols = ["lei", "dataset_year"] + list(cfg.cols) + ["source_file_last_modified"]
    return f"COPY {cfg.stage_table} ({', '.join(cols)}) FROM STDIN"


def upsert_from_stage_sql(cfg: FormConfig) -> str:
    natural_cols = list(cfg.cols)
    target_cols = (
        list(cfg.pk_cols)
        + natural_cols
        + ["source_file_last_modified", "ingested_at"]
    )
    select_cols = (
        list(cfg.pk_cols)
        + natural_cols
        + ["source_file_last_modified", "now()"]
    )
    pk = ", ".join(cfg.pk_cols)
    update_cols = natural_cols + ["source_file_last_modified"]
    update_assigns = ",\n      ".join(
        f"{c} = EXCLUDED.{c}" for c in update_cols
    ) + ",\n      ingested_at = now()"
    where_clause = " OR ".join(
        f"{cfg.fully_qualified}.{c} IS DISTINCT FROM EXCLUDED.{c}"
        for c in update_cols
    )
    return f"""
WITH upserted AS (
  INSERT INTO {cfg.fully_qualified} ({', '.join(target_cols)})
  SELECT {', '.join(select_cols)}
    FROM {cfg.stage_table}
   WHERE lei IS NOT NULL AND lei <> ''
   ON CONFLICT ({pk}) DO UPDATE SET
      {update_assigns}
   WHERE {where_clause}
   RETURNING (xmax = 0) AS inserted
)
SELECT
  count(*) FILTER (WHERE inserted)     AS rows_inserted,
  count(*) FILTER (WHERE NOT inserted) AS rows_updated
FROM upserted;
"""


def copy_chunk_to_stage(
    conn: psycopg.Connection,
    cfg: FormConfig,
    rows: list[tuple[Any, ...]],
) -> tuple[int, int]:
    """COPY into stage, then upsert into target. Returns (inserted, updated)."""
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
    cfg: FormConfig,
    csv_fh: io.TextIOWrapper,
    *,
    dataset_year: int,
    source_file_last_modified: datetime | None,
    batch_size: int,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, int]:
    """Streams CSV rows into the staging table and upserts. Returns
    (inserted, updated, rows_seen)."""
    reader = csv.reader(csv_fh)
    try:
        header = next(reader)
    except StopIteration:
        return 0, 0, 0
    header_lower = [h.strip().lower() for h in header]
    idx_by_name = {name: i for i, name in enumerate(header_lower)}

    expected_lower = {c.lower() for c in cfg.cols} | {"lei"}
    missing = sorted(expected_lower - set(header_lower))
    extra = sorted(set(header_lower) - expected_lower)
    if missing:
        log.warning("%s CSV missing %d column(s) expected by migration: %s",
                    log_prefix, len(missing), missing[:10])
    if extra:
        log.warning("%s CSV has %d unexpected column(s) (will be dropped): %s",
                    log_prefix, len(extra), extra[:10])

    lei_idx = idx_by_name.get("lei")
    if lei_idx is None:
        raise RuntimeError(f"{log_prefix} CSV header has no 'lei' column: {header}")

    col_indexes = [idx_by_name.get(c.lower()) for c in cfg.cols]

    rows_seen = total_inserted = total_updated = 0
    chunk: list[tuple[Any, ...]] = []
    page_started = time.monotonic()
    for raw in reader:
        rows_seen += 1
        if max_rows is not None and rows_seen > max_rows:
            log.info("%s --max-rows %d reached, stopping read", log_prefix, max_rows)
            break

        if lei_idx >= len(raw):
            continue
        lei = raw[lei_idx].strip() if raw[lei_idx] is not None else ""
        if not lei:
            continue

        out: list[Any] = [lei, dataset_year]
        for col, idx in zip(cfg.cols, col_indexes):
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
                "%s chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
                log_prefix, rows_seen, ins, upd,
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
            "%s final chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
            log_prefix, rows_seen, ins, upd,
            total_inserted, total_updated,
            time.monotonic() - page_started,
        )
    return total_inserted, total_updated, rows_seen


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    cfg: FormConfig,
    *,
    year: int,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.hmda_ingest_runs (
        dataset_form, dataset_year, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cfg.dataset_form, year, url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, cfg: FormConfig, year: int
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.hmda_ingest_runs
             WHERE dataset_form = %s AND dataset_year = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (cfg.dataset_form, year),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    cfg: FormConfig,
    *,
    year: int,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.hmda_ingest_runs (
                dataset_form, dataset_year, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                cfg.dataset_form, year, url, source_last_modified,
                prior_source_last_modified, started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def write_failed_404_run(
    conn: psycopg.Connection,
    cfg: FormConfig,
    *,
    year: int,
    url: str,
) -> None:
    started = datetime.now(timezone.utc)
    note = {
        "reason": "source url returned HTTP 404",
        "expected": (
            "Known unpublished — 2024 Panel is missing per CFPB hmda-frontend "
            "'Reporter Panel Unavailable' override"
            if (cfg.dataset_form, year) in KNOWN_MISSING else
            "Unexpected 404 — source URL pattern may have changed"
        ),
    }
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.hmda_ingest_runs (
                dataset_form, dataset_year, status, source_url,
                started_at, finished_at, duration_seconds,
                error_message, notes
            ) VALUES (%s, %s, 'failed', %s, %s, %s, 0, %s, %s);
            """,
            (
                cfg.dataset_form, year, url,
                started, started,
                "HTTP 404 from source URL",
                Jsonb(note),
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
            UPDATE ops.hmda_ingest_runs
               SET status = %s, zip_bytes_downloaded = %s,
                   csv_bytes_extracted = %s, rows_in_csv = %s,
                   rows_inserted = %s, rows_updated = %s, rows_unchanged = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, rows_in_csv,
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
    form_key: str
    table_fqn: str
    total_rows: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


def gather_recon_ts(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="ts", table_fqn=TS_FORM.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TS_FORM.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(
            f"SELECT dataset_year, count(*), count(DISTINCT lei) "
            f"  FROM {TS_FORM.fully_qualified} "
            f" GROUP BY dataset_year ORDER BY dataset_year;"
        )
        s.notes["rows_by_year"] = [
            {"year": int(r[0]), "rows": int(r[1]), "distinct_lei": int(r[2])}
            for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE respondent_name IS NOT NULL),
              count(*) FILTER (WHERE respondent_state IS NOT NULL),
              count(*) FILTER (WHERE respondent_zip_code IS NOT NULL),
              count(*) FILTER (WHERE tax_id IS NOT NULL),
              count(*) FILTER (WHERE lar_count IS NOT NULL),
              count(DISTINCT lei),
              count(DISTINCT tax_id)
              FROM {TS_FORM.fully_qualified};
        """)
        n_name, n_state, n_zip, n_ein, n_lc, d_lei, d_ein = cur.fetchone()
        s.notes["respondent_name_populated"] = int(n_name)
        s.notes["respondent_state_populated"] = int(n_state)
        s.notes["respondent_zip_populated"] = int(n_zip)
        s.notes["tax_id_populated"] = int(n_ein)
        s.notes["lar_count_populated"] = int(n_lc)
        s.notes["distinct_lei_overall"] = int(d_lei)
        s.notes["distinct_tax_id_overall"] = int(d_ein)
        cur.execute(f"""
            SELECT respondent_state, count(*) c
              FROM {TS_FORM.fully_qualified}
             WHERE respondent_state IS NOT NULL
             GROUP BY respondent_state ORDER BY c DESC LIMIT 15;
        """)
        s.notes["top_states_by_filing_count"] = [
            {"state": r[0], "rows": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT agency_code, count(*) c
              FROM {TS_FORM.fully_qualified}
             WHERE agency_code IS NOT NULL
             GROUP BY agency_code ORDER BY c DESC;
        """)
        s.notes["agency_code_distribution"] = [
            {"agency_code": r[0], "rows": int(r[1])} for r in cur.fetchall()
        ]
        # Top 15 lenders by lar_count for the most recent year landed.
        cur.execute(f"""
            SELECT max(dataset_year) FROM {TS_FORM.fully_qualified};
        """)
        max_year = cur.fetchone()[0]
        if max_year is not None:
            cur.execute(f"""
                SELECT respondent_name, lei, lar_count
                  FROM {TS_FORM.fully_qualified}
                 WHERE dataset_year = %s
                   AND lar_count IS NOT NULL
                 ORDER BY lar_count DESC NULLS LAST
                 LIMIT 15;
            """, (max_year,))
            s.notes[f"top_15_lenders_by_lar_count_{int(max_year)}"] = [
                {"name": r[0], "lei": r[1], "lar_count": int(r[2])} for r in cur.fetchall()
            ]
        # lar_count distribution: small / medium / large
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE lar_count < 100),
              count(*) FILTER (WHERE lar_count BETWEEN 100 AND 9999),
              count(*) FILTER (WHERE lar_count >= 10000),
              percentile_cont(0.5)  WITHIN GROUP (ORDER BY lar_count) FILTER (WHERE lar_count IS NOT NULL),
              percentile_cont(0.95) WITHIN GROUP (ORDER BY lar_count) FILTER (WHERE lar_count IS NOT NULL),
              max(lar_count)
              FROM {TS_FORM.fully_qualified};
        """)
        sm, md, lg, p50, p95, mx = cur.fetchone()
        s.notes["lar_count_size_buckets"] = {
            "small_lt_100": int(sm or 0),
            "medium_100_to_9999": int(md or 0),
            "large_gte_10000": int(lg or 0),
            "median": int(p50) if p50 is not None else None,
            "p95": int(p95) if p95 is not None else None,
            "max": int(mx) if mx is not None else None,
        }
    return s


def gather_recon_panel(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="panel", table_fqn=PANEL_FORM.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {PANEL_FORM.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(
            f"SELECT dataset_year, count(*), count(DISTINCT lei) "
            f"  FROM {PANEL_FORM.fully_qualified} "
            f" GROUP BY dataset_year ORDER BY dataset_year;"
        )
        s.notes["rows_by_year"] = [
            {"year": int(r[0]), "rows": int(r[1]), "distinct_lei": int(r[2])}
            for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE respondent_name IS NOT NULL),
              count(*) FILTER (WHERE respondent_state IS NOT NULL),
              count(*) FILTER (WHERE assets IS NOT NULL AND assets <> '-1'),
              count(*) FILTER (WHERE other_lender_code IS NOT NULL),
              count(*) FILTER (WHERE parent_rssd IS NOT NULL AND parent_rssd <> '-1'),
              count(*) FILTER (WHERE topholder_rssd IS NOT NULL AND topholder_rssd <> '-1'),
              count(DISTINCT lei),
              count(DISTINCT topholder_rssd) FILTER (WHERE topholder_rssd IS NOT NULL AND topholder_rssd <> '-1')
              FROM {PANEL_FORM.fully_qualified};
        """)
        n_name, n_state, n_assets, n_olc, n_par, n_top, d_lei, d_top = cur.fetchone()
        s.notes["respondent_name_populated"] = int(n_name)
        s.notes["respondent_state_populated"] = int(n_state)
        s.notes["assets_populated_nontrivial"] = int(n_assets)
        s.notes["other_lender_code_populated"] = int(n_olc)
        s.notes["parent_rssd_populated"] = int(n_par)
        s.notes["topholder_rssd_populated"] = int(n_top)
        s.notes["distinct_lei_overall"] = int(d_lei)
        s.notes["distinct_topholder_rssd"] = int(d_top)
        cur.execute(f"""
            SELECT other_lender_code, count(*) c
              FROM {PANEL_FORM.fully_qualified}
             WHERE other_lender_code IS NOT NULL
             GROUP BY other_lender_code ORDER BY c DESC;
        """)
        s.notes["other_lender_code_distribution"] = [
            {"code": r[0], "rows": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT topholder_name, count(DISTINCT lei) c
              FROM {PANEL_FORM.fully_qualified}
             WHERE topholder_name IS NOT NULL AND topholder_name <> ''
             GROUP BY topholder_name ORDER BY c DESC LIMIT 10;
        """)
        s.notes["top_10_holding_companies_by_subsidiaries"] = [
            {"topholder": r[0], "distinct_lei": int(r[1])} for r in cur.fetchall()
        ]
    return s


def gather_recon_cross(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="ts_panel_join", table_fqn="(join)")
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT count(*) FROM {TS_FORM.fully_qualified};
        """)
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT
              ts.dataset_year,
              count(*)                                    AS ts_rows,
              count(p.lei)                                AS ts_with_panel_match,
              round(100.0 * count(p.lei)::numeric / count(*), 1) AS pct
              FROM {TS_FORM.fully_qualified} ts
              LEFT JOIN {PANEL_FORM.fully_qualified} p
                ON p.lei = ts.lei AND p.dataset_year = ts.dataset_year
             GROUP BY ts.dataset_year
             ORDER BY ts.dataset_year;
        """)
        s.notes["ts_to_panel_match_rate_by_year"] = [
            {
                "year": int(r[0]),
                "ts_rows": int(r[1]),
                "ts_with_panel_match": int(r[2]),
                "match_pct": float(r[3]),
            }
            for r in cur.fetchall()
        ]
    return s


def print_recon(s: ReconStats) -> None:
    print(f"=== RECON: {s.form_key}  ({s.table_fqn}) ===")
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


# --------------------------------------------------------------------------- #
# Per-(form, year) main
# --------------------------------------------------------------------------- #


def ensure_stage_table(conn: psycopg.Connection, cfg: FormConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(stage_create_sql(cfg))
    conn.commit()


def ingest_one(
    cfg: FormConfig,
    *,
    year: int,
    batch_size: int,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
) -> int:
    url = cfg.url(year)
    log_prefix = f"[{cfg.key} {year}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/hmda-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(client, url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1

        if status_code == 404:
            note_msg = (
                "expected: 2024 Panel is not published"
                if (cfg.dataset_form, year) in KNOWN_MISSING else
                "unexpected: source URL returned 404"
            )
            log.warning("%s HEAD 404 (%s) — recording 'failed' run row, continuing",
                        log_prefix, note_msg)
            if not dry_run:
                with psycopg.connect(_database_url()) as conn:
                    write_failed_404_run(conn, cfg, year=year, url=url)
            return 0 if (cfg.dataset_form, year) in KNOWN_MISSING else 1

        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)

        if dry_run:
            log.info("%s DRY RUN — fetching ZIP and inspecting CSV header only", log_prefix)
            zip_path = workdir / f"hmda_{cfg.key}_{year}.zip"
            zip_bytes = download_zip(client, url, zip_path)
            log.info("%s downloaded %d bytes", log_prefix, zip_bytes)
            try:
                z, fh, name = open_csv_in_zip(zip_path)
                with z, fh:
                    header_line = fh.readline()
                    sample = fh.readline()
                    cols = header_line.rstrip("\n").split(",")
                    log.info("%s CSV name=%s cols=%d header=%s sample=%s",
                             log_prefix, name, len(cols), cols[:8],
                             sample[:200].rstrip())
            finally:
                zip_path.unlink(missing_ok=True)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, cfg, year)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source_last_modified unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, cfg, year=year, url=url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, cfg, year=year, url=url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)
            ensure_stage_table(conn, cfg)

            zip_path = workdir / f"hmda_{cfg.key}_{year}.zip"
            try:
                zip_bytes = download_zip(client, url, zip_path)
                log.info("%s downloaded %d bytes -> %s", log_prefix, zip_bytes, zip_path)

                z, fh, csv_name = open_csv_in_zip(zip_path)
                with z, fh:
                    csv_bytes = z.getinfo(csv_name).file_size
                    log.info("%s extracting %s (%d bytes uncompressed)",
                             log_prefix, csv_name, csv_bytes)
                    ins, upd, rows_seen = stream_csv_to_db(
                        conn, cfg, fh,
                        dataset_year=year,
                        source_file_last_modified=source_last_modified,
                        batch_size=batch_size,
                        log_prefix=log_prefix,
                        max_rows=max_rows,
                    )

                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes, csv_bytes=csv_bytes,
                    rows_in_csv=rows_seen,
                    rows_inserted=ins, rows_updated=upd,
                    rows_unchanged=max(0, rows_seen - ins - upd),
                    started_at=started_wall, error_message=None, notes=None,
                )
                log.info(
                    "%s DONE rows_in_csv=%d ins=%d upd=%d unch=%d wall=%.1fs",
                    log_prefix, rows_seen, ins, upd,
                    max(0, rows_seen - ins - upd),
                    time.monotonic() - started_wall,
                )
                return 0
            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes=0, csv_bytes=0, rows_in_csv=0,
                    rows_inserted=0, rows_updated=0, rows_unchanged=0,
                    started_at=started_wall, error_message=str(exc), notes=None,
                )
                return 1
            finally:
                zip_path.unlink(missing_ok=True)


def run_recon_only() -> None:
    with psycopg.connect(_database_url()) as conn:
        for fn in (gather_recon_ts, gather_recon_panel, gather_recon_cross):
            try:
                s = fn(conn)
                print_recon(s)
            except psycopg.errors.UndefinedTable:
                log.error("Table missing — apply the migration first.")
                return


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("form", choices=list(FORMS.keys()) + ["all"],
                   help="Form key (ts, panel) or 'all'.")
    p.add_argument("year", help="Year (2018..2024) or 'all'.")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help="Rows per COPY chunk (default: 25000).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if source Last-Modified has not advanced "
                        "since the prior successful run.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD + download + read CSV header only; no DB writes.")
    p.add_argument("--recon-only", action="store_true",
                   help="Run recon SELECTs against existing table contents and exit.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows read per CSV (smoke testing only).")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP downloads (default: /tmp/hmda_ingest).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.recon_only:
        run_recon_only()
        return 0

    forms = list(FORMS.values()) if args.form == "all" else [FORMS[args.form]]
    years: list[int]
    if args.year == "all":
        years = list(SUPPORTED_YEARS)
    else:
        try:
            yr = int(args.year)
        except ValueError:
            log.error("year must be an int or 'all'")
            return 2
        if yr not in SUPPORTED_YEARS:
            log.error("year %s not in supported set %s", yr, SUPPORTED_YEARS)
            return 2
        years = [yr]

    workdir = Path(args.workdir or "/tmp/hmda_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    rc = 0
    for cfg in forms:
        for year in years:
            ds_rc = ingest_one(
                cfg,
                year=year,
                batch_size=args.batch_size,
                skip_if_unchanged=args.skip_if_unchanged,
                dry_run=args.dry_run,
                workdir=workdir,
                max_rows=args.max_rows,
            )
            rc = rc or ds_rc

    if not args.dry_run:
        run_recon_only()
    return rc


if __name__ == "__main__":
    sys.exit(main())
