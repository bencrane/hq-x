"""CSCR (California State Contracts Register) events → R2 ZSTD Parquet ingest (one-shot).

Reads a locally-downloaded HTML export from the public Cal eProcure Event Search
page at https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx (the
"Download" button on that page emits an `.xls`-extension file whose body is
actually HTML — `file ps.xls` reports `HTML document text`). The script parses
the embedded `<table>`, writes a ZSTD Parquet snapshot to Cloudflare R2.

R2 layout (kebab-case per CLAUDE.md §"R2 prefix convention"):
  s3://dex-raw-landing-zone/castate/cscr-events/snapshot={YYYY-MM-DD}/data.parquet

All columns written as VARCHAR per CLAUDE.md §"Source ingest invariant"
sub-section "bulk-historical Volume-King sources → R2 ZSTD Parquet → Lance emit" (L9).
Re-running with the same --snapshot-date overwrites the same R2 key.

This is a one-shot manual ingest — no Modal, no audit ledger, no catalog row. To
refresh, re-download from the Event Search page and re-run the script.

Source columns (10, in upstream order):
  Department, Department Name, Event ID, Event Name, Format, Type,
  End Date, Status, Buyer Name, Buyer Email.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_ca_cscr_events_to_r2.py \\
        --input-path ~/Downloads/ps.xls \\
        [--snapshot-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import tempfile
from pathlib import Path

import boto3
import lxml.html
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── load-bearing constants ──────────────────────────────────────────────────

SOURCE_URL = "https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "castate/cscr-events"

_COLUMNS = [
    "department",
    "department_name",
    "event_id",
    "event_name",
    "format",
    "type",
    "end_date",
    "status",
    "buyer_name",
    "buyer_email",
]


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def _parse_html_table(html_text: str) -> list[list[str]]:
    """Walk the upstream's single `<table>` and yield row-cell-text lists.

    The upstream HTML is malformed (each `<tr>` is opened without a closing
    `</tr>`), so lxml's recovery mode is required. The first row is the header
    (`<th>` cells); subsequent rows are data (`<td>` cells, 10 each).
    """
    root = lxml.html.fromstring(html_text)
    tables = root.xpath("//table")
    if not tables:
        raise RuntimeError("CSCR: no <table> element in input HTML")
    table = tables[0]
    rows: list[list[str]] = []
    for tr in table.xpath(".//tr"):
        cells = tr.xpath("./td")
        if not cells:
            continue
        rows.append([(c.text_content() or "").strip() for c in cells])
    return rows


def ingest(input_path: Path, snapshot_date: datetime.date) -> int:
    """Parse a local CSCR HTML export and upload a ZSTD Parquet snapshot to R2.

    Returns the number of data rows written.
    """
    logger.info("reading CSCR export from %s", input_path)
    html_text = input_path.read_text(encoding="utf-8", errors="replace")
    logger.info("input bytes: %d", len(html_text))

    rows = _parse_html_table(html_text)
    if not rows:
        raise RuntimeError("CSCR: zero <td> rows parsed — input may be empty or malformed")

    width = len(rows[0])
    if width != len(_COLUMNS):
        raise RuntimeError(
            f"CSCR: expected {len(_COLUMNS)} columns per row, got {width}; "
            f"first row: {rows[0]!r}"
        )
    logger.info("parsed rows: %d × %d columns", len(rows), width)

    # Pivot list-of-rows → dict-of-columns; coerce empty strings to None.
    columns: dict[str, list[str | None]] = {name: [] for name in _COLUMNS}
    for row in rows:
        for i, name in enumerate(_COLUMNS):
            v = row[i] if i < len(row) else ""
            columns[name].append(v if v else None)

    table = pa.table({name: pa.array(values, type=pa.string()) for name, values in columns.items()})

    local_parquet: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            local_parquet = tmp.name
        pq.write_table(
            table, local_parquet,
            compression="ZSTD",
            compression_level=9,
        )
        size = Path(local_parquet).stat().st_size
        logger.info("wrote local parquet: %d bytes", size)

        r2_key = f"{R2_PREFIX}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded → s3://%s/%s", R2_BUCKET, r2_key)
    finally:
        if local_parquet and Path(local_parquet).exists():
            Path(local_parquet).unlink(missing_ok=True)

    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CSCR (Cal eProcure Event Search) events → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--input-path",
        required=True,
        type=Path,
        help="Local path to the .xls (actually HTML) export downloaded from Cal eProcure",
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date YYYY-MM-DD (default: today UTC)",
    )
    args = parser.parse_args()

    if args.snapshot_date:
        snapshot_date = datetime.date.fromisoformat(args.snapshot_date)
    else:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()

    if not args.input_path.exists():
        logger.error("input file not found: %s", args.input_path)
        return 2

    logger.info("snapshot_date=%s source_url=%s", snapshot_date, SOURCE_URL)
    n = ingest(args.input_path, snapshot_date)
    logger.info("CSCR ingest OK: %d rows landed", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
