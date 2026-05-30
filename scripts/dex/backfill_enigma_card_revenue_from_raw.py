"""Phase 2.1 backfill — replay persist_card_revenue_payload from a raw payload
on disk WITHOUT spending any Enigma credits.

Reads the source log row from entities.enigma_enrichment_log, resolves its
raw_payload_ref to a local file, reloads the JSON, and calls
persist_card_revenue_payload against the Phase 2.1 (migration 078) schema —
re-persisting every edge that was silently overwritten by the Phase 2 4-column
UNIQUE conflict target.

Refuses to run if:
  * log row status != 'success'
  * raw_payload_ref is missing or not a file:// reference
  * raw payload file does not exist on disk

Does NOT import httpx or call the Enigma API. Does NOT mutate the log row
(persist_card_revenue_payload only late-binds target_brand_id / target_ol_id
when NULL, which is already populated for the seed McDonald's log).

Usage:
    doppler run --project data-engine-x-api --config prd -- \
        python3 scripts/backfill_enigma_card_revenue_from_raw.py \
        --log-id d79bf796-97fa-44e0-8dfc-7699848197f1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from psycopg_pool import ConnectionPool  # noqa: E402

from app.services.enigma_persistence import persist_card_revenue_payload  # noqa: E402

DEFAULT_LOG_ID = "d79bf796-97fa-44e0-8dfc-7699848197f1"


def _resolve_payload_path(raw_payload_ref: str) -> Path:
    """Resolve a `file://...` ref to an absolute Path inside the repo tree."""
    if not raw_payload_ref:
        raise SystemExit("log row has NULL raw_payload_ref; nothing to backfill")
    if not raw_payload_ref.startswith("file://"):
        raise SystemExit(
            f"raw_payload_ref is not a file:// URI: {raw_payload_ref!r}"
        )
    rel = raw_payload_ref[len("file://"):]
    # Historical refs were written as relative (tmp/enigma_raw_payloads/<id>.json).
    candidate = (REPO_ROOT / rel).resolve()
    if not candidate.exists():
        # Fallback — absolute path case.
        abs_candidate = Path(rel).resolve()
        if abs_candidate.exists():
            return abs_candidate
        raise SystemExit(
            f"raw payload file not found: {candidate} (from ref {raw_payload_ref!r})"
        )
    return candidate


def _fetch_log_row(pool: ConnectionPool, log_id: uuid.UUID) -> dict[str, object]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, tier_reached, raw_payload_ref,
                   graphql_operation_name, target_brand_id
              FROM entities.enigma_enrichment_log
             WHERE id = %s
            """,
            (log_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise SystemExit(f"no enigma_enrichment_log row with id={log_id}")
    return {
        "id": row[0],
        "status": row[1],
        "tier_reached": row[2],
        "raw_payload_ref": row[3],
        "graphql_operation_name": row[4],
        "target_brand_id": row[5],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-id",
        type=str,
        default=DEFAULT_LOG_ID,
        help=f"enigma_enrichment_log.id (default: {DEFAULT_LOG_ID})",
    )
    args = parser.parse_args()

    try:
        log_id = uuid.UUID(args.log_id)
    except ValueError:
        raise SystemExit(f"--log-id is not a UUID: {args.log_id!r}")

    database_url = os.getenv("DEX_DB_URL_POOLED")
    if not database_url:
        raise SystemExit(
            "DEX_DB_URL_POOLED not set; run under `doppler run ... -- python3 ...`"
        )

    pool = ConnectionPool(
        conninfo=database_url, min_size=1, max_size=2, timeout=30.0, open=True
    )
    try:
        row = _fetch_log_row(pool, log_id)
        print(
            f"[backfill] log_id={row['id']} status={row['status']!r} "
            f"tier_reached={row['tier_reached']} "
            f"op={row['graphql_operation_name']!r} "
            f"raw_payload_ref={row['raw_payload_ref']!r} "
            f"target_brand_id={row['target_brand_id']}"
        )
        if row["status"] != "success":
            raise SystemExit(
                f"refusing to backfill — log status is {row['status']!r} (expected 'success')"
            )

        payload_path = _resolve_payload_path(str(row["raw_payload_ref"]))
        print(f"[backfill] loading raw payload from {payload_path}")
        payload = json.loads(payload_path.read_text())

        # The adapter persists only the `data` envelope to disk, but defensively
        # unwrap if the file was stored with the outer `{data: {...}}` shape.
        if isinstance(payload, dict) and "data" in payload and "search" not in payload:
            payload = payload["data"]

        source_op = str(row["graphql_operation_name"]) + ".backfill_2_1"
        print(
            f"[backfill] calling persist_card_revenue_payload with tier={row['tier_reached']} "
            f"source_operation_id={source_op!r}"
        )
        result = persist_card_revenue_payload(
            pool,
            payload,
            source_log_id=log_id,
            tier=int(row["tier_reached"]),
            source_operation_id=source_op,
        )
        print(
            f"[backfill] DONE: subject_kind={result.subject_kind} "
            f"subject_internal_id={result.subject_internal_id} "
            f"subject_enigma_uuid={result.subject_enigma_uuid} "
            f"upserted_row_count={result.upserted_row_count} "
            f"skipped_row_count={result.skipped_row_count} "
            f"truncation_warning={result.truncation_warning}"
        )
    finally:
        pool.close()


if __name__ == "__main__":
    main()
