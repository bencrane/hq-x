"""SEC EDGAR Schedule 13D / 13G (5%+ beneficial-ownership disclosures) — unattended R2 backfill via Modal.

Wraps `scripts/run_sec_edgar_schedule_13d_13g_r2_ingest.py` for unattended
long-running execution. Each cron tick spawns a Modal container; the container
runs the script for up to ~5 hours; the script's per-(year, accession) JSON
checkpoint persists to a Modal Volume mounted at /state so the next tick
resumes where this one left off.

**Why Modal:** Schedule 13D/G is per-filing fetch (no DERA bulk equivalent
exists; verified via SEC catalog audit 2026-05-10). ~120K filings × ~3 fetches
per filing = ~360K HTTP GETs. At SEC's 10 RPS fair-use cap, ~10 hours minimum
wall-clock end-to-end. Running locally on the operator's machine is fragile
(session timeouts, system sleep). Modal containers have an independent egress
IP so SEC's 10 RPS cap applies per Modal app — TARGET_RPS=5 is safe.

**Coverage:** the directive scopes 2010-2026 (17 years). The 2024 smoke run
shipped via PR #283 lives in R2 already; the script is idempotent per
(year, accession) via the state file so it skips already-completed accessions.

**Schedule:** every 6 hours, offset 1h from the DEF 14A cron to spread load.

**Secrets:**
- `dex-db` — DATABASE_URL → DEX_DB_URL_*
- `bulk-ingest-r2` — R2_*

**Volume:** `sec-edgar-ingest-state`

**Deploy:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sec_edgar_schedule_13d_13g_app.py

**Manual run (override years):**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sec_edgar_schedule_13d_13g_app.py::run_schedule_13d_13g_backfill --years=2010-2026
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sec-edgar-schedule-13d-13g")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

state_volume = modal.Volume.from_name("sec-edgar-ingest-state", create_if_missing=True)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

INGEST_MEMORY_MB = 4096
INGEST_TIMEOUT_SECONDS = 5 * 60 * 60
SUBPROCESS_TIMEOUT_SECONDS = INGEST_TIMEOUT_SECONDS - 5 * 60

DEFAULT_YEARS = "2010-2026"


def _bridge_database_url() -> None:
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    volumes={"/state": state_volume},
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 1,7,13,19 * * *"),  # 01:00, 07:00, 13:00, 19:00 UTC
)
def run_schedule_13d_13g_backfill(years: str = DEFAULT_YEARS) -> dict[str, Any]:
    _bridge_database_url()
    state_volume.reload()

    state_file = "/state/schedule_13d_13g_state.json"

    env = os.environ.copy()
    env["SEC_EDGAR_TARGET_RPS"] = "5"
    env["SEC_EDGAR_HTTP_CONCURRENCY"] = "16"

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    cmd = [
        "python3",
        "/root/scripts/run_sec_edgar_schedule_13d_13g_r2_ingest.py",
        "--years", years,
        "--state-file", state_file,
    ]

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_schedule_13d_13g_backfill",
        run_id=run_id,
    ) as hb:
        hb.set_stage("subprocess_running", {"years": years, "target_rps": env["SEC_EDGAR_TARGET_RPS"]})
        proc = subprocess.run(
            cmd,
            env=env,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

    state_volume.commit()

    return {
        "form": "schedule_13d_13g",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "years": years,
        "state_file": state_file,
        "target_rps": env["SEC_EDGAR_TARGET_RPS"],
    }
