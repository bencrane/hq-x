"""Overture Maps Places (US slice) ingest — Modal wrapper.

Wraps scripts/run_overture_places_ingest.py inside a Modal function with a
monthly cron schedule (5th of each month UTC midnight) and sized for the
full US Places parquet slice (~5-10M rows, ~5-15 GB compressed).

Secrets:
    Named secret 'overture-places-db' carries DATABASE_URL.
    Create once (or update) via:

        doppler run --project hq-all --config prd -- bash -c \\
            'modal secret create --force overture-places-db DATABASE_URL="$DEX_DB_URL_POOLED"'

    The _bridge_database_url() helper maps DATABASE_URL → DEX_DB_URL_POOLED
    so the script reads the expected env var name (FINRA pattern;
    modal/finra_brokercheck_ingest_app.py lines 77-83).

Deploy (must run from apps/data-engine-x/ — pip_install_from_pyproject and
add_local_dir resolve relative to the modal CLI cwd, and the monorepo root
pyproject.toml is a uv-workspace stub with no [project] dependencies):
    cd ~/Desktop/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/overture_places_ingest_app.py

One-shot run (detached, async):
    cd ~/Desktop/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/overture_places_ingest_app.py::run_ingest

Smoke test (few rows, verify DB write):
    cd ~/Desktop/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/overture_places_ingest_app.py::run_ingest \\
        --batch-size 1000 --max-rows 500
"""

from __future__ import annotations

import os
import sys
from typing import Any

import modal

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-overture-places-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

# dex-db required for HeartbeatLoop write to ops.cron_heartbeats.
FUNCTION_SECRETS = [
    modal.Secret.from_name("overture-places-db"),
    modal.Secret.from_name("hqx-db"),
]

# ~5-10M US rows from a ~5-15 GB compressed parquet slice. Memory peaks at
# ~6-8 GB during pyarrow batch materialization + COPY staging. Timeout 4 h
# covers worst-case S3-throttled run with margin. 2 vCPUs for pyarrow
# decompression parallelism.
INGEST_TIMEOUT_SECONDS = 4 * 60 * 60
INGEST_MEMORY_MB = 8192

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the script reads DEX_DB_URL_POOLED.
    Map across so the script can read DEX_DB_URL_POOLED. (FINRA pattern.)"""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _import_script() -> Any:
    """Import run_overture_places_ingest from /root/scripts.

    Import must happen *inside* the Modal function (not at module top level)
    so the local script files are present in the container.
    """
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    import importlib

    return importlib.import_module("run_overture_places_ingest")


# ---------------------------------------------------------------------------
# Modal function
# ---------------------------------------------------------------------------


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    cpu=2,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 0 5 * *"),  # 5th of each month UTC midnight
)
def run_ingest(
    release: str | None = None,
    batch_size: int = 10_000,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Run the Overture Places US-slice ingest.

    Args:
        release:    Pin a specific Overture release string, e.g. '2026-04-23.0'.
                    Defaults to auto-resolving the latest release in the bucket.
        batch_size: Parquet batch size (rows per iteration).
        max_rows:   Stop after ingesting this many US rows (smoke-test limit).
    """
    _bridge_database_url()
    script = _import_script()

    cli_args: list[str] = []
    if release:
        cli_args.extend(["--release", release])
    if batch_size != 10_000:
        cli_args.extend(["--batch-size", str(batch_size)])
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
        hb.set_stage("overture_places_ingest", {"release": release})
        return script.main(cli_args)


# ---------------------------------------------------------------------------
# Local entrypoint
# ---------------------------------------------------------------------------


@app.local_entrypoint()
def main(
    release: str = "",
    batch_size: int = 10_000,
    max_rows: int = 0,
) -> None:
    """Local entrypoint — invokes run_ingest.remote() and prints the result.

    Usage:
        modal run apps/data-engine-x/modal/overture_places_ingest_app.py
        modal run --detach apps/data-engine-x/modal/overture_places_ingest_app.py
    """
    kwargs: dict[str, Any] = {"batch_size": batch_size}
    if release:
        kwargs["release"] = release
    if max_rows:
        kwargs["max_rows"] = max_rows

    result = run_ingest.remote(**kwargs)
    print(result)
