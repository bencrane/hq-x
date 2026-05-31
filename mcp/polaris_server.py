"""FastMCP server exposing the Polaris LanceDB warehouse to autonomous agents.

Read-only catalog + query surface against ``s3://dex-raw-landing-zone/polaris-warehouse/``
on Cloudflare R2. Three tools:

* ``list_polaris_datasets`` — enumerate namespaces and ``*_lance`` datasets via
  boto3 common-prefix listing.
* ``get_polaris_schema``    — open a single Lance dataset via ``lance.dataset(...)``
  (metadata only; no row scan) and return its Arrow schema + row count.
* ``execute_read_only_duckdb_query`` — sandboxed DuckDB SELECT over Lance
  datasets, wired in via the Lance DuckDB extension's native ``__lance_scan``
  (filter + column-projection pushdown, BTREE scalar indexes) rather than a
  full Arrow materialization. AST-walked for forbidden statement types;
  resource-capped (4 threads, 4 GB memory); output capped at 100 rows.

R2 credentials are read from the environment at request time:
``R2_ENDPOINT``, ``R2_ACCESS_KEY_ID``, ``R2_SECRET_ACCESS_KEY``.

Run standalone (stdio transport) with::

    python -m apps.data_engine_x.mcp.polaris_server
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
from functools import lru_cache
from typing import Any

import boto3
import duckdb
import lance
import sqlglot
import sqlglot.expressions as exp
from botocore.client import Config as BotoConfig
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

R2_BUCKET = "dex-raw-landing-zone"
R2_BASE_PREFIX = "polaris-warehouse/"
R2_BASE_URI = f"s3://{R2_BUCKET}/{R2_BASE_PREFIX}"
LANCE_SUFFIX = "_lance"

# Polaris namespaces + dataset names are path slugs. This gates them before
# they are interpolated into a ``__lance_scan('<uri>')`` SQL string literal.
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]+$")

MAX_ROWS = 100
DUCKDB_THREADS = 4
DUCKDB_MEMORY_LIMIT = "4GB"

# Every sqlglot expression class that represents a side effect or non-SELECT
# statement. The AST walk in ``execute_read_only_duckdb_query`` rejects a
# parsed query if any node is an instance of one of these.
_FORBIDDEN_EXPRS: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Copy,
    exp.AlterSet,
    exp.Set,
    exp.Pragma,
    exp.Use,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)

# Top-level expression types that ARE valid read-only queries. Anything else
# is rejected even if it contains no forbidden sub-nodes (e.g. SHOW, DESCRIBE,
# CALL — none of these mutate but they aren't pure SELECT either).
_ALLOWED_TOP_EXPRS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.With,
    exp.Subquery,
    exp.Paren,
)


# ---------------------------------------------------------------------------
# Storage / S3 client (cached at process scope; recomputed if env mutates only
# after restart, which is the intended single-source-of-truth model)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _storage_options() -> dict[str, str]:
    """R2 storage options dict for ``lance.dataset(storage_options=...)``.

    Mirrors the canonical helper in ``app/services/polaris_catalog.py`` so the
    MCP server resolves the same URIs as the rest of DEX.
    """
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "auto",
        "aws_virtual_hosted_style_request": "false",
    }


@lru_cache(maxsize=1)
def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def _dataset_uri(namespace: str, dataset_name: str) -> str:
    """Build the canonical Lance URI for a Polaris-registered dataset.

    The ``_lance`` suffix is optional on input; both ``carriers`` and
    ``carriers_lance`` resolve to ``polaris-warehouse/<ns>/carriers_lance``.
    """
    name = (
        dataset_name
        if dataset_name.endswith(LANCE_SUFFIX)
        else f"{dataset_name}{LANCE_SUFFIX}"
    )
    return f"{R2_BASE_URI}{namespace}/{name}"


def _ensure_lance_object_store_env() -> None:
    """Export the R2 credentials as ``AWS_*`` env vars for the Lance extension.

    The ``lance`` DuckDB extension reads object-store credentials from the
    process environment (Lance's Rust ``object_store``); its
    ``__lance_scan(uri)`` table function takes no inline ``storage_options``
    argument the way ``lance.dataset(...)`` does. Mirror the canonical ``R2_*``
    env that ``_storage_options`` already consumes into the ``AWS_*`` names
    ``object_store`` recognizes, so a native ``__lance_scan('s3://...')``
    against R2 authenticates. Idempotent; verified against ``hq-all/prd`` R2.
    """
    os.environ["AWS_ACCESS_KEY_ID"] = os.environ["R2_ACCESS_KEY_ID"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["R2_SECRET_ACCESS_KEY"]
    os.environ["AWS_ENDPOINT"] = os.environ["R2_ENDPOINT"]
    os.environ["AWS_REGION"] = "auto"
    os.environ["AWS_DEFAULT_REGION"] = "auto"
    os.environ["AWS_VIRTUAL_HOSTED_STYLE_REQUEST"] = "false"


def _load_lance(con: duckdb.DuckDBPyConnection) -> None:
    """Load the Lance DuckDB extension into ``con`` (install once if absent).

    Provides the ``__lance_scan(uri)`` table function: a native Lance scan that
    pushes column projection and filter predicates into the Lance reader and
    engages BTREE scalar indexes, instead of materializing the whole dataset
    into an Arrow table. ``LOAD`` is per-connection; ``INSTALL`` is global and
    cached on disk, so the fallback runs at most once per image.
    """
    try:
        con.execute("LOAD lance")
    except duckdb.Error:
        con.execute("INSTALL lance")
        con.execute("LOAD lance")


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="polaris-catalog",
    instructions=(
        "Read-only access to the Polaris LanceDB warehouse on Cloudflare R2.\n"
        f"Base URI: {R2_BASE_URI}\n"
        "Workflow:\n"
        "  1. list_polaris_datasets — discover namespaces and Lance datasets.\n"
        "  2. get_polaris_schema     — inspect exact columns + row count for one dataset.\n"
        "  3. execute_read_only_duckdb_query — run a SELECT. Reference datasets\n"
        "     by dotted identifier `<namespace>.<dataset_name>`; the server\n"
        "     auto-resolves each to its Lance URI and registers it as a DuckDB\n"
        "     view (with and without the `_lance` suffix). DDL/DML is rejected.\n"
        "     Output is capped at 100 rows."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ---------------------------------------------------------------------------
# Tool 1: list_polaris_datasets
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Returns a complete list of all namespaces and registered datasets in "
        "the Polaris warehouse. Use this to discover what data is available."
    ),
)
def list_polaris_datasets() -> str:
    s3 = _s3_client()

    ns_resp = s3.list_objects_v2(
        Bucket=R2_BUCKET, Prefix=R2_BASE_PREFIX, Delimiter="/"
    )
    namespaces = sorted(
        cp["Prefix"][len(R2_BASE_PREFIX):].rstrip("/")
        for cp in ns_resp.get("CommonPrefixes", []) or []
        if cp.get("Prefix")
    )

    paginator = s3.get_paginator("list_objects_v2")
    catalog: dict[str, list[str]] = {}
    for ns in namespaces:
        ns_prefix = f"{R2_BASE_PREFIX}{ns}/"
        datasets: list[str] = []
        for page in paginator.paginate(
            Bucket=R2_BUCKET, Prefix=ns_prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []) or []:
                child = cp["Prefix"][len(ns_prefix):].rstrip("/")
                if child.endswith(LANCE_SUFFIX):
                    datasets.append(child)
        if datasets:
            catalog[ns] = sorted(datasets)

    lines: list[str] = [
        "# Polaris LanceDB warehouse",
        f"# Base: {R2_BASE_URI}",
        f"# Namespaces: {len(catalog)}  Datasets: "
        f"{sum(len(v) for v in catalog.values())}",
        "",
    ]
    for ns, datasets in catalog.items():
        lines.append(f"## {ns} ({len(datasets)})")
        for d in datasets:
            lines.append(f"  - {ns}.{d}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Tool 2: get_polaris_schema
# ---------------------------------------------------------------------------


class GetPolarisSchemaInput(BaseModel):
    namespace: str = Field(
        ...,
        description="Polaris namespace (e.g. 'fmcsa', 'usaspending', 'pdl').",
        min_length=1,
    )
    dataset_name: str = Field(
        ...,
        description=(
            "Lance dataset name within the namespace. The '_lance' suffix is "
            "optional — both 'carriers_lance' and 'carriers' resolve to the "
            "same dataset."
        ),
        min_length=1,
    )

    model_config = {"extra": "forbid"}


@mcp.tool(
    description=(
        "Fetch the exact column names and data types for a specific Lance "
        "dataset registered in the Polaris catalog. Always use this before "
        "writing DuckDB queries to ensure accurate column names."
    ),
)
def get_polaris_schema(params: GetPolarisSchemaInput) -> str:
    uri = _dataset_uri(params.namespace, params.dataset_name)
    try:
        ds = lance.dataset(uri, storage_options=_storage_options())
    except Exception as e:  # noqa: BLE001
        return f"ERROR opening {uri}: {type(e).__name__}: {e}"

    schema = ds.schema
    row_count = ds.count_rows()

    lines: list[str] = [
        f"# {params.namespace}.{uri.rsplit('/', 1)[-1]}",
        f"# URI: {uri}",
        f"# Rows: {row_count:,}",
        f"# Columns: {len(schema)}",
        "",
    ]
    for field in schema:
        nullable = "" if field.nullable else "  NOT NULL"
        lines.append(f"  {field.name}: {field.type}{nullable}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 3: execute_read_only_duckdb_query
# ---------------------------------------------------------------------------


class ExecuteDuckDBInput(BaseModel):
    sql_query: str = Field(
        ...,
        description=(
            "A single read-only DuckDB SQL statement. Reference Lance datasets "
            "by dotted identifier `<namespace>.<dataset_name>` (the `_lance` "
            "suffix is optional). The server resolves each referenced "
            "`<namespace>.<dataset_name>` to its "
            "`s3://dex-raw-landing-zone/polaris-warehouse/<ns>/<dataset>_lance` "
            "URI, opens the Lance dataset, and registers it as a DuckDB view "
            "before executing. DDL/DML (DROP, DELETE, INSERT, UPDATE, CREATE, "
            "ALTER, COPY, ATTACH, SET, PRAGMA, USE, transactions) is rejected. "
            "Output is capped at 100 rows."
        ),
        min_length=1,
    )

    model_config = {"extra": "forbid"}


def _scan_for_forbidden(parsed: exp.Expression) -> str | None:
    """Walk the parsed AST; return the name of the first forbidden node, or None."""
    for node in parsed.walk():
        # ``parsed.walk()`` yields either ``Expression`` instances or
        # ``(node, parent, key)`` tuples depending on sqlglot minor version.
        candidate = node[0] if isinstance(node, tuple) else node
        if isinstance(candidate, _FORBIDDEN_EXPRS):
            return type(candidate).__name__.upper()
    return None


def _cte_names(parsed: exp.Expression) -> set[str]:
    return {cte.alias_or_name for cte in parsed.find_all(exp.CTE)}


def _resolve_lance_references(
    parsed: exp.Expression, con: duckdb.DuckDBPyConnection
) -> list[str]:
    """Find every ``<namespace>.<dataset>`` table identifier in the AST, open the
    corresponding Lance dataset, and register it as a DuckDB view under the
    same dotted name (both with and without the ``_lance`` suffix).

    Returns the list of registered dotted view identifiers.
    """
    so = _storage_options()
    skip_names = _cte_names(parsed)
    seen: set[tuple[str, str]] = set()
    registered: list[str] = []

    for tbl in parsed.find_all(exp.Table):
        name_part = tbl.this
        if name_part is None:
            continue
        dataset = name_part.name if hasattr(name_part, "name") else str(name_part)
        db_part = tbl.args.get("db")
        if db_part is None:
            # Unqualified table reference. If it matches a CTE name, leave it
            # alone; otherwise it isn't resolvable in this server's catalog.
            if dataset in skip_names:
                continue
            continue
        namespace = db_part.name if hasattr(db_part, "name") else str(db_part)
        key = (namespace, dataset)
        if key in seen:
            continue
        seen.add(key)

        # ``namespace``/``dataset`` are parsed from agent-supplied SQL and get
        # interpolated into a ``__lance_scan('<uri>')`` string literal below.
        # Warehouse identifiers are path slugs; reject anything else so the
        # literal cannot be broken out of.
        if not (_SAFE_IDENT.match(namespace) and _SAFE_IDENT.match(dataset)):
            raise RuntimeError(
                f"unsafe dataset identifier {namespace!r}.{dataset!r} "
                "(expected [A-Za-z0-9_]+)"
            )

        uri = _dataset_uri(namespace, dataset)
        try:
            # Metadata-only open: validates existence and yields a clean error
            # naming the dataset before it is wired into a view.
            lance.dataset(uri, storage_options=so)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"failed to open Lance dataset {namespace!r}.{dataset!r} at {uri}: {e}"
            ) from e

        # Back the view with the Lance DuckDB extension's native scan instead
        # of a full ``ds.to_table()`` materialization. ``__lance_scan`` pushes
        # the outer query's column projection and filter predicates into the
        # Lance reader and engages BTREE scalar indexes — a
        # ``WHERE <indexed_key> = ...`` resolves via a ScalarIndexQuery that
        # reads a single fragment rather than the whole dataset, and nothing is
        # materialized in DuckDB, so the prior in-memory ceiling on which
        # datasets were queryable is removed.
        bare = dataset.removesuffix(LANCE_SUFFIX)
        suffixed = bare + LANCE_SUFFIX
        safe_uri = uri.replace("'", "''")

        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{namespace}"')
        for alias in {bare, suffixed}:
            con.execute(
                f'CREATE OR REPLACE VIEW "{namespace}"."{alias}" AS '
                f"SELECT * FROM __lance_scan('{safe_uri}')"
            )
        registered.append(f"{namespace}.{suffixed}")
    return registered


def _json_safe_row(columns: list[str], row: tuple) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, val in zip(columns, row):
        if isinstance(val, (bytes, bytearray)):
            out[col] = f"<{len(val)} bytes>"
        elif hasattr(val, "isoformat"):
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


@mcp.tool(
    description=(
        "Execute a read-only DuckDB SQL query against Lance datasets. "
        "Use this to sample data, audit bloat, or test join logic."
    ),
)
def execute_read_only_duckdb_query(params: ExecuteDuckDBInput) -> str:
    sql = params.sql_query.strip().rstrip(";").strip()
    if not sql:
        return "ERROR: empty query"

    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except sqlglot.errors.ParseError as e:
        return f"ERROR: parse failed: {e}"

    statements = [s for s in statements if s is not None]
    if not statements:
        return "ERROR: empty query"
    if len(statements) > 1:
        return "ERROR: multi-statement scripts are not allowed"

    parsed = statements[0]
    forbidden = _scan_for_forbidden(parsed)
    if forbidden:
        return f"ERROR: forbidden statement type: {forbidden}"
    if not isinstance(parsed, _ALLOWED_TOP_EXPRS):
        return (
            f"ERROR: only SELECT-style queries are allowed "
            f"(got {type(parsed).__name__})"
        )

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"SET threads={DUCKDB_THREADS}")
        con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
        _ensure_lance_object_store_env()
        _load_lance(con)

        try:
            registered = _resolve_lance_references(parsed, con)
        except RuntimeError as e:
            return f"ERROR: {e}"

        try:
            cur = con.execute(sql)
        except duckdb.Error as e:
            return f"ERROR: DuckDB execution failed: {type(e).__name__}: {e}"

        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]

        payload: dict[str, Any] = {
            "columns": columns,
            "row_count": len(rows),
            "truncated": truncated,
            "max_rows": MAX_ROWS,
            "lance_views_registered": registered,
            "rows": [_json_safe_row(columns, r) for r in rows],
        }
        return json.dumps(payload, default=str, indent=2)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Bearer-token gate (HTTP transport only)
# ---------------------------------------------------------------------------


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Single-shared-token bearer gate for the streamable-HTTP transport.

    Mirrors the dex-mcp pattern (app/mcp_server/dex_server.py) so
    deployment, vault wiring, and operational behavior are identical:

      * ``/health`` is always allowed through (Railway healthcheck).
      * ``/.well-known/`` requests short-circuit to 404 to discourage
        OAuth-discovery probes from confusing the bearer-auth model.
      * Everything else requires ``Authorization: Bearer <token>``;
        the comparison is constant-time via ``secrets.compare_digest``.

    Constructor takes the expected token explicitly (no env-var coupling),
    so the same middleware works whether the MCP is mounted as a
    sub-app or as a standalone Starlette app.
    """

    def __init__(self, app, *, expected_token: str) -> None:
        super().__init__(app)
        self._expected = expected_token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/health" or path.endswith("/health"):
            return await call_next(request)
        if path.startswith("/.well-known/"):
            return JSONResponse({"error": "not_found"}, status_code=404)
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix) or not secrets.compare_digest(
            header[len(prefix):], self._expected,
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Polaris MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport. stdio for local dev; streamable-http for the "
             "deployed Railway service that Anthropic Managed Agents reaches.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("GTM_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # streamable-http: serve via uvicorn with the bearer gate. The MCP route
    # MUST NOT be exposed without a token in any deployment — fail-fast at
    # boot if GTM_MCP_AUTH_TOKEN is missing.
    import uvicorn

    expected = os.environ.get("GTM_MCP_AUTH_TOKEN")
    if not expected:
        raise RuntimeError(
            "GTM_MCP_AUTH_TOKEN is required for streamable-http transport. "
            "Set it in Doppler (hq-all/prd) and register the same value in the "
            "Anthropic vault that the gtm-agent's polaris MCP server references."
        )

    async def _health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = mcp.streamable_http_app()
    app.add_route("/health", _health, methods=["GET"])
    app.add_middleware(BearerAuthMiddleware, expected_token=expected)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=os.environ.get("GTM_MCP_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
