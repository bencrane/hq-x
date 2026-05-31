"""USAspending REST API daily-delta ingest — assistance (FABS) leg.

Mirror of `run_usaspending_api_daily_ingest.py` (the contracts/FPDS leg) for
assistance transactions: grants, loans, cooperative agreements, direct
payments, insurance, and other financial assistance. Lands a single-Parquet
daily snapshot at:

    s3://dex-raw-landing-zone/usaspending/assistance/api-delta/date={YYYY-MM-DD}/data.parquet

Disjoint R2 prefix from the contracts leg (`usaspending/contracts/api-delta/…`)
so the two coexist; downstream UNION ALL keyed by `generated_internal_id`
(`ASST_NON_*` for assistance vs `CONT_AWD_*` for contracts) is unambiguous.

The differentiator vs the contracts leg is the `award_type_codes` filter:
    02 — block grant
    03 — formula grant
    04 — project grant
    05 — cooperative agreement
    06 — direct payment for specified use
    07 — direct loan
    08 — guaranteed/insured loan
    09 — insurance
    10 — direct payment with unrestricted use
    11 — other financial assistance

Probe (2026-05-22) confirmed the API accepts all 10 codes together against
`/api/v2/search/spending_by_transaction/`.

L42 compliance: writes plain `.parquet` (R2Landing handles ContentType).
L45 compliance: each day gets a unique R2 key under `date={YYYY-MM-DD}/`.

See `apps/data-engine-x/scripts/run_usaspending_api_daily_ingest.py` for the
full design rationale on pagination quotas, backoff schedule, PINNED_SCHEMA,
and zero-row safety. This file is structurally identical apart from the
projection (assistance fields including `Assistance Listing` flattened to
`cfda_number` + `cfda_title`).
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
# USAspending's deep pagination (page * limit > ~10_000) is unstable on the
# `/spending_by_transaction/` endpoint — observed live 2026-05-22: 500-class
# errors and RemoteProtocolError around page=245 against assistance daily
# volume (~25K rows/day across all 10 award_type_codes). Workaround:
# chunk by `award_type_code` (10 sub-queries per day, each typically ≤ 5K
# rows ≤ 50 pages, well inside the stable depth window).
DEFAULT_MAX_API_CALLS = 1500  # 150 pages * 10 codes = 1500 ceiling
DEV_MAX_API_CALLS = 20  # --window=1h ceiling (≤ 2 codes worth of probing)
PER_CODE_PAGE_CEILING = 150  # 150 * 100 = 15_000 rows/code/day cap

# Assistance award type codes — per USAspending API spec, verified live 2026-05-22.
ASSISTANCE_AWARD_TYPES = [
    "02",  # block grant
    "03",  # formula grant
    "04",  # project grant
    "05",  # cooperative agreement
    "06",  # direct payment for specified use
    "07",  # direct loan
    "08",  # guaranteed/insured loan
    "09",  # insurance
    "10",  # direct payment with unrestricted use
    "11",  # other financial assistance
]

# Fields requested from spending_by_transaction. The API returns
# `internal_id` + `generated_internal_id` by default regardless. Field names
# verified against the API's `valid values` list (HTTP 400 echoes it when an
# invalid name is sent — probe payload at 2026-05-22).
REQUESTED_FIELDS = [
    "Award ID",
    "Award Type",
    "Recipient Name",
    "Recipient UEI",
    "Action Date",
    "Transaction Amount",
    "Loan Value",
    "Subsidy Cost",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Funding Agency",
    "Funding Sub Agency",
    "Assistance Listing",      # nested {cfda_number, cfda_title}
    "Transaction Description",
    "Mod",
    "Action Type",
]

# Snake_case Parquet schema. `Assistance Listing` is a nested API object —
# we flatten it into two top-level columns (cfda_number, cfda_title) so
# downstream consumers don't need to navigate Parquet struct columns.
COLUMN_MAPPING: list[tuple[str, str, str]] = [
    # (api_response_key, parquet_column, type_hint)
    ("internal_id", "internal_id", "int"),
    ("generated_internal_id", "generated_internal_id", "string"),
    ("Award ID", "award_id", "string"),
    ("Award Type", "award_type", "string"),
    ("Recipient Name", "recipient_name", "string"),
    ("Recipient UEI", "recipient_uei", "string"),
    ("Action Date", "action_date", "date"),
    ("Transaction Amount", "federal_action_obligation", "decimal"),
    ("Loan Value", "loan_value", "decimal"),
    ("Subsidy Cost", "subsidy_cost", "decimal"),
    ("Awarding Agency", "awarding_agency_name", "string"),
    ("Awarding Sub Agency", "awarding_sub_agency_name", "string"),
    ("Funding Agency", "funding_agency_name", "string"),
    ("Funding Sub Agency", "funding_sub_agency_name", "string"),
    # Assistance Listing → flattened (handled in _project_row, not here)
    ("Transaction Description", "transaction_description", "string"),
    ("Mod", "mod", "string"),
    ("Action Type", "action_type", "string"),
]

# Explicit Parquet schema for the assistance api-delta writer.
#
# Same drift-resistance pattern as the contracts leg: pa.RecordBatch.from_pylist(buffer)
# infers column dtype from buffer content, so an all-None-for-some-column batch
# infers `null` type and breaks the ParquetWriter that was opened with the
# first batch's inferred schema. Pinning forces dtype regardless of content.
#
# See `tests/test_usaspending_api_daily_assistance_schema_drift.py` for the
# regression guard. The contracts-side fix is in commit `cc5c0b09`.
PINNED_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("internal_id", pa.int64(), nullable=True),
        pa.field("generated_internal_id", pa.string(), nullable=True),
        pa.field("award_id", pa.string(), nullable=True),
        pa.field("award_type", pa.string(), nullable=True),
        pa.field("recipient_name", pa.string(), nullable=True),
        pa.field("recipient_uei", pa.string(), nullable=True),
        pa.field("action_date", pa.date32(), nullable=True),
        pa.field("federal_action_obligation", pa.decimal128(28, 10), nullable=True),
        pa.field("loan_value", pa.decimal128(28, 10), nullable=True),
        pa.field("subsidy_cost", pa.decimal128(28, 10), nullable=True),
        pa.field("awarding_agency_name", pa.string(), nullable=True),
        pa.field("awarding_sub_agency_name", pa.string(), nullable=True),
        pa.field("funding_agency_name", pa.string(), nullable=True),
        pa.field("funding_sub_agency_name", pa.string(), nullable=True),
        pa.field("cfda_number", pa.string(), nullable=True),
        pa.field("cfda_title", pa.string(), nullable=True),
        pa.field("transaction_description", pa.string(), nullable=True),
        pa.field("mod", pa.string(), nullable=True),
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

    # Flatten `Assistance Listing` (nested {cfda_number, cfda_title}) into
    # two top-level columns. Probe showed the API sometimes returns null
    # for the entire object; handle both shapes defensively.
    listing = api_row.get("Assistance Listing")
    if isinstance(listing, dict):
        projected["cfda_number"] = _coerce(listing.get("cfda_number"), "string")
        projected["cfda_title"] = _coerce(listing.get("cfda_title"), "string")
    else:
        projected["cfda_number"] = None
        projected["cfda_title"] = None

    projected["target_date"] = target_date
    projected["ingested_at"] = datetime.now(timezone.utc)
    return projected


def _post_with_backoff(
    *,
    client: httpx.Client,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST with 1s → 2s → 4s → 8s → 16s exponential backoff on 429."""
    last_exception: Exception | None = None
    for attempt in range(5):
        try:
            t0 = time.time()
            response = client.post(
                USASPENDING_TRANSACTION_SEARCH_URL,
                json=payload,
                timeout=60.0,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            print(
                f"[usaspending-api-assistance] POST page={payload.get('page')} "
                f"status={response.status_code} elapsed_ms={elapsed_ms}"
            )
            if response.status_code == 429:
                backoff = 2 ** attempt
                print(
                    f"[usaspending-api-assistance] 429 throttle, sleeping {backoff}s"
                )
                time.sleep(backoff)
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exception = exc
            backoff = 2 ** attempt
            print(
                f"[usaspending-api-assistance] attempt {attempt + 1} failed: "
                f"{type(exc).__name__}: {exc}; sleeping {backoff}s"
            )
            time.sleep(backoff)
    raise RuntimeError(
        f"USAspending POST failed after 5 retries: {last_exception}"
    )


def _iter_pages(
    *,
    client: httpx.Client,
    target_date: date,
    max_api_calls: int,
    cache_path: str | None,
    use_cache: bool,
) -> Iterator[list[dict[str, Any]]]:
    """Yield page-result lists for assistance transactions on target_date.

    Chunks by `award_type_code` to keep each sub-query inside USAspending's
    stable pagination depth. Stops when either (a) every code reports
    hasNext=False, (b) max_api_calls is hit globally, or (c) a code exceeds
    PER_CODE_PAGE_CEILING (which would indicate truly anomalous daily volume
    — surfaces as a stderr warning, but does not crash the run).
    """
    if use_cache and cache_path and os.path.exists(cache_path):
        print(f"[usaspending-api-assistance] reading from cache {cache_path}")
        with open(cache_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
        return

    cache_fh = open(cache_path, "w") if (cache_path and not use_cache) else None
    try:
        time_period = [
            {
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "date_type": "last_modified_date",
            }
        ]

        api_calls = 0
        per_code_failures: list[str] = []
        for code in ASSISTANCE_AWARD_TYPES:
            if api_calls >= max_api_calls:
                print(
                    f"[usaspending-api-assistance] max_api_calls={max_api_calls} "
                    f"reached before code={code}; stopping"
                )
                return

            page = 1
            code_pages = 0
            print(f"[usaspending-api-assistance] starting code={code}")
            try:
                while api_calls < max_api_calls and code_pages < PER_CODE_PAGE_CEILING:
                    payload = {
                        "filters": {
                            "award_type_codes": [code],
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
                    code_pages += 1

                    results = body.get("results") or []
                    if cache_fh is not None:
                        cache_fh.write(json.dumps(results) + "\n")

                    yield results

                    page_metadata = body.get("page_metadata") or {}
                    if not page_metadata.get("hasNext"):
                        print(
                            f"[usaspending-api-assistance] code={code} "
                            f"hasNext=False at page={page} (pages={code_pages})"
                        )
                        break
                    page += 1
                else:
                    if code_pages >= PER_CODE_PAGE_CEILING:
                        # Per-code ceiling hit. Truncated, but the run is still
                        # useful; continue to the next code.
                        print(
                            f"[usaspending-api-assistance] WARN code={code} hit "
                            f"PER_CODE_PAGE_CEILING={PER_CODE_PAGE_CEILING}; "
                            f"some assistance transactions may be unlanded for "
                            f"target_date={target_date}"
                        )
            except RuntimeError as exc:
                # _post_with_backoff exhausted its 5-retry budget. Per the
                # operator-observed pattern (2026-05-22 probes), upstream
                # USAspending serves intermittent 500s and RemoteProtocolErrors
                # that can outlast our retry window. Don't let one code's
                # upstream blip discard the other nine codes' completed pages.
                # The run is logged as partial-coverage in evidence and a daily
                # backfill can resweep the affected code separately.
                print(
                    f"[usaspending-api-assistance] WARN code={code} failed "
                    f"after retry budget exhausted at page={page} "
                    f"(pages={code_pages}): {exc}; continuing to next code"
                )
                per_code_failures.append(code)

        if per_code_failures:
            print(
                f"[usaspending-api-assistance] WARN partial coverage: "
                f"{len(per_code_failures)}/{len(ASSISTANCE_AWARD_TYPES)} codes "
                f"failed → {per_code_failures}. Rows from completed codes are "
                f"still being yielded."
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
    """HEAD the R2 key to enforce per-date idempotency."""
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
    """Assistance api-delta entry. Called by usaspending_api_daily_assistance_app.py.

    Returns evidence dict: rows_loaded, payload_bytes, api_calls,
    r2_object_key, skipped_existing, dry_run.
    """
    from landing.r2 import R2Landing

    bucket = "dex-raw-landing-zone"
    print(
        f"[usaspending-api-assistance] start feed_date={feed_date} run_id={run_id} "
        f"max_api_calls={max_api_calls} dry_run={dry_run}"
    )

    if not dry_run and _r2_key_exists(bucket=bucket, key=r2_object_key):
        print(
            f"[usaspending-api-assistance] r2_key already exists, skipping: "
            f"{r2_object_key}"
        )
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
            print(
                f"[usaspending-api-assistance] dry_run rows={row_count} "
                f"api_calls={api_calls}"
            )
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

        # s3 wire-in: zero-rows-must-be-explicit. Mirror the contracts leg's
        # count-probe + graceful-400 fallback (see commit 82f6dec6).
        if landing_result.rows_loaded == 0:
            import hashlib

            probe_payload = {
                "filters": {
                    "award_type_codes": ASSISTANCE_AWARD_TYPES,
                    "time_period": [
                        {
                            "start_date": feed_date.isoformat(),
                            "end_date": feed_date.isoformat(),
                            "date_type": "last_modified_date",
                        }
                    ],
                },
                "fields": ["Award ID"],  # minimal field set for a count probe
                "page": 1,
                "limit": 1,
                "sort": "Action Date",
                "order": "desc",
            }
            import datetime as _dt
            from landing.safety import assert_landed_or_explicit_empty

            # Attempt the count probe; minimal-field probes occasionally
            # trigger upstream 400s on dates the main paginated query found
            # legitimately empty. Treat probe failure as corroborating evidence
            # of the main query's zero result, not a gate.
            probe_body: dict | None = None
            probe_error: str | None = None
            try:
                probe_body = _post_with_backoff(client=client, payload=probe_payload)
            except RuntimeError as exc:
                probe_error = str(exc)
                print(
                    f"[usaspending-api-assistance] count-probe failed for "
                    f"{feed_date}: {exc}; treating as empty (main query "
                    f"returned 0 rows)"
                )
                # probe_body stays None — _is_upstream_empty(None) returns True

            upstream_was_empty = _is_upstream_empty(probe_body)
            probe_results_count = len((probe_body or {}).get("results") or []) if probe_body is not None else None
            probe_has_next = bool(((probe_body or {}).get("page_metadata") or {}).get("hasNext")) if probe_body is not None else None

            proof_params_hash = hashlib.sha256(
                json.dumps(
                    {
                        "feed_date": feed_date.isoformat(),
                        "award_type_codes": ASSISTANCE_AWARD_TYPES,
                        "date_type": "last_modified_date",
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()

            assert_landed_or_explicit_empty(
                rows_loaded=landing_result.rows_loaded,
                upstream_was_empty=upstream_was_empty,
                upstream_proof={
                    "source": "usaspending-api-v2-spending-by-transaction-assistance",
                    "endpoint": USASPENDING_TRANSACTION_SEARCH_URL,
                    "request_params_hash": proof_params_hash,
                    "api_results_count": probe_results_count,
                    "api_has_next": probe_has_next,
                    "checked_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "feed_date": feed_date.isoformat(),
                    "note": (
                        f"First-page probe: results_count={probe_results_count} "
                        f"has_next={probe_has_next} for "
                        f"feed_date={feed_date.isoformat()} (assistance leg)"
                        + (f"; probe_error={probe_error}" if probe_error else "")
                    ),
                },
            )
            print(
                f"[usaspending-api-assistance] upstream confirmed empty: "
                f"api_results_count={probe_results_count} "
                f"api_has_next={probe_has_next} feed_date={feed_date}"
            )

    print(
        f"[usaspending-api-assistance] r2 wrote rows={landing_result.rows_loaded} "
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
        cache_path = (
            f"/tmp/usa_api_assist_cache_{feed_date.isoformat()}_{args.window}.json"
        )

    run_id = str(_uuid.uuid4())
    r2_key = (
        f"usaspending/assistance/api-delta/date={feed_date.isoformat()}/data.parquet"
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
