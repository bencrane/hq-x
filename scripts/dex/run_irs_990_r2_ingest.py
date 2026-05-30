#!/usr/bin/env python3
"""IRS Form 990 / 990-EZ / 990-PF e-File XML → R2 ZSTD Parquet ingest.

Source: IRS TEOS bulk download portal at
``https://apps.irs.gov/pub/epostcard/990/xml/{YYYY}/...`` (the deprecated
``s3://irs-form-990/`` mirror is empty as of 2024).

Each invocation processes one submission year (the calendar year the IRS
published the e-filed return). Within a year, the IRS publishes one or more
ZIPs per month; this script scrapes the year's ZIP URLs from the IRS
landing page and downloads each ZIP. Inside each ZIP is a flat directory
of XML files (one per filing). The script iterates the XMLs, parses each
via ``_lib.irs_990_xml_parser``, and accumulates buckets:

  - filings_990   (full Form 990 + 990-EZ — same schema)
  - filings_990pf (private foundations)
  - persons_990   (officers/directors/trustees/key-emp/highest-paid + top contractors from Form 990)
  - persons_990pf (managers from Form 990-PF)
  - compensation_990 / compensation_990pf
  - related_orgs (Schedule R)

At end-of-run, each bucket is materialized as a single ZSTD Parquet via
DuckDB and uploaded to::

    s3://dex-raw-landing-zone/irs-990/year={submission_year}/<table>.parquet

Audit: one ``ops.irs_990_r2_ingest_runs`` row per (submission_year, table).
RisingWave wiring is DEFERRED.

Usage::

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_990_r2_ingest.py 2024
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_990_r2_ingest.py 2024 --max-filings 1000
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_990_r2_ingest.py 2024 --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_990_r2_ingest.py --all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_990_r2_ingest.py --years 2022-2024

Default span: 2019 through 2026 — the years available on the TEOS portal.

See directive
~/Desktop/hq/directives/2026-05-08-irs-form-990-efile-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import irs_990_xml_parser as P  # noqa: E402


# --------------------------------------------------------------------------- #
# Deflate64 (compression method 9) support — IRS switched the calendar-year
# CT1 archive (e.g. 2020_TEOS_XML_CT1.zip) from method 8 (Deflate) to method
# 9 (Deflate64) starting in 2020. Python's stdlib zipfile doesn't support
# method 9, so we register a third-party Deflate64 decompressor (the
# `inflate64` package) at module-import time. With this patch in place,
# zipfile.ZipFile.open() transparently handles both method 8 and method 9
# entries inside the same archive.
# --------------------------------------------------------------------------- #


def _patch_zipfile_for_deflate64() -> None:
    """Register a Deflate64 decompressor so stdlib zipfile can read the IRS
    2020 CT1 archive (and any future Deflate64-compressed sources).

    Idempotent: if `inflate64` is unavailable or the patch is already
    applied, this function silently does nothing — the stdlib's existing
    NotImplementedError will surface on first access.
    """
    try:
        import inflate64  # type: ignore[import-untyped]
    except ImportError:
        return
    import zipfile as _zf

    if getattr(_zf, "_deflate64_patched", False):
        return

    class _Deflate64Decompressor:
        """Minimal API-compatible wrapper for the C-level zlib decompressor
        protocol that ZipExtFile expects."""

        def __init__(self) -> None:
            self._inf = inflate64.Inflater()

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            if max_length:
                return self._inf.inflate(data, max_length)
            return self._inf.inflate(data)

        @property
        def eof(self) -> bool:
            return getattr(self._inf, "eof", False)

        def flush(self) -> bytes:
            flush = getattr(self._inf, "flush", None)
            return flush() if callable(flush) else b""

    _orig_get_decompressor = _zf._get_decompressor

    def _patched(method: int):  # noqa: ANN202
        if method == 9:  # ZIP_DEFLATE64
            return _Deflate64Decompressor()
        return _orig_get_decompressor(method)

    _zf._get_decompressor = _patched
    _zf._check_compression = lambda m: None  # bypass stdlib's reject list
    _zf.compressor_names[9] = "deflate64"
    _zf._deflate64_patched = True  # type: ignore[attr-defined]


_patch_zipfile_for_deflate64()

R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

LANDING_PAGE_URL = "https://www.irs.gov/charities-non-profits/form-990-series-downloads"
ZIP_URL_RE = re.compile(
    r'https://apps\.irs\.gov/pub/epostcard/990/xml/(\d{4})/[\w._-]+\.zip',
    re.IGNORECASE,
)

# All 8 submission years 2019-2026 available on the TEOS portal.
DEFAULT_SPAN: tuple[int, ...] = tuple(range(2019, 2027))

# Output table buckets.
TABLES: tuple[str, ...] = (
    "filings_990",
    "filings_990pf",
    "persons_990",
    "persons_990pf",
    "compensation_990",
    "compensation_990pf",
    "related_orgs",
)


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("irs-990-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# Source discovery
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ZipMeta:
    year: int
    url: str
    filename: str
    last_modified: datetime | None
    content_length: int | None


def discover_zip_urls(client: httpx.Client, target_year: int) -> list[ZipMeta]:
    """Scrape the IRS Form 990 series download landing page for ZIP URLs.

    Returns ZipMeta(year, url, filename, last_modified, content_length) for
    every ZIP listed under the given target year. Last-Modified + Content-Length
    are populated by a HEAD on each URL.
    """
    r = client.get(LANDING_PAGE_URL, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    urls = sorted({m.group(0) for m in ZIP_URL_RE.finditer(r.text)})
    if not urls:
        raise RuntimeError(
            f"could not discover any 990-XML ZIP URLs at {LANDING_PAGE_URL}"
        )

    out: list[ZipMeta] = []
    for url in urls:
        m = ZIP_URL_RE.match(url)
        if not m:
            continue
        year_str = m.group(1)
        if int(year_str) != target_year:
            continue
        filename = url.rsplit("/", 1)[-1]
        try:
            head = client.head(url, follow_redirects=True, timeout=30.0)
            cl_raw = head.headers.get("content-length")
            cl = int(cl_raw) if cl_raw and cl_raw.isdigit() else None
            lm_raw = head.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(
                        lm_raw, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
        except httpx.HTTPError as exc:
            log.warning("HEAD %s failed: %s", url, exc)
            cl, lm = None, None
        out.append(
            ZipMeta(
                year=int(year_str),
                url=url,
                filename=filename,
                last_modified=lm,
                content_length=cl,
            )
        )
    return out


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=3600.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss", url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    last_log = time.monotonic()
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 15.0:
                            log.info(
                                "  download progress: %.1f MB written",
                                written / (1 << 20),
                            )
                            last_log = now
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download {url} failed: {last_exc}")


# --------------------------------------------------------------------------- #
# Per-XML parse worker
# --------------------------------------------------------------------------- #


def _parse_one(xml_bytes: bytes) -> tuple[str, dict[str, Any] | None]:
    """Worker function — parses one XML, returns ('ok', records) /
    ('filtered', None) / ('error', None).

    Runs inside a ProcessPoolExecutor worker. lxml is GIL-locked at the
    parser level so process-level parallelism is the right model for big
    ZIP runs.

    Outcomes:
      - 'ok'       : in-scope filing parsed successfully.
      - 'filtered' : the XML parsed but the return type is out of scope
                     (990-T or unknown). Expected: ~5% of every ZIP.
      - 'error'    : the XML failed to parse (malformed). Should be ~0%.
    """
    try:
        # parse_filing returns None for both malformed XML and out-of-scope
        # types. Distinguish via a quick sniff.
        result = P.parse_filing(xml_bytes)
    except Exception:
        return ("error", None)
    if result is not None:
        return (
            "ok",
            {
                "filing_type": result.filing.get("filing_type"),
                "filing": result.filing,
                "persons": result.persons,
                "compensation": result.compensation,
                "related_orgs": result.related_orgs,
            },
        )
    # Distinguish filtered from error by re-parsing — cheap because we only
    # do this for the rejection path (~5% of XMLs).
    try:
        from lxml import etree
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return ("error", None)
    rt = root.find("e:ReturnHeader/e:ReturnTypeCd", {"e": "http://www.irs.gov/efile"})
    if rt is not None and rt.text:
        return ("filtered", None)
    return ("error", None)


# --------------------------------------------------------------------------- #
# Bucketed accumulator → Parquet
# --------------------------------------------------------------------------- #


@dataclass
class Buckets:
    """In-memory record accumulators.

    Each bucket is a list of dicts; converted to PyArrow Table at flush time.
    Memory profile: ~1.4M filings × ~10 persons each × ~300 bytes/dict = ~4GB
    in worst case for a year. We keep one year in memory at a time. If this
    overflows, the future improvement is to flush per-month into multiple
    sub-Parquets and reduce-merge at the end.
    """

    filings_990: list[dict[str, Any]]
    filings_990pf: list[dict[str, Any]]
    persons_990: list[dict[str, Any]]
    persons_990pf: list[dict[str, Any]]
    compensation_990: list[dict[str, Any]]
    compensation_990pf: list[dict[str, Any]]
    related_orgs: list[dict[str, Any]]

    @classmethod
    def empty(cls) -> "Buckets":
        return cls([], [], [], [], [], [], [])

    def add(self, parsed: dict[str, Any], submission_year: int) -> None:
        ftype = parsed["filing_type"]
        # Annotate with submission_year — partition metadata. Tax year is
        # already on each row from the parser.
        parsed["filing"]["submission_year"] = submission_year
        for r in parsed["persons"]:
            r["submission_year"] = submission_year
        for r in parsed["compensation"]:
            r["submission_year"] = submission_year
        for r in parsed["related_orgs"]:
            r["submission_year"] = submission_year

        if ftype == "990PF":
            self.filings_990pf.append(parsed["filing"])
            self.persons_990pf.extend(parsed["persons"])
            self.compensation_990pf.extend(parsed["compensation"])
        else:  # 990 or 990EZ
            self.filings_990.append(parsed["filing"])
            self.persons_990.extend(parsed["persons"])
            self.compensation_990.extend(parsed["compensation"])
        self.related_orgs.extend(parsed["related_orgs"])

    def counts(self) -> dict[str, int]:
        return {
            "filings_990": len(self.filings_990),
            "filings_990pf": len(self.filings_990pf),
            "persons_990": len(self.persons_990),
            "persons_990pf": len(self.persons_990pf),
            "compensation_990": len(self.compensation_990),
            "compensation_990pf": len(self.compensation_990pf),
            "related_orgs": len(self.related_orgs),
        }

    def get(self, table_name: str) -> list[dict[str, Any]]:
        return getattr(self, table_name)


def _write_parquet(
    rows: list[dict[str, Any]],
    out_path: Path,
    *,
    log_prefix: str,
) -> tuple[int, int]:
    """Materialize a list of dicts to a single ZSTD Parquet file.

    Returns (row_count, bytes_written). Empty lists produce a zero-row Parquet
    with the union schema, which keeps the R2 layout consistent across years.
    """
    if not rows:
        # Write an empty Parquet with a placeholder schema — RW glob source
        # paths still need an object to exist.
        empty_schema = pa.schema([("submission_year", pa.int16())])
        empty_table = pa.Table.from_pydict(
            {"submission_year": []}, schema=empty_schema,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(empty_table, out_path, compression="zstd",
                       compression_level=9)
        return 0, out_path.stat().st_size

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert dicts → PyArrow Table. PyArrow infers schema from the first
    # row by default; for heterogeneous nullables, we explicitly let it
    # pick INT64/DOUBLE/STRING dynamically.
    t0 = time.monotonic()
    table = pa.Table.from_pylist(rows)
    pq.write_table(
        table, out_path, compression="zstd", compression_level=9,
        row_group_size=100_000,
    )
    log.info(
        "%s wrote %s rows → %s (%.1f MB) in %.1fs",
        log_prefix, f"{len(rows):,}", out_path.name,
        out_path.stat().st_size / (1 << 20),
        time.monotonic() - t0,
    )
    return len(rows), out_path.stat().st_size


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def get_prior_source_last_modified(
    conn: psycopg.Connection,
    submission_year: int,
    table_name: str,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_last_modified_max
              FROM ops.irs_990_r2_ingest_runs
             WHERE submission_year = %s
               AND table_name = %s
               AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (submission_year, table_name),
        )
        row = cur.fetchone()
    return row[0] if row else None


def insert_run_row(
    conn: psycopg.Connection,
    *,
    submission_year: int,
    table_name: str,
    source_zip_urls: list[str],
    source_last_modified_max: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.irs_990_r2_ingest_runs (
                submission_year, table_name, status, source_zip_urls,
                source_last_modified_max, prior_source_last_modified
            ) VALUES (%s, %s, 'running', %s, %s, %s)
            RETURNING id;
            """,
            (
                submission_year, table_name, Jsonb(source_zip_urls),
                source_last_modified_max, prior_source_last_modified,
            ),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    submission_year: int,
    table_name: str,
    source_zip_urls: list[str],
    source_last_modified_max: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.irs_990_r2_ingest_runs (
                submission_year, table_name, status, source_zip_urls,
                source_last_modified_max, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                submission_year, table_name, Jsonb(source_zip_urls),
                source_last_modified_max, prior_source_last_modified,
                started, started,
                Jsonb({"reason": "all source ZIPs Last-Modified <= prior"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    source_zip_count: int,
    source_zip_bytes_total: int,
    source_xml_count: int,
    source_xml_parsed: int,
    source_xml_failed: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    parquet_column_count: int,
    r2_bucket: str | None,
    r2_key: str | None,
    r2_total_bytes: int,
    ein_len9_rate: float | None,
    person_name_null_rate: float | None,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.irs_990_r2_ingest_runs
               SET status = %s,
                   source_zip_count = %s,
                   source_zip_bytes_total = %s,
                   source_xml_count = %s,
                   source_xml_parsed = %s,
                   source_xml_failed = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_key = %s, r2_total_bytes = %s,
                   ein_len9_rate = %s,
                   person_name_null_rate = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """,
            (
                status, source_zip_count, source_zip_bytes_total,
                source_xml_count, source_xml_parsed, source_xml_failed,
                parquet_row_count, parquet_bytes_written, parquet_column_count,
                r2_bucket, r2_key, r2_total_bytes,
                ein_len9_rate, person_name_null_rate,
                duration, error_message,
                Jsonb(notes) if notes else None, run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-year main
# --------------------------------------------------------------------------- #


def _validation_rates(
    filings: list[dict[str, Any]],
    persons: list[dict[str, Any]],
) -> tuple[float | None, float | None]:
    """Compute (ein_len9_rate, person_name_null_rate) for the year buckets.

    These are the directive's headline validation gates:
      - org_ein_normalized length=9 rate > 99%
      - person_first_normalized + person_last_normalized jointly NULL < 0.5%
    """
    ein_len9 = None
    if filings:
        ok = sum(
            1 for f in filings
            if f.get("org_ein_normalized") and len(f["org_ein_normalized"]) == 9
        )
        ein_len9 = round(ok / len(filings), 6)

    person_null = None
    if persons:
        null = sum(
            1 for p in persons
            if not p.get("person_first_normalized")
            and not p.get("person_last_normalized")
        )
        person_null = round(null / len(persons), 6)
    return ein_len9, person_null


def ingest_year(
    submission_year: int,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_filings: int | None,
    only_zips: set[str] | None,
    workers: int,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[year={submission_year}]"
    started_wall = time.monotonic()
    log.info("%s start", log_prefix)

    user_agent = "data-engine-x/irs-990-r2-ingest"
    with httpx.Client(headers={"User-Agent": user_agent}) as client:
        try:
            zips = discover_zip_urls(client, submission_year)
        except Exception:
            log.exception("%s ZIP discovery failed", log_prefix)
            return 1
        if not zips:
            log.error("%s no ZIPs published for year=%s", log_prefix, submission_year)
            return 1
        if only_zips:
            zips = [z for z in zips if z.filename in only_zips]
            if not zips:
                log.error("%s no ZIPs matched --only-zips=%s", log_prefix, only_zips)
                return 1
        log.info("%s discovered %d ZIPs for year=%s", log_prefix, len(zips), submission_year)
        for z in zips:
            log.info(
                "  %s  last_modified=%s  size=%s",
                z.filename, z.last_modified,
                f"{z.content_length:,}" if z.content_length else "?",
            )

        source_urls = [z.url for z in zips]
        last_mods = [z.last_modified for z in zips if z.last_modified is not None]
        source_last_modified_max = max(last_mods) if last_mods else None

        if dry_run:
            log.info("%s DRY RUN — exiting after discovery", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            # Skip-if-unchanged at the (year, filings_990) granularity. If the
            # filings_990 table is up to date, we skip the entire year.
            prior = get_prior_source_last_modified(conn, submission_year, "filings_990")
            log.info("%s prior source_last_modified_max: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified_max is not None
                and source_last_modified_max <= prior
            ):
                log.info("%s sources unchanged — recording no_change for all 7 tables",
                         log_prefix)
                for tbl in TABLES:
                    write_no_change_run(
                        conn,
                        submission_year=submission_year, table_name=tbl,
                        source_zip_urls=source_urls,
                        source_last_modified_max=source_last_modified_max,
                        prior_source_last_modified=prior,
                    )
                return 0

            # Insert one 'running' audit row per table — finalize at the end.
            run_ids: dict[str, str] = {}
            for tbl in TABLES:
                run_ids[tbl] = insert_run_row(
                    conn,
                    submission_year=submission_year, table_name=tbl,
                    source_zip_urls=source_urls,
                    source_last_modified_max=source_last_modified_max,
                    prior_source_last_modified=prior,
                )
            log.info("%s run ids: %s", log_prefix, run_ids)

            buckets = Buckets.empty()
            workdir.mkdir(parents=True, exist_ok=True)
            zip_bytes_total = 0
            xml_total = 0
            xml_parsed = 0
            xml_filtered = 0
            xml_failed = 0

            try:
                for zmeta in zips:
                    zip_path = workdir / zmeta.filename
                    log.info("%s downloading %s …", log_prefix, zmeta.filename)
                    n = download_zip(client, zmeta.url, zip_path)
                    zip_bytes_total += n
                    log.info("%s   %s downloaded (%.1f MB)",
                             log_prefix, zmeta.filename, n / (1 << 20))

                    # Iterate the ZIP, dispatch per-XML to a process pool.
                    with zipfile.ZipFile(zip_path) as zf:
                        names = [
                            n for n in zf.namelist()
                            if n.lower().endswith(".xml")
                        ]
                        log.info("%s   %s contains %d XMLs", log_prefix,
                                 zmeta.filename, len(names))

                        # Pre-load all XML bytes (the ZIP is on local disk; this
                        # is cheap). We need bytes-in-memory because the worker
                        # processes can't open the ZIP independently without
                        # reopening it themselves (which fights with our own
                        # iteration). At ~600MB uncompressed per ZIP this is
                        # comfortable in RAM.
                        if max_filings is not None:
                            names = names[: max_filings - xml_total]

                        with ProcessPoolExecutor(max_workers=workers) as exe:
                            # submit batches to limit memory; the executor
                            # buffers all submitted futures, so we batch.
                            BATCH = 5_000
                            for batch_start in range(0, len(names), BATCH):
                                batch = names[batch_start: batch_start + BATCH]
                                futs = []
                                for n in batch:
                                    with zf.open(n) as f:
                                        xml_bytes = f.read()
                                    futs.append(exe.submit(_parse_one, xml_bytes))
                                for fut in as_completed(futs):
                                    outcome, parsed = fut.result()
                                    xml_total += 1
                                    if outcome == "ok" and parsed is not None:
                                        xml_parsed += 1
                                        buckets.add(parsed, submission_year)
                                    elif outcome == "filtered":
                                        xml_filtered += 1
                                    else:
                                        xml_failed += 1
                                if batch_start // BATCH % 4 == 0:
                                    log.info(
                                        "%s   progress: zip=%s parsed=%s/%s "
                                        "(filtered=%s failed=%s) %s",
                                        log_prefix, zmeta.filename,
                                        f"{xml_parsed:,}", f"{xml_total:,}",
                                        f"{xml_filtered:,}", f"{xml_failed:,}",
                                        json.dumps(buckets.counts()),
                                    )

                    # Drop the ZIP once parsed.
                    try:
                        zip_path.unlink(missing_ok=True)
                    except Exception:
                        pass

                    if max_filings is not None and xml_total >= max_filings:
                        log.info("%s reached --max-filings=%s; stopping ZIP loop",
                                 log_prefix, max_filings)
                        break

                log.info(
                    "%s parse complete: total=%s parsed=%s filtered=%s failed=%s "
                    "buckets=%s",
                    log_prefix, f"{xml_total:,}", f"{xml_parsed:,}",
                    f"{xml_filtered:,}", f"{xml_failed:,}",
                    json.dumps(buckets.counts()),
                )

                # Compute validation rates (used for the audit row).
                ein_rate_990, ppl_rate_990 = _validation_rates(
                    buckets.filings_990, buckets.persons_990,
                )
                ein_rate_990pf, ppl_rate_990pf = _validation_rates(
                    buckets.filings_990pf, buckets.persons_990pf,
                )
                log.info(
                    "%s validation: 990 ein_len9=%s person_null=%s | "
                    "990pf ein_len9=%s person_null=%s",
                    log_prefix, ein_rate_990, ppl_rate_990,
                    ein_rate_990pf, ppl_rate_990pf,
                )

                # Per-table: write Parquet → upload R2 → finalize audit row.
                r2_prefix = (
                    r2_prefix_override
                    or f"irs-990/year={submission_year}/"
                ).rstrip("/") + "/"

                for tbl in TABLES:
                    rows = buckets.get(tbl)
                    out_path = workdir / f"{tbl}.parquet"
                    rc, parquet_bytes = _write_parquet(
                        rows, out_path, log_prefix=f"{log_prefix} [{tbl}]"
                    )
                    if rows:
                        col_count = len(rows[0])
                    else:
                        col_count = 0

                    target_key = r2_prefix + f"{tbl}.parquet"
                    uploaded = upload_to_r2(
                        out_path, bucket=R2_BUCKET, key=target_key,
                    )
                    log.info(
                        "%s [%s] uploaded → s3://%s/%s (%.1f MB, %s rows)",
                        log_prefix, tbl, R2_BUCKET, target_key,
                        uploaded / (1 << 20), f"{rc:,}",
                    )

                    # Pick validation rates per filing-type table.
                    if tbl == "filings_990":
                        ein_rate, ppl_rate = ein_rate_990, None
                    elif tbl == "filings_990pf":
                        ein_rate, ppl_rate = ein_rate_990pf, None
                    elif tbl == "persons_990":
                        ein_rate, ppl_rate = None, ppl_rate_990
                    elif tbl == "persons_990pf":
                        ein_rate, ppl_rate = None, ppl_rate_990pf
                    else:
                        ein_rate, ppl_rate = None, None

                    finalize_run_row(
                        conn, run_ids[tbl], status="completed",
                        source_zip_count=len(zips),
                        source_zip_bytes_total=zip_bytes_total,
                        source_xml_count=xml_total,
                        source_xml_parsed=xml_parsed,
                        source_xml_failed=xml_failed,
                        parquet_row_count=rc,
                        parquet_bytes_written=parquet_bytes,
                        parquet_column_count=col_count,
                        r2_bucket=R2_BUCKET, r2_key=target_key,
                        r2_total_bytes=uploaded,
                        ein_len9_rate=ein_rate,
                        person_name_null_rate=ppl_rate,
                        started_at=started_wall,
                        error_message=None,
                        notes={
                            "max_filings": max_filings,
                            "workers": workers,
                            "zip_filenames": [z.filename for z in zips],
                            "xml_filtered": xml_filtered,
                        },
                    )

                    try:
                        out_path.unlink(missing_ok=True)
                    except Exception:
                        pass

                log.info(
                    "%s DONE total_xml=%s parsed=%s filtered=%s failed=%s wall=%.1fs",
                    log_prefix, f"{xml_total:,}", f"{xml_parsed:,}",
                    f"{xml_filtered:,}", f"{xml_failed:,}",
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                for tbl in TABLES:
                    rid = run_ids.get(tbl)
                    if rid:
                        try:
                            finalize_run_row(
                                conn, rid, status="failed",
                                source_zip_count=len(zips),
                                source_zip_bytes_total=zip_bytes_total,
                                source_xml_count=xml_total,
                                source_xml_parsed=xml_parsed,
                                source_xml_failed=xml_failed,
                                parquet_row_count=0,
                                parquet_bytes_written=0,
                                parquet_column_count=0,
                                r2_bucket=None, r2_key=None, r2_total_bytes=0,
                                ein_len9_rate=None,
                                person_name_null_rate=None,
                                started_at=started_wall,
                                error_message=str(exc), notes=None,
                            )
                        except Exception:
                            log.exception("failed to finalize audit row for %s", tbl)
                return 1
            finally:
                # Best-effort scratch cleanup.
                for f in workdir.glob("*.zip"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
                for f in workdir.glob("*.parquet"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
                shutil.rmtree(workdir / "extracted", ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_year_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        ya, yb = int(a), int(b)
    else:
        ya = yb = int(s)
    out: list[int] = []
    for y in range(ya, yb + 1):
        if y in DEFAULT_SPAN:
            out.append(y)
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("year", nargs="?", type=int,
                   help="Single 4-digit submission year (2019-2025).")
    p.add_argument("--years", default=None, help="Year range, e.g., 2022-2024.")
    p.add_argument("--all", action="store_true",
                   help=f"All years in {DEFAULT_SPAN[0]}-{DEFAULT_SPAN[-1]}.")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-filings", type=int, default=None,
                   help="Limit total XMLs parsed (smoke testing).")
    p.add_argument("--only-zips", default=None,
                   help="Comma-separated ZIP filenames (e.g., '2024_TEOS_XML_01A.zip')")
    p.add_argument("--workers", type=int, default=8,
                   help="ProcessPoolExecutor max workers (default 8).")
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override the R2 destination prefix (smoke-test use).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/irs_990_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.all:
        years = list(DEFAULT_SPAN)
    elif args.years:
        years = parse_year_range(args.years)
    else:
        if args.year is None:
            log.error("must pass year (or --years / --all)")
            return 2
        if args.year not in DEFAULT_SPAN:
            log.error("year %s not in supported span %s-%s",
                      args.year, DEFAULT_SPAN[0], DEFAULT_SPAN[-1])
            return 2
        years = [args.year]

    only_zips: set[str] | None = None
    if args.only_zips:
        only_zips = {s.strip() for s in args.only_zips.split(",") if s.strip()}

    rc = 0
    for y in years:
        log.info("=" * 70)
        log.info("=== INGEST: year=%s ===", y)
        log.info("=" * 70)
        rc_one = ingest_year(
            y,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_filings=args.max_filings,
            only_zips=only_zips,
            workers=args.workers,
            r2_prefix_override=args.r2_prefix_override,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("year %s failed; continuing with remaining years", y)
    return rc


if __name__ == "__main__":
    sys.exit(main())
