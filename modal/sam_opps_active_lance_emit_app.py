"""SAM.gov opps active → Lance emit (Wave 1 sweep daily cron).

Wave 1 of the Lance sweep — daily SAM.gov opportunity feed. Same operational
discipline as the FMCSA canary.

Schedule: ``30 12 * * *`` UTC — 30min after sam-opps-active-daily produces
the source Parquet at 12:00 UTC.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
                         (Lance commit_lock + ops.data_source_ingest_runs
                          target the same DEX Postgres for all Lance datasets;
                          the secret is named "dex-db" historically
                          but the URL points at the shared DEX DB.)
    bulk-ingest-r2     — R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sam_opps_active_lance_emit_app.py
"""
from __future__ import annotations

import json
import sys

import modal

# Local import — modal/_lib is mounted into the container at /root/_lib via
# build_image(); this sys.path tweak makes the same import work at deploy
# time when this file is imported by `modal deploy`.
sys.path.insert(0, "modal")  # noqa: E402  (must be before _lib import)

from _lib.pattern_a_lance_emit import (  # noqa: E402
    ORCHESTRATOR_SECRETS,
    PatternALanceEmitConfig,
    build_image,
    run_emit,
)

CONFIG = PatternALanceEmitConfig(
    app_name="data-engine-x-sam-opps-active-lance-emit",
    script_path="/root/scripts/run_sam_opps_active_lance_emit.py",
    display_name="sam_gov_opps_active_lance",
    cron_schedule="30 12 * * *",
    timeout_seconds=1800,
)

app = modal.App(CONFIG.app_name)
image = build_image(CONFIG)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=ORCHESTRATOR_SECRETS,
    timeout=CONFIG.timeout_seconds,
    memory=CONFIG.memory_mb,
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=modal.Cron(CONFIG.cron_schedule),
)
def emit() -> dict:
    return run_emit(CONFIG)


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(emit.remote(), indent=2, default=str))
