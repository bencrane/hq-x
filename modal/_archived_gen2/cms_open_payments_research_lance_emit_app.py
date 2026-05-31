"""CMS Open Payments Research -> Lance emit (Wave 2 sweep daily cron).

Wave 2 of the Lance sweep -- CMS Open Payments "Research" feed (industry
payments tied to clinical research), 2024 onward (the normalized 15-column
schema). The R2 layout is
``cms-open-payments/year=YYYY/feed=research/*.parquet``.

Same operational discipline as the General feed app -- daily cron 15min
after general. Far smaller dataset (~756K rows vs ~15.4M for general) so
memory + timeout shrink accordingly.

Schedule: ``45 7 * * *`` UTC -- 07:45 UTC daily, 15min after general.

Secrets:
    dex-db    -- DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
    bulk-ingest-r2     -- R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/cms_open_payments_research_lance_emit_app.py
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
    app_name="data-engine-x-cms-open-payments-research-lance-emit",
    script_path="/root/scripts/run_cms_open_payments_research_lance_emit.py",
    display_name="cms_open_payments_research_lance",
    cron_schedule="45 7 * * *",
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
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron(CONFIG.cron_schedule),
)
def emit() -> dict:
    return run_emit(CONFIG)


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(emit.remote(), indent=2, default=str))
