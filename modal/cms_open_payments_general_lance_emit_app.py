"""CMS Open Payments General -> Lance emit (Wave 2 sweep daily cron).

Wave 2 of the Lance sweep -- CMS Open Payments "General" feed (drug /
biological / device payments to physicians and teaching hospitals),
2024 onward (the normalized 15-column schema). The R2 layout is
``cms-open-payments/year=YYYY/feed=general/*.parquet``.

CMS publishes annual data but our R2 mirror is refreshed weekly; running
this cron daily is cheap (Lance overwrite mode is a no-op if row content
matches the previous version) and gives us tight upper-bound staleness.

Schedule: ``30 7 * * *`` UTC -- daily 07:30 UTC, well after the FMCSA
crons (06:30/06:45/06:50 UTC) and the carrier-embeddings cron (07:45 UTC).

Secrets:
    dex-db    -- DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
    bulk-ingest-r2     -- R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/cms_open_payments_general_lance_emit_app.py
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
    app_name="data-engine-x-cms-open-payments-general-lance-emit",
    script_path="/root/scripts/run_cms_open_payments_general_lance_emit.py",
    display_name="cms_open_payments_general_lance",
    cron_schedule="30 7 * * *",
    memory_mb=8192,
    timeout_seconds=90 * 60,
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
