"""One-shot load: SFNet asset-based-lending directory -> gtm.* canonical book.

Two phases:
  1. Land each of the 5 operator-staged SFNet CSVs verbatim into its own
     gtm.raw_sfnet_* table — one JSONB row per CSV row, original headers
     preserved as JSON keys. Each raw table is TRUNCATEd then reloaded.
  2. Extract canonical rows into gtm.companies + gtm.people (source='sfnet'),
     idempotent upserts:
       - companies <- raw_sfnet_companies
       - people    <- raw_sfnet_people_enriched_with_linkedin (richest CSV:
                      split name, location, company LinkedIn, profile URL)

SFNet CSVs carry no person emails and only company-level phones, so this
load populates companies + people only. Emails/phones come from providers
later; company work_phone / fax stay in raw_sfnet_companies for trace-back.

Run:
  doppler run --project hq-all --config prd -- \
    python3 apps/data-engine-x/scripts/load_sfnet_gtm.py
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import psycopg
from psycopg.types.json import Jsonb

SOURCE = "sfnet"
DOWNLOADS = "/Users/benjamincrane/Downloads"

# (raw table name, CSV filename)
CSVS = [
    ("raw_sfnet_seed_urls",
     "sfnet_seed_urls-Default-view-export-1779315959339.csv"),
    ("raw_sfnet_companies",
     "sfnet_companies-Default-view-export-1779315883422.csv"),
    ("raw_sfnet_people",
     "sfnet_people-Default-view-export-1779315906021.csv"),
    ("raw_sfnet_people_enriched",
     "sfnet_people_enriched-Default-view-export-1779315922064.csv"),
    ("raw_sfnet_people_enriched_with_linkedin",
     "sfnet_people_enriched_with_linkedin-Default-view-export-1779316020818.csv"),
]


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--downloads", default=DOWNLOADS)
    args = p.parse_args(argv)

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        print("FATAL: DEX_DB_URL_DIRECT / DEX_DB_URL_POOLED not in env", file=sys.stderr)
        return 2

    # Read all 5 CSVs up front so a missing file fails before any DB write.
    loaded: dict[str, list[dict]] = {}
    for tbl, fname in CSVS:
        path = os.path.join(args.downloads, fname)
        if not os.path.exists(path):
            print(f"FATAL: CSV not found: {path}", file=sys.stderr)
            return 2
        with open(path, newline="", encoding="utf-8") as f:
            loaded[tbl] = list(csv.DictReader(f))
        print(f"  read {len(loaded[tbl]):>5} rows  {fname}", file=sys.stderr)

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # Phase 1 — land each CSV verbatim into its raw table.
            for tbl, _ in CSVS:
                cur.execute(f"TRUNCATE gtm.{tbl}")
                payload = [
                    (Jsonb({k: v for k, v in row.items() if k is not None}),)
                    for row in loaded[tbl]
                ]
                if payload:
                    cur.executemany(
                        f"INSERT INTO gtm.{tbl} (row_data) VALUES (%s)", payload
                    )
            print("Phase 1: raw tables loaded", file=sys.stderr)

            # Phase 2 — extract companies from raw_sfnet_companies.
            cur.execute("SELECT row_data FROM gtm.raw_sfnet_companies")
            company_id_by_url: dict[str, str] = {}
            n_co = 0
            for (rd,) in cur.fetchall():
                name = _clean(rd.get("company_name"))
                if not name:
                    continue
                source_url = _clean(rd.get("source_url"))
                domain = _clean(rd.get("Normalize a Domain"))
                if domain:
                    domain = domain.lower()
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
                    (name, domain,
                     _clean(rd.get("company_linkedin_url")),
                     SOURCE, source_url),
                )
                cid = cur.fetchone()[0]
                if source_url:
                    company_id_by_url[source_url] = cid
                n_co += 1
            print(f"Phase 2: companies upserted = {n_co}", file=sys.stderr)

            # Phase 3 — extract people from the richest people CSV.
            cur.execute(
                "SELECT row_data FROM gtm.raw_sfnet_people_enriched_with_linkedin"
            )
            n_pe = 0
            n_orphan = 0
            for (rd,) in cur.fetchall():
                full_name = _clean(rd.get("person_name"))
                if not full_name:
                    continue
                company_url = _clean(rd.get("company_source_url"))
                company_id = company_id_by_url.get(company_url) if company_url else None
                if company_id is None:
                    n_orphan += 1
                    continue
                cur.execute(
                    """
                    INSERT INTO gtm.people
                        (company_id, full_name, first_name, last_name, title,
                         company_linkedin_url, source, source_external_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (company_id, lower(full_name))
                    DO UPDATE SET
                        first_name           = EXCLUDED.first_name,
                        last_name            = EXCLUDED.last_name,
                        title                = EXCLUDED.title,
                        company_linkedin_url = EXCLUDED.company_linkedin_url,
                        source_external_id   = EXCLUDED.source_external_id,
                        updated_at           = now()
                    """,
                    (company_id, full_name,
                     _clean(rd.get("person_first_name")),
                     _clean(rd.get("person_last_name")),
                     _clean(rd.get("person_title")),
                     _clean(rd.get("company_linkedin_url")),
                     SOURCE,
                     _clean(rd.get("person_profile_url"))),
                )
                n_pe += 1
        conn.commit()

    print(
        f"Loaded SFNet: companies={n_co}  people={n_pe}  "
        f"(people with no matched company: {n_orphan})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
