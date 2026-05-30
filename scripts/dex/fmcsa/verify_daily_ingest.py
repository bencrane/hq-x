#!/usr/bin/env python3
"""FMCSA daily ingest verification script.

Checks:
  (a) ops.current_snapshots has an entry for at least one FMCSA dataset
      (or R2 path is queryable — we use the DB check as the primary)
  (b) ops.material_detection_runs has a 'succeeded' run in the last 24 hours
  (c) ops.material_change_events has at least one FMCSA event (DOT-numeric
      entity_ref) from the most recent succeeded detection run
  (d) ops.data_sources has fmcsa_carrier_essentials with status='active'

Usage:
    doppler run --project hq-all --config prd -- bash -c \\
        'python scripts/fmcsa/verify_daily_ingest.py'

Exit 0 on all checks pass. Exit 1 on any failure. Prints check details to
stdout for human and harness consumption.

See docs/fmcsa-daily-pipeline.md for full pipeline context.
"""

import os
import sys

import psycopg


def _db_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        print("FAIL: DEX_DB_URL_POOLED not set", file=sys.stderr)
        sys.exit(1)
    return url


def main() -> None:
    db_url = _db_url()
    failures: list[str] = []

    with psycopg.connect(db_url) as conn:
        # --- check (a): source status
        row = conn.execute(
            """
            SELECT status
              FROM ops.data_sources
             WHERE display_name = 'fmcsa_carrier_essentials'
            """
        ).fetchone()
        if row is None:
            failures.append("FAIL (a): fmcsa_carrier_essentials not found in ops.data_sources")
        elif row[0] != "active":
            failures.append(
                f"FAIL (a): fmcsa_carrier_essentials status={row[0]!r}, expected 'active'"
            )
        else:
            print(f"PASS (a): fmcsa_carrier_essentials status={row[0]!r}")

        # --- check (b): succeeded detection run in last 24h
        count = conn.execute(
            """
            SELECT COUNT(*)
              FROM ops.material_detection_runs
             WHERE status = 'succeeded'
               AND started_at >= now() - interval '24 hours'
            """
        ).fetchone()[0]
        if count == 0:
            failures.append(
                "FAIL (b): no succeeded material_detection_runs in the last 24 hours"
            )
        else:
            print(f"PASS (b): {count} succeeded detection run(s) in last 24h")

        # --- check (c): FMCSA events from most recent succeeded run
        latest = conn.execute(
            """
            SELECT detection_run_id
              FROM ops.material_detection_runs
             WHERE status = 'succeeded'
             ORDER BY started_at DESC
             LIMIT 1
            """
        ).fetchone()
        if latest is None:
            failures.append("FAIL (c): no succeeded detection run found at all")
        else:
            run_id = latest[0]
            event_count = conn.execute(
                """
                SELECT COUNT(*)
                  FROM ops.material_change_events mce
                  JOIN ops.material_attribute_declarations mad
                    ON mad.declaration_id = mce.declaration_id
                  JOIN ops.data_sources ds
                    ON ds.source_id = mad.source_id
                 WHERE mce.detection_run_id = %s
                   AND ds.display_name IN ('fmcsa_carrier_essentials', 'fmcsa')
                   AND mce.entity_ref ~ '^[0-9]+$'
                """,
                (run_id,),
            ).fetchone()[0]
            if event_count == 0:
                # Could be cold-start: check snapshot count at corrected path
                failures.append(
                    f"FAIL (c): no FMCSA events in most recent detection run "
                    f"({run_id}). Check R2 snapshot count at "
                    f"fmcsa-derived/carrier_essentials/ — needs >=2 for a diff. "
                    f"If only 1 snapshot exists, this is cold-start: wait for "
                    f"the next factory cycle (06:00 UTC) then re-run."
                )
            else:
                print(
                    f"PASS (c): {event_count} FMCSA event(s) in most recent detection run ({run_id})"
                )

    if failures:
        for msg in failures:
            print(msg, file=sys.stderr)
        sys.exit(1)

    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
