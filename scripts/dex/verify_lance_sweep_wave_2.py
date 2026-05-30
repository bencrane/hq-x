#!/usr/bin/env python3
"""Wave 2 Lance sweep -- multi-source verification harness.

Runs the canary's verification gates against each of the 3 Wave-2 sources:

  1. cms_open_payments_general_lance       (BTREE on record_id)
  2. cms_open_payments_research_lance      (BTREE on record_id)
  3. gleif_lei_records_lance               (BTREE on lei)

For each source, gates are:
  G1. Lance dataset exists at the configured URI.
  G2. Lance row count matches Parquet row count within +/-0.1%.
  G3. Per-key random-access p50 < 100ms (Lance's headline benefit).
  G4. Polaris Generic Table API returns format=lance.

This is structurally identical to ``verify_lance_sweep_wave_1.py`` -- the
only delta is the source list + a new ``MULTI_YEAR_FEED:`` parquet-count
resolver to handle the cms-open-payments year=YYYY/feed=FEED/ layout.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance --with duckdb --with requests \\
        --with boto3 --with psycopg python3 \\
        apps/data-engine-x/scripts/verify_lance_sweep_wave_2.py

Exit codes:
    0 -- every gate passed for every source.
    1 -- at least one gate failed; details in JSON / log.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass

import boto3
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("verify-lance-sweep-wave-2")

ROW_COUNT_TOLERANCE_PCT = 0.1
PER_KEY_LATENCY_MS_BUDGET = 100.0
PER_KEY_SAMPLES = 20


@dataclass(frozen=True)
class SourceSpec:
    display_name: str
    namespace: str
    polaris_table: str
    lance_uri: str
    btree_column: str
    parquet_uri_for_count: str


SOURCES: list[SourceSpec] = [
    SourceSpec(
        display_name="cms_open_payments_general_lance",
        namespace="cms_open_payments",
        polaris_table="general_payments_lance",
        lance_uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/"
            "cms_open_payments/general_payments_lance"
        ),
        btree_column="record_id",
        # MULTI_YEAR_FEED:prefix:feed:years_csv
        parquet_uri_for_count=(
            "MULTI_YEAR_FEED:r2://dex-raw-landing-zone/cms-open-payments:general:2024"
        ),
    ),
    SourceSpec(
        display_name="cms_open_payments_research_lance",
        namespace="cms_open_payments",
        polaris_table="research_payments_lance",
        lance_uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/"
            "cms_open_payments/research_payments_lance"
        ),
        btree_column="record_id",
        parquet_uri_for_count=(
            "MULTI_YEAR_FEED:r2://dex-raw-landing-zone/cms-open-payments:research:2024"
        ),
    ),
    SourceSpec(
        display_name="gleif_lei_records_lance",
        namespace="gleif",
        polaris_table="lei_records_lance",
        lance_uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/"
            "gleif/lei_records_lance"
        ),
        btree_column="lei",
        # LATEST_SNAPSHOT_SINGLE_FILE:prefix:file_pattern
        parquet_uri_for_count=(
            "LATEST_SNAPSHOT_SINGLE_FILE:r2://dex-raw-landing-zone/gleif:lei_records.parquet"
        ),
    ),
]


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _lance_size_bytes(lance_uri: str) -> int:
    assert lance_uri.startswith("s3://"), lance_uri
    rest = lance_uri[len("s3://"):]
    bucket, _, prefix = rest.partition("/")
    s3 = _s3_client()
    total = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            total += int(obj["Size"])
    return total


def _open_lance(uri: str):
    import lance
    return lance.dataset(uri, storage_options=_lance_storage_options())


def _connect_duckdb_to_r2():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _resolve_parquet_count(con, spec: SourceSpec) -> int:
    uri_spec = spec.parquet_uri_for_count

    if uri_spec.startswith("MULTI_YEAR_FEED:"):
        # MULTI_YEAR_FEED:prefix:feed:years_csv
        body = uri_spec[len("MULTI_YEAR_FEED:"):]
        # Split on : but the prefix has r2:// so we keep the first :// intact.
        # Format: r2://bucket/prefix:feed:years_csv
        # Parse from the right: years_csv, feed, prefix
        prefix_feed, _, years_csv = body.rpartition(":")
        prefix, _, feed = prefix_feed.rpartition(":")
        years = [int(y) for y in years_csv.split(",")]
        uri_list = ", ".join(
            f"'{prefix}/year={y}/feed={feed}/*.parquet'" for y in years
        )
        return con.execute(
            f"SELECT COUNT(*) FROM read_parquet([{uri_list}])"
        ).fetchone()[0]

    if uri_spec.startswith("LATEST_SNAPSHOT_SINGLE_FILE:"):
        # LATEST_SNAPSHOT_SINGLE_FILE:prefix:file_pattern
        body = uri_spec[len("LATEST_SNAPSHOT_SINGLE_FILE:"):]
        prefix, _, fname = body.rpartition(":")
        # List snapshots that contain the file_pattern
        rows = con.execute(
            f"SELECT file FROM glob('{prefix}/snapshot=*/{fname}')"
        ).fetchall()
        snapshots = sorted(set(
            seg[len("snapshot="):]
            for (path,) in rows
            for seg in path.split("/")
            if seg.startswith("snapshot=") and len(seg) == len("snapshot=YYYY-MM-DD")
        ))
        if not snapshots:
            raise SystemExit(f"FAIL: no snapshots under {prefix}")
        latest = snapshots[-1]
        uri = f"{prefix}/snapshot={latest}/{fname}"
        return con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{uri}')"
        ).fetchone()[0]

    raise ValueError(f"unknown parquet_uri_for_count spec: {uri_spec!r}")


def _polaris_check_table(spec: SourceSpec) -> dict:
    base_url = os.environ["POLARIS_PUBLIC_URL"].rstrip("/")
    client_id = os.environ["POLARIS_ROOT_PRINCIPAL_ID"]
    client_secret = os.environ["POLARIS_ROOT_PRINCIPAL_SECRET"]
    catalog = os.environ["POLARIS_DEFAULT_CATALOG_NAME"]
    tok_resp = requests.post(
        f"{base_url}/api/catalog/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        timeout=30,
    )
    if tok_resp.status_code != 200:
        return {"ok": False, "error": f"oauth {tok_resp.status_code}"}
    tok = tok_resp.json()["access_token"]
    url = (
        f"{base_url}/api/catalog/polaris/v1/{catalog}/namespaces/"
        f"{spec.namespace}/generic-tables/{spec.polaris_table}"
    )
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "body": r.text[:300]}
    payload = r.json().get("table", {})
    return {
        "ok": payload.get("format") == "lance",
        "format": payload.get("format"),
        "name": payload.get("name"),
        "base-location": payload.get("base-location"),
    }


def _bench_per_key_lookup(ds, btree_column: str, n: int) -> dict:
    total_rows = ds.count_rows()
    offsets = [int(total_rows * pct) for pct in (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95)]
    seen: set = set()
    for off in offsets:
        rows = ds.scanner(
            columns=[btree_column],
            limit=200,
            offset=off,
        ).to_table().to_pylist()
        for r in rows:
            v = r.get(btree_column)
            if v is not None and v != "":
                seen.add(v)
    keys = list(seen)
    if not keys:
        return {"samples": 0, "p50_ms": None, "error": "no keys sampled"}
    random.shuffle(keys)
    keys = keys[:n]

    for warmup_key in keys[:3]:
        try:
            _ = ds.scanner(
                filter=f"{btree_column} = '{warmup_key}'",
                columns=[btree_column],
            ).to_table()
        except Exception:
            pass

    timings_ms: list[float] = []
    for key in keys:
        t0 = time.perf_counter()
        rows = ds.scanner(
            filter=f"{btree_column} = '{key}'",
            columns=[btree_column],
        ).to_table().to_pylist()
        dur_ms = (time.perf_counter() - t0) * 1000
        timings_ms.append(dur_ms)
        if len(rows) == 0:
            LOG.warning(
                "key %r returned %d rows (expected >=1)", key, len(rows),
            )
    timings_ms.sort()
    n = len(timings_ms)
    return {
        "samples": n,
        "min_ms": round(timings_ms[0], 2) if n else None,
        "p50_ms": round(timings_ms[n // 2], 2) if n else None,
        "p90_ms": round(timings_ms[int(n * 0.9)], 2) if n else None,
        "max_ms": round(timings_ms[-1], 2) if n else None,
    }


def verify_source(con, spec: SourceSpec) -> dict:
    LOG.info("=" * 60)
    LOG.info("verifying %s", spec.display_name)
    LOG.info("  lance uri: %s", spec.lance_uri)

    result: dict = {
        "display_name": spec.display_name,
        "lance_uri": spec.lance_uri,
        "btree_column": spec.btree_column,
    }
    gates: dict[str, bool] = {}

    try:
        ds = _open_lance(spec.lance_uri)
        lance_rows = ds.count_rows()
        gates["lance_dataset_exists"] = lance_rows > 0
        result["lance_rows"] = lance_rows
    except Exception as e:
        gates["lance_dataset_exists"] = False
        result["lance_open_error"] = str(e)
        result["gates"] = gates
        return result

    try:
        result["lance_bytes"] = _lance_size_bytes(spec.lance_uri)
    except Exception as e:
        result["lance_bytes_error"] = str(e)

    try:
        parquet_rows = _resolve_parquet_count(con, spec)
        result["parquet_rows"] = parquet_rows
        if parquet_rows == 0:
            gates["row_count_parity"] = False
            result["row_drift_pct"] = None
        else:
            drift_pct = abs(lance_rows - parquet_rows) / parquet_rows * 100
            result["row_drift_pct"] = round(drift_pct, 4)
            gates["row_count_parity"] = drift_pct <= ROW_COUNT_TOLERANCE_PCT
    except Exception as e:
        gates["row_count_parity"] = False
        result["parquet_count_error"] = str(e)

    LOG.info(
        "  benchmarking %d per-key lookups on %s ...",
        PER_KEY_SAMPLES, spec.btree_column,
    )
    bench = _bench_per_key_lookup(ds, spec.btree_column, PER_KEY_SAMPLES)
    result["per_key"] = bench
    if bench.get("p50_ms") is not None:
        gates["per_key_p50_under_budget"] = (
            bench["p50_ms"] < PER_KEY_LATENCY_MS_BUDGET
        )
    else:
        gates["per_key_p50_under_budget"] = False

    polaris = _polaris_check_table(spec)
    result["polaris"] = polaris
    gates["polaris_registered_lance"] = bool(polaris.get("ok"))

    result["gates"] = gates
    result["all_gates_passed"] = all(gates.values())
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    ap.add_argument(
        "--sources",
        default=None,
        help="Comma-separated subset of display_names to verify (default: all)",
    )
    args = ap.parse_args()

    for v in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "POLARIS_PUBLIC_URL", "POLARIS_ROOT_PRINCIPAL_ID",
        "POLARIS_ROOT_PRINCIPAL_SECRET", "POLARIS_DEFAULT_CATALOG_NAME",
    ):
        if not os.environ.get(v):
            LOG.error("FAIL: %s not set in environment", v)
            return 64

    selected_names = (
        set(s.strip() for s in args.sources.split(",")) if args.sources else None
    )
    sources = [
        s for s in SOURCES
        if selected_names is None or s.display_name in selected_names
    ]
    if not sources:
        LOG.error("no sources match --sources %r", args.sources)
        return 64

    con = _connect_duckdb_to_r2()
    results: list[dict] = []
    for spec in sources:
        results.append(verify_source(con, spec))
    con.close()

    all_passed = all(r.get("all_gates_passed") for r in results)
    summary = {
        "sources": results,
        "all_sources_passed": all_passed,
        "thresholds": {
            "row_count_tolerance_pct": ROW_COUNT_TOLERANCE_PCT,
            "per_key_latency_ms_budget": PER_KEY_LATENCY_MS_BUDGET,
        },
    }

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        LOG.info("=" * 60)
        LOG.info("=== Wave 2 Lance sweep -- verification summary ===")
        LOG.info("=" * 60)
        for r in results:
            LOG.info(
                "%s -- %s rows: %s/%s (drift %s%%); p50 %s ms; polaris %s; gates %s",
                r["display_name"],
                "PASS" if r.get("all_gates_passed") else "FAIL",
                r.get("lance_rows"),
                r.get("parquet_rows"),
                r.get("row_drift_pct"),
                r.get("per_key", {}).get("p50_ms"),
                "ok" if r.get("polaris", {}).get("ok") else "fail",
                r.get("gates"),
            )
        LOG.info("all sources passed: %s", all_passed)

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
