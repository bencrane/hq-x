#!/usr/bin/env python3
"""FMCSA material-attribute coverage check.

For each declared FMCSA material attribute (in ops.material_attribute_declarations),
count events emitted in ops.material_change_events over the last 7 days.

Exits 0 if all attributes have at least 1 event in 7d (active).
Exits 1 if any attribute is dormant (0 events in 7d).

Prints a Markdown table to stdout with one row per attribute.

Usage:
    doppler run --project hq-all --config prd -- bash -c \\
        'python scripts/fmcsa/material_attribute_coverage_check.py'

Scoped to the canonical FMCSA source (display_name='fmcsa_carrier_essentials',
source_id resolved at query time). The 4 expected attributes are:
  - email_address (value_disappeared)
  - power_units (threshold_crossed)
  - safety_rating (tier_change)
  - status_code (value_revoked)
"""

import os
import sys

import psycopg


def _db_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        print("FAIL: DEX_DB_URL_POOLED not set", file=sys.stderr)
        sys.exit(2)
    return url


def main() -> None:
    db_url = _db_url()

    with psycopg.connect(db_url) as conn:
        # Resolve canonical FMCSA source_id.
        row = conn.execute(
            """
            SELECT source_id
              FROM ops.data_sources
             WHERE display_name = 'fmcsa_carrier_essentials'
            """
        ).fetchone()
        if row is None:
            print("FAIL: fmcsa_carrier_essentials not in ops.data_sources", file=sys.stderr)
            sys.exit(2)
        source_id = row[0]

        # Per-attribute event count in last 7d.
        rows = conn.execute(
            """
            SELECT
                d.attribute_name,
                d.change_kind,
                COUNT(e.event_id) FILTER (
                    WHERE e.detected_at >= now() - interval '7 days'
                ) AS events_7d,
                MAX(e.detected_at) AS last_event_at
              FROM ops.material_attribute_declarations d
              LEFT JOIN ops.material_change_events e
                ON e.declaration_id = d.declaration_id
             WHERE d.source_id = %s
             GROUP BY d.attribute_name, d.change_kind
             ORDER BY d.attribute_name
            """,
            (source_id,),
        ).fetchall()

    if not rows:
        print("FAIL: no FMCSA material_attribute_declarations found", file=sys.stderr)
        sys.exit(2)

    # Print Markdown table.
    print("| attribute_name | change_kind | events_7d | last_event_at | status |")
    print("|----------------|-------------|-----------|---------------|--------|")
    dormant = []
    for attr, kind, count, last in rows:
        status = "active" if (count or 0) > 0 else "DORMANT"
        last_str = last.isoformat() if last is not None else "(never)"
        print(f"| {attr} | {kind} | {count or 0} | {last_str} | {status} |")
        if (count or 0) == 0:
            dormant.append(attr)

    print()
    if dormant:
        print(f"DORMANT: {len(dormant)} attribute(s) have zero events in last 7 days: {dormant}", file=sys.stderr)
        sys.exit(1)

    print(f"ALL ACTIVE: {len(rows)} attribute(s) have ≥1 event in last 7 days")
    sys.exit(0)


if __name__ == "__main__":
    main()
