#!/usr/bin/env python3
"""BTS Form 41 Air Carrier Financial Reports — raw CSV ingest.

Source:
    https://www.transtats.bts.gov/DL_SelectFields.aspx
      P-10 (annual employee statistics by labor category):  gnoyr_VQ=GDF
      P-6  (quarterly operating expenses by objective):     gnoyr_VQ=FME
      P-7  (quarterly operating expenses by functional grp): gnoyr_VQ=FKL

    The download endpoint is ASP.NET WebForms — there is no static prezip URL.
    Each request must:
      1. GET DL_SelectFields.aspx?gnoyr_VQ=<code> to seed __VIEWSTATE,
         __VIEWSTATEGENERATOR, __EVENTVALIDATION + cookies.
      2. POST back the form with cboYear / cboPeriod set + every column
         checkbox set + chkDownloadZip=on + btnDownload=Download.
      3. Server replies with application/zip (Content-Disposition:
         T_F41SCHEDULE_<sched>_<ts>.zip), one CSV per response.

Idempotency:
    INSERT ... ON CONFLICT (pk_cols) DO UPDATE ... WHERE row IS DISTINCT FROM
    EXCLUDED. PK shapes:
      P-10: (year, airline_id, entity)
      P-6:  (year, quarter, airline_id, unique_carrier_entity)
      P-7:  (year, quarter, airline_id, unique_carrier_entity)

Audit:
    ops.bts_form41_ingest_runs — one row per invocation. rows_seen /
    rows_upserted are jsonb objects keyed by schedule code (p10, p6, p7).

Coverage:
    P-10 historical years available via TranStats: 2017–most-recent-released
    P-6  historical years available via TranStats: 2020–most-recent-released
    P-7  historical years available via TranStats: 2020–most-recent-released
    Script defaults to "all years" but takes --years to narrow.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_form41_ingest.py p10
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_form41_ingest.py p6
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_form41_ingest.py p7
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_form41_ingest.py all
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_form41_ingest.py p7 --years 2024-2025
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_form41_ingest.py all --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "bts_form41"
USER_AGENT = "data-engine-x-api/bts-form41-ingest"
BASE_URL = "https://www.transtats.bts.gov/DL_SelectFields.aspx"
BATCH_SIZE = 5_000

# Default year ranges (inclusive). TranStats publishes updates as BTS releases;
# the ingest naturally extends as new years appear in the cboYear dropdown.
# P-10 dropdown shows 1990–most-recent; P-6 dropdown shows 2020–most-recent
# (the column shape changed in 2020; earlier P-6 years use a different code).
P10_DEFAULT_YEARS = (1990, 2030)
P6_DEFAULT_YEARS = (2020, 2030)
P7_DEFAULT_YEARS = (2020, 2030)  # P-7 publishes alongside P-6, same shape since 2020.

# Schedule code → (gnoyr_VQ, csv basename, target table, pk cols, typed cols)
SCHEDULES: dict[str, dict[str, Any]] = {
    "p10": {
        "gnoyr_vq": "GDF",
        "csv_basename": "T_F41SCHEDULE_P10.csv",
        "table": "entities.source_bts_f41_p10",
        "default_years": P10_DEFAULT_YEARS,
        "period": "All",
        "pk_cols": ("year", "airline_id", "entity"),
        # Order MUST match migration column order (excluding provenance).
        "typed_cols": (
            "year", "airline_id", "unique_carrier", "unique_carrier_name",
            "carrier", "carrier_name", "entity",
            "general_manage", "pilots_copilots", "other_flt_pers",
            "pass_gen_svc_admin", "maintenance",
            "arcft_traf_handling_grp1", "gen_arcft_traf_handling",
            "aircraft_control", "passenger_handling", "cargo_handling",
            "trainees_intructor", "statistical", "traffic_soliciters",
            "other", "transport_related", "total",
        ),
        "form_field_names": (
            "AIRLINE_ID", "UNIQUE_CARRIER", "UNIQUE_CARRIER_NAME",
            "CARRIER", "CARRIER_NAME", "ENTITY",
            "PILOTS_COPILOTS", "OTHER_FLT_PERS", "TRAINEES_INTRUCTOR",
            "MAINTENANCE", "GEN_ARCFT_TRAF_HANDLING",
            "ARCFT_TRAF_HANDLING_GRP1", "AIRCRAFT_CONTROL",
            "PASSENGER_HANDLING", "CARGO_HANDLING",
            "PASS_GEN_SVC_ADMIN", "TRAFFIC_SOLICITERS",
            "STATISTICAL", "TRANSPORT_RELATED", "GENERAL_MANAGE",
            "OTHER", "TOTAL", "YEAR",
        ),
        "int_cols": frozenset({
            "year", "airline_id", "general_manage", "pilots_copilots",
            "other_flt_pers", "pass_gen_svc_admin", "maintenance",
            "arcft_traf_handling_grp1", "gen_arcft_traf_handling",
            "aircraft_control", "passenger_handling", "cargo_handling",
            "trainees_intructor", "statistical", "traffic_soliciters",
            "other", "transport_related", "total",
        }),
        "numeric_cols": frozenset(),
    },
    "p6": {
        "gnoyr_vq": "FME",
        "csv_basename": "T_F41SCHEDULE_P6.csv",
        "table": "entities.source_bts_f41_p6",
        "default_years": P6_DEFAULT_YEARS,
        "period": "All",
        "pk_cols": ("year", "quarter", "airline_id", "unique_carrier_entity"),
        "typed_cols": (
            "salaries_mgt", "salaries_flight", "salaries_maint",
            "salaries_traffic", "salaries_other", "salaries",
            "benefits_personnel", "benefits_pensions", "benefits_payroll",
            "benefits", "salaries_benefits",
            "aircraft_fuel", "maint_material", "food", "other_materials",
            "materials_total", "advertising", "communication", "insurance",
            "outside_equip", "commisions_pax", "commissions_cargo",
            "other_services", "services_total", "landing_fees", "rentals",
            "depreciation", "amortization", "other", "trans_expense",
            "op_expense",
            "airline_id", "unique_carrier", "unique_carrier_name", "carrier",
            "carrier_name", "unique_carrier_entity", "region",
            "carrier_group_new", "carrier_group", "year", "quarter",
        ),
        "form_field_names": (
            "YEAR", "QUARTER", "AIRLINE_ID", "UNIQUE_CARRIER",
            "UNIQUE_CARRIER_NAME", "UNIQUE_CARRIER_ENTITY",
            "CARRIER", "CARRIER_NAME", "CARRIER_GROUP", "CARRIER_GROUP_NEW",
            "REGION",
            "SALARIES_FLIGHT", "SALARIES_MAINT", "SALARIES_TRAFFIC",
            "SALARIES_MGT", "SALARIES_OTHER", "SALARIES",
            "BENEFITS_PERSONNEL", "BENEFITS_PAYROLL", "BENEFITS_PENSIONS",
            "BENEFITS", "SALARIES_BENEFITS",
            "AIRCRAFT_FUEL", "MAINT_MATERIAL", "MATERIALS_TOTAL", "RENTALS",
            "DEPRECIATION", "AMORTIZATION", "LANDING_FEES", "INSURANCE",
            "COMMUNICATION", "ADVERTISING", "COMMISIONS_PAX",
            "COMMISSIONS_CARGO", "FOOD", "OTHER_MATERIALS", "OTHER_SERVICES",
            "OUTSIDE_EQUIP", "SERVICES_TOTAL", "TRANS_EXPENSE",
            "OTHER", "OP_EXPENSE",
        ),
        "int_cols": frozenset({
            "year", "quarter", "airline_id", "carrier_group", "carrier_group_new",
        }),
        "numeric_cols": frozenset({
            "salaries_mgt", "salaries_flight", "salaries_maint",
            "salaries_traffic", "salaries_other", "salaries",
            "benefits_personnel", "benefits_pensions", "benefits_payroll",
            "benefits", "salaries_benefits",
            "aircraft_fuel", "maint_material", "food", "other_materials",
            "materials_total", "advertising", "communication", "insurance",
            "outside_equip", "commisions_pax", "commissions_cargo",
            "other_services", "services_total", "landing_fees", "rentals",
            "depreciation", "amortization", "other", "trans_expense",
            "op_expense",
        }),
    },
    "p7": {
        "gnoyr_vq": "FKL",
        "csv_basename": "T_F41SCHEDULE_P7.csv",
        "table": "entities.source_bts_f41_p7",
        "default_years": P7_DEFAULT_YEARS,
        "period": "All",
        "pk_cols": ("year", "quarter", "airline_id", "unique_carrier_entity"),
        # Order MUST match migration column order (excluding provenance).
        "typed_cols": (
            "air_op_expense", "fl_att_expense", "food_expense",
            "oth_in_fl_expense", "pax_svc_expense", "line_svc_expense",
            "control_expense", "landing_fees", "air_svc_expense",
            "traffic_exp_pax", "traffic_exp_cargo", "traffic_exp_oth",
            "traffic_expense", "res_exp_pax", "res_exp_cargo", "res_exp_oth",
            "res_expense", "ad_exp_pax", "ad_exp_cargo", "ad_exp_inst",
            "ad_expense", "admin_expense", "depr_exp_maint", "amortization",
            "transport_exp", "total_op_expense", "maint_prop_equip",
            "depr_prop_equip", "maint_depr", "svc_sales_op_exp",
            "airline_id", "unique_carrier", "unique_carrier_name", "carrier",
            "carrier_name", "unique_carrier_entity", "region",
            "carrier_group_new", "carrier_group", "year", "quarter",
        ),
        "form_field_names": (
            "YEAR", "QUARTER", "AIRLINE_ID", "UNIQUE_CARRIER",
            "UNIQUE_CARRIER_NAME", "UNIQUE_CARRIER_ENTITY",
            "CARRIER", "CARRIER_NAME", "CARRIER_GROUP", "CARRIER_GROUP_NEW",
            "REGION",
            "AIR_OP_EXPENSE", "FL_ATT_EXPENSE", "FOOD_EXPENSE",
            "OTH_IN_FL_EXPENSE", "PAX_SVC_EXPENSE", "LINE_SVC_EXPENSE",
            "CONTROL_EXPENSE", "LANDING_FEES", "AIR_SVC_EXPENSE",
            "TRAFFIC_EXP_PAX", "TRAFFIC_EXP_CARGO", "TRAFFIC_EXP_OTH",
            "TRAFFIC_EXPENSE", "RES_EXP_PAX", "RES_EXP_CARGO", "RES_EXP_OTH",
            "RES_EXPENSE", "AD_EXP_PAX", "AD_EXP_CARGO", "AD_EXP_INST",
            "AD_EXPENSE", "ADMIN_EXPENSE", "DEPR_EXP_MAINT", "AMORTIZATION",
            "TRANSPORT_EXP", "TOTAL_OP_EXPENSE", "MAINT_PROP_EQUIP",
            "DEPR_PROP_EQUIP", "MAINT_DEPR", "SVC_SALES_OP_EXP",
        ),
        "int_cols": frozenset({
            "year", "quarter", "airline_id", "carrier_group", "carrier_group_new",
        }),
        "numeric_cols": frozenset({
            "air_op_expense", "fl_att_expense", "food_expense",
            "oth_in_fl_expense", "pax_svc_expense", "line_svc_expense",
            "control_expense", "landing_fees", "air_svc_expense",
            "traffic_exp_pax", "traffic_exp_cargo", "traffic_exp_oth",
            "traffic_expense", "res_exp_pax", "res_exp_cargo", "res_exp_oth",
            "res_expense", "ad_exp_pax", "ad_exp_cargo", "ad_exp_inst",
            "ad_expense", "admin_expense", "depr_exp_maint", "amortization",
            "transport_exp", "total_op_expense", "maint_prop_equip",
            "depr_prop_equip", "maint_depr", "svc_sales_op_exp",
        }),
    },
}

# CSV header → DB column name mapping (snake_case lower).
def _csv_to_db(header: str) -> str:
    return header.strip().lower()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("bts_form41_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Download (ASP.NET WebForms POST flow)
# --------------------------------------------------------------------------- #


_VS_RE = {
    name: re.compile(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name))
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
}


def _fetch_form(client: httpx.Client, gnoyr_vq: str) -> tuple[dict[str, str], list[int]]:
    """GET the form, return (viewstate dict, available years)."""
    url = f"{BASE_URL}?gnoyr_VQ={gnoyr_vq}&QO_fu146_anzr="
    r = client.get(url, timeout=30)
    r.raise_for_status()
    html = r.text
    vs = {k: m.search(html).group(1) for k, m in _VS_RE.items() if m.search(html)}
    if "__VIEWSTATE" not in vs:
        raise RuntimeError(f"viewstate missing from {url}")
    # Years from cboYear dropdown
    m = re.search(r'<select[^>]*name="cboYear"[^>]*>(.*?)</select>', html, re.S)
    years = []
    if m:
        years = sorted({int(v) for v in re.findall(r'value="(\d{4})"', m.group(1))})
    return vs, years


def _download_year(
    client: httpx.Client,
    gnoyr_vq: str,
    period: str,
    year: int,
    form_field_names: tuple[str, ...],
) -> tuple[bytes, dict[str, str]]:
    """POST the form for one (year, period) and return (csv_bytes, response_headers).

    Re-seeds __VIEWSTATE on every call (TranStats invalidates after one POST).
    """
    vs, _ = _fetch_form(client, gnoyr_vq)
    payload: list[tuple[str, str]] = [
        ("__EVENTTARGET", ""),
        ("__EVENTARGUMENT", ""),
        ("__VIEWSTATE", vs["__VIEWSTATE"]),
        ("__VIEWSTATEGENERATOR", vs["__VIEWSTATEGENERATOR"]),
        ("__EVENTVALIDATION", vs["__EVENTVALIDATION"]),
        ("cboYear", str(year)),
        ("cboPeriod", period),
        ("chkDownloadZip", "on"),
        ("chkAllVars", "on"),
        ("btnDownload", "Download"),
    ]
    payload.extend((f, "on") for f in form_field_names)

    url = f"{BASE_URL}?gnoyr_VQ={gnoyr_vq}&QO_fu146_anzr="
    r = client.post(
        url,
        data=urllib.parse.urlencode(payload),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": url,
        },
        timeout=120,
    )
    r.raise_for_status()
    if "application/zip" not in (r.headers.get("Content-Type") or ""):
        raise RuntimeError(
            f"non-zip response for {gnoyr_vq} year={year}: "
            f"ct={r.headers.get('Content-Type')!r} body[:200]={r.content[:200]!r}"
        )
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = [n for n in zf.namelist() if n.startswith("T_F41SCHEDULE_")]
        if not names:
            raise RuntimeError(f"no T_F41SCHEDULE_*.csv in zip for {gnoyr_vq} y={year}")
        csv_bytes = zf.read(names[0])
    return csv_bytes, dict(r.headers)


def _parse_observed_at(headers: dict[str, str]) -> datetime | None:
    lm = headers.get("Last-Modified") or headers.get("last-modified")
    if not lm:
        return None
    try:
        dt = parsedate_to_datetime(lm)
        return dt.astimezone(timezone.utc) if dt else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Row coercion
# --------------------------------------------------------------------------- #


def _coerce(raw: dict[str, str], int_cols: frozenset[str], numeric_cols: frozenset[str]) -> dict[str, Any]:
    """Map CSV header → db col, coerce types. Empty strings → None."""
    out: dict[str, Any] = {}
    for header, value in raw.items():
        col = _csv_to_db(header)
        v = (value or "").strip()
        if v == "":
            out[col] = None
        elif col in int_cols:
            try:
                out[col] = int(float(v))     # tolerate "10.0" style
            except (TypeError, ValueError):
                out[col] = None
        elif col in numeric_cols:
            try:
                out[col] = float(v)
            except (TypeError, ValueError):
                out[col] = None
        else:
            out[col] = v
    return out


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #


def _upsert(
    conn: psycopg.Connection,
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    raw_rows: list[dict[str, str]],
    *,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime | None,
    source_run_metadata: dict[str, Any],
    task_id: str | None,
    schedule_id: str | None,
) -> int:
    """INSERT…ON CONFLICT…DO UPDATE in batches of BATCH_SIZE. Returns rows upserted."""
    table = spec["table"]
    typed_cols = spec["typed_cols"]
    pk_cols = spec["pk_cols"]
    all_cols = (
        *typed_cols,
        "raw_source_row", "source_provider", "source_filename",
        "source_download_url", "source_observed_at", "source_run_metadata",
        "source_task_id", "source_schedule_id",
    )
    placeholders = ",".join(["%s"] * len(all_cols))
    update_cols = [c for c in typed_cols if c not in pk_cols] + [
        "raw_source_row", "source_filename", "source_download_url",
        "source_observed_at", "source_run_metadata",
        "source_task_id", "source_schedule_id",
        # ingested_at intentionally NOT updated — preserves first-seen audit.
    ]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    distinct_clause = " OR ".join(
        f"{table}.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in update_cols
    )
    sql = (
        f"INSERT INTO {table} ({','.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({','.join(pk_cols)}) DO UPDATE SET {set_clause} "
        f"WHERE {distinct_clause}"
    )

    upserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            chunk = rows[i:i + BATCH_SIZE]
            chunk_raw = raw_rows[i:i + BATCH_SIZE]
            params = []
            for row, raw in zip(chunk, chunk_raw):
                p = [row.get(c) for c in typed_cols]
                p.append(Jsonb(raw))
                p.append(PROVIDER)
                p.append(source_filename)
                p.append(source_download_url)
                p.append(source_observed_at)
                p.append(Jsonb(source_run_metadata))
                p.append(task_id)
                p.append(schedule_id)
                params.append(p)
            cur.executemany(sql, params)
            upserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return upserted


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def _start_run(
    conn: psycopg.Connection,
    schedules: list[str],
    years: tuple[int, int] | None,
    task_id: str | None,
    schedule_id: str | None,
) -> int:
    yrange = f"[{years[0]},{years[1] + 1})" if years else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.bts_form41_ingest_runs "
            "(status, schedules_requested, years_requested, task_id, schedule_id) "
            "VALUES ('running', %s, %s::int4range, %s, %s) RETURNING id",
            (schedules, yrange, task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    rows_seen: dict[str, int],
    rows_upserted: dict[str, int],
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.bts_form41_ingest_runs SET "
            "  status = %s, finished_at = now(), "
            "  rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, error_message = %s "
            "WHERE id = %s",
            (status, Jsonb(rows_seen), Jsonb(rows_upserted),
             source_observed_at, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Per-schedule pipeline
# --------------------------------------------------------------------------- #


def _ingest_schedule(
    conn: psycopg.Connection | None,
    client: httpx.Client,
    sched: str,
    years: tuple[int, int],
    *,
    dry_run: bool,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[int, int, datetime | None]:
    """Returns (rows_seen, rows_upserted, earliest_source_observed_at)."""
    spec = SCHEDULES[sched]
    _, available = _fetch_form(client, spec["gnoyr_vq"])
    log.info("[%s] available years on TranStats: %s", sched, available)
    target_years = [y for y in available if years[0] <= y <= years[1]]
    if not target_years:
        log.warning("[%s] no years matched range %s—skip", sched, years)
        return 0, 0, None

    total_seen = 0
    total_upserted = 0
    earliest_observed: datetime | None = None

    for year in target_years:
        log.info("[%s %d] downloading…", sched, year)
        csv_bytes, headers = _download_year(
            client, spec["gnoyr_vq"], spec["period"], year, spec["form_field_names"]
        )
        observed = _parse_observed_at(headers)
        if observed and (earliest_observed is None or observed < earliest_observed):
            earliest_observed = observed

        text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        raw_rows = list(reader)
        coerced = [
            _coerce(r, spec["int_cols"], spec["numeric_cols"])
            for r in raw_rows
        ]
        rows_in_year = len(raw_rows)
        total_seen += rows_in_year

        # Filter rows missing any PK column. Pre-1995 P-10 data has rows with
        # null airline_id / unique_carrier (e.g., 1990 Western Airlines 'WE',
        # entity L). Raw rows with no PK can't be upserted idempotently;
        # log + skip rather than fail the whole year.
        keep_pairs = [
            (rc, raw)
            for rc, raw in zip(coerced, raw_rows)
            if all(rc.get(c) is not None for c in spec["pk_cols"])
        ]
        skipped = rows_in_year - len(keep_pairs)
        if skipped:
            log.warning("[%s %d] skipping %d rows missing PK columns %s",
                        sched, year, skipped, spec["pk_cols"])
        coerced = [c for c, _ in keep_pairs]
        raw_rows = [r for _, r in keep_pairs]
        log.info("[%s %d] parsed %d rows (%d ingestable)",
                 sched, year, rows_in_year, len(coerced))

        if dry_run or conn is None:
            log.info("[%s %d] dry-run: skipping upsert", sched, year)
            continue

        filename = headers.get("Content-Disposition", "")
        if "filename=" in filename:
            filename = filename.split("filename=", 1)[1].strip().strip('"')
        else:
            filename = spec["csv_basename"]
        download_url = (
            f"{BASE_URL}?gnoyr_VQ={spec['gnoyr_vq']}"
            f"&cboYear={year}&cboPeriod={spec['period']}"
        )
        run_meta = {
            "year": year,
            "period": spec["period"],
            "csv_bytes": len(csv_bytes),
            "rows_in_csv": len(raw_rows),
            "tranStats_filename": filename,
        }
        upserted = _upsert(
            conn, spec, coerced, raw_rows,
            source_filename=filename,
            source_download_url=download_url,
            source_observed_at=observed,
            source_run_metadata=run_meta,
            task_id=task_id,
            schedule_id=schedule_id,
        )
        total_upserted += upserted
        log.info("[%s %d] upserted %d rows", sched, year, upserted)

    return total_seen, total_upserted, earliest_observed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_years(raw: str | None, default: tuple[int, int]) -> tuple[int, int]:
    if not raw:
        return default
    if "-" in raw:
        a, b = raw.split("-", 1)
        return int(a), int(b)
    y = int(raw)
    return y, y


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("schedule", choices=("p10", "p6", "p7", "all"))
    parser.add_argument("--years", help="YYYY or YYYY-YYYY (inclusive)")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse only, no DB writes")
    args = parser.parse_args()

    schedules = ("p10", "p6", "p7") if args.schedule == "all" else (args.schedule,)
    task_id = os.environ.get("TRIGGER_TASK_ID")
    schedule_id = os.environ.get("TRIGGER_SCHEDULE_ID")

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not args.dry_run and not db_url:
        log.error("DEX_DB_URL_DIRECT or DEX_DB_URL_POOLED must be set "
                  "(or pass --dry-run).")
        return 2

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        conn = psycopg.connect(db_url, autocommit=False)

    rows_seen: dict[str, int] = {}
    rows_upserted: dict[str, int] = {}
    earliest_observed: datetime | None = None
    run_id: int | None = None
    status = "success"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(
                conn, list(schedules),
                _parse_years(args.years, P10_DEFAULT_YEARS) if args.years else None,
                task_id, schedule_id,
            )

        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            for sched in schedules:
                yrs = _parse_years(args.years, SCHEDULES[sched]["default_years"])
                seen, upserted, obs = _ingest_schedule(
                    conn, client, sched, yrs,
                    dry_run=args.dry_run,
                    task_id=task_id, schedule_id=schedule_id,
                )
                rows_seen[sched] = seen
                rows_upserted[sched] = upserted
                if obs and (earliest_observed is None or obs < earliest_observed):
                    earliest_observed = obs

    except Exception as exc:
        status = "error"
        err = f"{type(exc).__name__}: {exc}"
        log.exception("ingest failed")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                log.exception("rollback failed during error cleanup")

    finally:
        if conn is not None and run_id is not None:
            _finish_run(conn, run_id, status, rows_seen, rows_upserted,
                        earliest_observed, err)
            conn.close()

    log.info("done. status=%s rows_seen=%s rows_upserted=%s",
             status, rows_seen, rows_upserted)
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
