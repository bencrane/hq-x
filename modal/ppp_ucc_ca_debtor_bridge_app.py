"""PPP × CA UCC-1 debtor Pattern B Lance bridge — weekly cron.

Delegates to build_bridge_ppp_ucc_ca_debtor_lance.py with --apply.
Cadence: Thursday 16:00 UTC — staggered from existing bridge crons.

Pattern B bridge: CA-state PPP borrowers × CA UCC-1 debtor filings (deduped
to debtor-name-grain via SELECT DISTINCT). The equipment-finance-lien signal:
PPP recipients that pledged collateral on a California UCC-1 filing. Method:
legal_name_state_exact_ca v1.0.0 (L21 REUSE — no new method row).

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + bridge-run ledger.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/ppp_ucc_ca_debtor_bridge_app.py

Verify deploy:
    modal app list --json | jq -e '.[] | select(.description=="data-engine-x-ppp-ucc-ca-debtor-bridge") | select(.state=="deployed")'
"""
from __future__ import annotations

import os
import sys

import modal
from modal import Cron, Secret

app = modal.App("data-engine-x-ppp-ucc-ca-debtor-bridge")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "pyarrow>=16.0",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/modal/landing")
)

FUNCTION_SECRETS = [
    Secret.from_name("hqx-db"),
    Secret.from_name("bulk-ingest-r2"),
]


def _bridge_database_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=30 * 60,
    memory=8192,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=Cron("0 16 * * 4"),  # Thursday 16:00 UTC
)
def weekly_refresh() -> None:
    """Build PPP CA borrowers × CA UCC-1 debtors bridge and write to Lance."""
    sys.path.insert(0, "/root")

    _bridge_database_url()

    from build_bridge_ppp_ucc_ca_debtor_lance import main  # noqa: F401 — Modal path
    import sys as _sys
    _sys.argv = ["build_bridge_ppp_ucc_ca_debtor_lance.py", "--apply"]
    raise SystemExit(main())
