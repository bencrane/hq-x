#!/usr/bin/env python3
"""ACRIS full backfill — bulk per-dataset Socrata download + chunked load.

Usage:
    doppler run -- python3 scripts/run_acris_full_backfill.py rp-master
    doppler run -- python3 scripts/run_acris_full_backfill.py rp-legals
    doppler run -- python3 scripts/run_acris_full_backfill.py lookup-codes
    doppler run -- python3 scripts/run_acris_full_backfill.py all

    # Resume an interrupted bulk load against an already-downloaded CSV:
    doppler run -- python3 scripts/run_acris_full_backfill.py rp-master \\
        --csv-path /tmp/acris/bnx9-e6tj.csv

Mode selection:
    Fact tables (rp-*, pp-*) use CSV streaming — single multi-GB download
    to /tmp/acris/{4x4}.csv, then chunked load. Lookup tables use SODA
    JSON pagination (they're tiny).

Idempotency:
    Every insert is ON CONFLICT DO NOTHING on each dataset's natural key.
    Re-running against the same source is safe; it inserts the (small) new
    rows and skips the rest.

Run audit:
    Each invocation creates one row per dataset in ops.acris_ingest_runs
    (mig 146).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

# Make sibling module importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from acris_common import (
    ALL_DATASET_KEYS,
    DEFAULT_CHUNK_SIZE,
    DATASETS,
    LOOKUP_KEYS,
    DatasetConfig,
    build_persist_row,
    chunked_upsert,
    connect,
    get_table_columns,
    ingest_run,
    mark_run_completed,
    paginate_socrata_json,
    stream_socrata_csv,
    update_run_progress,
)

logger = logging.getLogger("acris-backfill")

SCRATCH_DIR = Path("/tmp/acris_backfill")


# ---------------------------------------------------------------------------
# Lookup ingest path — tiny tables, single SODA page
# ---------------------------------------------------------------------------

def ingest_lookup(cfg: DatasetConfig, run_id: uuid.UUID) -> None:
    print(f"[{cfg.key}] lookup ingest, dataset={cfg.socrata_4x4}")
    with ingest_run(
        run_id=run_id,
        dataset_id=cfg.socrata_4x4,
        ingest_mode="bulk",
        source_url=f"https://data.cityofnewyork.us/resource/{cfg.socrata_4x4}.json",
    ) as (audit_conn, handle):
        loader_conn = connect(direct=False)
        try:
            if cfg.truncate_before_load:
                with loader_conn.cursor() as cur:
                    cur.execute(f"TRUNCATE TABLE {cfg.target_schema}.{cfg.target_table}")
                loader_conn.commit()
                print(f"[{cfg.key}] TRUNCATEd before reload")
            cols = get_table_columns(loader_conn, cfg)
            total_loaded = 0
            total_skipped = 0
            for page in paginate_socrata_json(cfg.socrata_4x4):
                rows = [build_persist_row(r, cfg, table_columns=cols) for r in page]
                loaded, skipped = chunked_upsert(loader_conn, cfg, rows)
                total_loaded += loaded
                total_skipped += skipped
            mark_run_completed(
                handle,
                rows_loaded=total_loaded,
                rows_skipped_idempotent=total_skipped,
            )
            print(f"[{cfg.key}] loaded={total_loaded:,} skipped={total_skipped:,}")
        finally:
            loader_conn.close()


# ---------------------------------------------------------------------------
# Fact-table ingest path — bulk CSV → chunked persist
# ---------------------------------------------------------------------------

def _csv_path_for(cfg: DatasetConfig) -> Path:
    return SCRATCH_DIR / f"{cfg.socrata_4x4}.csv"


def _stream_csv(cfg: DatasetConfig, csv_path: Path) -> tuple[int, str]:
    """Download to disk if not already present. Returns (bytes, sha256)."""
    if csv_path.exists() and csv_path.stat().st_size > 0:
        print(f"[{cfg.key}] reusing existing CSV: {csv_path} ({csv_path.stat().st_size:,} bytes)")
        # Compute SHA-256 from disk.
        h = hashlib.sha256()
        with open(csv_path, "rb") as fh:
            for buf in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(buf)
        return (csv_path.stat().st_size, h.hexdigest())

    print(f"[{cfg.key}] downloading CSV → {csv_path}")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    nbytes = stream_socrata_csv(cfg.socrata_4x4, str(csv_path))
    dt = time.monotonic() - t0
    print(f"[{cfg.key}] downloaded {nbytes:,} bytes in {dt:.1f}s ({nbytes / max(dt, 1) / 1e6:.1f} MB/s)")
    h = hashlib.sha256()
    with open(csv_path, "rb") as fh:
        for buf in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(buf)
    return (nbytes, h.hexdigest())


def _iter_csv_rows(csv_path: Path) -> Iterator[dict[str, Any]]:
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            # Socrata CSV uses uppercase display headers (e.g. "DOCUMENT ID").
            # Normalize to lower_snake fieldNames the way SODA JSON returns them.
            yield {_normalize_csv_header(k): v for k, v in row.items()}


_HEADER_OVERRIDES = {
    # Socrata's CSV headers are usually the display name (e.g. "DOCUMENT ID").
    # Map them to the JSON `fieldName` form. Only entries that don't fall out
    # of the lower-snake rule below need to live here.
    "doc. type": "doc_type",
    "doc. amount": "document_amt",
    "doc. date": "document_date",
    "doc. type description": "doc__type_description",
    "recorded / filed": "recorded_datetime",
    "% transferred": "percent_trans",
    "remark line nbr": "sequence_number",
    "remark text line": "remark_text",
    "reference by crfn": "reference_by_crfn_",
    "reference by doc id": "reference_by_doc_id",
    "reference by reel year": "reference_by_reel_year",
    "reference by reel borough": "reference_by_reel_borough",
    "reference by reel nbr": "reference_by_reel_nbr",
    "reference by reel page": "reference_by_reel_page",
    "ucc collateral code": "ucc_collateral_code",
    "ucc collateral codes": "ucc_collateral_code",
    "country code": "country_code",
    "state code": "state_code",
    "class code description": "class_code_description",
    "party1 type": "party1_type",
    "party2 type": "party2_type",
    "party3 type": "party3_type",
    "doc. type ": "doc__type",
    "address 1": "address_1",
    "address 2": "address_2",
    "street number": "street_number",
    "street name": "street_name",
    "good through date": "good_through_date",
    "modified date": "modified_date",
    "reel year": "reel_yr",
    "reel nbr": "reel_nbr",
    "reel page": "reel_pg",
    "borough": "borough",
    "block": "block",
    "lot": "lot",
    "easement": "easement",
    "partial lot": "partial_lot",
    "air rights": "air_rights",
    "subterranean rights": "subterranean_rights",
    "property type": "property_type",
    "unit": "unit",
    "addr unit": "addr_unit",
    "name": "name",
    "city": "city",
    "state": "state",
    "country": "country",
    "zip": "zip",
    "party type": "party_type",
    "doc id ref": "doc_id_ref",
    "file number": "file_nbr",
    "file nbr": "file_nbr",
    "rptt #": "rpttl_nbr",
    "rptt nbr": "rpttl_nbr",
    "collateral": "ucc_collateral",
    "slid#": "fedtax_serial_nbr",
    "assessment date": "fedtax_assessment_date",
    "crfn": "crfn",
    "document id": "document_id",
    "document_id": "document_id",
    "record type": "record_type",
    "record_type": "record_type",
    "description": "description",
    "doc type": "doc__type",
}


def _normalize_csv_header(header: str) -> str:
    if header is None:
        return ""
    h = header.strip().lower()
    if h in _HEADER_OVERRIDES:
        return _HEADER_OVERRIDES[h]
    # Default: lowercase, replace spaces and dots with underscores.
    return (
        h.replace(".", "")
         .replace("/", "_")
         .replace("#", "nbr")
         .replace("%", "pct")
         .replace(" ", "_")
         .strip("_")
    )


def ingest_fact_via_csv(cfg: DatasetConfig, run_id: uuid.UUID, csv_path: Path | None) -> None:
    print(f"[{cfg.key}] fact-table ingest, dataset={cfg.socrata_4x4}, table={cfg.target_schema}.{cfg.target_table}")
    if csv_path is None:
        csv_path = _csv_path_for(cfg)

    chunk_size = int(os.environ.get("DEX_ACRIS_CHUNK", DEFAULT_CHUNK_SIZE))

    with ingest_run(
        run_id=run_id,
        dataset_id=cfg.socrata_4x4,
        ingest_mode="bulk",
        source_url=f"https://data.cityofnewyork.us/resource/{cfg.socrata_4x4}.csv",
        source_filename=str(csv_path),
    ) as (audit_conn, handle):
        nbytes, sha = _stream_csv(cfg, csv_path)
        update_run_progress(handle, bytes_downloaded=nbytes, source_sha256=sha)

        loader_conn = connect(direct=False)
        try:
            cols = get_table_columns(loader_conn, cfg)
            batch: list[dict[str, Any]] = []
            total_loaded = 0
            total_skipped = 0
            n_seen = 0
            t0 = time.monotonic()

            for raw in _iter_csv_rows(csv_path):
                batch.append(build_persist_row(raw, cfg, table_columns=cols))
                n_seen += 1
                if len(batch) >= chunk_size:
                    loaded, skipped = chunked_upsert(loader_conn, cfg, batch)
                    total_loaded += loaded
                    total_skipped += skipped
                    if (n_seen // chunk_size) % 10 == 0:
                        elapsed = time.monotonic() - t0
                        rate = n_seen / max(elapsed, 1)
                        print(
                            f"[{cfg.key}] seen={n_seen:,} loaded={total_loaded:,} "
                            f"skipped={total_skipped:,} rate={rate:,.0f} rps "
                            f"elapsed={elapsed:.0f}s"
                        )
                        update_run_progress(
                            handle,
                            rows_loaded=total_loaded,
                            rows_skipped_idempotent=total_skipped,
                        )
                    batch = []

            if batch:
                loaded, skipped = chunked_upsert(loader_conn, cfg, batch)
                total_loaded += loaded
                total_skipped += skipped

            mark_run_completed(
                handle,
                rows_loaded=total_loaded,
                rows_skipped_idempotent=total_skipped,
                bytes_downloaded=nbytes,
                source_sha256=sha,
            )
            elapsed = time.monotonic() - t0
            print(
                f"[{cfg.key}] DONE seen={n_seen:,} loaded={total_loaded:,} "
                f"skipped={total_skipped:,} elapsed={elapsed:.0f}s"
            )
        finally:
            loader_conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

ALL_KEYS = list(DATASETS.keys()) + ["lookup-codes", "all"]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dataset", choices=ALL_KEYS)
    p.add_argument(
        "--csv-path",
        type=Path,
        default=None,
        help="Use a pre-downloaded CSV (skips streaming download). Single dataset only.",
    )
    args = p.parse_args()

    run_id = uuid.uuid4()
    print(f"=== ACRIS backfill run {run_id} dataset={args.dataset} ===")

    if args.dataset == "lookup-codes":
        for k in LOOKUP_KEYS:
            ingest_lookup(DATASETS[k], run_id)
        return 0

    if args.dataset == "all":
        for k in ALL_DATASET_KEYS:
            cfg = DATASETS[k]
            if cfg.target_schema == "lookup":
                ingest_lookup(cfg, run_id)
            else:
                ingest_fact_via_csv(cfg, run_id, csv_path=None)
        return 0

    cfg = DATASETS[args.dataset]
    if cfg.target_schema == "lookup":
        if args.csv_path is not None:
            print("--csv-path is ignored for lookup datasets (paginated JSON).")
        ingest_lookup(cfg, run_id)
    else:
        ingest_fact_via_csv(cfg, run_id, csv_path=args.csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
