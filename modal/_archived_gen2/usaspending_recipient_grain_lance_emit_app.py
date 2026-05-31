"""USAspending recipient-grain → Lance emit (daily cron).

Pre-aggregates USAspending contract data at recipient_uei grain into a Lance
dataset on R2. Reads from two sources mirroring the MV's own CTEs exactly:

  - recency_agg: entities.mv_usaspending_contracts_typed (typed MV — closes
    the predecessor PR #433 +13.8% dollar drift caused by reading raw).
  - set_aside_agg: entities.usaspending_contracts (raw — only source that
    carries all 15 set-aside columns).

Output schema (26 cols): recipient_uei + 10 recency cols + 15 set-aside flags.

Schedule: ``0 4 * * *`` UTC — 4 AM daily, 3h after the usaspending API
daily-delta finishes at 1 AM.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
    bulk-ingest-r2     — R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_recipient_grain_lance_emit_app.py
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
    app_name="data-engine-x-usaspending-recipient-grain-lance-emit",
    script_path="/root/scripts/run_usaspending_recipient_grain_lance_emit.py",
    display_name="usaspending_recipient_grain_lance",
    cron_schedule="0 4 * * *",
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
