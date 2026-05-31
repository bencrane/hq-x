"""grants.gov daily open-opportunities ingest — Modal-hosted.

Downloads today's grants.gov bulk zip, transcodes XML → Parquet × 2,
uploads to R2, emits Lance datasets × 2 (Pattern A), registers in Polaris.

Schedule: daily at 08:00 UTC — well after grants.gov's ~03-04 UTC publish
window per L44 cadence observation. Configurable via env override
MODAL_GRANTS_GOV_CRON.

App name: data-engine-x-grants-gov-daily (c16 — exactly one declaration)

Cite:
    modal/usaspending_api_daily_assistance_app.py:47,74,196-201 — Modal app shape
    scripts/run_ca_sos_entities_lance_emit.py:40,65-67 — image + FUNCTION_SECRETS shape
    DATA-FACTORY-DATASET-LIFECYCLE-PLAYBOOK.md §"Stage 5" — LANCE_BYPASS_SPILLING
    DATA-FACTORY-LESSONS-LEARNED.md §L47 — modal run --detach for >5min jobs

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/grants_gov_daily_app.py

Manual single run (--detach per L47 — transcode + Lance can exceed 5 min):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/grants_gov_daily_app.py::run_grants_gov_daily \\
        --feed-date 2026-05-22
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from typing import Any

import modal

# ── App (c16: single declaration of this exact name) ────────────────────────
app = modal.App("data-engine-x-grants-gov-daily")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install(
        "boto3",
        "lxml",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
        "requests",
    )
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("polaris-health-check"),
]

# ≥4 GB headroom: XML transcode + Parquet write + Lance double-emit.
# 81k synopsis rows is small but Polaris subprocess + lxml iterparse need room.
INGEST_MEMORY_MB = 4096
INGEST_TIMEOUT_SECONDS = 60 * 60  # 1h ceiling; transcode+emit expected ~10min

DEFAULT_CRON = os.environ.get("MODAL_GRANTS_GOV_CRON", "0 8 * * *")


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; ingest script expects DEX_DB_URL_DIRECT."""
    if "DATABASE_URL" in os.environ:
        os.environ.setdefault("DEX_DB_URL_DIRECT", os.environ["DATABASE_URL"])
        os.environ.setdefault("DEX_DB_URL_POOLED", os.environ["DATABASE_URL"])


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron(DEFAULT_CRON),
)
def run_grants_gov_daily(
    feed_date: str | None = None,
    local_zip: str | None = None,
    dry_run: bool = False,
    skip_polaris: bool = False,
) -> dict[str, Any]:
    """Daily entry point. Defaults to today UTC when called by Cron;
    accepts an explicit feed_date for manual backfill / smoke tests."""
    _bridge_database_url()

    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")

    target_date: date
    if feed_date:
        target_date = date.fromisoformat(feed_date)
    else:
        target_date = datetime.now(timezone.utc).date()

    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_grants_gov_to_r2 import run_ingest  # noqa: E402 — inserted sys.path

    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_grants_gov_daily",
        run_id=run_id,
    ) as hb:
        hb.set_stage("r2_ingest_and_lance_emit", {"feed_date": target_date.isoformat(), "dry_run": dry_run})
        result = run_ingest(
            feed_date=target_date,
            local_zip_path=local_zip,
            dry_run=dry_run,
            skip_polaris=skip_polaris,
        )
    result["run_id"] = run_id
    return result


@app.local_entrypoint()
def main(
    feed_date: str | None = None,
    dry_run: bool = False,
    skip_polaris: bool = False,
) -> None:
    """`modal run modal/grants_gov_daily_app.py` for local smoke testing."""
    import json
    result = run_grants_gov_daily.remote(
        feed_date=feed_date,
        dry_run=dry_run,
        skip_polaris=skip_polaris,
    )
    print(json.dumps(result, default=str, indent=2))
