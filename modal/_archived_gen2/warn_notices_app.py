"""WARN Act layoff notices (Big Local News) — Modal app.

Scheduling: Trigger.dev drives this feed — apps/hq-x/src/trigger/warn-notices-daily.ts
(schedule `30 13 * * *` UTC) POSTs the `trigger_refresh_via_http` endpoint daily,
which spawns daily_refresh in Modal. The native modal.Cron was removed 2026-05-29
as the first feed of the Modal-cron -> Trigger.dev scheduling migration; Modal
still does all of the compute. 13:30 UTC sits well after BLN's ~23:50 UTC publish.

Functions:

  daily_refresh — downloads the Big Local News warn-transformer consolidated
    integrated.csv → R2 Parquet, then emits the Lance dataset (overwrite mode —
    always the latest snapshot). Invoked via spawn from trigger_refresh_via_http
    (or `modal run ...::daily_refresh` manually).

Delegates to:
  scripts/run_warn_notices_to_r2.py       — R2 ingest
  scripts/run_warn_notices_lance_emit.py  — Lance emit

Secrets (state-procurement-runbook standard):
  dex-db  → DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
  bulk-ingest-r2   → R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/warn_notices_app.py

Manual trigger (first-run or re-run):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/warn_notices_app.py::daily_refresh
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import modal
from modal import Image, Secret

app = modal.App("data-engine-x-warn-notices")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "certifi>=2024.7.4",
        "pandas>=2.0",  # not in pyproject.toml — needed by run_warn_notices_to_r2.py
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]

INGEST_MEMORY_MB = 4096
INGEST_TIMEOUT_SECONDS = 30 * 60


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the scripts read DEX_DB_URL_*."""
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _run_ingest(snapshot_date: str) -> None:
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from run_warn_notices_to_r2 import ingest
    import datetime as _dt

    ingest(_dt.date.fromisoformat(snapshot_date))


def _run_lance_emit() -> None:
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from run_warn_notices_lance_emit import emit

    emit()


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
)
def daily_refresh(snapshot_date: str | None = None) -> dict:
    """Daily WARN Act notices ingest + Lance emit.

    Downloads the Big Local News consolidated integrated.csv, writes today's
    snapshot to R2, then re-emits the Lance dataset (overwrite, latest snapshot).

    `snapshot_date` (YYYY-MM-DD) is passed explicitly by the Trigger.dev
    scheduler so the ingested date is deterministic regardless of dispatch
    latency; falls back to today (UTC) for the native cron and manual runs.
    """
    _bridge_database_url()
    if snapshot_date is None:
        snapshot_date = _today_str()

    _run_ingest(snapshot_date)
    _run_lance_emit()

    return {"status": "ok", "snapshot_date": snapshot_date}


# HTTP dispatch wrapper for the Trigger.dev-scheduled migration (pilot).
# Trigger task `apps/hq-x/src/trigger/warn-notices-daily.ts` POSTs this
# Modal-issued stable URL daily; we spawn daily_refresh async and return the
# call_id immediately (fire-and-forget — the ingest runs in its own container,
# never in Trigger.dev). Mirrors txdot_letting_ingest_app.trigger_ingest_via_http.
# Unauthenticated, like txdot: the Big Local News upstream is public. Proxy-auth
# (requires_proxy_auth=True + Modal-Key/Modal-Secret) is added before the broader
# Modal->Trigger rollout, not for this single-feed pilot.
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_refresh_via_http(snapshot_date: str | None = None) -> dict:
    call = daily_refresh.spawn(snapshot_date)
    return {"call_id": call.object_id, "snapshot_date": snapshot_date or _today_str()}
