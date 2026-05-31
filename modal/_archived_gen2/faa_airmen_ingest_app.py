"""FAA Releasable Airmen Database ingest — Modal wrapper.

Wraps scripts/run_faa_airmen_ingest.py inside a Modal function with a
monthly cron schedule (5th of each month UTC midnight) and sized for
~2.5M rows across 4 CSVs (~150-300 MB ZIP).

Secrets:
    Named secret 'faa-airmen-db' carries DATABASE_URL.
    Create once (or update) via:

        doppler run --project hq-all --config prd -- bash -c \\
            'modal secret create --force faa-airmen-db DATABASE_URL="$DEX_DB_URL_POOLED"'

    The _bridge_database_url() helper maps DATABASE_URL → DEX_DB_URL_POOLED
    so the script reads the expected env var name (FINRA pattern;
    modal/finra_brokercheck_ingest_app.py lines 77-83).

Deploy:
    cd /Users/benjamincrane/hq-all && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy apps/data-engine-x/modal/faa_airmen_ingest_app.py

One-shot run (detached, async):
    doppler run --project hq-all --config prd -- \\
        modal run --detach apps/data-engine-x/modal/faa_airmen_ingest_app.py::run_ingest

Smoke test (few rows, verify DB write):
    modal run apps/data-engine-x/modal/faa_airmen_ingest_app.py::run_ingest \\
        --max-rows 500 --dry-run true
"""

from __future__ import annotations

import os
import sys
from typing import Any

import modal

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-faa-airmen-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

# `dex-db` is required for the orchestrator's HeartbeatLoop write to
# ops.cron_heartbeats; `faa-airmen-db` is the per-source DB the ingest
# script talks to. Both must be present — the heartbeat helper resolves
# DEX_DB_URL_POOLED (from dex-db) before falling back to DATABASE_URL
# (from faa-airmen-db).
FUNCTION_SECRETS = [
    modal.Secret.from_name("faa-airmen-db"),
    modal.Secret.from_name("hqx-db"),
]

# FAA ZIP is ~150-300 MB; 4 CSVs total ~2.5M rows. CSV streaming + COPY-to-temp
# peaks well under 2 GB; 4 GB is comfortable margin (vs Overture's 8 GB for
# pyarrow batch materialization on a 5-15 GB parquet slice).
# 1 h timeout gives 2× margin over the expected ~30 min runtime.
INGEST_TIMEOUT_SECONDS = 1 * 60 * 60
INGEST_MEMORY_MB = 4096

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the script reads DEX_DB_URL_POOLED.
    Map across so the script can read DEX_DB_URL_POOLED. (FINRA pattern.)"""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _import_script() -> Any:
    """Import run_faa_airmen_ingest from /root/scripts.

    Import must happen *inside* the Modal function (not at module top level)
    so the local script files are present in the container.
    """
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    import importlib

    return importlib.import_module("run_faa_airmen_ingest")


# ---------------------------------------------------------------------------
# Modal function
# ---------------------------------------------------------------------------


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    cpu=1,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 0 5 * *"),  # 5th of each month UTC midnight
)
def run_ingest(
    month: str | None = None,
    dry_run: bool = False,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Run the FAA Releasable Airmen ingest.

    Args:
        month:    Pin a specific month, e.g. '042026' (MMYYYY).
                  Defaults to probing current month then prior month.
        dry_run:  Download and parse but do not write to DB.
        max_rows: Stop after this many rows per CSV (smoke-test limit).
    """
    _bridge_database_url()
    script = _import_script()

    cli_args: list[str] = []
    if month:
        cli_args.extend(["--month", month])
    if dry_run:
        cli_args.append("--dry-run")
    if max_rows is not None:
        cli_args.extend(["--max-rows", str(max_rows)])

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_ingest",
        run_id=run_id,
    ) as hb:
        hb.set_stage("faa_airmen_ingest", {"month": month, "dry_run": dry_run})
        return script.main(cli_args)


# ---------------------------------------------------------------------------
# Local entrypoint
# ---------------------------------------------------------------------------


@app.local_entrypoint()
def main(
    month: str = "",
    dry_run: bool = False,
    max_rows: int = 0,
) -> None:
    """Local entrypoint — invokes run_ingest.remote() and prints the result.

    Usage:
        modal run apps/data-engine-x/modal/faa_airmen_ingest_app.py
        modal run --detach apps/data-engine-x/modal/faa_airmen_ingest_app.py
    """
    kwargs: dict[str, Any] = {}
    if month:
        kwargs["month"] = month
    if dry_run:
        kwargs["dry_run"] = dry_run
    if max_rows:
        kwargs["max_rows"] = max_rows

    result = run_ingest.remote(**kwargs)
    print(result)
