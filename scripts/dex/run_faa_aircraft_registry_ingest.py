#!/usr/bin/env python3
"""FAA Aircraft Registry — CSV ingest.

Source:
    https://registry.faa.gov/database/ReleasableAircraft.zip — public Akamai CDN.
    ZIP contains comma-delimited .txt files. v1 ingests 3 of them:
    MASTER.txt, ACFTREF.txt, ENGINE.txt. (DEREG, RESERVED, DEALER, DOCINDEX
    deferred to a follow-up scope.)

Freshness probe:
    Single fixed URL — FAA replaces the file in place each Wednesday night.
    Records the file's Last-Modified header as source_observed_at.
    Note: registry.faa.gov is behind Akamai and 403s from local IPs;
    Modal/AWS egress IPs have no such block.

Idempotency:
    COPY rows into a temp staging table, then INSERT ... ON CONFLICT
    (pk_cols) DO UPDATE ... WHERE row IS DISTINCT FROM EXCLUDED.
    MASTER PK: (n_number). ACFTREF PK: (code). ENGINE PK: (code).

Audit:
    One row per invocation in ops.faa_aircraft_registry_ingest_runs.
    rows_seen / rows_upserted are jsonb objects keyed by csv basename
    (master, acftref, engine).

Usage:
    DEX_DB_URL_POOLED=<url> python3 scripts/run_faa_aircraft_registry_ingest.py
    DEX_DB_URL_POOLED=<url> python3 scripts/run_faa_aircraft_registry_ingest.py --dry-run
    DEX_DB_URL_POOLED=<url> python3 scripts/run_faa_aircraft_registry_ingest.py --max-rows 1000
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ZIP_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
ZIP_FILENAME = "ReleasableAircraft.zip"
PROVIDER = "faa_aircraft_registry"
USER_AGENT = "data-engine-x-api/faa-aircraft-registry-ingest"
BATCH_SIZE = 10_000

# Column shape for MASTER.txt. Standard FAA releasable-aircraft column set
# per ardata.pdf (community references stable since 2014). The actual CSV
# headers may have leading spaces and parens — header normalization handles
# that. Unknown headers are silently skipped (raw_source_row captures them).
MASTER_COLS = (
    "n_number",
    "serial_number",
    "mfr_mdl_code",
    "eng_mfr_mdl",
    "year_mfr",
    "type_registrant",
    "registrant_name",
    "street",
    "street2",
    "city",
    "state",
    "zip_code",
    "region",
    "county",
    "country",
    "last_action_date",
    "cert_issue_date",
    "certification",
    "type_aircraft",
    "type_engine",
    "status_code",
    "mode_s_code",
    "fract_owner",
    "air_worth_date",
    "other_names_1",
    "other_names_2",
    "other_names_3",
    "other_names_4",
    "other_names_5",
    "expiration_date",
    "unique_id",
    "kit_mfr",
    "kit_model",
    "mode_s_code_hex",
)

ACFTREF_COLS = (
    "code",
    "mfr",
    "model",
    "type_acft",
    "type_eng",
    "ac_cat",
    "build_cert_ind",
    "no_eng",
    "no_seats",
    "ac_weight",
    "speed",
    "tc_data_sheet",
    "tc_data_holder",
)

ENGINE_COLS = (
    "code",
    "mfr",
    "model",
    "type",
    "horsepower",
    "thrust",
)

DEALER_COLS = (
    "certificate_number",
    "ownership",
    "certificate_date",
    "expiration_date",
    "expiration_flag",
    "certificate_issue_count",
    "dealer_name",
    "street",
    "street2",
    "city",
    "state_abbrev",
    "zip_code",
    "other_names_count",
    *(f"other_names_{i}" for i in range(1, 26)),
)

RESERVED_COLS = (
    "n_number",
    "registrant",
    "street",
    "street2",
    "city",
    "state",
    "zip_code",
    "rsv_date",
    "tr",
    "exp_date",
    "n_num_chg",
    "purge_date",
)

DEREG_COLS = (
    "n_number",
    "serial_number",
    "mfr_mdl_code",
    "status_code",
    "registrant_name",
    "street_mail",
    "street2_mail",
    "city_mail",
    "state_abbrev_mail",
    "zip_code_mail",
    "eng_mfr_mdl",
    "year_mfr",
    "certification",
    "region",
    "county_mail",
    "country_mail",
    "air_worth_date",
    "cancel_date",
    "mode_s_code",
    "indicator_group",
    "exp_country",
    "last_act_date",
    "cert_issue_date",
    "street_physical",
    "street2_physical",
    "city_physical",
    "state_abbrev_physical",
    "zip_code_physical",
    "county_physical",
    "country_physical",
    "other_names_1",
    "other_names_2",
    "other_names_3",
    "other_names_4",
    "other_names_5",
    "kit_mfr",
    "kit_model",
    "mode_s_code_hex",
)

DOCINDEX_COLS = (
    "doc_id",
    "type_collateral",
    "collateral",
    "party",
    "drdate",
    "processing_date",
    "corr_date",
    "corr_id",
    "serial_id",
    "doc_type",
    # Synthetic PK — md5(canonical_json(raw_source_row)) computed in the adapter.
    # DOCINDEX has multiple rows per doc_id (one per party/collateral/etc.); a
    # natural composite would be unwieldy, so we hash the full raw row instead.
    "row_md5",
)

# CSV filename → (target table, pk_cols tuple, typed_cols tuple)
CSV_TABLES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "MASTER.txt":   (
        "entities.source_faa_aircraft_master",
        ("n_number",),
        MASTER_COLS,
    ),
    "ACFTREF.txt":  (
        "entities.source_faa_aircraft_acftref",
        ("code",),
        ACFTREF_COLS,
    ),
    "ENGINE.txt":   (
        "entities.source_faa_aircraft_engine",
        ("code",),
        ENGINE_COLS,
    ),
    "DEALER.txt":   (
        "entities.source_faa_aircraft_dealer",
        ("certificate_number",),
        DEALER_COLS,
    ),
    "RESERVED.txt": (
        "entities.source_faa_aircraft_reserved",
        ("n_number",),
        RESERVED_COLS,
    ),
    "DEREG.txt":    (
        "entities.source_faa_aircraft_dereg",
        ("n_number", "serial_number", "cancel_date"),
        DEREG_COLS,
    ),
    "DOCINDEX.txt": (
        "entities.source_faa_aircraft_docindex",
        ("row_md5",),
        DOCINDEX_COLS,
    ),
}

# FAA CSV header → snake_case column name mapping. The FAA's documented
# headers (per ardata.pdf and stable community references) include a few
# quirks: leading/trailing spaces on most columns after the first, parens
# in OTHER NAMES column names, mixed punctuation in ACFTREF/ENGINE.
# Unknown headers are silently skipped (raw_source_row captures them verbatim).
HEADER_TO_COLUMN: dict[str, str] = {
    # MASTER.txt headers
    "N-NUMBER":            "n_number",
    "SERIAL NUMBER":       "serial_number",
    "MFR MDL CODE":        "mfr_mdl_code",
    "ENG MFR MDL":         "eng_mfr_mdl",
    "YEAR MFR":            "year_mfr",
    "TYPE REGISTRANT":     "type_registrant",
    "NAME":                "registrant_name",
    "STREET":              "street",
    "STREET2":             "street2",
    "CITY":                "city",
    "STATE":               "state",
    "ZIP CODE":            "zip_code",
    "REGION":              "region",
    "COUNTY":              "county",
    "COUNTRY":             "country",
    "LAST ACTION DATE":    "last_action_date",
    "CERT ISSUE DATE":     "cert_issue_date",
    "CERTIFICATION":       "certification",
    "TYPE AIRCRAFT":       "type_aircraft",
    "TYPE ENGINE":         "type_engine",
    "STATUS CODE":         "status_code",
    "MODE S CODE":         "mode_s_code",
    "FRACT OWNER":         "fract_owner",
    "AIR WORTH DATE":      "air_worth_date",
    "OTHER NAMES(1)":      "other_names_1",
    "OTHER NAMES(2)":      "other_names_2",
    "OTHER NAMES(3)":      "other_names_3",
    "OTHER NAMES(4)":      "other_names_4",
    "OTHER NAMES(5)":      "other_names_5",
    "EXPIRATION DATE":     "expiration_date",
    "UNIQUE ID":           "unique_id",
    "KIT MFR":             "kit_mfr",
    "KIT MODEL":           "kit_model",
    "MODE S CODE HEX":     "mode_s_code_hex",

    # ACFTREF.txt headers
    "CODE":                "code",
    "MFR":                 "mfr",
    "MODEL":               "model",
    "TYPE-ACFT":           "type_acft",
    "TYPE-ENG":            "type_eng",
    "AC-CAT":              "ac_cat",
    "BUILD-CERT-IND":      "build_cert_ind",
    "NO-ENG":              "no_eng",
    "NO-SEATS":            "no_seats",
    "AC-WEIGHT":           "ac_weight",
    "SPEED":               "speed",
    "TC-DATA-SHEET":       "tc_data_sheet",
    "TC-DATA-HOLDER":      "tc_data_holder",

    # ENGINE.txt headers (CODE/MFR/MODEL shared with ACFTREF)
    "TYPE":                "type",
    "HORSEPOWER":          "horsepower",
    "THRUST":              "thrust",

    # DEALER.txt headers — most are unique to DEALER (hyphen-separated style).
    # NAME collides with MASTER.NAME → handled via PER_CSV_HEADER_OVERRIDES below.
    "CERTIFICATE-NUMBER":      "certificate_number",
    "OWNERSHIP":               "ownership",
    "CERTIFICATE-DATE":        "certificate_date",
    "EXPIRATION-DATE":         "expiration_date",
    "EXPIRATION-FLAG":         "expiration_flag",
    "CERTIFICATE-ISSUE-COUNT": "certificate_issue_count",
    "STATE-ABBREV":            "state_abbrev",
    "ZIP-CODE":                "zip_code",
    "OTHER-NAMES-COUNT":       "other_names_count",
    **{f"OTHER-NAMES-{i}": f"other_names_{i}" for i in range(1, 26)},

    # RESERVED.txt headers (most overlap with MASTER; unique ones below).
    "REGISTRANT":              "registrant",
    "RSV DATE":                "rsv_date",
    "TR":                      "tr",
    "EXP DATE":                "exp_date",
    "N-NUM-CHG":               "n_num_chg",
    "PURGE DATE":              "purge_date",

    # DEREG.txt headers — hyphen-separated variants of MASTER's space-separated.
    "SERIAL-NUMBER":           "serial_number",
    "MFR-MDL-CODE":            "mfr_mdl_code",
    "STATUS-CODE":              "status_code",
    "STREET-MAIL":             "street_mail",
    "STREET2-MAIL":            "street2_mail",
    "CITY-MAIL":               "city_mail",
    "STATE-ABBREV-MAIL":       "state_abbrev_mail",
    "ZIP-CODE-MAIL":           "zip_code_mail",
    "ENG-MFR-MDL":             "eng_mfr_mdl",
    "YEAR-MFR":                "year_mfr",
    "COUNTY-MAIL":             "county_mail",
    "COUNTRY-MAIL":            "country_mail",
    "AIR-WORTH-DATE":          "air_worth_date",
    "CANCEL-DATE":             "cancel_date",
    "MODE-S-CODE":             "mode_s_code",
    "INDICATOR-GROUP":         "indicator_group",
    "EXP-COUNTRY":             "exp_country",
    "LAST-ACT-DATE":           "last_act_date",
    "CERT-ISSUE-DATE":         "cert_issue_date",
    "STREET-PHYSICAL":         "street_physical",
    "STREET2-PHYSICAL":        "street2_physical",
    "CITY-PHYSICAL":           "city_physical",
    "STATE-ABBREV-PHYSICAL":   "state_abbrev_physical",
    "ZIP-CODE-PHYSICAL":       "zip_code_physical",
    "COUNTY-PHYSICAL":         "county_physical",
    "COUNTRY-PHYSICAL":        "country_physical",
    "OTHER-NAMES(1)":          "other_names_1",
    "OTHER-NAMES(2)":          "other_names_2",
    "OTHER-NAMES(3)":          "other_names_3",
    "OTHER-NAMES(4)":          "other_names_4",
    "OTHER-NAMES(5)":          "other_names_5",

    # DOCINDEX.txt headers.
    "TYPE-COLLATERAL":         "type_collateral",
    "COLLATERAL":              "collateral",
    "PARTY":                   "party",
    "DOC-ID":                  "doc_id",
    "DRDATE":                  "drdate",
    "PROCESSING-DATE":         "processing_date",
    "CORR-DATE":               "corr_date",
    "CORR-ID":                 "corr_id",
    "SERIAL-ID":               "serial_id",
    "DOC-TYPE":                "doc_type",
}

# Per-CSV header overrides — checked before HEADER_TO_COLUMN. Resolves header-name
# collisions between CSVs that mean different things (e.g. DEALER.NAME is the
# dealer's certificate-holder name, not the aircraft registrant in MASTER).
PER_CSV_HEADER_OVERRIDES: dict[str, dict[str, str]] = {
    "DEALER.txt": {
        "NAME": "dealer_name",
    },
}

# Provenance columns appended to every row (not from the CSV).
PROVENANCE_COLS = (
    "raw_source_row",
    "source_provider",
    "source_filename",
    "source_download_url",
    "source_observed_at",
    "source_run_metadata",
    "source_task_id",
    "source_schedule_id",
    "ingested_at",
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("faa-aircraft-registry-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# DB URL
# --------------------------------------------------------------------------- #

def _database_url() -> str:
    """Prefer DEX_DB_URL_POOLED; fall back to DEX_DB_URL_DIRECT."""
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        raise RuntimeError(
            "neither DEX_DB_URL_POOLED nor DEX_DB_URL_DIRECT is set — "
            "are you running under `doppler run` or inside a Modal function?"
        )
    return url


# --------------------------------------------------------------------------- #
# ZIP URL probe
# --------------------------------------------------------------------------- #

def _probe_zip(url: str) -> datetime | None:
    """GET with Range: bytes=0-0 to fetch Last-Modified; return parsed dt or None.

    Akamai on registry.faa.gov blocks HEAD requests; range GETs work.
    """
    log.info("probing %s", url)
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        resp = client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(
                f"probe GET {url} returned HTTP {resp.status_code}"
            )
        lm_header = resp.headers.get("last-modified")
        if not lm_header:
            log.warning("server omitted Last-Modified header")
            return None
        try:
            return parsedate_to_datetime(lm_header)
        except Exception:
            log.warning("could not parse Last-Modified: %r", lm_header)
            return None


# --------------------------------------------------------------------------- #
# ZIP download
# --------------------------------------------------------------------------- #

def _download_zip(url: str) -> Path:
    """Stream the ZIP to a temp file; return the path."""
    log.info("downloading %s", url)
    with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        content_length = resp.headers.get("content-length")
        if content_length:
            log.info("content-length: %s bytes", content_length)
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        bytes_written = 0
        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
            tmp.write(chunk)
            bytes_written += len(chunk)
        tmp.flush()
        tmp.close()
    log.info("downloaded %d bytes to %s", bytes_written, tmp.name)
    return Path(tmp.name)


# --------------------------------------------------------------------------- #
# ops.faa_aircraft_registry_ingest_runs helpers
# --------------------------------------------------------------------------- #

def insert_run(
    conn: psycopg.Connection,
    filename: str,
    url: str,
    observed_at: datetime | None,
) -> str:
    """INSERT a 'running' row; return run_id (UUID str)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.faa_aircraft_registry_ingest_runs
              (status, source_filename, source_download_url, source_observed_at)
            VALUES ('running', %s, %s, %s)
            RETURNING run_id;
            """,
            (filename, url, observed_at),
        )
        run_id = str(cur.fetchone()[0])
    conn.commit()
    log.info("audit run_id=%s", run_id)
    return run_id


def finalize_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_seen: dict[str, int],
    rows_upserted: dict[str, int],
    error_text: str | None = None,
) -> None:
    """UPDATE the run row with terminal status + counters."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.faa_aircraft_registry_ingest_runs SET
              status        = %s,
              rows_seen     = %s,
              rows_upserted = %s,
              completed_at  = now(),
              error_text    = %s
            WHERE run_id = %s;
            """,
            (
                status,
                Jsonb(rows_seen),
                Jsonb(rows_upserted),
                error_text,
                run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-CSV processing
# --------------------------------------------------------------------------- #

def process_csv(
    conn: psycopg.Connection,
    csv_basename: str,
    csv_fileobj: io.TextIOWrapper,
    *,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime | None,
    run_id: str,
    max_rows: int | None = None,
) -> tuple[int, int]:
    """Process one CSV file. Returns (rows_seen, rows_upserted)."""
    table_name, pk_cols, typed_cols = CSV_TABLES[csv_basename]
    overrides = PER_CSV_HEADER_OVERRIDES.get(csv_basename, {})
    stage_table = f"_stage_faa_aircraft_{csv_basename.lower().replace('.txt', '')}"

    log.info("processing %s → %s (pk=%s)", csv_basename, table_name, pk_cols)

    # Build provenance fields constant across all rows in this CSV.
    now_ts = datetime.now(timezone.utc)
    run_meta = {
        "run_id": run_id,
        "csv_basename": csv_basename,
    }
    task_id = os.environ.get("MODAL_TASK_ID")
    schedule_id = os.environ.get("MODAL_SCHEDULE_ID")

    all_copy_cols = tuple(typed_cols) + PROVENANCE_COLS

    reader = csv.DictReader(csv_fileobj)

    # Log the actual headers seen in this CSV so drift is auditable.
    log.info("%s actual headers: %r", csv_basename, reader.fieldnames)

    rows_seen = 0
    rows_upserted = 0
    seen_pks: set[tuple] = set()  # dedupe rows colliding on pk within this batch

    # Stream in batches of BATCH_SIZE.
    batch: list[tuple] = []

    def _flush_batch(batch: list[tuple]) -> int:
        if not batch:
            return 0
        with conn.cursor() as cur:
            # Create/truncate temp staging table.
            cur.execute(f"CREATE TEMP TABLE IF NOT EXISTS {stage_table} AS SELECT * FROM {table_name} WHERE FALSE;")
            cur.execute(f"TRUNCATE {stage_table};")

            # COPY batch into staging.
            copy_cols_str = ", ".join(all_copy_cols)
            with cur.copy(f"COPY {stage_table} ({copy_cols_str}) FROM STDIN") as copy:
                for row_tuple in batch:
                    copy.write_row(row_tuple)

            # Upsert from staging into target.
            non_pk_typed = [c for c in typed_cols if c not in pk_cols]
            update_cols = non_pk_typed + [
                "raw_source_row", "source_provider", "source_filename",
                "source_download_url", "source_observed_at", "source_run_metadata",
                "source_task_id", "source_schedule_id",
            ]
            set_clause = ",\n                ".join(
                f"{c} = EXCLUDED.{c}" for c in update_cols
            )
            set_clause += ",\n                ingested_at = now()"

            conflict_target = ", ".join(pk_cols)

            # Build WHERE clause for IS DISTINCT FROM check (typed + provenance data cols).
            check_cols = non_pk_typed + [
                "raw_source_row", "source_filename", "source_download_url",
            ]
            if check_cols:
                distinct_clauses = " OR ".join(
                    f"(t.{c} IS DISTINCT FROM EXCLUDED.{c})" for c in check_cols
                )
                where_clause = f"WHERE {distinct_clauses}"
            else:
                where_clause = ""

            cur.execute(f"""
                WITH ins AS (
                  INSERT INTO {table_name} AS t ({copy_cols_str})
                  SELECT {copy_cols_str} FROM {stage_table}
                  ON CONFLICT ({conflict_target}) DO UPDATE SET
                    {set_clause}
                  {where_clause}
                  RETURNING (xmax = 0) AS inserted
                )
                SELECT COUNT(*) FILTER (WHERE inserted),
                       COUNT(*) FROM ins;
            """)
            inserted, _total = cur.fetchone()
        conn.commit()
        return int(inserted)

    for raw_row in reader:
        if max_rows is not None and rows_seen >= max_rows:
            break

        # Snake-case the CSV headers; skip unknown headers.
        # Strip whitespace AND BOM from headers. The FAA file is UTF-8 BOM
        # encoded but read as latin-1, so the leading BOM bytes 0xEF 0xBB 0xBF
        # decode to the 3-char prefix "ï»¿" — strip those alongside U+FEFF
        # (in case the file is read as utf-8-sig elsewhere) and whitespace.
        typed_vals: dict[str, str | None] = {}
        for header, value in raw_row.items():
            if header is None:
                continue
            normalized = header.lstrip("﻿ï»¿ \t").strip()
            col = overrides.get(normalized) or HEADER_TO_COLUMN.get(normalized)
            if col and col in typed_cols:
                typed_vals[col] = value.strip() if value else None

        # Synthetic row_md5 PK for tables that lack a usable natural PK
        # (currently only DOCINDEX). Computed from canonical-json of the raw
        # source row so re-ingests are idempotent.
        if "row_md5" in typed_cols:
            canonical = json.dumps(dict(raw_row), sort_keys=True, ensure_ascii=False)
            typed_vals["row_md5"] = hashlib.md5(canonical.encode("utf-8")).hexdigest()

        # Dedupe within batch on PK — FAA files occasionally have dup natural-key
        # rows (e.g. revoked-then-reissued n_number); keep the last one. Skip
        # rows missing any PK component (would conflict on NULL during INSERT).
        pk_tuple = tuple(typed_vals.get(c) for c in pk_cols)
        if any(v is None for v in pk_tuple):
            continue
        if pk_tuple in seen_pks:
            # Replace prior occurrence by removing it from batch.
            batch = [b for b in batch
                     if tuple(b[i] for i, c in enumerate(typed_cols) if c in pk_cols) != pk_tuple]
        seen_pks.add(pk_tuple)

        # Build the row tuple in all_copy_cols order.
        row_tuple: tuple = (
            *tuple(typed_vals.get(c) for c in typed_cols),
            Jsonb(dict(raw_row)),       # raw_source_row
            PROVIDER,                   # source_provider
            source_filename,            # source_filename
            source_download_url,        # source_download_url
            source_observed_at,         # source_observed_at
            Jsonb(run_meta),            # source_run_metadata
            task_id,                    # source_task_id
            schedule_id,                # source_schedule_id
            now_ts,                     # ingested_at
        )

        batch.append(row_tuple)
        rows_seen += 1

        if len(batch) >= BATCH_SIZE:
            rows_upserted += _flush_batch(batch)
            batch = []
            seen_pks.clear()
            log.info("  %s: %d rows seen so far", csv_basename, rows_seen)

    # Flush remaining rows.
    rows_upserted += _flush_batch(batch)

    log.info(
        "%s done: rows_seen=%d rows_upserted=%d",
        csv_basename, rows_seen, rows_upserted,
    )
    return rows_seen, rows_upserted


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> dict[str, Any]:
    """Entry point. Returns a dict with run_id, rows_seen, rows_upserted."""
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and parse but do not write to DB",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N rows per CSV (smoke-test limit)",
    )
    args = p.parse_args(argv)

    # Probe + record observed_at.
    observed_at = _probe_zip(ZIP_URL)
    log.info("resolved: %s (Last-Modified: %s)", ZIP_URL, observed_at)

    if args.dry_run:
        log.info("DRY RUN — no DB writes")
        zip_path = _download_zip(ZIP_URL)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                log.info("ZIP contents: %s", names)
                # Dump headers for every .txt member — both ingested ones (in
                # CSV_TABLES) and yet-to-be-ingested ones, marked accordingly.
                # Useful when adding new source tables for previously-skipped files.
                for name in names:
                    if not name.endswith(".txt"):
                        continue
                    marker = "[ingested]" if name in CSV_TABLES else "[NOT YET INGESTED]"
                    with zf.open(name) as f:
                        reader = csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"))
                        first = next(iter(reader), None)
                        log.info("  %s %s headers: %s", marker, name, list(first.keys()) if first else "(empty)")
        finally:
            Path(zip_path).unlink(missing_ok=True)
        return {"dry_run": True, "url": ZIP_URL}

    # Download.
    zip_path = _download_zip(ZIP_URL)

    db_url = _database_url()
    rows_seen: dict[str, int] = {}
    rows_upserted: dict[str, int] = {}

    try:
        with psycopg.connect(db_url, autocommit=False) as conn:
            run_id = insert_run(conn, ZIP_FILENAME, ZIP_URL, observed_at)

            try:
                with zipfile.ZipFile(zip_path) as zf:
                    available = set(zf.namelist())
                    log.info("ZIP contains: %s", sorted(available))
                    for csv_basename in CSV_TABLES:
                        if csv_basename not in available:
                            log.warning("expected CSV not found in ZIP: %s — skipping", csv_basename)
                            key = csv_basename.replace(".txt", "").lower()
                            rows_seen[key] = 0
                            rows_upserted[key] = 0
                            continue
                        key = csv_basename.replace(".txt", "").lower()
                        with zf.open(csv_basename) as raw_f:
                            text_f = io.TextIOWrapper(raw_f, encoding="latin-1")
                            seen, upserted = process_csv(
                                conn,
                                csv_basename,
                                text_f,
                                source_filename=ZIP_FILENAME,
                                source_download_url=ZIP_URL,
                                source_observed_at=observed_at,
                                run_id=run_id,
                                max_rows=args.max_rows,
                            )
                            rows_seen[key] = seen
                            rows_upserted[key] = upserted

                finalize_run(
                    conn,
                    run_id,
                    status="succeeded",
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                )

            except Exception as exc:
                log.exception("ingest failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                finalize_run(
                    conn,
                    run_id,
                    status="failed",
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    error_text=str(exc),
                )
                raise

    finally:
        Path(zip_path).unlink(missing_ok=True)

    log.info(
        "DONE — rows_seen=%s rows_upserted=%s",
        json.dumps(rows_seen),
        json.dumps(rows_upserted),
    )
    return {
        "run_id": run_id,
        "rows_seen": rows_seen,
        "rows_upserted": rows_upserted,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
    sys.exit(0)
