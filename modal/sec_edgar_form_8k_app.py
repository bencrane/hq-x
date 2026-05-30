"""SEC EDGAR Form 8-K (current report — material events) — unattended R2 backfill via Modal.

Wraps `scripts/run_sec_edgar_form_8k_r2_ingest.py` for unattended long-running
execution. Each cron tick spawns a Modal container; the container runs the
script for up to ~5 hours; the script's per-(year, quarter, accession) JSON
checkpoint persists to a Modal Volume mounted at /state so the next tick
resumes where this one left off.

**Why Modal:** Form 8-K is per-filing fetch (no DERA bulk equivalent — same
constraint as the 13F/DEF 14A/13D-13G siblings). 8-K filings volume is ~1.4M
since 2010 with ~80K of those carrying Item 2.03 (5-15% of total). With TARGET_RPS=1
from Modal's independent egress IP, 16h to 25h end-to-end across all the cron
ticks. Running locally on the operator's machine for that wall-clock would be
operationally fragile.

**Coverage:** parent SPLIT scopes 2010-2026 (16 years; modernized Item taxonomy
post-Aug-2004 + healthy Item 2.03 reporting volume from 2010+). The script is
idempotent per (year, quarter, accession) via the state file.

**Schedule:** every 6 hours at 30 minutes past the hour, offset from the
sibling SEC EDGAR Modal crons to avoid concurrent SEC EDGAR fair-use cap
overflow. Existing slots: 13d-13g (01:00), 13f (02:00), def-14a (00:00),
abs-15g (TBD, sibling SPLIT), 10-k (TBD, sibling SPLIT). This app's
30 3,9,15,21 slot leaves room for the sibling SPLITs at slots 4/5/10/11/16/17/22/23.

**Secrets / Volume:** same as sibling SEC EDGAR per-filing apps
(`dex-db` + `bulk-ingest-r2`).

**TARGET_RPS:** Modal container env injects SEC_EDGAR_TARGET_RPS=1 (parent SPLIT's
inherited p1-rps-budget-exceeded constraint — aggregate SEC EDGAR fair-use cap
is 10 RPS across all concurrent crons).

**Deploy:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sec_edgar_form_8k_app.py

**Manual smoke run:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sec_edgar_form_8k_app.py::run_form_8k_backfill --years=2024

**Stop / rollback:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal app stop data-engine-x-sec-edgar-form-8k
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sec-edgar-form-8k")

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

# Parent SPLIT scopes 2010+; phasing strategy lives in the script.
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
    # 30 3,9,15,21 UTC — zero-overlap with existing 13f (02/08/14/20),
    # def-14a (00/06/12/18), 13d-13g (01/07/13/19). Offset by 30 min so
    # the start-of-hour bursts from the sibling crons settle before this
    # app's container boots.
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("30 3,9,15,21 * * *"),
)
def run_form_8k_backfill(years: str = DEFAULT_YEARS) -> dict[str, Any]:
    _bridge_database_url()
    state_volume.reload()

    state_file = "/state/form_8k_state.json"

    env = os.environ.copy()
    # Parent SPLIT inherited constraint p1-rps-budget-exceeded: TARGET_RPS=1.
    # Override the script's already-defaulted-to-1 with an explicit value so
    # operators inspecting the Modal log see the choice.
    env["SEC_EDGAR_TARGET_RPS"] = "1"
    env["SEC_EDGAR_HTTP_CONCURRENCY"] = "16"

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    cmd = [
        "python3",
        "/root/scripts/run_sec_edgar_form_8k_r2_ingest.py",
        "--years", years,
        "--state-file", state_file,
    ]

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_form_8k_backfill",
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
        "form": "form_8k",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "years": years,
        "state_file": state_file,
        "target_rps": env["SEC_EDGAR_TARGET_RPS"],
    }
