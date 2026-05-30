"""SBA canonical borrowers Lance emit — one-shot Modal wrapper.

Wraps `scripts/emit_sba_borrowers_lance.py` so it can run durably on Modal
infrastructure instead of in a session-scoped local process. The script
itself takes a long httpfs side-scan over raw SBA parquets + a LEFT JOIN
to `sba/ppp_borrowers_lance` (v1.2.0 PPP backfill), which is too long-running
to safely babysit from an interactive agent session.

No cron schedule — this is a one-shot trigger via `modal run`. The
function decorator omits `schedule=` so Modal treats it as manually-invoked
only. If we later want recurring emits, add a Cron schedule here.

Secrets:
    dex-db       — DEX_DB_URL_DIRECT for commit lock + ingest-run ledger.
    bulk-ingest-r2 — R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sba_borrowers_lance_emit_app.py

Run (one-shot):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/sba_borrowers_lance_emit_app.py::main
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
    app_name="data-engine-x-sba-borrowers-lance-emit",
    script_path="/root/scripts/emit_sba_borrowers_lance.py",
    display_name="sba_borrowers_lance",
    # No recurring schedule — placeholder kept for scaffold compat but not used
    # because the @app.function below omits `schedule=`.
    cron_schedule="0 0 31 12 0",
    memory_mb=16384,  # 16 GB — handles 12M-row aggregate + PPP backfill JOIN
    # Modal R2-egress is slower than the operator's local network. The first
    # detached run timed out at 29 min (subprocess timeout = container
    # timeout - 60s = 1740s). Bumped to 90 min container / 89 min subprocess
    # to give 3x headroom over the local ~10-min expectation.
    timeout_seconds=60 * 90,
)

app = modal.App(CONFIG.app_name)
image = build_image(CONFIG)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=ORCHESTRATOR_SECRETS,
    timeout=CONFIG.timeout_seconds,
    memory=CONFIG.memory_mb,
    # Intentionally NO schedule= — one-shot trigger via `modal run` only.
)
def emit() -> dict:
    return run_emit(CONFIG)


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(emit.remote(), indent=2, default=str))
