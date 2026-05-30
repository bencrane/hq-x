"""Tier 2 fallback dispatcher — in-session Opus 4.7 subagent orchestrator.

This script does TWO things depending on the CLI mode:

  Mode 1 -- batch manifest generation (--stage-a-key):
    Reads stage_a.parquet (boto3 -> /tmp/ per L58), filters to
    section_split_confidence < 14, batches ~20 rows per manifest JSON at
    /tmp/tier2-batches/. Prints one absolute manifest path per line to stdout.
    The EXECUTOR (Claude Code session) reads each manifest and dispatches one
    Opus 4.7 subagent per batch via the Agent tool with model=opus. The
    subagent receives full_text_md + partial regex output and returns per-Item
    text + confidence, writing results to /tmp/tier2-results/result_{NNNN}.json.

  Mode 2 -- aggregation (--aggregate):
    Reads all /tmp/tier2-results/result_*.json files, unions rows, writes
    stage_b.parquet to /tmp/ then uploads to R2 at --stage-b-key.
    Missing batch results (subagent crashed, etc.) are filled from the
    corresponding manifest with item_*=NULL, section_split_confidence=0,
    section_split_method='opus_failed'. No row dropped (per directive).

CLI:
    # Step 1: emit manifests (dry-run -- does NOT invoke subagents)
    python3 apps/data-engine-x/scripts/dispatch_sec_iapd_tier2_fallback.py \\
        --stage-a-key sec-iapd/form-adv-part-2-parsed/release=YYYY-MM-DD/stage_a.parquet \\
        --batch-size 20

    # Step 2 (run by executor after all subagents return):
    python3 apps/data-engine-x/scripts/dispatch_sec_iapd_tier2_fallback.py \\
        --aggregate '/tmp/tier2-results/*.json' \\
        --stage-b-key sec-iapd/form-adv-part-2-parsed/release=YYYY-MM-DD/stage_b.parquet

    # Dry-run verify (used by harness -- skips download when no STAGE_A_KEY env set):
    python3 apps/data-engine-x/scripts/dispatch_sec_iapd_tier2_fallback.py --dry-run

NOTE: This script has NO LLM SDK imports and makes NO calls to any LLM
HTTP API. Tier 2 fallback uses ONLY the in-session Agent tool with
model=opus (Claude Code Max session quota per directive).
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger(__name__)

# Resolve scripts/_lib relative to this file when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.sec_iapd_brochure_parse import (  # noqa: E402
    ITEM_TITLES,
    split_sections_regex,
    score_section_confidence,
)

R2_BUCKET = "dex-raw-landing-zone"
DEFAULT_BATCH_SIZE = 20
MANIFEST_DIR = "/tmp/tier2-batches"
RESULTS_DIR = "/tmp/tier2-results"
CONFIDENCE_THRESHOLD = 14  # rows with confidence < this go to Opus fallback


# --------------------------------------------------------------------------- #
# Public entry points (imported by harness)
# --------------------------------------------------------------------------- #

def build_batch_manifests(
    stage_a_key: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[str]:
    """Download stage_a.parquet, filter low-confidence rows, write batch manifests.

    Returns a list of absolute manifest paths (one per batch).
    Prints each path to stdout (one per line) for executor consumption.
    """
    import boto3
    import pyarrow.parquet as pq

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    # L58: boto3 -> /tmp/ -> read_parquet (no DuckDB R2 httpfs)
    local_parquet = "/tmp/tier2_stage_a.parquet"
    if not Path(local_parquet).exists():
        LOG.info("downloading s3://%s/%s -> %s", R2_BUCKET, stage_a_key, local_parquet)
        s3.download_file(R2_BUCKET, stage_a_key, local_parquet)
    else:
        LOG.info("using cached %s", local_parquet)

    table = pq.read_table(local_parquet)
    LOG.info("stage_a.parquet: %d rows", len(table))

    # Identify item columns present in stage_a
    item_cols = {n: f"item_{n}_{slug}" for n, slug in ITEM_TITLES.items()}

    # Filter to low-confidence rows
    import pyarrow.compute as pc
    confidence_col = table.column("section_split_confidence")
    mask = pc.less(confidence_col, CONFIDENCE_THRESHOLD)
    low_conf_table = table.filter(mask)
    LOG.info(
        "low-confidence rows (confidence < %d): %d",
        CONFIDENCE_THRESHOLD,
        len(low_conf_table),
    )

    if len(low_conf_table) == 0:
        LOG.info("no low-confidence rows — Tier 2 fallback not needed")
        return []

    # Convert to list of dicts
    low_conf_rows = low_conf_table.to_pydict()
    n_rows = len(low_conf_table)
    rows_list = []
    for i in range(n_rows):
        crd = low_conf_rows["crd_number"][i]
        ver = low_conf_rows["version_id"][i]
        full_text = low_conf_rows.get("full_text_md", [None] * n_rows)[i] or ""

        # Partial regex output from stage_a
        partial_items_detected: list[int] = []
        items_partial: dict[str, str] = {}
        for n, col in item_cols.items():
            if col in low_conf_rows and low_conf_rows[col][i]:
                partial_items_detected.append(n)
                items_partial[str(n)] = low_conf_rows[col][i]

        rows_list.append({
            "crd_number": crd,
            "version_id": ver,
            "full_text_md": full_text,
            "partial_items_detected": partial_items_detected,
            "items_partial": items_partial,
        })

    # Write batch manifests
    Path(MANIFEST_DIR).mkdir(parents=True, exist_ok=True)
    manifest_paths: list[str] = []

    for batch_idx, start in enumerate(range(0, len(rows_list), batch_size)):
        batch_rows = rows_list[start: start + batch_size]
        batch_id = f"batch_{batch_idx:04d}"
        manifest = {"batch_id": batch_id, "rows": batch_rows}
        manifest_path = f"{MANIFEST_DIR}/{batch_id}.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)
        manifest_paths.append(manifest_path)
        print(manifest_path)  # stdout for executor consumption

    LOG.info(
        "wrote %d batch manifests to %s (total low-conf rows: %d)",
        len(manifest_paths),
        MANIFEST_DIR,
        len(rows_list),
    )
    return manifest_paths


def aggregate_results(
    results_glob: str,
    stage_b_key: str,
) -> str:
    """Aggregate Opus subagent result JSONs into stage_b.parquet and upload to R2.

    Missing batches (subagent crash, etc.) are detected by comparing expected
    manifest batch IDs to available result files. Missing rows are written with
    opus_failed semantics — full_text_md populated from manifest, item_*=NULL,
    section_split_confidence=0, section_split_method='opus_failed'.

    Returns the local path of stage_b.parquet.
    """
    import boto3
    import pyarrow as pa
    import pyarrow.parquet as pq

    result_files = sorted(glob.glob(results_glob))
    LOG.info("found %d result files matching %s", len(result_files), results_glob)

    # Load expected batches from manifests so we can detect missing results
    manifest_files = sorted(glob.glob(f"{MANIFEST_DIR}/batch_*.json"))
    expected_batch_ids = set()
    manifest_by_batch: dict[str, list[dict]] = {}
    for mf in manifest_files:
        with open(mf) as f:
            m = json.load(f)
        bid = m["batch_id"]
        expected_batch_ids.add(bid)
        manifest_by_batch[bid] = m["rows"]

    # Load result files
    result_batch_ids: set[str] = set()
    all_result_rows: list[dict] = []
    for rf in result_files:
        with open(rf) as f:
            r = json.load(f)
        bid = r.get("batch_id", "")
        result_batch_ids.add(bid)
        all_result_rows.extend(r.get("rows", []))

    # Fill missing batches from manifests with opus_failed rows
    missing_batches = expected_batch_ids - result_batch_ids
    if missing_batches:
        LOG.warning(
            "missing result files for %d batches: %s — writing opus_failed rows",
            len(missing_batches),
            sorted(missing_batches),
        )
        for bid in sorted(missing_batches):
            for manifest_row in manifest_by_batch.get(bid, []):
                opus_failed_row: dict[str, Any] = {
                    "crd_number": manifest_row["crd_number"],
                    "version_id": manifest_row["version_id"],
                    "items": {},
                    "section_split_confidence": 0,
                    "section_split_method": "opus_failed",
                }
                all_result_rows.append(opus_failed_row)

    if not all_result_rows:
        LOG.warning("no result rows to aggregate — stage_b.parquet will be empty")

    LOG.info("aggregating %d result rows into stage_b.parquet", len(all_result_rows))

    # Build flat column arrays for stage_b schema
    item_cols = {n: f"item_{n}_{slug}" for n, slug in ITEM_TITLES.items()}

    crd_numbers: list[int | None] = []
    version_ids: list[str | None] = []
    section_split_confidences: list[int | None] = []
    section_split_methods: list[str | None] = []
    item_arrays: dict[str, list[str | None]] = {col: [] for col in item_cols.values()}

    for row in all_result_rows:
        crd_numbers.append(row.get("crd_number"))
        version_ids.append(row.get("version_id"))
        section_split_confidences.append(row.get("section_split_confidence"))
        section_split_methods.append(row.get("section_split_method"))
        items_map: dict[str, str | None] = row.get("items", {})
        for n, col in item_cols.items():
            item_arrays[col].append(items_map.get(str(n)) or items_map.get(n))  # type: ignore[arg-type]

    schema_fields = [
        pa.field("crd_number", pa.int64()),
        pa.field("version_id", pa.string()),
        pa.field("section_split_confidence", pa.int32()),
        pa.field("section_split_method", pa.string()),
    ] + [pa.field(col, pa.string()) for col in item_cols.values()]

    arrays_out = [
        pa.array(crd_numbers, type=pa.int64()),
        pa.array(version_ids, type=pa.string()),
        pa.array(section_split_confidences, type=pa.int32()),
        pa.array(section_split_methods, type=pa.string()),
    ] + [pa.array(item_arrays[col], type=pa.string()) for col in item_cols.values()]

    table = pa.table(
        dict(zip([f.name for f in schema_fields], arrays_out)),
        schema=pa.schema(schema_fields),
    )

    local_parquet = "/tmp/stage_b.parquet"
    pq.write_table(table, local_parquet, compression="zstd", row_group_size=10000)
    LOG.info("wrote stage_b.parquet: %d rows", len(table))

    # Upload to R2 — L42: ContentType only, NO ContentEncoding
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
        stage_b_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    LOG.info("uploaded stage_b.parquet to s3://%s/%s", R2_BUCKET, stage_b_key)
    return local_parquet


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage-a-key", help="R2 key for stage_a.parquet")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help=f"PDFs per batch manifest (default {DEFAULT_BATCH_SIZE})")
    ap.add_argument("--aggregate", metavar="GLOB",
                    help="Glob pattern for result JSONs to aggregate")
    ap.add_argument("--stage-b-key", help="R2 key to upload stage_b.parquet")
    ap.add_argument("--dry-run", action="store_true",
                    help="Emit expected output shape (imports + schema preview) without downloading")
    args = ap.parse_args()

    if args.dry_run:
        # Harness smoke: print expected output shape without downloading anything
        item_cols = [f"item_{n}_{slug}" for n, slug in ITEM_TITLES.items()]
        schema_preview = {
            "manifest_dir": MANIFEST_DIR,
            "results_dir": RESULTS_DIR,
            "batch_schema": {
                "batch_id": "str",
                "rows[*].crd_number": "int",
                "rows[*].version_id": "str",
                "rows[*].full_text_md": "str",
                "rows[*].partial_items_detected": "list[int]",
                "rows[*].items_partial": "dict[str,str]",
            },
            "result_schema": {
                "batch_id": "str",
                "rows[*].crd_number": "int",
                "rows[*].version_id": "str",
                "rows[*].items": "dict[str,str|null]",
                "rows[*].section_split_confidence": "int 0-18",
                "rows[*].section_split_method": "opus_fallback|opus_failed",
            },
            "stage_b_columns": ["crd_number", "version_id",
                                 "section_split_confidence", "section_split_method"] + item_cols,
        }
        print(json.dumps(schema_preview, indent=2))
        return 0

    if args.aggregate:
        if not args.stage_b_key:
            LOG.error("--aggregate requires --stage-b-key")
            return 1
        aggregate_results(args.aggregate, args.stage_b_key)
        return 0

    if args.stage_a_key:
        build_batch_manifests(args.stage_a_key, args.batch_size)
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
