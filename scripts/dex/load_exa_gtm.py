"""One-shot load: exa.ai equipment-financing companies -> gtm.companies.

Reads the exa.ai export CSV (company_name, domain, company_linkedin_url) and
upserts one row per company into gtm.companies (source='exa'). Companies-only
list with no contacts. Operator LLM-validated every row as an equipment-
financing company before export.

Idempotent: upsert on (source, source_external_id), where source_external_id
is the company domain (the exa export carries no natural ID).

Run:
  doppler run --project hq-all --config prd -- \
    python3 apps/data-engine-x/scripts/load_exa_gtm.py
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import psycopg

DEFAULT_CSV = (
    "/Users/benjamincrane/Downloads/"
    "exa.ai - equipment financing companies - Sheet1.csv"
)
SOURCE = "exa"


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return s or None


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

    n_co = 0
    skipped = 0

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for r in rows:
                company_name = _clean(r.get("company_name"))
                domain = _clean(r.get("domain"))
                if not company_name or not domain:
                    skipped += 1
                    continue
                domain = domain.lower()
                linkedin = _clean(r.get("company_linkedin_url"))

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
                    """,
                    (company_name, domain, linkedin, SOURCE, domain),
                )
                n_co += 1
        conn.commit()

    print(f"Loaded: companies={n_co}  (skipped no name/domain: {skipped})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
