"""USAspending BTREE substrate build — action_date / date_signed index additions.

One-shot Modal wrapper (NO schedule= arg) for building BTREE scalar indices on
the 3 large USAspending Lance datasets. Dispatches per-table via `--table` arg.

Datasets:
    fpds   → transaction_fpds_lance  (~107M rows) → BTREE on action_date
    fabs   → transaction_fabs_lance  (~128M rows) → BTREE on action_date
    awards → awards_lance            (~180M rows) → BTREE on date_signed

Sized at 32 GB (DataFusion sort-spill OOM mitigation per LANCE_BYPASS_SPILLING=true)
following the sam_entities_longitudinal_v2_emit_app.py precedent for >9M-row builds.
awards gets +30 min timeout headroom (90 min) vs fpds/fabs (60 min).

This app has NO ``schedule=`` argument — invoke via:
    doppler run --project hq-all --config prd -- \\
        modal run --detach modal/usaspending_btree_action_date_emit_app.py --table fpds
    (repeat for --table fabs and --table awards)

Secrets:
    dex-db  — DEX_DB_URL_DIRECT for lance_commit_lock advisory lock.
    bulk-ingest-r2   — R2 credentials.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_btree_action_date_emit_app.py
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-usaspending-btree-action-date-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# 32 GB: DataFusion sort-spill OOM mitigation — LANCE_BYPASS_SPILLING=true + 32 GB headroom.
# All 3 builds (107M, 128M, 180M rows) exceed the 9M-row floor from precedent.
MEMORY_MB = 32768

# awards (180M rows) gets +30 min headroom.
TIMEOUT_FPDS_FABS_S = 60 * 60       # 60 min
TIMEOUT_AWARDS_S = 90 * 60          # 90 min

SCRIPT_PATH = "/root/scripts/run_usaspending_btree_action_date_build.py"

logger = logging.getLogger(__name__)

_TABLE_CONFIG = {
    "fpds": {
        "column": "action_date",
        "timeout": TIMEOUT_FPDS_FABS_S,
    },
    "fabs": {
        "column": "action_date",
        "timeout": TIMEOUT_FPDS_FABS_S,
    },
    "awards": {
        "column": "date_signed",
        "timeout": TIMEOUT_AWARDS_S,
    },
}


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)
    # Required BEFORE create_scalar_index — prevents DataFusion sort-spill OOM.
    os.environ["LANCE_BYPASS_SPILLING"] = "true"


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=TIMEOUT_AWARDS_S,  # longest possible; per-table logic inside script
    memory=MEMORY_MB,
)
def build_btree(table: str) -> dict[str, Any]:
    _bridge_database_url()
    _ensure_tmpdir()

    if table not in _TABLE_CONFIG:
        raise ValueError(f"unknown table: {table!r}; must be one of {list(_TABLE_CONFIG)}")

    cfg = _TABLE_CONFIG[table]
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()

    logger.info(
        "starting BTREE build table=%s column=%s at %s (memory=%dMB)",
        table, cfg["column"], started_at, MEMORY_MB,
    )

    try:
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--table", table],
            capture_output=True,
            text=True,
            env=os.environ,
            check=False,
            timeout=cfg["timeout"] - 60,
        )
    except subprocess.TimeoutExpired as e:
        logger.error("BTREE build table=%s timed out after %s", table, e.timeout)
        raise

    duration_s = round(time.time() - t0, 1)
    stdout_tail = result.stdout[-3000:] if result.stdout else ""
    stderr_tail = result.stderr[-3000:] if result.stderr else ""

    if result.returncode != 0:
        logger.error(
            "BTREE build table=%s failed (exit=%d) in %.1fs\nstdout:\n%s\nstderr:\n%s",
            table, result.returncode, duration_s, stdout_tail, stderr_tail,
        )
        raise RuntimeError(
            f"BTREE build for {table} failed (exit={result.returncode}). "
            f"stderr tail: {stderr_tail[-500:]}"
        )

    logger.info("BTREE build table=%s OK in %.1fs\nstdout:\n%s", table, duration_s, stdout_tail)

    return {
        "status": "succeeded",
        "table": table,
        "column": cfg["column"],
        "duration_s": duration_s,
        "started_at": started_at,
        "stdout_tail": stdout_tail[-1500:],
    }


@app.local_entrypoint()
def main(table: str) -> None:
    out = build_btree.remote(table)
    print(json.dumps(out, indent=2, default=str))
