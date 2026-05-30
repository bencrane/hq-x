"""Throwaway probe — compare dex-db vs dex-db Modal secrets.

Verifies what each secret injects, that both resolve to the same Postgres,
and that each role has the rights the USAspending Lance cron needs
(INSERT/UPDATE/SELECT on bulk_ingest.feed_ingest_runs + advisory lock).

Run::

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/db_secret_probe_app.py::run

Safe to delete once the dex-db migration ships.
"""
from __future__ import annotations

import json

import modal

app = modal.App("data-engine-x-db-secret-probe")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]>=3.2")
)

DB_ENV_PREFIXES = ("DATABASE", "DEX_DB", "DB_", "POSTGRES", "PG", "SUPABASE")


def _probe_impl(label: str) -> dict:
    import os

    import psycopg

    db_envs = sorted(
        k for k in os.environ
        if any(k.upper().startswith(p) for p in DB_ENV_PREFIXES)
    )

    candidates = ["DEX_DB_URL_DIRECT", "DEX_DB_URL_POOLED", "DATABASE_URL"]
    url = None
    url_var = None
    for v in candidates:
        if os.environ.get(v):
            url = os.environ[v]
            url_var = v
            break

    # Redact: keep scheme + host hash, drop user/pass/dbname
    def _redact(u: str | None) -> str | None:
        if not u:
            return None
        from urllib.parse import urlparse
        try:
            p = urlparse(u)
            return f"{p.scheme}://<user>:<pass>@{p.hostname}:{p.port}/<db>?{(p.query[:60] + '...') if p.query else ''}"
        except Exception:
            return "(unredactable)"

    result: dict = {
        "label": label,
        "db_env_keys": db_envs,
        "connection_url_picked_from": url_var,
        "url_redacted": _redact(url),
    }

    if not url:
        result["connect_error"] = "no DB URL env var found"
        return result

    try:
        with psycopg.connect(url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_database(), current_user, "
                    "inet_server_addr()::text, current_setting('server_version')"
                )
                row = cur.fetchone()
                result["current_database"] = row[0]
                result["current_user"] = row[1]
                result["server_addr"] = row[2]
                result["server_version"] = row[3]

                for priv in ("SELECT", "INSERT", "UPDATE"):
                    cur.execute(
                        "SELECT has_table_privilege(current_user, "
                        "'bulk_ingest.feed_ingest_runs', %s)",
                        (priv,),
                    )
                    result[f"bulk_ingest_feed_ingest_runs_{priv.lower()}"] = cur.fetchone()[0]

                try:
                    cur.execute("SELECT pg_try_advisory_xact_lock(424242)")
                    result["advisory_lock_xact"] = cur.fetchone()[0]
                except Exception as e:  # noqa: BLE001
                    result["advisory_lock_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    except Exception as e:  # noqa: BLE001
        result["connect_error"] = f"{type(e).__name__}: {str(e)[:300]}"

    return result


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=[modal.Secret.from_name("hqx-db")],
    timeout=60,
)
def probe_fmcsa() -> dict:
    return _probe_impl("dex-db")


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=[modal.Secret.from_name("hqx-db")],
    timeout=60,
)
def probe_dex() -> dict:
    return _probe_impl("dex-db")


@app.local_entrypoint()
def run() -> None:
    print("=== dex-db ===")
    fmcsa = probe_fmcsa.remote()
    print(json.dumps(fmcsa, indent=2, default=str))
    print()
    print("=== dex-db ===")
    dex = probe_dex.remote()
    print(json.dumps(dex, indent=2, default=str))
    print()
    print("=== diff (✓ = match, ✗ = mismatch) ===")
    keys_to_compare = [
        "db_env_keys",
        "connection_url_picked_from",
        "current_database",
        "current_user",
        "server_addr",
        "server_version",
        "bulk_ingest_feed_ingest_runs_select",
        "bulk_ingest_feed_ingest_runs_insert",
        "bulk_ingest_feed_ingest_runs_update",
        "advisory_lock_xact",
    ]
    for k in keys_to_compare:
        f = fmcsa.get(k)
        d = dex.get(k)
        match = "✓" if f == d else "✗"
        print(f"  {match}  {k:42s}  fmcsa={f!r:60s}  dex={d!r}")
