"""FMCSA carrier_essentials → Lance emit (Wave 3 canary daily cron).

Wave 3 of the multi-phase hq-all rebuild — Lance canary. Daily cron that:

  1. Reads the latest snapshot of the existing
     ``fmcsa-derived/carrier_essentials/snapshot=YYYY-MM-DD/data.parquet``
     (produced by ``data-engine-x-fmcsa-factory-daily`` at 06:00 UTC).
  2. Re-emits it as the Lance dataset
     ``s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance/``.
  3. Builds the BTREE scalar index on ``dot_number`` (Lance's headline benefit
     — per-DOT random-access in <100ms — requires the index).
  4. Optimizes (compact + cleanup_older_than=7d).
  5. Records the run in ``ops.data_source_ingest_runs`` for system-health
     observability against the registered 24h SLA.

Schedule: ``30 6 * * *`` UTC. Offset 30min after the FMCSA factory daily cron
(``0 6 * * *``) so the source Parquet is fresh by the time we re-emit.

This is the Wave 3 canary cron. The pattern (Postgres advisory commit_lock,
LANCE_BYPASS_SPILLING, TMPDIR redirect, daily optimize/cleanup, ingest-run
ledger) is the operational discipline that gets replicated to other sources
in the Lance sweep cycle.

Secrets required (Modal):
    dex-db                — DEX_DB_URL_DIRECT for commit lock +
                                     ops.data_source_ingest_runs writes.
    bulk-ingest-r2                 — R2_ENDPOINT / R2_ACCESS_KEY_ID /
                                     R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fmcsa_carrier_essentials_lance_emit_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/fmcsa_carrier_essentials_lance_emit_app.py::emit
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
    app_name="data-engine-x-fmcsa-carrier-essentials-lance-emit",
    script_path="/root/scripts/run_fmcsa_carrier_essentials_lance_emit.py",
    display_name="fmcsa_carrier_essentials_lance",
    cron_schedule="30 6 * * *",
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
