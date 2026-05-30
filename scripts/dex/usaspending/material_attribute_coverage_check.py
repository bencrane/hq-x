#!/usr/bin/env python3
"""USAspending material-attribute coverage check.

Cycle: usaspending-pipeline-remediation (2026-05-13). Parallel to
scripts/fmcsa/material_attribute_coverage_check.py.

For each declared USAspending material attribute (in ops.material_attribute_declarations
joined to ops.data_sources where display_name='usaspending_contracts_lance'), count
events emitted in ops.material_change_events over the last 7 days.

Exits 0 if all attributes have at least 1 event in 7d (active).
Exits 1 if any attribute is dormant (0 events in 7d).

NOTE: Cold-start expectation — on the first weekly cron run after deploy, ALL
4 USAspending declarations will be dormant (no detection runs yet have wired
the resolver against ≥2 snapshots). Operator should expect a single
cohort_drift alert on the first Monday post-deploy.

Prints a Markdown table to stdout with one row per attribute.

Usage:
    doppler run --project hq-all --config prd -- bash -c \\
        'python scripts/usaspending/material_attribute_coverage_check.py'
"""

import os
import sys

import psycopg


def _db_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        print("FAIL: DEX_DB_URL_POOLED (or DIRECT) not set", file=sys.stderr)
        sys.exit(2)
    return url


def main() -> None:
    db_url = _db_url()

    with psycopg.connect(db_url) as conn:
        row = conn.execute(
            """
            SELECT source_id
              FROM ops.data_sources
             WHERE display_name = 'usaspending_contracts_lance'
            """
        ).fetchone()
        if row is None:
            print("FAIL: usaspending_contracts_lance not in ops.data_sources", file=sys.stderr)
            sys.exit(2)
        source_id = row[0]

        rows = conn.execute(
            """
            SELECT
                d.attribute_name,
                d.change_kind,
                COUNT(e.event_id) AS events_7d,
                MAX(e.detected_at) AS last_event_at
              FROM ops.material_attribute_declarations d
         LEFT JOIN ops.material_change_events e
                ON e.declaration_id = d.declaration_id
               AND e.detected_at >= now() - interval '7 days'
             WHERE d.source_id = %s
          GROUP BY d.attribute_name, d.change_kind
          ORDER BY d.attribute_name
            """,
            (source_id,),
        ).fetchall()

    dormant = [r for r in rows if r[2] == 0]

    # Markdown table
    print("| attribute_name | change_kind | events_7d | last_event_at |")
    print("|---|---|---|---|")
    for attribute_name, change_kind, events_7d, last_event_at in rows:
        print(
            f"| {attribute_name} | {change_kind} | {events_7d} | "
            f"{last_event_at.isoformat() if last_event_at else 'never'} |"
        )

    if dormant:
        names = ", ".join(r[0] for r in dormant)
        print(f"\nFAIL: {len(dormant)} dormant attribute(s): {names}", file=sys.stderr)
        sys.exit(1)

    print(f"\nPASS: all {len(rows)} attribute(s) active in last 7d")
    sys.exit(0)


if __name__ == "__main__":
    main()
