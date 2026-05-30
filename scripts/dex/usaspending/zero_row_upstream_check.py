#!/usr/bin/env python3
"""Zero-row upstream check — detect silent zero-row USAspending failures.

Cycle: usaspending-pipeline-remediation (2026-05-13).

Background:
    Predecessor audit found the daily USAspending cron was declaring
    `succeeded_with_zero_new_rows` outcomes silently — the cron pulled zero
    rows, recorded success, and no alert fired. The audit identified ~8,183
    USAspending contracts that should have landed for action_date=2026-05-10
    but the cron landed a 0-byte poison file instead.

What this check does:
    Hits the USAspending sync endpoint with a tiny payload (size=1, single
    target_date), and checks whether upstream has data for that date. If yes
    but our cron landed zero rows, the cron's outcome should be
    `failed_upstream_has_data` instead of `succeeded_zero_rows` — and an
    alert should fire.

Usage (called by modal/usaspending_api_daily_app.py before declaring success
on a zero-row run):
    from scripts.usaspending.zero_row_upstream_check import upstream_has_data

    if rows_loaded == 0:
        if upstream_has_data(target_date):
            return 'failed_upstream_has_data'  # alert fires
        else:
            return 'succeeded_zero_rows'       # legitimate; quiet

CLI usage (operator debugging):
    doppler run --project hq-all --config prd -- \\
        bash -c 'python scripts/usaspending/zero_row_upstream_check.py 2026-05-10'

    Exit 0 = upstream HAS data (failure inference).
    Exit 1 = upstream has zero rows (legitimate succeeded_zero outcome).
    Exit 2 = HTTP error (caller should NOT infer; default to success).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

USASPENDING_SEARCH_URL = (
    "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
)
PRIME_CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]


def upstream_has_data(target_date: date, *, timeout_seconds: float = 30.0) -> bool:
    """Return True if USAspending has ≥1 prime-contract transaction modified
    on the given target_date. Returns False on zero rows. Raises on HTTP error
    so callers can decide whether to default-to-success.
    """
    payload = {
        "filters": {
            "time_period": [
                {
                    "start_date": target_date.isoformat(),
                    "end_date": target_date.isoformat(),
                }
            ],
            "award_type_codes": PRIME_CONTRACT_AWARD_TYPES,
        },
        "fields": ["internal_id"],
        "limit": 1,
        "page": 1,
        "sort": "internal_id",
        "order": "asc",
    }
    with httpx.Client(
        headers={"User-Agent": "data-engine-x/1.0 (zero-row-check)"},
        timeout=timeout_seconds,
    ) as client:
        resp = client.post(USASPENDING_SEARCH_URL, json=payload)
        resp.raise_for_status()
        body = resp.json()
    results = body.get("results", [])
    log.info(
        "upstream probe for %s: results_len=%d page_metadata=%s",
        target_date,
        len(results),
        body.get("page_metadata", {}),
    )
    return len(results) > 0


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: zero_row_upstream_check.py YYYY-MM-DD", file=sys.stderr)
        return 64
    try:
        target = date.fromisoformat(sys.argv[1])
    except ValueError as exc:
        print(f"FAIL: invalid date: {exc}", file=sys.stderr)
        return 64
    try:
        has = upstream_has_data(target)
    except httpx.HTTPError as exc:
        print(f"HTTP error probing upstream: {exc}", file=sys.stderr)
        return 2
    if has:
        print(json.dumps({"target_date": target.isoformat(), "upstream_has_data": True}))
        return 0
    print(json.dumps({"target_date": target.isoformat(), "upstream_has_data": False}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
