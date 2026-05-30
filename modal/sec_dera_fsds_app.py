"""Modal app: data-engine-x-sec-dera-fsds.

Wraps scripts/run_sec_dera_fsds_r2_ingest.py for unattended cadence.

Daily cron at 04:00 UTC (FSDS publishes ~13 days after quarter-end;
daily probe + L4 idempotency keeps no-op re-runs cheap — most runs are
"no quarter Last-Modified delta → no-op").

Entrypoints:
  - daily_incremental()           — cron-fired, skip-if-unchanged
  - run_fsds_backfill(quarters=None) — manual backfill via `modal run --detach` per L47
  - emit_pre_lance()              — 32GB Modal for ~50M-row fsds_pre_lance BTREE creation
  - emit_num_lance()              — 64GB/4h Modal for ~200M-row fsds_num_lance (CRITICAL sizing)

Volume: sec-dera-fsds-state (per-quarter checkpoint cache).
Secrets: dex-db (DEX_DB_URL_DIRECT), bulk-ingest-r2 (R2_*).
Image: duckdb + boto3 + httpx + psycopg[binary] + pyarrow + beautifulsoup4 + lxml.
Memory: 8 GB base (small ingest); 32 GB for emit_pre_lance; 64 GB for emit_num_lance.

Backfill invocation (~2h wall-clock for 69 quarters × 4 tables):
  doppler run --project hq-all --config prd -- \\
    modal run --detach apps/data-engine-x/modal/sec_dera_fsds_app.py::run_fsds_backfill

DO NOT pipe through `tee | tail` for long-running jobs — SIGPIPE kills the process.
Use background redirect + separate tail per L60 lesson from sub-A.
"""
import pathlib
import uuid

import modal

app = modal.App("data-engine-x-sec-dera-fsds")

_LANDING_DIR = str(pathlib.Path(__file__).parent / "landing")

image = (
    modal.Image.debian_slim()
    .pip_install(
        "duckdb",
        "boto3",
        "httpx",
        "psycopg[binary]",
        "pyarrow",
        "beautifulsoup4",
        "lxml",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir(_LANDING_DIR, remote_path="/root/landing")
)

volume = modal.Volume.from_name("sec-dera-fsds-state", create_if_missing=True)

secrets = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    volumes={"/state": volume},
    secrets=secrets,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 4 * * *"),
    memory=8192,
    timeout=3600,
)
def daily_incremental() -> None:
    """Cron-fired incremental: probes DERA FSDS index, skip-if-unchanged."""
    import sys
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.run_sec_dera_fsds_r2_ingest import main as ingest_main
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="daily_incremental",
        run_id=run_id,
    ) as hb:
        hb.set_stage("r2_ingest_skip_if_unchanged")
        ingest_main(["--apply", "--skip-if-unchanged"])


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    volumes={"/state": volume},
    secrets=secrets,
    memory=8192,
    timeout=14400,  # 4h — FSDS num.txt transcode is ~30x slower per quarter than Form D
)
def run_fsds_backfill(quarters: list[str] | None = None) -> None:
    """Manual backfill — invoke via `modal run --detach`.

    quarters=None  =>  all discovered quarters from DERA FSDS index (69 quarters back to 2009q1).
    quarters=["2009q1","2009q2"]  =>  scoped subset.

    Wall-clock estimate: ~2h for full 69-quarter × 4-table backfill.
    FSDS num.txt is ~559MB/quarter; DuckDB transcode dominates.
    """
    import sys
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.run_sec_dera_fsds_r2_ingest import main as ingest_main
    args = ["--apply"]
    if quarters:
        args += ["--quarters", ",".join(quarters)]
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_fsds_backfill",
        run_id=run_id,
    ) as hb:
        hb.set_stage("backfill", {"quarters": quarters})
        ingest_main(args)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=secrets,
    memory=32768,   # 32GB for DataFusion sort-spill at BTREE creation (~50M rows, PR #464 precedent)
    timeout=7200,   # 2h
)
def emit_pre_lance() -> None:
    """Emit fsds_pre_lance (~50M rows historical). Requires 32GB Modal.

    Run via:
        doppler run --project hq-all --config prd -- \\
          modal run apps/data-engine-x/modal/sec_dera_fsds_app.py::emit_pre_lance
    """
    import sys
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.run_sec_dera_fsds_pre_lance_emit import main as emit_main
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit_pre_lance",
        run_id=run_id,
    ) as hb:
        hb.set_stage("pre_lance_emit")
        emit_main(["--apply"])


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=secrets,
    memory=65536,    # 64GB — primary + secondary BTREE creation on ~200M rows
    timeout=14400,   # 4h — DataFusion sort-spill + Arrow read of 4-5GB Parquet shards
)
def emit_num_lance() -> None:
    """Emit fsds_num_lance (~200-350M rows historical). CRITICAL: requires 64GB Modal / 4h.

    Local execution is INFEASIBLE (>32GB RAM required).
    Run via (--detach recommended; 2-3h wall-clock):
        doppler run --project hq-all --config prd -- \\
          modal run --detach apps/data-engine-x/modal/sec_dera_fsds_app.py::emit_num_lance
    Monitor via: modal app logs data-engine-x-sec-dera-fsds
    """
    import sys
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.run_sec_dera_fsds_num_lance_emit import main as emit_main
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit_num_lance",
        run_id=run_id,
    ) as hb:
        hb.set_stage("num_lance_emit")
        emit_main(["--apply"])
