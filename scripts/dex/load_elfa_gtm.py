"""One-shot load: ELFA FundingSource funders -> gtm.* canonical book.

Reads the Clay-enriched ELFA export CSV (the scrape columns plus Clay-added
Website / domain / company LinkedIn) and upserts:

  gtm.companies  one row per funder        (source='elfa')
  gtm.people     one row per primary contact
  gtm.emails     contact email             (source='elfa', verification_status='self_listed')
  gtm.phones     contact phone             (source='elfa')

Idempotent: every upsert is ON CONFLICT DO UPDATE on the table's natural key.
ELFA contact emails are contact-published, so they land 'self_listed' and
skip MillionVerifier verification.

Run:
  doppler run --project hq-all --config prd -- \
    python3 apps/data-engine-x/scripts/load_elfa_gtm.py
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

import psycopg

DEFAULT_CSV = (
    "/Users/benjamincrane/Downloads/"
    "elfa_fundingsource_with_website_2026-05-18-Default-view-export-1779142638417.csv"
)
SOURCE = "elfa"


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return s or None


def _digits(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default=DEFAULT_CSV)
    args = p.parse_args(argv)

    if not os.path.exists(args.csv):
        print(f"FATAL: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        print("FATAL: DEX_DB_URL_DIRECT / DEX_DB_URL_POOLED not in env", file=sys.stderr)
        return 2

    with open(args.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Read {len(rows)} rows from {args.csv}", file=sys.stderr)

    n_co = n_pe = n_em = n_ph = 0
    skipped_no_name = 0

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for r in rows:
                company_name = _clean(r.get("entity_name"))
                if not company_name:
                    continue
                entity_id = _clean(r.get("entity_id_int"))
                domain = _clean(r.get("Normalize a Domain"))
                if domain:
                    domain = domain.lower()
                company_linkedin = _clean(r.get("company_linkedin_url"))

                cur.execute(
                    """
                    INSERT INTO gtm.companies
                        (company_name, domain, linkedin_url,
                         source, source_external_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (source, source_external_id)
                        WHERE source_external_id IS NOT NULL
                    DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        domain       = EXCLUDED.domain,
                        linkedin_url = EXCLUDED.linkedin_url,
                        updated_at   = now()
                    RETURNING id
                    """,
                    (company_name, domain, company_linkedin,
                     SOURCE, entity_id),
                )
                company_id = cur.fetchone()[0]
                n_co += 1

                contact_name = _clean(r.get("primary_contact_name"))
                if not contact_name:
                    skipped_no_name += 1
                    continue

                cur.execute(
                    """
                    INSERT INTO gtm.people
                        (company_id, full_name, company_linkedin_url, source)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (company_id, lower(full_name))
                    DO UPDATE SET
                        company_linkedin_url = EXCLUDED.company_linkedin_url,
                        updated_at           = now()
                    RETURNING id
                    """,
                    (company_id, contact_name, company_linkedin, SOURCE),
                )
                person_id = cur.fetchone()[0]
                n_pe += 1

                email = _clean(r.get("primary_contact_email"))
                if email:
                    cur.execute(
                        """
                        INSERT INTO gtm.emails
                            (person_id, email, source, verification_status)
                        VALUES (%s, %s, %s, 'self_listed')
                        ON CONFLICT (person_id, lower(email))
                        DO UPDATE SET
                            source              = EXCLUDED.source,
                            verification_status = EXCLUDED.verification_status,
                            updated_at          = now()
                        """,
                        (person_id, email.lower(), SOURCE),
                    )
                    n_em += 1

                phone = _clean(r.get("primary_contact_phone"))
                if phone:
                    cur.execute(
                        """
                        INSERT INTO gtm.phones
                            (person_id, phone, phone_normalized, source)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (person_id, phone_normalized)
                        DO UPDATE SET
                            source     = EXCLUDED.source,
                            updated_at = now()
                        """,
                        (person_id, phone, _digits(phone), SOURCE),
                    )
                    n_ph += 1
        conn.commit()

    print(
        f"Loaded: companies={n_co}  people={n_pe}  emails={n_em}  phones={n_ph}  "
        f"(companies w/o contact name: {skipped_no_name})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
