#!/usr/bin/env python3
"""Ingest SEC Form ADV Part 1 from a sec.gov bulk compilation ZIP.

Lands rows into:
    entities.sec_form_adv_part1_firms                          (from IA_ADV_Base_A)
    entities.sec_form_adv_part1_schedule_d_1f_branch_offices   (from IA_Schedule_D_1F)
    entities.sec_form_adv_part1_aux_rows                       (everything else)

The Part 1 ZIP expands to ~24 CSVs all keyed on FilingID. We treat
IA_ADV_Base_A as canonical firm rows and stash every other CSV row in
the aux relay table, with the source row in raw_jsonb for forensics +
forward compatibility with a downstream typed-projection MV.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_sec_form_adv_part1_ingest.py \\
        --zip-path /path/to/adv-filing-data.zip \\
        --compilation-date 2024-12-31

    # Or fetch from sec.gov:
    PYTHONPATH=. doppler run -- python3 scripts/run_sec_form_adv_part1_ingest.py \\
        --source-url https://www.sec.gov/files/adv-filing-data-20111105-20241231-part1.zip \\
        --compilation-date 2024-12-31
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

# Add repo root to path so direct invocation works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg.types.json import Json  # noqa: E402

from scripts.sec_form_adv_common import (  # noqa: E402
    CRD_KEYS,
    LEGAL_NAME_KEYS,
    PRIMARY_BUSINESS_NAME_KEYS,
    SEC_NUMBER_KEYS,
    chunked,
    classify_error,
    db_connection,
    finish_run,
    first_present,
    parse_int,
    parse_iso_date,
    parse_yn_flag,
    start_run,
    stream_download,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("sec_form_adv_part1")

CHUNK_SIZE = 5_000

# CSV name → loader hint. Anything not in this map goes to the aux relay
# table tagged with the CSV's filename root.
BASE_A_PREFIX = "IA_ADV_Base_A_"
SCHEDULE_D_1F_PREFIX = "IA_Schedule_D_1F_"


def _csv_root(name: str) -> str:
    """Return the CSV's filename root without the date suffix.

    e.g. 'IA_DRP_Regulatory_20001019_20111104.csv' -> 'IA_DRP_Regulatory'
    """
    base = Path(name).name
    if base.endswith(".csv"):
        base = base[:-4]
    parts = base.split("_")
    out = []
    for p in parts:
        if p.isdigit() and len(p) >= 8:
            break
        out.append(p)
    return "_".join(out) if out else base


def _coerce_filing_id(row: dict) -> int | None:
    for k in ("FilingID", "Filing ID", "Filing Id"):
        v = row.get(k)
        if v is not None and str(v).strip() not in ("", '""'):
            n = parse_int(v)
            if n is not None:
                return n
    return None


def load_base_a(
    cur,
    csv_iter,
    csv_name: str,
    compilation_date: str,
    compilation_filename: str,
) -> tuple[int, int]:
    """Insert canonical firm rows from IA_ADV_Base_A_*.csv. Returns (loaded, skipped)."""
    rows_loaded = 0
    rows_skipped = 0

    def gen():
        for raw in csv_iter:
            filing_id = _coerce_filing_id(raw)
            if filing_id is None:
                continue
            crd = parse_int(first_present(raw, CRD_KEYS))
            sec_number = first_present(raw, SEC_NUMBER_KEYS)
            legal_name = first_present(raw, LEGAL_NAME_KEYS)
            primary_business_name = first_present(raw, PRIMARY_BUSINESS_NAME_KEYS)
            form_version = (raw.get("FormVersion") or raw.get("Form Version") or "").strip().strip('"') or None
            date_submitted = parse_iso_date(
                raw.get("DateSubmitted")
                or raw.get("Date Submitted")
                or raw.get("Date_Submitted")
            )
            yield (
                filing_id,
                crd,
                sec_number,
                form_version,
                date_submitted,
                legal_name,
                primary_business_name,
                Json(raw),
                csv_name,
                compilation_date,
                compilation_filename,
            )

    for batch in chunked(gen(), CHUNK_SIZE):
        cur.executemany(
            """
            INSERT INTO entities.sec_form_adv_part1_firms
              (filing_id, crd_number, sec_number, form_version, date_submitted,
               legal_name, primary_business_name, raw_jsonb,
               source_csv_filename, compilation_date, compilation_filename)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (filing_id, compilation_date) DO UPDATE SET
              crd_number        = EXCLUDED.crd_number,
              sec_number        = EXCLUDED.sec_number,
              form_version      = EXCLUDED.form_version,
              date_submitted    = EXCLUDED.date_submitted,
              legal_name        = EXCLUDED.legal_name,
              primary_business_name = EXCLUDED.primary_business_name,
              raw_jsonb         = EXCLUDED.raw_jsonb,
              source_csv_filename = EXCLUDED.source_csv_filename,
              compilation_filename = EXCLUDED.compilation_filename,
              updated_at        = NOW()
            """,
            batch,
        )
        rows_loaded += cur.rowcount
        rows_skipped += len(batch) - cur.rowcount
        logger.info(
            "base_a_chunk_persisted",
            extra={"chunk_size": len(batch), "rows_loaded": rows_loaded},
        )
    return rows_loaded, rows_skipped


def load_schedule_d_1f(
    cur,
    csv_iter,
    csv_name: str,
    compilation_date: str,
    filing_id_to_crd: dict[int, int | None],
) -> tuple[int, int]:
    """Insert branch-grain rows from IA_Schedule_D_1F_*.csv. Returns (loaded, skipped)."""
    rows_loaded = 0
    rows_skipped = 0

    def gen():
        for raw in csv_iter:
            filing_id = _coerce_filing_id(raw)
            if filing_id is None:
                continue
            yield (
                filing_id,
                filing_id_to_crd.get(filing_id),
                (raw.get("Branch Number") or raw.get("BranchNumber") or "").strip().strip('"') or None,
                (raw.get("Street 1") or "").strip().strip('"') or None,
                (raw.get("Street 2") or "").strip().strip('"') or None,
                (raw.get("City") or "").strip().strip('"') or None,
                (raw.get("State") or "").strip().strip('"') or None,
                (raw.get("Country") or "").strip().strip('"') or None,
                (raw.get("Postal Code") or raw.get("PostalCode") or "").strip().strip('"') or None,
                (raw.get("Private Residence") or "").strip().strip('"') or None,
                (raw.get("Telephone Number") or raw.get("Phone") or "").strip().strip('"') or None,
                (raw.get("Facsimile Number") or raw.get("Fax") or "").strip().strip('"') or None,
                parse_int(raw.get("Employees") or raw.get("Employee Count")),
                parse_yn_flag(raw.get("BD")),
                parse_yn_flag(raw.get("Bank")),
                parse_yn_flag(raw.get("Insurance")),
                parse_yn_flag(raw.get("Commodity")),
                parse_yn_flag(raw.get("Municipal")),
                parse_yn_flag(raw.get("Accounting")),
                parse_yn_flag(raw.get("Law")),
                parse_yn_flag(raw.get("Other")),
                Json(raw),
                csv_name,
                compilation_date,
            )

    for batch in chunked(gen(), CHUNK_SIZE):
        cur.executemany(
            """
            INSERT INTO entities.sec_form_adv_part1_schedule_d_1f_branch_offices
              (filing_id, crd_number, branch_number,
               street_1, street_2, city, state, country, postal_code,
               private_residence, phone, fax, employee_count,
               bd_flag, bank_flag, insurance_flag, commodity_flag,
               municipal_flag, accounting_flag, law_flag, other_flag,
               raw_jsonb, source_csv_filename, compilation_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (filing_id, COALESCE(branch_number, ''), compilation_date)
              DO NOTHING
            """,
            batch,
        )
        rows_loaded += cur.rowcount
        rows_skipped += len(batch) - cur.rowcount
        logger.info(
            "branches_chunk_persisted",
            extra={"chunk_size": len(batch), "rows_loaded": rows_loaded},
        )
    return rows_loaded, rows_skipped


def load_aux(
    cur,
    csv_iter,
    csv_name: str,
    csv_root: str,
    compilation_date: str,
    filing_id_to_crd: dict[int, int | None],
) -> tuple[int, int]:
    """Land any non-Base-A, non-1F CSV row in entities.sec_form_adv_part1_aux_rows."""
    rows_loaded = 0
    rows_skipped = 0

    def gen():
        for idx, raw in enumerate(csv_iter):
            filing_id = _coerce_filing_id(raw)
            crd = filing_id_to_crd.get(filing_id) if filing_id is not None else None
            yield (
                filing_id,
                crd,
                csv_root,
                idx,
                Json(raw),
                csv_name,
                compilation_date,
            )

    for batch in chunked(gen(), CHUNK_SIZE):
        cur.executemany(
            """
            INSERT INTO entities.sec_form_adv_part1_aux_rows
              (filing_id, crd_number, csv_name, row_index, raw_jsonb,
               source_csv_filename, compilation_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (csv_name, source_csv_filename, row_index, compilation_date)
              DO NOTHING
            """,
            batch,
        )
        rows_loaded += cur.rowcount
        rows_skipped += len(batch) - cur.rowcount
    logger.info(
        "aux_csv_persisted",
        extra={
            "csv_root": csv_root,
            "csv_name": csv_name,
            "rows_loaded": rows_loaded,
            "rows_skipped": rows_skipped,
        },
    )
    return rows_loaded, rows_skipped


def fetch_filing_id_to_crd(cur, compilation_date: str) -> dict[int, int | None]:
    """Build the filing_id -> crd_number lookup from the firms table."""
    cur.execute(
        """
        SELECT filing_id, crd_number
        FROM entities.sec_form_adv_part1_firms
        WHERE compilation_date = %s
        """,
        (compilation_date,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def open_csv_in_zip(zf: zipfile.ZipFile, name: str) -> csv.DictReader:
    raw = zf.open(name, "r")
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    return csv.DictReader(text)


_BACKFILL_BATCH = 25_000


def backfill_crd_nulls(cur) -> dict[str, int]:
    """Backfill crd_number on rows where raw_jsonb->>'1E1' is a clean integer
    but crd_number is NULL. Catches historical rows that landed before the
    CRD_KEYS projection fix, plus any rows where the loader-time `parse_int`
    rejected a value the SQL `~ '^[0-9]+$'` filter accepts.

    Batched (filing_id windows of _BACKFILL_BATCH rows) to stay under the
    prod statement_timeout. Idempotent: a no-op when the loader's in-line
    projection already covered every row. Returns counts per table.
    """
    cur.execute(
        """
        SELECT min(filing_id), max(filing_id)
          FROM entities.sec_form_adv_part1_firms
         WHERE crd_number IS NULL
           AND raw_jsonb ? '1E1'
           AND raw_jsonb ->> '1E1' ~ '^[0-9]+$'
        """
    )
    bounds = cur.fetchone()
    firm_rows = 0
    if bounds and bounds[0] is not None:
        lo, hi = bounds
        cursor_lo = lo
        while cursor_lo <= hi:
            cur.execute(
                """
                UPDATE entities.sec_form_adv_part1_firms
                   SET crd_number = (raw_jsonb ->> '1E1')::int,
                       updated_at = NOW()
                 WHERE filing_id >= %s
                   AND filing_id < %s
                   AND crd_number IS NULL
                   AND raw_jsonb ? '1E1'
                   AND raw_jsonb ->> '1E1' ~ '^[0-9]+$'
                """,
                (cursor_lo, cursor_lo + _BACKFILL_BATCH),
            )
            firm_rows += cur.rowcount
            cursor_lo += _BACKFILL_BATCH

    cur.execute(
        """
        UPDATE entities.sec_form_adv_part1_schedule_d_1f_branch_offices b
           SET crd_number = f.crd_number
          FROM entities.sec_form_adv_part1_firms f
         WHERE b.filing_id = f.filing_id
           AND b.crd_number IS NULL
           AND f.crd_number IS NOT NULL
        """
    )
    branch_rows = cur.rowcount

    cur.execute(
        """
        UPDATE entities.sec_form_adv_part1_aux_rows a
           SET crd_number = f.crd_number
          FROM entities.sec_form_adv_part1_firms f
         WHERE a.filing_id = f.filing_id
           AND a.crd_number IS NULL
           AND f.crd_number IS NOT NULL
        """
    )
    aux_rows = cur.rowcount

    return {"firms": firm_rows, "branches": branch_rows, "aux": aux_rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip-path", type=Path, help="Local path to a downloaded Part 1 ZIP.")
    parser.add_argument(
        "--source-url",
        type=str,
        help="sec.gov URL to fetch (used when --zip-path not given).",
    )
    parser.add_argument(
        "--compilation-date",
        type=str,
        required=True,
        help="ISO date (YYYY-MM-DD) tagging this compilation snapshot.",
    )
    parser.add_argument("--run-id", type=str, default=None, help="Override run UUID.")
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help=(
            "Skip the SEC ZIP fetch + ingest and just backfill crd_number "
            "for any existing row where raw_jsonb->>'1E1' is numeric but "
            "crd_number is NULL. Use after fixing CRD projection logic to "
            "clean up rows that already landed."
        ),
    )
    args = parser.parse_args()

    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
    handle = None

    if args.backfill_only:
        with db_connection() as conn:
            with conn.cursor() as cur:
                counts = backfill_crd_nulls(cur)
                conn.commit()
        logger.info("backfill_only_complete", extra=counts)
        return 0

    if args.zip_path is None and not args.source_url:
        parser.error("must supply either --zip-path or --source-url")

    tmp_dir = None
    bytes_downloaded = 0
    source_sha256: str | None = None
    source_byte_size: int | None = None

    try:
        if args.zip_path is None:
            tmp_dir = tempfile.TemporaryDirectory()
            tmp_path = Path(tmp_dir.name) / "adv-part1.zip"
            handle = start_run(
                run_id=run_id,
                feed_name="part1",
                source_url=args.source_url,
                source_filename=Path(args.source_url).name,
                compilation_date=args.compilation_date,
            )
            logger.info("downloading", extra={"url": args.source_url})
            bytes_downloaded, source_sha256 = stream_download(args.source_url, tmp_path)
            source_byte_size = bytes_downloaded
            zip_path = tmp_path
            source_filename = Path(args.source_url).name
        else:
            zip_path = args.zip_path
            source_filename = zip_path.name
            handle = start_run(
                run_id=run_id,
                feed_name="part1",
                source_url=args.source_url or f"file://{zip_path}",
                source_filename=source_filename,
                compilation_date=args.compilation_date,
            )

        rows_loaded_total = 0
        rows_skipped_total = 0

        with zipfile.ZipFile(zip_path, "r") as zf, db_connection() as conn:
            with conn.cursor() as cur:
                csv_names = sorted(
                    n for n in zf.namelist() if n.lower().endswith(".csv")
                )
                logger.info(
                    "zip_opened",
                    extra={"zip_path": str(zip_path), "csv_count": len(csv_names)},
                )

                # Pass 1: Base_A first so the filing_id -> crd lookup is built
                # before we load the dependent tables.
                base_a_csvs = [n for n in csv_names if Path(n).name.startswith(BASE_A_PREFIX)]
                for name in base_a_csvs:
                    logger.info("loading_base_a", extra={"csv": name})
                    reader = open_csv_in_zip(zf, name)
                    loaded, skipped = load_base_a(
                        cur, reader, name, args.compilation_date, source_filename
                    )
                    rows_loaded_total += loaded
                    rows_skipped_total += skipped

                conn.commit()

                # Now build the lookup
                filing_id_to_crd = fetch_filing_id_to_crd(cur, args.compilation_date)
                logger.info(
                    "filing_lookup_built",
                    extra={"firm_rows": len(filing_id_to_crd)},
                )

                # Pass 2: Schedule D 1F branches
                d1f_csvs = [n for n in csv_names if Path(n).name.startswith(SCHEDULE_D_1F_PREFIX)]
                for name in d1f_csvs:
                    logger.info("loading_branches", extra={"csv": name})
                    reader = open_csv_in_zip(zf, name)
                    loaded, skipped = load_schedule_d_1f(
                        cur, reader, name, args.compilation_date, filing_id_to_crd
                    )
                    rows_loaded_total += loaded
                    rows_skipped_total += skipped

                conn.commit()

                # Pass 3: everything else into aux relay
                other_csvs = [
                    n for n in csv_names
                    if not Path(n).name.startswith(BASE_A_PREFIX)
                    and not Path(n).name.startswith(SCHEDULE_D_1F_PREFIX)
                ]
                for name in other_csvs:
                    csv_root = _csv_root(name)
                    logger.info(
                        "loading_aux",
                        extra={"csv": name, "csv_root": csv_root},
                    )
                    reader = open_csv_in_zip(zf, name)
                    loaded, skipped = load_aux(
                        cur,
                        reader,
                        name,
                        csv_root,
                        args.compilation_date,
                        filing_id_to_crd,
                    )
                    rows_loaded_total += loaded
                    rows_skipped_total += skipped

                conn.commit()

                # Final pass: backfill crd_number on any row where raw_jsonb
                # has a clean numeric '1E1' but crd_number is NULL. Idempotent;
                # catches historical rows that pre-date the CRD_KEYS fix.
                backfill_counts = backfill_crd_nulls(cur)
                conn.commit()
                logger.info("crd_backfill", extra=backfill_counts)

        finish_run(
            handle,
            status="completed",
            rows_loaded=rows_loaded_total,
            rows_skipped_idempotent=rows_skipped_total,
            bytes_downloaded=bytes_downloaded or None,
            source_sha256=source_sha256,
            source_byte_size=source_byte_size,
        )
        logger.info(
            "ingest_complete",
            extra={
                "rows_loaded": rows_loaded_total,
                "rows_skipped": rows_skipped_total,
            },
        )
        return 0
    except Exception as exc:
        logger.exception("ingest_failed")
        if handle is not None:
            try:
                finish_run(
                    handle,
                    status="failed",
                    error_message=str(exc)[:1000],
                    error_class=classify_error(exc),
                )
            except Exception:
                logger.exception("finish_run_after_failure_also_failed")
        return 1
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()


if __name__ == "__main__":
    sys.exit(main())
