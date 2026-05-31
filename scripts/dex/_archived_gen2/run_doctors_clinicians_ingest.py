#!/usr/bin/env python3
"""CMS Doctors and Clinicians National Downloadable File ingest.

Source-first per CLAUDE.md (2026-04-16). Full 1:1 raw column mirror of the
CSV into entities.source_doctors_clinicians. PK npi (collapses CMS's true
(npi, adrs_id) grain to one row per NPI with last-writer-wins; raw_source_row
preserves the lossless row). Audit: ops.doctors_clinicians_ingest_runs.

Workflow:
  1. GET metastore item for dataset mj5m-pzi6, parse distribution[], pick
     mediaType='text/csv' downloadURL.
  2. HEAD the CSV → capture Last-Modified.
  3. If --skip-if-unchanged AND Last-Modified == prior successful run's
     Last-Modified → exit 0 (status='no_change').
  4. INSERT ops.doctors_clinicians_ingest_runs row (status='running').
  5. Stream-download CSV → COPY into a staging temp table.
  6. UPSERT staging → entities.source_doctors_clinicians via INSERT ... ON
     CONFLICT (npi) DO UPDATE.
  7. UPDATE runs row (status='completed', counts, finished_at).
  8. On error: UPDATE runs row (status='failed', error_text), exit nonzero.

Idempotent: re-running the same monthly snapshot produces the same end-state.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_doctors_clinicians_ingest.py
  PYTHONPATH=. doppler run -- python3 scripts/run_doctors_clinicians_ingest.py --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_doctors_clinicians_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

import httpx
import psycopg
from psycopg.types.json import Jsonb


METASTORE_URL = (
    "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6"
)
SOURCE_PROVIDER = "cms_provider_data_catalog"
RUNS_TABLE = "ops.doctors_clinicians_ingest_runs"
SOURCE_TABLE = "entities.source_doctors_clinicians"

USER_AGENT = "data-engine-x/cms-doctors-clinicians-ingest"

# CSV header → SQL column. Order matches CSV exactly so we can stream COPY.
COLUMN_MAP: list[tuple[str, str]] = [
    ("NPI", "npi"),
    ("Ind_PAC_ID", "ind_pac_id"),
    ("Ind_enrl_ID", "ind_enrl_id"),
    ("Provider Last Name", "provider_last_name"),
    ("Provider First Name", "provider_first_name"),
    ("Provider Middle Name", "provider_middle_name"),
    ("suff", "suff"),
    ("gndr", "gndr"),
    ("Cred", "cred"),
    ("Med_sch", "med_sch"),
    ("Grd_yr", "grd_yr"),
    ("pri_spec", "pri_spec"),
    ("sec_spec_1", "sec_spec_1"),
    ("sec_spec_2", "sec_spec_2"),
    ("sec_spec_3", "sec_spec_3"),
    ("sec_spec_4", "sec_spec_4"),
    ("sec_spec_all", "sec_spec_all"),
    ("Telehlth", "telehlth"),
    ("Facility Name", "facility_name"),
    ("org_pac_id", "org_pac_id"),
    ("num_org_mem", "num_org_mem"),
    ("adr_ln_1", "adr_ln_1"),
    ("adr_ln_2", "adr_ln_2"),
    ("ln_2_sprs", "ln_2_sprs"),
    ("City/Town", "city_town"),
    ("State", "state"),
    ("ZIP Code", "zip_code"),
    ("Telephone Number", "telephone_number"),
    ("ind_assgn", "ind_assgn"),
    ("grp_assgn", "grp_assgn"),
    ("adrs_id", "adrs_id"),
]

NUMERIC_COLS = {"npi", "ind_pac_id", "org_pac_id", "num_org_mem", "grd_yr"}

REQUEST_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("doctors-clinicians-ingest")


log = _logger()


def resolve_csv_url() -> str:
    """Hit the CMS metastore, return the text/csv distribution downloadURL."""
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as c:
        r = c.get(METASTORE_URL)
        r.raise_for_status()
        body = r.json()
    for d in body.get("distribution", []):
        if (d.get("mediaType") or "").lower() == "text/csv" or (d.get("format") or "").lower() == "csv":
            url = d.get("downloadURL") or d.get("data", {}).get("downloadURL")
            if url:
                log.info("resolved CSV URL: %s", url)
                return url
    raise RuntimeError(f"no text/csv distribution found in metastore response for mj5m-pzi6")


def head_last_modified(url: str) -> tuple[datetime | None, int | None]:
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        r = c.head(url)
        r.raise_for_status()
        lm_raw = r.headers.get("last-modified")
        cl_raw = r.headers.get("content-length")
    lm = parsedate_to_datetime(lm_raw) if lm_raw else None
    if lm and lm.tzinfo is None:
        lm = lm.replace(tzinfo=timezone.utc)
    cl = int(cl_raw) if cl_raw and cl_raw.isdigit() else None
    return lm, cl


def prior_run_last_modified(conn: psycopg.Connection) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT source_last_modified FROM {RUNS_TABLE} "
            f"WHERE status = 'completed' ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def coerce_value(col: str, raw: str) -> Any:
    if raw == "":
        return None
    if col in NUMERIC_COLS:
        # Pass numeric as text; Postgres parses on COPY. Strip, return None on bad.
        s = raw.strip()
        if not s:
            return None
        return s
    return raw


def stream_rows(url: str) -> Iterator[tuple[list[Any], dict[str, str]]]:
    """Stream the CSV and yield (typed_values, raw_row_dict) per data row."""
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            buffer = io.StringIO()
            decoder = io.TextIOWrapper(io.BufferedReader(r.iter_raw()), encoding="utf-8", newline="")  # not used; iter_lines below
            for line in r.iter_lines():
                buffer.write(line + "\n")
                # Flush in chunks to avoid unbounded memory.
                if buffer.tell() > 8 * 1024 * 1024:
                    yield from _drain_buffer(buffer)
                    buffer = io.StringIO()
            yield from _drain_buffer(buffer)


_HEADER_SEEN = {"value": False, "csv_to_sql": {}}


def _drain_buffer(buffer: io.StringIO) -> Iterator[tuple[list[Any], dict[str, str]]]:
    buffer.seek(0)
    reader = csv.reader(buffer)
    for row in reader:
        if not row:
            continue
        if not _HEADER_SEEN["value"]:
            # Validate header matches expected order.
            if row != [csv_h for csv_h, _ in COLUMN_MAP]:
                raise RuntimeError(f"unexpected CSV header: {row}")
            _HEADER_SEEN["value"] = True
            continue
        raw_dict = {COLUMN_MAP[i][0]: row[i] for i in range(len(COLUMN_MAP))}
        typed = [coerce_value(sql_c, row[i]) for i, (_, sql_c) in enumerate(COLUMN_MAP)]
        yield typed, raw_dict


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Exit 0 (status='no_change') if HEAD Last-Modified == prior completed run's Last-Modified.")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve URL, HEAD it, log row counts; do not write to DB.")
    args = p.parse_args()

    csv_url = resolve_csv_url()
    last_modified, content_length = head_last_modified(csv_url)
    log.info("CSV last-modified: %s; content-length: %s", last_modified, content_length)

    if args.dry_run:
        log.info("dry-run: skipping DB writes")
        return 0

    db_url = os.environ.get("DEX_DB_URL_DIRECT")
    if not db_url:
        log.error("DEX_DB_URL_DIRECT env var is required (DDL-safe direct URL).")
        return 2

    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)

    with psycopg.connect(db_url, autocommit=False) as conn:
        prior = prior_run_last_modified(conn)
        if args.skip_if_unchanged and prior is not None and last_modified is not None and prior >= last_modified:
            log.info("skip-if-unchanged: prior=%s, current=%s — no change", prior, last_modified)
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {RUNS_TABLE} (run_id, status, distribution_url, source_filename, "
                    f"source_last_modified, prior_source_last_modified, started_at, finished_at, run_metadata) "
                    f"VALUES (%s, 'no_change', %s, %s, %s, %s, %s, now(), %s)",
                    (run_id, csv_url, csv_url.rsplit("/", 1)[-1], last_modified, prior, started,
                     Jsonb({"reason": "skip-if-unchanged"})),
                )
            conn.commit()
            return 0

        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {RUNS_TABLE} (run_id, status, distribution_url, source_filename, "
                f"source_last_modified, prior_source_last_modified, started_at, run_metadata) "
                f"VALUES (%s, 'running', %s, %s, %s, %s, %s, %s)",
                (run_id, csv_url, csv_url.rsplit("/", 1)[-1], last_modified, prior, started,
                 Jsonb({"script": "run_doctors_clinicians_ingest.py"})),
            )
        conn.commit()

        try:
            sql_cols = [sql_c for _, sql_c in COLUMN_MAP]
            stage_cols_ddl = ", ".join(f"{c} text" for c in sql_cols)
            with conn.cursor() as cur:
                cur.execute(f"CREATE TEMP TABLE _stage_dac ({stage_cols_ddl}, raw_source_row jsonb) ON COMMIT DROP")

                copy_cols = sql_cols + ["raw_source_row"]
                with cur.copy(f"COPY _stage_dac ({', '.join(copy_cols)}) FROM STDIN WITH (FORMAT csv)") as copy:
                    rows_seen = 0
                    csv_w_buf = io.StringIO()
                    csv_w = csv.writer(csv_w_buf)
                    for typed, raw in stream_rows(csv_url):
                        rows_seen += 1
                        # Append raw_source_row JSON as the last field.
                        csv_w.writerow(typed + [json.dumps(raw, separators=(",", ":"))])
                        if csv_w_buf.tell() > 4 * 1024 * 1024:
                            copy.write(csv_w_buf.getvalue())
                            csv_w_buf = io.StringIO()
                            csv_w = csv.writer(csv_w_buf)
                    if csv_w_buf.tell():
                        copy.write(csv_w_buf.getvalue())

                upsert_cols = sql_cols + ["raw_source_row", "source_filename", "source_download_url",
                                          "source_provider", "source_observed_at", "source_run_metadata"]
                set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in upsert_cols if c != "npi")

                cur.execute(
                    f"""
                    INSERT INTO {SOURCE_TABLE} ({', '.join(upsert_cols)})
                    SELECT
                      NULLIF(npi, '')::bigint,
                      NULLIF(ind_pac_id, '')::bigint,
                      ind_enrl_id,
                      provider_last_name, provider_first_name, provider_middle_name,
                      suff, gndr, cred, med_sch,
                      NULLIF(grd_yr, '')::smallint,
                      pri_spec, sec_spec_1, sec_spec_2, sec_spec_3, sec_spec_4, sec_spec_all,
                      telehlth, facility_name,
                      NULLIF(org_pac_id, '')::bigint,
                      NULLIF(num_org_mem, '')::integer,
                      adr_ln_1, adr_ln_2, ln_2_sprs, city_town, state, zip_code, telephone_number,
                      ind_assgn, grp_assgn, adrs_id,
                      raw_source_row,
                      %s, %s, %s, %s, %s
                    FROM _stage_dac
                    WHERE NULLIF(npi, '') IS NOT NULL
                    ON CONFLICT (npi) DO UPDATE SET {set_clause}
                    """,
                    (csv_url.rsplit("/", 1)[-1], csv_url, SOURCE_PROVIDER, last_modified,
                     Jsonb({"run_id": run_id})),
                )
                rows_loaded = cur.rowcount

                cur.execute(
                    f"UPDATE {RUNS_TABLE} SET status = 'completed', finished_at = now(), "
                    f"duration_seconds = EXTRACT(EPOCH FROM (now() - started_at)), "
                    f"rows_in_csv = %s, rows_inserted = %s, csv_bytes_downloaded = %s "
                    f"WHERE run_id = %s",
                    (rows_seen, rows_loaded, content_length, run_id),
                )
            conn.commit()
            log.info("ingest complete: rows_in_csv=%s, rows_loaded=%s", rows_seen, rows_loaded)
            return 0
        except Exception as e:
            log.exception("ingest failed")
            conn.rollback()
            with psycopg.connect(db_url, autocommit=True) as conn2, conn2.cursor() as cur2:
                cur2.execute(
                    f"UPDATE {RUNS_TABLE} SET status = 'failed', finished_at = now(), "
                    f"duration_seconds = EXTRACT(EPOCH FROM (now() - started_at)), "
                    f"error_text = %s WHERE run_id = %s",
                    (str(e), run_id),
                )
            return 1


if __name__ == "__main__":
    sys.exit(main())
