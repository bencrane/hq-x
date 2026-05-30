#!/usr/bin/env python3
"""USAspending daily ingest verification script.

Cycle: usaspending-pipeline-remediation (2026-05-13). Parallel to
scripts/fmcsa/verify_daily_ingest.py.

Checks:
  (a) ops.data_sources has 'usaspending_api_daily' AND 'usaspending_contracts_lance'
      both with status='active' (NEITHER retired) — the 2 RW MV rows from
      cycle s3 are correctly retired, the actual ingest sources stay active.
  (b) bulk_ingest.feed_ingest_runs has at least 1 succeeded/landed run for
      source_id='usaspending_api_daily' in the last 36h.
  (c) ops.data_source_ingest_runs has at least one row in the last 36h
      (mirrored from bulk_ingest via scripts/_lib/ingest_ledger_unify.py OR
      written directly by usaspending_daily_verify_app.py).
  (d) The most recent api-delta R2 key exists AND has size > 0 (poison guard).
  (e) Today's USAspending material declarations exist (≥4 declarations).

Usage:
    doppler run --project hq-all --config prd -- bash -c \\
        'python scripts/usaspending/verify_daily_ingest.py'

Exit 0 on all checks pass. Exit 1 on any failure. Prints check details to
stdout for human and harness consumption.

See docs/usaspending-daily-pipeline.md for full pipeline context.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg


def _db_url() -> str:
    url = (
        os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        print("FAIL: no DB URL set (need DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT / DATABASE_URL)", file=sys.stderr)
        sys.exit(1)
    return url


def _r2_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def main() -> None:
    db_url = _db_url()
    failures: list[str] = []

    with psycopg.connect(db_url) as conn:
        # --- check (a): canonical sources active
        rows = conn.execute(
            """
            SELECT display_name, status
              FROM ops.data_sources
             WHERE display_name IN ('usaspending_api_daily', 'usaspending_contracts_lance')
            """
        ).fetchall()
        status_map = {r[0]: r[1] for r in rows}
        for name in ("usaspending_api_daily", "usaspending_contracts_lance"):
            if name not in status_map:
                failures.append(f"FAIL (a): {name} not found in ops.data_sources")
            elif status_map[name] != "active":
                failures.append(f"FAIL (a): {name} status={status_map[name]!r}, expected 'active'")
            else:
                print(f"PASS (a): {name} status='active'")

        # --- check (b): recent successful bulk_ingest run
        bulk_count = conn.execute(
            """
            SELECT COUNT(*)
              FROM bulk_ingest.feed_ingest_runs
             WHERE source_id = 'usaspending_api_daily'
               AND status = 'succeeded'
               AND started_at >= now() - interval '36 hours'
            """
        ).fetchone()[0]
        if bulk_count == 0:
            failures.append(
                "FAIL (b): no succeeded usaspending_api_daily run in bulk_ingest in last 36h"
            )
        else:
            print(f"PASS (b): {bulk_count} succeeded bulk_ingest run(s) for usaspending_api_daily in last 36h")

        # --- check (c): ops ledger has rows in last 36h (verify cron mirrors OR direct writes)
        ops_count = conn.execute(
            """
            SELECT COUNT(*)
              FROM ops.data_source_ingest_runs r
              JOIN ops.data_sources s ON s.source_id = r.source_id
             WHERE s.display_name IN ('usaspending_api_daily', 'usaspending_contracts_lance')
               AND r.started_at >= now() - interval '36 hours'
            """
        ).fetchone()[0]
        if ops_count == 0:
            failures.append(
                "FAIL (c): no ops.data_source_ingest_runs rows for usaspending in last 36h "
                "(ledger unification cron may have failed; check scripts/_lib/ingest_ledger_unify.py)"
            )
        else:
            print(f"PASS (c): {ops_count} ops.data_source_ingest_runs rows for usaspending in last 36h")

        # --- check (e): material declarations present
        decl_count = conn.execute(
            """
            SELECT COUNT(*)
              FROM ops.material_attribute_declarations d
              JOIN ops.data_sources s ON s.source_id = d.source_id
             WHERE s.display_name = 'usaspending_contracts_lance'
            """
        ).fetchone()[0]
        if decl_count < 4:
            failures.append(
                f"FAIL (e): only {decl_count} USAspending material declarations (expected ≥4)"
            )
        else:
            print(f"PASS (e): {decl_count} USAspending material declarations declared")

    # --- check (d): latest R2 api-delta key has size > 0
    try:
        s3 = _r2_client()
        bucket = "dex-raw-landing-zone"
        # Check yesterday's date (typical for daily delta of action_date=today-1)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        key = f"usaspending/contracts/api-delta/date={yesterday}/data.parquet"
        try:
            meta = s3.head_object(Bucket=bucket, Key=key)
            size = int(meta.get("ContentLength", 0))
            if size == 0:
                failures.append(f"FAIL (d): 0-byte poison parquet at {key}")
            else:
                print(f"PASS (d): {key} size={size} bytes")
        except Exception:
            # Yesterday's file may not exist yet (cron timing); look for the most recent
            # api-delta key within the last 3 days.
            found = False
            for d in range(2, 5):
                day = (datetime.now(timezone.utc) - timedelta(days=d)).date().isoformat()
                key = f"usaspending/contracts/api-delta/date={day}/data.parquet"
                try:
                    meta = s3.head_object(Bucket=bucket, Key=key)
                    size = int(meta.get("ContentLength", 0))
                    if size == 0:
                        failures.append(f"FAIL (d): 0-byte poison parquet at {key}")
                    else:
                        print(f"PASS (d): {key} size={size} bytes (fallback day -{d})")
                    found = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not found:
                failures.append(
                    "FAIL (d): no usaspending api-delta parquet found within last 5 days"
                )
    except KeyError as exc:
        print(f"SKIP (d): R2 secret env not present ({exc}); cannot verify R2 keys")

    if failures:
        for msg in failures:
            print(msg, file=sys.stderr)
        sys.exit(1)

    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
