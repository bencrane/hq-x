#!/usr/bin/env python3
"""BLS Occupational Employment and Wage Statistics (OEWS) — XLSX ingest.

Source:
    https://www.bls.gov/oes/special-requests/oesm{YY}all.zip
    ZIP contains a single workbook: oesm{YY}all/all_data_M_{YYYY}.xlsx
    Active sheet 'All May {YYYY} data' carries ~400K rows × 32 columns.

    Numeric-bearing columns may carry BLS suppression sentinels:
      '*'  estimate not released
      '**' wage withheld
      '#'  wage ≥ $115/hr or ≥ $239,200/yr
    These are preserved verbatim — every measurement column lands as TEXT
    in entities.source_bls_oews.

Idempotency:
    Stream rows in batches; COPY into a temp staging table; UPSERT into
    entities.source_bls_oews via INSERT … ON CONFLICT (pk_cols) DO UPDATE
    … WHERE row IS DISTINCT FROM EXCLUDED.

    PK: (release_year, area, naics, occ_code, i_group, o_group, own_code)

Audit:
    One row per invocation in ops.bls_oews_ingest_runs.

release_year:
    Inferred from filename — 'oesm24all.zip' → 2024, 'oesm25all.zip' → 2025.
    Override via --release-year if needed.

Usage:
    DEX_DB_URL_POOLED=<url> python3 scripts/run_bls_oews_ingest.py \\
        --zip-path ~/Downloads/oesm24all.zip
    DEX_DB_URL_POOLED=<url> python3 scripts/run_bls_oews_ingest.py \\
        --zip-path ~/Downloads/oesm24all.zip --dry-run
    DEX_DB_URL_POOLED=<url> python3 scripts/run_bls_oews_ingest.py \\
        --zip-path ~/Downloads/oesm24all.zip --max-rows 1000
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import openpyxl
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "bls_oews"
BATCH_SIZE = 5_000

# 1:1 column mirror of XLSX headers (UPPER_SNAKE → snake_case).
# Order matches the XLSX columns 0..31.
XLSX_HEADERS: tuple[str, ...] = (
    "AREA", "AREA_TITLE", "AREA_TYPE", "PRIM_STATE",
    "NAICS", "NAICS_TITLE", "I_GROUP", "OWN_CODE",
    "OCC_CODE", "OCC_TITLE", "O_GROUP",
    "TOT_EMP", "EMP_PRSE", "JOBS_1000", "LOC_QUOTIENT",
    "PCT_TOTAL", "PCT_RPT",
    "H_MEAN", "A_MEAN", "MEAN_PRSE",
    "H_PCT10", "H_PCT25", "H_MEDIAN", "H_PCT75", "H_PCT90",
    "A_PCT10", "A_PCT25", "A_MEDIAN", "A_PCT75", "A_PCT90",
    "ANNUAL", "HOURLY",
)

TYPED_COLS: tuple[str, ...] = ("release_year",) + tuple(h.lower() for h in XLSX_HEADERS)

# Subset typed as smallint in the migration; everything else is text.
INT_COLS: frozenset[str] = frozenset({"release_year", "area_type", "own_code"})

PK_COLS: tuple[str, ...] = (
    "release_year", "area", "naics", "occ_code",
    "i_group", "o_group", "own_code",
)

PROVENANCE_COLS: tuple[str, ...] = (
    "raw_source_row",
    "source_provider",
    "source_filename",
    "source_download_url",
    "source_observed_at",
    "source_run_metadata",
    "source_task_id",
    "source_schedule_id",
    "ingested_at",
)

TARGET_TABLE = "entities.source_bls_oews"
RUNS_TABLE = "ops.bls_oews_ingest_runs"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("bls-oews-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# DB URL
# --------------------------------------------------------------------------- #

def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        raise RuntimeError(
            "neither DEX_DB_URL_POOLED nor DEX_DB_URL_DIRECT is set — "
            "are you running under `doppler run`?"
        )
    return url


# --------------------------------------------------------------------------- #
# release_year inference
# --------------------------------------------------------------------------- #

_FILENAME_YEAR_RE = re.compile(r"oesm(\d{2})all", re.IGNORECASE)


def _infer_release_year(zip_path: Path, override: int | None) -> int:
    if override is not None:
        return override
    m = _FILENAME_YEAR_RE.search(zip_path.name)
    if not m:
        raise RuntimeError(
            f"could not infer release_year from filename {zip_path.name!r}; "
            f"pass --release-year explicitly"
        )
    yy = int(m.group(1))
    # BLS files are M-YYYY; oesm24all → 2024. Two-digit window: 70..99 → 19YY,
    # 00..69 → 20YY (safe through 2069).
    return 1900 + yy if yy >= 70 else 2000 + yy


# --------------------------------------------------------------------------- #
# XLSX extraction
# --------------------------------------------------------------------------- #

def _extract_xlsx(zip_path: Path) -> Path:
    """Extract the single XLSX from the OEWS ZIP into a temp dir; return path."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="bls_oews_"))
    log.info("extracting %s → %s", zip_path, tmp_dir)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        xlsx_names = [n for n in names if n.lower().endswith(".xlsx")]
        if not xlsx_names:
            raise RuntimeError(f"no .xlsx in {zip_path}: contents={names}")
        if len(xlsx_names) > 1:
            log.warning("multiple xlsx in zip; using first: %s", xlsx_names)
        xlsx_name = xlsx_names[0]
        zf.extract(xlsx_name, tmp_dir)
        xlsx_path = tmp_dir / xlsx_name
    log.info("extracted %s (%d bytes)", xlsx_path, xlsx_path.stat().st_size)
    return xlsx_path


# --------------------------------------------------------------------------- #
# ops.bls_oews_ingest_runs helpers
# --------------------------------------------------------------------------- #

def insert_run(
    conn: psycopg.Connection,
    *,
    release_year: int,
    filename: str,
    download_url: str,
    observed_at: datetime | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {RUNS_TABLE}
              (status, release_year, source_filename, source_download_url, source_observed_at)
            VALUES ('running', %s, %s, %s, %s)
            RETURNING run_id;
            """,
            (release_year, filename, download_url, observed_at),
        )
        run_id = str(cur.fetchone()[0])
    conn.commit()
    log.info("audit run_id=%s", run_id)
    return run_id


def finalize_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_seen: int,
    rows_upserted: int,
    error_text: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {RUNS_TABLE} SET
              status        = %s,
              rows_seen     = %s,
              rows_upserted = %s,
              completed_at  = now(),
              error_text    = %s
            WHERE run_id = %s;
            """,
            (status, rows_seen, rows_upserted, error_text, run_id),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Row coercion
# --------------------------------------------------------------------------- #

def _cell_to_text(v: Any) -> str | None:
    """Coerce an openpyxl cell value to text, preserving sentinels."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _cell_to_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """Map XLSX cell tuple to {HEADER: cell-text} dict for raw_source_row."""
    out: dict[str, Any] = {}
    for header, cell in zip(XLSX_HEADERS, row):
        if cell is None:
            out[header] = None
        elif isinstance(cell, str):
            out[header] = cell.strip() if cell.strip() else None
        elif isinstance(cell, float) and cell.is_integer():
            out[header] = int(cell)
        else:
            out[header] = cell
    return out


# --------------------------------------------------------------------------- #
# Streaming row iterator
# --------------------------------------------------------------------------- #

def _iter_xlsx_rows(xlsx_path: Path, release_year: int) -> Iterator[tuple[dict, tuple]]:
    """Yield (raw_dict, typed_tuple) per data row.

    typed_tuple is ordered to match TYPED_COLS exactly.
    """
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(min_row=2, values_only=True)

    for row in rows:
        # Defensive: skip blank trailing rows (openpyxl can over-report max_row).
        if row is None or all(c is None or c == "" for c in row):
            continue

        raw = _row_to_dict(row)

        # Build typed_tuple in TYPED_COLS order: release_year first, then 32 source cols.
        typed: list[Any] = [release_year]
        for header, cell in zip(XLSX_HEADERS, row):
            col = header.lower()
            if col in INT_COLS:
                typed.append(_cell_to_int(cell))
            else:
                typed.append(_cell_to_text(cell))
        yield raw, tuple(typed)


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #

def process_xlsx(
    conn: psycopg.Connection,
    xlsx_path: Path,
    *,
    release_year: int,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime | None,
    run_id: str,
    max_rows: int | None = None,
) -> tuple[int, int]:
    """Stream the XLSX into entities.source_bls_oews. Returns (seen, upserted)."""
    stage_table = "_stage_bls_oews"
    log.info("processing %s → %s (pk=%s)", xlsx_path.name, TARGET_TABLE, PK_COLS)

    now_ts = datetime.now(timezone.utc)
    run_meta = {"run_id": run_id, "release_year": release_year}
    task_id = os.environ.get("MODAL_TASK_ID")
    schedule_id = os.environ.get("MODAL_SCHEDULE_ID")

    all_copy_cols: tuple[str, ...] = TYPED_COLS + PROVENANCE_COLS

    rows_seen = 0
    rows_upserted = 0
    batch: list[tuple] = []

    def _flush_batch(batch: list[tuple]) -> int:
        if not batch:
            return 0
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TEMP TABLE IF NOT EXISTS {stage_table} "
                f"AS SELECT * FROM {TARGET_TABLE} WHERE FALSE;"
            )
            cur.execute(f"TRUNCATE {stage_table};")

            copy_cols_str = ", ".join(all_copy_cols)
            with cur.copy(f"COPY {stage_table} ({copy_cols_str}) FROM STDIN") as copy:
                for row_tuple in batch:
                    copy.write_row(row_tuple)

            non_pk_typed = [c for c in TYPED_COLS if c not in PK_COLS]
            update_cols = non_pk_typed + [
                "raw_source_row", "source_provider", "source_filename",
                "source_download_url", "source_observed_at", "source_run_metadata",
                "source_task_id", "source_schedule_id",
            ]
            set_clause = ",\n                ".join(
                f"{c} = EXCLUDED.{c}" for c in update_cols
            )
            set_clause += ",\n                ingested_at = now()"

            conflict_target = ", ".join(PK_COLS)

            check_cols = non_pk_typed + [
                "raw_source_row", "source_filename", "source_download_url",
            ]
            distinct_clauses = " OR ".join(
                f"(t.{c} IS DISTINCT FROM EXCLUDED.{c})" for c in check_cols
            )
            where_clause = f"WHERE {distinct_clauses}" if check_cols else ""

            cur.execute(f"""
                WITH ins AS (
                  INSERT INTO {TARGET_TABLE} AS t ({copy_cols_str})
                  SELECT {copy_cols_str} FROM {stage_table}
                  ON CONFLICT ({conflict_target}) DO UPDATE SET
                    {set_clause}
                  {where_clause}
                  RETURNING (xmax = 0) AS inserted
                )
                SELECT COUNT(*) FILTER (WHERE inserted),
                       COUNT(*) FROM ins;
            """)
            inserted, total = cur.fetchone()
        conn.commit()
        return int(inserted)

    for raw, typed in _iter_xlsx_rows(xlsx_path, release_year):
        if max_rows is not None and rows_seen >= max_rows:
            break

        # Defensive: skip rows missing PK fields.
        pk_idx = [TYPED_COLS.index(pk) for pk in PK_COLS]
        if any(typed[i] is None or typed[i] == "" for i in pk_idx):
            log.debug("skipping row missing PK col(s): %r", typed[:11])
            continue

        row_tuple: tuple = typed + (
            Jsonb(raw),                 # raw_source_row
            PROVIDER,                   # source_provider
            source_filename,            # source_filename
            source_download_url,        # source_download_url
            source_observed_at,         # source_observed_at
            Jsonb(run_meta),            # source_run_metadata
            task_id,                    # source_task_id
            schedule_id,                # source_schedule_id
            now_ts,                     # ingested_at
        )

        batch.append(row_tuple)
        rows_seen += 1

        if len(batch) >= BATCH_SIZE:
            rows_upserted += _flush_batch(batch)
            batch = []
            log.info("  rows seen so far: %d", rows_seen)

    rows_upserted += _flush_batch(batch)
    log.info("done: rows_seen=%d rows_upserted=%d", rows_seen, rows_upserted)
    return rows_seen, rows_upserted


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--zip-path", required=True,
                   help="Path to oesm{YY}all.zip on disk")
    p.add_argument("--release-year", type=int, default=None,
                   help="Override release year (default: parse from filename)")
    p.add_argument("--download-url", default=None,
                   help="Original BLS download URL for provenance "
                        "(default: file://<absolute-path>)")
    p.add_argument("--dry-run", action="store_true",
                   help="Extract + parse but do not write to DB")
    p.add_argument("--max-rows", type=int, default=None, metavar="N",
                   help="Stop after N rows (smoke-test limit)")
    args = p.parse_args(argv)

    zip_path = Path(args.zip_path).expanduser().resolve()
    if not zip_path.is_file():
        raise FileNotFoundError(f"zip not found: {zip_path}")

    release_year = _infer_release_year(zip_path, args.release_year)
    log.info("zip=%s release_year=%d", zip_path, release_year)

    download_url = args.download_url or f"file://{zip_path}"
    observed_at = datetime.fromtimestamp(zip_path.stat().st_mtime, tz=timezone.utc)

    xlsx_path = _extract_xlsx(zip_path)

    try:
        if args.dry_run:
            log.info("DRY RUN — parsing first %s rows, no DB writes",
                     args.max_rows or "(all)")
            n = 0
            for raw, typed in _iter_xlsx_rows(xlsx_path, release_year):
                n += 1
                if n <= 3:
                    log.info("  raw[%d]: %s", n, dict(list(raw.items())[:6]))
                    log.info("  typed[%d]: %s", n, typed[:11])
                if args.max_rows and n >= args.max_rows:
                    break
            log.info("dry-run rows: %d", n)
            return {"dry_run": True, "rows_seen": n, "release_year": release_year}

        db_url = _database_url()
        with psycopg.connect(db_url, autocommit=False) as conn:
            run_id = insert_run(
                conn,
                release_year=release_year,
                filename=zip_path.name,
                download_url=download_url,
                observed_at=observed_at,
            )
            try:
                seen, upserted = process_xlsx(
                    conn, xlsx_path,
                    release_year=release_year,
                    source_filename=zip_path.name,
                    source_download_url=download_url,
                    source_observed_at=observed_at,
                    run_id=run_id,
                    max_rows=args.max_rows,
                )
                finalize_run(
                    conn, run_id,
                    status="succeeded",
                    rows_seen=seen,
                    rows_upserted=upserted,
                )
                return {
                    "run_id": run_id,
                    "release_year": release_year,
                    "rows_seen": seen,
                    "rows_upserted": upserted,
                }
            except Exception as exc:
                finalize_run(
                    conn, run_id,
                    status="failed",
                    rows_seen=0,
                    rows_upserted=0,
                    error_text=str(exc)[:1000],
                )
                raise
    finally:
        # Clean up extracted XLSX.
        try:
            xlsx_path.unlink(missing_ok=True)
            xlsx_path.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        result = main()
        log.info("result: %s", result)
    except Exception:
        log.exception("ingest failed")
        sys.exit(1)
