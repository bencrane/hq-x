"""FMCSA weekly attribute coverage — Modal cron monitoring material-change-event coverage.

Runs every Monday at 12:00 UTC. Invokes
`scripts/fmcsa/material_attribute_coverage_check.py` and records the outcome.

On all-active (exit 0): writes a heartbeat row to ops.data_source_ingest_runs
  (writer='fmcsa-weekly-coverage', status='succeeded').
On dormancy (exit 1): writes a row to ops.alert_emissions referencing the
  pre-seeded cohort_drift Telegram alert subscription for fmcsa_carrier_essentials.
  Payload text explicitly distinguishes attribute-coverage-dormancy semantics
  (the cohort_drift enum is the closest match per
  ops.alert_kind = {breach, cohort_drift, ingest_failed}; payload text bridges
  the semantic gap for operator clarity).

Secrets required (Modal):
    dex-db — DEX_DB_URL_DIRECT for DB writes.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fmcsa_weekly_coverage_app.py

Manual invocation:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/fmcsa_weekly_coverage_app.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

import modal

app = modal.App("data-engine-x-fmcsa-weekly-coverage")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
]

COVERAGE_TIMEOUT_SECONDS = 5 * 60

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the coverage script reads DEX_DB_URL_POOLED."""
    if "DEX_DB_URL_POOLED" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _record_heartbeat(stdout_tail: str, started_at: str) -> str:
    """INSERT one heartbeat row into ops.data_source_ingest_runs; return run_id."""
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("no DB URL")

    completed_at = datetime.now(timezone.utc).isoformat()
    run_metadata = json.dumps(
        {
            "writer": "fmcsa-weekly-coverage",
            "outcome": "all-attributes-active",
            "stdout_tail": stdout_tail[-2000:],
            "started_at": started_at,
        }
    )

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.data_source_ingest_runs
                    (source_id, started_at, completed_at, status, run_metadata)
                SELECT s.source_id, %s::timestamptz, %s::timestamptz, 'succeeded'::data_source_run_status, %s::jsonb
                  FROM ops.data_sources s
                 WHERE s.display_name = 'fmcsa_carrier_essentials'
                RETURNING run_id
                """,
                (started_at, completed_at, run_metadata),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("fmcsa_carrier_essentials not found in ops.data_sources")
            conn.commit()
            return str(row[0])


def _emit_dormancy_alert(dormant_attrs: list[str], stdout_tail: str) -> None:
    """Find the cohort_drift Telegram alert subscription for FMCSA and emit one row.

    Payload text explicitly says 'attribute-coverage-dormancy' so the operator
    reading the Telegram alert isn't confused by the cohort_drift enum label
    (closest semantic match in the existing enum domain).
    """
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        return

    payload = {
        "writer": "fmcsa-weekly-coverage",
        "alert_semantic": "attribute-coverage-dormancy",
        "dormant_attributes": dormant_attrs,
        "stdout_tail": stdout_tail[-1500:],
        "summary": (
            f"FMCSA attribute coverage DORMANCY: "
            f"{len(dormant_attrs)} attribute(s) emitted zero events in last 7 days: "
            f"{', '.join(dormant_attrs)}"
        ),
    }

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.alert_id
                  FROM ops.alert_subscriptions a
                  JOIN ops.data_sources s ON s.source_id = a.source_id
                 WHERE s.display_name = 'fmcsa_carrier_essentials'
                   AND a.alert_kind = 'cohort_drift'
                   AND a.channel = 'telegram'
                   AND a.enabled = true
                 LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                logger.warning("no cohort_drift alert subscription for FMCSA; skipping alert emission")
                return
            alert_id = row[0]

            cur.execute(
                """
                INSERT INTO ops.alert_emissions (alert_id, alert_payload, delivery_status)
                VALUES (%s, %s::jsonb, 'sent'::alert_delivery_status)
                """,
                (alert_id, json.dumps(payload)),
            )
            conn.commit()


def _parse_dormant_attrs(stderr_text: str) -> list[str]:
    """Extract dormant attribute names from coverage-check stderr line.

    Stderr format (from material_attribute_coverage_check.py):
        DORMANT: N attribute(s) have zero events in last 7 days: ['attr1', 'attr2']
    """
    for line in stderr_text.splitlines():
        if line.startswith("DORMANT:"):
            # Find the list literal at the end.
            try:
                lb = line.index("[")
                rb = line.rindex("]")
                inner = line[lb + 1 : rb]
                # Strip quotes and whitespace per item.
                return [s.strip().strip("'\"") for s in inner.split(",") if s.strip()]
            except ValueError:
                return []
    return []


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=COVERAGE_TIMEOUT_SECONDS,
    schedule=modal.Cron("0 12 * * 1"),  # 12:00 UTC Monday
)
def run_weekly_coverage() -> dict:
    """Run scripts/fmcsa/material_attribute_coverage_check.py and record outcome."""
    _bridge_database_url()
    started_at = datetime.now(timezone.utc).isoformat()

    result = subprocess.run(
        [sys.executable, "/root/scripts/fmcsa/material_attribute_coverage_check.py"],
        capture_output=True,
        text=True,
        env=os.environ,
        check=False,
    )

    summary: dict[str, object] = {
        "started_at": started_at,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
    }

    if result.returncode == 0:
        run_id = _record_heartbeat(result.stdout, started_at)
        summary["outcome"] = "all-attributes-active"
        summary["run_id"] = run_id
    else:
        dormant = _parse_dormant_attrs(result.stderr)
        _emit_dormancy_alert(dormant, result.stdout)
        summary["outcome"] = "dormancy-alert-emitted"
        summary["dormant_attributes"] = dormant
        # Raise so Modal dashboard marks the cron run red — dormancy is a signal worth investigating.
        raise RuntimeError(
            f"FMCSA attribute coverage DORMANCY: {len(dormant)} attribute(s) dormant: {dormant}"
        )

    return summary


@app.local_entrypoint()
def main() -> None:
    """Manual entrypoint for one-off testing (`modal run`)."""
    out = run_weekly_coverage.remote()
    print(json.dumps(out, indent=2, default=str))
