"""FMCSA authhist_essentials → Lance emit (Wave 1 sweep daily cron).

Wave 1 of the Lance sweep — authority-history cohort. Same operational
discipline as the canary; see canary docs for rationale.

Schedule: ``50 6 * * *`` UTC — 50min after fmcsa-factory-daily; offset 5min
after crash_essentials Lance emit to avoid commit_lock thrash within the
fmcsa namespace.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
    bulk-ingest-r2     — R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fmcsa_authhist_essentials_lance_emit_app.py
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
    app_name="data-engine-x-fmcsa-authhist-essentials-lance-emit",
    script_path="/root/scripts/run_fmcsa_authhist_essentials_lance_emit.py",
    display_name="fmcsa_authhist_essentials_lance",
    cron_schedule="50 6 * * *",
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
