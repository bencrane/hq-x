#!/usr/bin/env python3
"""Verify the Lance read path for FMCSA carrier_essentials_lance (Wave 3 canary).

Validates the load-bearing gates of the Wave 3 cycle:

  1. **Row-count parity** — Lance row count matches the Parquet row count
     (within ±0.1% for any in-flight refresh delta).
  2. **Per-DOT random-access latency** — a single DOT lookup against the Lance
     dataset should complete in <100ms (Lance's headline benefit over Parquet).
  3. **Schema evolution speed** — adding a new column (``canary_test_column``)
     to the Lance dataset should write in <5 seconds. Parquet would require
     a full rewrite of all 419 MB. After verifying, the column is dropped
     so we don't leak test state into the production dataset.
  4. **File-size delta** — record Lance vs Parquet on-disk sizes for the
     before/after measurement doc.

The DuckDB ``lance`` extension is not yet stable for DuckDB 1.5.2 / osx_arm64
(community-extensions repo returns 404 for that combo as of 2026-05-12). The
fallback path used here:

  - Lance → Arrow → DuckDB (via ``con.register(name, arrow_table)``)
  - per-DOT lookup uses ``lance.dataset.scanner(filter=...)`` directly, which
    pushes filters into Lance's columnar reader. DuckDB-over-the-scanner-result
    is just for SQL ergonomics; the actual scan is done by Lance.

When lance-duckdb is available for our DuckDB version, switch the per-DOT path
to native DuckDB SQL ``SELECT ... FROM lance_scan('<uri>')``.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance --with duckdb python3 \\
        apps/data-engine-x/scripts/verify_lance_read_path.py

    # JSON-only output (for piping):
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance --with duckdb python3 \\
        apps/data-engine-x/scripts/verify_lance_read_path.py --json

Exit codes:
    0 — all gates passed.
    1 — at least one gate failed (row-count mismatch, latency miss, etc.).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from typing import Any

import boto3

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("verify-lance-read-path")

R2_BUCKET = "dex-raw-landing-zone"
PARQUET_PREFIX = "fmcsa-derived/carrier_essentials"
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
LANCE_R2_PREFIX = "polaris-warehouse/fmcsa/carrier_essentials_lance"

# Verification gate thresholds (from directive).
ROW_COUNT_TOLERANCE_PCT = 0.1  # ±0.1% drift acceptable (in-flight refresh)
PER_DOT_LATENCY_MS_BUDGET = 100.0  # Lance per-DOT lookup must be <100ms
SCHEMA_EVO_SECONDS_BUDGET = 5.0  # add-column must be <5s


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _latest_snapshot() -> str:
    s3 = _s3_client()
    snapshots: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=PARQUET_PREFIX + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            for p in key.split("/"):
                if p.startswith("snapshot=") and len(p) == len("snapshot=YYYY-MM-DD"):
                    snapshots.add(p[len("snapshot="):])
    if not snapshots:
        raise SystemExit("FAIL: no parquet snapshots discovered")
    return max(snapshots)


def _parquet_size_bytes(snapshot: str) -> int:
    s3 = _s3_client()
    key = f"{PARQUET_PREFIX}/snapshot={snapshot}/data.parquet"
    head = s3.head_object(Bucket=R2_BUCKET, Key=key)
    return int(head["ContentLength"])


def _lance_size_bytes() -> int:
    s3 = _s3_client()
    total = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=LANCE_R2_PREFIX + "/"):
        for obj in page.get("Contents", []):
            total += int(obj["Size"])
    return total


def _parquet_row_count(snapshot: str) -> int:
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
    uri = f"r2://{R2_BUCKET}/{PARQUET_PREFIX}/snapshot={snapshot}/data.parquet"
    cnt = con.execute(f"SELECT COUNT(*) FROM read_parquet('{uri}')").fetchone()[0]
    con.close()
    return int(cnt)


def _open_lance():
    import lance
    return lance.dataset(LANCE_URI, storage_options=_lance_storage_options())


def _lance_row_count(ds) -> int:
    return ds.count_rows()


def _sample_dot_numbers(ds, n: int) -> list[str]:
    """Pick n DOT numbers from Lance for the per-DOT lookup benchmark."""
    # Take a 100-row sample, pick n of them — keeps the sampling cheap.
    sample = ds.scanner(columns=["dot_number"], limit=2000).to_table().to_pylist()
    dots = [r["dot_number"] for r in sample if r["dot_number"]]
    if not dots:
        return []
    random.shuffle(dots)
    return dots[:n]


def _bench_per_dot_lookup(ds, dot_numbers: list[str]) -> dict:
    """Time per-DOT random-access lookups against the Lance dataset.

    Lance's headline benefit: per-row random-access in milliseconds, not the
    1-10s a Parquet full-scan would take. We measure latency-per-lookup as
    the median of N samples.
    """
    timings_ms: list[float] = []
    for dot in dot_numbers:
        t0 = time.perf_counter()
        # filter pushdown — Lance evaluates this without reading the full file
        rows = ds.scanner(
            filter=f"dot_number = '{dot}'",
            columns=["dot_number", "legal_name", "phy_state", "power_units_int"],
        ).to_table().to_pylist()
        dur_ms = (time.perf_counter() - t0) * 1000
        timings_ms.append(dur_ms)
        # Sanity — every DOT we picked should round-trip a single row.
        if len(rows) != 1:
            LOG.warning("DOT %r returned %d rows (expected 1)", dot, len(rows))
    timings_ms.sort()
    n = len(timings_ms)
    return {
        "samples": n,
        "min_ms": round(timings_ms[0], 2) if n else None,
        "p50_ms": round(timings_ms[n // 2], 2) if n else None,
        "p90_ms": round(timings_ms[int(n * 0.9)], 2) if n else None,
        "max_ms": round(timings_ms[-1], 2) if n else None,
    }


def _bench_full_scan(ds) -> dict:
    """Time a full count + a full-column projection (Lance vs eventual Parquet).

    Lance is OPTIMIZED for random-access; full-scan parity-with-Parquet is
    not a stretch goal. We capture this anyway for the measurement doc so
    future cycles can see how Lance scan performance evolves.
    """
    t0 = time.perf_counter()
    n = ds.count_rows()
    cnt_ms = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    # Project a representative subset (everything we'd use in a serving query)
    _ = ds.scanner(columns=[
        "dot_number", "legal_name", "phy_state", "power_units_int",
        "total_drivers_int", "has_email", "is_free_mail_domain",
        "email_domain_normalized", "fleet_bucket",
    ]).to_table()
    proj_ms = (time.perf_counter() - t1) * 1000
    return {"count_rows_ms": round(cnt_ms, 2), "projection_scan_ms": round(proj_ms, 2),
            "rows_scanned": n}


def _bench_duckdb_roundtrip(ds) -> dict:
    """Sanity: Lance → Arrow → DuckDB → COUNT(*) round-trips correctly.

    The eventual lance-duckdb extension will skip this round-trip. For now,
    confirm SQL ergonomics work via Arrow handoff.
    """
    import duckdb
    t0 = time.perf_counter()
    arrow_tbl = ds.scanner(columns=["dot_number"], limit=100_000).to_table()
    con = duckdb.connect()
    con.register("lance_sample", arrow_tbl)
    cnt = con.execute("SELECT COUNT(*) FROM lance_sample").fetchone()[0]
    dur_ms = (time.perf_counter() - t0) * 1000
    con.close()
    return {"sample_rows_via_duckdb": cnt, "duration_ms": round(dur_ms, 2)}


def _bench_schema_evolution(ds) -> dict:
    """Add a synthetic column to the Lance dataset, time it, then drop.

    This is the headline schema-evolution benefit: Parquet would require
    rewriting all 419 MB to add a column. Lance writes only the new column's
    file. We add, measure, then drop so we don't pollute production data.

    Lance's add_columns takes a SQL expression. We add a constant NULL
    typed string column. The expectation is sub-5-second wall time.
    """
    canary_col = "canary_test_column"
    # Add — Lance new API uses add_columns(transforms=[{...}]) but older
    # versions use a different signature. Try the LanceDataset.alter_columns /
    # add_columns path. We use SQL expression form per pylance 0.32+ docs.
    t0 = time.perf_counter()
    try:
        # transforms list of (new_name, expr); 'cast(null as string)' is a
        # DataFusion-evaluated SQL expression yielding a typed NULL column.
        ds.add_columns({canary_col: "cast(null as string)"})
    except Exception as e:
        LOG.warning("add_columns failed: %s", e)
        return {"add_seconds": None, "drop_seconds": None, "error": str(e)}
    add_dur = time.perf_counter() - t0

    # Drop — Lance's drop_columns. Reload the dataset since add_columns may
    # have created a new version.
    import lance
    ds2 = lance.dataset(LANCE_URI, storage_options=_lance_storage_options())
    t1 = time.perf_counter()
    try:
        ds2.drop_columns([canary_col])
        drop_dur = time.perf_counter() - t1
    except Exception as e:
        LOG.warning("drop_columns failed: %s", e)
        drop_dur = None

    return {
        "add_seconds": round(add_dur, 3),
        "drop_seconds": round(drop_dur, 3) if drop_dur is not None else None,
        "canary_col": canary_col,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON, not human log")
    ap.add_argument(
        "--skip-schema-evo",
        action="store_true",
        help="Skip the schema-evolution gate (which mutates the dataset)",
    )
    ap.add_argument(
        "--per-dot-samples",
        type=int,
        default=10,
        help="Number of DOT-number samples for per-DOT lookup benchmark",
    )
    args = ap.parse_args()

    for v in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    ):
        if not os.environ.get(v):
            LOG.error("FAIL: %s not set in environment", v)
            return 64

    snapshot = _latest_snapshot()
    LOG.info("snapshot (parquet ground truth): %s", snapshot)

    # File-size deltas.
    parquet_bytes = _parquet_size_bytes(snapshot)
    lance_bytes = _lance_size_bytes()
    size_delta_pct = (
        (lance_bytes - parquet_bytes) / parquet_bytes * 100 if parquet_bytes else None
    )
    LOG.info(
        "size: parquet=%.1f MB lance=%.1f MB delta=%+.1f%%",
        parquet_bytes / 1e6, lance_bytes / 1e6, size_delta_pct or 0.0,
    )

    # Row-count parity.
    parquet_rows = _parquet_row_count(snapshot)
    ds = _open_lance()
    lance_rows = _lance_row_count(ds)
    drift_pct = abs(lance_rows - parquet_rows) / parquet_rows * 100 if parquet_rows else 999
    LOG.info(
        "rows: parquet=%d lance=%d drift=%+.4f%% (budget ±%.2f%%)",
        parquet_rows, lance_rows, (lance_rows - parquet_rows) / parquet_rows * 100,
        ROW_COUNT_TOLERANCE_PCT,
    )

    # Per-DOT random-access bench.
    dots = _sample_dot_numbers(ds, args.per_dot_samples)
    LOG.info("benchmarking %d per-DOT lookups ...", len(dots))
    per_dot_metrics = _bench_per_dot_lookup(ds, dots)
    LOG.info("per-DOT lookup: %s", per_dot_metrics)

    # Full-scan bench (informational).
    LOG.info("benchmarking full scan ...")
    full_scan_metrics = _bench_full_scan(ds)
    LOG.info("full scan: %s", full_scan_metrics)

    # DuckDB Arrow round-trip sanity.
    duckdb_metrics = _bench_duckdb_roundtrip(ds)
    LOG.info("DuckDB Arrow round-trip: %s", duckdb_metrics)

    # Schema evolution gate.
    schema_evo: dict[str, Any] | None = None
    if not args.skip_schema_evo:
        LOG.info("benchmarking schema evolution (add+drop canary_test_column) ...")
        schema_evo = _bench_schema_evolution(ds)
        LOG.info("schema evolution: %s", schema_evo)

    # Evaluate gates.
    gates: dict[str, bool] = {}
    gates["row_count_parity"] = drift_pct <= ROW_COUNT_TOLERANCE_PCT
    if per_dot_metrics.get("p50_ms") is not None:
        gates["per_dot_p50_under_budget"] = (
            per_dot_metrics["p50_ms"] < PER_DOT_LATENCY_MS_BUDGET
        )
    else:
        gates["per_dot_p50_under_budget"] = False
    if schema_evo and schema_evo.get("add_seconds") is not None:
        gates["schema_evolution_under_budget"] = (
            schema_evo["add_seconds"] < SCHEMA_EVO_SECONDS_BUDGET
        )
    else:
        gates["schema_evolution_under_budget"] = bool(args.skip_schema_evo)

    result = {
        "snapshot": snapshot,
        "parquet_bytes": parquet_bytes,
        "lance_bytes": lance_bytes,
        "size_delta_pct": round(size_delta_pct, 2) if size_delta_pct is not None else None,
        "parquet_rows": parquet_rows,
        "lance_rows": lance_rows,
        "row_drift_pct": round(drift_pct, 4),
        "per_dot": per_dot_metrics,
        "full_scan": full_scan_metrics,
        "duckdb_roundtrip": duckdb_metrics,
        "schema_evolution": schema_evo,
        "gates": gates,
        "all_gates_passed": all(gates.values()),
        "thresholds": {
            "row_count_tolerance_pct": ROW_COUNT_TOLERANCE_PCT,
            "per_dot_latency_ms_budget": PER_DOT_LATENCY_MS_BUDGET,
            "schema_evo_seconds_budget": SCHEMA_EVO_SECONDS_BUDGET,
        },
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        LOG.info("=" * 60)
        for k, v in gates.items():
            LOG.info("  GATE %-32s %s", k, "PASS" if v else "FAIL")
        LOG.info("=" * 60)
        LOG.info("all_gates_passed: %s", result["all_gates_passed"])

    return 0 if result["all_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
