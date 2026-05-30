#!/usr/bin/env python3
"""IRS Exempt Organizations Business Master File (EO-BMF) → R2 ingest.

Mirrors the FDIC / NCUA / SBA pattern. Sources the IRS public bulk extract
of every 501(c) tax-exempt organization in the US:

  https://www.irs.gov/pub/irs-soi/eo1.csv     # Northeast
  https://www.irs.gov/pub/irs-soi/eo2.csv     # Mid-Atlantic + Great Lakes
  https://www.irs.gov/pub/irs-soi/eo3.csv     # Gulf Coast + Pacific Coast
  https://www.irs.gov/pub/irs-soi/eo_xx.csv   # International (non-domestic)
  https://www.irs.gov/pub/irs-soi/eo_pr.csv   # Puerto Rico

eo4.csv on the IRS landing page is a SUPERSET of eo_xx + eo_pr — including
all six files would double-count ~4.9K rows. We pull only the 5 above; the
IRS-published unique total of 1,952,238 reconciles exactly.

Pipeline:
  1. HEAD all 5 URLs, capture Last-Modified per file.
  2. Skip-if-unchanged: if max(Last-Modified) <= prior completed run's
     source_last_modified_max, write a no_change audit row and exit.
  3. Stream-download all 5 CSVs.
  4. DuckDB UNION ALL into single relation; project + add normalized columns
     (ein_normalized, org_name_normalized, mailing_zip5, irs_region) and
     numeric / date casts.
  5. Single ZSTD Parquet output: eo_combined.parquet.
  6. boto3 upload to s3://dex-raw-landing-zone/irs-bmf/snapshot={YYYY-MM}/
     eo_combined.parquet.
  7. Audit row: ops.irs_bmf_r2_ingest_runs.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_bmf_r2_ingest.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_bmf_r2_ingest.py --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_bmf_r2_ingest.py --skip-if-unchanged
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_irs_bmf_r2_ingest.py --only-regions eo_pr \\
        --max-rows 100   # smoke
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb


R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# Five regional / territorial CSV files. The IRS landing page enumerates
# these under https://www.irs.gov/charities-non-profits/exempt-organizations-
# business-master-file-extract-eo-bmf.
#
# Notes vs the directive's original URL list:
#   - directive's eopr.csv is stale; current URL is eo_pr.csv.
#   - eo4.csv is a SUPERSET of eo_xx.csv + eo_pr.csv (i.e. eo4 ==
#     International + PR) — including all six would double-count ~4.9K
#     rows. We pull eo_xx + eo_pr separately for finer irs_region tagging
#     and skip eo4. The IRS-published unique row count of 1,952,238 then
#     reconciles exactly.
REGIONS: tuple[tuple[str, str], ...] = (
    ("eo1", "Northeast (CT, ME, MA, NH, NJ, NY, RI, VT)"),
    ("eo2", "Mid-Atlantic + Great Lakes (DE, DC, IL, IN, IA, KY, MD, MI, MN, NE, NC, ND, OH, PA, SC, SD, VA, WV, WI)"),
    ("eo3", "Gulf Coast + Pacific Coast (AL, AK, AR, AZ, CA, CO, FL, GA, HI, ID, KS, LA, MS, MO, MT, NV, NM, OK, OR, TX, TN, UT, WA, WY)"),
    ("eo_xx", "International (non-domestic), excluding PR"),
    ("eo_pr", "Puerto Rico"),
)

URL_TEMPLATE = "https://www.irs.gov/pub/irs-soi/{slug}.csv"


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("irs-bmf-r2-ingest")


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


def _default_snapshot_year_month() -> str:
    """Default snapshot label: current UTC year-month (YYYY-MM)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


# --------------------------------------------------------------------------- #
# HTTP layer (clone of NCUA shape)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RegionHead:
    slug: str
    description: str
    url: str
    status_code: int
    content_length: int | None
    last_modified: datetime | None


def head_url(client: httpx.Client, slug: str, description: str) -> RegionHead:
    url = URL_TEMPLATE.format(slug=slug)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            cl_raw = r.headers.get("content-length")
            cl = int(cl_raw) if cl_raw and cl_raw.isdigit() else None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(
                        lm_raw, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            return RegionHead(
                slug=slug, description=description, url=url,
                status_code=r.status_code, content_length=cl, last_modified=lm,
            )
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD {url} failed: {last_exc}")


def download_csv(client: httpx.Client, url: str, dest: Path) -> int:
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
                    last_log = time.monotonic()
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 10.0:
                            log.info("  download progress: %.1f MB written",
                                     written / (1 << 20))
                            last_log = now
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download {url} failed: {last_exc}")


# --------------------------------------------------------------------------- #
# DuckDB UNION + transform
# --------------------------------------------------------------------------- #


# Raw IRS columns (28). Keep verbatim VARCHAR projection of every column —
# downstream consumers may need fields not enumerated in the directive (e.g.
# ACTIVITY codes, FOUNDATION classifier).
_RAW_COLUMNS: tuple[str, ...] = (
    "EIN", "NAME", "ICO", "STREET", "CITY", "STATE", "ZIP", "GROUP",
    "SUBSECTION", "AFFILIATION", "CLASSIFICATION", "RULING",
    "DEDUCTIBILITY", "FOUNDATION", "ACTIVITY", "ORGANIZATION", "STATUS",
    "TAX_PERIOD", "ASSET_CD", "INCOME_CD", "FILING_REQ_CD",
    "PF_FILING_REQ_CD", "ACCT_PD", "ASSET_AMT", "INCOME_AMT",
    "REVENUE_AMT", "NTEE_CD", "SORT_NAME",
)


def _build_union_sql(
    csv_paths: list[tuple[str, Path]],
    *,
    snapshot_year_month: str,
    max_rows: int | None,
) -> str:
    """Build the DuckDB SQL projecting all 6 regional CSVs into one relation
    with normalized columns + partition metadata. UNION ALL — preserves all
    rows; the regional split is captured in irs_region.
    """
    # Slug → irs_region label. eoN files become '1', '2', '3'. The two
    # territorial files become 'XX' (International) and 'PR'.
    region_label_for: dict[str, str] = {
        "eo1": "1",
        "eo2": "2",
        "eo3": "3",
        "eo_xx": "XX",
        "eo_pr": "PR",
    }
    union_parts: list[str] = []
    for slug, path in csv_paths:
        region_label = region_label_for.get(slug, slug)

        limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
        union_parts.append(f"""
            SELECT
                "EIN", "NAME", "ICO", "STREET", "CITY", "STATE", "ZIP",
                "GROUP", "SUBSECTION", "AFFILIATION", "CLASSIFICATION",
                "RULING", "DEDUCTIBILITY", "FOUNDATION", "ACTIVITY",
                "ORGANIZATION", "STATUS", "TAX_PERIOD", "ASSET_CD",
                "INCOME_CD", "FILING_REQ_CD", "PF_FILING_REQ_CD",
                "ACCT_PD", "ASSET_AMT", "INCOME_AMT", "REVENUE_AMT",
                "NTEE_CD", "SORT_NAME",
                CAST('{region_label}' AS VARCHAR) AS irs_region
            FROM read_csv(
                '{path}',
                header=TRUE, delim=',', quote='"', escape='"',
                all_varchar=TRUE, ignore_errors=TRUE
            )
            {limit_clause}
        """)
    return " UNION ALL ".join(union_parts)


def union_csvs_to_parquet(
    csv_paths: list[tuple[str, Path]],
    parquet_path: Path,
    *,
    snapshot_year_month: str,
    max_rows: int | None,
    log_prefix: str,
) -> tuple[int, int, dict[str, int]]:
    """Read all 6 IRS regional CSVs as VARCHAR via DuckDB, UNION ALL,
    add normalized columns + numeric / date casts, write ZSTD Parquet.

    Returns (rows_in, rows_in_parquet, per_region_row_counts).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")

    union_sql = _build_union_sql(
        csv_paths,
        snapshot_year_month=snapshot_year_month,
        max_rows=max_rows,
    )

    # Stage the UNION as a temp view so we can compute per-region counts and
    # then project transformations in a second pass.
    con.execute(f"CREATE VIEW raw_union AS {union_sql};")

    region_counts_raw = con.execute("""
        SELECT irs_region, COUNT(*) AS n
        FROM raw_union
        GROUP BY 1
        ORDER BY 1
    """).fetchall()
    per_region: dict[str, int] = {row[0]: int(row[1]) for row in region_counts_raw}
    rows_in = sum(per_region.values())
    log.info("%s rows by region: %s (total=%s)",
             log_prefix, per_region, f"{rows_in:,}")

    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    # Final projection: raw 28 columns + numeric / date casts + normalized
    # columns + irs_region + partition metadata.
    #
    # Numeric casts via TRY_CAST so corrupted rows don't poison the column.
    # Date casts via TRY_STRPTIME on YYYYMM strings.
    select_sql = f"""
        SELECT
            "EIN", "NAME", "ICO", "STREET", "CITY", "STATE", "ZIP",
            "GROUP", "SUBSECTION", "AFFILIATION", "CLASSIFICATION",
            "RULING", "DEDUCTIBILITY", "FOUNDATION", "ACTIVITY",
            "ORGANIZATION", "STATUS", "TAX_PERIOD", "ASSET_CD",
            "INCOME_CD", "FILING_REQ_CD", "PF_FILING_REQ_CD",
            "ACCT_PD", "ASSET_AMT", "INCOME_AMT", "REVENUE_AMT",
            "NTEE_CD", "SORT_NAME",
            irs_region,

            TRY_CAST("ASSET_AMT" AS DOUBLE)   AS asset_amt_numeric,
            TRY_CAST("INCOME_AMT" AS DOUBLE)  AS income_amt_numeric,
            TRY_CAST("REVENUE_AMT" AS DOUBLE) AS revenue_amt_numeric,

            TRY_STRPTIME("RULING",     '%Y%m')::DATE AS ruling_date,
            TRY_STRPTIME("TAX_PERIOD", '%Y%m')::DATE AS tax_period_date,

            -- Normalized join keys.
            -- ein_normalized: strip non-digit, left-pad to 9 (NULL if too long
            --   or zero-digit input).
            CASE
                WHEN regexp_replace("EIN", '\\D', '', 'g') = '' THEN NULL
                WHEN length(regexp_replace("EIN", '\\D', '', 'g')) > 9 THEN NULL
                ELSE lpad(regexp_replace("EIN", '\\D', '', 'g'), 9, '0')
            END AS ein_normalized,

            -- org_name_normalized: lowercase + collapse-ws + iterative
            -- US legal-form suffix strip. The DuckDB regex is the SQL twin
            -- of scripts/_lib/irs_bmf_normalize.normalize_org_name; the
            -- pure-Python version is unit-tested in
            -- tests/unit/test_irs_bmf_normalize.py and the SQL stays in
            -- step with it.
            nullif(
              trim(
                regexp_replace(
                  regexp_replace(
                    lower(
                      regexp_replace(
                        regexp_replace("NAME",
                          '([\\s,.]+(INCORPORATED|CORPORATION|ASSOCIATION|FOUNDATION|MINISTRIES|MINISTRY|FELLOWSHIP|INSTITUTE|SOCIETY|COUNCIL|ALLIANCE|FEDERATION|COALITION|NETWORK|PARTNERSHIP|FUND|TRUST|CHARITIES|CHARITY|CHURCH|MISSION|ASSEMBLY|CENTER|CENTRE|GROUP|INC|CORP|CO|LLC|LP|LLP|ORG))+\\s*[,.]*\\s*$',
                          '', 'i'
                        ),
                        '\\s+', ' ', 'g'
                      )
                    ),
                    '[^\\w\\s]+', ' ', 'g'
                  ),
                  '\\s+', ' ', 'g'
                )
              ),
              ''
            ) AS org_name_normalized,

            -- mailing_zip5: first 5 chars of ZIP. Non-digit ZIPs become NULL.
            CASE
                WHEN "ZIP" IS NULL OR "ZIP" = '' THEN NULL
                WHEN length(regexp_replace("ZIP", '\\D', '', 'g')) >= 5
                    THEN substr(regexp_replace("ZIP", '\\D', '', 'g'), 1, 5)
                ELSE NULL
            END AS mailing_zip5,

            CAST('{snapshot_year_month}' AS VARCHAR) AS irs_bmf_snapshot_year_month
        FROM raw_union
    """

    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info("%s Parquet write done in %.1fs (%.1f MB)",
             log_prefix, time.monotonic() - t0,
             parquet_path.stat().st_size / (1 << 20))

    rows_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}');"
    ).fetchone()
    rows_pq = int(rows_pq_row[0]) if rows_pq_row else 0

    # Print the validation gates from the directive — emitted to logs even
    # before the audit row is finalized, so partial runs are diagnosable.
    gates = con.execute(f"""
        WITH p AS (SELECT * FROM read_parquet('{parquet_path}'))
        SELECT
            (SELECT count(*) FROM p) AS row_count,
            (SELECT 1.0 * count(*) FILTER (WHERE length(ein_normalized) = 9)
                  / NULLIF(count(*), 0)
             FROM p) AS ein_ok_rate,
            (SELECT 1.0 * count(*) FILTER (WHERE org_name_normalized IS NULL)
                  / NULLIF(count(*), 0)
             FROM p) AS name_null_rate
    """).fetchone()
    if gates is not None:
        log.info(
            "%s validation: rows=%s, ein_len9_rate=%.4f, name_null_rate=%.4f",
            log_prefix, f"{int(gates[0]):,}",
            float(gates[1] or 0.0), float(gates[2] or 0.0),
        )

    con.close()
    return rows_in, rows_pq, per_region


def upload_to_r2(parquet_path: Path, *, key: str, log_prefix: str) -> int:
    s3 = _r2_client()
    n_bytes = parquet_path.stat().st_size
    log.info("%s uploading %.1f MB → s3://%s/%s",
             log_prefix, n_bytes / (1 << 20), R2_BUCKET, key)
    s3.upload_file(
        str(parquet_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return n_bytes


# --------------------------------------------------------------------------- #
# Audit ledger
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot_year_month: str,
    source_urls: list[str],
    source_last_modified_max: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.irs_bmf_r2_ingest_runs (
        snapshot_year_month, status, source_urls,
        source_last_modified_max, prior_source_last_modified
    ) VALUES (%s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            snapshot_year_month, Jsonb(source_urls),
            source_last_modified_max, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, snapshot_year_month: str,
) -> datetime | None:
    """Return the most recent completed run's source_last_modified_max
    across ALL snapshots (not just the current one) — re-ingestion is gated
    by source freshness, not by snapshot label."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified_max
              FROM ops.irs_bmf_r2_ingest_runs
             WHERE status = 'completed'
             ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    snapshot_year_month: str,
    source_urls: list[str],
    source_last_modified_max: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.irs_bmf_r2_ingest_runs (
                snapshot_year_month, status, source_urls,
                source_last_modified_max, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                snapshot_year_month, Jsonb(source_urls), source_last_modified_max,
                prior_source_last_modified, started, started,
                Jsonb({"reason": "all source_last_modified <= prior"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    csv_bytes_total: int,
    rows_in_csv_total: int,
    region_file_count: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    r2_key: str | None,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.irs_bmf_r2_ingest_runs
               SET status = %s,
                   csv_bytes_downloaded_total = %s,
                   rows_in_csv_total = %s,
                   region_file_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_key = %s, r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
        """, (
            status, csv_bytes_total, rows_in_csv_total, region_file_count,
            parquet_row_count, parquet_bytes_written,
            R2_BUCKET if r2_key else None, r2_key, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def ingest(
    *,
    snapshot_year_month: str,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    only_regions: set[str] | None,
    r2_key_override: str | None,
) -> int:
    log_prefix = f"[irs-bmf {snapshot_year_month}]"
    started_wall = time.monotonic()
    log.info("%s start", log_prefix)

    selected: list[tuple[str, str]] = [
        (slug, desc) for slug, desc in REGIONS
        if only_regions is None or slug in only_regions
    ]
    if not selected:
        log.error("%s no regions selected (only_regions=%s)",
                  log_prefix, only_regions)
        return 2

    with httpx.Client(headers={"User-Agent": "data-engine-x/irs-bmf-r2-ingest"}) as client:
        # 1. HEAD all selected regions.
        heads: list[RegionHead] = []
        for slug, desc in selected:
            try:
                h = head_url(client, slug, desc)
            except Exception:
                log.exception("%s HEAD %s failed", log_prefix, slug)
                return 1
            log.info(
                "%s HEAD %s status=%s last_modified=%s content_length=%s",
                log_prefix, slug, h.status_code, h.last_modified, h.content_length,
            )
            heads.append(h)

        # 2. Validate HEAD success.
        bad = [h for h in heads if h.status_code != 200]
        if bad:
            log.error("%s HEAD non-200 for: %s", log_prefix,
                      [(h.slug, h.status_code) for h in bad])
            return 1

        source_urls = [h.url for h in heads]
        last_mods = [h.last_modified for h in heads if h.last_modified is not None]
        source_last_modified_max = max(last_mods) if last_mods else None

        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        # 3. Connect to DB; check skip-if-unchanged.
        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, snapshot_year_month)
            log.info("%s prior source_last_modified_max: %s", log_prefix, prior)

            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified_max is not None
                and source_last_modified_max <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn,
                    snapshot_year_month=snapshot_year_month,
                    source_urls=source_urls,
                    source_last_modified_max=source_last_modified_max,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn,
                snapshot_year_month=snapshot_year_month,
                source_urls=source_urls,
                source_last_modified_max=source_last_modified_max,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            workdir.mkdir(parents=True, exist_ok=True)
            csv_paths: list[tuple[str, Path]] = []
            csv_bytes_total = 0

            try:
                # 4. Download every CSV.
                for h in heads:
                    csv_path = workdir / f"{h.slug}.csv"
                    log.info("%s downloading %s …", log_prefix, h.slug)
                    n = download_csv(client, h.url, csv_path)
                    csv_bytes_total += n
                    log.info("%s   %s downloaded %.1f MB",
                             log_prefix, h.slug, n / (1 << 20))
                    csv_paths.append((h.slug, csv_path))

                # 5. UNION + transform + write Parquet.
                parquet_path = workdir / "eo_combined.parquet"
                rows_in, rows_pq, per_region = union_csvs_to_parquet(
                    csv_paths, parquet_path,
                    snapshot_year_month=snapshot_year_month,
                    max_rows=max_rows,
                    log_prefix=log_prefix,
                )
                pq_bytes = parquet_path.stat().st_size

                # 6. Upload to R2.
                key = r2_key_override or (
                    f"irs-bmf/snapshot={snapshot_year_month}/eo_combined.parquet"
                )
                uploaded = upload_to_r2(
                    parquet_path, key=key, log_prefix=log_prefix,
                )

                # 7. Finalize audit row.
                finalize_run_row(
                    conn, run_id, status="completed",
                    csv_bytes_total=csv_bytes_total,
                    rows_in_csv_total=rows_in,
                    region_file_count=len(heads),
                    parquet_row_count=rows_pq,
                    parquet_bytes_written=pq_bytes,
                    r2_key=key, r2_total_bytes=uploaded,
                    started_at=started_wall, error_message=None,
                    notes={
                        "regions": per_region,
                        "max_rows": max_rows,
                        "source_urls": source_urls,
                    },
                )
                log.info(
                    "%s DONE rows=%s parquet=%.1f MB wall=%.1fs r2_key=%s",
                    log_prefix, f"{rows_pq:,}", pq_bytes / (1 << 20),
                    time.monotonic() - started_wall, key,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    csv_bytes_total=csv_bytes_total,
                    rows_in_csv_total=0,
                    region_file_count=len(heads),
                    parquet_row_count=0,
                    parquet_bytes_written=0,
                    r2_key=None, r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1
            finally:
                # Clean up — keep the workdir directory but remove large files
                # so we don't fill /tmp on repeated runs.
                for _slug, p in csv_paths:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    parquet_path.unlink(missing_ok=True)
                except Exception:
                    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot-year-month", default=None,
                   help="Snapshot label, YYYY-MM. Default: current UTC year-month.")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Skip ingest if all sources' Last-Modified <= last completed run.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD only; do not download / upload / write audit row.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Limit per-region rows (smoke testing).")
    p.add_argument("--only-regions", default=None,
                   help="Comma-separated subset, e.g. 'eo_pr,eo_xx'. Default: all 6.")
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-key-override", default=None,
                   help="Override the R2 destination key (default: "
                        "irs-bmf/snapshot={YYYY-MM}/eo_combined.parquet).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    snapshot_year_month = args.snapshot_year_month or _default_snapshot_year_month()
    workdir = Path(args.workdir or "/tmp/irs_bmf_r2_ingest")

    only_regions: set[str] | None = None
    if args.only_regions:
        only_regions = {
            s.strip().lower() for s in args.only_regions.split(",") if s.strip()
        }

    return ingest(
        snapshot_year_month=snapshot_year_month,
        skip_if_unchanged=args.skip_if_unchanged,
        dry_run=args.dry_run,
        workdir=workdir,
        max_rows=args.max_rows,
        only_regions=only_regions,
        r2_key_override=args.r2_key_override,
    )


if __name__ == "__main__":
    sys.exit(main())
