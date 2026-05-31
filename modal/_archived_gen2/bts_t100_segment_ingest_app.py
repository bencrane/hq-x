"""BTS T-100 Segment (All Carriers) ingest — Modal wrapper.

Wraps scripts/run_bts_t100_segment_ingest.py inside a Modal function with a
monthly cron schedule. T-100 publishes monthly with ~4-6 month lag, so monthly
cron picks up new releases without unnecessary re-pulls.

The full backfill (1990–present, 37 years × ~700K rows/year ≈ 25M rows) is
expected to take 60-90 minutes wall-clock. Recurring monthly runs only re-pull
the most-recent year (which absorbs new month releases via natural-key
conflict update) so they finish in 5-15 minutes.

Modal-cron pattern (NOT Trigger.dev) — keeps consistency with epiq_claims,
epiq_dockets, warn_notices, faa_airmen and avoids the documented Trigger.dev
→ DEX M2M boundary gap (CLAUDE.md §"Auth model" → "Open boundary gap
(2026-04-29)").

Secrets:
    Named secret 'bts-t100-db' carries DATABASE_URL.
    Create once (or update) via:

        doppler run --project hq-all --config prd -- bash -c \\
            'modal secret create --force bts-t100-db DATABASE_URL="$DEX_DB_URL_POOLED"'

    The _bridge_database_url() helper maps DATABASE_URL → DEX_DB_URL_POOLED
    so the script reads the expected env var name (FAA / WARN / Epiq pattern).

Deploy:
    cd /Users/benjamincrane/hq-all && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy apps/data-engine-x/modal/bts_t100_segment_ingest_app.py

Full backfill (detached, async — recommended for first run):
    doppler run --project hq-all --config prd -- \\
        modal run --detach apps/data-engine-x/modal/bts_t100_segment_ingest_app.py::run_ingest

Targeted year(s):
    modal run apps/data-engine-x/modal/bts_t100_segment_ingest_app.py::run_ingest \\
        --years 2024-2026

Smoke test (few rows from one year, verify DB write):
    modal run apps/data-engine-x/modal/bts_t100_segment_ingest_app.py::run_ingest \\
        --years 2025 --max-rows 100
"""

from __future__ import annotations

import os
import sys
from typing import Any

import modal

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = modal.App("data-engine-x-bts-t100-segment-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

# dex-db required for HeartbeatLoop write to ops.cron_heartbeats.
FUNCTION_SECRETS = [
    modal.Secret.from_name("bts-t100-db"),
    modal.Secret.from_name("hqx-db"),
]

# Full backfill = 37 years × ~700K rows/year. At ~5K-row batch upserts and ~30s
# per-year download from TranStats, expect 60-90 min wall-clock. 4-hour timeout
# is ~3× margin; recurring monthly runs typically finish in 5-15 min.
INGEST_TIMEOUT_SECONDS = 4 * 60 * 60
INGEST_MEMORY_MB = 4096

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the script reads DEX_DB_URL_POOLED.
    Map across so the script can read DEX_DB_URL_POOLED. (FAA / WARN / Epiq
    pattern.)"""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _import_script() -> Any:
    """Import run_bts_t100_segment_ingest from /root/scripts.

    Import must happen *inside* the Modal function (not at module top level)
    so the local script files are present in the container.
    """
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    import importlib

    return importlib.import_module("run_bts_t100_segment_ingest")


# --------------------------------------------------------------------------- #
# Modal function
# --------------------------------------------------------------------------- #


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    cpu=2,
    # Monthly cron: 03:00 UTC on the 5th of each month. BTS publishes monthly
    # T-100 releases mid-month with a ~4-6 month lag; the 5th gives BTS time to
    # land any late-month release for the prior month before we pull.
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 3 5 * *"),
)
def run_ingest(
    years: str | None = None,
    dry_run: bool = False,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Run the BTS T-100 Segment (All Carriers) ingest.

    Args:
        years:    YYYY or YYYY-YYYY (inclusive). Defaults to all available
                  years on TranStats.
        dry_run:  Fetch + parse but do not write to DB.
        max_rows: Stop after this many rows per year (smoke-test limit).
    """
    _bridge_database_url()
    script = _import_script()

    cli_args: list[str] = []
    if years:
        cli_args.extend(["--years", years])
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
        hb.set_stage("bts_t100_ingest", {"years": years, "dry_run": dry_run})
        return script.main(cli_args)


# --------------------------------------------------------------------------- #
# Local entrypoint
# --------------------------------------------------------------------------- #


@app.local_entrypoint()
def main(
    years: str = "",
    dry_run: bool = False,
    max_rows: int = 0,
) -> None:
    """Local entrypoint — invokes run_ingest.remote() and prints the result.

    Usage:
        modal run apps/data-engine-x/modal/bts_t100_segment_ingest_app.py
        modal run apps/data-engine-x/modal/bts_t100_segment_ingest_app.py --years 2025
        modal run --detach apps/data-engine-x/modal/bts_t100_segment_ingest_app.py
    """
    kwargs: dict[str, Any] = {}
    if years:
        kwargs["years"] = years
    if dry_run:
        kwargs["dry_run"] = dry_run
    if max_rows:
        kwargs["max_rows"] = max_rows

    result = run_ingest.remote(**kwargs)
    print(result)
