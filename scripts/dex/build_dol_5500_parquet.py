#!/usr/bin/env python3
"""Stream DOL Form 5500 bulk CSVs into ZSTD-Parquet on Cloudflare R2.

Output layout in `dex-raw-landing-zone`:
    dol-5500/year=<YYYY>/table=<table_name>/part-NNNNN.parquet

The 4 tables landed by this script (2024 plan-year FOIA distribution):

    f_5500                   — F_5500 main form (large plans + DFEs).
                               Spine table; PK=ack_id; carries spons_dfe_ein
                               (the canonical employer EIN).
    f_sch_c_part1_item2      — F_SCH_C_PART1_ITEM2 service-provider rows
                               (provider name, EIN, service codes).
    f_sch_c_part1_item3      — F_SCH_C_PART1_ITEM3 direct-compensation rows
                               (direct comp amount per service provider).
    f_sch_h                  — F_SCH_H plan financial info (total assets,
                               net assets, total income).

Source URL pattern:
    https://askebsa.dol.gov/FOIA Files/<year>/Latest/<file_prefix>_<year>_Latest.zip

The 4 zips are independent. Each zip contains one CSV with a stable name
prefix (matches across re-stamp dates).

Identity-key contract: every Parquet row carries `ack_id` (string), the
natural PK that joins F_5500 ↔ Sch C ↔ Sch H. F_5500 also exposes
`spons_dfe_ein` (9-digit string, normalized from the source). Sch C
provider rows carry composite `(ack_id, row_order)`.

Usage (under uv-managed venv with httpx, pyarrow, boto3):

    doppler run -p hq-all -c prd -- \\
        uv run --project apps/data-engine-x \\
        python apps/data-engine-x/scripts/build_dol_5500_parquet.py \\
        --all --year 2024

    # Multi-year backfill (2009-2023, modern URL pattern era), idempotent
    # against R2 — re-running with --skip-if-exists is a no-op for years
    # that already landed:
    doppler run -p hq-all -c prd -- \\
        uv run --project apps/data-engine-x \\
        python apps/data-engine-x/scripts/build_dol_5500_parquet.py \\
        --all --years 2009-2023 --skip-if-exists

    # Smoke test (Sch C P1I3 only — small CSV):
    doppler run -p hq-all -c prd -- \\
        uv run --project apps/data-engine-x \\
        python apps/data-engine-x/scripts/build_dol_5500_parquet.py \\
        --tables f_sch_c_part1_item3 --year 2024

    # Local-only build (skip R2 upload):
    python apps/data-engine-x/scripts/build_dol_5500_parquet.py \\
        --all --year 2024 --no-upload
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
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import boto3
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_dol_5500_parquet")

# ---------------------------------------------------------------------------
# Source: EBSA FOIA distribution
# ---------------------------------------------------------------------------

EBSA_BASE = "https://askebsa.dol.gov"
USER_AGENT = "data-engine-x ingest (operator: tools@substrate.build)"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "dol-5500"

CHUNK_ROWS = 50_000

# Each table = one zip with one CSV. Prefix matches the CSV filename inside
# the zip; EBSA stamps a date suffix that drifts across refresh runs, so we
# match on the stable prefix.
TABLES: dict[str, dict[str, str]] = {
    "f_5500": {
        "file_prefix": "F_5500",
        "csv_prefix":  "F_5500_",
    },
    "f_sch_c_part1_item2": {
        "file_prefix": "F_SCH_C_PART1_ITEM2",
        "csv_prefix":  "F_SCH_C_PART1_ITEM2_",
    },
    "f_sch_c_part1_item3": {
        "file_prefix": "F_SCH_C_PART1_ITEM3",
        "csv_prefix":  "F_SCH_C_PART1_ITEM3_",
    },
    "f_sch_h": {
        "file_prefix": "F_SCH_H",
        "csv_prefix":  "F_SCH_H_",
    },
}
ALL_TABLES = list(TABLES.keys())


def url_for(file_prefix: str, year: int) -> str:
    path = f"/FOIA Files/{year}/Latest/{file_prefix}_{year}_Latest.zip"
    return EBSA_BASE + urllib.parse.quote(path, safe="/")


# ---------------------------------------------------------------------------
# Type-coercion helpers
# ---------------------------------------------------------------------------


def _strip(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    return s if s else None


def _normalize_ein(s: str | None) -> str | None:
    """Strip non-digits, left-pad to 9 chars. Return None if no digits."""
    s = _strip(s)
    if s is None:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(9)[-9:]


def _row_key_lookup(row: dict, *names: str) -> str | None:
    """Source CSVs use ALL_CAPS_SNAKE; some scripts pre-lowercased. Try both."""
    for n in names:
        if n in row:
            v = _strip(row[n])
            if v is not None:
                return v
        upper = n.upper()
        if upper in row:
            v = _strip(row[upper])
            if v is not None:
                return v
        lower = n.lower()
        if lower in row:
            v = _strip(row[lower])
            if v is not None:
                return v
    return None


# ---------------------------------------------------------------------------
# Per-table schemas + row transforms
#
# Strategy: declare a curated typed schema that includes the columns needed
# by mv_employer_stability_signals plus the join keys. ALL OTHER columns go
# into raw_json so the parquet stays lossless. This mirrors build_sec_adv.
# ---------------------------------------------------------------------------


# ── f_5500 ─────────────────────────────────────────────────────────────────
F_5500_SCHEMA = pa.schema(
    [
        ("ack_id", pa.string()),
        ("spons_dfe_ein", pa.string()),  # 9-digit normalized
        ("plan_name", pa.string()),
        ("sponsor_dfe_name", pa.string()),
        ("spons_dfe_dba_name", pa.string()),
        ("business_code", pa.string()),
        ("form_plan_year_begin_date", pa.string()),
        ("form_tax_prd", pa.string()),
        ("plan_eff_date", pa.string()),
        ("date_received", pa.string()),
        ("spons_dfe_pn", pa.string()),
        ("tot_partcp_boy_cnt", pa.string()),
        ("tot_active_partcp_cnt", pa.string()),
        ("sch_h_attached_ind", pa.string()),
        ("sch_c_attached_ind", pa.string()),
        ("dataset_year", pa.int32()),
        ("raw_json", pa.string()),
    ]
)


def _t_f_5500(row: dict, year: int) -> dict:
    return {
        "ack_id":                    _row_key_lookup(row, "ACK_ID"),
        "spons_dfe_ein":             _normalize_ein(_row_key_lookup(row, "SPONS_DFE_EIN")),
        "plan_name":                 _row_key_lookup(row, "PLAN_NAME"),
        "sponsor_dfe_name":          _row_key_lookup(row, "SPONSOR_DFE_NAME"),
        "spons_dfe_dba_name":        _row_key_lookup(row, "SPONS_DFE_DBA_NAME"),
        "business_code":             _row_key_lookup(row, "BUSINESS_CODE"),
        "form_plan_year_begin_date": _row_key_lookup(row, "FORM_PLAN_YEAR_BEGIN_DATE"),
        "form_tax_prd":              _row_key_lookup(row, "FORM_TAX_PRD"),
        "plan_eff_date":             _row_key_lookup(row, "PLAN_EFF_DATE"),
        "date_received":             _row_key_lookup(row, "DATE_RECEIVED"),
        "spons_dfe_pn":              _row_key_lookup(row, "SPONS_DFE_PN"),
        "tot_partcp_boy_cnt":        _row_key_lookup(row, "TOT_PARTCP_BOY_CNT"),
        "tot_active_partcp_cnt":     _row_key_lookup(row, "TOT_ACTIVE_PARTCP_CNT"),
        "sch_h_attached_ind":        _row_key_lookup(row, "SCH_H_ATTACHED_IND"),
        "sch_c_attached_ind":        _row_key_lookup(row, "SCH_C_ATTACHED_IND"),
        "dataset_year":              year,
        "raw_json":                  json.dumps(row, separators=(",", ":")),
    }


# ── f_sch_c_part1_item2 (service providers receiving direct or eligible
#    indirect compensation; carries the DIRECT comp amount) ────────────────
F_SCH_C_P1I2_SCHEMA = pa.schema(
    [
        ("ack_id", pa.string()),
        ("row_order", pa.string()),
        ("provider_other_name", pa.string()),
        ("provider_other_ein", pa.string()),  # 9-digit normalized
        ("provider_other_relation", pa.string()),
        ("provider_other_srvc_codes", pa.string()),
        ("provider_other_direct_comp_amt", pa.string()),  # cast in MV
        ("prov_other_indirect_comp_ind", pa.string()),
        ("prov_other_elig_ind_comp_ind", pa.string()),
        ("prov_other_tot_ind_comp_amt", pa.string()),  # cast in MV
        ("provider_other_amt_formula_ind", pa.string()),
        ("dataset_year", pa.int32()),
        ("raw_json", pa.string()),
    ]
)


def _t_f_sch_c_p1i2(row: dict, year: int) -> dict:
    return {
        "ack_id":                         _row_key_lookup(row, "ACK_ID"),
        "row_order":                      _row_key_lookup(row, "ROW_ORDER"),
        "provider_other_name":            _row_key_lookup(row, "PROVIDER_OTHER_NAME"),
        "provider_other_ein":             _normalize_ein(_row_key_lookup(row, "PROVIDER_OTHER_EIN")),
        "provider_other_relation":        _row_key_lookup(row, "PROVIDER_OTHER_RELATION"),
        "provider_other_srvc_codes":      _row_key_lookup(row, "PROVIDER_OTHER_SRVC_CODES"),
        "provider_other_direct_comp_amt": _row_key_lookup(row, "PROVIDER_OTHER_DIRECT_COMP_AMT"),
        "prov_other_indirect_comp_ind":   _row_key_lookup(row, "PROV_OTHER_INDIRECT_COMP_IND"),
        "prov_other_elig_ind_comp_ind":   _row_key_lookup(row, "PROV_OTHER_ELIG_IND_COMP_IND"),
        "prov_other_tot_ind_comp_amt":    _row_key_lookup(row, "PROV_OTHER_TOT_IND_COMP_AMT"),
        "provider_other_amt_formula_ind": _row_key_lookup(row, "PROVIDER_OTHER_AMT_FORMULA_IND"),
        "dataset_year":                   year,
        "raw_json":                       json.dumps(row, separators=(",", ":")),
    }


# ── f_sch_c_part1_item3 (indirect-compensation rows with named payor) ──────
# Each row is one "provider receives indirect comp from payor" record. The
# provider gets PROVIDER_INDIRECT_*, the entity that paid the comp gets
# PROVIDER_PAYOR_*. Note this is SEPARATE from the direct-comp amount on
# Part 1 Item 2 — Item 2's TOT_IND_COMP_AMT is the aggregate-by-provider,
# Item 3 itemizes by payor.
F_SCH_C_P1I3_SCHEMA = pa.schema(
    [
        ("ack_id", pa.string()),
        ("row_order", pa.string()),
        ("provider_indirect_name", pa.string()),
        ("provider_indirect_srvc_codes", pa.string()),
        ("provider_indirect_comp_amt", pa.string()),  # cast in MV
        ("provider_payor_name", pa.string()),
        ("provider_payor_ein", pa.string()),  # 9-digit normalized
        ("provider_comp_explain_text", pa.string()),
        ("dataset_year", pa.int32()),
        ("raw_json", pa.string()),
    ]
)


def _t_f_sch_c_p1i3(row: dict, year: int) -> dict:
    return {
        "ack_id":                       _row_key_lookup(row, "ACK_ID"),
        "row_order":                    _row_key_lookup(row, "ROW_ORDER"),
        "provider_indirect_name":       _row_key_lookup(row, "PROVIDER_INDIRECT_NAME"),
        "provider_indirect_srvc_codes": _row_key_lookup(row, "PROVIDER_INDIRECT_SRVC_CODES"),
        "provider_indirect_comp_amt":   _row_key_lookup(row, "PROVIDER_INDIRECT_COMP_AMT"),
        "provider_payor_name":          _row_key_lookup(row, "PROVIDER_PAYOR_NAME"),
        "provider_payor_ein":           _normalize_ein(_row_key_lookup(row, "PROVIDER_PAYOR_EIN")),
        "provider_comp_explain_text":   _row_key_lookup(row, "PROVIDER_COMP_EXPLAIN_TEXT"),
        "dataset_year":                 year,
        "raw_json":                     json.dumps(row, separators=(",", ":")),
    }


# ── f_sch_h (plan financial info) ──────────────────────────────────────────
# Per EBSA 2024 F_SCH_H schema: financial columns carry _BOY_AMT / _EOY_AMT
# (balance-sheet items) or _AMT (income-statement items, single year).
# Stored as strings in Parquet; RisingWave MV does CAST(... AS DECIMAL).
# SCH_H_EIN is captured as a backup join key (some Sch H rows publish EIN
# even when the F_5500 spine has gaps).
F_SCH_H_SCHEMA = pa.schema(
    [
        ("ack_id", pa.string()),
        ("sch_h_ein", pa.string()),  # 9-digit normalized
        ("sch_h_pn", pa.string()),
        ("sch_h_plan_year_begin_date", pa.string()),
        ("sch_h_tax_prd", pa.string()),
        ("tot_assets_boy_amt", pa.string()),
        ("tot_assets_eoy_amt", pa.string()),
        ("tot_liabilities_boy_amt", pa.string()),
        ("tot_liabilities_eoy_amt", pa.string()),
        ("net_assets_boy_amt", pa.string()),
        ("net_assets_eoy_amt", pa.string()),
        ("tot_income_amt", pa.string()),
        ("tot_expenses_amt", pa.string()),
        ("net_income_amt", pa.string()),
        ("tot_contrib_amt", pa.string()),
        ("dataset_year", pa.int32()),
        ("raw_json", pa.string()),
    ]
)


def _t_f_sch_h(row: dict, year: int) -> dict:
    return {
        "ack_id":                     _row_key_lookup(row, "ACK_ID"),
        "sch_h_ein":                  _normalize_ein(_row_key_lookup(row, "SCH_H_EIN")),
        "sch_h_pn":                   _row_key_lookup(row, "SCH_H_PN"),
        "sch_h_plan_year_begin_date": _row_key_lookup(row, "SCH_H_PLAN_YEAR_BEGIN_DATE"),
        "sch_h_tax_prd":              _row_key_lookup(row, "SCH_H_TAX_PRD"),
        "tot_assets_boy_amt":         _row_key_lookup(row, "TOT_ASSETS_BOY_AMT"),
        "tot_assets_eoy_amt":         _row_key_lookup(row, "TOT_ASSETS_EOY_AMT"),
        "tot_liabilities_boy_amt":    _row_key_lookup(row, "TOT_LIABILITIES_BOY_AMT"),
        "tot_liabilities_eoy_amt":    _row_key_lookup(row, "TOT_LIABILITIES_EOY_AMT"),
        "net_assets_boy_amt":         _row_key_lookup(row, "NET_ASSETS_BOY_AMT"),
        "net_assets_eoy_amt":         _row_key_lookup(row, "NET_ASSETS_EOY_AMT"),
        "tot_income_amt":             _row_key_lookup(row, "TOT_INCOME_AMT"),
        "tot_expenses_amt":           _row_key_lookup(row, "TOT_EXPENSES_AMT"),
        "net_income_amt":             _row_key_lookup(row, "NET_INCOME_AMT"),
        "tot_contrib_amt":            _row_key_lookup(row, "TOT_CONTRIB_AMT"),
        "dataset_year":               year,
        "raw_json":                   json.dumps(row, separators=(",", ":")),
    }


@dataclass
class TableSpec:
    name: str
    file_prefix: str
    csv_prefix: str
    schema: pa.Schema
    transform: callable


TABLE_SPECS: dict[str, TableSpec] = {
    "f_5500": TableSpec(
        name="f_5500",
        file_prefix="F_5500",
        csv_prefix="F_5500_",
        schema=F_5500_SCHEMA,
        transform=_t_f_5500,
    ),
    "f_sch_c_part1_item2": TableSpec(
        name="f_sch_c_part1_item2",
        file_prefix="F_SCH_C_PART1_ITEM2",
        csv_prefix="F_SCH_C_PART1_ITEM2_",
        schema=F_SCH_C_P1I2_SCHEMA,
        transform=_t_f_sch_c_p1i2,
    ),
    "f_sch_c_part1_item3": TableSpec(
        name="f_sch_c_part1_item3",
        file_prefix="F_SCH_C_PART1_ITEM3",
        csv_prefix="F_SCH_C_PART1_ITEM3_",
        schema=F_SCH_C_P1I3_SCHEMA,
        transform=_t_f_sch_c_p1i3,
    ),
    "f_sch_h": TableSpec(
        name="f_sch_h",
        file_prefix="F_SCH_H",
        csv_prefix="F_SCH_H_",
        schema=F_SCH_H_SCHEMA,
        transform=_t_f_sch_h,
    ),
}


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def stream_download(url: str, dest: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    bytes_total = 0
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    next_log = 50 * 1024 * 1024
    with httpx.stream(
        "GET", url, headers=headers, timeout=600.0, follow_redirects=True
    ) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                sha.update(chunk)
                bytes_total += len(chunk)
                if bytes_total >= next_log:
                    logger.info(
                        "download_progress mb=%d url=%s",
                        bytes_total // (1 << 20),
                        url,
                    )
                    next_log += 50 * 1024 * 1024
    return bytes_total, sha.hexdigest()


def ensure_zip(url: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    p = cache_dir / name
    if p.exists() and p.stat().st_size > 1024:
        logger.info("zip_cached path=%s bytes=%d", p, p.stat().st_size)
        return p
    logger.info("zip_download url=%s -> %s", url, p)
    n, sha = stream_download(url, p)
    logger.info("zip_downloaded bytes=%d sha256=%s", n, sha[:16])
    return p


def find_csv_in_zip(zip_path: Path, prefix: str) -> str | None:
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            base = Path(member).name
            if base.upper().startswith(prefix.upper()) and base.lower().endswith(".csv"):
                return member
    return None


def iter_csv_rows(zip_path: Path, csv_member: str) -> Iterator[dict]:
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(csv_member) as raw:
            txt = io.TextIOWrapper(raw, encoding="latin-1", newline="")
            yield from csv.DictReader(txt)


# ---------------------------------------------------------------------------
# Per-table writer
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    table: str
    rows_in: int = 0
    rows_out: int = 0
    parts: int = 0
    bytes_written: int = 0
    elapsed_seconds: float = 0.0
    r2_keys: list[str] = field(default_factory=list)


def write_table(
    spec: TableSpec,
    *,
    zip_path: Path,
    out_dir: Path,
    s3,
    year: int,
    upload_r2: bool,
    rows_per_part: int,
) -> WriteResult:
    member = find_csv_in_zip(zip_path, spec.csv_prefix)
    if member is None:
        raise RuntimeError(
            f"CSV with prefix {spec.csv_prefix!r} not found in {zip_path.name}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    result = WriteResult(table=spec.name)
    t0 = time.monotonic()

    buf: list[dict] = []
    part_idx = 0

    def flush() -> None:
        nonlocal part_idx
        if not buf:
            return
        cols: dict[str, list] = {f.name: [] for f in spec.schema}
        for r in buf:
            for f in spec.schema:
                cols[f.name].append(r.get(f.name))
        tbl = pa.table(cols, schema=spec.schema)
        local_path = out_dir / f"part-{part_idx:05d}.parquet"
        pq.write_table(
            tbl, local_path, compression="zstd", compression_level=9
        )
        size = local_path.stat().st_size
        r2_key = f"{R2_PREFIX}/year={year}/table={spec.name}/part-{part_idx:05d}.parquet"
        if upload_r2:
            s3.upload_file(str(local_path), R2_BUCKET, r2_key)
            local_path.unlink(missing_ok=True)
        else:
            r2_key = f"(local) {local_path}"
        result.parts += 1
        result.bytes_written += size
        result.r2_keys.append(r2_key)
        logger.info(
            "part_written table=%s part=%d rows=%d bytes=%d r2_key=%s",
            spec.name, part_idx, len(buf), size, r2_key,
        )
        part_idx += 1
        buf.clear()

    for row in iter_csv_rows(zip_path, member):
        result.rows_in += 1
        try:
            transformed = spec.transform(row, year)
        except Exception as exc:
            logger.warning(
                "row_transform_error table=%s row_in=%d err=%s",
                spec.name, result.rows_in, exc,
            )
            continue
        if transformed.get("ack_id") is None:
            continue
        buf.append(transformed)
        result.rows_out += 1
        if len(buf) >= rows_per_part:
            flush()
        if result.rows_in % 250_000 == 0:
            logger.info(
                "read_progress table=%s rows_in=%d rows_out=%d",
                spec.name, result.rows_in, result.rows_out,
            )

    flush()
    result.elapsed_seconds = time.monotonic() - t0
    logger.info(
        "table_done table=%s rows_in=%d rows_out=%d parts=%d bytes=%d elapsed=%.1fs",
        spec.name, result.rows_in, result.rows_out, result.parts,
        result.bytes_written, result.elapsed_seconds,
    )
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def make_s3_client():
    endpoint = os.environ["R2_ENDPOINT"]
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def r2_table_has_objects(s3, *, year: int, table: str) -> bool:
    """Return True if any Parquet objects already exist for (year, table)."""
    if s3 is None:
        return False
    prefix = f"{R2_PREFIX}/year={year}/table={table}/"
    resp = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=prefix, MaxKeys=1)
    return bool(resp.get("Contents"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--all", action="store_true", help="Run all 4 tables.")
    grp.add_argument("--tables", help="Comma-separated subset of table names.")
    yr = ap.add_mutually_exclusive_group()
    yr.add_argument("--year", type=int,
                    help="Single plan year (default 2024 if no range given).")
    yr.add_argument("--years", metavar="START-END",
                    help="Year range, inclusive. Example: --years 2009-2023.")
    ap.add_argument("--cache-dir", type=Path, default=Path("/tmp/dol_5500_zips"))
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/dol_5500_parquet"))
    ap.add_argument("--no-upload", action="store_true",
                    help="Write Parquet locally only, skip R2 upload.")
    ap.add_argument("--skip-if-exists", action="store_true",
                    help="Skip (year, table) pairs that already have R2 objects.")
    ap.add_argument("--rows-per-part", type=int, default=CHUNK_ROWS)
    args = ap.parse_args()

    if args.all:
        tables = ALL_TABLES
    else:
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
        unknown = [t for t in tables if t not in TABLE_SPECS]
        if unknown:
            ap.error(f"unknown table(s): {unknown}; valid={ALL_TABLES}")

    if args.years:
        try:
            start_str, end_str = args.years.split("-", 1)
            start_year, end_year = int(start_str), int(end_str)
        except ValueError:
            ap.error(f"--years must be START-END, got {args.years!r}")
        if start_year > end_year:
            ap.error(f"--years START must be <= END, got {start_year}-{end_year}")
        years = list(range(start_year, end_year + 1))
    else:
        years = [args.year if args.year is not None else 2024]

    upload_r2 = not args.no_upload
    s3 = make_s3_client() if upload_r2 else None

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    per_year: list[dict] = []
    run_started = datetime.now(timezone.utc)

    for year in years:
        year_started = datetime.now(timezone.utc)
        logger.info("year_start year=%d tables=%s", year, tables)
        results: list[WriteResult] = []
        skipped: list[str] = []

        for table_name in tables:
            spec = TABLE_SPECS[table_name]
            if args.skip_if_exists and r2_table_has_objects(
                s3, year=year, table=table_name
            ):
                logger.info(
                    "skip_existing year=%d table=%s "
                    "(R2 objects already present)",
                    year, table_name,
                )
                skipped.append(table_name)
                continue
            zip_url = url_for(spec.file_prefix, year)
            try:
                zip_path = ensure_zip(zip_url, args.cache_dir)
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "zip_unavailable year=%d table=%s url=%s status=%s — skipping",
                    year, table_name, zip_url, exc.response.status_code,
                )
                skipped.append(f"{table_name}(http_{exc.response.status_code})")
                continue
            out_sub = args.out_dir / f"year={year}" / table_name
            result = write_table(
                spec,
                zip_path=zip_path,
                out_dir=out_sub,
                s3=s3,
                year=year,
                upload_r2=upload_r2,
                rows_per_part=args.rows_per_part,
            )
            results.append(result)

        year_total_rows = sum(r.rows_out for r in results)
        year_total_bytes = sum(r.bytes_written for r in results)
        year_elapsed = (
            datetime.now(timezone.utc) - year_started
        ).total_seconds()
        logger.info(
            "year_done year=%d tables_written=%d skipped=%d "
            "total_rows_out=%d total_bytes=%d elapsed=%.1fs",
            year, len(results), len(skipped),
            year_total_rows, year_total_bytes, year_elapsed,
        )
        per_year.append({
            "year": year,
            "tables": [
                {
                    "table": r.table,
                    "rows_in": r.rows_in,
                    "rows_out": r.rows_out,
                    "parts": r.parts,
                    "bytes": r.bytes_written,
                    "elapsed_seconds": round(r.elapsed_seconds, 2),
                    "r2_keys": r.r2_keys[:3] + (
                        [f"... ({len(r.r2_keys)-3} more)"]
                        if len(r.r2_keys) > 3 else []
                    ),
                }
                for r in results
            ],
            "skipped_tables": skipped,
            "elapsed_seconds": round(year_elapsed, 2),
        })

    total_rows = sum(t["rows_out"] for y in per_year for t in y["tables"])
    total_bytes = sum(t["bytes"] for y in per_year for t in y["tables"])
    logger.info(
        "BUILD COMPLETE years=%d total_rows_out=%d total_bytes=%d",
        len(per_year), total_rows, total_bytes,
    )
    summary = {
        "started_at": run_started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "years": [y["year"] for y in per_year],
        "uploaded_to_r2": upload_r2,
        "bucket": R2_BUCKET if upload_r2 else None,
        "skip_if_exists": args.skip_if_exists,
        "per_year": per_year,
        "totals": {
            "rows_out": total_rows,
            "bytes": total_bytes,
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
