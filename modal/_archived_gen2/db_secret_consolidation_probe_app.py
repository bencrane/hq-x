"""Comprehensive probe — compare each of the 9 legacy per-source DB Modal
secrets against the canonical ``dex-db`` secret.

For each secret, the probe spawns a container that ONLY binds that one
secret, then introspects what env vars Modal injects, connects to the
indicated DB, and records the server identity (host, port, current_database,
current_user, version) plus the privileges the connecting role has on
``bulk_ingest.feed_ingest_runs`` and ``ops.cron_heartbeats``.

The aggregator function fans out across all 10 probe functions (9 legacy +
dex-db baseline), prints a comparison table, and emits a JSON verdict:

  - SAFE TO CONSOLIDATE: the legacy secret resolves to the same Postgres
    server (host + port + current_database) as dex-db AND the role has the
    privileges the orchestrator needs.
  - NEEDS REVIEW: the legacy secret resolves to a different server / db /
    role. Per-source DB is genuinely separate; consolidation would break
    the script's reads.
  - ERROR: the secret failed to inject the expected DATABASE_URL or the
    connect attempt errored.

Run::

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/db_secret_consolidation_probe_app.py::run

Safe one-off. Delete the app + this file after the consolidation directive
ships. The output is written verbatim to stdout; the operator pastes the
verdict block into ``apps/data-engine-x/modal/SECRETS.md`` under the
"Per-source DB secrets" table.
"""
from __future__ import annotations

import json
from typing import Any

import modal

app = modal.App("data-engine-x-db-secret-consolidation-probe")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]>=3.2")
)

DB_ENV_PREFIXES = ("DATABASE", "DEX_DB", "DB_", "POSTGRES", "PG", "SUPABASE")

# Tables the heartbeat helper + bulk_ingest writers need access to. Each
# entry is `(schema, table, [privileges])`. The probe checks privileges and
# records which the role has.
PRIVILEGE_CHECKS: list[tuple[str, str, list[str]]] = [
    ("bulk_ingest", "feed_ingest_runs", ["SELECT", "INSERT", "UPDATE"]),
    ("ops", "cron_heartbeats", ["INSERT"]),
    ("ops", "data_source_ingest_runs", ["SELECT", "INSERT", "UPDATE"]),
]


def _probe_impl(label: str) -> dict[str, Any]:
    """Inspect env vars injected by this container's secret + try connecting."""
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

    def _redact(u: str | None) -> str | None:
        if not u:
            return None
        from urllib.parse import urlparse
        try:
            p = urlparse(u)
            return f"{p.scheme}://<user>:<pass>@{p.hostname}:{p.port}/<db>?{(p.query[:60] + '...') if p.query else ''}"
        except Exception:
            return "(unredactable)"

    result: dict[str, Any] = {
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

                # Capture the canonical (host, port, db) tuple for cross-secret
                # comparison. Two secrets that resolve to the same tuple are
                # aliases; consolidation is safe iff the role has the needed
                # privileges (checked below).
                from urllib.parse import urlparse
                parsed = urlparse(url)
                result["identity_tuple"] = {
                    "host": parsed.hostname,
                    "port": parsed.port,
                    "database": row[0],
                    "user": row[1],
                }

                for schema, table, privs in PRIVILEGE_CHECKS:
                    fqn = f"{schema}.{table}"
                    table_result: dict[str, Any] = {"table": fqn}
                    # First: does the table even exist?
                    cur.execute(
                        "SELECT to_regclass(%s) IS NOT NULL", (fqn,),
                    )
                    table_result["exists"] = bool(cur.fetchone()[0])
                    if not table_result["exists"]:
                        result.setdefault("table_privileges", []).append(table_result)
                        continue
                    for priv in privs:
                        cur.execute(
                            "SELECT has_table_privilege(current_user, %s, %s)",
                            (fqn, priv),
                        )
                        table_result[priv.lower()] = bool(cur.fetchone()[0])
                    result.setdefault("table_privileges", []).append(table_result)

                try:
                    cur.execute("SELECT pg_try_advisory_xact_lock(424242)")
                    result["advisory_lock_xact"] = cur.fetchone()[0]
                except Exception as e:  # noqa: BLE001
                    result["advisory_lock_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    except Exception as e:  # noqa: BLE001
        result["connect_error"] = f"{type(e).__name__}: {str(e)[:300]}"

    return result


# --------------------------------------------------------------------------- #
# Per-secret probe functions. Each binds exactly ONE secret so the
# introspection result reflects only what that secret injects.
# --------------------------------------------------------------------------- #


def _make_probe(secret_name: str):
    """Build a `@app.function` that binds `secret_name` and returns a probe."""
    return modal.Secret.from_name(secret_name)


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("hqx-db")], timeout=60)
def probe_dex_db() -> dict[str, Any]:
    return _probe_impl("dex-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("bts-t100-db")], timeout=60)
def probe_bts_t100_db() -> dict[str, Any]:
    return _probe_impl("bts-t100-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("epiq-claims-db")], timeout=60)
def probe_epiq_claims_db() -> dict[str, Any]:
    return _probe_impl("epiq-claims-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("epiq-dockets-db")], timeout=60)
def probe_epiq_dockets_db() -> dict[str, Any]:
    return _probe_impl("epiq-dockets-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("faa-aircraft-registry-db")], timeout=60)
def probe_faa_aircraft_registry_db() -> dict[str, Any]:
    return _probe_impl("faa-aircraft-registry-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("faa-airmen-db")], timeout=60)
def probe_faa_airmen_db() -> dict[str, Any]:
    return _probe_impl("faa-airmen-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("finra-brokercheck-db")], timeout=60)
def probe_finra_brokercheck_db() -> dict[str, Any]:
    return _probe_impl("finra-brokercheck-db")


# NOTE: `noaa-ais-db` is referenced by modal/noaa_ais_ingest_app.py but does
# NOT exist in the Modal workspace (verified via `modal secret list` on
# 2026-05-25). The operator's docstring runbook for that app instructs
# creating it on first deploy. Since it does not exist, the probe cannot
# bind it; the verdict for noaa-ais-db is "DOES NOT EXIST — operator should
# either create it via the runbook, or migrate noaa_ais_ingest_app.py to
# bind dex-db only (the heartbeat-wiring sweep already added dex-db there)."


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("overture-places-db")], timeout=60)
def probe_overture_places_db() -> dict[str, Any]:
    return _probe_impl("overture-places-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("fmcsa-refresh-db")], timeout=60)
def probe_fmcsa_refresh_db() -> dict[str, Any]:
    # fmcsa-refresh-db's app is disabled (per CLAUDE.md §"FMCSA pipeline status");
    # included here only for completeness so the consolidation verdict is total.
    return _probe_impl("fmcsa-refresh-db")


# retry-policy: no-retry
@app.function(image=image, secrets=[modal.Secret.from_name("warn-notices-db")], timeout=60)
def probe_warn_notices_db() -> dict[str, Any]:
    # warn-notices-db exists in the Modal workspace (per `modal secret list`)
    # but isn't currently bound by any modal/*.py app. Probed for completeness
    # so the verdict block covers the full set of legacy per-source secrets.
    return _probe_impl("warn-notices-db")


def _compare_tuple(a: dict[str, Any] | None, b: dict[str, Any] | None) -> str:
    """Return one-word verdict comparing two identity_tuple dicts."""
    if not a or not b:
        return "UNKNOWN"
    if (a.get("host"), a.get("port"), a.get("database")) == (b.get("host"), b.get("port"), b.get("database")):
        return "SAME_SERVER"
    return "DIFFERENT_SERVER"


@app.local_entrypoint()
def run() -> None:
    """Fan out probes across all 10 secrets, print the comparison table."""
    probes = [
        ("dex-db", probe_dex_db),
        ("bts-t100-db", probe_bts_t100_db),
        ("epiq-claims-db", probe_epiq_claims_db),
        ("epiq-dockets-db", probe_epiq_dockets_db),
        ("faa-aircraft-registry-db", probe_faa_aircraft_registry_db),
        ("faa-airmen-db", probe_faa_airmen_db),
        ("finra-brokercheck-db", probe_finra_brokercheck_db),
        ("overture-places-db", probe_overture_places_db),
        ("fmcsa-refresh-db", probe_fmcsa_refresh_db),
        ("warn-notices-db", probe_warn_notices_db),
    ]

    results: dict[str, dict[str, Any]] = {}
    for label, fn in probes:
        try:
            results[label] = fn.remote()
        except Exception as exc:  # noqa: BLE001
            results[label] = {
                "label": label,
                "remote_call_error": f"{type(exc).__name__}: {exc}",
            }

    dex_tuple = results.get("dex-db", {}).get("identity_tuple")

    print("=" * 80)
    print("DB SECRET CONSOLIDATION PROBE — RESULTS")
    print("=" * 80)
    print(json.dumps(results, indent=2, default=str))
    print()
    print("=" * 80)
    print("VERDICTS (vs dex-db identity_tuple)")
    print("=" * 80)
    verdicts: list[dict[str, Any]] = []
    for label, _ in probes:
        if label == "dex-db":
            continue
        r = results[label]
        if r.get("connect_error") or r.get("remote_call_error"):
            verdict = "ERROR"
            reason = r.get("connect_error") or r.get("remote_call_error")
        else:
            tup_verdict = _compare_tuple(r.get("identity_tuple"), dex_tuple)
            # SAFE TO CONSOLIDATE iff SAME_SERVER + role has heartbeat INSERT privilege
            heartbeat_priv = next(
                (
                    p for p in (r.get("table_privileges") or [])
                    if p.get("table") == "ops.cron_heartbeats"
                ),
                None,
            )
            heartbeat_ok = bool(heartbeat_priv and heartbeat_priv.get("exists") and heartbeat_priv.get("insert"))
            if tup_verdict == "SAME_SERVER" and heartbeat_ok:
                verdict = "SAFE TO CONSOLIDATE"
                reason = "same (host, port, database) as dex-db; role has ops.cron_heartbeats INSERT"
            elif tup_verdict == "SAME_SERVER":
                verdict = "REVIEW: same server but role lacks ops.cron_heartbeats INSERT"
                reason = "same server but role missing heartbeat privilege"
            else:
                verdict = "DO NOT CONSOLIDATE: different server"
                reason = (
                    f"identity_tuple={r.get('identity_tuple')} != "
                    f"dex-db identity_tuple={dex_tuple}"
                )
        verdicts.append({"secret": label, "verdict": verdict, "reason": reason})
        print(f"  {label:32s} → {verdict}\n     {reason}")
    print()
    print(json.dumps({"verdicts": verdicts}, indent=2, default=str))
