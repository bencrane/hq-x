"""USAspending capital matrix (SAM × awards) → Lance emit (manual one-shot).

INNER JOIN spines.sam_entities_lance × usaspending.awards_lance on UEI,
WHERE total_obligation > 0. 13-col output: SAM identity (uei, cage_code,
legal_business_name) + award identity (generated_unique_award_id, piid, fain,
award_type) + award provenance (awarding_toptier_agency_name,
funding_subtier_agency_name, contract_signed_date, contract_end_date) +
financial (total_obligated_usd, potential_total_value_usd).

No cron schedule — manual one-shot trigger via `modal run`. Cron can be wired
later once refresh cadence is decided (likely weekly given the awards_lance
parent refresh cycle).

Secrets:
    dex-db                — DEX_DB_URL_DIRECT for lance_commit_lock advisory.
    bulk-ingest-r2        — R2 credentials.
    polaris-health-check  — POLARIS_PUBLIC_URL + ROOT_PRINCIPAL_ID/SECRET +
                            POLARIS_DEFAULT_CATALOG_NAME.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_capital_matrix_lance_emit_app.py

Trigger one-shot:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_capital_matrix_lance_emit_app.py
"""
from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import time

import modal

app = modal.App("data-engine-x-usaspending-capital-matrix-lance-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "boto3",
        "requests",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("polaris-health-check"),
]

# JOIN of SAM (876K UEIs) × awards (179M-row filter via BTREE) × DISTINCT.
# Heavier than recipient_grain (4GB), comparable to contracts_lance emit
# (16GB). 32GB gives headroom for DuckDB hash-join state + Arrow buffers.
EMIT_MEMORY_MB = 16 * 1024  # 16GB — matches contracts_lance emit; no CPU spec (use Modal default scheduling tier)
EMIT_TIMEOUT_SECONDS = 3 * 60 * 60  # 3h

DISPLAY_NAME = "usaspending_capital_matrix_lance"
SCRIPT_PATH = "/root/scripts/run_usaspending_capital_matrix_lance_emit.py"

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=EMIT_TIMEOUT_SECONDS,
    memory=EMIT_MEMORY_MB,
)
def run_emit(
    apply: bool = True,
    target_path_override: str | None = None,
    sandbox: bool = False,
) -> dict:
    _bridge_database_url()
    _ensure_tmpdir()

    t0 = time.time()
    cmd = [sys.executable, SCRIPT_PATH]
    if apply:
        cmd.append("--apply")
    if sandbox:
        cmd.append("--sandbox")
    if target_path_override:
        cmd += ["--target-path-override", target_path_override]

    result = subprocess.run(
        cmd, capture_output=True, text=True, env=os.environ,
        check=False, timeout=EMIT_TIMEOUT_SECONDS - 60,
    )
    duration_s = round(time.time() - t0, 1)
    stdout_tail = result.stdout[-3000:] if result.stdout else ""
    stderr_tail = result.stderr[-3000:] if result.stderr else ""

    if result.returncode != 0:
        logger.error(
            "emit FAILED (exit=%d) in %.1fs\nstdout tail:\n%s\nstderr tail:\n%s",
            result.returncode, duration_s, stdout_tail, stderr_tail,
        )
        raise RuntimeError(
            f"{DISPLAY_NAME} emit exited {result.returncode}: "
            f"{stderr_tail[-500:] if stderr_tail else stdout_tail[-500:]}"
        )

    metrics: dict = {}
    for line in result.stdout.splitlines():
        if line.startswith("OK — metrics:"):
            try:
                metrics = json.loads(line.split("metrics:", 1)[1].strip())
            except Exception:
                try:
                    metrics = ast.literal_eval(line.split("metrics:", 1)[1].strip())
                except Exception:
                    pass

    logger.info("emit OK in %.1fs (rows=%s)", duration_s, metrics.get("lance_rows"))
    return {
        "status": "succeeded",
        "duration_s": duration_s,
        "metrics": metrics,
        "stdout_tail": stdout_tail[-1500:],
    }


@app.local_entrypoint()
def main(apply: bool = True, sandbox: bool = False) -> None:
    out = run_emit.remote(apply=apply, sandbox=sandbox)
    print(json.dumps(out, indent=2, default=str))
