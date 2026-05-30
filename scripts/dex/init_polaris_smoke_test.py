#!/usr/bin/env python3
"""Polaris catalog smoke test — proves end-to-end that the Polaris service
on Railway is healthy and that the Generic Table API works for Lance interop.

This script is the load-bearing verification gate for the Polaris standup
cycle. It executes a full CRUD round-trip across:

  1. OAuth2 token acquisition (client_credentials).
  2. Catalog ensure (creates `polaris_catalog` if missing, with the R2-specific
     stsUnavailable=true configuration so non-STS S3-compat works).
  3. Namespace `_polaris_smoke_test` create + list + assert.
  4. Iceberg test table CRUD via PyIceberg RestCatalog (5 rows in/out).
  5. Generic Table API round-trip with format='lance' — THE GATE — create,
     list, get, drop. If this fails, the Polaris-for-Lance bet is broken.
  6. Cleanup: drop test tables + drop namespace.

Exit 0 = full success (cycle verification passes).
Exit non-zero = which step failed printed to stderr (see step numbers above).

Usage:
    doppler run --project hq-all --config prd -- python apps/data-engine-x/scripts/init_polaris_smoke_test.py

    # Cleanup-only mode (after a failed run, removes residual smoke artifacts):
    doppler run --project hq-all --config prd -- python apps/data-engine-x/scripts/init_polaris_smoke_test.py --cleanup-only

Required env (from Doppler):
    POLARIS_PUBLIC_URL              — https://polaris-production-XXX.up.railway.app
    POLARIS_ROOT_PRINCIPAL_ID       — root client_id
    POLARIS_ROOT_PRINCIPAL_SECRET   — root client_secret
    POLARIS_DEFAULT_REALM           — e.g. "default-realm"
    POLARIS_DEFAULT_CATALOG_NAME    — e.g. "polaris_catalog"
    POLARIS_WAREHOUSE_BASE_LOCATION — e.g. "s3://dex-raw-landing-zone/polaris-warehouse"
    POLARIS_R2_ACCESS_KEY_ID        — R2 access key
    POLARIS_R2_SECRET_ACCESS_KEY    — R2 secret
    POLARIS_R2_ENDPOINT             — https://<account>.r2.cloudflarestorage.com
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import requests

NAMESPACE = "_polaris_smoke_test"
ICE_TABLE = "_ice_smoke"
LANCE_TABLE = "_lance_smoke"


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: required env var {name} not set", file=sys.stderr)
        sys.exit(64)
    return val


def step(num: int, msg: str) -> None:
    print(f"[{num}] {msg}", flush=True)


def err(num: int, msg: str) -> None:
    print(f"[{num}] FAIL: {msg}", file=sys.stderr, flush=True)


# -----------------------------------------------------------------------------
# Step 1: OAuth2 token
# -----------------------------------------------------------------------------
def get_token(base_url: str, client_id: str, client_secret: str) -> str:
    step(1, "acquiring OAuth2 token via client_credentials")
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
        err(1, f"token endpoint returned {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    token = resp.json().get("access_token")
    if not token:
        err(1, f"no access_token in response: {resp.json()}")
        sys.exit(1)
    return token


# -----------------------------------------------------------------------------
# Step 2: Catalog ensure (idempotent)
# -----------------------------------------------------------------------------
def ensure_catalog(base_url: str, token: str, catalog_name: str,
                   warehouse: str, r2_endpoint: str,
                   r2_key: str, r2_secret: str) -> None:
    step(2, f"ensuring catalog {catalog_name!r} exists")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # Check existing.
    list_resp = requests.get(
        f"{base_url}/api/management/v1/catalogs",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if list_resp.status_code != 200:
        err(2, f"list catalogs returned {list_resp.status_code}: {list_resp.text[:300]}")
        sys.exit(2)
    existing = [c.get("name") for c in list_resp.json().get("catalogs", [])]
    if catalog_name in existing:
        print(f"    catalog {catalog_name!r} already exists; skipping create")
        return
    # Create. R2-specific config: stsUnavailable=true, pathStyleAccess=true.
    # allowedLocations bounds writes to the warehouse prefix.
    #
    # Critical: do NOT put s3.access-key-id/secret-access-key in catalog
    # properties. If they're there, Polaris attempts credential vending for
    # every table create response, which fails with "no credentials available"
    # on a non-STS S3 (R2). Clients supply their own R2 creds out-of-band.
    # S3 creds live on the Polaris server itself (AWS_ACCESS_KEY_ID,
    # AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3 env vars) so Polaris's
    # internal SDK calls (commit metadata writes, etc.) can still reach R2.
    body = {
        "catalog": {
            "name": catalog_name,
            "type": "INTERNAL",
            "properties": {
                "default-base-location": warehouse,
            },
            "storageConfigInfo": {
                "storageType": "S3",
                "allowedLocations": [f"{warehouse}/*"],
                "endpoint": r2_endpoint,
                "stsUnavailable": True,
                "pathStyleAccess": True,
                # roleArn is required by Polaris's S3 storage validation even
                # when stsUnavailable=true; supply a dummy that will never be
                # called since stsUnavailable bypasses AssumeRole.
                "roleArn": "arn:aws:iam::000000000000:role/dummy-r2-no-sts",
            },
        }
    }
    resp = requests.post(
        f"{base_url}/api/management/v1/catalogs",
        headers=headers, json=body, timeout=30,
    )
    if resp.status_code not in (200, 201):
        err(2, f"create catalog returned {resp.status_code}: {resp.text[:500]}")
        sys.exit(2)
    print(f"    catalog {catalog_name!r} created")
    # Grant CATALOG_MANAGE_CONTENT on the default catalog_admin role — this
    # privilege is not assigned automatically and is required for table CRUD
    # (CREATE_TABLE_DIRECT_WITH_WRITE_DELEGATION). Idempotent — re-grants
    # return 200 with no-op.
    g = requests.put(
        f"{base_url}/api/management/v1/catalogs/{catalog_name}/catalog-roles/catalog_admin/grants",
        headers=headers,
        json={"grant": {"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}},
        timeout=30,
    )
    if g.status_code not in (200, 201):
        print(f"    WARN: grant CATALOG_MANAGE_CONTENT returned {g.status_code}: {g.text[:200]}",
              file=sys.stderr)
    else:
        print(f"    CATALOG_MANAGE_CONTENT granted to catalog_admin role")


# -----------------------------------------------------------------------------
# Step 3: Namespace create + list + assert
# -----------------------------------------------------------------------------
def create_namespace(base_url: str, token: str, catalog: str, ns: str) -> None:
    step(3, f"creating namespace {ns!r} in catalog {catalog!r}")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces",
        headers=headers,
        json={"namespace": [ns], "properties": {}},
        timeout=30,
    )
    if resp.status_code == 409:
        print(f"    namespace {ns!r} already exists; continuing")
    elif resp.status_code not in (200, 201):
        err(3, f"create namespace returned {resp.status_code}: {resp.text[:300]}")
        sys.exit(3)


def assert_namespace(base_url: str, token: str, catalog: str, ns: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces",
        headers=headers, timeout=30,
    )
    if resp.status_code != 200:
        err(3, f"list namespaces returned {resp.status_code}: {resp.text[:300]}")
        sys.exit(3)
    namespaces = [n[0] if isinstance(n, list) else n for n in resp.json().get("namespaces", [])]
    if ns not in namespaces:
        err(3, f"namespace {ns!r} not found in {namespaces}")
        sys.exit(3)
    print(f"    namespace {ns!r} confirmed in list")


def drop_namespace(base_url: str, token: str, catalog: str, ns: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces/{ns}",
        headers=headers, timeout=30,
    )
    if resp.status_code not in (200, 204, 404):
        print(f"    WARN: drop namespace returned {resp.status_code}: {resp.text[:300]}",
              file=sys.stderr)
    else:
        print(f"    namespace {ns!r} dropped")


# -----------------------------------------------------------------------------
# Step 4 + 5: Iceberg + Lance table CRUD
# -----------------------------------------------------------------------------
def iceberg_round_trip(base_url: str, token: str, catalog: str,
                       ns: str, table: str, warehouse: str) -> None:
    step(4, f"Iceberg table CRUD via PyIceberg RestCatalog ({ns}.{table})")
    try:
        from pyiceberg.catalog import load_catalog
        from pyiceberg.schema import Schema
        from pyiceberg.types import IntegerType, NestedField, StringType
        import pyarrow as pa
    except ImportError as e:
        err(4, f"pyiceberg or pyarrow not installed: {e}")
        sys.exit(4)
    rest = load_catalog(
        "polaris_smoke",
        **{
            "type": "rest",
            "uri": f"{base_url}/api/catalog",
            "warehouse": catalog,
            "credential": f"{os.environ['POLARIS_ROOT_PRINCIPAL_ID']}:{os.environ['POLARIS_ROOT_PRINCIPAL_SECRET']}",
            "scope": "PRINCIPAL_ROLE:ALL",
            # NO vended-credentials header — R2 has no STS so Polaris's
            # `stsUnavailable=true` catalog config means the server cannot
            # subscope/vend creds. We supply S3 creds client-side instead.
            # Pyarrow file IO speaks S3 directly; supply R2 creds + endpoint.
            "s3.endpoint": os.environ["POLARIS_R2_ENDPOINT"],
            "s3.access-key-id": os.environ["POLARIS_R2_ACCESS_KEY_ID"],
            "s3.secret-access-key": os.environ["POLARIS_R2_SECRET_ACCESS_KEY"],
            "s3.path-style-access": "true",
            "s3.region": "auto",
        },
    )
    schema = Schema(
        NestedField(field_id=1, name="id", field_type=IntegerType(), required=True),
        NestedField(field_id=2, name="label", field_type=StringType(), required=False),
    )
    full_name = (ns, table)
    # Drop if exists (cleanup from prior failed run).
    try:
        rest.drop_table(full_name)
    except Exception:
        pass
    tbl = rest.create_table(full_name, schema=schema)
    print(f"    iceberg table created at {tbl.metadata.location}")
    # Pyarrow schema must match Iceberg field types exactly:
    # - Iceberg IntegerType → int32 (not the default int64)
    # - required=True → nullable=False
    pa_schema = pa.schema([
        pa.field("id", pa.int32(), nullable=False),
        pa.field("label", pa.string(), nullable=True),
    ])
    df = pa.table(
        {"id": [1, 2, 3, 4, 5],
         "label": ["alpha", "beta", "gamma", "delta", "epsilon"]},
        schema=pa_schema,
    )
    tbl.append(df)
    print(f"    appended 5 rows")
    scan = tbl.scan().to_arrow()
    if scan.num_rows != 5:
        err(4, f"expected 5 rows, got {scan.num_rows}")
        sys.exit(4)
    print(f"    read back {scan.num_rows} rows; round-trip OK")
    rest.drop_table(full_name)
    print(f"    iceberg table dropped")


def generic_table_round_trip(base_url: str, token: str, catalog: str,
                             ns: str, table: str, warehouse: str) -> None:
    step(5, f"GENERIC TABLE API CRUD with format='lance' ({ns}.{table}) — HARD GATE")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{base_url}/api/catalog/polaris/v1/{catalog}/namespaces/{ns}/generic-tables"
    body = {
        "name": table,
        "format": "lance",
        "base-location": f"{warehouse}/{ns}/{table}/",
        "doc": "Polaris standup smoke test — Lance Generic Table API gate",
        "properties": {"table_type": "lance"},
    }
    # Cleanup any residual.
    requests.delete(f"{base}/{table}", headers=headers, timeout=30)

    # CREATE
    resp = requests.post(base, headers=headers, json=body, timeout=30)
    if resp.status_code not in (200, 201):
        err(5, f"create generic-table returned {resp.status_code}: {resp.text[:500]}")
        err(5, "GENERIC TABLE API IS BROKEN — Polaris-for-Lance path NOT viable on this version.")
        sys.exit(5)
    print(f"    generic table {table!r} created (status={resp.status_code})")

    # LIST
    list_resp = requests.get(base, headers=headers, timeout=30)
    if list_resp.status_code != 200:
        err(5, f"list generic-tables returned {list_resp.status_code}: {list_resp.text[:300]}")
        sys.exit(5)
    listed = list_resp.json().get("identifiers", [])
    found = any(i.get("name") == table for i in listed)
    if not found:
        err(5, f"generic table {table!r} not in list: {listed}")
        sys.exit(5)
    print(f"    generic table {table!r} confirmed in list ({len(listed)} total)")

    # GET
    get_resp = requests.get(f"{base}/{table}", headers=headers, timeout=30)
    if get_resp.status_code != 200:
        err(5, f"get generic-table returned {get_resp.status_code}: {get_resp.text[:300]}")
        sys.exit(5)
    payload = get_resp.json().get("table", {})
    if payload.get("format") != "lance":
        err(5, f"format round-trip failed: expected 'lance', got {payload.get('format')!r}")
        sys.exit(5)
    if payload.get("name") != table:
        err(5, f"name round-trip failed: expected {table!r}, got {payload.get('name')!r}")
        sys.exit(5)
    print(f"    generic table metadata round-tripped (format=lance, name={table})")

    # DROP
    del_resp = requests.delete(f"{base}/{table}", headers=headers, timeout=30)
    if del_resp.status_code not in (200, 204):
        err(5, f"drop generic-table returned {del_resp.status_code}: {del_resp.text[:300]}")
        sys.exit(5)
    print(f"    generic table {table!r} dropped")
    print("    GENERIC TABLE API GATE PASSED")


# -----------------------------------------------------------------------------
# Cleanup-only mode
# -----------------------------------------------------------------------------
def cleanup_only(base_url: str, token: str, catalog: str, ns: str) -> None:
    print(f"cleanup-only mode: removing {ns!r} and any test artifacts")
    headers = {"Authorization": f"Bearer {token}"}
    # Best-effort drop tables and namespace.
    requests.delete(
        f"{base_url}/api/catalog/polaris/v1/{catalog}/namespaces/{ns}/generic-tables/{LANCE_TABLE}",
        headers=headers, timeout=30,
    )
    requests.delete(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces/{ns}/tables/{ICE_TABLE}",
        headers=headers, timeout=30,
    )
    requests.delete(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces/{ns}",
        headers=headers, timeout=30,
    )
    print("cleanup complete")


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup-only", action="store_true",
                        help="Drop test artifacts; don't run smoke")
    parser.add_argument("--skip-iceberg", action="store_true",
                        help="Skip Iceberg PyIceberg round-trip (still runs Generic Table API)")
    args = parser.parse_args()

    base_url = _req("POLARIS_PUBLIC_URL").rstrip("/")
    client_id = _req("POLARIS_ROOT_PRINCIPAL_ID")
    client_secret = _req("POLARIS_ROOT_PRINCIPAL_SECRET")
    catalog = _req("POLARIS_DEFAULT_CATALOG_NAME")
    warehouse = _req("POLARIS_WAREHOUSE_BASE_LOCATION").rstrip("/")
    r2_endpoint = _req("POLARIS_R2_ENDPOINT")
    r2_key = _req("POLARIS_R2_ACCESS_KEY_ID")
    r2_secret = _req("POLARIS_R2_SECRET_ACCESS_KEY")

    print(f"polaris-url: {base_url}")
    print(f"catalog:     {catalog}")
    print(f"warehouse:   {warehouse}")
    print()

    token = get_token(base_url, client_id, client_secret)

    if args.cleanup_only:
        cleanup_only(base_url, token, catalog, NAMESPACE)
        return 0

    ensure_catalog(base_url, token, catalog, warehouse, r2_endpoint, r2_key, r2_secret)
    create_namespace(base_url, token, catalog, NAMESPACE)
    assert_namespace(base_url, token, catalog, NAMESPACE)

    if not args.skip_iceberg:
        iceberg_round_trip(base_url, token, catalog, NAMESPACE, ICE_TABLE, warehouse)

    generic_table_round_trip(base_url, token, catalog, NAMESPACE, LANCE_TABLE, warehouse)

    drop_namespace(base_url, token, catalog, NAMESPACE)

    step(6, "all smoke checks passed; Polaris standup verified end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
