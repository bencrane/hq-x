"""Epiq11 daily ingest — R2 fresh-fetch + Lance emit pipeline.

Cron schedule (dependency order — each step reads from prior steps):

  01:00 UTC daily_cases_refresh
        Pulls case universe (type=Cases) → R2 + emits epiq.cases_lance.
  02:00 UTC daily_claims_refresh
        For every case, pulls claims register → R2 + emits epiq.claims_lance.
  02:30 UTC daily_dockets_refresh
        Same pattern for dockets → emits epiq.dockets_lance.
  03:00 UTC daily_claims_resolved_refresh
        Reads claims_lance, applies _lib normalizers, writes
        epiq.claims_resolved_lance — claim-grain spine with 5 identity-
        resolution columns. The canonical JOIN axis for bridges.
  03:15 UTC daily_creditors_refresh
        Reads claims_resolved_lance, aggregates to creditor-identity grain,
        writes epiq.creditors_lance (convenience rolodex with neutral
        statistical rollups; NOT the bridge join axis).
  03:30 UTC daily_bridge_ppp_borrower_refresh
        Reads claims_resolved × sba.ppp_borrowers_lance → writes
        bridges.epiq_claim_ppp_borrower_lance (claim-grain).
  04:00 UTC daily_bridge_uspto_owner_refresh
        Reads claims_resolved × uspto.case_file_owner_lance × case_file_lance
        → writes bridges.epiq_claim_uspto_owner_lance (claim-grain).

Each function delegates to:
  scripts/run_epiq_to_r2.py                          — R2 ingest subcommand
  scripts/run_epiq_lance_emit.py                     — raw Lance emit subcommand
  scripts/emit_epiq_claims_resolved_lance.py         — claim-grain spine
  scripts/emit_epiq_creditors_lance.py               — identity rolodex
  scripts/build_bridge_epiq_claim_ppp_borrower_lance.py
  scripts/build_bridge_epiq_claim_uspto_owner_lance.py

The 30-min spacing between claims and dockets avoids hammering Epiq's
SPA-backing JSON endpoint with two parallel multi-case fan-outs (Cloudflare
bot protection triggers at higher concurrent connection rates from one IP).

Secrets:
  dex-db          → DATABASE_URL / DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT
                    (required for lance_commit_lock advisory locks)
  bulk-ingest-r2  → R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/epiq_ingest_app.py

Manual trigger (one surface, first-run or re-run):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/epiq_ingest_app.py::daily_cases_refresh
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import modal
from modal import Cron, Image, Secret

app = modal.App("data-engine-x-epiq-ingest")

image = (
    Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "pyarrow>=16.0",
        "boto3",
        "httpx",
    )
    .add_local_dir(
        Path(__file__).resolve().parent.parent / "scripts" / "dex",
        remote_path="/root/scripts",
    )
    .add_local_dir(
        Path(__file__).resolve().parent / "landing",
        remote_path="/root/landing",
    )
)

FUNCTION_SECRETS = [
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]

# Cases: one POST, ~22s. Claims/dockets: ~946 cases @ concurrency=2 with
# 0.5s polite delay ≈ 12-15 min per surface. 60 min covers worst case +
# Cloudflare backoff after transient 403s.
INGEST_TIMEOUT_SECONDS = 60 * 60
INGEST_MEMORY_MB = 4096

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _bridge_database_url() -> None:
    """Bridge DATABASE_URL (from dex-db secret) to DEX_DB_URL_* names."""
    if "DATABASE_URL" in os.environ:
        if "DEX_DB_URL_POOLED" not in os.environ:
            os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
        if "DEX_DB_URL_DIRECT" not in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _run_script(*args: str) -> None:
    """Invoke a script under /root/scripts in a subprocess for hermetic stdout/stderr.

    We use subprocess (not `import + call main()`) because the scripts use
    argparse + sys.exit, and isolation prevents leaked argparse state across
    multiple surface refreshes in the same Modal function call.
    """
    cmd = [sys.executable, *args]
    logger.info("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd="/root", check=False, capture_output=False)
    if proc.returncode != 0:
        raise RuntimeError(f"script failed rc={proc.returncode}: {' '.join(cmd)}")


# --------------------------------------------------------------------------- #
# Surface 1 — cases
# --------------------------------------------------------------------------- #


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("0 1 * * *"),  # 01:00 UTC daily
)
def daily_cases_refresh() -> dict:
    """Daily Epiq case-universe refresh (~22s for 946 cases)."""
    _bridge_database_url()
    _run_script("/root/scripts/run_epiq_to_r2.py", "cases")
    _run_script("/root/scripts/run_epiq_lance_emit.py", "cases", "--apply")
    return {"status": "ok", "surface": "cases"}


# --------------------------------------------------------------------------- #
# Surface 2 — claims
# --------------------------------------------------------------------------- #


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("0 2 * * *"),  # 02:00 UTC daily (1h after cases)
)
def daily_claims_refresh() -> dict:
    """Daily Epiq claims-register refresh — every case in the universe."""
    _bridge_database_url()
    _run_script(
        "/root/scripts/run_epiq_to_r2.py", "claims",
        "--all-cases", "--concurrency", "2",
    )
    _run_script("/root/scripts/run_epiq_lance_emit.py", "claims", "--apply")
    return {"status": "ok", "surface": "claims"}


# --------------------------------------------------------------------------- #
# Surface 3 — dockets
# --------------------------------------------------------------------------- #


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("30 2 * * *"),  # 02:30 UTC daily (30 min after claims start)
)
def daily_dockets_refresh() -> dict:
    """Daily Epiq docket-register refresh — every case in the universe."""
    _bridge_database_url()
    _run_script(
        "/root/scripts/run_epiq_to_r2.py", "dockets",
        "--all-cases", "--concurrency", "2",
    )
    _run_script("/root/scripts/run_epiq_lance_emit.py", "dockets", "--apply")
    return {"status": "ok", "surface": "dockets"}


# --------------------------------------------------------------------------- #
# Surface 4 — claims_resolved (claim-grain spine, the canonical JOIN axis)
# --------------------------------------------------------------------------- #


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("0 3 * * *"),  # 03:00 UTC daily (30min after dockets at 02:30)
)
def daily_claims_resolved_refresh() -> dict:
    """Daily Epiq claim-grain spine — derived from claims_lance.

    Reads epiq.claims_lance, applies canonical _lib v1.0.0 entity-name +
    address normalizers + the epiq state-parse helper, and writes
    epiq.claims_resolved_lance — every claim row PLUS five neutral
    identity-resolution columns. This is the canonical JOIN axis for
    bridges; per-claim granularity preserved end-to-end.
    """
    _bridge_database_url()
    _run_script(
        "/root/scripts/emit_epiq_claims_resolved_lance.py", "--apply",
    )
    return {"status": "ok", "surface": "claims_resolved"}


# --------------------------------------------------------------------------- #
# Surface 5 — creditors rolodex (identity-grain convenience spine)
# --------------------------------------------------------------------------- #


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=Cron("15 3 * * *"),  # 03:15 UTC daily (15min after claims_resolved)
)
def daily_creditors_refresh() -> dict:
    """Daily Epiq creditor rolodex spine — derived from claims_resolved_lance.

    Aggregates the claim-grain spine to creditor-identity grain (name +
    state + zip5). One row per distinct creditor with neutral statistical
    rollups (claim counts, dollar exposure across all 11 Epiq amount
    buckets, distinct cases stiffed-in, time window).

    This is a CONVENIENCE spine for identity-level lookups, NOT the bridge
    JOIN axis — bridges JOIN through claims_resolved_lance to preserve
    per-claim fan-out detail.
    """
    _bridge_database_url()
    _run_script(
        "/root/scripts/emit_epiq_creditors_lance.py", "--apply",
    )
    return {"status": "ok", "surface": "creditors"}


# --------------------------------------------------------------------------- #
# Bridges (downstream — read claims_resolved_lance, JOIN to canonical spines)
# --------------------------------------------------------------------------- #


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=Cron("30 3 * * *"),  # 03:30 UTC daily (30min after claims_resolved)
)
def daily_bridge_ppp_borrower_refresh() -> dict:
    """Daily bridge: epiq claims × SBA PPP borrowers (legal_name+state)."""
    _bridge_database_url()
    _run_script(
        "/root/scripts/build_bridge_epiq_claim_ppp_borrower_lance.py", "--apply",
    )
    return {"status": "ok", "surface": "bridge_ppp_borrower"}


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=Cron("0 4 * * *"),  # 04:00 UTC daily (30min after PPP bridge)
)
def daily_bridge_uspto_owner_refresh() -> dict:
    """Daily bridge: epiq claims × USPTO trademark owners (legal_name+state)."""
    _bridge_database_url()
    _run_script(
        "/root/scripts/build_bridge_epiq_claim_uspto_owner_lance.py", "--apply",
    )
    return {"status": "ok", "surface": "bridge_uspto_owner"}
