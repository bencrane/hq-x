"""Singleton loader for the Iceberg SQL catalog (read-only from hq-x).

Mirrors the data-engine-x ``scripts/_lib/iceberg_catalog.py`` shape so the
two stay swappable when Polaris (parallel cycle) ships and the catalog
seam moves. hq-x reads from the SAME catalog that DEX writes to —
PyIceberg's ``SqlCatalog`` is multi-reader-safe.

Environment variables (Doppler ``hq-all/prd``):
    DEX_DB_URL_DIRECT       — Postgres URL where iceberg_tables lives
    R2_ENDPOINT             — Cloudflare R2 S3-compat endpoint
    R2_ACCESS_KEY_ID        — R2 access key
    R2_SECRET_ACCESS_KEY    — R2 secret access key

Why the same DEX_DB_URL_DIRECT: PyIceberg's ``SqlCatalog`` stores its
catalog metadata in the configured database. The catalog rows
(iceberg_tables, iceberg_namespace_properties) live in DEX's Postgres,
so hq-x must read from there. When Polaris lands, this collapses to a
REST URL.
"""
from __future__ import annotations

import os
import threading
from typing import Any

PRODUCTION_WAREHOUSE = "s3://dex-raw-landing-zone/iceberg-warehouse/"
PRODUCTION_CATALOG_NAME = "dex"

_catalog_lock = threading.Lock()
_catalog_singleton: Any = None


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(f"{name} is required for the Iceberg catalog")
    return val


def _build_catalog_kwargs(warehouse: str) -> dict[str, Any]:
    db_url = _required_env("DEX_DB_URL_DIRECT")
    sa_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return {
        "uri": sa_url,
        "warehouse": warehouse,
        "s3.endpoint": _required_env("R2_ENDPOINT"),
        "s3.access-key-id": _required_env("R2_ACCESS_KEY_ID"),
        "s3.secret-access-key": _required_env("R2_SECRET_ACCESS_KEY"),
        "s3.region": "auto",
    }


def get_catalog(
    *,
    name: str = PRODUCTION_CATALOG_NAME,
    warehouse: str = PRODUCTION_WAREHOUSE,
) -> Any:
    """Return a process-wide singleton ``SqlCatalog``.

    Lazy-imported because pyiceberg is a heavy dependency and we don't
    want it loaded at module import time (slows the FastAPI cold start).
    """
    global _catalog_singleton
    from pyiceberg.catalog.sql import SqlCatalog

    if warehouse != PRODUCTION_WAREHOUSE or name != PRODUCTION_CATALOG_NAME:
        return SqlCatalog(name, **_build_catalog_kwargs(warehouse))
    with _catalog_lock:
        if _catalog_singleton is None:
            _catalog_singleton = SqlCatalog(name, **_build_catalog_kwargs(warehouse))
        return _catalog_singleton


def configure_duckdb_secret(con: Any) -> None:
    """Install + load httpfs and create a DuckDB R2 secret on the connection.

    Required for any DuckDB code path that reads parquet directly from R2
    (the FMCSA L1 view layer in DEX uses ``read_parquet`` against absolute
    s3:// URIs and needs this to authenticate).
    """
    endpoint = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint.replace("https://", "").replace("http://", "")
    key_id = _required_env("R2_ACCESS_KEY_ID")
    secret = _required_env("R2_SECRET_ACCESS_KEY")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2_for_iceberg (
            TYPE s3,
            KEY_ID '{key_id}',
            SECRET '{secret}',
            ENDPOINT '{endpoint_host}',
            URL_STYLE 'path',
            REGION 'auto'
        )
        """
    )
