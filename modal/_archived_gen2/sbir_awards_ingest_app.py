"""SBIR + STTR awards monthly ingest — Modal cron wrapper.

Fires on the 5th of each month at 06:00 UTC. Wraps
`scripts/run_sbir_awards_r2_ingest.py` (single-shot bulk-CSV → ZSTD Parquet
to R2 at sbir/snapshot={YYYY-MM-DD}/awards.parquet, audit ledger to
ops.sbir_ingest_runs).

Secrets:
- `dex-db`   — DATABASE_URL → DEX_DB_URL_*
- `bulk-ingest-r2`    — R2_*

Deploy:

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sbir_awards_ingest_app.py

Manual run:

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sbir_awards_ingest_app.py::run_sbir_awards_monthly_ingest

See directive ~/Desktop/hq/directives/2026-05-10-sbir-sttr-awards-r2-ingest.md.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sbir-awards-monthly-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

INGEST_MEMORY_MB = 2048
INGEST_TIMEOUT_SECONDS = 30 * 60
SUBPROCESS_TIMEOUT_SECONDS = INGEST_TIMEOUT_SECONDS - 60


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; ingest script expects DEX_DB_URL_*."""
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 6 5 * *"),  # 5th of each month, 06:00 UTC
)
def run_sbir_awards_monthly_ingest() -> dict[str, Any]:
    _bridge_database_url()

    started_at = datetime.now(timezone.utc).isoformat()

    cmd = [
        "python3",
        "/root/scripts/run_sbir_awards_r2_ingest.py",
        "--invoked-via", "modal_cron",
    ]

    proc = subprocess.run(
        cmd,
        env=os.environ.copy(),
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"sbir-awards ingest failed (rc={proc.returncode}): "
            f"{proc.stderr[-2000:]}"
        )

    return {
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-500:],
    }
