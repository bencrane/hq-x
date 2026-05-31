"""FMCSA insurance_history (inshist_essentials) → Lance emit (daily cron).

Per-cohort Modal app following the one-app-per-cohort pattern.

Source: ``fmcsa-derived/inshist_essentials/snapshot=YYYY-MM-DD/data.parquet``
Output: ``s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/insurance_history_lance/``

Schedule: ``45 6 * * *`` UTC (06:45 UTC — staggered 15min after insurance_active,
45min after fmcsa-factory-daily). Heaviest insurance cohort (~7.4M rows).

Secrets required (Modal):
    dex-db   — DEX_DB_URL_DIRECT for commit lock + ingest-run ledger
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd --no-check-version -- \\
        modal deploy modal/fmcsa_insurance_history_lance_emit_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd --no-check-version -- \\
        modal run modal/fmcsa_insurance_history_lance_emit_app.py::emit
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
    app_name="data-engine-x-fmcsa-insurance-history-lance-emit",
    script_path="/root/scripts/run_fmcsa_insurance_history_lance_emit.py",
    display_name="fmcsa_insurance_history_lance",
    cron_schedule="45 6 * * *",
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
