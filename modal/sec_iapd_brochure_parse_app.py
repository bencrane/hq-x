"""SEC IAPD Form ADV Part 2 brochure parse — Stage A Modal app.

Tier 1 (pdfplumber per-page text + table extraction) and Tier 2 (regex
section split on 18 SEC-mandated Item headers) main pass over the 17,525
PDFs preserved in R2 by Phase 1 (PR #318).

Stage A outputs:
  - R2 intermediate Parquet at:
    s3://dex-raw-landing-zone/sec-iapd/form-adv-part-2-parsed/release=YYYY-MM-DD/stage_a.parquet
  - Audit ledger rows in ops.sec_iapd_brochure_parse_runs

Low-confidence rows (section_split_confidence < 14) are flagged for Tier 2
fallback via dispatch_sec_iapd_tier2_fallback.py (runs in-session, not on
Modal, using Claude Code Max session quota per directive).

Secrets: canonical 2-secret bundle per auditor override (bulk-ingest-r2 + dex-db):
    bulk-ingest-r2     -- R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    dex-db    -- DEX_DB_URL_DIRECT

Deploy:
    cd /Users/benjamincrane/hq-all && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy apps/data-engine-x/modal/sec_iapd_brochure_parse_app.py

Launch Stage A (detached per L47 — expected ~60 min wall-clock at parallel-50):
    cd /Users/benjamincrane/hq-all/.claude/worktrees/interesting-hawking-a8b507 && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach apps/data-engine-x/modal/sec_iapd_brochure_parse_app.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from typing import Any

import modal

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = modal.App("data-engine-x-sec-iapd-brochure-parse")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libpoppler-cpp-dev", "poppler-utils")
    .pip_install(
        "pdfplumber>=0.10",
        "pyarrow>=18",
        "boto3>=1.40",
        "psycopg[binary]>=3.2",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

# Canonical 2-secret bundle per auditor override (see directive ## Audit plan §"Modal secrets correction").
FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),    # R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    modal.Secret.from_name("hqx-db"),   # DEX_DB_URL_DIRECT
]

PARSER_VERSION = "1.1.0"
R2_BUCKET = "dex-raw-landing-zone"
LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Per-PDF parse function
# --------------------------------------------------------------------------- #

# retry-policy: modal-retries-transient
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    cpu=2.0,
    memory=4096,
    retries=modal.Retries(max_retries=3, backoff_coefficient=2.0),
    timeout=600,
)
def parse_one(task: dict) -> dict:
    """Per-PDF task: boto3 download -> pdfplumber -> regex split -> return dict.

    Input task keys: crd_number, version_id, brochure_type, filing_date,
                     r2_key, content_sha256, content_bytes, parse_run_id.
    Output: flat dict with page_count, full_text_md, item_1..item_18,
            section_split_confidence, section_split_method, parser_version,
            parsed_at, status ('completed'|'failed'), error_message.
    """
    import sys
    import tempfile

    # Ensure scripts/_lib is importable from the mounted /root/scripts dir.
    sys.path.insert(0, "/root")

    import boto3
    from scripts._lib.sec_iapd_brochure_parse import (
        ITEM_TITLES,
        extract_text_and_tables,
        score_section_confidence,
        split_sections_regex,
    )

    crd = task["crd_number"]
    ver = task["version_id"]
    r2_key = task["r2_key"]
    parse_run_id = task["parse_run_id"]

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    result: dict[str, Any] = {
        **task,
        "parser_version": PARSER_VERSION,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "error_message": None,
    }

    tmp_pdf = f"/tmp/{crd}_{ver.replace('/', '_')}.pdf"
    try:
        # 1. boto3 download r2_key -> /tmp/
        s3.download_file(R2_BUCKET, r2_key, tmp_pdf)

        # 2. Tier 1: pdfplumber per-page text + table extraction
        full_text_md, _tables = extract_text_and_tables(tmp_pdf)
        result["page_count"] = full_text_md.count("\x0c") + 1 if full_text_md else 0
        result["full_text_md"] = full_text_md

        # 3. Tier 2 main pass: regex section split
        items_dict = split_sections_regex(full_text_md)
        confidence = score_section_confidence(items_dict)
        result["section_split_confidence"] = confidence
        result["section_split_method"] = "regex" if confidence >= 14 else "regex_partial"

        # 4. Populate item_N columns (None for missing items)
        for n, slug in ITEM_TITLES.items():
            col = f"item_{n}_{slug}"
            result[col] = items_dict.get(n)

        result["status"] = "completed"

    except Exception as exc:
        result["error_message"] = str(exc)[:4000]
        result["status"] = "failed"
        # Ensure item columns exist even on failure
        for n, slug in ITEM_TITLES.items():
            col = f"item_{n}_{slug}"
            result.setdefault(col, None)
        result.setdefault("page_count", 0)
        result.setdefault("full_text_md", None)
        result.setdefault("section_split_confidence", 0)
        result.setdefault("section_split_method", "regex_partial")
    finally:
        try:
            os.unlink(tmp_pdf)
        except OSError:
            pass

    return result


# --------------------------------------------------------------------------- #
# Local entrypoint — discovers unparsed rows, maps, writes stage_a.parquet
# --------------------------------------------------------------------------- #

@app.local_entrypoint()
def run() -> None:
    """`modal run --detach apps/data-engine-x/modal/sec_iapd_brochure_parse_app.py::run`"""
    import json
    import tempfile

    import boto3
    import psycopg
    import pyarrow as pa
    import pyarrow.parquet as pq

    LOG.info("Stage A: discovering unparsed (crd_number, version_id) pairs ...")
    db_url = os.environ["DEX_DB_URL_DIRECT"]

    with psycopg.connect(db_url) as conn:
        rows = conn.execute(
            """
            SELECT s.crd_number, s.version_id, s.brochure_type,
                   s.filing_date::text, s.r2_key, s.content_sha256,
                   s.content_bytes
              FROM ops.sec_iapd_form_adv_part_2_brochure_scrape_runs s
              LEFT JOIN ops.sec_iapd_brochure_parse_runs p
                ON  p.crd_number = s.crd_number
                AND p.version_id = s.version_id
                AND p.parser_version = %s
                AND p.status = 'completed'
             WHERE s.status = 'completed'
               AND p.run_id IS NULL
            """,
            (PARSER_VERSION,),
        ).fetchall()

    LOG.info("  %d PDFs to parse", len(rows))

    if not rows:
        LOG.info("  nothing to do — all PDFs already parsed at version %s", PARSER_VERSION)
        return

    # Pre-insert pending ledger rows so failed tasks are tracked
    pending_run_ids: dict[tuple, str] = {}
    with psycopg.connect(db_url) as conn:
        for r in rows:
            crd_number, version_id, brochure_type, filing_date_str, r2_key, content_sha256, content_bytes = r
            run_id = str(uuid.uuid4())
            pending_run_ids[(crd_number, version_id)] = run_id
            conn.execute(
                """
                INSERT INTO ops.sec_iapd_brochure_parse_runs
                    (run_id, crd_number, version_id, brochure_type, filing_date,
                     r2_key, parser_version, status, started_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', now())
                ON CONFLICT (crd_number, version_id, parser_version) DO UPDATE
                    SET status = 'running', started_at = now()
                """,
                (run_id, crd_number, version_id, brochure_type, filing_date_str, r2_key, PARSER_VERSION),
            )
        conn.commit()

    tasks = [
        {
            "crd_number": r[0],
            "version_id": r[1],
            "brochure_type": r[2],
            "filing_date": r[3],
            "r2_key": r[4],
            "content_sha256": r[5],
            "content_bytes": r[6],
            "parse_run_id": pending_run_ids[(r[0], r[1])],
        }
        for r in rows
    ]

    LOG.info("  dispatching %d tasks via .map() ...", len(tasks))
    results = list(parse_one.map(tasks, return_exceptions=True))
    LOG.info("  .map() complete — %d results returned", len(results))

    # Aggregate results
    good: list[dict] = []
    failed_count = 0
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            # Map-level exception (Modal infra failure) — mark as failed
            failed_count += 1
            task = tasks[i]
            key = (task["crd_number"], task["version_id"])
            with psycopg.connect(db_url) as conn:
                conn.execute(
                    """
                    UPDATE ops.sec_iapd_brochure_parse_runs
                       SET status = 'failed', completed_at = now(),
                           error_message = %s
                     WHERE crd_number = %s AND version_id = %s
                       AND parser_version = %s
                    """,
                    (str(res)[:4000], task["crd_number"], task["version_id"], PARSER_VERSION),
                )
                conn.commit()
        else:
            good.append(res)
            status = res.get("status", "failed")
            with psycopg.connect(db_url) as conn:
                conn.execute(
                    """
                    UPDATE ops.sec_iapd_brochure_parse_runs
                       SET status = %s, completed_at = now(),
                           section_split_confidence = %s,
                           section_split_method = %s,
                           page_count = %s,
                           error_message = %s
                     WHERE crd_number = %s AND version_id = %s
                       AND parser_version = %s
                    """,
                    (
                        status,
                        res.get("section_split_confidence"),
                        res.get("section_split_method"),
                        res.get("page_count"),
                        res.get("error_message"),
                        res["crd_number"],
                        res["version_id"],
                        PARSER_VERSION,
                    ),
                )
                conn.commit()

    LOG.info("  completed=%d failed=%d map_errors=%d",
             sum(1 for r in good if r.get("status") == "completed"),
             sum(1 for r in good if r.get("status") == "failed"),
             failed_count)

    if not good:
        LOG.error("FAIL: no successful results — aborting stage_a.parquet write")
        sys.exit(1)

    # Build stage_a.parquet from all results (including failed rows — no row dropped)
    from scripts._lib.sec_iapd_brochure_parse import ITEM_TITLES as _ITEM_TITLES

    ITEM_COLS = [f"item_{n}_{slug}" for n, slug in _ITEM_TITLES.items()]

    def _to_row(r: dict) -> dict:
        base = {
            "crd_number": r.get("crd_number"),
            "version_id": r.get("version_id"),
            "brochure_type": r.get("brochure_type"),
            "filing_date": r.get("filing_date"),
            "r2_key": r.get("r2_key"),
            "content_sha256": r.get("content_sha256"),
            "content_bytes": r.get("content_bytes"),
            "page_count": r.get("page_count"),
            "full_text_md": r.get("full_text_md"),
            "section_split_confidence": r.get("section_split_confidence"),
            "section_split_method": r.get("section_split_method"),
            "parser_version": r.get("parser_version", PARSER_VERSION),
            "parsed_at": r.get("parsed_at"),
            "parse_run_id": r.get("parse_run_id"),
            "status": r.get("status", "failed"),
            "error_message": r.get("error_message"),
        }
        for col in ITEM_COLS:
            base[col] = r.get(col)
        return base

    all_rows = [_to_row(r) for r in good]

    # Write to /tmp then upload to R2 — L42: ContentType only, NO ContentEncoding
    local_parquet = "/tmp/stage_a.parquet"
    schema_fields = [
        pa.field("crd_number", pa.int64()),
        pa.field("version_id", pa.string()),
        pa.field("brochure_type", pa.string()),
        pa.field("filing_date", pa.string()),
        pa.field("r2_key", pa.string()),
        pa.field("content_sha256", pa.string()),
        pa.field("content_bytes", pa.int64()),
        pa.field("page_count", pa.int32()),
        pa.field("full_text_md", pa.string()),
        pa.field("section_split_confidence", pa.int32()),
        pa.field("section_split_method", pa.string()),
        pa.field("parser_version", pa.string()),
        pa.field("parsed_at", pa.string()),
        pa.field("parse_run_id", pa.string()),
        pa.field("status", pa.string()),
        pa.field("error_message", pa.string()),
    ] + [pa.field(col, pa.string()) for col in ITEM_COLS]

    schema = pa.schema(schema_fields)

    def _safe_str(v: Any) -> str | None:
        if v is None:
            return None
        return str(v)

    def _safe_int32(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _safe_int64(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    arrays = []
    for field in schema:
        col_name = field.name
        if field.type == pa.int64():
            col_data = [_safe_int64(r.get(col_name)) for r in all_rows]
            arrays.append(pa.array(col_data, type=pa.int64()))
        elif field.type == pa.int32():
            col_data = [_safe_int32(r.get(col_name)) for r in all_rows]
            arrays.append(pa.array(col_data, type=pa.int32()))
        else:
            col_data = [_safe_str(r.get(col_name)) for r in all_rows]
            arrays.append(pa.array(col_data, type=pa.string()))

    table = pa.table(dict(zip(schema.names, arrays)), schema=schema)
    pq.write_table(table, local_parquet, compression="zstd", row_group_size=10000)

    release = date.today().isoformat()
    stage_a_key = f"sec-iapd/form-adv-part-2-parsed/release={release}/stage_a.parquet"
    LOG.info("  uploading stage_a.parquet to s3://%s/%s ...", R2_BUCKET, stage_a_key)

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    s3.upload_file(
        local_parquet,
        R2_BUCKET,
        stage_a_key,
        ExtraArgs={"ContentType": "application/x-parquet"},  # L42: NO ContentEncoding
    )
    LOG.info("  stage_a.parquet uploaded: %d rows, key=%s", len(all_rows), stage_a_key)
    LOG.info("Stage A complete. release=%s stage_a_key=%s", release, stage_a_key)
