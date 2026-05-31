"""SEC EDGAR Form 10-K (annual reports) — unattended R2 backfill via Modal.

Wraps `scripts/run_sec_edgar_form_10k_r2_ingest.py` for unattended long-running
execution. Each cron tick spawns a Modal container; the container runs the
script for up to ~5 hours; the script's per-(year, accession) JSON checkpoint
persists to a Modal Volume mounted at /state so the next tick resumes where
this one left off.

**Why Modal:** Form 10-K is per-filing fetch (no DERA bulk equivalent). At
TARGET_RPS=1 against an in-house egress, ~75K-120K filings × ~2 fetches
each ≈ 41-67h end-to-end. Modal containers have an INDEPENDENT egress IP,
so this app overrides TARGET_RPS to 5 in the cron environment to drop
wall-clock to ~5-8h. The script's argparse default stays at 1 so any
non-Modal local invocation honors the inherited cron-stagger constraint
from the parent /scope cycle 2026-05-12-sec-edgar-credit-facility-feeds.md.

**Coverage:** the script is idempotent per (year, accession) — already-fetched
filings are skipped via the state file. Default ``years="2010-2026"`` re-runs
the whole range; old years complete instantly via state-file skip, while
recent + delta years get the actual fetches. The 10-K's annual cadence
means new filings are concentrated Q1-Q2 (FY-end-Dec to filing-deadline)
and Q3 (FY-end-Jun); the rest of the year is delta-only.

**7 streams emitted per filing:** filings, officers_directors,
executive_compensation, security_ownership, properties, legal_proceedings,
risk_factors. See ``scripts/_lib/sec_edgar_form_10k_parser.py``.

**Schedule:** quarterly — first day of every quarter at 03:30 UTC. The
minute=30 hour=03 offset places this cron OUTSIDE the existing 4-app daily
SEC EDGAR cadence (def_14a at 0,6,12,18; schedule_13d_13g at 1,7,13,19;
form_13f at 2,8,14,20; form_144 at its own cadence) so the aggregate
5-crons-per-6h-window cap from the parent /scope cycle holds. 10-K's
annual cadence makes quarterly cron sufficient — each tick resumes
backfill or no-ops on idempotent state.

**Secrets:**
- ``dex-db`` — DATABASE_URL → DEX_DB_URL_* (reused across bulk-ingest apps)
- ``bulk-ingest-r2`` — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY

**Volume:**
- ``sec-edgar-ingest-state`` — persistent /state mount carrying per-form JSON checkpoints.

**Deploy:**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sec_edgar_form_10k_app.py

**Manual run (kick off the bootstrap 2010-2026 backfill once deploy lands):**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sec_edgar_form_10k_app.py::run_form_10k_backfill --years=2010-2026 --detach

**Reset state (destructive — restarts backfill from year=2010):**

    modal volume rm sec-edgar-ingest-state form_10k_state.json

See directive ``~/Desktop/hq/directives/2026-05-12-sec-10k-activation.md`` (sub-cycle 2 of 3).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-sec-edgar-form-10k")

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
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("30 3 1 */3 *"),  # 03:30 UTC, first day of every quarter
)
def run_form_10k_backfill(years: str = DEFAULT_YEARS) -> dict[str, Any]:
    """Spawn the script subprocess; let it run until it finishes (no more
    pending accessions) or Modal kills it at SUBPROCESS_TIMEOUT_SECONDS.
    The script's per-accession checkpoint in /state/form_10k_state.json
    persists across runs via the Modal Volume."""
    _bridge_database_url()
    state_volume.reload()

    state_file = "/state/form_10k_state.json"

    env = os.environ.copy()
    # Modal containers have an independent egress IP; SEC's 10 RPS cap applies
    # per Modal app, so TARGET_RPS=5 is safe (vs script-default=1 set per
    # parent /scope cycle for local-machine parallel-execution constraint).
    env["SEC_EDGAR_TARGET_RPS"] = "5"
    env["SEC_EDGAR_HTTP_CONCURRENCY"] = "16"

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    cmd = [
        "python3",
        "/root/scripts/run_sec_edgar_form_10k_r2_ingest.py",
        "--years", years,
        "--state-file", state_file,
    ]

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_form_10k_backfill",
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
        "form": "form_10k",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": proc.returncode,
        "years": years,
        "state_file": state_file,
        "target_rps": env["SEC_EDGAR_TARGET_RPS"],
    }
