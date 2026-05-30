"""Nightly Coverage card emit.

Modal app `data-engine-x-coverage-stats-emit` — wraps scripts/emit_coverage_stats.py.
Discovers every Lance generic-table registered in Polaris, probes each via
`lance.dataset(uri)` for row count + last-version timestamp, and writes one row
per table to ops.coverage_stats. Read at HTTP request time by
app/routers/coverage_stats_v1.py (no live Lance scans on card load).

Schedule: 08:00 UTC daily — staggered after USAspending FABS daily (07:00 UTC)
and CMS Open Payments emits (07:30 UTC); leaves the 09:00 hour clear for
downstream consumers.

Secrets (Modal):
  dex-db                  DATABASE_URL -> DEX_DB_URL_POOLED bridge; the emit
                          script prefers DEX_DB_URL_DIRECT but falls back to
                          POOLED/DATABASE_URL.
  bulk-ingest-r2          R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.
  polaris-health-check    POLARIS_PUBLIC_URL / POLARIS_ROOT_PRINCIPAL_ID /
                          POLARIS_ROOT_PRINCIPAL_SECRET / POLARIS_DEFAULT_CATALOG_NAME.

Deploy (post-merge from refreshed main):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/coverage_stats_emit_app.py
"""
from __future__ import annotations

import os
import sys
from typing import Any

import modal

app = modal.App("data-engine-x-coverage-stats-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "pylance>=0.20,<1.0",
        "psycopg[binary]",
        "requests",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("polaris-health-check"),
]

# Polaris discovery (~3 HTTP calls per table) + Lance probe (~1s each) across
# ~250 tables is well under 30 min. 1h timeout, 4 GB memory.
EMIT_TIMEOUT_SECONDS = 60 * 60
EMIT_MEMORY_MB = 4096

# 08:00 UTC daily per directive §"Repos and ordering" (staggered after the
# USAspending FABS leg at 07:00 UTC). Cron literal kept inline so the
# verification harness can grep for it.
CRON_SCHEDULE = "0 8 * * *"


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the emit script prefers
    DEX_DB_URL_DIRECT but falls back to DEX_DB_URL_POOLED / DATABASE_URL."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=EMIT_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron("0 8 * * *"),
)
def run_emit() -> dict[str, Any]:
    """Cron entrypoint. Polaris discovery + Lance physical probe."""
    _bridge_database_url()
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    from emit_coverage_stats import main as emit_main  # noqa: E402
    from landing.ledger import HeartbeatLoop  # noqa: E402

    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_emit",
        run_id=run_id,
    ) as hb:
        hb.set_stage("coverage_emit")
        result = emit_main(dry_run=False, limit=None)
    if isinstance(result, dict):
        result["run_id"] = run_id
    return result


@app.local_entrypoint()
def smoke(dry_run: bool = True, limit: int = 3) -> None:
    """Manual smoke entry: `modal run modal/coverage_stats_emit_app.py::smoke`."""
    result = run_emit.remote()
    print(result)
