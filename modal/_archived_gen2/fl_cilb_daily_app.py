"""FL CILB (Construction Industry Licensing Board) daily ingest — Modal cron.

HEAD-probes https://www2.myfloridalicense.com/sto/file_download/extracts//CONSTRUCTIONLICENSE_1.csv
for Last-Modified; compares to prior completed run; downloads + transcodes to
ZSTD Parquet if changed; uploads to R2; writes ops.fl_cilb_r2_ingest_runs ledger.

CSV schema (21 documented + 2 trailing undocumented cols = 23 total, per
Stage 1 probe 2026-05-18; all_varchar=TRUE per L9 / TRUE 1:1 column mirror):

    col 1:  board_number             (e.g. "06" = CILB)
    col 2:  occupation_code          (e.g. "CBC" = Certified Building Contractor)
    col 3:  licensee_name            (LAST, FIRST MIDDLE format)
    col 4:  doing_business_as_name
    col 5:  class_code
    col 6:  address_line_1
    col 7:  address_line_2
    col 8:  address_line_3
    col 9:  city
    col 10: state                    (always "FL" for FL CILB)
    col 11: zip
    col 12: county_code
    col 13: license_number           (canonical PK — numeric like "0015061")
    col 14: primary_status           (e.g. "C" Certified, "R" Registered)
    col 15: secondary_status         (e.g. "I" Inactive, "A" Active)
    col 16: original_licensure_date  (MM/DD/YYYY)
    col 17: effective_date           (MM/DD/YYYY)
    col 18: expiration_date          (MM/DD/YYYY)
    col 19: blank                    (documented as "Blank")
    col 20: renewal_period
    col 21: alternate_license_number (e.g. "CBC015061")
    col 22: col_22_unknown           (trailing undocumented; captured verbatim)

Trailing col 22 is captured verbatim per Source ingest invariant
§"TRUE 1:1 column mirror" — no silent column drops.
(Stage 1 probe noted col 23 as possible; live CSV confirmed exactly 22 cols 2026-05-18.)

Schedule: 0 11 * * * (11:00 UTC daily — after FL DBPR's observed ~10:47 UTC refresh).
Modal-native cron via @app.function(schedule=modal.Cron("0 11 * * *")).
NOT a Trigger.dev task (per FMCSA daily / USAspending daily precedents).

R2 output: s3://dex-raw-landing-zone/fl-cilb/release=YYYY-MM-DD/data.parquet
ContentType: application/x-parquet only (L42 — no extra encoding headers).

Directive: /Users/benjamincrane/Desktop/hq/directives/2026-05-18-fl-cilb-ingest-and-bridges.md
Precedent: modal/usaspending_api_daily_app.py (HEAD-probe + daily R2 landing pattern).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import modal

app = modal.App("data-engine-x-fl-cilb-daily")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "boto3",
        "duckdb",
        "psycopg[binary]",
        "requests",
        "pyarrow>=16.0",
    )
    .add_local_dir(
        Path(__file__).resolve().parent.parent / "scripts" / "dex",
        remote_path="/root/scripts",
    )
    .add_local_dir(
        Path(__file__).resolve().parent / "landing",
        remote_path="/root/landing",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("hqx-db"),
]

# FL DBPR bulk extract URL (double-slash in path is intentional per L43 probe)
FL_CILB_CSV_URL = (
    "https://www2.myfloridalicense.com/sto/file_download/extracts//CONSTRUCTIONLICENSE_1.csv"
)

R2_BUCKET = "dex-raw-landing-zone"
R2_KEY_TEMPLATE = "fl-cilb/release={release}/data.parquet"

# 23 columns: 21 documented + 2 trailing undocumented (Stage 1 probe 2026-05-18)
COLUMN_NAMES = [
    "board_number",
    "occupation_code",
    "licensee_name",
    "doing_business_as_name",
    "class_code",
    "address_line_1",
    "address_line_2",
    "address_line_3",
    "city",
    "state",
    "zip",
    "county_code",
    "license_number",
    "primary_status",
    "secondary_status",
    "original_licensure_date",
    "effective_date",
    "expiration_date",
    "blank",
    "renewal_period",
    "alternate_license_number",
    "col_22_unknown",
]

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _bridge_database_url() -> None:
    """Modal dex-db secret carries DATABASE_URL; bridge to DEX_DB_URL_DIRECT."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_DIRECT" not in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _r2_client():
    """Return configured boto3 S3 client for R2."""
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


_DBPR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; data-engine-x/1.0; "
        "+https://substrate.build)"
    )
}


def _head_probe_last_modified() -> str | None:
    """HEAD-probe the FL DBPR CSV URL; return Last-Modified header value or None."""
    import requests
    try:
        resp = requests.head(FL_CILB_CSV_URL, timeout=30, allow_redirects=True, headers=_DBPR_HEADERS)
        resp.raise_for_status()
        return resp.headers.get("Last-Modified")
    except Exception as exc:
        logger.warning("HEAD probe failed: %s", exc)
        return None


def _get_prior_completed_last_modified(db_url: str) -> str | None:
    """Query ops.fl_cilb_r2_ingest_runs for the most recent completed run's
    source_last_modified header value."""
    import psycopg
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_last_modified
                    FROM ops.fl_cilb_r2_ingest_runs
                    WHERE status = 'completed'
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as exc:
        logger.warning("Could not query prior runs: %s", exc)
        return None


def _insert_ledger_row(
    db_url: str,
    *,
    status: str,
    source_observed_at: datetime | None,
    source_last_modified: str | None,
    r2_prefix: str | None,
    r2_object_count: int | None,
    r2_total_bytes: int | None,
    parquet_row_count: int | None,
    started_at: datetime,
    completed_at: datetime | None,
    error_message: str | None,
) -> None:
    """INSERT a row into ops.fl_cilb_r2_ingest_runs."""
    import psycopg
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.fl_cilb_r2_ingest_runs (
                    status, source_observed_at, source_last_modified,
                    r2_prefix, r2_object_count, r2_total_bytes, parquet_row_count,
                    started_at, completed_at, error_message
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    status,
                    source_observed_at,
                    source_last_modified,
                    r2_prefix,
                    r2_object_count,
                    r2_total_bytes,
                    parquet_row_count,
                    started_at,
                    completed_at,
                    error_message,
                ),
            )
        conn.commit()
    logger.info("Ledger row inserted: status=%s", status)


def _columns_dict() -> str:
    """Build DuckDB columns={...} spec for all 22 named columns (all VARCHAR)."""
    entries = ", ".join(f"'{name}': 'VARCHAR'" for name in COLUMN_NAMES)
    return "{" + entries + "}"


def _run_ingest() -> dict:
    """Core ingest logic: HEAD-probe → compare → download + transcode → R2 upload → ledger."""
    import duckdb
    import requests

    _bridge_database_url()
    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        raise RuntimeError("DEX_DB_URL_DIRECT / DATABASE_URL not set")

    started_at = datetime.now(timezone.utc)
    release = date.today().isoformat()

    logger.info("FL CILB daily ingest started: release=%s", release)

    # HEAD-probe for Last-Modified
    last_modified = _head_probe_last_modified()
    logger.info("Last-Modified from HEAD probe: %s", last_modified)

    # Compare to prior completed run
    prior_last_modified = _get_prior_completed_last_modified(db_url)
    logger.info("Prior completed run Last-Modified: %s", prior_last_modified)

    if last_modified and prior_last_modified and last_modified == prior_last_modified:
        logger.info("Source unchanged (Last-Modified matches prior run) — writing no_change row")
        _insert_ledger_row(
            db_url,
            status="no_change",
            source_observed_at=started_at,
            source_last_modified=last_modified,
            r2_prefix=None,
            r2_object_count=None,
            r2_total_bytes=None,
            parquet_row_count=None,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            error_message=None,
        )
        return {"status": "no_change", "last_modified": last_modified}

    # Source changed (or no prior run) — proceed with download + ingest
    logger.info("Source changed or first run — proceeding with download")

    local_csv = f"/tmp/fl-cilb-{release}.csv"
    local_parquet = f"/tmp/fl-cilb-{release}.parquet"
    r2_key = R2_KEY_TEMPLATE.format(release=release)
    r2_prefix = f"fl-cilb/release={release}/"

    # Download CSV — FL DBPR blocks default python-requests UA; use _DBPR_HEADERS
    logger.info("Downloading CSV: %s", FL_CILB_CSV_URL)
    t_download = time.time()
    try:
        resp = requests.get(FL_CILB_CSV_URL, timeout=120, stream=True, headers=_DBPR_HEADERS)
        resp.raise_for_status()
        with open(local_csv, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
    except Exception as exc:
        logger.error("CSV download failed: %s", exc)
        _insert_ledger_row(
            db_url,
            status="failed",
            source_observed_at=started_at,
            source_last_modified=last_modified,
            r2_prefix=r2_prefix,
            r2_object_count=None,
            r2_total_bytes=None,
            parquet_row_count=None,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            error_message=f"download_failed: {exc}",
        )
        raise

    download_dur = time.time() - t_download
    csv_bytes = Path(local_csv).stat().st_size
    logger.info("Downloaded %.1f MB in %.1fs", csv_bytes / 1_048_576, download_dur)

    # DuckDB transcode: CSV → ZSTD Parquet (all_varchar=TRUE per L9)
    # 22 named columns via positional columns={...} dict (no header in file)
    logger.info("Transcoding CSV → ZSTD Parquet: %s", local_parquet)
    t_transcode = time.time()
    cols_dict = _columns_dict()
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
                SELECT *
                FROM read_csv(
                    '{local_csv}',
                    all_varchar=TRUE,
                    header=FALSE,
                    columns={cols_dict},
                    null_padding=TRUE,
                    strict_mode=FALSE
                )
            ) TO '{local_parquet}' (
                FORMAT PARQUET,
                COMPRESSION ZSTD,
                ROW_GROUP_SIZE 100000
            )
            """
        )
    except Exception as exc:
        logger.error("DuckDB transcode failed: %s", exc)
        _insert_ledger_row(
            db_url,
            status="failed",
            source_observed_at=started_at,
            source_last_modified=last_modified,
            r2_prefix=r2_prefix,
            r2_object_count=None,
            r2_total_bytes=None,
            parquet_row_count=None,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            error_message=f"transcode_failed: {exc}",
        )
        raise

    transcode_dur = time.time() - t_transcode
    parquet_row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{local_parquet}')"
    ).fetchone()[0]
    parquet_bytes = Path(local_parquet).stat().st_size
    logger.info(
        "Transcoded: %d rows, %.1f MB Parquet in %.1fs",
        parquet_row_count, parquet_bytes / 1_048_576, transcode_dur,
    )
    con.close()

    # Upload to R2 — ContentType only; L42 prohibits extra encoding headers
    logger.info("Uploading to R2: s3://%s/%s", R2_BUCKET, r2_key)
    t_upload = time.time()
    try:
        s3 = _r2_client()
        s3.upload_file(
            local_parquet,
            R2_BUCKET,
            r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
    except Exception as exc:
        logger.error("R2 upload failed: %s", exc)
        _insert_ledger_row(
            db_url,
            status="failed",
            source_observed_at=started_at,
            source_last_modified=last_modified,
            r2_prefix=r2_prefix,
            r2_object_count=None,
            r2_total_bytes=None,
            parquet_row_count=parquet_row_count,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            error_message=f"r2_upload_failed: {exc}",
        )
        raise

    upload_dur = time.time() - t_upload
    logger.info("R2 upload done in %.1fs", upload_dur)

    # Write completed ledger row
    _insert_ledger_row(
        db_url,
        status="completed",
        source_observed_at=started_at,
        source_last_modified=last_modified,
        r2_prefix=r2_prefix,
        r2_object_count=1,
        r2_total_bytes=parquet_bytes,
        parquet_row_count=parquet_row_count,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        error_message=None,
    )

    result = {
        "status": "completed",
        "release": release,
        "last_modified": last_modified,
        "parquet_row_count": parquet_row_count,
        "parquet_bytes": parquet_bytes,
        "r2_key": r2_key,
        "download_dur_s": round(download_dur, 1),
        "transcode_dur_s": round(transcode_dur, 1),
        "upload_dur_s": round(upload_dur, 1),
    }
    logger.info("Ingest complete: %s", result)
    return result


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=3600,
    memory=4096,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 11 * * *"),
)
def run_daily() -> dict:
    """Daily FL CILB ingest: HEAD-probe → download if changed → ZSTD Parquet → R2."""
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_daily",
        run_id=run_id,
    ) as hb:
        hb.set_stage("fl_cilb_ingest")
        result = _run_ingest()
    if isinstance(result, dict):
        result["heartbeat_run_id"] = run_id
    return result


@app.local_entrypoint()
def run() -> None:
    """`modal run modal/fl_cilb_daily_app.py::run` — one-shot manual trigger."""
    import json
    out = run_daily.remote()
    print(json.dumps(out, indent=2, default=str))
