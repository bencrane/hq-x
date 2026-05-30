#!/usr/bin/env python3
"""Emit spines.sam_usaspending_capital_matrix_lance — Federal Capital
Allocation Matrix: INNER JOIN spines.sam_entities_lance × usaspending.awards_lance
on UEI, WHERE total_obligation > 0.

Output schema (13 cols):
  uei, cage_code, legal_business_name,
  generated_unique_award_id, piid, fain, award_type,
  awarding_toptier_agency_name, funding_subtier_agency_name,
  contract_signed_date (DATE), contract_end_date (DATE),
  total_obligated_usd (DECIMAL(20,2)), potential_total_value_usd (DECIMAL(20,2))

BTREE scalar indices (strict, non-swallowed): uei, generated_unique_award_id,
total_obligated_usd. On any BTREE failure, the Lance dataset is deleted from
R2 (rollback) and the process exits non-zero.

Production path:
  s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_usaspending_capital_matrix_lance/
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# Allow running from scripts/ dir or from project root.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

LOG = logging.getLogger(__name__)

DATASET_SLUG = "usaspending_capital_matrix_lance"
LANCE_PROD_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_usaspending_capital_matrix_lance/"
)
LANCE_SANDBOX_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_usaspending_capital_matrix_lance_sandbox/"
)
SAM_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_entities_lance/"
AWARDS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_lance/"
POLARIS_NAMESPACE = "spines"
POLARIS_TABLE_NAME = "sam_usaspending_capital_matrix_lance"

# Volume floor — INNER JOIN over SAM × awards-with-positive-obligation will
# yield millions of rows. Set conservatively: any matched cohort < 1M rows
# is a structural failure (e.g., SAM source went empty, awards lost UEIs).
MIN_ROW_FLOOR = 1_000_000


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "auto",
        "aws_virtual_hosted_style_request": "false",
    }


def _s3_client():
    import boto3
    from botocore.config import Config as BotoConfig
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def _r2_delete_prefix(bucket: str, prefix: str) -> int:
    cli = _s3_client()
    deleted = 0
    token = None
    while True:
        kw: dict = dict(Bucket=bucket, Prefix=prefix)
        if token:
            kw["ContinuationToken"] = token
        resp = cli.list_objects_v2(**kw)
        keys = [{"Key": o["Key"]} for o in resp.get("Contents", []) or []]
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            cli.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)
        token = resp.get("NextContinuationToken")
        if not token:
            break
    return deleted


def _polaris_token() -> str:
    import requests
    base = os.environ["POLARIS_PUBLIC_URL"].rstrip("/")
    r = requests.post(
        f"{base}/api/catalog/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["POLARIS_ROOT_PRINCIPAL_ID"],
            "client_secret": os.environ["POLARIS_ROOT_PRINCIPAL_SECRET"],
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _polaris_register(uri: str, doc: str) -> str:
    import requests
    base = os.environ["POLARIS_PUBLIC_URL"].rstrip("/")
    cat = os.environ["POLARIS_DEFAULT_CATALOG_NAME"]
    tok = _polaris_token()
    hdr_auth = {"Authorization": f"Bearer {tok}"}
    hdr_json = {**hdr_auth, "Content-Type": "application/json"}

    # Ensure namespace exists
    ns_url = f"{base}/api/catalog/v1/{cat}/namespaces"
    existing = requests.get(ns_url, headers=hdr_auth, timeout=30).json()
    existing_ns = {".".join(n) if isinstance(n, list) else n
                   for n in existing.get("namespaces", [])}
    if POLARIS_NAMESPACE not in existing_ns:
        requests.post(ns_url, headers=hdr_json,
                      json={"namespace": [POLARIS_NAMESPACE]}, timeout=30)

    tbl_base = (f"{base}/api/catalog/polaris/v1/{cat}/namespaces/"
                f"{POLARIS_NAMESPACE}/generic-tables")
    get = requests.get(f"{tbl_base}/{POLARIS_TABLE_NAME}", headers=hdr_auth, timeout=30)
    if get.status_code == 200:
        return "already-registered"
    body = {
        "name": POLARIS_TABLE_NAME,
        "format": "lance",
        "base-location": uri.rstrip("/") + "/",
        "doc": doc,
        "properties": {"table_type": "lance"},
    }
    r = requests.post(tbl_base, headers=hdr_json, json=body, timeout=30)
    if r.status_code in (200, 201):
        return f"created ({r.status_code})"
    return f"FAILED {r.status_code}: {r.text[:300]}"


def _duckdb_con():
    import duckdb
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = "/tmp/lance"
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='24GB'")
    con.execute("SET temp_directory='/tmp/lance'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    return con


def run(target_uri: str, apply: bool) -> dict:
    import lance
    import pyarrow.compute as pc

    metrics: dict = {"target_uri": target_uri, "apply": apply}
    t_total = time.perf_counter()
    so = _r2_storage_options()

    # ── Pre-flight ──
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=so)
    awards_ds = lance.dataset(AWARDS_LANCE_URI, storage_options=so)
    sam_cols = {f.name for f in sam_ds.schema}
    awards_cols = {f.name for f in awards_ds.schema}
    for c in ("uei", "cage_code", "legal_business_name"):
        if c not in sam_cols:
            raise SystemExit(f"FAIL: spines.sam_entities_lance missing required col {c!r}")
    needed_awards = [
        "recipient_uei", "generated_unique_award_id", "piid", "fain",
        "type_description", "awarding_toptier_agency_name",
        "funding_subtier_agency_name", "date_signed",
        "period_of_performance_current_end_date",
        "total_obligation", "base_and_all_options_value",
    ]
    for c in needed_awards:
        if c not in awards_cols:
            raise SystemExit(f"FAIL: usaspending.awards_lance missing required col {c!r}")
    LOG.info("pre-flight: SAM cols + awards cols OK")

    # ── [1] SAM scan ──
    t0 = time.perf_counter()
    sam_tbl = sam_ds.scanner(columns=["uei", "cage_code", "legal_business_name"]).to_table()
    metrics["sam_scan_s"] = round(time.perf_counter() - t0, 1)
    metrics["sam_rows"] = sam_tbl.num_rows
    LOG.info("[1] SAM scan: %s rows (%.1fs)", sam_tbl.num_rows, metrics["sam_scan_s"])

    # ── [2] SAM distinct UEIs for filter pushdown ──
    t0 = time.perf_counter()
    sam_uei_unique = pc.unique(sam_tbl.column("uei").combine_chunks())
    metrics["sam_distinct_uei"] = len(sam_uei_unique)
    LOG.info("[2] SAM distinct UEIs: %s (%.1fs)", metrics["sam_distinct_uei"], time.perf_counter() - t0)

    # ── [3] awards scanner — STREAMING (don't pre-materialize the filtered
    #        subset; DuckDB joins batch-by-batch via Arrow scanner) ──
    t0 = time.perf_counter()
    awards_reader = awards_ds.scanner(
        columns=needed_awards,
        filter=pc.field("recipient_uei").isin(sam_uei_unique),
    ).to_reader()
    metrics["awards_scanner_setup_s"] = round(time.perf_counter() - t0, 2)
    LOG.info("[3] awards streaming scanner ready (%.2fs setup)",
             metrics["awards_scanner_setup_s"])

    # ── [4] DuckDB JOIN + DISTINCT + CAST (streams awards through Arrow) ──
    con = _duckdb_con()
    con.register("sam", sam_tbl)
    con.register("awards", awards_reader)
    sql = """
        SELECT DISTINCT
            sam.uei,
            sam.cage_code,
            sam.legal_business_name,
            awards.generated_unique_award_id,
            awards.piid,
            awards.fain,
            awards.type_description                                                       AS award_type,
            awards.awarding_toptier_agency_name,
            awards.funding_subtier_agency_name,
            try_strptime(awards.date_signed, '%Y-%m-%d')::DATE                            AS contract_signed_date,
            try_strptime(awards.period_of_performance_current_end_date, '%Y-%m-%d')::DATE AS contract_end_date,
            TRY_CAST(awards.total_obligation AS DECIMAL(20,2))                            AS total_obligated_usd,
            TRY_CAST(awards.base_and_all_options_value AS DECIMAL(20,2))                  AS potential_total_value_usd
        FROM sam
        INNER JOIN awards ON sam.uei = awards.recipient_uei
        WHERE TRY_CAST(awards.total_obligation AS DECIMAL(20,2)) > 0
    """

    if not apply:
        # Dry-run: count only, no Lance write
        t0 = time.perf_counter()
        n = con.execute(
            f"SELECT COUNT(*) FROM ({sql})"
        ).fetchone()[0]
        metrics["dry_run_row_count"] = n
        metrics["dry_run_count_s"] = round(time.perf_counter() - t0, 1)
        LOG.info("[dry-run] row count: %s (%.1fs)", n, metrics["dry_run_count_s"])
        if n < MIN_ROW_FLOOR:
            raise SystemExit(f"FAIL: row count {n:,} < floor {MIN_ROW_FLOOR:,}")
        metrics["total_s"] = round(time.perf_counter() - t_total, 1)
        return metrics

    # ── [5] Lance write inside commit lock ──
    spine_existed = False
    try:
        import lance as _l
        _l.dataset(target_uri.rstrip("/"), storage_options=so)
        spine_existed = True
    except Exception:
        pass
    metrics["spine_existed_before"] = spine_existed

    bucket = target_uri.removeprefix("s3://").split("/", 1)[0]
    prefix = target_uri.removeprefix(f"s3://{bucket}/")
    if not prefix.endswith("/"):
        prefix += "/"

    t0 = time.perf_counter()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(sql).fetch_record_batch(100_000)
        ds = lance.write_dataset(reader, target_uri.rstrip("/"), mode="overwrite",
                                 storage_options=so)
        nrows = ds.count_rows()
        metrics["distinct_write_s"] = round(time.perf_counter() - t0, 1)
        metrics["lance_rows"] = nrows
        LOG.info("[5] Lance write: %s rows (%.1fs)", nrows, metrics["distinct_write_s"])

        if nrows < MIN_ROW_FLOOR:
            LOG.error("HARD FAIL: row count %s < floor %s", nrows, MIN_ROW_FLOOR)
            if not spine_existed:
                deleted = _r2_delete_prefix(bucket, prefix)
                LOG.error("rollback: deleted %s R2 objects", deleted)
            raise SystemExit(f"FAIL: row count {nrows:,} < floor {MIN_ROW_FLOOR:,}")

        # ── [6] BTREE × 3 — strict, non-swallowed ──
        btree_cols = ["uei", "generated_unique_award_id", "total_obligated_usd"]
        idx_metrics: dict = {}
        try:
            for col in btree_cols:
                t_i = time.perf_counter()
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                idx_metrics[col] = round(time.perf_counter() - t_i, 1)
                LOG.info("[6] BTREE %s: %.1fs", col, idx_metrics[col])
        except Exception as e:
            LOG.exception("[6] BTREE failure on a column — ROLLING BACK")
            if not spine_existed:
                deleted = _r2_delete_prefix(bucket, prefix)
                LOG.error("rollback: deleted %s R2 objects", deleted)
            raise
        metrics["btree_s"] = idx_metrics

        # ── [7] compact + cleanup ──
        t0 = time.perf_counter()
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        metrics["compact_cleanup_s"] = round(time.perf_counter() - t0, 1)
        LOG.info("[7] compact+cleanup: %.1fs", metrics["compact_cleanup_s"])

    # ── [8] Polaris register (post-lock) ──
    t0 = time.perf_counter()
    status = _polaris_register(target_uri,
        "Master spine — SAM × USAspending capital allocation matrix. "
        "INNER JOIN spines.sam_entities_lance × usaspending.awards_lance on UEI, "
        "WHERE total_obligation > 0. 13-col projection w/ DATE + DECIMAL cast, "
        "DISTINCT, BTREE on uei + generated_unique_award_id + total_obligated_usd.")
    metrics["polaris_register"] = status
    metrics["polaris_s"] = round(time.perf_counter() - t0, 1)
    LOG.info("[8] Polaris register: %s (%.1fs)", status, metrics["polaris_s"])

    metrics["total_s"] = round(time.perf_counter() - t_total, 1)
    return metrics


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Write Lance dataset to R2 (omit for dry-run count-only)")
    ap.add_argument("--sandbox", action="store_true",
                    help="Target the sandbox URI instead of prod")
    ap.add_argument("--target-path-override", default=None,
                    help="Explicit Lance URI; overrides --sandbox")
    args = ap.parse_args(argv)

    target = args.target_path_override or (LANCE_SANDBOX_URI if args.sandbox else LANCE_PROD_URI)
    metrics = run(target, apply=args.apply)
    print("\nOK — metrics:", json.dumps(metrics, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
