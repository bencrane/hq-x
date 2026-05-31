"""SEC EDGAR Form ABS-15G — unattended R2 backfill + recurring ingest via Modal.

Wraps ``scripts/run_sec_edgar_form_abs_15g_r2_ingest.py`` for unattended
long-running execution. Each cron tick spawns a Modal container; the
container runs the script for up to ~5 hours; the script's per-(year,
quarter, accession) JSON checkpoint persists to a Modal Volume mounted at
/state so the next tick resumes where this one left off.

**Why Modal:** ABS-15G is per-filing fetch (primary_doc.xml + Exhibit 99
per accession). Historical corpus is 2012+; estimated 25K-75K filings.
At TARGET_RPS=1 from the bare script + 3 RPS+ from Modal-deployed (where
each Modal app has an independent egress IP), running locally would be
operationally fragile.

**Cron stagger constraint:** existing SEC EDGAR Modal crons run at:
- form-13f       0 2,8,14,20 UTC
- def-14a        0 0,6,12,18 UTC
- 13d-13g        0 1,7,13,19 UTC

This app picks 0 3,9,15,21 UTC — fills the next free 6h-offset slot so no
more than 4 of the 6 SEC EDGAR crons share any 6h window.

**Schedule:** every 6 hours at 03:00 / 09:00 / 15:00 / 21:00 UTC.

**Secrets / Volume:** same as sibling SEC EDGAR per-filing apps.

**Deploy:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sec_edgar_form_abs_15g_app.py

**Manual run (smoke / backfill):**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sec_edgar_form_abs_15g_app.py::run_form_abs_15g_backfill \\
          --years=2024

Per directive ~/Desktop/hq/directives/2026-05-12-abs-15g-ingest.md.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sec-edgar-form-abs-15g")

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

DEFAULT_YEARS = "2012-2026"


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
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 3,9,15,21 * * *"),  # 03:00, 09:00, 15:00, 21:00 UTC
)
def run_form_abs_15g_backfill(years: str = DEFAULT_YEARS) -> dict[str, Any]:
    _bridge_database_url()
    state_volume.reload()

    state_file = "/state/form_abs_15g_state.json"

    env = os.environ.copy()
    # TARGET_RPS=1 per inherited /scope cycle constraint (parent
    # 2026-05-13 sec-edgar-feeds-backfill-completion §"Inherited constraint
    # (applies to all 3 sub-directives)" — SEC EDGAR aggregate fair-use cap
    # across the 6 SEC EDGAR Modal crons + chained backfill invocations).
    # Modal egress IPs are per-app, but the aggregate cap is the relevant
    # constraint here since chained `modal run --detach` invocations can
    # collide with sibling crons firing concurrently.
    env["SEC_EDGAR_TARGET_RPS"] = "1"
    env["SEC_EDGAR_HTTP_CONCURRENCY"] = "16"

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    cmd = [
        "python3",
        "/root/scripts/run_sec_edgar_form_abs_15g_r2_ingest.py",
        "--years", years,
        "--state-file", state_file,
    ]

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_form_abs_15g_backfill",
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
        "form": "form_abs_15g",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "years": years,
        "state_file": state_file,
        "target_rps": env["SEC_EDGAR_TARGET_RPS"],
    }
