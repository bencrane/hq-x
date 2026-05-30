"""Nightly emit for ops.coverage_stats — Polaris-first discovery.

Polaris is the absolute system of record for what datasets exist (per
PROTOCOL.md §"The Architecture Reality"). This script:

  1. Mints a Polaris OAuth token via client_credentials.
  2. Lists every namespace registered in the configured catalog.
  3. For each namespace, lists every generic-table and fetches its
     `base-location`, `format`, and `doc` from Polaris.
  4. For each Lance-format table, opens the dataset via `lance.dataset(uri)`
     to capture the physical row count and last-version timestamp.
  5. Writes one row per table to ops.coverage_stats. Tables whose name
     starts with `bridges_` land with scope='bridge'; everything else
     lands with scope='dataset'.

R2 bucket enumeration and YAML intersection parsing are removed — Polaris
is the catalog, Lance is the physical truth.

Per-row failures are recorded as `payload={'error': ...}` so the Coverage
card surfaces partial state rather than disappearing the row.

Doppler env (project hq-all, config prd):
  DEX_DB_URL_DIRECT             Postgres writer (DDL-safe direct URL).
  DEX_DB_URL_POOLED             Writer fallback.
  POLARIS_PUBLIC_URL            Polaris catalog base URL.
  POLARIS_ROOT_PRINCIPAL_ID     OAuth client_id.
  POLARIS_ROOT_PRINCIPAL_SECRET OAuth client_secret.
  POLARIS_DEFAULT_CATALOG_NAME  Catalog name (e.g. polaris_catalog).
  R2_ENDPOINT                   Cloudflare R2 endpoint.
  R2_ACCESS_KEY_ID              R2 key.
  R2_SECRET_ACCESS_KEY          R2 secret.

Smoke:
  cd apps/data-engine-x && doppler run -- python3 scripts/emit_coverage_stats.py --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("emit_coverage_stats")


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _r2_storage_options() -> dict[str, str]:
    """R2 storage options for lance.dataset(). Reads R2_* env injected by Doppler."""
    endpoint = os.environ.get("R2_ENDPOINT")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not all([endpoint, access_key, secret_key]):
        raise RuntimeError(
            "R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY not set "
            "(expected from Doppler hq-all/prd)"
        )
    return {
        "aws_endpoint": endpoint,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "aws_region": "auto",
        "aws_virtual_hosted_style_request": "false",
    }


def _db_url_writer() -> str:
    url = (
        os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise RuntimeError("DEX_DB_URL_DIRECT / DEX_DB_URL_POOLED / DATABASE_URL not set")
    return url


def _serialize_ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Polaris client
# ---------------------------------------------------------------------------


def _polaris_oauth_token() -> str:
    """Mint a Polaris OAuth token via client_credentials."""
    import requests

    base = os.environ["POLARIS_PUBLIC_URL"].rstrip("/")
    resp = requests.post(
        f"{base}/api/catalog/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["POLARIS_ROOT_PRINCIPAL_ID"],
            "client_secret": os.environ["POLARIS_ROOT_PRINCIPAL_SECRET"],
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _polaris_list_namespaces(base: str, catalog: str, hdr: dict[str, str]) -> list[str]:
    """Return every namespace registered in the catalog as dot-joined strings."""
    import requests

    resp = requests.get(
        f"{base}/api/catalog/v1/{catalog}/namespaces",
        headers=hdr,
        timeout=30,
    )
    resp.raise_for_status()
    out: list[str] = []
    for ns in resp.json().get("namespaces", []):
        out.append(".".join(ns) if isinstance(ns, list) else str(ns))
    return out


def _polaris_list_generic_table_names(
    base: str, catalog: str, hdr: dict[str, str], namespace: str
) -> list[str]:
    """Return every generic-table name registered in a namespace."""
    import requests

    resp = requests.get(
        f"{base}/api/catalog/polaris/v1/{catalog}/namespaces/{namespace}/generic-tables",
        headers=hdr,
        timeout=30,
    )
    if resp.status_code != 200:
        logger.warning(
            "polaris list-tables failed ns=%s status=%d body=%s",
            namespace, resp.status_code, resp.text[:200],
        )
        return []
    names: list[str] = []
    for ident in resp.json().get("identifiers", []):
        name = ident.get("name")
        if name:
            names.append(name)
    return names


def _polaris_get_generic_table(
    base: str, catalog: str, hdr: dict[str, str], namespace: str, name: str
) -> dict[str, str] | None:
    """Fetch one generic-table's base-location + format + doc.

    Per-table GET wraps the table data under a `table` key:
      {"table": {"name": ..., "format": "lance", "base-location": "s3://...",
                 "doc": "...", "properties": {...}},
       "storage-access-configs": []}
    """
    import requests

    resp = requests.get(
        f"{base}/api/catalog/polaris/v1/{catalog}/namespaces/{namespace}/generic-tables/{name}",
        headers=hdr,
        timeout=30,
    )
    if resp.status_code != 200:
        logger.warning(
            "polaris get-table failed ns=%s name=%s status=%d body=%s",
            namespace, name, resp.status_code, resp.text[:200],
        )
        return None
    t = resp.json().get("table", {})
    return {
        "namespace": namespace,
        "name": name,
        "base_location": (t.get("base-location") or "").rstrip("/"),
        "format": (t.get("format") or "unknown").lower(),
        "doc": t.get("doc", "") or "",
    }


def _discover_polaris_lance_tables() -> list[dict[str, str]]:
    """Walk Polaris → return every Lance generic-table with non-empty s3:// base."""
    base = os.environ["POLARIS_PUBLIC_URL"].rstrip("/")
    catalog = os.environ["POLARIS_DEFAULT_CATALOG_NAME"]
    token = _polaris_oauth_token()
    hdr = {"Authorization": f"Bearer {token}"}

    out: list[dict[str, str]] = []
    namespaces = _polaris_list_namespaces(base, catalog, hdr)
    logger.info("polaris namespaces=%d", len(namespaces))
    for namespace in namespaces:
        for name in _polaris_list_generic_table_names(base, catalog, hdr, namespace):
            t = _polaris_get_generic_table(base, catalog, hdr, namespace, name)
            if t is None:
                continue
            if t["format"] != "lance":
                logger.debug(
                    "skipping non-lance ns=%s name=%s format=%s",
                    namespace, name, t["format"],
                )
                continue
            if not t["base_location"].startswith("s3://"):
                logger.warning(
                    "skipping ns=%s name=%s — base-location missing or non-s3://",
                    namespace, name,
                )
                continue
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Lance physical probe
# ---------------------------------------------------------------------------


def _open_lance(uri: str, storage_options: dict[str, str]):
    import lance
    return lance.dataset(uri, storage_options=storage_options)


def _lance_physical_stats(uri: str, storage_options: dict[str, str]) -> dict[str, Any]:
    """Return row_count + last_version_ts + schema_version for one Lance dataset."""
    ds = _open_lance(uri, storage_options)
    row_count = int(ds.count_rows())
    versions = ds.versions()
    last_version_ts: str | None = None
    schema_version: int | None = None
    if versions:
        last = versions[-1]
        last_version_ts = _serialize_ts(last.get("timestamp"))
        ver = last.get("version")
        if ver is not None:
            schema_version = int(ver)
    return {
        "row_count": row_count,
        "last_version_ts": last_version_ts,
        "schema_version": schema_version,
    }


# ---------------------------------------------------------------------------
# Classification + row build
# ---------------------------------------------------------------------------


def _scope_for(name: str) -> str:
    return "bridge" if name.lower().startswith("bridges_") else "dataset"


def _build_rows(
    rows: list[tuple[str, str, dict[str, Any]]],
    limit: int | None,
) -> None:
    """Polaris discovery → per-table Lance probe → append to `rows`."""
    storage_options = _r2_storage_options()
    tables = _discover_polaris_lance_tables()
    if limit is not None:
        tables = tables[:limit]
    logger.info("polaris lance-tables discovered=%d", len(tables))
    for t in tables:
        name = t["name"]
        namespace = t["namespace"]
        uri = t["base_location"]
        scope = _scope_for(name)
        payload: dict[str, Any] = {
            "namespace": namespace,
            "display_name": name,
            "doc": t.get("doc", ""),
            "uri": uri,
        }
        try:
            stats = _lance_physical_stats(uri, storage_options)
            payload.update(stats)
            logger.info(
                "%s/%s rows=%s last_version_ts=%s schema_version=%s",
                scope, name,
                stats["row_count"], stats["last_version_ts"], stats["schema_version"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s/%s lance probe failed: %s", scope, name, exc)
            payload["error"] = str(exc)[:500]
        rows.append((scope, name, payload))


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------


def _insert_rows(rows: list[tuple[str, str, dict[str, Any]]]) -> int:
    """INSERT one row per (scope, metric_name, captured_at=now()).

    Single transaction. captured_at defaults to now() — PK collisions only
    occur on sub-microsecond same-scope-same-metric retries, vanishingly rare.
    """
    if not rows:
        return 0
    import psycopg

    url = _db_url_writer()
    inserted = 0
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for scope, metric_name, payload in rows:
                cur.execute(
                    """
                    INSERT INTO ops.coverage_stats (scope, metric_name, payload)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (scope, metric_name, json.dumps(payload, default=str)),
                )
                inserted += 1
        conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Module entrypoint — called from Modal app's run_emit().

    Args:
      dry_run: log computed payloads, skip DB writes.
      limit: cap total discovered table count (smoke test).

    Returns: summary dict with counts.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    started = time.time()

    rows: list[tuple[str, str, dict[str, Any]]] = []
    _build_rows(rows, limit)

    by_scope: dict[str, int] = {}
    for scope, _, _ in rows:
        by_scope[scope] = by_scope.get(scope, 0) + 1

    if dry_run:
        logger.info("dry-run: %d rows pending, by_scope=%s", len(rows), by_scope)
        inserted = 0
    else:
        inserted = _insert_rows(rows)
        logger.info("inserted %d rows, by_scope=%s", inserted, by_scope)

    return {
        "rows_total": len(rows),
        "rows_inserted": inserted,
        "by_scope": by_scope,
        "duration_seconds": round(time.time() - started, 2),
        "dry_run": dry_run,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Skip DB writes")
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap discovered table count (smoke test)",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    summary = main(dry_run=args.dry_run, limit=args.limit)
    print(json.dumps(summary, indent=2, default=str))
