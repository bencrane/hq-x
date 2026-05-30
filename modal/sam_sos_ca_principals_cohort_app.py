"""SAM × CA SoS principals Pattern A enriched-cohort — weekly cron.

Delegates to build_bridge_sam_sos_ca_principals_lance.py with --apply.
Cadence: Monday 14:00 UTC (07:00 PT) — chained downstream of the entities-bridge
cron at Mon 13:00 UTC so the predecessor refresh lands first.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for lance_commit_lock + Polaris check.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sam_sos_ca_principals_cohort_app.py
"""
from __future__ import annotations

import os
import sys

import modal
from modal import Cron, Secret

app = modal.App("data-engine-x-sam-sos-ca-principals-cohort")

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
    timeout=30 * 60,   # 30 min — cohort is fast (~12s probe; write + BTREE < 15 min)
    memory=8192,       # 8 GiB — DuckDB hash-join headroom for 18.7M principals
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=Cron("0 14 * * 1"),  # Monday 14:00 UTC — one hour after entities-bridge (Mon 13:00 UTC)
)
def weekly_refresh() -> None:
    """Build SAM × CA SoS principals enriched-cohort and write to Lance."""
    sys.path.insert(0, "/root")

    _bridge_database_url()

    from build_bridge_sam_sos_ca_principals_lance import main  # noqa: F401 — Modal path
    import sys as _sys
    _sys.argv = ["build_bridge_sam_sos_ca_principals_lance.py", "--apply"]
    raise SystemExit(main())
