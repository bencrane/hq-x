"""FAA Aircraft Registry ingest — Modal wrapper.

Wraps scripts/run_faa_aircraft_registry_ingest.py inside a Modal function with
a weekly cron (Thu 06:00 UTC, after FAA's Wed-night release).

Source: https://registry.faa.gov/database/ReleasableAircraft.zip — single
fixed URL that FAA replaces in place each Wednesday night. ZIP is ~50-100 MB
containing comma-delimited .txt files. v1 ingests MASTER (~400K aircraft),
ACFTREF (~25K make/model), ENGINE (~6K engine refs).

Secrets:
    Named secret 'faa-aircraft-registry-db' carries DATABASE_URL.
    Create once (or update) via:

        doppler run --project hq-all --config prd -- bash -c \\
            'modal secret create --force faa-aircraft-registry-db DATABASE_URL="$DEX_DB_URL_POOLED"'

    The _bridge_database_url() helper maps DATABASE_URL → DEX_DB_URL_POOLED
    so the script reads the expected env var name (FAA airmen / FINRA pattern).

Deploy:
    cd /Users/benjamincrane/hq-all && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy apps/data-engine-x/modal/faa_aircraft_registry_ingest_app.py

One-shot run (detached, async):
    doppler run --project hq-all --config prd -- \\
        modal run --detach apps/data-engine-x/modal/faa_aircraft_registry_ingest_app.py::run_ingest

Smoke test (few rows, verify DB write):
    modal run apps/data-engine-x/modal/faa_aircraft_registry_ingest_app.py::run_ingest \\
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

app = modal.App("data-engine-x-faa-aircraft-registry-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

# dex-db required for HeartbeatLoop write to ops.cron_heartbeats.
FUNCTION_SECRETS = [
    modal.Secret.from_name("faa-aircraft-registry-db"),
    modal.Secret.from_name("hqx-db"),
]

# FAA aircraft ZIP is smaller than the airmen ZIP (~50-100 MB vs 150-300 MB);
# 3 CSVs total ~430K rows. Streaming COPY-to-temp peaks well under 2 GB; 4 GB
# is comfortable margin. 1 h timeout gives 4× margin over expected ~15 min runtime.
INGEST_TIMEOUT_SECONDS = 1 * 60 * 60
INGEST_MEMORY_MB = 4096

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the script reads DEX_DB_URL_POOLED.
    Map across so the script can read DEX_DB_URL_POOLED. (FINRA / airmen pattern.)"""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _import_script() -> Any:
    """Import run_faa_aircraft_registry_ingest from /root/scripts.

    Import must happen *inside* the Modal function (not at module top level)
    so the local script files are present in the container.
    """
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    import importlib

    return importlib.import_module("run_faa_aircraft_registry_ingest")


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
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 6 * * 4"),  # Thu 06:00 UTC, after FAA Wed-night release
)
def run_ingest(
    dry_run: bool = False,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Run the FAA Aircraft Registry ingest.

    Args:
        dry_run:  Download and parse but do not write to DB.
        max_rows: Stop after this many rows per CSV (smoke-test limit).
    """
    _bridge_database_url()
    script = _import_script()

    cli_args: list[str] = []
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
        hb.set_stage("faa_aircraft_registry_ingest", {"dry_run": dry_run})
        return script.main(cli_args)


# ---------------------------------------------------------------------------
# Local entrypoint
# ---------------------------------------------------------------------------


@app.local_entrypoint()
def main(
    dry_run: bool = False,
    max_rows: int = 0,
) -> None:
    """Local entrypoint — invokes run_ingest.remote() and prints the result.

    Usage:
        modal run apps/data-engine-x/modal/faa_aircraft_registry_ingest_app.py
        modal run --detach apps/data-engine-x/modal/faa_aircraft_registry_ingest_app.py
    """
    kwargs: dict[str, Any] = {}
    if dry_run:
        kwargs["dry_run"] = dry_run
    if max_rows:
        kwargs["max_rows"] = max_rows

    result = run_ingest.remote(**kwargs)
    print(result)
