#!/usr/bin/env python3
"""Catalog drift remediation (2026-05-28).

Registers 8 Lance datasets that were built (and are live + fresh on R2) but
never landed in ops.data_sources, so the catalog points at older generations
of the same spine while consumers reading the registry miss the canonical cut.

Successor to remediate_sam_poc_catalog_drift_20260525.py (which covered an
earlier orphan set; all 8 of its datasets are now registered). This pass adds:

  spines.sam_recipients_lance
      Canonical federal-recipient spine — one row per distinct UEI across
      SAM-registered + prime + contract-subawardee + assistance-subawardee
      populations (~896K). Supersedes the slim spines.sam_entities_lance
      (12-col, SAM-only) as the join axis for every SAM address bridge.
  spines.sam_pocs_ffata_officers_lance
      Per-UEI people spine — 6 SAM POC kinds + FFATA highly-compensated
      officer name/amount lists from USAspending subaward filings (~884K).
      Supersedes the POC-only sam_entity_pocs_lance.
  bridges.assistance_subawardee_overture_address_lance
  bridges.sam_sos_co_entities_lance
  bridges.sos_ca_overture_address_lance
  bridges.sos_co_overture_address_lance
  bridges.sos_fl_overture_address_lance
  bridges.sos_ny_overture_address_lance

Display-name convention is dot-separated (matching the 20260518142248 +
20260525 precedents). Single atomic transaction; ON CONFLICT (display_name)
DO NOTHING makes re-runs idempotent. No SLA rows are written — these spines /
bridges are rebuilt on demand, not on a daily cron, so a freshness SLA would
emit false dormancy alerts.

Usage:
  doppler run --project hq-all --config prd -- \\
    python3 apps/data-engine-x/scripts/remediate_catalog_drift_20260528.py
"""
from __future__ import annotations

import os
import sys

import psycopg

S3_ROOT = "s3://dex-raw-landing-zone/polaris-warehouse"

# (display_name, storage_uri) — display_name uses dot separator per house
# convention; storage_uri preserves the slash-pathed S3 key.
ORPHAN_DATASETS: list[tuple[str, str]] = [
    ("spines.sam_recipients_lance",
     f"{S3_ROOT}/spines/sam_recipients_lance"),
    ("spines.sam_pocs_ffata_officers_lance",
     f"{S3_ROOT}/spines/sam_pocs_ffata_officers_lance"),
    ("bridges.assistance_subawardee_overture_address_lance",
     f"{S3_ROOT}/bridges/assistance_subawardee_overture_address_lance"),
    ("bridges.sam_sos_co_entities_lance",
     f"{S3_ROOT}/bridges/sam_sos_co_entities_lance"),
    ("bridges.sos_ca_overture_address_lance",
     f"{S3_ROOT}/bridges/sos_ca_overture_address_lance"),
    ("bridges.sos_co_overture_address_lance",
     f"{S3_ROOT}/bridges/sos_co_overture_address_lance"),
    ("bridges.sos_fl_overture_address_lance",
     f"{S3_ROOT}/bridges/sos_fl_overture_address_lance"),
    ("bridges.sos_ny_overture_address_lance",
     f"{S3_ROOT}/bridges/sos_ny_overture_address_lance"),
]


def main() -> int:
    dsn = os.environ.get("DEX_DB_URL_DIRECT")
    if not dsn:
        print("ERROR: DEX_DB_URL_DIRECT is not set in the environment.", file=sys.stderr)
        print("       Run via: doppler run --project hq-all --config prd -- python3 ...", file=sys.stderr)
        return 2

    values_clause = ",\n      ".join(
        ["(%s, %s, 'lance', 'active', 'data-engine-x')"] * len(ORPHAN_DATASETS)
    )
    insert_params: list[str] = [v for pair in ORPHAN_DATASETS for v in pair]

    print("=" * 72)
    print("Catalog drift remediation — 2026-05-28")
    print("Target: data-engine-x (DEX_DB_URL_DIRECT)")
    print("Mode:   single atomic transaction")
    print("=" * 72)

    with psycopg.connect(dsn) as conn:
        cur = conn.execute(
            f"""
            INSERT INTO ops.data_sources
                (display_name, storage_uri, format, status, owner_app)
            VALUES
                {values_clause}
            ON CONFLICT (display_name) DO NOTHING
            RETURNING display_name
            """,
            insert_params,
        )
        inserted = [row[0] for row in cur.fetchall()]
        skipped = sorted(set(d[0] for d in ORPHAN_DATASETS) - set(inserted))

        print()
        print(f"ops.data_sources — candidate orphans: {len(ORPHAN_DATASETS)}")
        print(f"    INSERTed:                          {len(inserted)}")
        print(f"    skipped (ON CONFLICT DO NOTHING):  {len(skipped)}")
        for name in inserted:
            print(f"      + INSERT {name}")
        for name in skipped:
            print(f"      = SKIP   {name} (already registered)")

        conn.commit()

        print()
        print("=" * 72)
        print(f"COMMIT — transaction closed cleanly. inserted={len(inserted)}")
        print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
