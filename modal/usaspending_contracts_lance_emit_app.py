"""USAspending contracts → Lance emit (Wave 1 sweep monthly cron).

Wave 1 of the Lance sweep — federal contract awards across the actively-
refreshed fiscal years (2024, 2025, 2026). Cadence is monthly (aligned with
the USAspending monthly bulk drop on the 16th). Wider memory + longer
timeout than the FMCSA crons: ~30M rows × 298 cols → ~3-4 GB Arrow buffer.

Schedule: ``0 7 16 * *`` UTC — 16th of each month at 07:00 UTC, 1h after
usaspending-monthly produces the source year=YYYY Parquets at 06:00 UTC.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
                         (Shared DB across all Lance datasets.)
    bulk-ingest-r2     — R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_contracts_lance_emit_app.py
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
    app_name="data-engine-x-usaspending-contracts-lance-emit",
    script_path="/root/scripts/run_usaspending_contracts_lance_emit.py",
    display_name="usaspending_contracts_lance",
    cron_schedule="0 7 16 * *",
    memory_mb=16384,
    timeout_seconds=10800,
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
