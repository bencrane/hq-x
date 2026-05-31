"""SBA × Overture + SBA × Overture × USPTO bridge rebuilds — Modal chain.

Sequential rebuild of two downstream bridges that read from
`sba/borrowers_lance`. Trigger AFTER the SBA borrowers emit completes
(see `modal/sba_borrowers_lance_emit_app.py`).

Step 1: `build_bridge_sba_overture_address_lance.py --apply`
        Joins sba/borrowers_lance.borrstreet_normalized to
        overture/us_places_lance.address_base_normalized.

Step 2: `build_bridge_sba_overture_uspto_lance.py --apply`
        Triple-axis composite: sba_overture_address × sba_uspto_owner
        on shared SBA-borrower identity.

Both scripts log to ops.bridge_generation_runs. No cron schedule — fire
manually via `modal run --detach` after the emit completes.

Secrets:
    dex-db       — DEX_DB_URL_DIRECT for commit lock + bridge ledger.
    bulk-ingest-r2 — R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/sba_overture_bridges_chain_app.py

Run (one-shot):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/sba_overture_bridges_chain_app.py::chain
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import modal

app = modal.App("data-engine-x-sba-overture-bridges-chain")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "pyarrow>=16.0",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/_lib", remote_path="/root/_lib")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _run_bridge(script_name: str, label: str, timeout_s: int) -> dict:
    """Run a bridge build script as a subprocess; capture metrics + tail."""
    script_path = f"/root/scripts/{script_name}"
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()

    try:
        result = subprocess.run(
            [sys.executable, script_path, "--apply"],
            capture_output=True,
            text=True,
            env=os.environ,
            check=False,
            timeout=timeout_s,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "step": label,
            "status": "failed",
            "error": f"{type(e).__name__}: {e}",
            "started_at": started,
            "duration_s": round(time.time() - t0, 1),
        }

    duration = round(time.time() - t0, 1)
    stdout_tail = (result.stdout or "")[-3000:]
    stderr_tail = (result.stderr or "")[-1500:]

    payload = {
        "step": label,
        "status": "succeeded" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "started_at": started,
        "duration_s": duration,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }

    if result.returncode != 0:
        raise RuntimeError(
            f"{label} FAILED (exit={result.returncode}). stderr tail: {stderr_tail[-500:]}"
        )

    return payload


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60 * 60,  # 60 min total — generous for two ~5-min bridges
    memory=16384,  # 16GB — bridges have large Arrow tables in memory
    # No schedule= — one-shot manual trigger only.
)
def chain() -> dict:
    """Run both SBA × Overture bridges sequentially. Bails on first failure."""
    sys.path.insert(0, "/root")
    _bridge_database_url()

    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    results: list[dict] = []

    # Step 1: name+address axis bridge
    r1 = _run_bridge(
        "build_bridge_sba_overture_address_lance.py",
        label="sba_overture_address",
        timeout_s=20 * 60,  # 20 min
    )
    results.append(r1)

    # Step 2: triple-axis composite (depends on Step 1 having landed)
    r2 = _run_bridge(
        "build_bridge_sba_overture_uspto_lance.py",
        label="sba_overture_uspto",
        timeout_s=20 * 60,  # 20 min
    )
    results.append(r2)

    return {
        "chain_status": "succeeded",
        "started_at": started_at,
        "total_duration_s": round(time.time() - t0, 1),
        "steps": results,
    }


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(chain.remote(), indent=2, default=str))
