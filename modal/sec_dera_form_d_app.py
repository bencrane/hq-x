"""Modal app: data-engine-x-sec-dera-form-d.

Wraps scripts/run_sec_dera_form_d_r2_ingest.py for unattended cadence.

Daily cron at 04:00 UTC (DERA publishes ~6 days after quarter-end;
daily probe + L4 idempotency keeps no-op re-runs cheap — most runs are
"no quarter Last-Modified delta → no-op").

Two entrypoints:
  - daily_incremental()      — cron-fired, skip-if-unchanged
  - run_form_d_backfill(quarters=None) — manually triggered, full backfill
                                          via `modal run --detach` per L47

Volume: sec-dera-form-d-state (per-quarter checkpoint cache).
Secrets: dex-db (DEX_DB_URL_DIRECT), bulk-ingest-r2 (R2_*).
Image: pyarrow + duckdb + httpx + psycopg + boto3 + beautifulsoup4.
Memory: 8 GB (small Form D ingest; 32 GB only for c7 Lance emit not for c1 ingest).
Timeout: 7200 (allows full ~73-quarter backfill in single container).
"""
import pathlib
import uuid

import modal

app = modal.App("data-engine-x-sec-dera-form-d")

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
    )
    .add_local_dir(_LANDING_DIR, remote_path="/root/landing")
)

volume = modal.Volume.from_name("sec-dera-form-d-state", create_if_missing=True)

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
    """Cron-fired incremental: probes DERA index, skip-if-unchanged."""
    import sys
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.run_sec_dera_form_d_r2_ingest import main as ingest_main
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
    timeout=7200,
)
def run_form_d_backfill(quarters: list[str] | None = None) -> None:
    """Manual backfill — invoke via `modal run --detach`.

    quarters=None  =>  all discovered quarters from DERA index.
    quarters=["2008q1","2008q2"]  =>  scoped subset.
    """
    import sys
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.run_sec_dera_form_d_r2_ingest import main as ingest_main
    args = ["--apply"]
    if quarters:
        args += ["--quarters", ",".join(quarters)]
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_form_d_backfill",
        run_id=run_id,
    ) as hb:
        hb.set_stage("backfill", {"quarters": quarters})
        ingest_main(args)
