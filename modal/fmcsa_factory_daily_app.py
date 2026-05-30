"""FMCSA factory daily — fire every FMCSA derivation in derivations.yaml
once per day at 06:00 UTC.

Replaces the legacy `build_fmcsa_*.py` operator-on-demand path with a
single Modal cron that:

  1. Reads scripts/_config/derivations.yaml.
  2. For each derivation whose name starts with `fmcsa_`, invokes
     scripts/run_derivation.py --name <name> --apply.
  3. Each apply writes one R2 Parquet (snapshot=YYYY-MM-DD) and upserts
     ops.current_snapshots.

The 19 FMCSA derivations replace 11 hand-rolled build_fmcsa_*.py scripts +
13 unwired R2 prefixes (7 SMS + 6 AWH, of which 3 are pass-through-only).

L45 status: this cron does NOT invoke any build_*.py script. The
data-engine-x-fmcsa-ingest app (raw-landing) is untouched; raw R2 ingest
runs on its own 15-minute schedule.

Secrets required (Modal):
    dex-db   — DEX_DB_URL_DIRECT for ops.current_snapshots upsert.
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fmcsa_factory_daily_app.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-fmcsa-factory-daily")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("pyyaml", "duckdb", "psycopg[binary]")
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# 16GB / 2h leaves headroom for the larger derivations (inshist ~7M rows,
# vehicle_inspection ~8M rows). Each apply is sequential; sequencing avoids
# spiking R2 PUT throughput.
FACTORY_MEMORY_MB = 16384
FACTORY_TIMEOUT_SECONDS = 2 * 60 * 60

logger = logging.getLogger(__name__)


# Ordering note: fmcsa_carrier_inspection_state_footprint reads from
# fmcsa_vehicle_inspection_essentials (the previous step's R2 output), so
# vehicle_inspection MUST be applied first. Other derivations are
# independent.
FMCSA_DERIVATIONS_IN_ORDER = [
    # Foundation (read raw FMCSA Hive layout, write derived Parquet).
    "fmcsa_carrier_essentials",
    "fmcsa_actpendinsur_essentials",
    "fmcsa_carrier_registrations_essentials",
    "fmcsa_inshist_essentials",
    "fmcsa_rejected_essentials",
    "fmcsa_revocation_essentials",
    "fmcsa_crash_essentials",
    "fmcsa_vehicle_inspection_essentials",
    # Depends on vehicle_inspection_essentials output.
    "fmcsa_carrier_inspection_state_footprint",
    # Pass-through (read raw, write derived).
    "fmcsa_boc3_awh",
    "fmcsa_insur_awh",
    "fmcsa_revocation_awh",
    "fmcsa_sms_ab_pass",
    "fmcsa_sms_ab_pass_property",
    "fmcsa_sms_c_pass",
    "fmcsa_sms_c_pass_property",
    "fmcsa_sms_input_inspection",
    "fmcsa_sms_input_motor_carrier_census",
    "fmcsa_sms_input_violation",
]


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the factory upsert reads
    DEX_DB_URL_DIRECT. Bridge across so run_derivation works."""
    if "DEX_DB_URL_DIRECT" not in os.environ:
        if "DATABASE_URL" in os.environ:
            os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _apply_one(name: str) -> dict[str, Any]:
    """Invoke run_derivation.py --name <name> --apply as a subprocess.
    Returns {name, status, duration_s, stdout_tail}."""
    t0 = time.time()
    cmd = [
        sys.executable,
        "/root/scripts/run_derivation.py",
        "--config", "/root/scripts/_config/derivations.yaml",
        "--name", name,
        "--apply",
    ]
    logger.info("[%s] starting apply", name)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=os.environ,
        check=False,
    )
    duration_s = round(time.time() - t0, 1)
    if result.returncode != 0:
        logger.error(
            "[%s] FAILED in %.1fs (exit=%d)\nstdout:\n%s\nstderr:\n%s",
            name, duration_s, result.returncode,
            result.stdout[-2000:],
            result.stderr[-2000:],
        )
        return {
            "name": name,
            "status": "failed",
            "exit_code": result.returncode,
            "duration_s": duration_s,
            "stdout_tail": result.stdout[-1000:],
            "stderr_tail": result.stderr[-1000:],
        }
    logger.info(
        "[%s] OK in %.1fs\nstdout (tail):\n%s",
        name, duration_s, result.stdout[-500:],
    )
    return {
        "name": name,
        "status": "ok",
        "exit_code": 0,
        "duration_s": duration_s,
        "stdout_tail": result.stdout[-500:],
    }


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=FACTORY_TIMEOUT_SECONDS,
    memory=FACTORY_MEMORY_MB,
    schedule=modal.Cron("0 6 * * *"),  # 06:00 UTC daily
)
def run_factory_daily(only: list[str] | None = None) -> dict[str, Any]:
    """Apply every FMCSA derivation. If `only` is set, restrict to those names.
    Returns a summary dict; raises on any failure so Modal flags the run red.
    """
    _bridge_database_url()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    t0 = time.time()

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    targets = (
        [n for n in FMCSA_DERIVATIONS_IN_ORDER if n in (only or [])]
        if only
        else list(FMCSA_DERIVATIONS_IN_ORDER)
    )
    if only and len(targets) != len(only):
        unknown = set(only) - set(FMCSA_DERIVATIONS_IN_ORDER)
        raise ValueError(f"unknown derivation names: {sorted(unknown)}")

    logger.info("FMCSA factory daily — applying %d derivations", len(targets))
    results: list[dict[str, Any]] = []
    failures: list[str] = []

    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_factory_daily",
        run_id=run_id,
    ) as hb:
        for idx, name in enumerate(targets, start=1):
            hb.set_stage(
                f"derivation_{name}",
                {"index": idx, "total": len(targets), "failures_so_far": len(failures)},
            )
            r = _apply_one(name)
            results.append(r)
            if r["status"] != "ok":
                failures.append(name)

    completed_at = datetime.now(timezone.utc).isoformat()
    duration_s = round(time.time() - t0, 1)
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_s": duration_s,
        "total": len(targets),
        "succeeded": len(targets) - len(failures),
        "failed": len(failures),
        "failure_names": failures,
        "results": results,
    }

    if failures:
        # Raise so Modal marks the cron run as failed; the dashboard sees red.
        raise RuntimeError(
            f"{len(failures)} of {len(targets)} FMCSA derivations failed: "
            f"{failures}"
        )

    logger.info("FMCSA factory daily — ALL OK (%s in %.1fs)", len(targets), duration_s)
    return summary


@app.local_entrypoint()
def main(only: str | None = None) -> None:
    """Manual entrypoint for one-off runs (`modal run`).

    Pass --only fmcsa_carrier_essentials,fmcsa_actpendinsur_essentials to
    restrict to a subset.
    """
    names = [s.strip() for s in only.split(",")] if only else None
    out = run_factory_daily.remote(only=names)
    import json
    print(json.dumps(out, indent=2, default=str))
