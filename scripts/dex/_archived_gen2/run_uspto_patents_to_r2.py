"""USPTO Patents (PatentsView bulk) → R2 ZSTD Parquet ingest.

Downloads the complete USPTO PatentsView bulk patent corpus from the USPTO Open
Data Portal (data.uspto.gov) and writes ZSTD Parquet snapshots to Cloudflare R2.
Two products, 60 .tsv.zip tables total:

  PVGPATDIS  — Granted Patent Disambiguated Data (35 tables, 1976→2025)  → slug "granted"
  PVPGPUBDIS — Pre-Grant Publication Disambiguated Data (25 tables, 2001→2025) → slug "pregrant"

R2 layout (per-table partitioning — citation tables are too large for one combined file):
  s3://dex-raw-landing-zone/uspto-patents/{granted,pregrant}/{table}/snapshot={YYYY-MM-DD}/data.parquet
  s3://dex-raw-landing-zone/uspto-patents/{granted,pregrant}/_data_dictionary/PV_*_data_dictionary.pdf

The ingest is uniform across all 60 tables — one generic per-file pipeline driven
by the per-product JSON manifest; no per-table bespoke handlers.

Per table: download .tsv.zip → unzip the single .tsv → sniff encoding (transcode
via iconv only if non-UTF8) → DuckDB single-shot all-VARCHAR ZSTD Parquet
transcode → TSV-vs-Parquet row-count parity (>0.1% delta fails the table) →
upload to R2 → ledger row in ops.uspto_patents_ingest_runs. A single table
failing is recorded status='failed' and the loop continues to the next.

Skip-if-unchanged / resume primitive: the last status='completed' ledger row for
each (product, table_name) is compared against the manifest entry's
fileLastModifiedDateTime; if equal, the table is skipped (no ledger row written).
This is also the resume primitive — a re-run after a partial/killed run completes
only the not-yet-done tables. --force ignores it.

WAF: data.uspto.gov sits behind an AWS WAF that 403s bare curl / python-requests
default agents. A browser-shape User-Agent is sent on every request (manifest +
file). No API key — bulk downloads are anonymous.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_uspto_patents_to_r2.py \\
        [--product {granted,pregrant,all}] [--table <name>] \\
        [--snapshot-date YYYY-MM-DD] [--force]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import boto3
import botocore.config
import duckdb
import psycopg2
import requests

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── load-bearing constants (verify harness greps for these) ─────────────────

# USPTO Open Data Portal — per-product manifest (no auth; browser UA required).
MANIFEST_URL_TEMPLATE = (
    "https://data.uspto.gov/ui/datasets/products/{product}?includeFiles=true"
)
R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX_ROOT = "uspto-patents"

# AWS WAF on data.uspto.gov 403s bare curl / python-requests — send a browser UA
# on every request (L55).
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {"User-Agent": BROWSER_UA}

# Two PatentsView bulk products → R2 slug.
PRODUCT_TO_SLUG = {
    "PVGPATDIS":  "granted",
    "PVPGPUBDIS": "pregrant",
}
# CLI product arg → upstream product code.
CLI_PRODUCT_TO_CODE = {
    "granted":  "PVGPATDIS",
    "pregrant": "PVPGPUBDIS",
}

# Row-count parity tolerance (L41) — >0.1% delta fails the table.
ROW_COUNT_DELTA_TOLERANCE = 0.001


# ── helpers ──────────────────────────────────────────────────────────────────

def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=botocore.config.Config(
            connect_timeout=60,
            read_timeout=300,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def _fetch_manifest(product_code: str) -> dict:
    """Fetch a USPTO Open Data Portal bulk product manifest (browser UA required).

    data.uspto.gov sits behind an AWS WAF that intermittently returns an empty
    body or a non-JSON challenge page even to a browser-UA request. Retry with
    backoff so a single transient blip does not abort a multi-table ingest."""
    url = MANIFEST_URL_TEMPLATE.format(product=product_code)
    last_exc: Exception | None = None
    for attempt in range(1, 6):
        logger.info(
            "fetching USPTO manifest for %s from %s (attempt %d/5)",
            product_code, url, attempt,
        )
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            logger.warning(
                "USPTO manifest fetch failed for %s (attempt %d/5): %s",
                product_code, attempt, exc,
            )
            if attempt < 5:
                time.sleep(5 * attempt)
    raise RuntimeError(
        f"USPTO manifest fetch for {product_code} failed after 5 attempts"
    ) from last_exc


def _manifest_files(manifest: dict) -> list[dict]:
    """Extract the fileDataBag list, or raise if the ODP API structure drifted."""
    try:
        bag = manifest["bulkDataProductBag"][0]
        return bag["productFileBag"]["fileDataBag"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "USPTO manifest structure changed — "
            "bulkDataProductBag[0].productFileBag.fileDataBag absent. "
            "STOP and inspect the ODP API."
        ) from exc


def _to_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
    """Normalize a datetime to tz-aware UTC. USPTO manifest timestamps are
    naive-but-UTC; the ledger column is timestamptz. Comparing both as
    tz-aware UTC instants makes skip-if-unchanged / resume robust regardless
    of which side carries tzinfo."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _last_completed_source_modified(
    conn, product: str, table_name: str
) -> datetime.datetime | None:
    """Return source_last_modified of the last completed run for this
    (product, table_name) as a tz-aware UTC datetime, or None. Used for
    skip-if-unchanged / resume."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_last_modified
              FROM ops.uspto_patents_ingest_runs
             WHERE product = %s
               AND table_name = %s
               AND status = 'completed'
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (product, table_name),
        )
        row = cur.fetchone()
    return _to_utc(row[0]) if row and row[0] is not None else None


def _record_run_start(
    conn,
    product: str,
    table_name: str,
    snapshot_date: datetime.date,
    source_last_modified: datetime.datetime | None,
) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.uspto_patents_ingest_runs
                (ingest_run_id, product, table_name, snapshot_date,
                 source_last_modified, started_at, status)
            VALUES (%s, %s, %s, %s, %s, now(), 'running')
            """,
            (run_id, product, table_name, snapshot_date, source_last_modified),
        )
    conn.commit()
    logger.info(
        "started run %s product=%s table=%s snapshot=%s",
        run_id, product, table_name, snapshot_date,
    )
    return run_id


def _record_run_complete(conn, run_id: str, rows_ingested: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.uspto_patents_ingest_runs
               SET status = 'completed', completed_at = now(), rows_ingested = %s
             WHERE ingest_run_id = %s
            """,
            (rows_ingested, run_id),
        )
    conn.commit()
    logger.info("completed run %s rows=%d", run_id, rows_ingested)


def _record_run_failed(conn, run_id: str, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.uspto_patents_ingest_runs
               SET status = 'failed', completed_at = now(), error_message = %s
             WHERE ingest_run_id = %s
            """,
            (error_message[:4000], run_id),
        )
    conn.commit()
    logger.error("failed run %s: %s", run_id, error_message[:300])


def _parse_modified(value: str | None) -> datetime.datetime | None:
    """Parse a USPTO manifest timestamp ('2026-03-12 22:34:17') to a datetime."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
    logger.warning("could not parse manifest timestamp %r", value)
    return None


def _sniff_and_transcode_utf8(tsv_path: str) -> str:
    """Sniff the TSV for non-UTF8 bytes (L41). If found, transcode WINDOWS-1252 →
    UTF-8 via iconv and return the new path; otherwise return tsv_path unchanged."""
    try:
        with open(tsv_path, "rb") as f:
            sample = f.read(8 * 1024 * 1024)
        sample.decode("utf-8")
        return tsv_path  # valid UTF-8 (at least the head) — no transcode
    except UnicodeDecodeError:
        logger.warning("%s: non-UTF8 byte found — transcoding WINDOWS-1252 → UTF-8", tsv_path)

    out_path = tsv_path + ".utf8"
    subprocess.run(
        ["iconv", "-f", "WINDOWS-1252", "-t", "UTF-8", "-o", out_path, tsv_path],
        check=True,
    )
    return out_path


def _tsv_data_row_count(tsv_path: str) -> int:
    """Count *logical* data rows in a TSV (excluding the 1 header row).

    Uses the csv module — a physical line count is wrong for tables whose
    quoted fields contain embedded newlines (g_us_patent_citation etc.), where
    one logical row spans multiple physical lines. csv.reader applies the same
    tab-delim + double-quote dialect DuckDB parses with, so this count matches
    a correct transcode."""
    csv.field_size_limit(sys.maxsize)  # patent abstracts / citation text are large
    total = 0
    with open(tsv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t", quotechar='"')
        for _ in reader:
            total += 1
    return max(total - 1, 0)


def _transcode_to_parquet(tsv_path: str, parquet_path: str) -> int:
    """DuckDB single-shot transcode: TSV → all-VARCHAR ZSTD Parquet. Returns the
    Parquet row count (L9, L56).

    parallel=FALSE is required: DuckDB's parallel CSV scanner cannot combine
    null_padding with quoted newlines (several PatentsView tables — citations,
    abstracts — carry embedded newlines inside quoted fields), and would fail
    with 'parallel scanner does not support null_padding in conjunction with
    quoted new lines'. Single-threaded read is slower but correct for all 60
    tables regardless of whether a given table has embedded newlines.

    escape='"' is pinned explicitly. PatentsView TSVs are RFC-4180-quoted —
    quote char is '"', and a literal '"' inside a field is doubled (""). Left
    to auto-detect, DuckDB samples the file and on the large citation tables
    mis-detects the escape char as the apostrophe (e.g. the cited-by name
    "Pilo'"), which makes a quoted field never close — it then swallows
    hundreds of rows and fails with a CSV parse error. Pinning escape='"'
    makes apostrophes literal and "" the only escape, the correct dialect."""
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
                SELECT * FROM read_csv(
                    '{tsv_path}',
                    delim='\t', header=TRUE, quote='"', escape='"',
                    all_varchar=TRUE, null_padding=TRUE, strict_mode=FALSE,
                    parallel=FALSE
                )
            )
            TO '{parquet_path}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
            """
        )
        (row_count,) = con.execute(
            f"SELECT count(*) FROM read_parquet('{parquet_path}')"
        ).fetchone()
        return int(row_count)
    finally:
        con.close()


def _resolve_download_url(download_uri: str) -> str:
    """Resolve a USPTO ODP file URL to the actual downloadable .tsv.zip URL.

    As of 2026-05 the ODP file endpoint frequently does not stream the zip
    directly: it returns a short JSON instruction body — "Use redirect URL to
    download: <signed-cloudfront-url>. ..." — carrying a time-limited signed
    CloudFront URL (USPTO's load-shedding path for bursty callers). A response
    that is already zip / octet-stream bytes (the direct path) is passed
    through unchanged. The signed URL is short-lived, so the caller resolves
    immediately before each download attempt."""
    resp = requests.get(
        download_uri, headers=HTTP_HEADERS, timeout=120,
        allow_redirects=False, stream=True,
    )
    try:
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "").lower()
        if "zip" in ctype or "octet-stream" in ctype:
            return download_uri
        body = resp.text
    finally:
        resp.close()
    match = re.search(r"https://\S+?Key-Pair-Id=[A-Za-z0-9]+", body)
    if not match:
        raise RuntimeError(
            f"USPTO file endpoint returned neither zip bytes nor a redirect "
            f"URL: {body[:200]!r}"
        )
    return match.group(0)


def _download_table_zip(download_uri: str, zip_path: str, file_name: str) -> None:
    """Resolve + download one USPTO .tsv.zip to zip_path, validating it is a
    real zip archive. Retries with backoff — the ODP endpoint rate-limits
    bursts and the signed redirect URLs are short-lived."""
    last_exc: Exception | None = None
    for attempt in range(1, 6):
        try:
            resolved = _resolve_download_url(download_uri)
            logger.info(
                "downloading %s (attempt %d/5) from %s",
                file_name, attempt, resolved,
            )
            with requests.get(
                resolved, headers=HTTP_HEADERS, timeout=1800, stream=True,
            ) as resp:
                resp.raise_for_status()
                with open(zip_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
                        f.write(chunk)
            if not zipfile.is_zipfile(zip_path):
                raise RuntimeError(
                    f"{file_name} downloaded but is not a valid zip "
                    f"({Path(zip_path).stat().st_size} bytes)"
                )
            logger.info(
                "downloaded → %s (%d bytes)",
                zip_path, Path(zip_path).stat().st_size,
            )
            return
        except (requests.RequestException, RuntimeError, OSError) as exc:
            last_exc = exc
            logger.warning(
                "download of %s failed (attempt %d/5): %s",
                file_name, attempt, exc,
            )
            Path(zip_path).unlink(missing_ok=True)
            if attempt < 5:
                time.sleep(15 * attempt)
    raise RuntimeError(
        f"download of {file_name} failed after 5 attempts"
    ) from last_exc


def ingest_table(
    conn,
    s3,
    product: str,
    slug: str,
    file_entry: dict,
    snapshot_date: datetime.date,
    force: bool,
) -> str:
    """Download, transcode, and upload one USPTO .tsv.zip table to R2.

    Returns one of: 'completed', 'failed', 'skipped'. Never raises for a
    single-table failure — the caller's loop continues to the next table.
    """
    file_name: str = file_entry["fileName"]              # e.g. "g_patent.tsv.zip"
    table_name = file_name[: -len(".tsv.zip")]           # e.g. "g_patent"
    download_uri: str = file_entry["fileDownloadURI"]
    source_modified = _parse_modified(file_entry.get("fileLastModifiedDateTime"))
    source_modified_utc = _to_utc(source_modified)

    # Skip-if-unchanged / resume primitive — compare datetime instants, not
    # string forms (the ledger column is timestamptz, the manifest is naive-UTC).
    if not force:
        last_completed = _last_completed_source_modified(conn, product, table_name)
        if (
            last_completed is not None
            and source_modified_utc is not None
            and last_completed == source_modified_utc
        ):
            logger.info(
                "product=%s table=%s unchanged (source_last_modified=%s) — skipping",
                product, table_name, source_modified_utc.isoformat(),
            )
            return "skipped"

    run_id: str | None = None
    tmp_dir = tempfile.mkdtemp(prefix=f"uspto_{table_name}_")
    zip_path = os.path.join(tmp_dir, file_name)
    tsv_path: str | None = None
    transcoded_path: str | None = None
    parquet_path = os.path.join(tmp_dir, f"{table_name}.parquet")

    try:
        run_id = _record_run_start(
            conn, product, table_name, snapshot_date, source_modified_utc
        )

        # Download — resolve the ODP signed-redirect URL, stream to disk,
        # validate the zip, retry through the endpoint's bursty rate-limiting.
        _download_table_zip(download_uri, zip_path, file_name)

        # Unzip → the single .tsv member.
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist() if n.endswith(".tsv")]
            if not members:
                raise RuntimeError(f"no .tsv member in {file_name}: {zf.namelist()}")
            tsv_member = members[0]
            zf.extract(tsv_member, tmp_dir)
            tsv_path = os.path.join(tmp_dir, tsv_member)
        logger.info(
            "unzipped → %s (%d bytes)", tsv_path, Path(tsv_path).stat().st_size
        )

        # Free the zip immediately — disk hygiene.
        Path(zip_path).unlink(missing_ok=True)

        # Encoding sniff (L41) — transcode only if a non-UTF8 byte is found.
        transcoded_path = _sniff_and_transcode_utf8(tsv_path)
        read_path = transcoded_path

        # TSV data-row count for parity (count on the same bytes DuckDB reads).
        tsv_rows = _tsv_data_row_count(read_path)

        # DuckDB single-shot transcode → all-VARCHAR ZSTD Parquet.
        parquet_rows = _transcode_to_parquet(read_path, parquet_path)
        logger.info(
            "transcoded %s: tsv_rows=%d parquet_rows=%d → %s (%d bytes)",
            table_name, tsv_rows, parquet_rows, parquet_path,
            Path(parquet_path).stat().st_size,
        )

        # Row-count parity (L41) — >0.1% delta fails the table.
        if tsv_rows > 0:
            delta = abs(parquet_rows - tsv_rows) / tsv_rows
            if delta > ROW_COUNT_DELTA_TOLERANCE:
                raise RuntimeError(
                    f"row-count parity failed for {table_name}: "
                    f"tsv_rows={tsv_rows} parquet_rows={parquet_rows} "
                    f"delta={delta:.4%} > {ROW_COUNT_DELTA_TOLERANCE:.2%}"
                )
        elif parquet_rows != 0:
            raise RuntimeError(
                f"row-count parity failed for {table_name}: "
                f"tsv_rows=0 but parquet_rows={parquet_rows}"
            )

        # Upload to R2 — ContentType only, no ContentEncoding (L42).
        r2_key = (
            f"{R2_PREFIX_ROOT}/{slug}/{table_name}/"
            f"snapshot={snapshot_date}/data.parquet"
        )
        s3.upload_file(
            parquet_path, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, parquet_rows)
        return "completed"

    except Exception as exc:
        logger.exception("table %s failed", table_name)
        if run_id is not None:
            try:
                _record_run_failed(conn, run_id, str(exc))
            except Exception:
                logger.exception("could not record failed status for run %s", run_id)
        return "failed"
    finally:
        # Disk hygiene — delete every local artifact before the next table.
        for p in (zip_path, tsv_path, transcoded_path, parquet_path):
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                except OSError:
                    pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def ingest_data_dictionary(
    s3, slug: str, file_entry: dict
) -> None:
    """Download a product's data-dictionary PDF and preserve it to R2."""
    file_name: str = file_entry["fileName"]
    download_uri: str = file_entry["fileDownloadURI"]
    tmp_dir = tempfile.mkdtemp(prefix="uspto_dict_")
    local_path = os.path.join(tmp_dir, file_name)
    try:
        logger.info("downloading data dictionary %s", file_name)
        with requests.get(
            download_uri, headers=HTTP_HEADERS, timeout=300, stream=True
        ) as resp:
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        r2_key = f"{R2_PREFIX_ROOT}/{slug}/_data_dictionary/{file_name}"
        s3.upload_file(
            local_path, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        logger.info("uploaded data dictionary → s3://%s/%s", R2_BUCKET, r2_key)
    except Exception:
        # Non-fatal — the dictionary is a reference artifact, not table data.
        logger.exception("data dictionary %s failed (non-fatal)", file_name)
    finally:
        if Path(local_path).exists():
            Path(local_path).unlink(missing_ok=True)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def ingest_product(
    product_code: str,
    snapshot_date: datetime.date,
    table_filter: str | None,
    force: bool,
) -> dict:
    """Ingest all .tsv.zip tables for one PatentsView product. Returns a summary."""
    slug = PRODUCT_TO_SLUG[product_code]
    manifest = _fetch_manifest(product_code)
    files = _manifest_files(manifest)

    tsv_files = sorted(
        (f for f in files if f["fileName"].endswith(".tsv.zip")),
        key=lambda f: int(f.get("fileSize") or 0),
        reverse=True,  # largest-first
    )
    pdf_files = [f for f in files if f["fileName"].endswith(".pdf")]

    if table_filter:
        tsv_files = [
            f for f in tsv_files
            if f["fileName"][: -len(".tsv.zip")] == table_filter
        ]
        if not tsv_files:
            logger.warning(
                "table filter %r matched no .tsv.zip in product %s",
                table_filter, product_code,
            )

    logger.info(
        "product=%s slug=%s: %d .tsv.zip tables, %d data-dictionary PDFs",
        product_code, slug, len(tsv_files), len(pdf_files),
    )

    summary = {"completed": 0, "failed": 0, "skipped": 0, "failed_tables": []}
    conn = _pg_conn()
    s3 = _r2_client()
    try:
        for file_entry in tsv_files:
            result = ingest_table(
                conn, s3, slug, slug, file_entry, snapshot_date, force
            )
            summary[result] += 1
            if result == "failed":
                summary["failed_tables"].append(
                    file_entry["fileName"][: -len(".tsv.zip")]
                )

        # Data-dictionary PDFs (skip when a single-table smoke filter is set).
        if not table_filter:
            for pdf_entry in pdf_files:
                ingest_data_dictionary(s3, slug, pdf_entry)
    finally:
        conn.close()

    logger.info(
        "product=%s done: completed=%d failed=%d skipped=%d",
        product_code, summary["completed"], summary["failed"], summary["skipped"],
    )
    if summary["failed_tables"]:
        logger.warning("product=%s failed tables: %s", product_code, summary["failed_tables"])
    return summary


def ingest(
    products: list[str] | None = None,
    snapshot_date: datetime.date | None = None,
    table_filter: str | None = None,
    force: bool = False,
) -> dict:
    """Ingest one or more PatentsView products to R2.

    Called by the Modal app. `products` is a list of CLI product names
    ('granted' / 'pregrant'); defaults to both. Returns the aggregate summary.
    Raises RuntimeError at the end if any table failed — so the Modal cron run
    is marked red — after every table has been attempted.
    """
    if products is None:
        products = ["granted", "pregrant"]
    if snapshot_date is None:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()

    totals = {"completed": 0, "failed": 0, "skipped": 0, "failed_tables": []}
    for cli_product in products:
        product_code = CLI_PRODUCT_TO_CODE[cli_product]
        s = ingest_product(product_code, snapshot_date, table_filter, force)
        for k in ("completed", "failed", "skipped"):
            totals[k] += s[k]
        totals["failed_tables"].extend(s["failed_tables"])

    logger.info(
        "INGEST TOTAL: completed=%d failed=%d skipped=%d",
        totals["completed"], totals["failed"], totals["skipped"],
    )
    if totals["failed_tables"]:
        raise RuntimeError(
            f"{totals['failed']} table(s) failed: {totals['failed_tables']}. "
            f"Re-run picks them up via skip-if-unchanged."
        )
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(
        description="USPTO Patents (PatentsView bulk) → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--product",
        choices=["granted", "pregrant", "all"],
        default="all",
        help="Which PatentsView product to ingest (default: all)",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Optional single-table filter (table name without .tsv.zip), for smoke",
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date YYYY-MM-DD (default: today UTC)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore skip-if-unchanged — re-ingest every table",
    )
    args = parser.parse_args()

    if args.snapshot_date:
        snapshot_date = datetime.date.fromisoformat(args.snapshot_date)
    else:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()

    if args.product == "all":
        products = ["granted", "pregrant"]
    else:
        products = [args.product]

    logger.info(
        "products=%s table=%s snapshot_date=%s force=%s",
        products, args.table, snapshot_date, args.force,
    )
    ingest(products, snapshot_date, args.table, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
