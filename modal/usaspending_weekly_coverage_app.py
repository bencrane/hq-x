"""USAspending weekly attribute coverage — Modal cron monitoring material-change-event coverage.

Cycle: usaspending-pipeline-remediation (2026-05-13). Parallel to
modal/fmcsa_weekly_coverage_app.py.

Runs every Monday at 12:30 UTC (30 min after FMCSA weekly @ 12:00 to stagger).
Invokes `scripts/usaspending/material_attribute_coverage_check.py` and records
the outcome.

On all-active (exit 0): writes heartbeat row to ops.data_source_ingest_runs.
On dormancy (exit 1): writes row to ops.alert_emissions referencing the
  pre-seeded cohort_drift Telegram alert subscription for
  'usaspending_contracts_lance'.

COLD-START EXPECTATION: On the first weekly cron run after deploy, ALL 4
USAspending declarations will be dormant (no detection runs have wired the
resolver against ≥2 snapshots yet). Operator should expect a single
cohort_drift alert on the first Monday post-deploy.

Secrets required (Modal):
    dex-db — DEX_DB_URL_DIRECT for DB writes (shared DEX DB).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_weekly_coverage_app.py

Manual invocation:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_weekly_coverage_app.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import modal

app = modal.App("data-engine-x-usaspending-weekly-coverage")

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
    if "DEX_DB_URL_POOLED" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _record_heartbeat(stdout_tail: str, started_at: str) -> str:
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("no DB URL")

    completed_at = datetime.now(timezone.utc).isoformat()
    run_metadata = json.dumps(
        {
            "writer": "usaspending-weekly-coverage",
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
                 WHERE s.display_name = 'usaspending_contracts_lance'
                RETURNING run_id
                """,
                (started_at, completed_at, run_metadata),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("usaspending_contracts_lance not found in ops.data_sources")
            conn.commit()
            return str(row[0])


def _emit_dormancy_alert(dormant_attrs: list[str], stdout_tail: str) -> None:
    """Find cohort_drift Telegram subscription for USAspending; emit one row.

    Payload distinguishes 'attribute-coverage-dormancy' semantics for clarity
    (cohort_drift is closest enum match per ops.alert_kind domain).
    """
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        return

    payload = {
        "writer": "usaspending-weekly-coverage",
        "alert_semantic": "attribute-coverage-dormancy",
        "dormant_attributes": dormant_attrs,
        "stdout_tail": stdout_tail[-1500:],
        "summary": (
            f"USAspending attribute coverage DORMANCY: "
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
                 WHERE s.display_name = 'usaspending_contracts_lance'
                   AND a.alert_kind = 'cohort_drift'
                   AND a.channel = 'telegram'
                   AND a.enabled = true
                 LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                logger.warning("no cohort_drift alert subscription for USAspending; skipping alert emission")
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

    Stderr format (from scripts/usaspending/material_attribute_coverage_check.py):
        FAIL: N dormant attribute(s): name1, name2, name3
    """
    pattern = re.compile(r"^FAIL:\s*\d+\s*dormant attribute\(s\):\s*(.+)$")
    for line in stderr_text.splitlines():
        match = pattern.match(line.strip())
        if match:
            names = match.group(1)
            return [n.strip() for n in names.split(",") if n.strip()]
    return []


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=COVERAGE_TIMEOUT_SECONDS,
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=modal.Cron("30 12 * * 1"),  # Monday 12:30 UTC (30min after FMCSA)
)
def run_weekly_coverage() -> dict:
    """Run scripts/usaspending/material_attribute_coverage_check.py and record outcome."""
    _bridge_database_url()
    started_at = datetime.now(timezone.utc).isoformat()

    result = subprocess.run(
        [sys.executable, "/root/scripts/usaspending/material_attribute_coverage_check.py"],
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
        raise RuntimeError(
            f"USAspending attribute coverage DORMANCY: {len(dormant)} attribute(s) dormant: {dormant}"
        )

    return summary


@app.local_entrypoint()
def main() -> None:
    """Manual entrypoint for one-off testing (`modal run`)."""
    out = run_weekly_coverage.remote()
    print(json.dumps(out, indent=2, default=str))
