#!/usr/bin/env python3
"""Ingest SEC Form ADV Part 2 brochures and/or Part 3 (Form CRS) PDFs.

Each compilation ZIP from sec.gov contains:
  * The PDFs themselves (Part 2: brochures, Part 3: Form CRS).
  * A mapping CSV (Part 2: 9 cols incl. CRDNumber, BrochureID, Version,
    DateFiled, PDFFileName; Part 3: 13 cols incl. FIRM_CRD_NB, CRS_ID,
    CRS_FILE, CRS_TYPE).

The PDF bytes are uploaded to Supabase Storage bucket `sec-form-adv-pdfs`
under content-addressable keys. Manifest rows record the metadata + the
storage key + SHA-256 + byte size. Idempotency: if a manifest row for
the same uniqueness key already exists with a matching pdf_sha256, the
upload is skipped.

This loader does NOT parse the PDFs — that's a downstream workstream.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_sec_form_adv_pdfs_ingest.py \\
        --part 2 \\
        --zip-path /path/to/adv-brochures-2024-december.zip \\
        --mapping-csv /path/to/adv-brochure-mapping-20241201-20241231.csv \\
        --compilation-date 2024-12-01

    PYTHONPATH=. doppler run -- python3 scripts/run_sec_form_adv_pdfs_ingest.py \\
        --part 3 \\
        --zip-path /path/to/firm_crs_docs_monthly_20241101_429.zip \\
        --mapping-csv /path/to/firm_crs_monthly_20241101_429.csv \\
        --compilation-date 2024-11-01
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402 (needed before psycopg for env lookup)

from psycopg.types.json import Json  # noqa: E402
from supabase import create_client  # noqa: E402

from scripts.sec_form_adv_common import (  # noqa: E402
    STORAGE_BUCKET,
    classify_error,
    db_connection,
    finish_run,
    parse_int,
    parse_iso_date,
    start_run,
    stream_download,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("sec_form_adv_pdfs")


def _get_raw_supabase_client():
    """Build Supabase client from Doppler-injected env vars (no app.config import)."""
    url = os.environ.get("DEX_SUPABASE_URL")
    key = os.environ.get("DEX_SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "DEX_SUPABASE_URL and DEX_SUPABASE_SERVICE_ROLE_KEY must be set "
            "— invoke under `doppler run -p hq-all -c prd -- ...`"
        )
    return create_client(url, key)


def ensure_bucket(supabase) -> None:
    """Create the storage bucket if it doesn't exist (idempotent)."""
    try:
        existing = supabase.storage.list_buckets()
        names = {b.name if hasattr(b, "name") else b.get("name") for b in existing}
        if STORAGE_BUCKET in names:
            return
        supabase.storage.create_bucket(
            STORAGE_BUCKET,
            options={"public": False},
        )
        logger.info("bucket_created", extra={"bucket": STORAGE_BUCKET})
    except Exception as exc:  # idempotency: bucket-exists errors are fine
        msg = repr(exc).lower()
        if "already exists" in msg or "duplicate" in msg or "exists" in msg:
            return
        raise


def read_zip_pdf_index(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Map basename(.pdf) -> ZipInfo for every PDF in the ZIP.

    Mapping CSVs key on PDFFileName / CRS_FILE which is the basename, not
    the full path. Some ZIPs nest PDFs in subdirs.
    """
    out: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        name = Path(info.filename).name
        if name.lower().endswith(".pdf"):
            out[name] = info
    return out


def upload_pdf(
    supabase,
    *,
    storage_key: str,
    pdf_bytes: bytes,
) -> None:
    """Upload PDF bytes to Supabase Storage, no-op if already there with same SHA."""
    try:
        supabase.storage.from_(STORAGE_BUCKET).upload(
            path=storage_key,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )
    except Exception as exc:
        # supabase-py raises on duplicate; we use upsert=true above so
        # this should not normally fire. Re-raise on real errors.
        raise RuntimeError(f"storage upload failed for {storage_key}: {exc}") from exc


def fetch_existing_part2(cur, compilation_date: str) -> set[tuple]:
    cur.execute(
        """
        SELECT crd_number, brochure_id, brochure_version, date_filed, pdf_sha256
        FROM entities.sec_form_adv_part2_filings
        WHERE compilation_date = %s
        """,
        (compilation_date,),
    )
    return {tuple(r) for r in cur.fetchall()}


def fetch_existing_part3(cur, compilation_date: str) -> set[tuple]:
    cur.execute(
        """
        SELECT crs_id, pdf_sha256
        FROM entities.sec_form_adv_part3_filings
        WHERE compilation_date = %s
        """,
        (compilation_date,),
    )
    return {tuple(r) for r in cur.fetchall()}


def load_part2(
    *,
    zf: zipfile.ZipFile,
    pdf_index: dict[str, zipfile.ZipInfo],
    mapping_rows: list[dict],
    csv_filename: str,
    zip_filename: str,
    compilation_date: str,
    supabase,
) -> tuple[int, int, int]:
    """Returns (rows_loaded, rows_skipped_idempotent, pdfs_uploaded)."""
    rows_loaded = 0
    rows_skipped = 0
    pdfs_uploaded = 0

    with db_connection() as conn:
        with conn.cursor() as cur:
            existing = fetch_existing_part2(cur, compilation_date)

            for raw in mapping_rows:
                crd = parse_int(raw.get("CRDNumber") or raw.get("CRD Number"))
                brochure_id = parse_int(raw.get("BrochureID") or raw.get("Brochure ID"))
                brochure_version = parse_int(
                    raw.get("BrochureVersion") or raw.get("Brochure Version")
                )
                date_filed_dt = parse_iso_date(raw.get("DateFiled") or raw.get("Date Filed"))
                pdf_filename = (raw.get("PDFFileName") or "").strip().strip('"') or None
                if crd is None or brochure_id is None or pdf_filename is None:
                    continue

                date_filed = date_filed_dt.date() if date_filed_dt else None
                info = pdf_index.get(pdf_filename)
                if info is None:
                    logger.warning(
                        "pdf_missing_in_zip",
                        extra={"pdf_filename": pdf_filename, "crd": crd},
                    )
                    continue

                pdf_bytes = zf.read(info)
                pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

                key_check = (crd, brochure_id, brochure_version, date_filed, pdf_sha256)
                if any(
                    e[:4] == (crd, brochure_id, brochure_version, date_filed)
                    and e[4] == pdf_sha256
                    for e in existing
                ):
                    rows_skipped += 1
                    continue

                storage_key = f"part2/{crd}/{brochure_id}/{brochure_version or 0}/{pdf_sha256}.pdf"
                upload_pdf(supabase, storage_key=storage_key, pdf_bytes=pdf_bytes)
                pdfs_uploaded += 1

                cur.execute(
                    """
                    INSERT INTO entities.sec_form_adv_part2_filings
                      (firm_name, sec_number, crd_number, filing_id, brochure_name,
                       brochure_id, brochure_version, date_filed, pdf_filename,
                       storage_bucket, storage_key, pdf_sha256, pdf_byte_size,
                       raw_jsonb, source_csv_filename, source_zip_filename,
                       compilation_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    ON CONFLICT (crd_number, brochure_id, brochure_version, date_filed)
                      DO NOTHING
                    """,
                    (
                        (raw.get("FirmName") or "").strip().strip('"') or None,
                        (raw.get("SECNumber") or "").strip().strip('"') or None,
                        crd,
                        parse_int(raw.get("FilingID") or raw.get("Filing ID")),
                        (raw.get("BrochureName") or "").strip().strip('"') or None,
                        brochure_id,
                        brochure_version,
                        date_filed,
                        pdf_filename,
                        STORAGE_BUCKET,
                        storage_key,
                        pdf_sha256,
                        len(pdf_bytes),
                        Json(raw),
                        csv_filename,
                        zip_filename,
                        compilation_date,
                    ),
                )
                if cur.rowcount > 0:
                    rows_loaded += 1
                else:
                    rows_skipped += 1

                if (rows_loaded + rows_skipped) % 250 == 0:
                    conn.commit()
                    logger.info(
                        "part2_progress",
                        extra={
                            "rows_loaded": rows_loaded,
                            "rows_skipped": rows_skipped,
                            "pdfs_uploaded": pdfs_uploaded,
                        },
                    )

    return rows_loaded, rows_skipped, pdfs_uploaded


def load_part3(
    *,
    zf: zipfile.ZipFile,
    pdf_index: dict[str, zipfile.ZipInfo],
    mapping_rows: list[dict],
    csv_filename: str,
    zip_filename: str,
    compilation_date: str,
    supabase,
) -> tuple[int, int, int]:
    rows_loaded = 0
    rows_skipped = 0
    pdfs_uploaded = 0

    with db_connection() as conn:
        with conn.cursor() as cur:
            existing = fetch_existing_part3(cur, compilation_date)
            existing_by_id = {e[0]: e[1] for e in existing}

            for raw in mapping_rows:
                crd = parse_int(raw.get("FIRM_CRD_NB") or raw.get("CRDNumber"))
                crs_id_str = (raw.get("CRS_ID") or "").strip().strip('"')
                if crd is None or not crs_id_str:
                    continue
                try:
                    crs_id = uuid.UUID(crs_id_str)
                except ValueError:
                    continue

                pdf_filename = (raw.get("CRS_FILE") or "").strip().strip('"') or None
                if pdf_filename is None:
                    continue
                info = pdf_index.get(pdf_filename)
                if info is None:
                    logger.warning(
                        "pdf_missing_in_zip",
                        extra={"pdf_filename": pdf_filename, "crd": crd, "crs_id": crs_id_str},
                    )
                    continue

                pdf_bytes = zf.read(info)
                pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

                if existing_by_id.get(crs_id) == pdf_sha256:
                    rows_skipped += 1
                    continue

                storage_key = f"part3/{crd}/{crs_id}.pdf"
                upload_pdf(supabase, storage_key=storage_key, pdf_bytes=pdf_bytes)
                pdfs_uploaded += 1

                submitted_dt = parse_iso_date(raw.get("SBMTD_DT"))
                rec_created = parse_iso_date(raw.get("REC_CRTN_TS"))
                rec_updated = parse_iso_date(raw.get("REC_UPDT_TS"))
                crs_start = parse_iso_date(raw.get("CRS_ST_DT"))

                cur.execute(
                    """
                    INSERT INTO entities.sec_form_adv_part3_filings
                      (filing_id, crd_number, crs_id, pdf_filename,
                       submitted_at, template_version, record_created_at,
                       record_updated_at, crs_start_date, crs_status,
                       crs_type, crs_dual_type, affiliate_info,
                       storage_bucket, storage_key, pdf_sha256, pdf_byte_size,
                       raw_jsonb, source_csv_filename, source_zip_filename,
                       compilation_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (crs_id) DO NOTHING
                    """,
                    (
                        (raw.get("FLNG_ID") or "").strip().strip('"') or "",  # filing_id NOT NULL
                        crd,
                        crs_id,
                        pdf_filename,
                        submitted_dt,
                        (raw.get("TEMPL_VRSN_NM") or "").strip().strip('"') or None,
                        rec_created,
                        rec_updated,
                        crs_start.date() if crs_start else None,
                        (raw.get("CRS_ST_NM") or "").strip().strip('"') or None,
                        (raw.get("CRS_TYPE") or "").strip().strip('"') or None,
                        (raw.get("CRS_DUAL_TYPE") or "").strip().strip('"') or None,
                        (raw.get("AFFIL_INFO") or "").strip().strip('"') or None,
                        STORAGE_BUCKET,
                        storage_key,
                        pdf_sha256,
                        len(pdf_bytes),
                        Json(raw),
                        csv_filename,
                        zip_filename,
                        compilation_date,
                    ),
                )
                if cur.rowcount > 0:
                    rows_loaded += 1
                else:
                    rows_skipped += 1

                if (rows_loaded + rows_skipped) % 100 == 0:
                    conn.commit()

    return rows_loaded, rows_skipped, pdfs_uploaded


def read_mapping_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=["2", "3"], required=True)
    parser.add_argument("--zip-path", type=Path, help="Local path to PDF compilation ZIP.")
    parser.add_argument("--zip-url", type=str, help="sec.gov URL for the PDF compilation ZIP.")
    parser.add_argument("--mapping-csv", type=Path, help="Local path to the mapping CSV.")
    parser.add_argument("--mapping-url", type=str, help="sec.gov URL for the mapping CSV.")
    parser.add_argument("--compilation-date", type=str, required=True)
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args()

    if not (args.zip_path or args.zip_url):
        parser.error("must supply --zip-path or --zip-url")
    if not (args.mapping_csv or args.mapping_url):
        parser.error("must supply --mapping-csv or --mapping-url")

    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
    feed = "part2" if args.part == "2" else "part3"
    handle = None
    tmp = None
    bytes_downloaded = 0
    source_sha256 = None
    source_byte_size = None

    try:
        supabase = _get_raw_supabase_client()
        ensure_bucket(supabase)

        tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(tmp.name)

        if args.zip_path:
            zip_path = args.zip_path
        else:
            zip_path = tmp_root / "compilation.zip"
            handle = start_run(
                run_id=run_id,
                feed_name=feed,
                source_url=args.zip_url,
                source_filename=Path(args.zip_url).name,
                compilation_date=args.compilation_date,
            )
            bytes_downloaded, source_sha256 = stream_download(args.zip_url, zip_path)
            source_byte_size = bytes_downloaded

        if handle is None:
            handle = start_run(
                run_id=run_id,
                feed_name=feed,
                source_url=args.zip_url or f"file://{zip_path}",
                source_filename=zip_path.name,
                compilation_date=args.compilation_date,
            )

        if args.mapping_csv:
            mapping_path = args.mapping_csv
        else:
            mapping_path = tmp_root / "mapping.csv"
            stream_download(args.mapping_url, mapping_path)

        mapping_rows = read_mapping_csv(mapping_path)
        logger.info(
            "mapping_loaded",
            extra={"mapping_path": str(mapping_path), "rows": len(mapping_rows)},
        )

        with zipfile.ZipFile(zip_path, "r") as zf:
            pdf_index = read_zip_pdf_index(zf)
            logger.info(
                "zip_indexed",
                extra={"zip_path": str(zip_path), "pdf_count": len(pdf_index)},
            )

            if args.part == "2":
                loaded, skipped, uploaded = load_part2(
                    zf=zf,
                    pdf_index=pdf_index,
                    mapping_rows=mapping_rows,
                    csv_filename=mapping_path.name,
                    zip_filename=zip_path.name,
                    compilation_date=args.compilation_date,
                    supabase=supabase,
                )
            else:
                loaded, skipped, uploaded = load_part3(
                    zf=zf,
                    pdf_index=pdf_index,
                    mapping_rows=mapping_rows,
                    csv_filename=mapping_path.name,
                    zip_filename=zip_path.name,
                    compilation_date=args.compilation_date,
                    supabase=supabase,
                )

        finish_run(
            handle,
            status="completed",
            rows_loaded=loaded,
            rows_skipped_idempotent=skipped,
            pdfs_uploaded=uploaded,
            pdfs_skipped_idempotent=skipped,
            bytes_downloaded=bytes_downloaded or None,
            source_sha256=source_sha256,
            source_byte_size=source_byte_size,
        )
        logger.info(
            "ingest_complete",
            extra={"feed": feed, "loaded": loaded, "skipped": skipped, "uploaded": uploaded},
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
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
