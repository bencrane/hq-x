#!/usr/bin/env python3
"""Ingest SEC Form ADV-W (withdrawal filings) from a compilation ZIP.

Lands rows into:
    entities.sec_form_adv_w_main             (ADVW_*.csv,        43 cols)
    entities.sec_form_adv_w_w1_5_locations   (ADVW_W1_5_*.csv,   16 cols)
    entities.sec_form_adv_w_w1_8_custodians  (ADVW_W1_8_*.csv,   26 cols)
    entities.sec_form_adv_w_w2_balance_sheet (ADVW_W2_*.csv,     25 cols)

ADV-W is small (5 MB historical / tens of KB monthly delta). We coerce
well-known numeric/date columns at ingest. Everything also lives in
raw_jsonb.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_sec_form_adv_w_ingest.py \\
        --zip-path /tmp/sec_form_adv/advw-historical.zip \\
        --compilation-date 2024-12-31

    PYTHONPATH=. doppler run -- python3 scripts/run_sec_form_adv_w_ingest.py \\
        --source-url https://www.sec.gov/files/advw-20241201-20241231.zip \\
        --compilation-date 2024-12-01
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg.types.json import Json  # noqa: E402

from scripts.sec_form_adv_common import (  # noqa: E402
    chunked,
    classify_error,
    db_connection,
    finish_run,
    parse_int,
    parse_iso_date,
    parse_numeric,
    start_run,
    stream_download,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("sec_form_adv_w")

CHUNK_SIZE = 2_000


def s(raw, *keys):
    """Get the first non-empty stripped value for any of the given keys."""
    for k in keys:
        v = raw.get(k)
        if v is not None:
            stripped = str(v).strip().strip('"')
            if stripped:
                return stripped
    return None


def open_csv_in_zip(zf, name):
    raw = zf.open(name, "r")
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    return csv.DictReader(text)


def coerce_filing_id(raw):
    return parse_int(s(raw, "Filing ID", "FilingID"))


def load_main(cur, csv_iter, csv_name, compilation_date):
    rows_loaded = 0
    rows_skipped = 0

    def gen():
        for raw in csv_iter:
            filing_id = coerce_filing_id(raw)
            if filing_id is None:
                continue
            crd = parse_int(s(raw, "CRD Number", "CRDNumber"))
            filing_date = parse_iso_date(s(raw, "Filing Date"))
            date_ceased_dt = parse_iso_date(s(raw, "Date Ceased"))
            signature_date_dt = parse_iso_date(s(raw, "Signature Date"))
            yield (
                filing_id,
                crd,
                s(raw, "SEC File Number"),
                s(raw, "Primary Business Name"),
                s(raw, "Full Legal Name"),
                s(raw, "Form Type"),
                s(raw, "Filing Type"),
                s(raw, "Form Version"),
                filing_date,
                s(raw, "Contact Name"),
                s(raw, "Contact Title"),
                s(raw, "Contact Street 1"),
                s(raw, "Contact Street 2"),
                s(raw, "Contact City"),
                s(raw, "Contact State"),
                s(raw, "Contact Country"),
                s(raw, "Contact Postal Code"),
                s(raw, "Contact Phone"),
                s(raw, "Contact Email"),
                s(raw, "Office Street 1"),
                s(raw, "Office Street 2"),
                s(raw, "Office City"),
                s(raw, "Office State"),
                s(raw, "Office Country"),
                s(raw, "Office Postal Code"),
                s(raw, "Private Residence"),
                s(raw, "Business Ceased"),
                date_ceased_dt.date() if date_ceased_dt else None,
                s(raw, "Reasons for Withdrawal"),
                s(raw, "Custody of Client Assets"),
                parse_int(raw.get("Number of Clients")),
                parse_numeric(raw.get("Cash Amount")),
                parse_numeric(raw.get("Market Value of Securities")),
                parse_numeric(raw.get("Market Value of Other Assets")),
                parse_numeric(raw.get("Money Owed")),
                parse_numeric(raw.get("Prepaid Fees")),
                parse_numeric(raw.get("Borrowed Funds")),
                s(raw, "Contacts Assigned"),
                s(raw, "Client Consent"),
                s(raw, "Unsatisfied Judgments/Liens", "Unsatisfied Judgments"),
                signature_date_dt.date() if signature_date_dt else None,
                s(raw, "Signature Name"),
                s(raw, "Signature Title"),
                Json(raw),
                csv_name,
                compilation_date,
            )

    for batch in chunked(gen(), CHUNK_SIZE):
        cur.executemany(
            """
            INSERT INTO entities.sec_form_adv_w_main
              (filing_id, crd_number, sec_number, primary_business_name,
               full_legal_name, form_type, filing_type, form_version, filing_date,
               contact_name, contact_title, contact_street_1, contact_street_2,
               contact_city, contact_state, contact_country, contact_postal_code,
               contact_phone, contact_email,
               office_street_1, office_street_2, office_city, office_state,
               office_country, office_postal_code,
               private_residence, business_ceased, date_ceased,
               reasons_for_withdrawal, custody_of_client_assets,
               number_of_clients, cash_amount, market_value_of_securities,
               market_value_of_other_assets, money_owed, prepaid_fees,
               borrowed_funds, contacts_assigned, client_consent,
               unsatisfied_judgments_liens, signature_date, signature_name,
               signature_title, raw_jsonb, source_csv_filename, compilation_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (filing_id, compilation_date) DO NOTHING
            """,
            batch,
        )
        rows_loaded += cur.rowcount
        rows_skipped += len(batch) - cur.rowcount
    return rows_loaded, rows_skipped


def load_w1_5(cur, csv_iter, csv_name, compilation_date):
    rows_loaded = 0
    rows_skipped = 0

    def gen():
        for idx, raw in enumerate(csv_iter):
            filing_id = coerce_filing_id(raw)
            if filing_id is None:
                continue
            crd = parse_int(s(raw, "CRD Number", "CRDNumber"))
            filing_date = parse_iso_date(s(raw, "Filing Date"))
            yield (
                filing_id,
                crd,
                s(raw, "Primary Business Name"),
                s(raw, "Form Type"),
                s(raw, "Filing Type"),
                s(raw, "Form Version"),
                filing_date,
                s(raw, "Name"),
                s(raw, "Street 1"),
                s(raw, "Street 2"),
                s(raw, "City"),
                s(raw, "State"),
                s(raw, "Country"),
                s(raw, "Postal Code"),
                s(raw, "Private Residence"),
                s(raw, "Phone Number"),
                Json(raw),
                csv_name,
                idx,
                compilation_date,
            )

    for batch in chunked(gen(), CHUNK_SIZE):
        cur.executemany(
            """
            INSERT INTO entities.sec_form_adv_w_w1_5_locations
              (filing_id, crd_number, primary_business_name, form_type,
               filing_type, form_version, filing_date, name,
               street_1, street_2, city, state, country, postal_code,
               private_residence, phone_number,
               raw_jsonb, source_csv_filename, row_index, compilation_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (filing_id, row_index, compilation_date) DO NOTHING
            """,
            batch,
        )
        rows_loaded += cur.rowcount
        rows_skipped += len(batch) - cur.rowcount
    return rows_loaded, rows_skipped


def load_w1_8(cur, csv_iter, csv_name, compilation_date):
    rows_loaded = 0
    rows_skipped = 0

    def gen():
        for idx, raw in enumerate(csv_iter):
            filing_id = coerce_filing_id(raw)
            if filing_id is None:
                continue
            crd = parse_int(s(raw, "CRD Number", "CRDNumber"))
            filing_date = parse_iso_date(s(raw, "Filing Date"))
            yield (
                filing_id,
                crd,
                s(raw, "Primary Business Name"),
                s(raw, "Form Type"),
                s(raw, "Filing Type"),
                s(raw, "Form Version"),
                filing_date,
                s(raw, "Custodian Name"),
                s(raw, "Custodian Street 1"),
                s(raw, "Custodian Street 2"),
                s(raw, "Custodian City"),
                s(raw, "Custodian State"),
                s(raw, "Custodian Country"),
                s(raw, "Custodian Postal Code"),
                s(raw, "Custodian Private Residence"),
                s(raw, "Custodian Phone Number"),
                s(raw, "Location Name"),
                s(raw, "Location Street 1"),
                s(raw, "Location Street 2"),
                s(raw, "Location City"),
                s(raw, "Location State"),
                s(raw, "Location Country"),
                s(raw, "Location Postal Code"),
                s(raw, "Location Private Residence"),
                s(raw, "Location Phone Number"),
                s(raw, "Description"),
                Json(raw),
                csv_name,
                idx,
                compilation_date,
            )

    for batch in chunked(gen(), CHUNK_SIZE):
        cur.executemany(
            """
            INSERT INTO entities.sec_form_adv_w_w1_8_custodians
              (filing_id, crd_number, primary_business_name, form_type,
               filing_type, form_version, filing_date,
               custodian_name, custodian_street_1, custodian_street_2,
               custodian_city, custodian_state, custodian_country,
               custodian_postal_code, custodian_private_residence,
               custodian_phone_number,
               location_name, location_street_1, location_street_2,
               location_city, location_state, location_country,
               location_postal_code, location_private_residence,
               location_phone_number, description,
               raw_jsonb, source_csv_filename, row_index, compilation_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s)
            ON CONFLICT (filing_id, row_index, compilation_date) DO NOTHING
            """,
            batch,
        )
        rows_loaded += cur.rowcount
        rows_skipped += len(batch) - cur.rowcount
    return rows_loaded, rows_skipped


def load_w2(cur, csv_iter, csv_name, compilation_date):
    rows_loaded = 0
    rows_skipped = 0

    def gen():
        for raw in csv_iter:
            filing_id = coerce_filing_id(raw)
            if filing_id is None:
                continue
            crd = parse_int(s(raw, "CRD Number", "CRDNumber"))
            filing_date = parse_iso_date(s(raw, "Filing Date"))
            yield (
                filing_id,
                crd,
                s(raw, "Primary Business Name"),
                s(raw, "Form Type"),
                s(raw, "Filing Type"),
                s(raw, "Form Version"),
                filing_date,
                parse_numeric(raw.get("Assets Cash")),
                parse_numeric(raw.get("Assets Securities at Market")),
                parse_numeric(raw.get("Assets Non-Marketable Sec.")),
                parse_numeric(raw.get("Assets Other")),
                parse_numeric(raw.get("Assets Total Current")),
                parse_numeric(raw.get("Assets Total Fixed")),
                parse_numeric(raw.get("Assets Total Assets")),
                parse_numeric(raw.get("Liabilities Prepaid")),
                parse_numeric(raw.get("Liabilities Short-Term Clients")),
                parse_numeric(raw.get("Liabilities Short-Term Other")),
                parse_numeric(raw.get("Liabilities Other")),
                parse_numeric(raw.get("Liabilities Total Current")),
                parse_numeric(raw.get("Liabilities LT Debt Clients")),
                parse_numeric(raw.get("Liabilities LT Debt Other")),
                parse_numeric(raw.get("Liabilities LT Other")),
                parse_numeric(raw.get("Liabilities Total Fixed")),
                parse_numeric(raw.get("Total Shareholder Equity")),
                parse_numeric(raw.get("Total Liabilities and Equity")),
                Json(raw),
                csv_name,
                compilation_date,
            )

    for batch in chunked(gen(), CHUNK_SIZE):
        cur.executemany(
            """
            INSERT INTO entities.sec_form_adv_w_w2_balance_sheet
              (filing_id, crd_number, primary_business_name, form_type,
               filing_type, form_version, filing_date,
               assets_cash, assets_securities_at_market,
               assets_non_marketable_sec, assets_other,
               assets_total_current, assets_total_fixed, assets_total_assets,
               liabilities_prepaid, liabilities_short_term_clients,
               liabilities_short_term_other, liabilities_other,
               liabilities_total_current, liabilities_lt_debt_clients,
               liabilities_lt_debt_other, liabilities_lt_other,
               liabilities_total_fixed, total_shareholder_equity,
               total_liabilities_and_equity,
               raw_jsonb, source_csv_filename, compilation_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s)
            ON CONFLICT (filing_id, compilation_date) DO NOTHING
            """,
            batch,
        )
        rows_loaded += cur.rowcount
        rows_skipped += len(batch) - cur.rowcount
    return rows_loaded, rows_skipped


CSV_DISPATCH = [
    ("ADVW_W1_5_", load_w1_5),
    ("ADVW_W1_8_", load_w1_8),
    ("ADVW_W2_", load_w2),
    ("ADVW_", load_main),  # generic ADVW_ prefix matched LAST since others share it
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip-path", type=Path)
    parser.add_argument("--source-url", type=str)
    parser.add_argument("--compilation-date", type=str, required=True)
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args()

    if not (args.zip_path or args.source_url):
        parser.error("must supply --zip-path or --source-url")

    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
    handle = None
    tmp = None
    bytes_downloaded = 0
    source_sha256 = None
    source_byte_size = None

    try:
        if args.zip_path is None:
            tmp = tempfile.TemporaryDirectory()
            zip_path = Path(tmp.name) / "advw.zip"
            handle = start_run(
                run_id=run_id,
                feed_name="adv_w",
                source_url=args.source_url,
                source_filename=Path(args.source_url).name,
                compilation_date=args.compilation_date,
            )
            bytes_downloaded, source_sha256 = stream_download(args.source_url, zip_path)
            source_byte_size = bytes_downloaded
        else:
            zip_path = args.zip_path
            handle = start_run(
                run_id=run_id,
                feed_name="adv_w",
                source_url=args.source_url or f"file://{zip_path}",
                source_filename=zip_path.name,
                compilation_date=args.compilation_date,
            )

        rows_loaded_total = 0
        rows_skipped_total = 0

        with zipfile.ZipFile(zip_path, "r") as zf, db_connection() as conn:
            with conn.cursor() as cur:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                logger.info("zip_opened", extra={"csv_count": len(csv_names)})

                for prefix, loader in CSV_DISPATCH:
                    matches = [n for n in csv_names if Path(n).name.startswith(prefix)]
                    # remove matched names so the generic ADVW_ prefix doesn't re-match
                    csv_names = [n for n in csv_names if n not in matches]
                    for name in matches:
                        logger.info(
                            "loading_csv",
                            extra={"csv": name, "loader": loader.__name__},
                        )
                        reader = open_csv_in_zip(zf, name)
                        loaded, skipped = loader(cur, reader, name, args.compilation_date)
                        rows_loaded_total += loaded
                        rows_skipped_total += skipped
                        conn.commit()

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
            extra={"loaded": rows_loaded_total, "skipped": rows_skipped_total},
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
