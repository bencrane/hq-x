#!/usr/bin/env python3
"""Stream SEC Form ADV bulk CSVs into ZSTD-Parquet on Cloudflare R2.

Output layout in `dex-raw-landing-zone`:
    sec-adv/year=<YYYY>/table=<table_name>/part-NNNNN.parquet

The 9 tables landed by this script:

    base_a                    — IA_ADV_Base_A   (the spine; CRD at column "1E1")
    schedule_a_b              — IA_Schedule_A_B (direct + indirect owners)
    schedule_d_1b             — IA_Schedule_D_1B (other business names)
    schedule_d_1f             — IA_Schedule_D_1F (branch offices)
    schedule_d_1i             — IA_Schedule_D_1I (websites)
    filing_types              — ADV_Filing_Types (filing-type lookup)
    advw_main                 — ADVW            (withdrawn filings — separate ZIP)
    advw_w1_5_locations       — ADVW_W1_5
    advw_w1_8_custodians      — ADVW_W1_8

Usage:
    doppler run -p hq-all -c prd -- /tmp/sec_adv_venv/bin/python \\
        apps/data-engine-x/scripts/build_sec_adv_parquet.py --all --year 2024

    # smoke test against ADV-W only (smallest):
    doppler run -p hq-all -c prd -- /tmp/sec_adv_venv/bin/python \\
        apps/data-engine-x/scripts/build_sec_adv_parquet.py --tables advw_main,advw_w1_5_locations,advw_w1_8_custodians --year 2024

Identity-key contract: every Parquet table includes `filing_id` (int64),
`crd_number` (int64), and (Part-1 only) `sec_number` (string). Part-1
non-base_a tables look up CRD/SEC via a filing_id→(crd, sec) map built
from base_a's "1E1" / "1D" columns. ADV-W tables carry CRD natively.
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
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import boto3
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_sec_adv_parquet")

# ---------------------------------------------------------------------------
# SEC source URLs + CSV-name patterns
# ---------------------------------------------------------------------------

PART1_URL = "https://www.sec.gov/files/adv-filing-data-20111105-20241231-part1.zip"
ADVW_URL = "https://www.sec.gov/files/advw-20001019-20241231.zip"

# CSV filename prefixes inside each ZIP. Match by stable prefix (not full
# filename) since SEC re-stamps date suffixes when refreshing the ZIPs.
PART1_CSV_PREFIXES = {
    "base_a":        "IA_ADV_Base_A_",
    "schedule_a_b":  "IA_Schedule_A_B_",
    "schedule_d_1b": "IA_Schedule_D_1B_",
    "schedule_d_1f": "IA_Schedule_D_1F_",
    "schedule_d_1i": "IA_Schedule_D_1I_",
    "filing_types":  "ADV_Filing_Types_",
}

ADVW_CSV_PREFIXES = {
    "advw_main":              "ADVW_2",   # ADVW_20001019_20241231.csv
    "advw_w1_5_locations":    "ADVW_W1_5_",
    "advw_w1_8_custodians":   "ADVW_W1_8_",
}

ALL_TABLES = list(PART1_CSV_PREFIXES.keys()) + list(ADVW_CSV_PREFIXES.keys())

USER_AGENT = "data-engine-x ingest (operator: benjaminjcrane@gmail.com)"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "sec-adv"

CHUNK_ROWS = 50_000

# ---------------------------------------------------------------------------
# Type-coercion helpers
# ---------------------------------------------------------------------------


def _strip(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s).strip().strip('"')
    return s if s else None


def parse_int(s: str | None) -> int | None:
    s = _strip(s)
    if s is None:
        return None
    s = s.replace(",", "")
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def parse_iso_date(s: str | None) -> datetime | None:
    s = _strip(s)
    if s is None:
        return None
    formats = (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%Y",
        "%Y",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse_yn(s: str | None) -> bool | None:
    s = _strip(s)
    if s is None:
        return None
    v = s.upper()
    if v == "Y":
        return True
    if v == "N":
        return False
    return None


def first_present(row: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = _strip(row.get(k))
        if v is not None:
            return v
    return None


# 1E1 first — 1D holds the SEC file number ("801-12345"), not a CRD; including
# it would lose CRD on rows where 1E1 is non-numeric or missing.
CRD_KEYS = ("1E1", "CRDNumber", "CRD Number", "CRD")
SEC_NUM_KEYS = ("1D", "SECNumber", "SEC Number", "SEC#", "SEC File Number")
LEGAL_NAME_KEYS = ("1A", "Legal Name", "Full Legal Name", "FullLegalName")
PRIMARY_BIZ_NAME_KEYS = ("1B1", "Primary Business Name", "BusinessName")
# Part 1 CSVs use "FilingID" (no space); ADV-W CSVs use "Filing ID" (with space).
FILING_ID_KEYS = ("FilingID", "Filing ID")
FORM_VERSION_KEYS = ("FormVersion", "Form Version")
DATE_SUBMITTED_KEYS = ("DateSubmitted", "Date Submitted", "Filing Date", "FilingDate")


# ---------------------------------------------------------------------------
# Per-table schema + row transformer
# ---------------------------------------------------------------------------


@dataclass
class TableSpec:
    name: str
    csv_prefix: str
    source_zip: str            # PART1_URL or ADVW_URL
    schema: pa.Schema
    row_transform: Callable[[dict, dict[int, tuple[int | None, str | None]]], dict]


def _ts_to_pa(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)


# --- base_a ----------------------------------------------------------------


_BASE_A_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("sec_number", pa.string()),
        ("form_version", pa.string()),
        ("date_submitted", pa.timestamp("us", tz="UTC")),
        ("legal_name", pa.string()),
        ("primary_business_name", pa.string()),
        ("raw_json", pa.string()),
    ]
)


def _t_base_a(row: dict, _crd_map) -> dict:
    return {
        "filing_id": parse_int(first_present(row, FILING_ID_KEYS)),
        "crd_number": parse_int(first_present(row, CRD_KEYS)),
        "sec_number": first_present(row, SEC_NUM_KEYS),
        "form_version": first_present(row, FORM_VERSION_KEYS),
        "date_submitted": _ts_to_pa(parse_iso_date(first_present(row, DATE_SUBMITTED_KEYS))),
        "legal_name": first_present(row, LEGAL_NAME_KEYS),
        "primary_business_name": first_present(row, PRIMARY_BIZ_NAME_KEYS),
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# --- schedule_a_b ----------------------------------------------------------


_SCH_AB_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("sec_number", pa.string()),
        ("schedule", pa.string()),         # "A" or "B"
        ("scha_3", pa.string()),
        ("full_legal_name", pa.string()),
        ("entity_type", pa.string()),      # DE/FE/I
        ("entity_in_which", pa.string()),
        ("title_or_status", pa.string()),
        ("status_acquired", pa.string()),
        ("ownership_code", pa.string()),
        ("control_person", pa.bool_()),
        ("pr", pa.bool_()),
        ("owner_id", pa.string()),
        ("raw_json", pa.string()),
    ]
)


def _t_schedule_a_b(row: dict, crd_map) -> dict:
    fid = parse_int(first_present(row, FILING_ID_KEYS))
    crd, sec = crd_map.get(fid, (None, None)) if fid is not None else (None, None)
    return {
        "filing_id": fid,
        "crd_number": crd,
        "sec_number": sec,
        "schedule": _strip(row.get("Schedule")),
        "scha_3": _strip(row.get("SchA-3")),
        "full_legal_name": _strip(row.get("Full Legal Name")),
        "entity_type": _strip(row.get("DE/FE/I")),
        "entity_in_which": _strip(row.get("Entity in Which")),
        "title_or_status": _strip(row.get("Title or Status")),
        "status_acquired": _strip(row.get("Status Acquired")),
        "ownership_code": _strip(row.get("Ownership Code")),
        "control_person": parse_yn(row.get("Control Person")),
        "pr": parse_yn(row.get("PR")),
        "owner_id": _strip(row.get("OwnerID")),
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# --- schedule_d_1b ---------------------------------------------------------

_SCH_D1B_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("sec_number", pa.string()),
        ("other_business_name", pa.string()),
        ("states", pa.string()),
        ("raw_json", pa.string()),
    ]
)

_US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA GU HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA VI "
    "WA WV WI WY"
).split()


def _t_schedule_d_1b(row: dict, crd_map) -> dict:
    fid = parse_int(first_present(row, FILING_ID_KEYS))
    crd, sec = crd_map.get(fid, (None, None)) if fid is not None else (None, None)
    states = ",".join(s for s in _US_STATES if parse_yn(row.get(s)) is True) or None
    return {
        "filing_id": fid,
        "crd_number": crd,
        "sec_number": sec,
        "other_business_name": _strip(row.get("Other Business Name")),
        "states": states,
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# --- schedule_d_1f ---------------------------------------------------------


_SCH_D1F_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("sec_number", pa.string()),
        ("street_1", pa.string()),
        ("street_2", pa.string()),
        ("city", pa.string()),
        ("state", pa.string()),
        ("country", pa.string()),
        ("postal_code", pa.string()),
        ("private_residence", pa.bool_()),
        ("telephone", pa.string()),
        ("facsimile", pa.string()),
        ("branch_number", pa.string()),
        ("employees", pa.int32()),
        ("raw_json", pa.string()),
    ]
)


def _t_schedule_d_1f(row: dict, crd_map) -> dict:
    fid = parse_int(first_present(row, FILING_ID_KEYS))
    crd, sec = crd_map.get(fid, (None, None)) if fid is not None else (None, None)
    emp = parse_int(row.get("Employees"))
    return {
        "filing_id": fid,
        "crd_number": crd,
        "sec_number": sec,
        "street_1": _strip(row.get("Street 1")),
        "street_2": _strip(row.get("Street 2")),
        "city": _strip(row.get("City")),
        "state": _strip(row.get("State")),
        "country": _strip(row.get("Country")),
        "postal_code": _strip(row.get("Postal Code")),
        "private_residence": parse_yn(row.get("Private Residence")),
        "telephone": _strip(row.get("Telephone Number")),
        "facsimile": _strip(row.get("Facsimile Number")),
        "branch_number": _strip(row.get("Branch Number")),
        "employees": emp if emp is not None and -2_000_000_000 <= emp <= 2_000_000_000 else None,
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# --- schedule_d_1i ---------------------------------------------------------


_SCH_D1I_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("sec_number", pa.string()),
        ("website", pa.string()),
    ]
)


def _t_schedule_d_1i(row: dict, crd_map) -> dict:
    fid = parse_int(first_present(row, FILING_ID_KEYS))
    crd, sec = crd_map.get(fid, (None, None)) if fid is not None else (None, None)
    return {
        "filing_id": fid,
        "crd_number": crd,
        "sec_number": sec,
        "website": _strip(row.get("Website")),
    }


# --- filing_types ----------------------------------------------------------


_FILING_TYPES_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("sec_number", pa.string()),
        ("filing_type", pa.string()),
        ("raw_json", pa.string()),
    ]
)


def _t_filing_types(row: dict, crd_map) -> dict:
    fid = parse_int(first_present(row, FILING_ID_KEYS))
    crd, sec = crd_map.get(fid, (None, None)) if fid is not None else (None, None)
    filing_type = first_present(row, ("Filing Type", "FilingType", "Form Type", "FormType"))
    return {
        "filing_id": fid,
        "crd_number": crd,
        "sec_number": sec,
        "filing_type": filing_type,
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# --- advw_main -------------------------------------------------------------


_ADVW_MAIN_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("sec_number", pa.string()),
        ("primary_business_name", pa.string()),
        ("full_legal_name", pa.string()),
        ("filing_date", pa.timestamp("us", tz="UTC")),
        ("form_version", pa.string()),
        ("raw_json", pa.string()),
    ]
)


def _t_advw_main(row: dict, _crd_map) -> dict:
    return {
        "filing_id": parse_int(first_present(row, FILING_ID_KEYS)),
        "crd_number": parse_int(first_present(row, CRD_KEYS)),
        "sec_number": first_present(row, SEC_NUM_KEYS),
        "primary_business_name": first_present(row, PRIMARY_BIZ_NAME_KEYS),
        "full_legal_name": first_present(row, LEGAL_NAME_KEYS),
        "filing_date": _ts_to_pa(parse_iso_date(first_present(row, DATE_SUBMITTED_KEYS))),
        "form_version": first_present(row, FORM_VERSION_KEYS),
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# --- advw_w1_5_locations ---------------------------------------------------


_ADVW_W15_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("street_1", pa.string()),
        ("street_2", pa.string()),
        ("city", pa.string()),
        ("state", pa.string()),
        ("country", pa.string()),
        ("postal_code", pa.string()),
        ("raw_json", pa.string()),
    ]
)


def _t_advw_w1_5(row: dict, _crd_map) -> dict:
    return {
        "filing_id": parse_int(first_present(row, FILING_ID_KEYS)),
        "crd_number": parse_int(first_present(row, CRD_KEYS)),
        "street_1": _strip(row.get("Street 1")),
        "street_2": _strip(row.get("Street 2")),
        "city": _strip(row.get("City")),
        "state": _strip(row.get("State")),
        "country": _strip(row.get("Country")),
        "postal_code": _strip(row.get("Postal Code")),
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# --- advw_w1_8_custodians --------------------------------------------------


_ADVW_W18_SCHEMA = pa.schema(
    [
        ("filing_id", pa.int64()),
        ("crd_number", pa.int64()),
        ("custodian_name", pa.string()),
        ("custodian_city", pa.string()),
        ("custodian_state", pa.string()),
        ("custodian_country", pa.string()),
        ("raw_json", pa.string()),
    ]
)


def _t_advw_w1_8(row: dict, _crd_map) -> dict:
    return {
        "filing_id": parse_int(first_present(row, FILING_ID_KEYS)),
        "crd_number": parse_int(first_present(row, CRD_KEYS)),
        "custodian_name": first_present(row, ("Custodian Name", "Name", "Custodian")),
        "custodian_city": _strip(row.get("Custodian City")),
        "custodian_state": _strip(row.get("Custodian State")),
        "custodian_country": _strip(row.get("Custodian Country")),
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


TABLE_SPECS: dict[str, TableSpec] = {
    "base_a":              TableSpec("base_a",              PART1_CSV_PREFIXES["base_a"],        PART1_URL, _BASE_A_SCHEMA,    _t_base_a),
    "schedule_a_b":        TableSpec("schedule_a_b",        PART1_CSV_PREFIXES["schedule_a_b"],  PART1_URL, _SCH_AB_SCHEMA,    _t_schedule_a_b),
    "schedule_d_1b":       TableSpec("schedule_d_1b",       PART1_CSV_PREFIXES["schedule_d_1b"], PART1_URL, _SCH_D1B_SCHEMA,   _t_schedule_d_1b),
    "schedule_d_1f":       TableSpec("schedule_d_1f",       PART1_CSV_PREFIXES["schedule_d_1f"], PART1_URL, _SCH_D1F_SCHEMA,   _t_schedule_d_1f),
    "schedule_d_1i":       TableSpec("schedule_d_1i",       PART1_CSV_PREFIXES["schedule_d_1i"], PART1_URL, _SCH_D1I_SCHEMA,   _t_schedule_d_1i),
    "filing_types":        TableSpec("filing_types",        PART1_CSV_PREFIXES["filing_types"],  PART1_URL, _FILING_TYPES_SCHEMA, _t_filing_types),
    "advw_main":           TableSpec("advw_main",           ADVW_CSV_PREFIXES["advw_main"],            ADVW_URL,  _ADVW_MAIN_SCHEMA,  _t_advw_main),
    "advw_w1_5_locations": TableSpec("advw_w1_5_locations", ADVW_CSV_PREFIXES["advw_w1_5_locations"],  ADVW_URL,  _ADVW_W15_SCHEMA,   _t_advw_w1_5),
    "advw_w1_8_custodians":TableSpec("advw_w1_8_custodians",ADVW_CSV_PREFIXES["advw_w1_8_custodians"], ADVW_URL,  _ADVW_W18_SCHEMA,   _t_advw_w1_8),
}


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def stream_download(url: str, dest: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    bytes_total = 0
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    next_log = 100 * 1024 * 1024
    with httpx.stream("GET", url, headers=headers, timeout=600.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                sha.update(chunk)
                bytes_total += len(chunk)
                if bytes_total >= next_log:
                    logger.info("download_progress mb=%d url=%s", bytes_total // (1 << 20), url)
                    next_log += 100 * 1024 * 1024
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
            if Path(member).name.startswith(prefix) and member.endswith(".csv"):
                return member
    return None


def iter_csv_rows(zip_path: Path, csv_member: str) -> Iterator[dict]:
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(csv_member) as raw:
            txt = io.TextIOWrapper(raw, encoding="latin-1", newline="")
            yield from csv.DictReader(txt)


def build_filing_id_to_crd_map(zip_path: Path) -> dict[int, tuple[int | None, str | None]]:
    """Single linear scan of IA_ADV_Base_A → filing_id → (crd, sec_number)."""
    member = find_csv_in_zip(zip_path, PART1_CSV_PREFIXES["base_a"])
    if member is None:
        raise RuntimeError("IA_ADV_Base_A CSV not found in Part 1 ZIP")
    out: dict[int, tuple[int | None, str | None]] = {}
    t0 = time.monotonic()
    n = 0
    for row in iter_csv_rows(zip_path, member):
        fid = parse_int(first_present(row, FILING_ID_KEYS))
        if fid is None:
            continue
        crd = parse_int(first_present(row, CRD_KEYS))
        sec = first_present(row, SEC_NUM_KEYS)
        out[fid] = (crd, sec)
        n += 1
        if n % 100_000 == 0:
            logger.info("crd_map_scan rows=%d unique_filing_ids=%d", n, len(out))
    logger.info("crd_map_built rows=%d unique_filing_ids=%d crd_nonnull=%d elapsed=%.1fs",
                n, len(out), sum(1 for v in out.values() if v[0] is not None), time.monotonic() - t0)
    return out


# ---------------------------------------------------------------------------
# Per-table writer
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    table: str
    rows: int = 0
    parts: int = 0
    bytes_written: int = 0
    elapsed_seconds: float = 0.0
    r2_keys: list[str] = field(default_factory=list)


def write_table(
    spec: TableSpec,
    *,
    zip_path: Path,
    crd_map: dict[int, tuple[int | None, str | None]],
    out_dir: Path,
    s3,
    year: int,
    upload_r2: bool,
    rows_per_part: int,
) -> WriteResult:
    member = find_csv_in_zip(zip_path, spec.csv_prefix)
    if member is None:
        raise RuntimeError(f"CSV with prefix {spec.csv_prefix!r} not found in {zip_path.name}")

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
        pq.write_table(tbl, local_path, compression="zstd", compression_level=9)
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
        logger.info("part_written table=%s part=%d rows=%d bytes=%d r2_key=%s",
                    spec.name, part_idx, len(buf), size, r2_key)
        part_idx += 1
        buf.clear()

    n_in = 0
    n_out = 0
    for row in iter_csv_rows(zip_path, member):
        n_in += 1
        try:
            transformed = spec.row_transform(row, crd_map)
        except Exception as exc:
            logger.warning("row_transform_error table=%s row_in=%d err=%s", spec.name, n_in, exc)
            continue
        if transformed.get("filing_id") is None:
            continue
        buf.append(transformed)
        n_out += 1
        if len(buf) >= rows_per_part:
            flush()
        if n_in % 250_000 == 0:
            logger.info("read_progress table=%s rows_in=%d rows_out=%d", spec.name, n_in, n_out)

    flush()
    result.rows = n_out
    result.elapsed_seconds = time.monotonic() - t0
    logger.info("table_done table=%s rows_in=%d rows_out=%d parts=%d bytes=%d elapsed=%.1fs",
                spec.name, n_in, n_out, result.parts, result.bytes_written, result.elapsed_seconds)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--all", action="store_true", help="Run all 9 tables.")
    grp.add_argument("--tables", help="Comma-separated subset of table names.")
    ap.add_argument("--year", type=int, default=2024, help="Partition year directory in R2.")
    ap.add_argument("--cache-dir", type=Path, default=Path("/tmp/sec_adv_zips"))
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/sec_adv_parquet"))
    ap.add_argument("--no-upload", action="store_true", help="Write Parquet locally only, skip R2 upload.")
    ap.add_argument("--rows-per-part", type=int, default=CHUNK_ROWS)
    args = ap.parse_args()

    if args.all:
        tables = ALL_TABLES
    else:
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
        unknown = [t for t in tables if t not in TABLE_SPECS]
        if unknown:
            ap.error(f"unknown table(s): {unknown}; valid={ALL_TABLES}")

    upload_r2 = not args.no_upload
    s3 = make_s3_client() if upload_r2 else None

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    zips: dict[str, Path] = {}
    if any(TABLE_SPECS[t].source_zip == PART1_URL for t in tables):
        zips[PART1_URL] = ensure_zip(PART1_URL, args.cache_dir)
    if any(TABLE_SPECS[t].source_zip == ADVW_URL for t in tables):
        zips[ADVW_URL] = ensure_zip(ADVW_URL, args.cache_dir)

    needs_crd_map = any(
        TABLE_SPECS[t].source_zip == PART1_URL and t != "base_a"
        for t in tables
    )
    crd_map: dict[int, tuple[int | None, str | None]] = {}
    if needs_crd_map and PART1_URL in zips:
        crd_map = build_filing_id_to_crd_map(zips[PART1_URL])

    results: list[WriteResult] = []
    ordered = [t for t in tables if t == "base_a"] + [t for t in tables if t != "base_a"]
    run_started = datetime.now(timezone.utc)

    for table_name in ordered:
        spec = TABLE_SPECS[table_name]
        zip_path = zips[spec.source_zip]
        out_sub = args.out_dir / table_name
        result = write_table(
            spec,
            zip_path=zip_path,
            crd_map=crd_map,
            out_dir=out_sub,
            s3=s3,
            year=args.year,
            upload_r2=upload_r2,
            rows_per_part=args.rows_per_part,
        )
        results.append(result)

    total_rows = sum(r.rows for r in results)
    total_bytes = sum(r.bytes_written for r in results)
    total_elapsed = sum(r.elapsed_seconds for r in results)
    logger.info("BUILD COMPLETE tables=%d total_rows=%d total_bytes=%d total_elapsed=%.1fs",
                len(results), total_rows, total_bytes, total_elapsed)
    summary = {
        "started_at": run_started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "year": args.year,
        "uploaded_to_r2": upload_r2,
        "bucket": R2_BUCKET if upload_r2 else None,
        "tables": [
            {
                "table": r.table,
                "rows": r.rows,
                "parts": r.parts,
                "bytes": r.bytes_written,
                "elapsed_seconds": round(r.elapsed_seconds, 2),
                "r2_keys": r.r2_keys[:3] + ([f"... ({len(r.r2_keys)-3} more)"] if len(r.r2_keys) > 3 else []),
            }
            for r in results
        ],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
