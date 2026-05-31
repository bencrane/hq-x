"""Compute worker — SAM.gov Daily Contract Opportunities (active) bulk ingest.

Part of the ``sam-gov-pipelines`` Modal app. NOT directly exposed — it is
spawned by the Universal Dispatcher (core/modal_dispatcher.py), which is the
only proxy-authed endpoint in the fleet. This worker has no web endpoint.

Data plane (unchanged, clean-room — no Iceberg, no Polaris, no legacy code):
    SAM.gov fileextractservices CSV
      → requests stream      → /tmp/sam_opps_full.csv     (Python: I/O only)
      → DuckDB read_csv        → clean / cast / filter / shape   (100% in SQL)
      → Arrow table
      → lance.write_dataset(s3://sam-gov-opps/active/, data_storage_version="2.0")

Control plane (Trigger v4 durable callback):
    The worker accepts ``trigger_callback_url`` (the pre-signed waitpoint URL
    minted by the Trigger task). On terminal state — success OR failure — it
    (1) writes the run row to ``ops.sam_opps_canonical_runs`` via psycopg and
    (2) POSTs ``{status, rows, feed}`` to ``trigger_callback_url`` to wake the
    suspended Trigger run. Trigger therefore owns true end-to-end success/
    failure state; no polling, no heartbeat.

    modal run    scripts/ingest/sam_gov/sam_opps_bulk_canonical.py   # manual (no callback)
    modal deploy scripts/ingest/sam_gov/sam_opps_bulk_canonical.py
"""

from __future__ import annotations

import os

import modal

# Public SAM.gov Contract Opportunities daily full extract. Unauthenticated
# fileextractservices download (the data-services API key gates only the
# *lookup* variant, which this canonical bulk path deliberately avoids).
# Overridable via env so the resolver is not pinned to one host/path.
DEFAULT_SAM_OPPS_CSV_URL = (
    "https://sam.gov/api/prod/fileextractservices/v1/api/download/"
    "Contract%20Opportunities/datagov/ContractOpportunitiesFullCSV.csv"
    "?privacy=Public"
)

# Lance dataset URI — system-of-record tier (NOT the raw landing zone).
LANCE_BASE_URI = os.environ.get("SAM_OPPS_LANCE_URI", "s3://sam-gov-opps/active/")

# Local fast scratchpad on the container's ephemeral NVMe disk.
SCRATCH_CSV_PATH = "/tmp/sam_opps_full.csv"

FEED = "sam_opps_active"

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    # Lower bounds, not exact pins — freeze with `modal shell` once validated.
    "duckdb>=1.1",
    "lancedb>=0.15",
    "pylance>=0.19",        # provides `import lance`; lancedb does not re-export it
    "pyarrow>=17",
    "requests>=2.32",
    "psycopg[binary]>=3.2",  # terminal state write to ops.*
)

app = modal.App("sam-gov-pipelines", image=image)


# DuckDB does 100% of the transform. Reading with all_varchar=true means zero
# type-inference surprises on a 40+ column government CSV; every cast below is
# explicit and defensive (TRY_CAST → NULL, never a hard parse failure). The
# `__CSV_PATH__` token is substituted with the controlled /tmp path at call
# time (our own path, no user input — no injection surface).
#
# Column identifiers follow SAM.gov's published `ContractOpportunitiesFullCSV`
# header. If SAM revises a header, adjust the quoted name on that one line.
TRANSFORM_SQL = """
WITH raw AS (
    SELECT *
    FROM read_csv(
        '__CSV_PATH__',
        all_varchar = true,
        header = true,
        sample_size = -1,
        ignore_errors = false
    )
)
SELECT
    nullif(trim("NoticeId"), '')                 AS notice_id,
    nullif(trim("Title"), '')                    AS title,
    nullif(trim("Sol#"), '')                     AS solicitation_number,
    nullif(trim("Department/Ind.Agency"), '')    AS department_agency,
    nullif(trim("Office"), '')                   AS office,
    TRY_CAST("PostedDate" AS TIMESTAMP)          AS posted_date,
    nullif(trim("Type"), '')                     AS notice_type,
    nullif(trim("BaseType"), '')                 AS base_type,
    nullif(trim("ArchiveType"), '')              AS archive_type,
    TRY_CAST("ArchiveDate" AS DATE)              AS archive_date,
    nullif(trim("SetASideCode"), '')             AS set_aside_code,
    nullif(trim("SetASide"), '')                 AS set_aside,
    TRY_CAST("ResponseDeadLine" AS TIMESTAMPTZ)  AS response_deadline,
    nullif(trim("NaicsCode"), '')                AS naics_code,
    nullif(trim("ClassificationCode"), '')       AS classification_code,
    nullif(trim("PopCity"), '')                  AS pop_city,
    nullif(trim("PopState"), '')                 AS pop_state,
    nullif(trim("PopZip"), '')                   AS pop_zip,
    nullif(trim("PopCountry"), '')               AS pop_country,
    nullif(trim("AwardNumber"), '')              AS award_number,
    TRY_CAST("AwardDate" AS DATE)                AS award_date,
    TRY_CAST(replace(replace("Award$", '$', ''), ',', '') AS DOUBLE) AS award_amount,
    nullif(trim("Awardee"), '')                  AS awardee,
    nullif(trim("Link"), '')                     AS link,
    nullif(trim("Description"), '')              AS description,
    CAST('__SNAPSHOT_DATE__' AS DATE)            AS snapshot_date
FROM raw
WHERE upper(trim("Active")) = 'YES'
"""


def _r2_storage_options() -> dict[str, str]:
    """object_store options for Cloudflare R2, sourced from the Modal secret.

    Per Lance's R2 guidance: AWS-style credentials + an explicit ``endpoint``
    and ``region`` ("auto" for R2). The endpoint is either supplied directly
    (``R2_ENDPOINT``) or derived from the account id.
    """
    endpoint = os.environ.get("R2_ENDPOINT")
    account_id = os.environ.get("R2_ACCOUNT_ID")
    if not endpoint and account_id:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    if not endpoint:
        raise RuntimeError("Set R2_ENDPOINT or R2_ACCOUNT_ID in the Modal secret.")
    return {
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "endpoint": endpoint,
        "region": "auto",
    }


def _record_run(feed, rows, status, started_at, completed_at, error) -> None:
    """Terminal run row → ops.sam_opps_canonical_runs (psycopg). Best-effort:
    never let an audit-write failure crash an otherwise-good ingest."""
    import psycopg

    dsn = os.environ.get("HQX_DB_URL_POOLED")
    if not dsn:
        print("WARN: HQX_DB_URL_POOLED not set; skipping ops.* state write.")
        return
    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.sam_opps_canonical_runs
                    (feed, rows_processed, status, error, started_at, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (feed, rows, status, error, started_at, completed_at),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — audit must not mask the ingest
        print(f"WARN: ops.* state write failed: {exc}")


def _post_callback(url, payload, attempts: int = 3) -> None:
    """POST terminal metadata to the Trigger waitpoint URL. A few retries for
    delivery reliability (this is the only thing that wakes Trigger) — not a
    polling loop."""
    if not url:
        print("No trigger_callback_url (manual run); skipping callback.")
        return
    import time

    import requests

    for i in range(attempts):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code < 300:
                print(f"Callback delivered: {payload}")
                return
            print(f"Callback attempt {i + 1} non-2xx: {resp.status_code} {resp.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            print(f"Callback attempt {i + 1} failed: {exc}")
        time.sleep(2 * (i + 1))
    print(f"WARN: callback delivery failed after {attempts} attempts → {url}")


@app.function(
    secrets=[
        modal.Secret.from_name("r2-credentials"),
        modal.Secret.from_name("hqx-postgres"),
    ],
    timeout=60 * 30,            # 30-min ceiling for the bulk job
    memory=8192,
    cpu=4.0,
)
def ingest_sam_opps_bulk(
    trigger_callback_url: str | None = None,
    csv_url: str | None = None,
    mode: str = "overwrite",
) -> dict:
    """Download → DuckDB transform → Lance write, then record state + wake Trigger.

    ``mode="overwrite"`` is canonical: a daily full snapshot of *currently-active*
    notices, so the Lance dataset always reflects today's active set; Lance's
    internal versioning retains prior snapshots for time-travel.

    Terminal behaviour (both success and failure): write the ops.* run row and
    POST ``{status, rows, feed}`` to ``trigger_callback_url``. On failure the
    function also re-raises so the Modal call itself is marked failed.
    """
    import datetime as dt

    import duckdb
    import lance

    import requests

    started_at = dt.datetime.now(dt.timezone.utc)
    snapshot_date = started_at.date().isoformat()
    url = csv_url or os.environ.get("SAM_OPPS_CSV_URL", DEFAULT_SAM_OPPS_CSV_URL)

    rows = 0
    final_status = "error"
    error_text: str | None = None

    try:
        # 1) Python: stream the bulk payload to fast local scratch. No parsing.
        #    SAM exports are Windows-1252 (cp1252), not UTF-8 — DuckDB only
        #    speaks utf-8/utf-16/latin-1 and rejects cp1252's 0x80-0x9F
        #    printables (smart quotes, em dashes that fill the description
        #    fields). Transcode to UTF-8 on the way to disk: an I/O concern,
        #    not data transformation (the SQL still does 100% of the transform),
        #    and lossless — cp1252 is single-byte so chunk boundaries never
        #    split a character, and no rows are dropped.
        with requests.get(url, stream=True, timeout=(30, 600)) as resp:
            resp.raise_for_status()
            with open(SCRATCH_CSV_PATH, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk.decode("cp1252", errors="replace").encode("utf-8"))

        # 2) DuckDB: 100% of the transform, reading the file directly off disk.
        sql = TRANSFORM_SQL.replace("__CSV_PATH__", SCRATCH_CSV_PATH).replace(
            "__SNAPSHOT_DATE__", snapshot_date
        )
        con = duckdb.connect(":memory:")
        try:
            con.execute("PRAGMA threads=4;")
            arrow_table = con.execute(sql).fetch_arrow_table()
        finally:
            con.close()

        rows = arrow_table.num_rows

        # 3) Python: commit the Arrow table to R2 as Lance, forcing 2.0 format.
        lance.write_dataset(
            arrow_table,
            LANCE_BASE_URI,
            mode=mode,
            data_storage_version="2.0",
            storage_options=_r2_storage_options(),
        )
        final_status = "success"
    except Exception as exc:  # noqa: BLE001 — terminal handling below + re-raise
        error_text = str(exc)
        final_status = "error"
    finally:
        completed_at = dt.datetime.now(dt.timezone.utc)
        _record_run(FEED, int(rows), final_status, started_at, completed_at, error_text)
        _post_callback(
            trigger_callback_url,
            {"status": final_status, "rows": int(rows), "feed": FEED},
        )

    if final_status != "success":
        raise RuntimeError(f"sam_opps ingest failed: {error_text}")

    return {"feed": FEED, "rows_processed": int(rows), "status": final_status}


@app.local_entrypoint()
def main(mode: str = "overwrite") -> None:
    # Manual run: no callback URL (callback is skipped); ops.* write still
    # fires if the hqx-postgres secret is attached.
    print(ingest_sam_opps_bulk.remote(trigger_callback_url=None, mode=mode))
