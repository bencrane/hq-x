"""NY State Active Corporations (DoS Beginning 1800) — Modal app.

**Cadence — Operator-Only Bulk Run (Quarterly Batch).** Per the 2026-05-25
operational policy shift, the state SoS data layer (CA / FL / NY / CO) is
retired from automated schedules: slow-moving corporate registries on
high-frequency crons are a compute-burning anti-pattern that introduced
severe upstream-feed CSV-bleed degradation (PR #731 post-mortem). The
prior ``schedule=Cron("0 14 * * *")`` arg has been removed and the Modal
deployment ``data-engine-x-ny-sos-active-corporations`` is left
permanently stopped. See ``modal/INDEX.md`` §"State SoS pipelines".

Single manual function:

  operator_refresh — Downloads CSV from Socrata n9v6-gdp6 → R2 ZSTD
    Parquet, then re-emits the Lance dataset (overwrite mode — always the
    full latest snapshot). Idempotent on X-SODA2-Truth-Last-Modified —
    no-ops if upstream unchanged.

    Baseline: 4,215,429 rows (clean baseline, 2026-05-25 manual drop).
    Timeout 60 min, memory 8 GB per validator p2.

Delegates to:
  scripts/run_ny_sos_active_corporations_to_r2.py       — R2 ingest (c1)
  scripts/run_ny_sos_active_corporations_lance_emit.py  — Lance emit (c4)

Secrets:
  dex-db  → DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
  bulk-ingest-r2   → R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Manual invocation (operator-triggered, point-in-time snapshot refresh):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/ny_sos_active_corporations_app.py::operator_refresh
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import modal
from modal import Image, Secret

app = modal.App("data-engine-x-ny-sos-active-corporations")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "certifi>=2024.7.4",
        "duckdb>=0.10.0",  # used by run_ny_sos_active_corporations_to_r2 (DuckDB CSV transform)
    )
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]

# Bumped from 30-min/4-GB defaults per validator p2:
# 4.2M rows materially exceeds the directive's 1.5-3M estimate;
# DuckDB transform + Lance write need full 8 GB at this scale.
INGEST_MEMORY_MB = 8192
INGEST_TIMEOUT_SECONDS = 60 * 60


def _bridge_database_url() -> None:
    """Bridge DATABASE_URL (from dex-db secret) to DEX_DB_URL_* names."""
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
    from run_ny_sos_active_corporations_to_r2 import ingest
    import datetime as _dt

    ingest(_dt.date.fromisoformat(snapshot_date))


def _run_lance_emit() -> None:
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from run_ny_sos_active_corporations_lance_emit import emit

    emit()


# schedule: REMOVED 2026-05-25 — state SoS pipelines are now Operator-Only
# Bulk Run (Quarterly Batch). Do NOT re-add a schedule= arg to this decorator
# without an explicit operator-policy reversal; see module docstring.
# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60*60,   # 60 min — bumped from 30-min default per validator p2 (4.2M rows)
    memory=8192,     # 8 GB — bumped from 4-GB default per validator p2
)
def operator_refresh() -> dict:
    """Operator-triggered NY DoS Active Corps ingest + Lance emit (manual).

    Downloads CSV from Socrata n9v6-gdp6, writes today's snapshot to R2,
    then re-emits the Lance dataset (overwrite mode — always the full
    latest snapshot). Idempotent on X-SODA2-Truth-Last-Modified — no-ops
    if upstream unchanged. Invoke explicitly via
    ``modal run modal/ny_sos_active_corporations_app.py::operator_refresh``.
    """
    _bridge_database_url()
    snapshot_date = _today_str()
    run_id = str(uuid.uuid4())

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="operator_refresh",
        run_id=run_id,
    ) as hb:
        hb.set_stage("r2_ingest", {"snapshot_date": snapshot_date})
        _run_ingest(snapshot_date)
        hb.set_stage("lance_emit")
        _run_lance_emit()

    return {"status": "ok", "run_id": run_id, "snapshot_date": snapshot_date}
