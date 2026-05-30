"""California UCC Bulk Ingest — Modal app wrapper.

Wraps `scripts/run_ucc_ca_ingest.py` as a Modal function so the operator
can fire ingests on demand without provisioning local Python deps. The
script itself contains all logic; this app exists for two reasons:

  1. Operator can `modal run modal/ucc_ca_ingest_app.py::run_ca_ucc_ingest
     --mode initial-dump --input-uri s3://...` from any machine with the
     Modal CLI.
  2. When the operator establishes the weekly-delta drop cadence (CA SOS
     publishes a free weekly download), uncomment the `schedule=` arg
     below to wire a `modal.Cron` slot for the recurring pull. The TODO
     comment marks the exact line.

Secrets (reused from existing sibling apps):
  - `dex-db` — DATABASE_URL pooled to data-engine-x. The Phase 0a
    observability ledger AND the existing ops.ucc_r2_ingest_runs both live
    here. Reused from FMCSA ingest because it's the same Postgres.
  - `bulk-ingest-r2` — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal deploy modal/ucc_ca_ingest_app.py

Manual run (operator drops bulk file at s3://... then calls):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run modal/ucc_ca_ingest_app.py::run_ca_ucc_ingest \\
        --mode initial-dump \\
        --input-uri s3://dex-raw-landing-zone/ucc/state=CA/initial-dump-incoming/master.zip

See `apps/data-engine-x/docs/ucc_ca_ingest.md` for the full operator flow
and `~/Desktop/hq/directives/2026-05-12-hq-all-ucc-ca-ingest-scaffold.md`
for the directive.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import modal

app = modal.App("data-engine-x-ucc-ca-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# 8GB / 2h sized for the Master Unload (multi-million-row historical dump).
# Weekly deltas are far smaller and fit comfortably; 2h is a defensive cap.
INGEST_MEMORY_MB = 8192
INGEST_TIMEOUT_SECONDS = 2 * 60 * 60


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the script expects DEX_DB_URL_POOLED.
    Mirror across so both readers work."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # TODO(weekly-delta-cadence): once operator establishes the bizfile
    # weekly-delta cadence, uncomment one of:
    #   schedule=modal.Cron("0 8 * * 1"),  # Mondays 08:00 UTC
    # and adjust the cron expression to match the CA SOS publishing day.
)
def run_ca_ucc_ingest(
    mode: str = "weekly-delta",
    input_uri: str | None = None,
    snapshot_date: str | None = None,
    max_rows: int | None = None,
    r2_prefix_override: str | None = None,
    skip_if_exists: bool = False,
) -> dict:
    """Modal entry point — wraps `scripts/run_ucc_ca_ingest.py:ingest`.

    Parameters mirror the CLI flags. `input_uri` is required for all real
    runs (can be local-to-Modal or s3://). When called via cron with no
    args, defaults to weekly-delta mode but will fail without an input URI
    — the operator must wire input_uri into the cron invocation once the
    drop location is established.
    """
    _bridge_database_url()

    if input_uri is None:
        raise RuntimeError(
            "input_uri is required. Operator must drop the bulk file at "
            "s3://dex-raw-landing-zone/ucc/state=CA/<incoming>/<file>.zip "
            "and pass that URI."
        )

    target_snapshot: date
    if snapshot_date:
        target_snapshot = date.fromisoformat(snapshot_date)
    else:
        target_snapshot = datetime.now(timezone.utc).date()

    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    from pathlib import Path
    import tempfile
    import uuid as _uuid

    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_ucc_ca_ingest import ingest  # noqa: E402

    workdir = Path(tempfile.mkdtemp(prefix="ucc_ca_ingest_"))
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_ca_ucc_ingest",
        run_id=run_id,
    ) as hb:
        hb.set_stage("ucc_ca_ingest", {"mode": mode, "snapshot_date": target_snapshot.isoformat()})
        rc = ingest(
            mode=mode,
            input_uri=input_uri,
            snapshot_date=target_snapshot,
            workdir=workdir,
            max_rows=max_rows,
            r2_prefix_override=r2_prefix_override,
            skip_if_exists=skip_if_exists,
        )

    return {
        "run_id": run_id,
        "mode": mode,
        "input_uri": input_uri,
        "snapshot_date": target_snapshot.isoformat(),
        "exit_code": rc,
        "ok": rc == 0,
    }


@app.local_entrypoint()
def main(
    mode: str = "weekly-delta",
    input_uri: str | None = None,
    snapshot_date: str | None = None,
    max_rows: int | None = None,
    r2_prefix_override: str | None = None,
    skip_if_exists: bool = False,
) -> None:
    """`modal run` entry point. Forwards to the remote function."""
    result = run_ca_ucc_ingest.remote(
        mode=mode,
        input_uri=input_uri,
        snapshot_date=snapshot_date,
        max_rows=max_rows,
        r2_prefix_override=r2_prefix_override,
        skip_if_exists=skip_if_exists,
    )
    print(result)
