"""GLEIF LEI records -> Lance emit (Wave 2 sweep weekly cron).

Wave 2 of the Lance sweep -- the universal legal-entity identity spine.
GLEIF (Global Legal Entity Identifier Foundation) publishes weekly
snapshots of every LEI-registered entity worldwide (~3.3M rows). The R2
layout is ``gleif/snapshot=YYYY-MM-DD/lei_records.parquet``.

Cadence is weekly (GLEIF source itself is weekly); cron is Sunday morning
to land soon after upstream's weekly snapshot is mirrored to R2.

Schedule: ``0 8 * * 0`` UTC -- weekly Sunday 08:00 UTC.

Secrets:
    dex-db    -- DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
    bulk-ingest-r2     -- R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/gleif_lei_records_lance_emit_app.py
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
    app_name="data-engine-x-gleif-lei-records-lance-emit",
    script_path="/root/scripts/run_gleif_lei_records_lance_emit.py",
    display_name="gleif_lei_records_lance",
    cron_schedule="0 8 * * 0",
    timeout_seconds=2700,
)

app = modal.App(CONFIG.app_name)
image = build_image(CONFIG)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=ORCHESTRATOR_SECRETS,
    timeout=CONFIG.timeout_seconds,
    memory=CONFIG.memory_mb,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron(CONFIG.cron_schedule),
)
def emit() -> dict:
    return run_emit(CONFIG)


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(emit.remote(), indent=2, default=str))
