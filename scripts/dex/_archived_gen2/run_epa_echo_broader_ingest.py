#!/usr/bin/env python3
"""EPA ECHO (broader-than-NPDES) — bulk-CSV ingest from echo.epa.gov.

5 ECHO program bundles → 53 source tables under entities schema:
  frs   (Facility Registry Service, cross-program join spine) —  4 tables
  case  (FedRACA federal enforcement cases — demand-side urgency) — 22 tables
  rcra  (RCRA hazwaste handlers + violations + enforcements) —  6 tables
  air   (ICIS-AIR Clean Air Act Title V) — 10 tables
  sdwa  (SDWA public water systems) — 11 tables

Companion to the parallel-agent NPDES ingest (npdes_downloads.zip — out of scope here).

Idempotency: TRUNCATE+COPY per (bundle, csv) per ingest run. ECHO bundles have
snapshot-replace semantics — re-running with the same bundle produces identical
row counts. No UPSERT logic.

Audit: ops.epa_echo_ingest_runs — one row per (dataset_bundle, dataset_file).
Skip-if-unchanged: HEAD bundle URL Last-Modified compared to prior successful
run for ANY CSV in that bundle (bundle-level cadence; ECHO refreshes weekly).

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_echo_broader_ingest.py case
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_echo_broader_ingest.py all
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_echo_broader_ingest.py all --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_echo_broader_ingest.py case --dry-run
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_echo_broader_ingest.py all --recon-only
  PYTHONPATH=. doppler run -- python3 scripts/run_epa_echo_broader_ingest.py case --max-rows 1000
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
from typing import Any, Iterator

import httpx
import psycopg
from psycopg.types.json import Jsonb


DEFAULT_BATCH_SIZE = 50_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("epa-echo-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Per-(bundle, csv) configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CsvConfig:
    bundle: str           # 'frs','case','rcra','air','sdwa'
    csv_name: str         # filename within the bundle (preserved case)
    schema: str           # 'entities'
    table: str            # source_epa_*
    numeric_cols: frozenset[str]

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True)
class BundleConfig:
    bundle: str
    url: str
    csvs: tuple[CsvConfig, ...]


# Numeric column sets — must match migration column types exactly.
# Anything not listed here is text in the migration.
NUMERIC = {
    "source_epa_frs_facilities": frozenset({"latitude_measure", "longitude_measure"}),
    "source_epa_case_enforcements": frozenset({
        "fiscal_year", "total_penalty_assessed_amt",
        "total_cost_recovery_amt", "total_comp_action_amt",
    }),
    "source_epa_case_violations": frozenset({"rank_order"}),
    "source_epa_case_penalties": frozenset({
        "fed_penalty", "st_lcl_penalty", "total_sep",
        "compliance_action_cost", "federal_cost_recovery_amt",
        "state_local_cost_recovery_amt", "penalty_collected_amt",
    }),
    "source_epa_case_law_sections": frozenset({"rank_order"}),
    "source_epa_case_priorities": frozenset({"fiscal_year"}),
    "source_epa_case_enforcement_conclusions": frozenset({
        "enf_conclusion_nmbr", "settlement_fy", "fed_penalty_assessed_amt",
        "state_local_penalty_amt", "sep_amt", "compliance_action_cost",
        "cost_recovery_awarded_amt",
    }),
    "source_epa_case_enforcement_conclusion_dollars": frozenset({
        "state_local_penalty_amt", "cost_recovery_amt", "fed_penalty",
        "compliance_action_cost", "sep_cost", "penalty_collected_amt",
    }),
    "source_epa_case_enforcement_conclusion_sep": frozenset({"sep_amt"}),
    "source_epa_rcra_facilities": frozenset({"latitude83", "longitude83"}),
    "source_epa_rcra_enforcements": frozenset({
        "pmp_amount", "fmp_amount", "fsc_amount", "scr_amount",
    }),
    "source_epa_air_formal_actions": frozenset({"penalty_amount"}),
    "source_epa_sdwa_pub_water_systems": frozenset({
        "population_served_count", "service_connections_count",
    }),
    "source_epa_sdwa_violations_enforcement": frozenset({"severity_ind_cnt"}),
}


def _csv(bundle: str, csv_name: str, table: str) -> CsvConfig:
    return CsvConfig(
        bundle=bundle,
        csv_name=csv_name,
        schema="entities",
        table=table,
        numeric_cols=NUMERIC.get(table, frozenset()),
    )


BUNDLES: dict[str, BundleConfig] = {
    "frs": BundleConfig(
        bundle="frs",
        url="https://echo.epa.gov/files/echodownloads/frs_downloads.zip",
        csvs=(
            _csv("frs", "FRS_FACILITIES.csv",    "source_epa_frs_facilities"),
            _csv("frs", "FRS_PROGRAM_LINKS.csv", "source_epa_frs_program_links"),
            _csv("frs", "FRS_NAICS_CODES.csv",   "source_epa_frs_naics_codes"),
            _csv("frs", "FRS_SIC_CODES.csv",     "source_epa_frs_sic_codes"),
        ),
    ),
    "case": BundleConfig(
        bundle="case",
        url="https://echo.epa.gov/files/echodownloads/case_downloads.zip",
        csvs=(
            _csv("case", "CASE_ENFORCEMENTS.csv",                            "source_epa_case_enforcements"),
            _csv("case", "CASE_DEFENDANTS.csv",                              "source_epa_case_defendants"),
            _csv("case", "CASE_FACILITIES.csv",                              "source_epa_case_facilities"),
            _csv("case", "CASE_VIOLATIONS.csv",                              "source_epa_case_violations"),
            _csv("case", "CASE_PENALTIES.csv",                               "source_epa_case_penalties"),
            _csv("case", "CASE_MILESTONES.csv",                              "source_epa_case_milestones"),
            _csv("case", "CASE_PROGRAMS.csv",                                "source_epa_case_programs"),
            _csv("case", "CASE_LAW_SECTIONS.csv",                            "source_epa_case_law_sections"),
            _csv("case", "CASE_POLLUTANTS.csv",                              "source_epa_case_pollutants"),
            _csv("case", "CASE_PRIORITIES.csv",                              "source_epa_case_priorities"),
            _csv("case", "CASE_RELIEF_SOUGHT.csv",                           "source_epa_case_relief_sought"),
            _csv("case", "CASE_ENFORCEMENT_TYPE.csv",                        "source_epa_case_enforcement_type"),
            _csv("case", "CASE_ENFORCEMENT_CONCLUSIONS.csv",                 "source_epa_case_enforcement_conclusions"),
            _csv("case", "CASE_ENFORCEMENT_CONCLUSION_DOLLARS.csv",          "source_epa_case_enforcement_conclusion_dollars"),
            _csv("case", "CASE_ENFORCEMENT_CONCLUSION_FACILITIES.csv",       "source_epa_case_enforcement_conclusion_facilities"),
            _csv("case", "CASE_ENFORCEMENT_CONCLUSION_POLLUTANTS.csv",       "source_epa_case_enforcement_conclusion_pollutants"),
            _csv("case", "CASE_ENFORCEMENT_CONCLUSION_COMPLYING_ACTIONS.csv","source_epa_case_enforcement_conclusion_complying_actions"),
            _csv("case", "CASE_ENFORCEMENT_CONCLUSION_SEP.csv",              "source_epa_case_enforcement_conclusion_sep"),
            _csv("case", "CASE_REGIONAL_DOCKETS.csv",                        "source_epa_case_regional_dockets"),
            _csv("case", "CASE_RELATED_ACTIVITIES.csv",                      "source_epa_case_related_activities"),
            _csv("case", "EPA_INFORMAL_ENFORCEMENT_ACTIONS.csv",             "source_epa_informal_enforcement_actions"),
            _csv("case", "ICIS_FEC_EPA_INSPECTIONS.csv",                     "source_epa_icis_fec_inspections"),
        ),
    ),
    "rcra": BundleConfig(
        bundle="rcra",
        url="https://echo.epa.gov/files/echodownloads/rcra_downloads.zip",
        csvs=(
            _csv("rcra", "RCRA_FACILITIES.csv",     "source_epa_rcra_facilities"),
            _csv("rcra", "RCRA_EVALUATIONS.csv",    "source_epa_rcra_evaluations"),
            _csv("rcra", "RCRA_VIOLATIONS.csv",     "source_epa_rcra_violations"),
            _csv("rcra", "RCRA_ENFORCEMENTS.csv",   "source_epa_rcra_enforcements"),
            _csv("rcra", "RCRA_VIOSNC_HISTORY.csv", "source_epa_rcra_viosnc_history"),
            _csv("rcra", "RCRA_NAICS.csv",          "source_epa_rcra_naics"),
        ),
    ),
    "air": BundleConfig(
        bundle="air",
        url="https://echo.epa.gov/files/echodownloads/ICIS-AIR_downloads.zip",
        csvs=(
            _csv("air", "ICIS-AIR_FACILITIES.csv",        "source_epa_air_facilities"),
            _csv("air", "ICIS-AIR_PROGRAMS.csv",          "source_epa_air_programs"),
            _csv("air", "ICIS-AIR_PROGRAM_SUBPARTS.csv",  "source_epa_air_program_subparts"),
            _csv("air", "ICIS-AIR_POLLUTANTS.csv",        "source_epa_air_pollutants"),
            _csv("air", "ICIS-AIR_FCES_PCES.csv",         "source_epa_air_fces_pces"),
            _csv("air", "ICIS-AIR_STACK_TESTS.csv",       "source_epa_air_stack_tests"),
            _csv("air", "ICIS-AIR_TITLEV_CERTS.csv",      "source_epa_air_titlev_certs"),
            _csv("air", "ICIS-AIR_FORMAL_ACTIONS.csv",    "source_epa_air_formal_actions"),
            _csv("air", "ICIS-AIR_INFORMAL_ACTIONS.csv",  "source_epa_air_informal_actions"),
            _csv("air", "ICIS-AIR_VIOLATION_HISTORY.csv", "source_epa_air_violation_history"),
        ),
    ),
    "sdwa": BundleConfig(
        bundle="sdwa",
        url="https://echo.epa.gov/files/echodownloads/SDWA_latest_downloads.zip",
        csvs=(
            _csv("sdwa", "SDWA_FACILITIES.csv",              "source_epa_sdwa_facilities"),
            _csv("sdwa", "SDWA_PUB_WATER_SYSTEMS.csv",       "source_epa_sdwa_pub_water_systems"),
            _csv("sdwa", "SDWA_GEOGRAPHIC_AREAS.csv",        "source_epa_sdwa_geographic_areas"),
            _csv("sdwa", "SDWA_SERVICE_AREAS.csv",           "source_epa_sdwa_service_areas"),
            _csv("sdwa", "SDWA_LCR_SAMPLES.csv",             "source_epa_sdwa_lcr_samples"),
            _csv("sdwa", "SDWA_VIOLATIONS_ENFORCEMENT.csv",  "source_epa_sdwa_violations_enforcement"),
            _csv("sdwa", "SDWA_PN_VIOLATION_ASSOC.csv",      "source_epa_sdwa_pn_violation_assoc"),
            _csv("sdwa", "SDWA_EVENTS_MILESTONES.csv",       "source_epa_sdwa_events_milestones"),
            _csv("sdwa", "SDWA_SITE_VISITS.csv",             "source_epa_sdwa_site_visits"),
            _csv("sdwa", "SDWA_REF_CODE_VALUES.csv",         "source_epa_sdwa_ref_code_values"),
            _csv("sdwa", "SDWA_REF_ANSI_AREAS.csv",          "source_epa_sdwa_ref_ansi_areas"),
        ),
    ),
}


# --------------------------------------------------------------------------- #
# DB / HTTP helpers
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED is not set in the environment.")
    return url


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
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
            return cl, lm
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=1800.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss", url, r.status_code, wait)
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


class _NulStrippingReader(io.TextIOBase):
    """Wraps a TextIOBase and strips ASCII NUL (\\x00) on every read.

    EPA ECHO CSVs occasionally embed literal NUL bytes (encountered in
    CASE_ENFORCEMENTS.csv during recon). The csv module raises
    `_csv.Error: line contains NUL`, so we filter at the stream layer."""

    def __init__(self, fh: io.TextIOBase):
        self._fh = fh

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> str:
        data = self._fh.read(size)
        if "\x00" in data:
            data = data.replace("\x00", "")
        return data

    def readline(self, size: int = -1) -> str:
        line = self._fh.readline(size)
        if "\x00" in line:
            line = line.replace("\x00", "")
        return line

    def close(self) -> None:
        self._fh.close()


def open_csv_in_zip(zip_path: Path, csv_name: str) -> tuple[zipfile.ZipFile, io.TextIOBase]:
    z = zipfile.ZipFile(zip_path)
    try:
        info = z.getinfo(csv_name)
    except KeyError:
        # Try case-insensitive match
        for n in z.namelist():
            if n.lower() == csv_name.lower():
                info = z.getinfo(n)
                break
        else:
            z.close()
            raise RuntimeError(
                f"{csv_name} not found in {zip_path.name}; contents: {z.namelist()[:10]}…"
            )
    raw = z.open(info, "r")
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    return z, _NulStrippingReader(text)


def fetch_table_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    """Read the actual column ordering from information_schema (excluding id and
    the trailing audit columns). The migration generated columns from the CSV
    header so this should match exactly."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
               AND column_name NOT IN ('id', 'dataset_year', 'source_file_last_modified', 'ingested_at')
             ORDER BY ordinal_position;
        """, (schema, table))
        return [r[0] for r in cur.fetchall()]


def truncate_target(conn: psycopg.Connection, fqn: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {fqn};")
    conn.commit()


def copy_chunk_to_target(
    conn: psycopg.Connection,
    fqn: str,
    cols: list[str],
    rows: list[tuple[Any, ...]],
) -> int:
    if not rows:
        return 0
    full_cols = list(cols) + ["dataset_year", "source_file_last_modified"]
    sql = f"COPY {fqn} ({', '.join(full_cols)}) FROM STDIN"
    with conn.cursor() as cur:
        with cur.copy(sql) as copy:
            for row in rows:
                copy.write_row(row)
    conn.commit()
    return len(rows)


def stream_csv_to_db(
    conn: psycopg.Connection,
    cfg: CsvConfig,
    csv_fh: io.TextIOBase,
    table_cols: list[str],
    *,
    dataset_year: int,
    source_file_last_modified: datetime | None,
    batch_size: int,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int]:
    """Returns (rows_inserted, rows_in_csv)."""
    reader = csv.reader(csv_fh)
    try:
        header = next(reader)
    except StopIteration:
        return 0, 0
    header_lower = [h.strip().lower() for h in header]
    expected = set(table_cols)
    missing = sorted(expected - set(header_lower))
    extra = sorted(set(header_lower) - expected)
    if missing:
        log.warning("%s CSV missing %d columns expected by migration: %s",
                    log_prefix, len(missing), missing[:10])
    if extra:
        log.warning("%s CSV has %d unexpected columns (will be dropped): %s",
                    log_prefix, len(extra), extra[:10])

    col_idx = [header_lower.index(c) if c in header_lower else None for c in table_cols]
    fqn = cfg.fully_qualified

    rows_seen = total_inserted = 0
    chunk: list[tuple[Any, ...]] = []
    chunk_started = time.monotonic()
    for raw in reader:
        rows_seen += 1
        if max_rows is not None and rows_seen > max_rows:
            rows_seen -= 1
            break
        out: list[Any] = []
        for col, idx in zip(table_cols, col_idx):
            if idx is None or idx >= len(raw):
                out.append(None)
                continue
            v = raw[idx]
            if v is None or v == "":
                out.append(None)
            else:
                out.append(v)
        out.append(dataset_year)
        out.append(source_file_last_modified)
        chunk.append(tuple(out))
        if len(chunk) >= batch_size:
            n = copy_chunk_to_target(conn, fqn, table_cols, chunk)
            total_inserted += n
            log.info(
                "%s chunk: rows_seen=%d ins=%d (cum=%d) elapsed=%.1fs",
                log_prefix, rows_seen, n, total_inserted,
                time.monotonic() - chunk_started,
            )
            chunk.clear()
            chunk_started = time.monotonic()
    if chunk:
        n = copy_chunk_to_target(conn, fqn, table_cols, chunk)
        total_inserted += n
        log.info(
            "%s final chunk: rows_seen=%d ins=%d (cum=%d) elapsed=%.1fs",
            log_prefix, rows_seen, n, total_inserted,
            time.monotonic() - chunk_started,
        )
    return total_inserted, rows_seen


# --------------------------------------------------------------------------- #
# Audit row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    cfg: CsvConfig,
    *,
    bundle_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.epa_echo_ingest_runs (
        dataset_bundle, dataset_file, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cfg.bundle, cfg.csv_name, bundle_url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, cfg: CsvConfig
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.epa_echo_ingest_runs
             WHERE dataset_bundle = %s AND dataset_file = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (cfg.bundle, cfg.csv_name),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    cfg: CsvConfig,
    *,
    bundle_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.epa_echo_ingest_runs (
                dataset_bundle, dataset_file, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                cfg.bundle, cfg.csv_name, bundle_url, source_last_modified,
                prior_source_last_modified, started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes: int | None,
    csv_bytes: int,
    rows_in_csv: int,
    rows_inserted: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.epa_echo_ingest_runs
               SET status = %s, zip_bytes_downloaded = %s,
                   csv_bytes_extracted = %s, rows_in_csv = %s,
                   rows_inserted = %s, rows_updated = 0,
                   rows_unchanged = 0,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, rows_in_csv,
            rows_inserted, duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Recon report
# --------------------------------------------------------------------------- #


@dataclass
class ReconStats:
    bundle: str
    table_fqn: str
    csv_name: str
    total_rows: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


def gather_recon(conn: psycopg.Connection, cfg: CsvConfig) -> ReconStats:
    s = ReconStats(bundle=cfg.bundle, table_fqn=cfg.fully_qualified, csv_name=cfg.csv_name)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {cfg.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s

    # Per-table targeted recon
    return _gather_recon_specific(conn, cfg, s)


def _gather_recon_specific(conn: psycopg.Connection, cfg: CsvConfig, s: ReconStats) -> ReconStats:
    fqn = cfg.fully_qualified
    with conn.cursor() as cur:

        if cfg.table == "source_epa_frs_facilities":
            cur.execute(f"""
                SELECT fac_state, count(*) c FROM {fqn}
                 WHERE fac_state IS NOT NULL
                 GROUP BY fac_state ORDER BY c DESC LIMIT 10;
            """)
            s.notes["top_states"] = [{"state": r[0], "count": int(r[1])} for r in cur.fetchall()]
            cur.execute(f"""
                SELECT count(*) FILTER (WHERE registry_id IS NOT NULL),
                       count(*) FILTER (WHERE fac_name IS NOT NULL),
                       count(*) FILTER (WHERE fac_street IS NOT NULL),
                       count(*) FILTER (WHERE latitude_measure IS NOT NULL)
                  FROM {fqn};
            """)
            r = cur.fetchone()
            s.notes["registry_id_populated"] = int(r[0])
            s.notes["fac_name_populated"] = int(r[1])
            s.notes["fac_street_populated"] = int(r[2])
            s.notes["lat_populated"] = int(r[3])

        elif cfg.table == "source_epa_frs_program_links":
            cur.execute(f"""
                SELECT pgm_sys_acrnm, count(*) c FROM {fqn}
                 WHERE pgm_sys_acrnm IS NOT NULL
                 GROUP BY pgm_sys_acrnm ORDER BY c DESC LIMIT 15;
            """)
            s.notes["top_epa_programs"] = [
                {"program": r[0], "facility_count": int(r[1])} for r in cur.fetchall()
            ]

        elif cfg.table == "source_epa_case_enforcements":
            cur.execute(f"""
                SELECT activity_status_desc, count(*) c FROM {fqn}
                 WHERE activity_status_desc IS NOT NULL
                 GROUP BY activity_status_desc ORDER BY c DESC LIMIT 10;
            """)
            s.notes["case_status_distribution"] = [
                {"status": r[0], "count": int(r[1])} for r in cur.fetchall()
            ]
            cur.execute(f"""
                SELECT
                  count(*) FILTER (WHERE total_penalty_assessed_amt > 0),
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY total_penalty_assessed_amt)
                    FILTER (WHERE total_penalty_assessed_amt > 0),
                  percentile_cont(0.9) WITHIN GROUP (ORDER BY total_penalty_assessed_amt)
                    FILTER (WHERE total_penalty_assessed_amt > 0),
                  max(total_penalty_assessed_amt),
                  sum(total_penalty_assessed_amt)
                  FROM {fqn};
            """)
            cnt, p50, p90, mx, total = cur.fetchone()
            s.notes["cases_with_assessed_penalty"] = int(cnt or 0)
            s.notes["penalty_p50_dollars"] = float(p50) if p50 else None
            s.notes["penalty_p90_dollars"] = float(p90) if p90 else None
            s.notes["penalty_max_dollars"] = float(mx) if mx else None
            s.notes["penalty_total_dollars"] = float(total) if total else None
            cur.execute(f"""
                SELECT min(fiscal_year), max(fiscal_year)
                  FROM {fqn} WHERE fiscal_year IS NOT NULL;
            """)
            mn, mx = cur.fetchone()
            s.notes["fiscal_year_min"] = int(mn) if mn else None
            s.notes["fiscal_year_max"] = int(mx) if mx else None

        elif cfg.table == "source_epa_case_defendants":
            cur.execute(f"""
                SELECT defendant_name, count(*) c FROM {fqn}
                 WHERE defendant_name IS NOT NULL
                 GROUP BY defendant_name ORDER BY c DESC LIMIT 15;
            """)
            s.notes["top_defendants_by_case_count"] = [
                {"name": r[0], "case_count": int(r[1])} for r in cur.fetchall()
            ]
            cur.execute(f"SELECT count(DISTINCT defendant_name) FROM {fqn};")
            s.notes["distinct_defendant_names"] = int(cur.fetchone()[0])

        elif cfg.table == "source_epa_rcra_facilities":
            cur.execute(f"""
                SELECT facility_name, count(*) c FROM {fqn}
                 WHERE facility_name IS NOT NULL
                 GROUP BY facility_name ORDER BY c DESC LIMIT 15;
            """)
            s.notes["top_handlers_by_facility_count"] = [
                {"name": r[0], "count": int(r[1])} for r in cur.fetchall()
            ]
            cur.execute(f"""
                SELECT state_code, count(*) c FROM {fqn}
                 WHERE state_code IS NOT NULL
                 GROUP BY state_code ORDER BY c DESC LIMIT 10;
            """)
            s.notes["top_states"] = [
                {"state": r[0], "count": int(r[1])} for r in cur.fetchall()
            ]

        elif cfg.table == "source_epa_air_facilities":
            cur.execute(f"""
                SELECT state, count(*) c FROM {fqn}
                 WHERE state IS NOT NULL
                 GROUP BY state ORDER BY c DESC LIMIT 10;
            """)
            s.notes["top_states"] = [
                {"state": r[0], "count": int(r[1])} for r in cur.fetchall()
            ]
            cur.execute(f"""
                SELECT current_hpv, count(*) c FROM {fqn}
                 GROUP BY current_hpv ORDER BY c DESC LIMIT 10;
            """)
            s.notes["hpv_status_distribution"] = [
                {"current_hpv": r[0], "count": int(r[1])} for r in cur.fetchall()
            ]

        elif cfg.table == "source_epa_air_violation_history":
            cur.execute(f"""
                SELECT pgm_sys_id, count(*) c FROM {fqn}
                 WHERE pgm_sys_id IS NOT NULL
                 GROUP BY pgm_sys_id ORDER BY c DESC LIMIT 15;
            """)
            s.notes["top_facilities_by_violation_count"] = [
                {"pgm_sys_id": r[0], "violation_count": int(r[1])} for r in cur.fetchall()
            ]

        elif cfg.table == "source_epa_sdwa_pub_water_systems":
            cur.execute(f"""
                SELECT pws_name, population_served_count
                  FROM {fqn}
                 WHERE population_served_count IS NOT NULL
                 ORDER BY population_served_count DESC NULLS LAST LIMIT 15;
            """)
            s.notes["top_pws_by_population"] = [
                {"name": r[0], "population": int(r[1])} for r in cur.fetchall()
            ]
            cur.execute(f"""
                SELECT count(*) FILTER (WHERE email_addr IS NOT NULL),
                       count(*) FILTER (WHERE phone_number IS NOT NULL),
                       count(*) FILTER (WHERE admin_name IS NOT NULL)
                  FROM {fqn};
            """)
            r = cur.fetchone()
            s.notes["email_populated"] = int(r[0])
            s.notes["phone_populated"] = int(r[1])
            s.notes["admin_name_populated"] = int(r[2])

        elif cfg.table == "source_epa_sdwa_violations_enforcement":
            cur.execute(f"""
                SELECT violation_status, count(*) c FROM {fqn}
                 WHERE violation_status IS NOT NULL
                 GROUP BY violation_status ORDER BY c DESC LIMIT 10;
            """)
            s.notes["violation_status_distribution"] = [
                {"status": r[0], "count": int(r[1])} for r in cur.fetchall()
            ]
            cur.execute(f"""
                SELECT count(*) FILTER (WHERE is_health_based_ind = 'Y'),
                       count(*) FILTER (WHERE is_major_viol_ind = 'Y')
                  FROM {fqn};
            """)
            hb, mj = cur.fetchone()
            s.notes["health_based_violations"] = int(hb or 0)
            s.notes["major_violations"] = int(mj or 0)

        else:
            # Default: just total. Already captured.
            pass
    return s


def print_recon(s: ReconStats) -> None:
    print(f"=== RECON: {s.bundle} / {s.csv_name}  ({s.table_fqn}) ===")
    print(f"  total rows: {s.total_rows:,}")
    for k, v in s.notes.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for kk, vv in v.items():
                print(f"      {kk}: {vv:,}" if isinstance(vv, int) else f"      {kk}: {vv}")
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
# Per-bundle ingest
# --------------------------------------------------------------------------- #


def ingest_bundle(
    bundle_cfg: BundleConfig,
    *,
    batch_size: int,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
) -> int:
    log_prefix = f"[{bundle_cfg.bundle}]"
    log.info("%s start url=%s", log_prefix, bundle_cfg.url)
    bundle_started_wall = time.monotonic()

    with httpx.Client(headers={"User-Agent": "data-engine-x/epa-echo-ingest"}) as client:
        try:
            content_length, source_last_modified = head_url(client, bundle_cfg.url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)

        # bundle-level skip-if-unchanged: if ANY CSV in the bundle has a prior
        # successful run with source_last_modified >= current, skip the whole
        # bundle (one bundle has one Last-Modified — they all advance together).
        if skip_if_unchanged and not dry_run:
            with psycopg.connect(_database_url()) as conn:
                first_csv = bundle_cfg.csvs[0]
                prior = get_prior_source_last_modified(conn, first_csv)
                if (
                    prior is not None
                    and source_last_modified is not None
                    and source_last_modified <= prior
                ):
                    log.info("%s source_last_modified unchanged — recording no_change for all CSVs", log_prefix)
                    for cfg in bundle_cfg.csvs:
                        write_no_change_run(
                            conn, cfg,
                            bundle_url=bundle_cfg.url,
                            source_last_modified=source_last_modified,
                            prior_source_last_modified=prior,
                        )
                    return 0

        # Download bundle once
        zip_path = workdir / f"{bundle_cfg.bundle}_downloads.zip"
        try:
            zip_bytes = download_zip(client, bundle_cfg.url, zip_path)
            log.info("%s downloaded %d bytes -> %s", log_prefix, zip_bytes, zip_path)

            if dry_run:
                log.info("%s DRY RUN — verifying CSV headers only", log_prefix)
                for cfg in bundle_cfg.csvs:
                    z, fh = open_csv_in_zip(zip_path, cfg.csv_name)
                    with z, fh:
                        line = fh.readline()
                        cols = line.rstrip("\n").rstrip("\r").split(",")
                        log.info("%s %s cols=%d header[:5]=%s",
                                 log_prefix, cfg.csv_name, len(cols), cols[:5])
                return 0

            # Compute dataset_year from the bundle's Last-Modified
            dataset_year = (source_last_modified or datetime.now(timezone.utc)).year

            rc_total = 0
            for cfg in bundle_cfg.csvs:
                csv_log_prefix = f"[{cfg.bundle}/{cfg.csv_name}]"
                csv_started = time.monotonic()
                try:
                    info = zipfile.ZipFile(zip_path).getinfo(cfg.csv_name)
                    csv_uncompressed_bytes = int(info.file_size)
                except KeyError:
                    csv_uncompressed_bytes = 0

                with psycopg.connect(_database_url()) as conn:
                    table_cols = fetch_table_columns(conn, cfg.schema, cfg.table)
                    if not table_cols:
                        log.error("%s no columns found for %s — skipping", csv_log_prefix, cfg.fully_qualified)
                        rc_total = 1
                        continue

                    prior = get_prior_source_last_modified(conn, cfg)
                    run_id = insert_run_row(
                        conn, cfg,
                        bundle_url=bundle_cfg.url,
                        source_last_modified=source_last_modified,
                        prior_source_last_modified=prior,
                    )
                    log.info("%s run id: %s table_cols=%d", csv_log_prefix, run_id, len(table_cols))

                    try:
                        truncate_target(conn, cfg.fully_qualified)
                        z, fh = open_csv_in_zip(zip_path, cfg.csv_name)
                        with z, fh:
                            ins, rows_seen = stream_csv_to_db(
                                conn, cfg, fh, table_cols,
                                dataset_year=dataset_year,
                                source_file_last_modified=source_last_modified,
                                batch_size=batch_size,
                                log_prefix=csv_log_prefix,
                                max_rows=max_rows,
                            )

                        finalize_run_row(
                            conn, run_id, status="completed",
                            zip_bytes=zip_bytes if cfg is bundle_cfg.csvs[0] else None,
                            csv_bytes=csv_uncompressed_bytes,
                            rows_in_csv=rows_seen, rows_inserted=ins,
                            started_at=csv_started, error_message=None, notes=None,
                        )
                        log.info(
                            "%s DONE rows_in_csv=%d ins=%d wall=%.1fs",
                            csv_log_prefix, rows_seen, ins,
                            time.monotonic() - csv_started,
                        )
                    except Exception as exc:
                        log.exception("%s ingest failed", csv_log_prefix)
                        finalize_run_row(
                            conn, run_id, status="failed",
                            zip_bytes=None, csv_bytes=csv_uncompressed_bytes,
                            rows_in_csv=0, rows_inserted=0,
                            started_at=csv_started,
                            error_message=str(exc), notes=None,
                        )
                        rc_total = 1

            log.info("%s bundle wall=%.1fs", log_prefix, time.monotonic() - bundle_started_wall)
            return rc_total
        finally:
            zip_path.unlink(missing_ok=True)


def run_recon_only() -> None:
    with psycopg.connect(_database_url()) as conn:
        for bundle_cfg in BUNDLES.values():
            print(f"\n###### BUNDLE: {bundle_cfg.bundle} ({bundle_cfg.url}) ######\n")
            for cfg in bundle_cfg.csvs:
                try:
                    s = gather_recon(conn, cfg)
                    print_recon(s)
                except psycopg.errors.UndefinedTable:
                    log.error("Table missing for %s — apply the migration first.", cfg.fully_qualified)
                    return
        # Cross-source summary
        print("\n###### CROSS-SOURCE TOTALS ######\n")
        for bundle_cfg in BUNDLES.values():
            with conn.cursor() as cur:
                bundle_total = 0
                for cfg in bundle_cfg.csvs:
                    cur.execute(f"SELECT count(*) FROM {cfg.fully_qualified};")
                    bundle_total += int(cur.fetchone()[0])
                print(f"  {bundle_cfg.bundle:6s}: {bundle_total:>14,} rows across {len(bundle_cfg.csvs)} tables")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*)
                  FROM (
                    SELECT 1 FROM entities.source_epa_frs_facilities
                    UNION ALL SELECT 1 FROM entities.source_epa_frs_program_links
                    UNION ALL SELECT 1 FROM entities.source_epa_frs_naics_codes
                    UNION ALL SELECT 1 FROM entities.source_epa_frs_sic_codes
                  ) f;
            """)
            frs_total = int(cur.fetchone()[0])
            print(f"\n  Grand total (FRS sample): {frs_total:,}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bundle", choices=list(BUNDLES.keys()) + ["all"],
                   help="Bundle key (frs/case/rcra/air/sdwa) or 'all'.")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help="Rows per COPY chunk (default: 50000).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op for the bundle if source Last-Modified has not advanced.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD + download + read CSV headers only; no DB writes.")
    p.add_argument("--recon-only", action="store_true",
                   help="Run recon SELECTs against existing table contents and exit.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Per-CSV smoke-test cap. Default unlimited.")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP downloads (default: /tmp/epa_echo_ingest).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.recon_only:
        run_recon_only()
        return 0

    bundles = list(BUNDLES.values()) if args.bundle == "all" else [BUNDLES[args.bundle]]

    workdir = Path(args.workdir or "/tmp/epa_echo_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    rc = 0
    for cfg in bundles:
        ds_rc = ingest_bundle(
            cfg,
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
