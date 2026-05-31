"""Polaris catalog health-check cron — pings Polaris REST /api/catalog/v1/config
daily (with OAuth2 client_credentials auth) and records each ping as a row in
ops.data_source_ingest_runs so the system-health dashboard can surface Polaris
freshness against its declared 24h SLA.

We hit /api/catalog/v1/config (auth'd) instead of /q/health because Polaris's
Quarkus management interface is build-time-fixed to listen on port 8182, and
Railway exposes only one public port per service (8181). The config endpoint
is a much better health signal anyway: it exercises the OAuth2 token path,
the realm-context resolver, AND the catalog metadata round-trip — if it
returns a JSON body with `defaults`/`overrides`, the catalog is genuinely
serving traffic, not just bound to a port.

Schedule: 06:00 UTC daily. Offset from existing crons (FMCSA refresh 02:00,
epiq dockets 02:15, USAspending daily 05:00) so Polaris pings don't bunch.

Recorded run shape (per ops.data_source_ingest_runs):
  status          'succeeded' | 'failed'
  rows_ingested   0  (synthetic — not an ingest)
  bytes_written   0
  source_publish_at  NULL
  error_message   NULL on success; the response body otherwise
  run_metadata    {writer: 'polaris-health-check', http_status: int, ...}

Secrets required (Modal):
    dex-db                 — DATABASE_URL pooled to data-engine-x.
    polaris-health-check            — POLARIS_PUBLIC_URL +
                                      POLARIS_ROOT_PRINCIPAL_ID +
                                      POLARIS_ROOT_PRINCIPAL_SECRET.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/polaris_health_check_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/polaris_health_check_app.py::health_check
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-polaris-health-check")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]==3.2.3", "requests>=2.31.0")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("polaris-health-check"),
]

DISPLAY_NAME = "polaris_catalog"


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the writer reads DEX_DB_URL_POOLED."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _connect():
    import psycopg  # local import — only available inside Modal image
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ["DATABASE_URL"]
    return psycopg.connect(url, autocommit=True)


def _resolve_source_id() -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
            (DISPLAY_NAME,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"ops.data_sources has no row with display_name={DISPLAY_NAME!r}; "
                "run apps/data-engine-x/scripts/seed_polaris_observability_source.py first"
            )
        return str(row[0])


def _record_start(source_id: str, run_metadata: dict[str, Any]) -> str:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO ops.data_source_ingest_runs
                (source_id, started_at, status, run_metadata)
            VALUES (%s, NOW(), 'running', %s)
            RETURNING run_id
            """,
            (source_id, json.dumps(run_metadata)),
        ).fetchone()
        assert row is not None
        return str(row[0])


def _record_complete(
    run_id: str,
    *,
    status: str,
    rows_ingested: int = 0,
    bytes_written: int = 0,
    error_message: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    metadata_patch = json.dumps(extra_metadata or {})
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ops.data_source_ingest_runs
            SET status        = %s,
                completed_at  = NOW(),
                rows_ingested = %s,
                bytes_written = %s,
                error_message = %s,
                run_metadata  = run_metadata || %s::jsonb
            WHERE run_id = %s
            """,
            (status, rows_ingested, bytes_written, error_message,
             metadata_patch, run_id),
        )


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=120,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron("0 6 * * *"),  # daily 06:00 UTC
)
def health_check() -> dict[str, Any]:
    """Ping Polaris REST /q/health. Record run state in the observability ledger."""
    import requests  # noqa: F401  (modal image install)

    _bridge_database_url()

    polaris_url = os.environ.get("POLARIS_PUBLIC_URL", "").rstrip("/")
    if not polaris_url:
        # Without the URL, we can still write a 'failed' row to surface the
        # missing-config state on the dashboard. But we need the source_id
        # first, so this branch is best-effort.
        try:
            source_id = _resolve_source_id()
            run_id = _record_start(source_id, {"writer": "polaris-health-check"})
            _record_complete(
                run_id, status="failed",
                error_message="POLARIS_PUBLIC_URL not set",
            )
        except Exception as e:
            print(f"could not record missing-URL failure: {e}", file=sys.stderr)
        return {"status": "failed", "error": "POLARIS_PUBLIC_URL not set"}

    source_id = _resolve_source_id()
    run_metadata = {
        "writer": "polaris-health-check",
        "polaris_url": polaris_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    run_id = _record_start(source_id, run_metadata)

    client_id = os.environ.get("POLARIS_ROOT_PRINCIPAL_ID")
    client_secret = os.environ.get("POLARIS_ROOT_PRINCIPAL_SECRET")
    if not client_id or not client_secret:
        _record_complete(
            run_id, status="failed",
            error_message="POLARIS_ROOT_PRINCIPAL_ID/SECRET not set",
        )
        return {"status": "failed", "error": "missing principal creds"}

    try:
        # 1. OAuth2 token
        tok_resp = requests.post(
            f"{polaris_url}/api/catalog/v1/oauth/tokens",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "PRINCIPAL_ROLE:ALL",
            },
            timeout=30,
        )
        if tok_resp.status_code != 200:
            _record_complete(
                run_id, status="failed",
                error_message=f"oauth token failed (status={tok_resp.status_code}): {tok_resp.text[:300]}",
                extra_metadata={"http_status": tok_resp.status_code, "step": "oauth_token"},
            )
            return {"status": "failed", "http_status": tok_resp.status_code, "step": "oauth_token"}
        token = tok_resp.json().get("access_token")

        # 2. /api/catalog/v1/config?warehouse=<name> — exercises realm-context
        # + catalog metadata. The warehouse query param is required (Polaris
        # 1.4+); without it the server returns 400 "Please specify a warehouse".
        cfg_resp = requests.get(
            f"{polaris_url}/api/catalog/v1/config",
            params={"warehouse": os.environ.get("POLARIS_DEFAULT_CATALOG_NAME", "polaris_catalog")},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        body_snippet = cfg_resp.text[:500]
        ok = (cfg_resp.status_code == 200
              and ('defaults' in body_snippet or 'overrides' in body_snippet))
        if ok:
            _record_complete(
                run_id, status="succeeded",
                extra_metadata={"http_status": cfg_resp.status_code,
                                "response_snippet": body_snippet},
            )
            return {"status": "succeeded", "http_status": cfg_resp.status_code}
        _record_complete(
            run_id, status="failed",
            error_message=f"config endpoint unexpected (status={cfg_resp.status_code}): {body_snippet}",
            extra_metadata={"http_status": cfg_resp.status_code,
                            "response_snippet": body_snippet, "step": "config"},
        )
        return {"status": "failed", "http_status": cfg_resp.status_code,
                "body": body_snippet}
    except Exception as e:
        _record_complete(
            run_id, status="failed",
            error_message=f"{type(e).__name__}: {e}",
            extra_metadata={"exception_type": type(e).__name__},
        )
        return {"status": "failed", "error": str(e)}


@app.local_entrypoint()
def main() -> None:
    """Local-dev entrypoint:
        modal run modal/polaris_health_check_app.py::health_check
    invokes health_check.remote() — this is the manual-trigger path.
    """
    result = health_check.remote()
    print(json.dumps(result, indent=2))
