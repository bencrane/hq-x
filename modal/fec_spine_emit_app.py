"""Modal wrapper: FEC spine Lance emit (transaction grain + donor rolodex).

Thin wrapper that runs the spine builders with enough memory/CPU for the full
281M-row FEC individual-contributions corpus + 12 BTREE index builds — work
that does not belong on a laptop over a residential link (the aborted-write
failure mode we are fixing is exactly what fragile local execution causes).

Targets:
  spine  -> scripts/build_fec_individual_contributions_spine_lance.py
            (transaction grain, PK sub_id, the canonical join axis)
  donors -> scripts/build_fec_donors_lance.py
            (derived person rolodex, PK person_key)

Polaris registration is intentionally NOT done here (--skip-polaris); it is a
tiny HTTP call run locally under doppler after the write, keeping this app's
secret surface to dex-db (heartbeat) + bulk-ingest-r2 (R2) only.

Resources: 96GB / 16 CPU / 6h. Lance index-build + compact are CPU-bound;
LANCE_BYPASS_SPILLING avoids slow spills to Modal ephemeral disk.

Dispatch (multi-hour job — use --detach):
  cd ~/hq-all/apps/data-engine-x && \\
    doppler run --project hq-all --config prd -- \\
    modal run --detach modal/fec_spine_emit_app.py --target spine \\
      --row-floor 270000000

  # plan only (counts, image/deps/R2 validation, no write):
  ... modal run modal/fec_spine_emit_app.py --target spine --dry-run

Deploy:
  ... modal deploy modal/fec_spine_emit_app.py
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid

import modal

app = modal.App("data-engine-x-fec-spine-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb>=1.5,<1.6",
        "pylance>=6,<7",
        "pyarrow",
        "nameparser",
        "unidecode",
        "psycopg[binary]",
    )
    .add_local_dir("scripts", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("dex-db"),          # DEX_DB_URL_* for HeartbeatLoop
    modal.Secret.from_name("bulk-ingest-r2"),  # R2_ENDPOINT / KEY / SECRET
]

EMIT_MEMORY_MB = 98_304       # 96 GB — headroom for 12.5M-name parse + 281M streaming join
EMIT_CPU = 16                 # Lance BTREE sort + compact are CPU-bound
EMIT_TIMEOUT = 6 * 60 * 60    # 6h

_SCRIPTS = {
    "spine": "build_fec_individual_contributions_spine_lance.py",
    "donors": "build_fec_donors_lance.py",
}

logger = logging.getLogger(__name__)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=EMIT_MEMORY_MB,
    cpu=EMIT_CPU,
    timeout=EMIT_TIMEOUT,
)
def build(
    target: str = "spine",
    dry_run: bool = False,
    cycles: str = "",
    row_floor: int = 0,
    max_rows_per_file: int = 1_000_000,
) -> None:
    if target not in _SCRIPTS:
        raise ValueError(f"unknown target {target!r}; expected one of {sorted(_SCRIPTS)}")

    os.environ["PYTHONPATH"] = "/root" + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )
    os.environ.setdefault("TMPDIR", "/tmp/lance")
    os.makedirs("/tmp/lance", exist_ok=True)
    sys.path.insert(0, "/root")

    from landing.ledger import HeartbeatLoop  # noqa: E402

    cmd = [
        sys.executable,
        f"/root/scripts/{_SCRIPTS[target]}",
        "--dry-run" if dry_run else "--apply",
        "--workers", "1",            # serial parse: ~2-3min for 12.5M; dodges Pool/spawn risk
        "--skip-polaris",            # registered locally under doppler post-write
        "--max-rows-per-file", str(max_rows_per_file),
    ]
    if cycles:
        cmd += ["--cycles", cycles]
    if row_floor:
        cmd += ["--row-floor", str(row_floor)]

    logger.info("Running: %s", " ".join(cmd))
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function=f"build_{target}",
        run_id=run_id,
        interval_seconds=60,
    ) as hb:
        hb.set_stage("subprocess_build", {"target": target, "dry_run": dry_run, "cycles": cycles})
        result = subprocess.run(cmd, env=os.environ.copy())
    if result.returncode != 0:
        raise RuntimeError(f"{_SCRIPTS[target]} exited with code {result.returncode}")
    logger.info("build complete (target=%s dry_run=%s)", target, dry_run)


@app.local_entrypoint()
def main(
    target: str = "spine",
    dry_run: bool = False,
    cycles: str = "",
    row_floor: int = 0,
) -> None:
    build.remote(target=target, dry_run=dry_run, cycles=cycles, row_floor=row_floor)
