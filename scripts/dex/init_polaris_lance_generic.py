#!/usr/bin/env python3
"""Register any Lance dataset as a Polaris Generic Table (Wave 1 sweep helper).

Generalizes the canary's ``init_polaris_lance_fmcsa_carrier_essentials.py``
so each Wave 1 source is one CLI invocation against this single script.

Usage:
    doppler run --project hq-all --config prd -- python3 \\
        apps/data-engine-x/scripts/init_polaris_lance_generic.py \\
        --namespace fmcsa --table crash_essentials_lance \\
        --doc "FMCSA crash essentials (Wave 1 Lance sweep) — latest snapshot per refresh"

    # --check-only: verify registration exists; do not create.
    doppler run --project hq-all --config prd -- python3 \\
        apps/data-engine-x/scripts/init_polaris_lance_generic.py \\
        --namespace usaspending --table contracts_lance --check-only

Required env (from Doppler):
    POLARIS_PUBLIC_URL
    POLARIS_ROOT_PRINCIPAL_ID
    POLARIS_ROOT_PRINCIPAL_SECRET
    POLARIS_DEFAULT_CATALOG_NAME       (e.g. polaris_catalog)
    POLARIS_WAREHOUSE_BASE_LOCATION    (e.g. s3://dex-raw-landing-zone/polaris-warehouse)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("init-polaris-lance-generic")

TABLE_FORMAT = "lance"


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        LOG.error("required env var %s not set", name)
        sys.exit(64)
    return val


def get_token(base_url: str, client_id: str, client_secret: str) -> str:
    resp = requests.post(
        f"{base_url}/api/catalog/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        LOG.error("token endpoint returned %d: %s", resp.status_code, resp.text[:300])
        sys.exit(1)
    return resp.json()["access_token"]


def ensure_namespace(base_url: str, token: str, catalog: str, ns: str) -> None:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    list_resp = requests.get(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if list_resp.status_code != 200:
        LOG.error("list namespaces failed: %d %s", list_resp.status_code, list_resp.text[:300])
        sys.exit(2)
    namespaces = [
        n[0] if isinstance(n, list) else n
        for n in list_resp.json().get("namespaces", [])
    ]
    if ns in namespaces:
        LOG.info("namespace %r already exists in catalog %r", ns, catalog)
        return
    resp = requests.post(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces",
        headers=headers,
        json={"namespace": [ns], "properties": {}},
        timeout=30,
    )
    if resp.status_code in (200, 201):
        LOG.info("namespace %r created in catalog %r", ns, catalog)
    elif resp.status_code == 409:
        LOG.info("namespace %r created concurrently; ok", ns)
    else:
        LOG.error("create namespace failed: %d %s", resp.status_code, resp.text[:300])
        sys.exit(2)


def ensure_generic_table(
    base_url: str,
    token: str,
    catalog: str,
    namespace: str,
    table_name: str,
    base_location: str,
    doc: str,
) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{base_url}/api/catalog/polaris/v1/{catalog}/namespaces/{namespace}/generic-tables"

    get_resp = requests.get(f"{base}/{table_name}", headers=headers, timeout=30)
    if get_resp.status_code == 200:
        payload = get_resp.json().get("table", {})
        if payload.get("format") != TABLE_FORMAT:
            LOG.error(
                "table %s.%s exists but format=%r (expected %r) — manual cleanup required",
                namespace, table_name, payload.get("format"), TABLE_FORMAT,
            )
            sys.exit(3)
        LOG.info(
            "generic-table %s.%s already registered (format=%s); verified",
            namespace, table_name, payload.get("format"),
        )
        return payload
    if get_resp.status_code not in (404,):
        LOG.error(
            "GET generic-table returned unexpected %d: %s",
            get_resp.status_code, get_resp.text[:300],
        )
        sys.exit(3)

    body = {
        "name": table_name,
        "format": TABLE_FORMAT,
        "base-location": base_location,
        "doc": doc,
        "properties": {"table_type": "lance"},
    }
    resp = requests.post(base, headers=headers, json=body, timeout=30)
    if resp.status_code not in (200, 201):
        LOG.error(
            "POST generic-table failed (%d): %s",
            resp.status_code, resp.text[:500],
        )
        sys.exit(3)
    LOG.info(
        "generic-table %s.%s created (status=%d)",
        namespace, table_name, resp.status_code,
    )
    verify = requests.get(f"{base}/{table_name}", headers=headers, timeout=30)
    if verify.status_code != 200:
        LOG.error(
            "post-create GET returned %d: %s",
            verify.status_code, verify.text[:300],
        )
        sys.exit(3)
    return verify.json().get("table", {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--namespace", required=True, help="Polaris namespace (e.g. fmcsa)")
    ap.add_argument(
        "--table", required=True, help="Generic table name (e.g. crash_essentials_lance)",
    )
    ap.add_argument(
        "--doc",
        default=None,
        help="Documentation string (default: auto-generated)",
    )
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="Verify registration exists; do not create.",
    )
    args = ap.parse_args()

    base_url = _req("POLARIS_PUBLIC_URL").rstrip("/")
    client_id = _req("POLARIS_ROOT_PRINCIPAL_ID")
    client_secret = _req("POLARIS_ROOT_PRINCIPAL_SECRET")
    catalog = _req("POLARIS_DEFAULT_CATALOG_NAME")
    warehouse_root = _req("POLARIS_WAREHOUSE_BASE_LOCATION").rstrip("/")

    base_location = f"{warehouse_root}/{args.namespace}/{args.table}/"
    doc = (
        args.doc
        or f"{args.namespace}.{args.table} — Wave 1 Lance sweep (universal substrate)"
    )

    LOG.info("polaris-url:   %s", base_url)
    LOG.info("catalog:       %s", catalog)
    LOG.info("namespace:     %s", args.namespace)
    LOG.info("table:         %s", args.table)
    LOG.info("base-location: %s", base_location)

    token = get_token(base_url, client_id, client_secret)
    ensure_namespace(base_url, token, catalog, args.namespace)

    if args.check_only:
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"{base_url}/api/catalog/polaris/v1/{catalog}"
            f"/namespaces/{args.namespace}/generic-tables/{args.table}"
        )
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            payload = r.json().get("table", {})
            LOG.info("verified: %s", json.dumps(payload, indent=2))
            return 0
        LOG.error("NOT REGISTERED: GET returned %d %s", r.status_code, r.text[:300])
        return 1

    table_payload = ensure_generic_table(
        base_url=base_url,
        token=token,
        catalog=catalog,
        namespace=args.namespace,
        table_name=args.table,
        base_location=base_location,
        doc=doc,
    )
    LOG.info("OK: %s", json.dumps(table_payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
