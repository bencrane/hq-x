"""TXDOT Construction Letting monthly Socrata → R2 ZSTD Parquet ingest.

Source: https://data.texas.gov/resource/de7b-7dna (Bid Tabulations, 24-mo rolling).
Bulk CSV: https://data.texas.gov/api/views/de7b-7dna/rows.csv?accessType=DOWNLOAD
Metadata: https://data.texas.gov/api/views/de7b-7dna.json (rowsUpdatedAt unix ts).
Row-count baseline: https://data.texas.gov/resource/de7b-7dna.json?$select=count(*)
R2 output: s3://dex-raw-landing-zone/txstate/txdot_letting/snapshot={YYYY-MM-DD}/data.parquet

Key invariants (per DEX CLAUDE.md):
  L9:  read_csv_auto(..., all_varchar=TRUE) — no type coercion at ingest
  L41: cp1252 sniff + iconv -f WINDOWS-1252 -t UTF-8 fallback; row-count parity post-write
  L42: ExtraArgs={"ContentType": "application/x-parquet"} ONLY — no Content-Encoding header;
       plain .parquet extension only
  L47: Modal entrypoint designed for --detach invocation

Idempotency via Socrata rowsUpdatedAt vs ops.txdot_letting_r2_ingest_runs.source_observed_at.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/txdot_letting_ingest_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/txdot_letting_ingest_app.py::main --snapshot-date 2026-05-18
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import modal

app = modal.App("data-engine-x-txdot-letting-ingest")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("procps")
    .run_commands("apt-get install -y iconv || apt-get install -y libc-bin")  # L41 cp1252 transcode
    .pip_install("duckdb", "boto3", "httpx", "psycopg[binary]", "fastapi[standard]")
    .add_local_dir(
        Path(__file__).resolve().parent / "landing",
        remote_path="/root/landing",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("hqx-db"),  # DEX_DB_URL_DIRECT for ledger writes
]

SOCRATA_DATASET_ID = "de7b-7dna"
SOCRATA_METADATA_URL = f"https://data.texas.gov/api/views/{SOCRATA_DATASET_ID}.json"
SOCRATA_CSV_URL = (
    f"https://data.texas.gov/api/views/{SOCRATA_DATASET_ID}/rows.csv?accessType=DOWNLOAD"
)
SOCRATA_COUNT_URL = (
    f"https://data.texas.gov/resource/{SOCRATA_DATASET_ID}.json?$select=count(*)"
)
R2_BUCKET = "dex-raw-landing-zone"
R2_KEY_TEMPLATE = "txstate/txdot_letting/snapshot={snapshot_date}/data.parquet"
SOURCE_PROVIDER = "txdot_socrata_de7b_7dna"

PARITY_TOLERANCE = 0.001  # 0.1% per L41


def _get_db_url() -> str:
    """Resolve DEX direct DB URL from environment (Modal injects via dex-db)."""
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DEX_DB_URL_DIRECT (or DATABASE_URL) not set in environment")
    return url


def _insert_run_row(
    run_id: uuid.UUID,
    snapshot_date: str,
    started_at: datetime,
    status: str = "pending",
) -> None:
    """Insert a new pending audit row into ops.txdot_letting_r2_ingest_runs."""
    import psycopg

    db_url = _get_db_url()
    with psycopg.connect(db_url) as conn:
        conn.execute(
            """
            INSERT INTO ops.txdot_letting_r2_ingest_runs
                (run_id, source_provider, source_download_url, started_at, status)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                str(run_id),
                SOURCE_PROVIDER,
                SOCRATA_CSV_URL,
                started_at,
                status,
            ),
        )
        conn.commit()


def _update_run(
    run_id: uuid.UUID,
    *,
    status: str,
    source_observed_at: datetime | None = None,
    completed_at: datetime | None = None,
    parquet_row_count: int | None = None,
    r2_prefix: str | None = None,
    r2_object_count: int | None = None,
    r2_total_bytes: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update an existing audit row."""
    import psycopg

    db_url = _get_db_url()
    with psycopg.connect(db_url) as conn:
        conn.execute(
            """
            UPDATE ops.txdot_letting_r2_ingest_runs
            SET status              = %s,
                source_observed_at  = COALESCE(%s, source_observed_at),
                completed_at        = COALESCE(%s, completed_at),
                parquet_row_count   = COALESCE(%s, parquet_row_count),
                r2_prefix           = COALESCE(%s, r2_prefix),
                r2_object_count     = COALESCE(%s, r2_object_count),
                r2_total_bytes      = COALESCE(%s, r2_total_bytes),
                error_message       = COALESCE(%s, error_message)
            WHERE run_id = %s
            """,
            (
                status,
                source_observed_at,
                completed_at,
                parquet_row_count,
                r2_prefix,
                r2_object_count,
                r2_total_bytes,
                error_message,
                str(run_id),
            ),
        )
        conn.commit()


def _latest_succeeded_observed_at() -> datetime | None:
    """Return the most-recent source_observed_at from a completed or no_change run, or None."""
    import psycopg

    db_url = _get_db_url()
    with psycopg.connect(db_url) as conn:
        row = conn.execute(
            """
            SELECT source_observed_at
            FROM ops.txdot_letting_r2_ingest_runs
            WHERE source_provider = %s
              AND status IN ('completed', 'no_change')
              AND source_observed_at IS NOT NULL
            ORDER BY source_observed_at DESC
            LIMIT 1
            """,
            (SOURCE_PROVIDER,),
        ).fetchone()
    return row[0] if row else None


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=3600,
    memory=8192,
    cpu=2,
)
def ingest_socrata_to_r2(snapshot_date: str) -> dict:
    """Entrypoint: fetch Socrata bulk CSV, transcode cp1252→utf-8 if needed,
    DuckDB → ZSTD Parquet, upload to R2 with ContentType only (L42), write
    ops.txdot_letting_r2_ingest_runs audit row.

    Called with --detach per L47:
        modal run --detach modal/txdot_letting_ingest_app.py::main --snapshot-date 2026-05-18
    """
    import boto3
    import duckdb
    import httpx
    import sys as _sys

    _sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    # 1. Insert pending audit row.
    run_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    _insert_run_row(run_id, snapshot_date, started_at, status="pending")

    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="ingest_socrata_to_r2",
        run_id=str(run_id),
    )
    hb_cm.__enter__()
    hb_cm.set_stage("socrata_metadata_probe", {"snapshot_date": snapshot_date})

    try:
        # 2. Fetch Socrata metadata → rowsUpdatedAt unix ts.
        meta = httpx.get(SOCRATA_METADATA_URL, timeout=30).json()
        rows_updated_at = meta.get("rowsUpdatedAt")
        if rows_updated_at is None:
            raise RuntimeError(
                f"Socrata metadata missing 'rowsUpdatedAt': {list(meta.keys())[:10]}"
            )
        observed_at = datetime.fromtimestamp(int(rows_updated_at), tz=timezone.utc)

        # 3. Idempotency check vs prior succeeded run.
        prior_observed = _latest_succeeded_observed_at()
        if prior_observed and observed_at <= prior_observed:
            _update_run(
                run_id,
                status="no_change",
                source_observed_at=observed_at,
                completed_at=datetime.now(timezone.utc),
            )
            hb_cm.__exit__(None, None, None)
            return {
                "status": "no_change",
                "source_observed_at": observed_at.isoformat(),
            }

        _update_run(run_id, status="running", source_observed_at=observed_at)
        hb_cm.set_stage("csv_download")

        # 4. Fetch CSV (stream to /tmp).
        csv_path = f"/tmp/txdot-letting-{snapshot_date}.csv"
        with httpx.stream("GET", SOCRATA_CSV_URL, timeout=600.0) as r:
            r.raise_for_status()
            with open(csv_path, "wb") as f:
                for chunk in r.iter_bytes(1024 * 1024):
                    f.write(chunk)

        # 5. cp1252 sniff (L41): probe first 4KB; transcode in-place via iconv if non-UTF8 byte found.
        with open(csv_path, "rb") as fh:
            probe = fh.read(4096)
        try:
            probe.decode("utf-8")
            csv_utf8_path = csv_path
        except UnicodeDecodeError:
            csv_utf8_path = csv_path + ".utf8"
            subprocess.check_call(
                ["iconv", "-f", "WINDOWS-1252", "-t", "UTF-8", csv_path, "-o", csv_utf8_path]
            )

        # 6. DuckDB CSV → ZSTD Parquet (single-shot). L9: all_varchar=TRUE.
        hb_cm.set_stage("duckdb_to_parquet")
        parquet_path = f"/tmp/txdot-letting-{snapshot_date}.parquet"
        con = duckdb.connect()
        con.execute(
            f"""
            COPY (SELECT * FROM read_csv_auto('{csv_utf8_path}', all_varchar=TRUE))
            TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
            """
        )
        parquet_row_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')"
        ).fetchone()[0]

        # 7. Row-count parity vs Socrata $select=count(*) baseline (L41 >0.1% delta = fail).
        baseline_resp = httpx.get(SOCRATA_COUNT_URL, timeout=30).json()
        baseline_count = int(baseline_resp[0]["count"])
        drift = abs(parquet_row_count - baseline_count) / max(baseline_count, 1)
        if drift > PARITY_TOLERANCE:
            _update_run(
                run_id,
                status="failed",
                error_message=f"row-count drift {drift:.3%} > {PARITY_TOLERANCE:.1%}",
                parquet_row_count=parquet_row_count,
                completed_at=datetime.now(timezone.utc),
            )
            hb_cm.__exit__(None, None, None)
            raise RuntimeError(f"row-count drift {drift:.3%} exceeds tolerance")

        # 8. Upload to R2. ExtraArgs with ContentType only per L42. Plain .parquet extension.
        hb_cm.set_stage("r2_upload")
        r2_key = R2_KEY_TEMPLATE.format(snapshot_date=snapshot_date)
        size = os.path.getsize(parquet_path)
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        with open(parquet_path, "rb") as fh:
            s3.upload_fileobj(
                fh,
                Bucket=R2_BUCKET,
                Key=r2_key,
                ExtraArgs={"ContentType": "application/x-parquet"},  # L42: ContentType ONLY
            )

        _update_run(
            run_id,
            status="completed",
            parquet_row_count=parquet_row_count,
            r2_prefix=f"s3://{R2_BUCKET}/{r2_key.rsplit('/', 1)[0]}/",
            r2_object_count=1,
            r2_total_bytes=size,
            completed_at=datetime.now(timezone.utc),
        )
        hb_cm.__exit__(None, None, None)
        return {
            "status": "completed",
            "rows": parquet_row_count,
            "r2_key": r2_key,
            "size_bytes": size,
        }
    except Exception as exc:
        _update_run(
            run_id,
            status="failed",
            error_message=repr(exc),
            completed_at=datetime.now(timezone.utc),
        )
        hb_cm.__exit__(None, None, None)
        raise


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60,
)
@modal.fastapi_endpoint(method="POST")
def trigger_ingest_via_http(snapshot_date: str | None = None) -> dict:
    """HTTP-triggerable wrapper that spawns the ingest async.

    Modal-issued stable URL (post-deploy):
        https://bencrane--data-engine-x-txdot-letting-ingest-trigger-ing-5a912b.modal.run

    Trigger.dev cron (`apps/hq-x/src/trigger/txdot-letting-monthly.ts`) POSTs to
    this URL with `?snapshot_date=YYYY-MM-DD` query parameter; this function
    calls `ingest_socrata_to_r2.spawn(snapshot_date)` and returns the call_id
    immediately (async invocation; the ingest itself runs in its own container
    per the 3600s timeout on ingest_socrata_to_r2).

    Auth: Modal Web Functions are public to the internet (per Modal docs).
    The Socrata source is also public, so no auth is required for this
    endpoint beyond Modal's platform-level rate-limit + DDoS protection.

    Verified working 2026-05-18T06:30Z probe returned
    {"call_id":"fc-01KRWWCKN6W0YNXWEG73BCDTVH","snapshot_date":"2026-05-18"}.
    """
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    call = ingest_socrata_to_r2.spawn(snapshot_date)
    return {"call_id": call.object_id, "snapshot_date": snapshot_date}


@app.local_entrypoint()
def main(snapshot_date: str | None = None) -> None:
    """`modal run --detach apps/data-engine-x/modal/txdot_letting_ingest_app.py::main --snapshot-date 2026-05-18`"""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = ingest_socrata_to_r2.remote(snapshot_date)
    print(json.dumps(result, indent=2, default=str))
