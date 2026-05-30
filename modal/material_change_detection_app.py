"""Phase 3 — Modal cron app for material-change detection + cohort drift scan.

Runs every 6 hours. Per directive 2026-05-12-hq-all-phase-3-material-change-
detection §E:

  1. Call DEX `POST /api/v1/internal/observability/material-changes/run-cycle`
     → detect_changes_all_sources() diffs latest snapshots vs prior for
     every source with declarations; emits ops.material_change_events
     rows.

  2. Call hq-x `POST /api/v1/internal/cohort-drift/run-cycle`
     → cohort_drift_scanner pulls new events from DEX, scans active
     signings, emits 'attribute_changed' deliveries + Telegram alerts.

Each call records via the Phase 0a observability ledger; failures are
logged but do not block the other phase from running.

Format-agnostic: the detector works against Parquet+Iceberg snapshots
today; Lance sources plug in via the SnapshotPair resolver. The cron
itself doesn't care about format.

Modal secret required (one-time setup):

    doppler run --project hq-all --config prd -- bash -c '
        modal secret create --force dex-material-change-cron \\
            DEX_API_BASE_URL="$DEX_API_BASE_URL" \\
            DEX_SERVICE_TOKEN="$DEX_SERVICE_TOKEN" \\
            HQX_API_BASE_URL="$HQX_API_BASE_URL" \\
            HQX_TRIGGER_SHARED_SECRET="$TRIGGER_SHARED_SECRET"
    '

(DEX uses the service token for /alerts/run-cycle style endpoints;
hq-x uses TRIGGER_SHARED_SECRET for its internal endpoints per the
existing /internal/* convention.)

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/material_change_detection_app.py

Manual run (test):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/material_change_detection_app.py::run_cycle
"""

from __future__ import annotations

import os
from typing import Any

import modal

app = modal.App("data-engine-x-material-change-cron")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install("httpx>=0.27.0", "certifi>=2024.7.4")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("dex-material-change-cron"),
]

CYCLE_TIMEOUT_SECONDS = 600  # detector can take a few minutes on large diffs
CRON_EXPRESSION = "0 */6 * * *"  # every 6 hours UTC


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=CYCLE_TIMEOUT_SECONDS,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron(CRON_EXPRESSION),
)
def run_cycle() -> dict[str, Any]:
    """Run material-change detection + cohort-drift scan, sequentially."""
    import httpx

    dex_base = os.environ.get("DEX_API_BASE_URL", "").rstrip("/")
    dex_key = os.environ.get("DEX_SERVICE_TOKEN")
    hqx_base = os.environ.get("HQX_API_BASE_URL", "").rstrip("/")
    hqx_secret = os.environ.get("HQX_TRIGGER_SHARED_SECRET")

    if not dex_base or not dex_key:
        raise RuntimeError("DEX_API_BASE_URL + DEX_SERVICE_TOKEN required")
    if not hqx_base or not hqx_secret:
        raise RuntimeError("HQX_API_BASE_URL + HQX_TRIGGER_SHARED_SECRET required")

    out: dict[str, Any] = {}

    # ── Step 1: detector ──
    detector_url = f"{dex_base}/api/v1/internal/observability/material-changes/run-cycle"
    detector_headers = {
        "Authorization": f"Bearer {dex_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=CYCLE_TIMEOUT_SECONDS) as client:
        try:
            r1 = client.post(detector_url, headers=detector_headers, json={})
            r1.raise_for_status()
            out["detector"] = r1.json()
        except Exception as exc:
            print(f"[material-change-cron] detector failed: {exc}")
            out["detector"] = {"error": str(exc)}

        # ── Step 2: cohort drift scan ──
        scan_url = f"{hqx_base}/api/v1/internal/cohort-drift/run-cycle"
        scan_headers = {
            "Authorization": f"Bearer {hqx_secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            r2 = client.post(scan_url, headers=scan_headers, json={})
            r2.raise_for_status()
            out["scanner"] = r2.json()
        except Exception as exc:
            print(f"[material-change-cron] cohort scanner failed: {exc}")
            out["scanner"] = {"error": str(exc)}

    print(
        f"[material-change-cron] detector={out.get('detector')} "
        f"scanner={out.get('scanner')}"
    )
    return out
