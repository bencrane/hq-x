#!/usr/bin/env python3
"""SAM.gov POC catalog drift remediation (2026-05-25).

Single atomic transaction against the data-engine-x Supabase Postgres
(DEX_DB_URL_DIRECT):

  1. INSERT 8 SAM-adjacent Lance datasets into ops.data_sources with
     ON CONFLICT (display_name) DO NOTHING. Display-name convention is
     dot-separated (matching the 20260518142248 precedent). Already-registered
     rows surface as silent no-ops; exact insert count is reported via
     RETURNING.
  2. UPSERT daily freshness SLA for sam_entity_pocs_lance into
     ops.data_source_slas (86400 s, last_ingested). Source_id is resolved by
     SELECT against ops.data_sources.display_name — fails loudly if absent.
  3. DELETE 3 retired bulk_ingest sentinel rows by display_name (not
     storage_uri — sentinels were emitted as display_name='bulk_ingest_unmapped_*'
     by migrate_feed_ingest_runs_to_observability_ledger.py).

Usage:
  doppler run --project hq-all --config prd -- \\
    python3 apps/data-engine-x/scripts/remediate_sam_poc_catalog_drift_20260525.py
"""
from __future__ import annotations

import os
import sys

import psycopg

S3_ROOT = "s3://dex-raw-landing-zone/polaris-warehouse"

# (display_name, storage_uri) — display_name uses dot separator per house
# convention (20260518142248); storage_uri preserves the slash-pathed S3 key.
ORPHAN_DATASETS: list[tuple[str, str]] = [
    ("sam_gov.entities_longitudinal_v2_lance",
     f"{S3_ROOT}/sam_gov/entities_longitudinal_v2_lance"),
    ("sam_gov.entities_longitudinal_pre_v2_lance",
     f"{S3_ROOT}/sam_gov/entities_longitudinal_pre_v2_lance"),
    ("spines.sam_entities_lance",
     f"{S3_ROOT}/spines/sam_entities_lance"),
    ("spines.sam_usaspending_capital_matrix_lance",
     f"{S3_ROOT}/spines/sam_usaspending_capital_matrix_lance"),
    ("bridges.sam_construction_contractors_lance",
     f"{S3_ROOT}/bridges/sam_construction_contractors_lance"),
    ("bridges.sam_sos_ca_principals_lance",
     f"{S3_ROOT}/bridges/sam_sos_ca_principals_lance"),
    ("bridges.sam_sos_fl_officers_lance",
     f"{S3_ROOT}/bridges/sam_sos_fl_officers_lance"),
    ("bridges.fmcsa_sam_usaspending_lance",
     f"{S3_ROOT}/bridges/fmcsa_sam_usaspending_lance"),
]

SLA_TARGET_DISPLAY_NAME = "sam_entity_pocs_lance"
SLA_FRESHNESS_SECONDS = 86400  # daily
SLA_BASIS = "last_ingested"
SLA_NOTES = "Daily freshness contract — established 2026-05-25 SAM POC remediation pass."

SENTINELS_TO_PURGE: list[str] = [
    "bulk_ingest_unmapped_sam_opps_active_active",
    "bulk_ingest_unmapped_sam_opps_api_uei_enrichment",
    "bulk_ingest_unmapped_sam_opps_archived_fy_archive",
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
    print("SAM POC catalog drift remediation — 2026-05-25")
    print("Target: data-engine-x (DEX_DB_URL_DIRECT)")
    print("Mode:   single atomic transaction")
    print("=" * 72)

    with psycopg.connect(dsn) as conn:
        # --- Step 1: UPSERT orphan datasets ----------------------------------
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
        print(f"[1] ops.data_sources — candidate orphans: {len(ORPHAN_DATASETS)}")
        print(f"    INSERTed:                              {len(inserted)}")
        print(f"    skipped (ON CONFLICT DO NOTHING):      {len(skipped)}")
        for name in inserted:
            print(f"      + INSERT {name}")
        for name in skipped:
            print(f"      = SKIP   {name} (already registered)")

        # --- Step 2: UPSERT daily SLA for sam_entity_pocs_lance --------------
        cur = conn.execute(
            """
            INSERT INTO ops.data_source_slas
                (source_id, sla_freshness_seconds, sla_basis, notes)
            SELECT source_id, %s, %s::data_source_sla_basis, %s
            FROM ops.data_sources
            WHERE display_name = %s
            ON CONFLICT (source_id) DO UPDATE
                SET sla_freshness_seconds = EXCLUDED.sla_freshness_seconds,
                    sla_basis             = EXCLUDED.sla_basis,
                    notes                 = EXCLUDED.notes,
                    updated_at            = NOW()
            RETURNING source_id, sla_freshness_seconds, sla_basis
            """,
            (SLA_FRESHNESS_SECONDS, SLA_BASIS, SLA_NOTES, SLA_TARGET_DISPLAY_NAME),
        )
        sla_row = cur.fetchone()
        if sla_row is None:
            raise RuntimeError(
                f"SLA UPSERT affected 0 rows — display_name "
                f"'{SLA_TARGET_DISPLAY_NAME}' not found in ops.data_sources. "
                f"Aborting transaction; nothing committed."
            )

        print()
        print(f"[2] ops.data_source_slas — UPSERT for {SLA_TARGET_DISPLAY_NAME}")
        print(f"      source_id:              {sla_row[0]}")
        print(f"      sla_freshness_seconds:  {sla_row[1]}  (= {sla_row[1] // 3600} h)")
        print(f"      sla_basis:              {sla_row[2]}")

        # --- Step 3: DELETE sentinel rows -----------------------------------
        cur = conn.execute(
            """
            DELETE FROM ops.data_sources
            WHERE display_name = ANY(%s)
            RETURNING display_name
            """,
            (SENTINELS_TO_PURGE,),
        )
        deleted = [row[0] for row in cur.fetchall()]
        not_found = sorted(set(SENTINELS_TO_PURGE) - set(deleted))

        print()
        print(f"[3] ops.data_sources — sentinels targeted: {len(SENTINELS_TO_PURGE)}")
        print(f"    DELETEd:                                {len(deleted)}")
        print(f"    not found (no-op):                      {len(not_found)}")
        for name in deleted:
            print(f"      - DELETE {name}")
        for name in not_found:
            print(f"      ? MISS   {name} (not present)")

        conn.commit()

        print()
        print("=" * 72)
        print(f"COMMIT — transaction closed cleanly.")
        print(
            f"Summary: inserted={len(inserted)} sla_upserts=1 "
            f"deleted={len(deleted)}"
        )
        print("=" * 72)

    return 0


if __name__ == "__main__":
    sys.exit(main())
