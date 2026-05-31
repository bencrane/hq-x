"""SEC EDGAR DEF 14A (annual proxy statements) — unattended R2 backfill via Modal.

Wraps `scripts/run_sec_edgar_def_14a_r2_ingest.py` for unattended long-running
execution. Each cron tick spawns a Modal container; the container runs the
script for up to ~5 hours; the script's per-(year, accession) JSON checkpoint
persists to a Modal Volume mounted at /state so the next tick resumes where
this one left off.

**Why Modal:** SEC EDGAR's per-filing fetch pattern (form.idx → per-accession
HTML) is rate-limited at SEC's 10 RPS fair-use cap. The original 2014-2024
backfill shipped via PR #263. The 2025-2026 delta + ongoing freshness need
unattended long-running fetches; running them locally on the operator's
machine is fragile (session timeouts, system sleep, crashes). Modal containers
have an independent egress IP, so this app's TARGET_RPS=5 doesn't aggregate
with anything the operator is doing locally.

**Coverage:** the script is idempotent per (year, accession) — already-fetched
filings are skipped via the state file. Default `years="2014-2026"` re-runs
the whole range; old years complete instantly via state-file skip, while
2025-2026 (and any future years) get the actual fetches.

**Schedule:** every 6 hours, starting 00:00 UTC. Each container runs ≤5h.

**Secrets:**
- `dex-db` — DATABASE_URL → DEX_DB_URL_* (reused across bulk-ingest apps)
- `bulk-ingest-r2` — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY

**Volume:**
- `sec-edgar-ingest-state` — persistent /state mount carrying per-form JSON checkpoints.

**Deploy:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sec_edgar_def_14a_app.py

**Manual run (override years range, e.g. force 2025-2026 only):**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sec_edgar_def_14a_app.py::run_def_14a_backfill --years=2025-2026

**Reset state (start a fresh backfill — destructive):**

    modal volume rm sec-edgar-ingest-state def_14a_state.json
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sec-edgar-def-14a")

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
INGEST_TIMEOUT_SECONDS = 5 * 60 * 60         # 5 hours hard cap
SUBPROCESS_TIMEOUT_SECONDS = INGEST_TIMEOUT_SECONDS - 5 * 60  # exit 5min before hard kill

# Default backfill range: 2014-2026 (incl. 2025-2026 delta). The script
# is idempotent per (year, accession) via the state file — old years skip
# instantly, new accessions fetch.
DEFAULT_YEARS = "2014-2026"


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the per-filing scripts expect
    DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT. Mirror so both readers work."""
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
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 */6 * * *"),  # 00:00, 06:00, 12:00, 18:00 UTC
)
def run_def_14a_backfill(years: str = DEFAULT_YEARS) -> dict[str, Any]:
    """Spawn the script subprocess; let it run until it finishes (no more
    pending accessions) or Modal kills it at SUBPROCESS_TIMEOUT_SECONDS.
    The script's per-accession checkpoint in /state/def_14a_state.json
    persists across runs via the Modal Volume."""
    _bridge_database_url()
    state_volume.reload()  # see any state writes from prior invocations

    state_file = "/state/def_14a_state.json"

    env = os.environ.copy()
    # Modal containers have independent egress IPs; SEC's 10 RPS cap applies
    # per Modal app, so TARGET_RPS=5 is safe (vs 2 for local-machine parallel).
    env["SEC_EDGAR_TARGET_RPS"] = "5"
    env["SEC_EDGAR_HTTP_CONCURRENCY"] = "16"

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    cmd = [
        "python3",
        "/root/scripts/run_sec_edgar_def_14a_r2_ingest.py",
        "--years", years,
        "--state-file", state_file,
    ]

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_def_14a_backfill",
        run_id=run_id,
    ) as hb:
        hb.set_stage("subprocess_running", {"years": years, "target_rps": env["SEC_EDGAR_TARGET_RPS"]})
        proc = subprocess.run(
            cmd,
            env=env,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

    state_volume.commit()  # persist final state writes back to the Volume

    return {
        "form": "def_14a",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "years": years,
        "state_file": state_file,
        "target_rps": env["SEC_EDGAR_TARGET_RPS"],
    }
