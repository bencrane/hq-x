"""USAspending REST API daily-delta ingest — synchronous paginated path.

Closes the ~30-day staleness gap between monthly Award Data Archive drops by
querying USAspending's `/api/v2/search/spending_by_transaction/` endpoint
for contract transactions modified in the prior 24h UTC window. Lands a
single-Parquet daily snapshot at:

    s3://dex-raw-landing-zone/usaspending/contracts/api-delta/date={YYYY-MM-DD}/data.parquet

This is COMPLEMENTARY to the bulk-archive path that lives in
`run_usaspending_daily_ingest.py` (which uses the async `/bulk_download/`
queue and ships everything for an action_date window — heavier, slower,
higher fidelity). The two ingests write to disjoint R2 prefixes so they
can coexist; downstream DuckDB consumers UNION ALL across them and dedup
on (internal_id, mod) when the API-delta overlap with the next monthly
snapshot eventually lands.

Overlap pattern (out of scope for this script; dedup is downstream):
    USAspending's monthly Full archive is end-of-window inclusive. The
    api-delta of the next day after that window can show the same
    transaction again with a later last_modified_date. Downstream
    consumers handle the dedup via (generated_internal_id, mod, max(last_modified_date)).

Quota discipline (see directive §4 anti-patterns):
  - Pagination max = 100 (USAspending hard cap). Always request the max.
  - --max-api-calls hard-caps pagination. Default 500 = ~50K rows ceiling.
  - --window=1h clamps to 10 calls for dev probes (the 24h window cap
    blew quota in prior dev cycles).
  - --cache-responses + --use-cache let dev iteration tune the parser
    without re-hitting the API.
  - Exponential backoff on 429: 1s → 2s → 4s → 8s → 16s, max 5 retries.

L42 compliance: writes plain `.parquet` (R2Landing handles ContentType).
L45 compliance: each day gets a unique R2 key under `date={YYYY-MM-DD}/`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import httpx
import pyarrow as pa

USASPENDING_TRANSACTION_SEARCH_URL = (
    "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
)

PAGINATION_LIMIT = 100  # USAspending hard cap
DEFAULT_MAX_API_CALLS = 500
DEV_MAX_API_CALLS = 10  # --window=1h ceiling

# Prime contract types — mirrors run_usaspending_daily_ingest.py
PRIME_CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]

# Fields requested from spending_by_transaction. The API returns
# `internal_id` + `generated_internal_id` by default regardless.
REQUESTED_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Recipient UEI",
    "Action Date",
    "Transaction Amount",
    "Awarding Agency",
    "Mod",
    "Award Type",
    "Action Type",
    # NOTE: "last_modified_date" was removed from the spending_by_transaction
    # valid `fields` list by USAspending some time before 2026-05-13 (confirmed
    # via direct API probe). The `date_type: "last_modified_date"` filter usage
    # below is still valid — only the field-projection use was removed.
    # Downstream dedup on (generated_internal_id, mod, max(last_modified_date))
    # will need to switch to another timestamp; tracked separately.
]

# Snake_case Parquet schema. Maps API PascalCase response keys → snake_case
# columns. Aligns to USAspending CSV header conventions where possible so
# downstream UNION ALL with bulk-archive Parquet stays clean.
COLUMN_MAPPING: list[tuple[str, str, str]] = [
    # (api_response_key, parquet_column, type_hint)
    ("internal_id", "internal_id", "int"),
    ("generated_internal_id", "generated_internal_id", "string"),
    ("Award ID", "award_id", "string"),
    ("Recipient Name", "recipient_name", "string"),
    ("Recipient UEI", "recipient_uei", "string"),
    ("Action Date", "action_date", "date"),
    ("Transaction Amount", "federal_action_obligation", "decimal"),
    ("Awarding Agency", "awarding_agency_name", "string"),
    ("Mod", "mod", "string"),
    ("Award Type", "award_type", "string"),
    ("Action Type", "action_type", "string"),
    # last_modified_date removed — see REQUESTED_FIELDS note above.
]

# Explicit Parquet schema for the api-delta writer.
#
# Root cause of the schema-drift bug (2026-05-11 + 2026-05-13 parse_failures):
# pa.RecordBatch.from_pylist(buffer) infers column dtype from the FIRST row in
# each batch. When a subsequent page has all-None values for a sparse column
# (e.g. internal_id=None for every row in the batch), pyarrow infers `null`
# type instead of `int64`. pq.ParquetWriter — opened with the first batch's
# inferred schema — then throws "Table schema does not match" on write_batch.
#
# Fix: declare PINNED_SCHEMA here and pass schema=PINNED_SCHEMA to every
# RecordBatch construction. This forces dtype to the declared type regardless
# of batch content, so null-only batches coerce cleanly to int64/string/etc.
#
# internal_id is pinned as int64 (consistent with type_hint='int' in
# COLUMN_MAPPING). The downstream DuckDB consumer (usaspending_contractor_detail)
# does not select internal_id, so the int64 vs string choice has no service-side
# impact. generated_internal_id is string (USAspending publishes it as
# "CONT_AWD_<award_id>" string keys).
#
# decimal128(28, 10) matches USAspending's published precision for obligation
# amounts. date32 matches pa.date32() — pyarrow's default for Python date objects.
PINNED_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("internal_id", pa.int64(), nullable=True),
        pa.field("generated_internal_id", pa.string(), nullable=True),
        pa.field("award_id", pa.string(), nullable=True),
        pa.field("recipient_name", pa.string(), nullable=True),
        pa.field("recipient_uei", pa.string(), nullable=True),
        pa.field("action_date", pa.date32(), nullable=True),
        pa.field("federal_action_obligation", pa.decimal128(28, 10), nullable=True),
        pa.field("awarding_agency_name", pa.string(), nullable=True),
        pa.field("mod", pa.string(), nullable=True),
        pa.field("award_type", pa.string(), nullable=True),
        pa.field("action_type", pa.string(), nullable=True),
        pa.field("target_date", pa.date32(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)


def _is_upstream_empty(body: dict | None) -> bool:
    """First-page-after-write probe: True iff results is empty AND no next page.

    USAspending's page_metadata no longer contains a `total` key (confirmed
    live 2026-05-24: keys are page/next/previous/hasNext/hasPrevious only).
    The previous implementation derived upstream_was_empty from the missing
    total field, which always evaluated to zero and therefore always reported
    upstream as empty — silently masking real-data days in the ledger.

    This function uses the presence of results AND the hasNext flag to correctly
    classify the response. When body is None (probe failed), returns True
    (treat as empty — the main query already confirmed zero rows, so the probe
    is corroborating evidence, not a gate; defaulting to True preserves the
    existing assert_landed_or_explicit_empty semantics for legitimate empty days).

    Cycle: usaspending-api-daily-app-probe-total-assertion-bug (2026-05-24)
    """
    if not body:
        return True
    results = body.get("results") or []
    if results:
        return False
    page_metadata = body.get("page_metadata") or {}
    return not page_metadata.get("hasNext")


def _coerce(value: Any, type_hint: str) -> Any:
    import decimal as _decimal

    if value is None or value == "":
        return None
    if type_hint == "string":
        return str(value)
    if type_hint == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if type_hint == "date":
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None
    if type_hint == "decimal":
        try:
            return _decimal.Decimal(str(value))
        except (_decimal.InvalidOperation, TypeError, ValueError):
            return None
    return value


def _project_row(api_row: dict[str, Any], target_date: date) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for api_key, parquet_key, type_hint in COLUMN_MAPPING:
        projected[parquet_key] = _coerce(api_row.get(api_key), type_hint)
    projected["target_date"] = target_date
    projected["ingested_at"] = datetime.now(timezone.utc)
    return projected


def _post_with_backoff(
    *,
    client: httpx.Client,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST without script-level retry — fail fast so Modal container
    spawns a fresh egress IP on the next attempt.

    Previous 5-retry exponential-backoff loop (1s→2s→4s→8s→16s, ~31s same-IP)
    was the wrong shape for USAspending's F5 BotDefense throttle: persistent
    connection drops on the IP don't recover within seconds — they require a
    fresh source IP. That fix lives on the `@app.function` decorator via
    `modal.Retries(max_retries=5, backoff_coefficient=2.0, initial_delay=30.0)`
    which gets the orchestrator a brand-new container (= new egress IP) per
    retry. See `apps/data-engine-x/modal/RETRIES.md` § Government-IP rate
    limit and the 2026-05-25 backfill post-mortem.

    Single attempt: any 4xx/5xx/HTTPError/TimeoutException raises immediately.
    The 429-specific path is preserved (one sleep + one retry) because 429s
    DO recover within seconds and don't need a fresh container.
    """
    t0 = time.time()
    response = client.post(
        USASPENDING_TRANSACTION_SEARCH_URL,
        json=payload,
        timeout=60.0,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    print(
        f"[usaspending-api] POST page={payload.get('page')} "
        f"status={response.status_code} elapsed_ms={elapsed_ms}"
    )
    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", "5"))
        print(f"[usaspending-api] 429 throttle, sleeping {retry_after}s (one-shot retry)")
        time.sleep(retry_after)
        t0 = time.time()
        response = client.post(
            USASPENDING_TRANSACTION_SEARCH_URL,
            json=payload,
            timeout=60.0,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        print(
            f"[usaspending-api] POST (retry-after) page={payload.get('page')} "
            f"status={response.status_code} elapsed_ms={elapsed_ms}"
        )
    response.raise_for_status()
    return response.json()


def _iter_pages(
    *,
    client: httpx.Client,
    target_date: date,
    max_api_calls: int,
    cache_path: str | None,
    use_cache: bool,
) -> Iterator[list[dict[str, Any]]]:
    """Yield page-result lists until hasNext=False or max_api_calls reached.

    If use_cache + cache file exists, read pages from cache instead. If
    cache_path + not use_cache, write each page to cache as JSON-lines.
    """
    if use_cache and cache_path and os.path.exists(cache_path):
        print(f"[usaspending-api] reading from cache {cache_path}")
        with open(cache_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
        return

    cache_fh = open(cache_path, "w") if (cache_path and not use_cache) else None
    try:
        # USAspending time_period is date-inclusive on both ends. For a
        # single-day window covering target_date's modifications: end = target_date,
        # start = target_date. The API treats this as the full 24h of that
        # UTC day. (Sub-day windowing is not supported — API granularity is days.)
        time_period = [
            {
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "date_type": "last_modified_date",
            }
        ]

        page = 1
        api_calls = 0
        while api_calls < max_api_calls:
            payload = {
                "filters": {
                    "award_type_codes": PRIME_CONTRACT_AWARD_TYPES,
                    "time_period": time_period,
                },
                "fields": REQUESTED_FIELDS,
                "page": page,
                "limit": PAGINATION_LIMIT,
                "sort": "Action Date",
                "order": "desc",
            }
            body = _post_with_backoff(client=client, payload=payload)
            api_calls += 1

            results = body.get("results") or []
            if cache_fh is not None:
                cache_fh.write(json.dumps(results) + "\n")

            yield results

            page_metadata = body.get("page_metadata") or {}
            if not page_metadata.get("hasNext"):
                print(f"[usaspending-api] hasNext=False at page={page}")
                return
            page += 1

        print(
            f"[usaspending-api] max_api_calls={max_api_calls} reached; "
            f"stopping pagination at page={page - 1}"
        )
    finally:
        if cache_fh is not None:
            cache_fh.close()


def _iter_record_batches(
    *,
    client: httpx.Client,
    target_date: date,
    max_api_calls: int,
    cache_path: str | None,
    use_cache: bool,
    chunk_size: int = 1_000,
) -> Iterator[Any]:
    buffer: list[dict[str, Any]] = []
    for page_results in _iter_pages(
        client=client,
        target_date=target_date,
        max_api_calls=max_api_calls,
        cache_path=cache_path,
        use_cache=use_cache,
    ):
        for row in page_results:
            buffer.append(_project_row(row, target_date))
            if len(buffer) >= chunk_size:
                # Pass PINNED_SCHEMA so each batch is forced to the declared
                # dtypes regardless of row content (fixes all-null-batch drift).
                yield pa.RecordBatch.from_pylist(buffer, schema=PINNED_SCHEMA)
                buffer = []
    if buffer:
        yield pa.RecordBatch.from_pylist(buffer, schema=PINNED_SCHEMA)


def _r2_key_exists(*, bucket: str, key: str) -> bool:
    """HEAD the R2 key to enforce per-date idempotency.

    Returns True ONLY when a non-empty object exists (ContentLength > 0).
    A 0-byte object is treated as nonexistent — it is a poison-file residue
    from the pre-fix writer bug (see modal/landing/r2.py) and must not
    block reruns. Defense-in-depth pair with the write_streaming_to_key fix.

    Uses scripts._lib.r2_keys.r2_object_is_landed for centralized behavior.
    """
    import boto3
    from scripts._lib.r2_keys import r2_object_is_landed

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    return r2_object_is_landed(client, bucket=bucket, key=key)


def run_ingest(
    *,
    feed_date: date,
    run_id: str,
    r2_object_key: str,
    max_api_calls: int = DEFAULT_MAX_API_CALLS,
    cache_path: str | None = None,
    use_cache: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """API-daily-delta entry. Called by usaspending_api_daily_app.py.

    Returns evidence dict: rows_loaded, payload_bytes, api_calls,
    r2_object_key, skipped_existing, dry_run.
    """
    from landing.r2 import R2Landing

    bucket = "dex-raw-landing-zone"
    print(
        f"[usaspending-api] start feed_date={feed_date} run_id={run_id} "
        f"max_api_calls={max_api_calls} dry_run={dry_run}"
    )

    if not dry_run and _r2_key_exists(bucket=bucket, key=r2_object_key):
        print(f"[usaspending-api] r2_key already exists, skipping: {r2_object_key}")
        return {
            "rows_loaded": 0,
            "payload_bytes": 0,
            "api_calls": 0,
            "r2_object_key": r2_object_key,
            "skipped_existing": True,
            "dry_run": dry_run,
        }

    with httpx.Client(headers={"User-Agent": "data-engine-x/1.0"}) as client:
        if dry_run:
            row_count = 0
            api_calls = 0
            for page_results in _iter_pages(
                client=client,
                target_date=feed_date,
                max_api_calls=max_api_calls,
                cache_path=cache_path,
                use_cache=use_cache,
            ):
                row_count += len(page_results)
                api_calls += 1
            print(f"[usaspending-api] dry_run rows={row_count} api_calls={api_calls}")
            return {
                "rows_loaded": row_count,
                "payload_bytes": 0,
                "api_calls": api_calls,
                "r2_object_key": None,
                "skipped_existing": False,
                "dry_run": True,
            }

        r2 = R2Landing(bucket=bucket)
        batches = _iter_record_batches(
            client=client,
            target_date=feed_date,
            max_api_calls=max_api_calls,
            cache_path=cache_path,
            use_cache=use_cache,
        )
        landing_result = r2.write_streaming_to_key(
            key=r2_object_key,
            batches=batches,
            pinned_schema=PINNED_SCHEMA,
        )

        # s3 wire-in: assert that zero rows is legitimate (upstream confirmed empty)
        # rather than a silent write failure. The post-s1 writer refuses to upload
        # a 0-byte artifact, so zero rows here means the API returned no results.
        # We probe the first page to get the API's total_count as proof.
        if landing_result.rows_loaded == 0:
            import hashlib

            # Canonical schema (docs/usaspending-api-canonical-schemas.md §1b):
            # `sort` MUST reference a name present in `fields` — otherwise the
            # API returns `400 {"detail":"Sort value not found in fields: …"}`.
            # The prior `fields=["Award ID"]` + `sort="Action Date"` combo
            # tripped this deterministically; include "Action Date" in fields.
            probe_payload = {
                "filters": {
                    "award_type_codes": PRIME_CONTRACT_AWARD_TYPES,
                    "time_period": [
                        {
                            "start_date": feed_date.isoformat(),
                            "end_date": feed_date.isoformat(),
                            "date_type": "last_modified_date",
                        }
                    ],
                },
                "fields": ["Award ID", "Action Date"],
                "sort": "Action Date",
                "order": "desc",
                "page": 1,
                "limit": 1,
            }
            import datetime as _dt
            from landing.safety import assert_landed_or_explicit_empty

            # Probe is corroborating, not gating: if it still errors for any
            # reason, fall back to the main query's zero-result evidence
            # (REQUESTED_FIELDS, full pagination) which already confirmed empty.
            probe_body: dict | None = None
            probe_error: str | None = None
            try:
                probe_body = _post_with_backoff(client=client, payload=probe_payload)
            except RuntimeError as exc:
                probe_error = str(exc)
                print(
                    f"[usaspending-api] count-probe failed for {feed_date}: {exc}; "
                    f"treating as empty (main query returned 0 rows)"
                )
                # probe_body stays None — _is_upstream_empty(None) returns True

            upstream_was_empty = _is_upstream_empty(probe_body)
            probe_results_count = len((probe_body or {}).get("results") or []) if probe_body is not None else None
            probe_has_next = bool(((probe_body or {}).get("page_metadata") or {}).get("hasNext")) if probe_body is not None else None

            proof_params_hash = hashlib.sha256(
                json.dumps(
                    {
                        "feed_date": feed_date.isoformat(),
                        "award_type_codes": PRIME_CONTRACT_AWARD_TYPES,
                        "date_type": "last_modified_date",
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()

            assert_landed_or_explicit_empty(
                rows_loaded=landing_result.rows_loaded,
                upstream_was_empty=upstream_was_empty,
                upstream_proof={
                    "source": "usaspending-api-v2-spending-by-transaction",
                    "endpoint": USASPENDING_TRANSACTION_SEARCH_URL,
                    "request_params_hash": proof_params_hash,
                    "api_results_count": probe_results_count,
                    "api_has_next": probe_has_next,
                    "checked_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "feed_date": feed_date.isoformat(),
                    "note": (
                        f"First-page probe: results_count={probe_results_count} "
                        f"has_next={probe_has_next} for "
                        f"feed_date={feed_date.isoformat()}"
                        + (f"; probe_error={probe_error}" if probe_error else "")
                    ),
                },
            )
            print(
                f"[usaspending-api] upstream confirmed empty: "
                f"api_results_count={probe_results_count} "
                f"api_has_next={probe_has_next} feed_date={feed_date}"
            )

    print(
        f"[usaspending-api] r2 wrote rows={landing_result.rows_loaded} "
        f"bytes={landing_result.payload_bytes} key={landing_result.r2_object_key}"
    )
    return {
        "rows_loaded": landing_result.rows_loaded,
        "payload_bytes": landing_result.payload_bytes,
        "api_calls": None,  # streaming path does not expose page count
        "r2_object_key": landing_result.r2_object_key,
        "skipped_existing": False,
        "dry_run": False,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="UTC date to query (defaults to yesterday UTC)",
    )
    parser.add_argument(
        "--window",
        choices=("1h", "24h"),
        default="24h",
        help="Quota guardrail: '1h' caps api-calls at 10 for dev probes",
    )
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=None,
        help=f"Hard cap on pages fetched (default {DEFAULT_MAX_API_CALLS} for 24h)",
    )
    parser.add_argument("--cache-responses", action="store_true")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    import uuid as _uuid

    args = _parse_args(sys.argv[1:])
    feed_date = args.target_date or (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).date()

    if args.max_api_calls is not None:
        max_calls = args.max_api_calls
    else:
        max_calls = DEV_MAX_API_CALLS if args.window == "1h" else DEFAULT_MAX_API_CALLS

    cache_path = None
    if args.cache_responses or args.use_cache:
        cache_path = f"/tmp/usa_api_cache_{feed_date.isoformat()}_{args.window}.json"

    run_id = str(_uuid.uuid4())
    r2_key = (
        f"usaspending/contracts/api-delta/date={feed_date.isoformat()}/data.parquet"
    )

    # Local CLI invocations need landing.r2 importable.
    sys.path.insert(
        0,
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "modal"),
    )

    result = run_ingest(
        feed_date=feed_date,
        run_id=run_id,
        r2_object_key=r2_key,
        max_api_calls=max_calls,
        cache_path=cache_path,
        use_cache=args.use_cache,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, default=str, indent=2))
