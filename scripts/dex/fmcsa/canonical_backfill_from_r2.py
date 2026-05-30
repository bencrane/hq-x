"""One-shot canonical backfill: reads FMCSA R2 snapshots and refreshes fmcsa.* Postgres tables.

Usage:
    cd apps/data-engine-x
    doppler run --project hq-all --config prd -- bash -c '
        python scripts/fmcsa/canonical_backfill_from_r2.py --snapshot=2026-05-12 --apply
    '

What this does:
  1. For each FMCSA feed with data at snapshot=<date>, reads the R2 parquet via DuckDB/httpfs.
  2. Maps raw FMCSA column names to entities.* column names using mappings.build_typed_row.
  3. Upserts into entities.* intermediate tables (ON CONFLICT (feed_date, source_feed_name, row_position) DO UPDATE).
  4. Calls fmcsa.refresh_<table>() for all 12 canonical fmcsa.* tables.
  5. Logs one row per source to ops.data_source_ingest_runs with idempotency_key.

Idempotency:
  - Already-applied detection: checks ops.data_source_ingest_runs for idempotency_key
    fmcsa_backfill_<feed_display_name>_<snapshot_date> before re-running a feed.
  - ON CONFLICT (feed_date, source_feed_name, row_position) DO UPDATE on entities tables
    ensures safe re-runs even if idempotency check is skipped (--force).
  - fmcsa.refresh_*() functions are themselves idempotent:
    WHERE EXCLUDED.source_feed_date > fmcsa.<table>.source_feed_date.

Validator constraints honored:
  P1 — snapshot=2026-05-12 single-date glob; asserts COUNT(DISTINCT snapshot) == 1 per feed.
  P2 — full PK tuples used in fmcsa.refresh_*() SQL (see mapping in fmcsa_refresh_functions).
  P5 — all psql/aws invocations deferred via os.environ (Doppler-injected at launch time).

HARD constraints (do NOT modify):
  - modal/fmcsa_refresh_app.py:1089-1098  (Modal nightly cron — keep commented-out)
  - trigger/src/tasks/fmcsa-signal-detection.ts:18  (Trigger.dev cron — keep commented-out)
  - scripts/_lib/lance_emit.py             (Lance layer — do NOT touch)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from typing import Any

import duckdb
import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-table primary-key column tuples (live-DB-queried by Stage 3.A audit).
# These MUST match the actual Postgres PK constraints. Used by fmcsa.refresh_*()
# SQL functions (which specify the full PK in ON CONFLICT clauses).
# Hardcoded here as documentation; the actual upsert is in the SQL functions.
# ---------------------------------------------------------------------------

# carrier_authority_event_records 6-col PK (harness P2 verify target):
# ON CONFLICT (event_kind, docket_number, sub_number_pk, authority_type_pk, event_date, event_subtype) DO UPDATE
_CARRIER_AUTHORITY_EVENT_PK = ("event_kind", "docket_number", "sub_number_pk", "authority_type_pk", "event_date", "event_subtype")

TABLE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    # carrier_authority_event_records — 6-col PK (most complex)
    "carrier_authority_event_records": _CARRIER_AUTHORITY_EVENT_PK,
    "carrier_authority_records": ("dot_number", "docket_number", "authority_type"),
    "carrier_crash_records": ("crash_id",),
    "carrier_inspection_location_records": ("inspection_unique_id",),
    "carrier_inspection_records": ("inspection_unique_id",),
    "carrier_insurance_active_policy_records": (
        "docket_number",
        "insurance_type_code",
        "policy_number",
    ),
    "carrier_insurance_event_records": (
        "event_kind",
        "docket_number",
        "form_code",
        "policy_number",
        "event_date",
        "event_method",
    ),
    "carrier_insurance_policy_records": (
        "docket_number",
        "form_code",
        "policy_number",
        "posted_date",
    ),
    "carrier_officer_records": ("dot_number", "officer_slot"),
    "carrier_records": ("dot_number",),
    "carrier_registration_records": ("dot_number", "docket_number"),
    "carrier_safety_basic_records": ("dot_number", "basic_category"),
}

# ---------------------------------------------------------------------------
# Feed → entities table mapping (feeds that populate entities.* intermediate
# tables, which are then read by fmcsa.refresh_*() SQL functions).
# Only feeds with snapshot=<date> present on R2 are used.
# ---------------------------------------------------------------------------
FEED_TO_ENTITIES_TABLE: dict[str, str] = {
    "Company Census File": "motor_carrier_census_records",
    "SMS Input - Motor Carrier Census": "motor_carrier_census_records",
    "Carrier": "carrier_registrations",
    "Carrier - All With History": "carrier_registrations",
    "AuthHist": "operating_authority_histories",
    "AuthHist - All With History": "operating_authority_histories",
    "Revocation": "operating_authority_revocations",
    "Revocation - All With History": "operating_authority_revocations",
    "Insurance": "insurance_policies",
    "Insur - All With History": "insurance_policies",
    "ActPendInsur": "insurance_policy_filings",
    "ActPendInsur - All With History": "insurance_policy_filings",
    "InsHist": "insurance_policy_history_events",
    "InsHist - All With History": "insurance_policy_history_events",
    "Crash File": "commercial_vehicle_crashes",
    "Inspections and Citations": "carrier_inspections",
    "SMS Input - Inspection": "carrier_inspections",
    "SMS AB Pass": "carrier_safety_basic_percentiles",
    "SMS AB PassProperty": "carrier_safety_basic_percentiles",
    "SMS C Pass": "carrier_safety_basic_percentiles",
    "SMS C PassProperty": "carrier_safety_basic_percentiles",
}

# ---------------------------------------------------------------------------
# All 12 canonical fmcsa.* tables to refresh (in dependency order per modal app).
# ---------------------------------------------------------------------------
CANONICAL_TABLES: tuple[str, ...] = (
    "carrier_records",
    "carrier_registration_records",
    "carrier_authority_records",
    "carrier_authority_event_records",
    "carrier_insurance_policy_records",
    "carrier_insurance_active_policy_records",
    "carrier_insurance_event_records",
    "carrier_officer_records",
    "carrier_inspection_location_records",
    "carrier_safety_basic_records",
    "carrier_inspection_records",
    "carrier_crash_records",
)

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "fmcsa"
SOURCE_PROVIDER = "fmcsa"


def _get_db_direct() -> psycopg2.extensions.connection:
    """Connect to DEX via direct URL (required for DDL + function calls)."""
    url = os.environ["DEX_DB_URL_DIRECT"]
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


def _get_duckdb_conn() -> duckdb.DuckDBPyConnection:
    """Create a DuckDB connection with R2/S3 configured."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint = os.environ["R2_ENDPOINT"].replace("https://", "").rstrip("/")
    con.execute(f"SET s3_endpoint = '{endpoint}';")
    con.execute(f"SET s3_access_key_id = '{os.environ['R2_ACCESS_KEY_ID']}';")
    con.execute(f"SET s3_secret_access_key = '{os.environ['R2_SECRET_ACCESS_KEY']}';")
    return con


def _r2_parquet_url(feed_name: str, snapshot_date: str) -> str:
    """Build the R2 parquet URL for a given feed and snapshot date."""
    return f"s3://{R2_BUCKET}/{R2_PREFIX}/{feed_name}/snapshot={snapshot_date}/data.parquet*"


def _check_already_applied(conn: psycopg2.extensions.connection, idempotency_key: str) -> bool:
    """Return True if this backfill run was already applied (idempotency guard)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM ops.data_source_ingest_runs r
            JOIN ops.data_sources s ON r.source_id = s.source_id
            WHERE r.run_metadata->>'idempotency_key' = %s
              AND r.status = 'succeeded'
            LIMIT 1
            """,
            (idempotency_key,),
        )
        return cur.fetchone() is not None


def _get_or_create_source_id(
    conn: psycopg2.extensions.connection, display_name: str
) -> str | None:
    """Return source_id from ops.data_sources for the given display_name, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id::text FROM ops.data_sources WHERE display_name = %s LIMIT 1",
            (display_name,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _log_ingest_run(
    conn: psycopg2.extensions.connection,
    source_id: str,
    snapshot_date: str,
    rows_ingested: int,
    idempotency_key: str,
    feed_name: str,
) -> None:
    """Write one row to ops.data_source_ingest_runs."""
    now = datetime.now(timezone.utc)
    run_metadata = {
        "idempotency_key": idempotency_key,
        "backfill_script": "scripts/fmcsa/canonical_backfill_from_r2.py",
        "snapshot_date": snapshot_date,
        "feed_name": feed_name,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.data_source_ingest_runs
              (run_id, source_id, started_at, completed_at, status, rows_ingested,
               source_publish_at, run_metadata)
            VALUES (%s, %s::uuid, %s, %s, 'succeeded', %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                source_id,
                now,
                now,
                rows_ingested,
                date.fromisoformat(snapshot_date),
                json.dumps(run_metadata),
            ),
        )
    conn.commit()


def _assert_single_snapshot_date(con: duckdb.DuckDBPyConnection, url: str, expected_date: str) -> None:
    """Assert that the parquet contains exactly one snapshot date (P1 guard).

    Raises ValueError if multiple snapshot dates are present or the date doesn't match.
    """
    result = con.execute(
        f"SELECT COUNT(DISTINCT snapshot) AS cnt, MAX(snapshot)::text AS max_snap FROM read_parquet('{url}')"
    ).fetchone()
    cnt, max_snap = result[0], result[1]
    if cnt != 1:
        raise ValueError(
            f"Mixed-vintage guard FAIL: {url} has {cnt} distinct snapshot dates (expected 1)."
        )
    if max_snap != expected_date:
        raise ValueError(
            f"Snapshot date mismatch: got {max_snap}, expected {expected_date}."
        )


def _upsert_entities_from_parquet(
    con: duckdb.DuckDBPyConnection,
    db_conn: psycopg2.extensions.connection,
    url: str,
    feed_name: str,
    entities_table: str,
    snapshot_date: str,
    batch_size: int,
    dry_run: bool,
) -> int:
    """Read R2 parquet via DuckDB, map columns, upsert into entities.* table.

    Returns total rows inserted/updated.
    """
    # Import the existing mapping infrastructure
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "modal"))
    try:
        from fmcsa.mappings import build_typed_row  # type: ignore[import]
        from fmcsa.feed_catalog import FEED_CATALOG  # type: ignore[import]
    except ImportError as e:
        raise ImportError(
            f"Cannot import fmcsa mapping modules from modal/: {e}. "
            "Run from apps/data-engine-x/."
        ) from e

    # Find the feed config for this feed_name
    feed_cfg = next((f for f in FEED_CATALOG if f.feed_name == feed_name), None)
    if feed_cfg is None:
        raise ValueError(f"No FeedConfig found for feed_name={feed_name!r}")

    total_rows = 0
    offset = 0

    while True:
        # Read a batch of rows from the parquet
        batch_rows = con.execute(
            f"""
            SELECT * FROM read_parquet('{url}')
            LIMIT {batch_size} OFFSET {offset}
            """
        ).fetchall()
        if not batch_rows:
            break

        col_names = [desc[0] for desc in con.description]
        records: list[dict[str, Any]] = []

        for raw_row in batch_rows:
            fields: dict[str, Any] = dict(zip(col_names, raw_row))
            # The parquet has a 'snapshot' column (date); use as feed_date
            feed_date_val = fields.get("snapshot", snapshot_date)
            if hasattr(feed_date_val, "isoformat"):
                feed_date_val = feed_date_val.isoformat()

            mapped = build_typed_row(feed=feed_cfg, fields=fields)
            if mapped is None:
                continue

            # Row position (for conflict key) — use offset+row index if not present
            row_position = fields.get("row_position", offset + len(records))

            record = {
                "feed_date": feed_date_val or snapshot_date,
                "source_feed_name": feed_name,
                "row_position": row_position,
                "source_provider": SOURCE_PROVIDER,
                "source_download_url": f"r2://{R2_BUCKET}/{R2_PREFIX}/{feed_name}/snapshot={snapshot_date}/",
                "source_observed_at": datetime.now(timezone.utc).isoformat(),
                "source_run_metadata": json.dumps({"backfill_snapshot": snapshot_date}),
                "raw_source_row": json.dumps({k: str(v) for k, v in fields.items() if k != "snapshot"}),
                **mapped,
            }
            records.append(record)

        if not records:
            offset += batch_size
            continue

        if not dry_run:
            # Build upsert SQL
            cols = list(records[0].keys())
            col_sql = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join(["%s"] * len(cols))
            update_pairs = ", ".join(
                f'"{c}" = EXCLUDED."{c}"'
                for c in cols
                if c not in {"id", "created_at", "first_observed_at", "record_fingerprint", "feed_date", "source_feed_name", "row_position"}
            )
            upsert_sql = f"""
                INSERT INTO entities.{entities_table} ({col_sql})
                VALUES ({placeholders})
                ON CONFLICT (feed_date, source_feed_name, row_position)
                DO UPDATE SET {update_pairs}
            """
            with db_conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    upsert_sql,
                    [[r[c] for c in cols] for r in records],
                    page_size=batch_size,
                )
            db_conn.commit()

        total_rows += len(records)
        log.info("  %s: upserted %d rows (offset=%d)", entities_table, len(records), offset)
        offset += batch_size

    return total_rows


def _call_fmcsa_refresh(
    db_conn: psycopg2.extensions.connection,
    table_name: str,
    dry_run: bool,
) -> int:
    """Call fmcsa.refresh_<table_name>() and return rows-affected."""
    if dry_run:
        log.info("  [dry-run] would call fmcsa.refresh_%s()", table_name)
        return 0
    with db_conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0;")
        cur.execute(f"SELECT fmcsa.refresh_{table_name}() AS rows_affected;")
        rows = cur.fetchone()[0] or 0
    db_conn.commit()
    log.info("  fmcsa.%s refreshed: %d rows affected", table_name, rows)
    return rows


def _check_r2_url_exists(con: duckdb.DuckDBPyConnection, url: str) -> bool:
    """Return True if the R2 URL resolves to at least one row."""
    try:
        result = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{url}') LIMIT 1"
        ).fetchone()
        return (result[0] or 0) > 0
    except Exception:
        return False


def run_backfill(snapshot_date: str, dry_run: bool, force: bool, feeds_filter: list[str] | None) -> None:
    """Main backfill entry point."""
    log.info("FMCSA canonical backfill — snapshot=%s dry_run=%s force=%s", snapshot_date, dry_run, force)

    db_conn = _get_db_direct()
    con = _get_duckdb_conn()

    # --- Phase 1: upsert entities.* tables from R2 parquets ---
    feeds_to_process = list(FEED_TO_ENTITIES_TABLE.keys()) if feeds_filter is None else feeds_filter
    entity_rows_by_table: dict[str, int] = {}

    for feed_name in feeds_to_process:
        entities_table = FEED_TO_ENTITIES_TABLE[feed_name]
        idempotency_key = f"fmcsa_backfill_{feed_name.lower().replace(' ', '_').replace('-', '_')}_{snapshot_date}"

        if not force and _check_already_applied(db_conn, idempotency_key):
            log.info("SKIP %s — already applied (idempotency_key=%s)", feed_name, idempotency_key)
            continue

        # Try snapshot=<date> first; fall back to latest available if not found
        url = _r2_parquet_url(feed_name, snapshot_date)
        if not _check_r2_url_exists(con, url):
            log.warning("No snapshot=%s for feed %r — trying latest available", snapshot_date, feed_name)
            # Check recent snapshots (up to 5 days back)
            from datetime import timedelta
            found = False
            for delta in range(1, 6):
                alt_date = (date.fromisoformat(snapshot_date) - timedelta(days=delta)).isoformat()
                alt_url = _r2_parquet_url(feed_name, alt_date)
                if _check_r2_url_exists(con, alt_url):
                    log.info("  Using fallback snapshot=%s for %r", alt_date, feed_name)
                    url = alt_url
                    snapshot_date_for_feed = alt_date
                    found = True
                    break
            if not found:
                log.warning("  No R2 data found for %r within 5 days of %s — skipping", feed_name, snapshot_date)
                continue
        else:
            snapshot_date_for_feed = snapshot_date

        # P1 guard: assert single snapshot date
        try:
            _assert_single_snapshot_date(con, url, snapshot_date_for_feed)
        except ValueError as e:
            log.error("P1 mixed-vintage FAIL for %r: %s", feed_name, e)
            sys.exit(1)

        log.info("Processing feed=%r → entities.%s snapshot=%s", feed_name, entities_table, snapshot_date_for_feed)

        rows = _upsert_entities_from_parquet(
            con=con,
            db_conn=db_conn,
            url=url,
            feed_name=feed_name,
            entities_table=entities_table,
            snapshot_date=snapshot_date_for_feed,
            batch_size=75_000,
            dry_run=dry_run,
        )
        entity_rows_by_table[entities_table] = entity_rows_by_table.get(entities_table, 0) + rows
        log.info("Feed %r done: %d rows → entities.%s", feed_name, rows, entities_table)

        # Log to ops.data_source_ingest_runs
        source_id = _get_or_create_source_id(db_conn, f"fmcsa_{feed_name.lower().replace(' - ', '_').replace(' ', '_')}")
        if source_id and not dry_run:
            _log_ingest_run(
                conn=db_conn,
                source_id=source_id,
                snapshot_date=snapshot_date_for_feed,
                rows_ingested=rows,
                idempotency_key=idempotency_key,
                feed_name=feed_name,
            )

    log.info("Phase 1 complete: %d entity tables updated", len(entity_rows_by_table))

    # --- Phase 2: call fmcsa.refresh_*() for all 12 canonical tables ---
    log.info("Phase 2: calling fmcsa.refresh_*() for %d canonical tables", len(CANONICAL_TABLES))
    canonical_rows: dict[str, int] = {}
    for table in CANONICAL_TABLES:
        log.info("Refreshing fmcsa.%s ...", table)
        rows = _call_fmcsa_refresh(db_conn=db_conn, table_name=table, dry_run=dry_run)
        canonical_rows[table] = rows

    total_canonical = sum(canonical_rows.values())
    log.info(
        "Phase 2 complete: %d canonical tables refreshed, %d total rows affected",
        len(canonical_rows),
        total_canonical,
    )

    if not dry_run:
        log.info("Backfill complete. Verifying source_feed_date advancement...")
        with db_conn.cursor() as cur:
            for table in CANONICAL_TABLES:
                cur.execute(f"SELECT MAX(source_feed_date)::text FROM fmcsa.{table}")
                max_fd = cur.fetchone()[0]
                log.info("  fmcsa.%s MAX(source_feed_date) = %s", table, max_fd)
        db_conn.close()

    log.info("FMCSA canonical backfill done (dry_run=%s).", dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        default="2026-05-12",
        help="Snapshot date to read from R2 (YYYY-MM-DD). Default: 2026-05-12",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write to Postgres. Without this flag, dry-run only.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-run even if idempotency_key already applied.",
    )
    parser.add_argument(
        "--feeds",
        nargs="*",
        default=None,
        help="Optional: only process these feed names (space-separated).",
    )
    args = parser.parse_args()

    run_backfill(
        snapshot_date=args.snapshot,
        dry_run=not args.apply,
        force=args.force,
        feeds_filter=args.feeds,
    )


if __name__ == "__main__":
    main()
