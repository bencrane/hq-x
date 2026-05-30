"""Tier 3 Item 5 extraction dispatcher — Opus 4.7 subagent orchestrator.

Architecturally a copy of dispatch_sec_iapd_tier2_fallback.py but with three modes
instead of two — adds a Lance add_columns step at the end (the scale-up gate).

  Mode 1 -- batch manifest generation (--prepare-batches):
    Reads sec_adv.part_2_brochures_lance, dedupes to latest version_id per crd_number
    (by filing_date desc, version_id desc), filters to rows where item_5 text is
    non-empty, batches --batch-size rows per manifest JSON at /tmp/tier3-item5-batches/.
    Optional --sample N selects N rows total (stratified random if --stratified) for
    tight-loop runs. Prints one manifest path per line to stdout.
    The EXECUTOR (Claude Code session) reads each manifest and dispatches one Opus
    subagent per batch via the Agent tool with model=opus. Each subagent reads
    /tmp/sec_iapd_tier3_item5_subagent_prompt.txt for protocol, processes the
    manifest, writes /tmp/tier3-item5-results/result_{NNNN}.json per the schema in
    apps/data-engine-x/scripts/_lib/sec_iapd_brochure_tier3_extract.py.

  Mode 2 -- aggregation + Pydantic validation (--aggregate):
    Reads all /tmp/tier3-item5-results/*.json, validates every row against
    Item5Extraction Pydantic schema, writes valid rows to stage_c_item5.parquet on R2.
    Invalid rows logged with full error trace; emit_invalid_rows lists them for retry.

  Mode 3 -- cross-validate against Part 1 (--cross-validate):
    Joins stage_c_item5.parquet to sec_adv.base_a_lance on crd_number (using latest
    Part 1 filing per CRD), compares aum_referenced_in_item_5_usd to Part 1 raw_json.5F2c
    (total assets under management). Reports match rate and outlier list.

CLI:
    # 1. emit batch manifests for tight-loop sample
    python3 apps/data-engine-x/scripts/dispatch_sec_iapd_tier3_item5_extract.py \\
        --prepare-batches --sample 100 --stratified --batch-size 20

    # 2. EXECUTOR loop — dispatch Opus subagents (one per batch path printed above)
    # ... [external; this script doesn't invoke Agent tool itself]

    # 3. aggregate + validate
    python3 apps/data-engine-x/scripts/dispatch_sec_iapd_tier3_item5_extract.py \\
        --aggregate '/tmp/tier3-item5-results/*.json' \\
        --stage-c-key sec-iapd/form-adv-part-2-extracted/release=YYYY-MM-DD/stage_c_item5.parquet

    # 4. cross-validate against Part 1 AUM
    python3 apps/data-engine-x/scripts/dispatch_sec_iapd_tier3_item5_extract.py \\
        --cross-validate \\
        --stage-c-key sec-iapd/form-adv-part-2-extracted/release=YYYY-MM-DD/stage_c_item5.parquet

NOTE: NO LLM SDK imports. NO HTTP calls to any LLM API. Subagents are dispatched
by the Claude Code executor session via the in-session Agent tool with model=opus,
consuming Claude Code Max session quota.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger(__name__)

# Resolve scripts/_lib relative to this file when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.sec_iapd_brochure_tier3_extract import (  # noqa: E402
    EXTRACTOR_VERSION,
    ITEM_CONFIG,
)

R2_BUCKET = "dex-raw-landing-zone"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sec_adv/part_2_brochures_lance/"
PART_1_LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sec_adv/base_a_lance/"
DEFAULT_BATCH_SIZE = 20


def _cfg(item: int) -> dict:
    if item not in ITEM_CONFIG:
        raise ValueError(f"unsupported item={item}; supported: {sorted(ITEM_CONFIG)}")
    return ITEM_CONFIG[item]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _r2_storage_options() -> dict:
    return {
        "endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "auto",
    }


def _r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _latest_version_per_crd_rows(
    item: int,
    min_text_length: int = 50,
    brochure_type_filter: str | None = None,
) -> list[dict]:
    """Read the Tier 1+2 Lance dataset, return one row per crd_number — the latest
    version_id by (filing_date DESC, version_id DESC). Item-N text included.

    Filters (applied BEFORE the latest-per-CRD window):
      - item text length >= min_text_length
      - brochure_type matches brochure_type_filter if set (e.g. 'Part2A_FirmBrochure')

    Returns: [{crd_number, version_id, filing_date, item_N_text}, ...]
    """
    import lance, duckdb
    cfg = _cfg(item)
    source_col = cfg["lance_source_col"]
    text_alias = cfg["manifest_text_key"]

    ds = lance.dataset(LANCE_URI, storage_options=_r2_storage_options())
    tbl = ds.to_table(
        columns=["crd_number", "version_id", "filing_date", "brochure_type", source_col]
    )
    con = duckdb.connect()
    con.register("brochures", tbl)
    where_clauses = [
        f"{source_col} IS NOT NULL",
        f"length({source_col}) >= {int(min_text_length)}",
    ]
    if brochure_type_filter:
        safe = brochure_type_filter.replace("'", "''")
        where_clauses.append(f"brochure_type = '{safe}'")
    where_sql = " AND ".join(where_clauses)
    query = f"""
        WITH ranked AS (
            SELECT
                crd_number,
                version_id,
                filing_date,
                brochure_type,
                {source_col} AS {text_alias},
                ROW_NUMBER() OVER (
                    PARTITION BY crd_number
                    ORDER BY filing_date DESC NULLS LAST, version_id DESC
                ) AS rn
            FROM brochures
            WHERE {where_sql}
        )
        SELECT crd_number, version_id, filing_date, {text_alias}
        FROM ranked
        WHERE rn = 1
        ORDER BY crd_number
    """
    res = con.execute(query).fetchall()
    cols = [d[0] for d in con.description]
    con.close()
    return [dict(zip(cols, r)) for r in res]


def _stratified_sample(rows: list[dict], text_key: str, n: int, seed: int = 42) -> list[dict]:
    """Stratify by item text length and pick representative variance across the corpus."""
    rng = random.Random(seed)
    buckets = {"very_short": [], "short": [], "medium": [], "long": [], "very_long": []}
    for r in rows:
        l = len(r[text_key])
        if l < 500: buckets["very_short"].append(r)
        elif l < 2000: buckets["short"].append(r)
        elif l < 8000: buckets["medium"].append(r)
        elif l < 25000: buckets["long"].append(r)
        else: buckets["very_long"].append(r)
    LOG.info("stratification buckets: %s", {k: len(v) for k, v in buckets.items()})
    # Allocate sample roughly proportional to bucket size, with min=2 per bucket
    total = sum(len(v) for v in buckets.values())
    out = []
    for name, bucket in buckets.items():
        if not bucket:
            continue
        share = max(2, round(n * len(bucket) / total))
        share = min(share, len(bucket))
        out.extend(rng.sample(bucket, share))
    # Trim to exactly n
    rng.shuffle(out)
    return out[:n]


# --------------------------------------------------------------------------- #
# Mode 1: prepare batches
# --------------------------------------------------------------------------- #

def prepare_batches(
    item: int,
    batch_size: int,
    sample: int | None,
    stratified: bool,
    min_text_length: int = 50,
    brochure_type_filter: str | None = None,
) -> list[str]:
    cfg = _cfg(item)
    text_key = cfg["manifest_text_key"]
    manifest_dir = cfg["batches_dir"]
    results_dir = cfg["results_dir"]

    rows = _latest_version_per_crd_rows(
        item=item, min_text_length=min_text_length, brochure_type_filter=brochure_type_filter,
    )
    LOG.info(
        "item %d latest-version-per-crd rows (min_length=%d, brochure_type=%s): %d",
        item, min_text_length, brochure_type_filter or "(any)", len(rows),
    )
    if sample and sample < len(rows):
        rows = _stratified_sample(rows, text_key, sample) if stratified else rows[:sample]
        LOG.info("sampled down to %d rows (stratified=%s)", len(rows), stratified)

    Path(manifest_dir).mkdir(parents=True, exist_ok=True)
    Path(results_dir).mkdir(parents=True, exist_ok=True)
    manifest_paths: list[str] = []

    for batch_idx, start in enumerate(range(0, len(rows), batch_size)):
        batch = rows[start: start + batch_size]
        batch_id = f"batch_{batch_idx:04d}"
        manifest = {
            "batch_id": batch_id,
            "extractor_version": EXTRACTOR_VERSION,
            "item": item,
            "rows": [
                {
                    "crd_number": int(r["crd_number"]),
                    "version_id": str(r["version_id"]),
                    text_key: str(r[text_key]),
                }
                for r in batch
            ],
        }
        p = f"{manifest_dir}/{batch_id}.json"
        with open(p, "w") as f:
            json.dump(manifest, f)
        manifest_paths.append(p)
        print(p)  # stdout for executor

    LOG.info("wrote %d batch manifests covering %d rows", len(manifest_paths), len(rows))
    return manifest_paths


# --------------------------------------------------------------------------- #
# Mode 2: aggregate + validate
# --------------------------------------------------------------------------- #

def aggregate_results(item: int, results_glob: str, stage_c_key: str) -> dict:
    """Read all result JSONs, validate every row against the item's schema, write
    valid rows to stage_c_item{N}.parquet on R2. Return metrics + invalid-row list.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    from pydantic import ValidationError

    cfg = _cfg(item)
    schema_cls = cfg["schema_cls"]
    to_lance_fn = cfg["to_lance_row"]

    files = sorted(glob.glob(results_glob))
    LOG.info("found %d result files matching %s", len(files), results_glob)

    valid_rows: list[dict] = []
    invalid: list[dict] = []

    for f in files:
        with open(f) as fh:
            payload = json.load(fh)
        batch_id = payload.get("batch_id", Path(f).stem)
        for raw in payload.get("rows", []):
            try:
                ext = schema_cls.model_validate(raw)
            except ValidationError as e:
                invalid.append({
                    "batch_id": batch_id,
                    "crd_number": raw.get("crd_number"),
                    "version_id": raw.get("version_id"),
                    "error": str(e)[:400],
                })
                continue
            valid_rows.append(to_lance_fn(ext))

    LOG.info(
        "item %d: aggregated %d valid rows, %d invalid rows (from %d files)",
        item, len(valid_rows), len(invalid), len(files),
    )
    if invalid:
        LOG.warning("first 5 invalid rows: %s", invalid[:5])

    if not valid_rows:
        LOG.error("FAIL: zero valid rows after Pydantic validation")
        return {"valid_rows": 0, "invalid_rows": len(invalid), "invalid": invalid}

    cols: dict[str, list] = {k: [] for k in valid_rows[0].keys()}
    for r in valid_rows:
        for k, v in r.items():
            cols[k].append(v)
    table = pa.table(cols)

    local_path = f"/tmp/sec_iapd_tier3_item{item}_stage_c.parquet"
    pq.write_table(table, local_path, compression="zstd", compression_level=9)
    LOG.info("wrote %s (%d rows)", local_path, len(valid_rows))

    s3 = _r2_client()
    s3.upload_file(
        local_path, R2_BUCKET, stage_c_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    LOG.info("uploaded stage_c_item%d.parquet to s3://%s/%s", item, R2_BUCKET, stage_c_key)

    return {
        "valid_rows": len(valid_rows),
        "invalid_rows": len(invalid),
        "invalid": invalid,
        "stage_c_key": stage_c_key,
    }


# --------------------------------------------------------------------------- #
# Mode 3: cross-validate against Part 1 5F2c AUM
# --------------------------------------------------------------------------- #

def cross_validate(stage_c_key: str, tolerance_pct: float = 25.0) -> dict:
    """Compare Item 5 extracted AUM against Part 1 5F2c (total AUM). Latest Part 1
    filing per CRD wins. Reports match rate; flags outliers > tolerance_pct% off.
    """
    import duckdb, lance

    s3 = _r2_client()
    local_c = "/tmp/sec_iapd_tier3_item5_stage_c.parquet"
    s3.download_file(R2_BUCKET, stage_c_key, local_c)

    # Load Part 1 Lance and extract latest-per-CRD 5F2c
    part1 = lance.dataset(PART_1_LANCE_URI, storage_options=_r2_storage_options())
    part1_tbl = part1.to_table(columns=["crd_number", "date_submitted", "raw_json"])
    con = duckdb.connect()
    con.register("part1", part1_tbl)
    con.execute(f"CREATE OR REPLACE VIEW stage_c AS SELECT * FROM read_parquet('{local_c}')")

    part1_aum_query = """
        WITH ranked AS (
            SELECT
                crd_number,
                date_submitted,
                TRY_CAST(json_extract_string(raw_json, '$.\"5F2c\"') AS DOUBLE) AS part1_aum_usd,
                ROW_NUMBER() OVER (PARTITION BY crd_number ORDER BY date_submitted DESC) AS rn
            FROM part1
            WHERE crd_number IS NOT NULL
        )
        SELECT crd_number, part1_aum_usd
        FROM ranked
        WHERE rn = 1 AND part1_aum_usd IS NOT NULL AND part1_aum_usd > 0
    """
    con.execute(f"CREATE TEMP TABLE part1_aum AS {part1_aum_query}")

    rows = con.execute("""
        SELECT
            c.crd_number,
            c.aum_referenced_in_item_5_usd AS tier3_aum,
            p.part1_aum_usd                AS part1_aum
        FROM stage_c c
        JOIN part1_aum p USING (crd_number)
        WHERE c.aum_referenced_in_item_5_usd IS NOT NULL
    """).fetchall()
    con.close()

    if not rows:
        LOG.warning("zero rows with Tier3 AUM AND Part1 AUM available — cross-validation skipped")
        return {"cross_validated_rows": 0}

    matches, outliers = 0, []
    for crd, t3, p1 in rows:
        if p1 == 0:
            continue
        pct_diff = abs(t3 - p1) / p1 * 100
        if pct_diff <= tolerance_pct:
            matches += 1
        else:
            outliers.append({"crd_number": crd, "tier3_aum": t3, "part1_aum": p1, "pct_diff": round(pct_diff, 1)})

    rate = matches / len(rows)
    LOG.info(
        "AUM cross-validation: %d/%d rows match within %.0f%% (%.1f%%)",
        matches, len(rows), tolerance_pct, rate * 100,
    )
    if outliers:
        LOG.warning("first 10 outliers: %s", outliers[:10])

    return {
        "cross_validated_rows": len(rows),
        "matches_within_tolerance": matches,
        "match_rate_pct": round(rate * 100, 1),
        "tolerance_pct": tolerance_pct,
        "outliers": outliers,
    }


# --------------------------------------------------------------------------- #
# Mode 4: validate-distributions (internal consistency + distribution checks)
# --------------------------------------------------------------------------- #

def validate_distributions(item: int, stage_c_key: str) -> dict:
    """Item-aware internal consistency + distribution sanity checks on stage_c parquet."""
    import duckdb
    s3 = _r2_client()
    local = f"/tmp/sec_iapd_tier3_item{item}_validate.parquet"
    s3.download_file(R2_BUCKET, stage_c_key, local)

    con = duckdb.connect()
    con.execute(f"CREATE OR REPLACE VIEW stg AS SELECT * FROM read_parquet('{local}')")
    total = con.execute("SELECT count(*) FROM stg").fetchone()[0]
    extracted_col = f"item_{item}_extracted"
    extracted = con.execute(f"SELECT count(*) FROM stg WHERE {extracted_col}").fetchone()[0]

    if item == 4:
        return _validate_item4(con, total, extracted)

    checks: dict[str, dict] = {}

    # C1: pct_aum_tiered implies non-empty aum_tier_breakpoints
    c1_violations = con.execute("""
        SELECT crd_number, version_id FROM stg
        WHERE list_contains(CAST(json_extract(fee_structure_types, '$') AS VARCHAR[]), 'pct_aum_tiered')
          AND json_array_length(aum_tier_breakpoints) = 0
    """).fetchall()
    checks["C1_tiered_requires_breakpoints"] = {
        "violations": len(c1_violations),
        "sample": [(r[0], r[1]) for r in c1_violations[:5]],
    }

    # C2: performance_fee_pct not null implies 'performance_fee' in fee_structure_types
    c2_violations = con.execute("""
        SELECT crd_number, version_id, performance_fee_pct FROM stg
        WHERE performance_fee_pct IS NOT NULL
          AND NOT list_contains(CAST(json_extract(fee_structure_types, '$') AS VARCHAR[]), 'performance_fee')
    """).fetchall()
    checks["C2_perf_fee_pct_requires_perf_type"] = {
        "violations": len(c2_violations),
        "sample": [(r[0], r[1], r[2]) for r in c2_violations[:5]],
    }

    # D1: median performance_fee_pct in [5, 35] (typical hedge-fund/RIA range 10-30)
    d1 = con.execute("""
        SELECT median(performance_fee_pct), count(*)
        FROM stg WHERE performance_fee_pct IS NOT NULL
    """).fetchone()
    checks["D1_performance_fee_pct_median"] = {
        "median": d1[0], "n": d1[1],
        "expected_range": [5, 35],
        "pass": d1[0] is None or (5 <= d1[0] <= 35),
    }

    # D2: median minimum_account_size_usd in [50K, 10M] (typical retail-to-HNW range)
    d2 = con.execute("""
        SELECT median(minimum_account_size_usd), count(*)
        FROM stg WHERE minimum_account_size_usd IS NOT NULL
    """).fetchone()
    checks["D2_min_account_size_median"] = {
        "median": d2[0], "n": d2[1],
        "expected_range": [50000, 10000000],
        "pass": d2[0] is None or (50000 <= d2[0] <= 10000000),
    }

    # D3: median lowest-tier AUM rate in [0.25, 3.0] %
    # Lowest tier = first element of aum_tier_breakpoints when non-empty
    d3 = con.execute("""
        WITH lowest_rates AS (
            SELECT TRY_CAST(json_extract(aum_tier_breakpoints, '$[0].rate_pct') AS DOUBLE) AS r
            FROM stg
            WHERE json_array_length(aum_tier_breakpoints) > 0
        )
        SELECT median(r), count(*) FROM lowest_rates WHERE r IS NOT NULL
    """).fetchone()
    checks["D3_lowest_aum_tier_rate_median"] = {
        "median": d3[0], "n": d3[1],
        "expected_range": [0.25, 3.0],
        "pass": d3[0] is None or (0.25 <= d3[0] <= 3.0),
    }

    # D4: >= 80% of extracted rows have at least one fee_structure_type
    d4 = con.execute("""
        SELECT
            count_if(json_array_length(fee_structure_types) > 0) AS with_types,
            count(*) AS total
        FROM stg WHERE item_5_extracted
    """).fetchone()
    rate = (d4[0] / d4[1]) if d4[1] else 0
    checks["D4_extracted_rows_with_fee_types"] = {
        "with_types": d4[0], "total_extracted": d4[1],
        "rate_pct": round(rate * 100, 1),
        "expected_pct_floor": 80,
        "pass": rate >= 0.80,
    }

    con.close()

    all_pass = all(
        v.get("pass", True if v.get("violations", 0) == 0 else False)
        for v in checks.values()
    )
    LOG.info("validate-distributions: total=%d extracted=%d all_pass=%s",
             total, extracted, all_pass)
    for name, v in checks.items():
        LOG.info("  %s: %s", name, v)

    return {
        "total_rows": total,
        "extracted_rows": extracted,
        "checks": checks,
        "all_pass": all_pass,
    }


def _validate_item4(con, total: int, extracted: int) -> dict:
    """Distribution + consistency checks specific to Item 4 (Advisory Business)."""
    checks: dict[str, dict] = {}

    # C1: discretionary + non_discretionary ≈ total (within 10% tolerance) when all populated
    c1 = con.execute("""
        SELECT count(*) AS n_triple,
               count_if(abs(coalesce(discretionary_aum_usd,0) + coalesce(non_discretionary_aum_usd,0) - total_aum_usd)
                        / total_aum_usd > 0.10) AS gt_10pct_off
        FROM stg
        WHERE total_aum_usd IS NOT NULL AND total_aum_usd > 0
          AND discretionary_aum_usd IS NOT NULL AND non_discretionary_aum_usd IS NOT NULL
    """).fetchone()
    checks["C1_aum_split_consistency"] = {
        "rows_with_all_three": c1[0], "gt_10pct_off": c1[1],
        "pass": c1[0] == 0 or c1[1] / c1[0] < 0.05,
    }

    # D1: median total_aum_usd in [10M, 10B] — RIA universe spans these
    d1 = con.execute("SELECT median(total_aum_usd), count(*) FROM stg WHERE total_aum_usd IS NOT NULL AND total_aum_usd > 0").fetchone()
    checks["D1_total_aum_median"] = {
        "median": d1[0], "n": d1[1], "expected_range": [10_000_000, 10_000_000_000],
        "pass": d1[0] is None or (10_000_000 <= d1[0] <= 10_000_000_000),
    }

    # D2: firm_founded_year — median should be plausible
    d2 = con.execute("SELECT median(firm_founded_year), count(*) FROM stg WHERE firm_founded_year IS NOT NULL").fetchone()
    checks["D2_firm_founded_year_median"] = {
        "median": d2[0], "n": d2[1], "expected_range": [1950, 2025],
        "pass": d2[0] is None or (1950 <= d2[0] <= 2025),
    }

    # D3: extracted rows with at least one service should be high
    d3 = con.execute("""
        SELECT count_if(json_array_length(services_offered) > 0) AS with_svc, count(*) AS total
        FROM stg WHERE item_4_extracted
    """).fetchone()
    rate = (d3[0] / d3[1]) if d3[1] else 0
    checks["D3_extracted_rows_with_services"] = {
        "with_services": d3[0], "total_extracted": d3[1], "rate_pct": round(rate * 100, 1),
        "expected_pct_floor": 70, "pass": rate >= 0.70,
    }

    con.close()
    all_pass = all(v.get("pass", True) for v in checks.values())
    LOG.info("validate-distributions (item 4): total=%d extracted=%d all_pass=%s", total, extracted, all_pass)
    for name, v in checks.items():
        LOG.info("  %s: %s", name, v)
    return {"total_rows": total, "extracted_rows": extracted, "checks": checks, "all_pass": all_pass}


def cross_validate_item4(stage_c_key: str, tolerance_pct: float = 25.0) -> dict:
    """Item 4 cross-validation: total_aum_usd vs Part 1 5F2c."""
    import duckdb, lance
    s3 = _r2_client()
    local_c = "/tmp/sec_iapd_tier3_item4_stage_c.parquet"
    s3.download_file(R2_BUCKET, stage_c_key, local_c)

    part1 = lance.dataset(PART_1_LANCE_URI, storage_options=_r2_storage_options())
    part1_tbl = part1.to_table(columns=["crd_number", "date_submitted", "raw_json"])
    con = duckdb.connect()
    con.register('part1', part1_tbl)
    con.execute(f"CREATE OR REPLACE VIEW stage_c AS SELECT * FROM read_parquet('{local_c}')")

    con.execute("""
        CREATE TEMP TABLE part1_aum AS
        WITH ranked AS (
            SELECT crd_number, date_submitted,
                   TRY_CAST(json_extract_string(raw_json, '$."5F2c"') AS DOUBLE) AS part1_aum_usd,
                   ROW_NUMBER() OVER (PARTITION BY crd_number ORDER BY date_submitted DESC) AS rn
            FROM part1
            WHERE crd_number IS NOT NULL
        )
        SELECT crd_number, part1_aum_usd FROM ranked
        WHERE rn = 1 AND part1_aum_usd IS NOT NULL AND part1_aum_usd > 0
    """)

    rows = con.execute("""
        SELECT c.crd_number, c.total_aum_usd, p.part1_aum_usd
        FROM stage_c c JOIN part1_aum p USING (crd_number)
        WHERE c.total_aum_usd IS NOT NULL AND c.total_aum_usd > 0
    """).fetchall()
    con.close()

    if not rows:
        LOG.warning("zero rows with Item4 AUM AND Part1 AUM available")
        return {"cross_validated_rows": 0}

    matches, outliers = 0, []
    for crd, t3, p1 in rows:
        pct_diff = abs(t3 - p1) / p1 * 100
        if pct_diff <= tolerance_pct:
            matches += 1
        else:
            outliers.append({"crd_number": crd, "tier3_aum": t3, "part1_aum": p1, "pct_diff": round(pct_diff, 1)})
    rate = matches / len(rows)
    LOG.info("AUM cross-validation (Item 4 vs Part 1 5F2c): %d/%d within %.0f%% (%.1f%%)",
             matches, len(rows), tolerance_pct, rate * 100)
    if outliers:
        LOG.warning("first 10 outliers: %s", outliers[:10])
    return {"cross_validated_rows": len(rows), "matches_within_tolerance": matches,
            "match_rate_pct": round(rate * 100, 1), "tolerance_pct": tolerance_pct,
            "outliers": outliers}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="mode", required=True)

    p1 = sp.add_parser("prepare-batches", help="Generate batch manifest JSONs")
    p1.add_argument("--item", type=int, default=5, choices=sorted(ITEM_CONFIG))
    p1.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p1.add_argument("--sample", type=int, default=None, help="Subsample to N rows total")
    p1.add_argument("--stratified", action="store_true", help="Stratified sample by item-text length")
    p1.add_argument("--min-length", type=int, default=50,
                    help="Min item text length to include. Raise to 500+ to skip TOC-only slices.")
    p1.add_argument("--brochure-type", default=None,
                    help="Filter to a specific brochure_type (e.g. 'Part2A_FirmBrochure').")

    p2 = sp.add_parser("aggregate", help="Aggregate results into stage_c_item{N}.parquet on R2")
    p2.add_argument("--item", type=int, default=5, choices=sorted(ITEM_CONFIG))
    p2.add_argument("--results-glob", default=None,
                    help="Default: cfg.results_dir/result_*.json for the selected item")
    p2.add_argument("--stage-c-key", required=True)

    p3 = sp.add_parser("cross-validate", help="Compare extracted AUM vs Part 1 5F2c")
    p3.add_argument("--item", type=int, default=5, choices=sorted(ITEM_CONFIG))
    p3.add_argument("--stage-c-key", required=True)
    p3.add_argument("--tolerance-pct", type=float, default=25.0)

    p4 = sp.add_parser("validate-distributions",
                       help="Internal consistency + distribution sanity checks")
    p4.add_argument("--item", type=int, default=5, choices=sorted(ITEM_CONFIG))
    p4.add_argument("--stage-c-key", required=True)

    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    if args.mode == "prepare-batches":
        paths = prepare_batches(
            args.item, args.batch_size, args.sample, args.stratified,
            min_text_length=args.min_length,
            brochure_type_filter=args.brochure_type,
        )
        return 0 if paths else 1
    if args.mode == "aggregate":
        results_glob = args.results_glob or f"{_cfg(args.item)['results_dir']}/result_*.json"
        out = aggregate_results(args.item, results_glob, args.stage_c_key)
        LOG.info("aggregate metrics: valid=%d invalid=%d",
                 out.get("valid_rows", 0), out.get("invalid_rows", 0))
        return 0 if out.get("valid_rows", 0) > 0 else 1
    if args.mode == "cross-validate":
        if args.item == 4:
            out = cross_validate_item4(args.stage_c_key, args.tolerance_pct)
        else:
            out = cross_validate(args.stage_c_key, args.tolerance_pct)
        LOG.info("cross-validate: %s", {k: v for k, v in out.items() if k != "outliers"})
        return 0
    if args.mode == "validate-distributions":
        out = validate_distributions(args.item, args.stage_c_key)
        return 0 if out["all_pass"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
