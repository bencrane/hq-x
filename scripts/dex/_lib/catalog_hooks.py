"""Polaris catalog lifecycle hook for Lance datasets.

Canonical entry point for "after Lance materialize, ensure Polaris registration."
Replaces ad-hoc invocations of ``scripts/init_polaris_lance_generic.py`` from
spine and bridge emit scripts.

Idempotent: GET-first against the Generic Table API; POST on missing; accept
200/201/409 on the POST as a successful steady state. Silent on success.
Raises ``PolarisRegistrationError`` on any API failure.

Reuses the same Generic Table API contract as
``scripts/init_polaris_lance_generic.py`` (OAuth client-credentials grant,
namespace ensure, generic-tables POST with ``format=lance``).

Required env (from Doppler ``hq-all/prd``):
    POLARIS_PUBLIC_URL
    POLARIS_ROOT_PRINCIPAL_ID
    POLARIS_ROOT_PRINCIPAL_SECRET
    POLARIS_DEFAULT_CATALOG_NAME

Usage:
    from scripts._lib.catalog_hooks import register_or_update_polaris

    register_or_update_polaris(
        namespace="sba",
        table_name="ppp_borrowers_lance",
        s3_uri="s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/",
        docstring="SBA PPP canonical borrower spine (Pattern A).",
    )
"""
from __future__ import annotations

import logging
import os

import requests

LOG = logging.getLogger(__name__)

TABLE_FORMAT = "lance"
_HTTP_TIMEOUT = 30


class PolarisRegistrationError(RuntimeError):
    """Raised when Polaris Generic Table registration fails."""


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise PolarisRegistrationError(f"required env var {name!r} not set")
    return val


def _get_token(base_url: str, client_id: str, client_secret: str) -> str:
    resp = requests.post(
        f"{base_url}/api/catalog/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise PolarisRegistrationError(
            f"Polaris OAuth token endpoint returned {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    return resp.json()["access_token"]


def _ensure_namespace(
    base_url: str, token: str, catalog: str, namespace: str
) -> None:
    list_resp = requests.get(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_HTTP_TIMEOUT,
    )
    if list_resp.status_code != 200:
        raise PolarisRegistrationError(
            f"list namespaces failed: {list_resp.status_code} "
            f"{list_resp.text[:300]}"
        )
    existing = [
        n[0] if isinstance(n, list) else n
        for n in list_resp.json().get("namespaces", [])
    ]
    if namespace in existing:
        return
    create_resp = requests.post(
        f"{base_url}/api/catalog/v1/{catalog}/namespaces",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"namespace": [namespace], "properties": {}},
        timeout=_HTTP_TIMEOUT,
    )
    if create_resp.status_code in (200, 201, 409):
        return
    raise PolarisRegistrationError(
        f"create namespace {namespace!r} failed: "
        f"{create_resp.status_code} {create_resp.text[:300]}"
    )


def register_or_update_polaris(
    namespace: str,
    table_name: str,
    s3_uri: str,
    docstring: str,
) -> None:
    """Register or verify a Lance dataset's Polaris Generic Table registration.

    Idempotent. Silent on success. Raises ``PolarisRegistrationError`` on any
    API failure.

    Args:
        namespace: Polaris namespace (e.g. ``sba``, ``sec_dera``, ``fmcsa``).
        table_name: Generic Table name (e.g. ``ppp_borrowers_lance``).
        s3_uri: Full Lance dataset URI (e.g.
            ``s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance``).
            Trailing slash optional — normalized to a trailing slash before POST.
        docstring: Human-readable description for the Generic Table ``doc`` field.

    Raises:
        PolarisRegistrationError: any non-success response from the Polaris API,
            a missing required env var, or a format mismatch on an existing
            registration (e.g. table already registered as ``iceberg`` instead
            of ``lance``).
    """
    base_url = _require_env("POLARIS_PUBLIC_URL").rstrip("/")
    client_id = _require_env("POLARIS_ROOT_PRINCIPAL_ID")
    client_secret = _require_env("POLARIS_ROOT_PRINCIPAL_SECRET")
    catalog = _require_env("POLARIS_DEFAULT_CATALOG_NAME")

    base_location = s3_uri if s3_uri.endswith("/") else f"{s3_uri}/"

    token = _get_token(base_url, client_id, client_secret)
    _ensure_namespace(base_url, token, catalog, namespace)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    base = (
        f"{base_url}/api/catalog/polaris/v1/{catalog}"
        f"/namespaces/{namespace}/generic-tables"
    )

    get_resp = requests.get(
        f"{base}/{table_name}", headers=headers, timeout=_HTTP_TIMEOUT
    )
    if get_resp.status_code == 200:
        existing = get_resp.json().get("table", {})
        existing_format = existing.get("format")
        if existing_format != TABLE_FORMAT:
            raise PolarisRegistrationError(
                f"{namespace}.{table_name} exists with format="
                f"{existing_format!r} (expected {TABLE_FORMAT!r}); "
                "manual cleanup required"
            )
        LOG.info(
            "polaris generic-table %s.%s already registered (format=%s)",
            namespace, table_name, existing_format,
        )
        return
    if get_resp.status_code != 404:
        raise PolarisRegistrationError(
            f"GET generic-table {namespace}.{table_name} returned unexpected "
            f"{get_resp.status_code}: {get_resp.text[:300]}"
        )

    body = {
        "name": table_name,
        "format": TABLE_FORMAT,
        "base-location": base_location,
        "doc": docstring,
        "properties": {"table_type": "lance"},
    }
    post_resp = requests.post(
        base, headers=headers, json=body, timeout=_HTTP_TIMEOUT
    )
    if post_resp.status_code in (200, 201):
        LOG.info(
            "polaris generic-table %s.%s registered (status=%d)",
            namespace, table_name, post_resp.status_code,
        )
        return
    if post_resp.status_code == 409:
        verify = requests.get(
            f"{base}/{table_name}", headers=headers, timeout=_HTTP_TIMEOUT
        )
        if (
            verify.status_code == 200
            and verify.json().get("table", {}).get("format") == TABLE_FORMAT
        ):
            LOG.info(
                "polaris generic-table %s.%s created concurrently; verified",
                namespace, table_name,
            )
            return
        raise PolarisRegistrationError(
            f"POST returned 409 for {namespace}.{table_name} but verify GET "
            f"returned {verify.status_code} "
            f"format={verify.json().get('table', {}).get('format')!r}"
        )
    raise PolarisRegistrationError(
        f"POST generic-table {namespace}.{table_name} failed "
        f"({post_resp.status_code}): {post_resp.text[:500]}"
    )
