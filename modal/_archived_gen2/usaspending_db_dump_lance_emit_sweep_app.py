"""Modal wrapper: USAspending db-dump → per-table Lance emit sweep.

Thin wrapper around
``apps/data-engine-x/scripts/run_usaspending_db_dump_lance_emit_sweep.py``
for running large-table emits (awards ~34GB, transaction_fpds ~27GB,
transaction_fabs ~21GB) with sufficient memory + CPU on Modal infrastructure.

Resources: 64GB RAM / 8 CPU / 6h timeout — enough for the largest single
table in one container.  Run with --detach per L47 (multi-hour job).

IMPORTANT: This app does NOT call Polaris registration. Per the directive
carve-out: "DO NOT register any new Lance dataset in Polaris." Do NOT import
or reference init_polaris_lance_generic.py or _register_polaris_tables here.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for the lance_commit_lock (Postgres
                         advisory lock).
    bulk-ingest-r2     — R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.
    NO polaris-health-check — out of scope for this cycle.

Dispatch (multi-hour job; use --detach):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/usaspending_db_dump_lance_emit_sweep_app.py \\
        --table awards

    # Or all 10 tables in one run (not recommended — 6h budget):
    doppler run --project hq-all --config prd -- \\
        modal run --detach modal/usaspending_db_dump_lance_emit_sweep_app.py

    # Dry run (count rows, no write):
    doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_db_dump_lance_emit_sweep_app.py --dry-run \\
        --table awards

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_db_dump_lance_emit_sweep_app.py
"""
from __future__ import annotations

import logging
import os
import sys

import modal

app = modal.App("data-engine-x-usaspending-db-dump-lance-emit-sweep")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "pyarrow",
        "boto3",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# 64 GB RAM / 8 CPU / 6h timeout
# Chosen to handle the largest table (awards ~34 GB Parquet → ~180M rows Lance).
# Lance's index-build and compact_files are CPU-bound; 8 cores saturates the
# Arrow sort path. LANCE_BYPASS_SPILLING=true prevents DataFusion from writing
# to disk (which would be slow on Modal's ephemeral storage).
EMIT_MEMORY_MB = 65_536   # 64 GB
EMIT_CPU = 8
EMIT_TIMEOUT = 6 * 60 * 60  # 6h

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=EMIT_MEMORY_MB,
    cpu=EMIT_CPU,
    timeout=EMIT_TIMEOUT,
)
def emit_sweep(table: str | None = None, dry_run: bool = False) -> None:
    """Run the Lance emit sweep for one or all target tables.

    Args:
        table:   If set, emit only this table (e.g. "awards"). Otherwise all 10.
        dry_run: If True, count rows and print plan; do not write Lance dataset.
    """
    import subprocess
    import uuid as _uuid

    _bridge_database_url()
    _ensure_tmpdir()

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    # Build the command
    cmd = [
        sys.executable,
        "/root/scripts/run_usaspending_db_dump_lance_emit_sweep.py",
    ]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.append("--apply")
    if table:
        cmd.extend(["--table", table])

    logger.info("Running: %s", " ".join(cmd))
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit_sweep",
        run_id=run_id,
    ) as hb:
        hb.set_stage("subprocess_emit", {"table": table, "dry_run": dry_run})
        result = subprocess.run(cmd, env=os.environ.copy())
    if result.returncode != 0:
        raise RuntimeError(
            f"Sweep script exited with code {result.returncode} "
            f"(table={table!r}, dry_run={dry_run})"
        )
    logger.info("Sweep completed successfully (table=%r, dry_run=%r)", table, dry_run)


@app.local_entrypoint()
def run(
    table: str = "",
    dry_run: bool = False,
) -> None:
    """Local entrypoint for `modal run`.

    Args:
        --table    Table to emit (empty = all 10).
        --dry-run  Count rows, print plan, no write.

    Examples:
        modal run --detach modal/usaspending_db_dump_lance_emit_sweep_app.py --table awards
        modal run modal/usaspending_db_dump_lance_emit_sweep_app.py --dry-run --table awards
    """
    table_arg = table or None
    emit_sweep.remote(table=table_arg, dry_run=dry_run)
