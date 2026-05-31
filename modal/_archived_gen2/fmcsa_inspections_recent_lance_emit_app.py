"""FMCSA inspections_recent (vehicle_inspection_essentials) → Lance emit (daily cron).

Per-cohort Modal app following the one-app-per-cohort pattern.

Heaviest of the 4 new cohorts (~8.18M rows). 8GB memory, 1h timeout.

Source: ``fmcsa-derived/vehicle_inspection_essentials/snapshot=YYYY-MM-DD/data.parquet``
Output: ``s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/inspections_recent_lance/``

Schedule: ``15 7 * * *`` UTC (07:15 UTC — latest slot of the 4 new cohorts;
heaviest dataset runs last to avoid R2 PUT throughput spikes overlapping with
the other 3 cohorts).

Secrets required (Modal):
    dex-db   — DEX_DB_URL_DIRECT for commit lock + ingest-run ledger
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd --no-check-version -- \\
        modal deploy modal/fmcsa_inspections_recent_lance_emit_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd --no-check-version -- \\
        modal run modal/fmcsa_inspections_recent_lance_emit_app.py::emit
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
    app_name="data-engine-x-fmcsa-inspections-recent-lance-emit",
    script_path="/root/scripts/run_fmcsa_inspections_recent_lance_emit.py",
    display_name="fmcsa_inspections_recent_lance",
    cron_schedule="15 7 * * *",
    memory_mb=8192,
)

app = modal.App(CONFIG.app_name)
image = build_image(CONFIG)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=ORCHESTRATOR_SECRETS,
    timeout=CONFIG.timeout_seconds,
    memory=CONFIG.memory_mb,
    schedule=modal.Cron(CONFIG.cron_schedule),
)
def emit() -> dict:
    return run_emit(CONFIG)


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(emit.remote(), indent=2, default=str))
