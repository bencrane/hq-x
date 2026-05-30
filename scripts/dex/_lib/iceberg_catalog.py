"""Singleton loader for the Iceberg SQL catalog backed by Postgres + R2.

This module is the canonical place for the data-engine-x ingest scripts to
acquire an Iceberg ``Catalog`` instance. Production-grade PyIceberg SqlCatalog
backed by the data-engine-x Postgres direct URL (NOT pooled — pgbouncer
transaction-mode rejects the CREATE TABLE statements PyIceberg uses to
bootstrap its internal catalog tables).

Environment variables:
    DEX_DB_URL_DIRECT  — non-pooled Postgres URL (required for DDL)
    R2_ENDPOINT        — Cloudflare R2 S3-compat endpoint URL
    R2_ACCESS_KEY_ID   — R2 access key
    R2_SECRET_ACCESS_KEY — R2 secret access key

The warehouse path is the production prefix (``iceberg-warehouse/``) under
the canonical R2 bucket. Test/probe code MUST pass a different warehouse
to avoid touching production metadata.

Pinned versions (see pyproject.toml):
  - pyiceberg 0.11.x  (SqlCatalog backend, add_files API)
  - duckdb     1.5.x  (iceberg + httpfs extensions)

Notes on the DuckDB integration:
  PyIceberg 0.11 + DuckDB 1.5 iceberg extension have an incompatibility on
  ``add_files()``-registered Parquets that live OUTSIDE the table's location
  (absolute s3:// paths in the manifest). The extension's manifest path
  resolver treats the manifest's absolute parquet path as a relative path
  and errors. The canonical query path used by the audience compiler is
  therefore ``table.scan(...).to_duckdb(table_name=...)`` which uses
  PyIceberg's Arrow-via-DuckDB bridge instead of the iceberg extension.
  Both paths get the same data; the bridge avoids the extension bug.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog

# Single canonical warehouse prefix in R2. Iceberg metadata + new Parquets
# land here. Existing-Parquet add_files() registrations point at the
# pre-existing usaspending/contracts/ keys, NOT at this warehouse.
PRODUCTION_WAREHOUSE = "s3://dex-raw-landing-zone/iceberg-warehouse/"
PRODUCTION_CATALOG_NAME = "dex"

_catalog_lock = threading.Lock()
_catalog_singleton: Catalog | None = None


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(f"{name} is required for the Iceberg catalog")
    return val


def _build_catalog_kwargs(warehouse: str) -> dict[str, Any]:
    db_url = _required_env("DEX_DB_URL_DIRECT")
    # PyIceberg's SqlCatalog expects a SQLAlchemy-style URL.
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
) -> Catalog:
    """Return a process-wide singleton ``SqlCatalog`` wired to R2 + Postgres.

    Test code MUST pass a non-production ``warehouse`` (e.g.
    ``s3://dex-raw-landing-zone/iceberg-test/``) and a distinct ``name``
    to avoid mixing test + production catalog metadata.
    """
    global _catalog_singleton
    if warehouse != PRODUCTION_WAREHOUSE or name != PRODUCTION_CATALOG_NAME:
        # Non-default: don't memoize. Tests get a fresh catalog each call.
        return SqlCatalog(name, **_build_catalog_kwargs(warehouse))
    with _catalog_lock:
        if _catalog_singleton is None:
            _catalog_singleton = SqlCatalog(name, **_build_catalog_kwargs(warehouse))
        return _catalog_singleton


def configure_duckdb_secret(con) -> None:
    """Install + load httpfs and create a DuckDB R2 secret on the connection.

    Used by anything that queries Iceberg-registered Parquets through
    DuckDB (whether via PyIceberg's ``to_duckdb`` bridge or directly via
    ``read_parquet`` against the absolute s3:// paths in the manifest).
    """
    endpoint = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint.replace("https://", "").replace("http://", "")
    key_id = _required_env("R2_ACCESS_KEY_ID")
    secret = _required_env("R2_SECRET_ACCESS_KEY")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # URL_STYLE='path' is REQUIRED for R2 — the default virtual-host style
    # fails authentication for accounts that publish via path-style URLs.
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
