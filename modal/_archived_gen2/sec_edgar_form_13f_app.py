"""SEC EDGAR Form 13F (institutional manager holdings) — unattended R2 backfill via Modal.

Wraps `scripts/run_sec_edgar_form_13f_r2_ingest.py` for unattended long-running
execution. Each cron tick spawns a Modal container; the container runs the
script for up to ~5 hours; the script's per-(year, quarter, accession) JSON
checkpoint persists to a Modal Volume mounted at /state so the next tick
resumes where this one left off.

**Why Modal:** Form 13F is per-filing fetch (no DERA bulk equivalent — verified
via SEC catalog audit 2026-05-10; Form N-PORT is the closest sibling but
covers RICs only, not all institutional managers). ~1.2M filings × ~3
fetches per filing = ~3.6M HTTP GETs. At SEC's 10 RPS fair-use cap with
TARGET_RPS=5 from Modal's independent egress IP, ~80h minimum end-to-end
across all the cron ticks. Running locally on the operator's machine for
that wall-clock would be operationally fragile.

**Coverage:** the directive scopes 2013-2024 (12 years; structured XML era
post-mandate). The 2024 Q1 smoke run shipped via PR #280 lives in R2 already;
the script is idempotent per (year, quarter, accession) via the state file.

**Note for downstream consumers:** SEC changed Form 13F's `<value>` field
unit-of-measure in 2022 amendments. Pre-2023 filings report value in
**thousands of dollars**; 2023-period+ filings report in **raw dollars**.
The parser preserves the raw int as `value_thousands_usd`. Downstream MVs
must apply:

    CASE WHEN period_of_report >= '2023-06-30'
         THEN value_thousands_usd::DOUBLE
         ELSE value_thousands_usd::DOUBLE * 1000
    END AS value_usd_corrected

This Modal app does not transform; it only orchestrates the per-filing fetch.

**Schedule:** every 6 hours, offset 2h to spread load with sibling apps.

**Secrets / Volume:** same as sibling SEC EDGAR per-filing apps.

**Deploy:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sec_edgar_form_13f_app.py

**Manual run:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sec_edgar_form_13f_app.py::run_form_13f_backfill --years=2013-2024
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sec-edgar-form-13f")

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

DEFAULT_YEARS = "2013-2024"


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
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 2,8,14,20 * * *"),  # 02:00, 08:00, 14:00, 20:00 UTC
)
def run_form_13f_backfill(years: str = DEFAULT_YEARS) -> dict[str, Any]:
    _bridge_database_url()
    state_volume.reload()

    state_file = "/state/form_13f_state.json"

    env = os.environ.copy()
    env["SEC_EDGAR_TARGET_RPS"] = "5"
    env["SEC_EDGAR_HTTP_CONCURRENCY"] = "16"

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    cmd = [
        "python3",
        "/root/scripts/run_sec_edgar_form_13f_r2_ingest.py",
        "--years", years,
        "--state-file", state_file,
    ]

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_form_13f_backfill",
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
        "form": "form_13f",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "years": years,
        "state_file": state_file,
        "target_rps": env["SEC_EDGAR_TARGET_RPS"],
    }
