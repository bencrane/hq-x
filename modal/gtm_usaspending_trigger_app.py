"""GTM USAspending trigger app — configuration-driven daily cron.

Reads active rows from ops.gtm_signals (Postgres), compiles each row's
criteria JSONB into a DuckDB query against usaspending.transaction_fpds_lance
JOINed to spines.sam_entities_lance (for SAM identity enrichment), and POSTs
the matched-cohort rows to the signal's webhook_prod_url via httpx.

Adding a signal = INSERT in ops.gtm_signals. No Python change required.
Muting a signal = UPDATE ... SET is_active = false. The cron skips it.

Schedule: 09:00 UTC daily. Each signal's criteria.time_window_hours determines
its action_date lookback window relative to cron-fire time.

Secrets:
    dex-db          — DEX_DB_URL_POOLED for ops.gtm_signals read.
    bulk-ingest-r2  — R2 credentials for Lance dataset reads.

Substitution note: the directive specified psycopg2; this app uses
psycopg (v3) to match the existing modal/*.py convention (`psycopg[binary]`).
Same API surface for our read-only path.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/gtm_usaspending_trigger_app.py

One-shot test (bypass cron):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/gtm_usaspending_trigger_app.py
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-gtm-usaspending-trigger")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "httpx>=0.27,<1.0",
        "pyarrow>=16",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

EMIT_MEMORY_MB = 8 * 1024
EMIT_TIMEOUT_SECONDS = 30 * 60

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    if "DEX_DB_URL_POOLED" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _fetch_active_signals() -> list[dict[str, Any]]:
    """Cron-specific: list ONLY the active signals. The single-slug read
    (which doesn't filter on is_active) lives in
    ``app/services/gtm_signal_cohort.get_signal_by_slug``."""
    import psycopg
    from psycopg.rows import dict_row
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ["DATABASE_URL"]
    with psycopg.connect(url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT signal_slug, spine_target, criteria, "
                "       webhook_target, webhook_prod_url, webhook_test_url "
                "FROM ops.gtm_signals WHERE is_active = true ORDER BY signal_slug"
            )
            return list(cur.fetchall())


def _run_signal(slug: str, criteria: dict[str, Any]) -> list[dict[str, Any]]:
    """Thin shim — the canonical implementation lives in
    ``app/services/gtm_signal_cohort.fetch_cohort_rows`` so this Modal
    function and the new ``/preview`` HTTP route share one source of
    truth for criteria → DuckDB SQL → cohort rows."""
    from app.services.gtm_signal_cohort import fetch_cohort_rows
    return fetch_cohort_rows(slug, criteria)


def _fetch_signal_by_slug(slug: str) -> dict[str, Any] | None:
    """Thin shim over ``app/services/gtm_signal_cohort.get_signal_by_slug``."""
    from app.services.gtm_signal_cohort import get_signal_by_slug
    return get_signal_by_slug(slug)


def _dispatch(webhook_url: str, signal_slug: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    import httpx
    payload = {
        "signal_slug": signal_slug,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "rows": rows,
    }
    body = json.dumps(payload, default=str)  # default=str handles date/decimal
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=60) as cli:
            r = cli.post(
                webhook_url,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        return {
            "status": r.status_code,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "body_bytes": len(body),
        }
    except Exception as e:
        return {
            "status": "exception",
            "exception_type": type(e).__name__,
            "exception": str(e)[:300],
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "body_bytes": len(body),
        }


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=EMIT_MEMORY_MB,
    min_containers=1,  # operator UI button → must be instant; one warm container.
)
def fire_one_signal(
    slug: str,
    target: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Manual one-shot for the operator UI. Identical compute + dispatch as the
    cron — same _run_signal() (DuckDB over Lance) and same _dispatch() (httpx
    POST). Differences from the cron:
      - Targets exactly one signal by slug.
      - target arg lets the operator override the signal's stored webhook_target
        ('test' | 'prod') for this single firing; falls back to the stored value.
      - limit caps the row count AFTER the ORDER BY federal_action_obligation
        DESC, so the operator gets the top-N rows by obligation deterministically.
        None / 0 / negative = no cap (production-equivalent payload).
      - Empty target URL is an error (not silently skipped) — the UI surfaces
        it so the operator can fix the URL and re-fire.
      - Not-found slug is an error.

    Returns the same per-signal shape as run_signals() results entries, plus
    `matched_rows_total` (pre-limit) so the UI can show truncation."""
    _bridge_database_url()
    sig = _fetch_signal_by_slug(slug)
    if sig is None:
        return {
            "slug": slug,
            "error": f"signal {slug!r} not found in ops.gtm_signals",
        }
    effective_target = (target or sig.get("webhook_target") or "test").lower()
    if effective_target not in ("test", "prod"):
        return {
            "slug": slug,
            "error": f"invalid target {effective_target!r} — must be 'test' or 'prod'",
        }
    webhook = (
        sig.get("webhook_prod_url") or ""
        if effective_target == "prod"
        else sig.get("webhook_test_url") or ""
    )
    if not webhook:
        return {
            "slug": slug,
            "webhook_target": effective_target,
            "error": f"webhook_{effective_target}_url is empty — set a URL before firing",
        }

    rows = _run_signal(slug, sig["criteria"])
    matched_total = len(rows)
    if limit is not None and limit > 0 and limit < matched_total:
        rows = rows[:limit]
    dispatch = _dispatch(webhook, slug, rows)
    return {
        "slug": slug,
        "webhook_target": effective_target,
        "webhook_url": webhook,
        "matched_rows_total": matched_total,
        "sent_rows": len(rows),
        "limit_applied": limit if (limit is not None and limit > 0 and limit < matched_total) else None,
        "dispatch": dispatch,
    }


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=EMIT_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron("0 9 * * *"),
)
def run_signals() -> dict[str, Any]:
    _bridge_database_url()
    signals = _fetch_active_signals()
    logger.info("loaded %d active signals", len(signals))
    results: list[dict[str, Any]] = []
    for sig in signals:
        slug = sig["signal_slug"]
        criteria = sig["criteria"]
        # Per-signal webhook_target ('test' | 'prod') selects which URL fires.
        # Empty URL on the chosen side = skip dispatch (signal stays active,
        # match-row count is still logged for visibility).
        target = sig.get("webhook_target") or "test"
        webhook = (
            sig.get("webhook_prod_url") or ""
            if target == "prod"
            else sig.get("webhook_test_url") or ""
        )
        try:
            rows = _run_signal(slug, criteria)
            if not webhook:
                results.append({
                    "slug": slug,
                    "matched_rows": len(rows),
                    "webhook_target": target,
                    "dispatch": {
                        "status": "skipped",
                        "reason": f"webhook_{target}_url is empty",
                    },
                })
                continue
            dispatch = _dispatch(webhook, slug, rows)
            results.append({
                "slug": slug,
                "matched_rows": len(rows),
                "webhook_target": target,
                "dispatch": dispatch,
            })
        except Exception as e:
            logger.exception("signal %s failed", slug)
            results.append({
                "slug": slug,
                "error": f"{type(e).__name__}: {e}",
            })
    return {
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "signals_loaded": len(signals),
        "results": results,
    }


@app.local_entrypoint()
def main() -> None:
    out = run_signals.remote()
    print(json.dumps(out, indent=2, default=str))
