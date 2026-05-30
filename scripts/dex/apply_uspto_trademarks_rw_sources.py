#!/usr/bin/env python3
"""USPTO Trademark Case Files Dataset (TCFD) → RisingWave source wiring
(Stage 4 — metadata-only).

Wires 6 USPTO TCFD streams (case_file, case_file_owner, classification,
case_file_statement, case_file_event_statement, correspondent_domrep_attorney)
as RisingWave ``CREATE SOURCE ... FORMAT PLAIN ENCODE PARQUET`` catalog
entries. Sources are metadata-only — RW does not read any Parquet until an
MV consumes from the source. Cost: near-zero CU.

Per-source flow:

  1. R2 ground-truth (L8): boto3 list_objects_v2 over the source's
     match_pattern; require ≥ 1 matching object (~141-142 per table — one
     per filing-year partition 1875-2024).
  2. DuckDB-on-R2 ``DESCRIBE`` introspection on earliest + latest era
     samples (year=1875/1884 + year=2024). Captures (col_name, duckdb_type)
     pairs. Schema drift verified absent across years 1875/2024 in the
     directive's preparation; cross-era introspection is defensive.
  3. Map DuckDB types → RW DDL types (per L2). Build CREATE SOURCE DDL.
  4. Idempotency: ``SELECT 1 FROM pg_class WHERE relname = ?`` — if hit, skip.
  5. DDL apply via psycopg.
  6. pg_class smoke (L6): metadata-only, no count(*).
  7. Typed-column smoke (L2 footgun safety net): for every column whose RW
     type is NOT CHARACTER VARYING, run ``SELECT <col> FROM source LIMIT 5``
     and verify ≥ 1 non-NULL value comes back. Hard-fail on partition-key
     columns (publication_year/case_file_year/serial_number_bigint); soft-warn
     on other typed columns.
  8. Insert ledger row into ``ops.rw_source_wiring_runs``.

After all 6 sources land successfully, UPDATE the
``ops.data_source_catalog`` row for ``source_slug='uspto_trademarks'``:
  - lifecycle_stage = 'rw_source_wired'
  - notes = L45 advisory text (operator-decided 2026-05-10 to accept the
    overwrite limitation given annual cadence)

Reuses the ``ops.rw_source_wiring_runs`` ledger from PR #249 — no new
migration. The directive slug is stamped into ``notes->>'directive'`` so
this batch is queryable separately.

Pre-flight invariants (per directive
2026-05-10-rw-source-wiring-sweep-uspto-trademarks):

  - L0: worktree-path discipline.
  - L2: source DDL types match the Parquet writer's actual types.
        USPTO TCFD = mostly VARCHAR + 2 SMALLINT partition keys
        (publication_year, case_file_year) + serial_number_bigint BIGINT
        on case_file only. Type mismatch silently NULLs the column.
  - L4: ledger CHECK includes {completed, failed, skipped, dry_run}.
  - L6: smoke = pg_class only for metadata; typed-column smoke uses LIMIT 5
        (small bounded read accepted as the cost of catching L2).
  - L8: R2 ground-truth before DDL; fail-loud if 0 matches.
  - L42: USPTO writer does NOT pass ContentEncoding=zstd (verified in
        run_uspto_trademarks_r2_ingest.py:upload_to_r2 — only ContentType
        is set).
  - L45: USPTO R2 layout uses single key per (case_file_year, table).
        Annual republish overwrites — RW source will not auto-pickup
        updates per L45 (S3_V2 + append_only=t treats overwritten keys
        as already-consumed). Operator-decided 2026-05-10 to accept the
        overwrite limitation given annual cadence; manual refresh path
        is DROP+RECREATE per L45 path #4 OR restructure to
        publication_year-prefixed keys per L45 path #1 at next USPTO
        TCFD release. Acceptance baked into ops.data_source_catalog.notes.

Usage:
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_uspto_trademarks_rw_sources.py --apply
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_uspto_trademarks_rw_sources.py --dry-run
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_uspto_trademarks_rw_sources.py \\
          --source source_uspto_trademarks_case_file --apply
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_uspto_trademarks_rw_sources.py --smoke-only

Per-source debug budget: 15 min wall-clock. Exceeding it logs ``failed``
and continues to the next source.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

import boto3
import duckdb
import psycopg


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


DIRECTIVE_SLUG = "2026-05-10-rw-source-wiring-sweep-uspto-trademarks"
BUCKET = "dex-raw-landing-zone"
PER_SOURCE_BUDGET_SEC = 15 * 60

# Hard-fail typed-column smoke on these columns: every USPTO TCFD Parquet
# writer guarantees they are populated on EVERY row (the `COPY ... WHERE
# case_file_year = X` per-year writes ensure it). If LIMIT 5 returns
# all-NULL on one of these, the DDL has a type mismatch — fix before
# downstream MVs join the source.
EXPECTED_DENSE_TYPED_COLS: frozenset[str] = frozenset({
    "publication_year",
    "case_file_year",
    "serial_number_bigint",
})

# L45 acceptance text stamped into ops.data_source_catalog.notes for
# source_slug='uspto_trademarks'. Visible to the next operator who reads
# the catalog so the overwrite-limitation constraint is preserved.
L45_ACCEPTANCE_NOTES = (
    "L45 advisory: R2 ingest writes single key per (case_file_year, table). "
    "Annual republish overwrites — RW source will not auto-pickup updates "
    "per L45 (S3_V2 + append_only=t treats overwritten keys as "
    "already-consumed). Manual refresh path: (a) DROP SOURCE ... CASCADE "
    "+ recreate per L45 path #4, OR (b) restructure ingest to "
    "publication_year-prefixed keys per L45 path #1. Decided 2026-05-10: "
    "accept overwrite limitation given annual cadence; revisit at next "
    "USPTO TCFD release (~Q1-Q2 2026 for 2025 publication-year)."
)


# --------------------------------------------------------------------------- #
# Source registry — 6 sources hardcoded (per directive)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceSpec:
    name: str
    prefix_group: str
    match_pattern: str


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "source_uspto_trademarks_case_file",
        "uspto_trademarks",
        "uspto-trademarks/year=*/case_file.parquet",
    ),
    SourceSpec(
        "source_uspto_trademarks_case_file_owner",
        "uspto_trademarks",
        "uspto-trademarks/year=*/case_file_owner.parquet",
    ),
    SourceSpec(
        "source_uspto_trademarks_classification",
        "uspto_trademarks",
        "uspto-trademarks/year=*/classification.parquet",
    ),
    SourceSpec(
        "source_uspto_trademarks_case_file_statement",
        "uspto_trademarks",
        "uspto-trademarks/year=*/case_file_statement.parquet",
    ),
    SourceSpec(
        "source_uspto_trademarks_case_file_event_statement",
        "uspto_trademarks",
        "uspto-trademarks/year=*/case_file_event_statement.parquet",
    ),
    SourceSpec(
        "source_uspto_trademarks_correspondent_domrep_attorney",
        "uspto_trademarks",
        "uspto-trademarks/year=*/correspondent_domrep_attorney.parquet",
    ),
)


# --------------------------------------------------------------------------- #
# DuckDB type → RisingWave DDL type map (per L2)
# --------------------------------------------------------------------------- #


_DUCKDB_TO_RW: dict[str, str] = {
    "VARCHAR":   "CHARACTER VARYING",
    "TEXT":      "CHARACTER VARYING",
    "STRING":    "CHARACTER VARYING",
    "BOOLEAN":   "BOOLEAN",
    "BOOL":      "BOOLEAN",
    "TINYINT":   "SMALLINT",   # RW has no TINYINT; widen.
    "UTINYINT":  "SMALLINT",
    "SMALLINT":  "SMALLINT",
    "USMALLINT": "INTEGER",
    "INTEGER":   "INTEGER",
    "INT":       "INTEGER",
    "INT4":      "INTEGER",
    "UINTEGER":  "BIGINT",
    "BIGINT":    "BIGINT",
    "INT8":      "BIGINT",
    "UBIGINT":   "BIGINT",
    "FLOAT":     "REAL",
    "FLOAT4":    "REAL",
    "REAL":      "REAL",
    "DOUBLE":    "DOUBLE PRECISION",
    "FLOAT8":    "DOUBLE PRECISION",
    "DATE":      "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP_NS": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP WITH TIME ZONE",
    "TIMESTAMPTZ":              "TIMESTAMP WITH TIME ZONE",
}


def _map_duckdb_type_to_rw(t: str) -> str:
    base = t.upper().strip()
    if "(" in base:
        base = base.split("(")[0].strip()
    if base in _DUCKDB_TO_RW:
        return _DUCKDB_TO_RW[base]
    if base.startswith("DECIMAL") or base.startswith("NUMERIC"):
        return "NUMERIC"
    raise RuntimeError(f"unmapped DuckDB type: {t!r}")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-uspto-trademarks-rw-sources")


log = _logger()


# --------------------------------------------------------------------------- #
# Env / connection helpers
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _rw_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=_required_env("RISINGWAVE_HOST"),
        port=int(_required_env("RISINGWAVE_PORT")),
        user=_required_env("RISINGWAVE_USER"),
        password=_required_env("RISINGWAVE_PASSWORD"),
        dbname=_required_env("RISINGWAVE_DATABASE"),
        sslmode=_required_env("RISINGWAVE_SSLMODE"),
        connect_timeout=10,
    )


def _pg_conn() -> psycopg.Connection:
    return psycopg.connect(_required_env("DEX_DB_URL_DIRECT"), connect_timeout=10)


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="us-east-1",
    )


def _duck_with_r2() -> duckdb.DuckDBPyConnection:
    endpoint_full = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_region='auto';")
    return con


# --------------------------------------------------------------------------- #
# R2 ground-truth + introspection
# --------------------------------------------------------------------------- #


def _glob_to_search_prefix(match_pattern: str) -> str:
    for ch in ("*", "?", "["):
        idx = match_pattern.find(ch)
        if idx >= 0:
            match_pattern = match_pattern[:idx]
    return match_pattern


def list_r2_keys_full(bucket: str, match_pattern: str) -> list[str]:
    """Return ALL R2 keys matching the glob, sorted lexicographically."""
    import fnmatch
    s3 = _r2_client()
    search_prefix = _glob_to_search_prefix(match_pattern)
    matches: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=search_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if fnmatch.fnmatchcase(key, match_pattern):
                matches.append(key)
    matches.sort()
    return matches


def pick_era_samples(all_keys: list[str]) -> list[str]:
    """Pick earliest + latest key per cross-era sampling protocol.

    For USPTO TCFD this captures pre-1900 (year=1875/1884) AND modern era
    (year=2024). Schema drift was verified absent in directive prep; this
    is defensive belt-and-braces.
    """
    if not all_keys:
        return []
    if len(all_keys) == 1:
        return [all_keys[0]]
    return [all_keys[0], all_keys[-1]]


def introspect_typed(
    s3_uri: str, duck: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str]]:
    """Return ordered [(col_name, duckdb_type)] for one Parquet file.

    Uses ``hive_partitioning=FALSE`` so path-derived ``year=YYYY`` doesn't
    get auto-promoted to a column that doesn't actually exist in the
    Parquet body.
    """
    rows = duck.execute(
        f"DESCRIBE SELECT * FROM "
        f"read_parquet('{s3_uri}', hive_partitioning=FALSE);"
    ).fetchall()
    if not rows:
        raise RuntimeError(f"DESCRIBE returned 0 columns for {s3_uri}")
    return [(r[0], r[1]) for r in rows]


_TYPE_WIDTH: dict[str, int] = {
    "BOOLEAN":           1,
    "SMALLINT":          2,
    "INTEGER":           3,
    "BIGINT":            4,
    "REAL":              5,
    "DOUBLE PRECISION":  6,
    "DATE":              7,
    "TIMESTAMP":         8,
    "TIMESTAMP WITH TIME ZONE": 9,
    "NUMERIC":           10,
    "CHARACTER VARYING": 11,
}


def _widen_rw_type(a: str, b: str) -> str:
    wa = _TYPE_WIDTH.get(a, 0)
    wb = _TYPE_WIDTH.get(b, 0)
    return a if wa >= wb else b


def merge_introspections(
    samples: list[list[tuple[str, str]]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Merge per-era column lists into one ordered DDL column set.

    Order is preserved from the FIRST sample; columns appearing only in
    later samples are appended at the end. Returns ``(merged_typed_cols,
    drift_warnings)`` where each drift warning is ``(col_name, type_a,
    type_b)`` for cross-era type mismatches.
    """
    if not samples:
        return [], []
    type_map: dict[str, str] = {}
    order: list[str] = []
    drift: list[tuple[str, str, str]] = []
    for cols in samples:
        for col_name, dtype in cols:
            rw_type = _map_duckdb_type_to_rw(dtype)
            if col_name not in type_map:
                type_map[col_name] = rw_type
                order.append(col_name)
            elif type_map[col_name] != rw_type:
                widened = _widen_rw_type(type_map[col_name], rw_type)
                drift.append((col_name, type_map[col_name], rw_type))
                type_map[col_name] = widened
    return [(c, type_map[c]) for c in order], drift


# --------------------------------------------------------------------------- #
# DDL build
# --------------------------------------------------------------------------- #


def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def build_create_source_ddl(
    *,
    source_name: str,
    typed_cols: list[tuple[str, str]],
    bucket: str,
    match_pattern: str,
) -> str:
    cols_decl = ",\n    ".join(
        f"{_quote_ident(c)} {t}" for c, t in typed_cols
    )
    return (
        f"CREATE SOURCE {source_name} (\n"
        f"    {cols_decl}\n"
        f") WITH (\n"
        f"    connector = 's3',\n"
        f"    s3.region_name = 'us-east-1',\n"
        f"    s3.bucket_name = '{bucket}',\n"
        f"    s3.endpoint_url = '{_required_env('R2_ENDPOINT')}',\n"
        f"    s3.credentials.access = '{_required_env('R2_ACCESS_KEY_ID')}',\n"
        f"    s3.credentials.secret = '{_required_env('R2_SECRET_ACCESS_KEY')}',\n"
        f"    match_pattern = '{match_pattern}'\n"
        f") FORMAT PLAIN ENCODE PARQUET"
    )


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def source_exists_in_rw(rw: psycopg.Connection, source_name: str) -> bool:
    cur = rw.execute(
        "SELECT 1 FROM pg_class WHERE relname = %s LIMIT 1",
        (source_name,),
    )
    return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# Typed-column smoke (L2 footgun safety net)
# --------------------------------------------------------------------------- #


def typed_column_smoke(
    rw: psycopg.Connection,
    source_name: str,
    typed_cols: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """For every non-VARCHAR column, fetch 5 rows and classify nullness."""
    hard_failures: list[tuple[str, str]] = []
    soft_warnings: list[tuple[str, str]] = []

    for col_name, rw_type in typed_cols:
        if rw_type == "CHARACTER VARYING":
            continue

        try:
            cur = rw.execute(
                f'SELECT {_quote_ident(col_name)} FROM {source_name} LIMIT 5'
            )
            rows = cur.fetchall()
        except Exception as exc:
            hard_failures.append((col_name, f"query_error:{exc}"))
            continue

        if not rows:
            soft_warnings.append((col_name, "source_returned_zero_rows"))
            continue

        non_null_count = sum(1 for r in rows if r[0] is not None)
        if non_null_count == 0:
            reason = "all_null_in_first_5_rows"
            if col_name in EXPECTED_DENSE_TYPED_COLS:
                hard_failures.append((col_name, reason))
            else:
                soft_warnings.append((col_name, reason))

    return hard_failures, soft_warnings


# --------------------------------------------------------------------------- #
# data_source_catalog UPDATE (L45 advisory action items)
# --------------------------------------------------------------------------- #


def update_catalog_lifecycle(pg: psycopg.Connection) -> tuple[int, str | None]:
    """UPDATE ops.data_source_catalog row for uspto_trademarks per L45 advisory.

    Returns (rows_updated, current_lifecycle_after_update).
    """
    with pg.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.data_source_catalog
               SET lifecycle_stage = 'rw_source_wired',
                   notes           = %s,
                   updated_at      = now()
             WHERE source_slug = 'uspto_trademarks'
            """,
            (L45_ACCEPTANCE_NOTES,),
        )
        rows_updated = cur.rowcount
        cur.execute(
            "SELECT lifecycle_stage FROM ops.data_source_catalog "
            "WHERE source_slug = 'uspto_trademarks'"
        )
        row = cur.fetchone()
    pg.commit()
    return rows_updated, row[0] if row else None


# --------------------------------------------------------------------------- #
# Per-source orchestrator
# --------------------------------------------------------------------------- #


@dataclass
class SourceOutcome:
    source_name: str
    prefix_group: str
    match_pattern: str
    status: str  # completed | failed | skipped | dry_run
    r2_object_count: int | None
    introspected_column_count: int | None
    ddl_text: str | None
    duration_seconds: float
    error_message: str | None
    notes: dict[str, Any] | None


def process_source(
    *,
    entry: SourceSpec,
    bucket: str,
    rw: psycopg.Connection,
    pg: psycopg.Connection,
    run_id: uuid.UUID,
    duck: duckdb.DuckDBPyConnection,
    mode: str,
) -> SourceOutcome:
    started = time.monotonic()

    def budget_ok(stage: str) -> None:
        if time.monotonic() - started > PER_SOURCE_BUDGET_SEC:
            raise TimeoutError(f"per-source budget exceeded before {stage}")

    if mode == "smoke_only":
        if not source_exists_in_rw(rw, entry.name):
            log.warning("[%s] not in pg_class — cannot smoke", entry.name)
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status="failed",
                r2_object_count=None,
                introspected_column_count=None,
                ddl_text=None,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message="source_not_in_pg_class",
                notes={"directive": DIRECTIVE_SLUG, "mode": "smoke_only"},
            )
            write_ledger(pg, run_id, outcome)
            return outcome

        try:
            all_keys = list_r2_keys_full(bucket, entry.match_pattern)
            if not all_keys:
                raise RuntimeError(f"no R2 objects at {entry.match_pattern!r}")
            era_keys = pick_era_samples(all_keys)
            samples = [
                introspect_typed(f"s3://{bucket}/{k}", duck) for k in era_keys
            ]
            typed_cols, drift = merge_introspections(samples)
            hard, soft = typed_column_smoke(rw, entry.name, typed_cols)
            status = "failed" if hard else "completed"
            err = (
                f"smoke_failed:{','.join(c for c, _ in hard)}"
                if hard else None
            )
            notes = {
                "directive": DIRECTIVE_SLUG,
                "mode": "smoke_only",
                "r2_object_count_total": len(all_keys),
                "era_sample_keys": era_keys,
                "type_drift_warnings": [list(d) for d in drift],
                "smoke_hard_failures": [list(p) for p in hard],
                "smoke_soft_warnings": [list(p) for p in soft],
            }
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status=status,
                r2_object_count=len(all_keys),
                introspected_column_count=len(typed_cols),
                ddl_text=None,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=err,
                notes=notes,
            )
            write_ledger(pg, run_id, outcome)
            log.info(
                "[%s] smoke-only %s — hard=%d soft=%d drift=%d",
                entry.name, status, len(hard), len(soft), len(drift),
            )
            return outcome
        except Exception as exc:
            log.error("[%s] smoke-only FAILED: %s", entry.name, exc)
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status="failed",
                r2_object_count=None,
                introspected_column_count=None,
                ddl_text=None,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=str(exc),
                notes={"directive": DIRECTIVE_SLUG, "mode": "smoke_only"},
            )
            write_ledger(pg, run_id, outcome)
            return outcome

    if mode == "apply" and source_exists_in_rw(rw, entry.name):
        log.info("[%s] already in pg_class — skipping", entry.name)
        outcome = SourceOutcome(
            source_name=entry.name,
            prefix_group=entry.prefix_group,
            match_pattern=entry.match_pattern,
            status="skipped",
            r2_object_count=None,
            introspected_column_count=None,
            ddl_text=None,
            duration_seconds=round(time.monotonic() - started, 2),
            error_message=None,
            notes={
                "directive": DIRECTIVE_SLUG,
                "reason": "already_exists_in_pg_class",
            },
        )
        write_ledger(pg, run_id, outcome)
        return outcome

    try:
        budget_ok("R2 list")
        all_keys = list_r2_keys_full(bucket, entry.match_pattern)
        if not all_keys:
            raise RuntimeError(
                f"R2 ground-truth FAIL — 0 objects match "
                f"{entry.match_pattern!r}"
            )
        era_keys = pick_era_samples(all_keys)
        log.info(
            "[%s] R2 ground-truth: %d total objects; era samples=%s",
            entry.name, len(all_keys), era_keys,
        )

        budget_ok("introspect")
        samples = [
            introspect_typed(f"s3://{bucket}/{k}", duck) for k in era_keys
        ]
        typed_cols, drift = merge_introspections(samples)
        type_summary = {t for _, t in typed_cols}
        log.info(
            "[%s] introspected %d cols across %d era(s); types=%s; drift=%d",
            entry.name, len(typed_cols), len(samples),
            sorted(type_summary), len(drift),
        )
        if drift:
            for col, ta, tb in drift:
                log.warning(
                    "[%s] type drift on %s: %s vs %s — using widest",
                    entry.name, col, ta, tb,
                )

        ddl = build_create_source_ddl(
            source_name=entry.name,
            typed_cols=typed_cols,
            bucket=bucket,
            match_pattern=entry.match_pattern,
        )

        if mode == "dry_run":
            log.info("[%s] DRY-RUN — DDL emitted", entry.name)
            print(f"-- {entry.name}")
            print(ddl + ";")
            print()
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status="dry_run",
                r2_object_count=len(all_keys),
                introspected_column_count=len(typed_cols),
                ddl_text=ddl,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=None,
                notes={
                    "directive": DIRECTIVE_SLUG,
                    "era_sample_keys": era_keys,
                    "type_drift_warnings": [list(d) for d in drift],
                },
            )
            write_ledger(pg, run_id, outcome)
            return outcome

        budget_ok("apply")
        with rw.cursor() as cur:
            cur.execute(ddl)
        rw.commit()
        log.info("[%s] CREATE SOURCE applied", entry.name)

        if not source_exists_in_rw(rw, entry.name):
            raise RuntimeError(
                "DDL applied but source not visible in pg_class — "
                "RW catalog desync?"
            )
        log.info("[%s] pg_class smoke OK", entry.name)

        budget_ok("typed_column_smoke")
        hard, soft = typed_column_smoke(rw, entry.name, typed_cols)
        if hard:
            log.error(
                "[%s] TYPED-COLUMN SMOKE FAILED: %d hard failures: %s",
                entry.name, len(hard), hard,
            )
        if soft:
            log.warning(
                "[%s] typed-column smoke soft warnings: %d cols: %s",
                entry.name, len(soft), soft,
            )
        if not hard and not soft:
            log.info("[%s] typed-column smoke OK", entry.name)

        notes = {
            "directive": DIRECTIVE_SLUG,
            "era_sample_keys": era_keys,
            "type_drift_warnings": [list(d) for d in drift],
            "smoke_hard_failures": [list(p) for p in hard],
            "smoke_soft_warnings": [list(p) for p in soft],
        }
        outcome = SourceOutcome(
            source_name=entry.name,
            prefix_group=entry.prefix_group,
            match_pattern=entry.match_pattern,
            status="failed" if hard else "completed",
            r2_object_count=len(all_keys),
            introspected_column_count=len(typed_cols),
            ddl_text=ddl,
            duration_seconds=round(time.monotonic() - started, 2),
            error_message=(
                f"typed_column_smoke_returned_all_null:"
                f"{','.join(c for c, _ in hard)}"
                if hard else None
            ),
            notes=notes,
        )
        write_ledger(pg, run_id, outcome)
        return outcome

    except Exception as exc:
        try:
            rw.rollback()
        except Exception:
            pass
        log.error("[%s] FAILED: %s", entry.name, exc)
        outcome = SourceOutcome(
            source_name=entry.name,
            prefix_group=entry.prefix_group,
            match_pattern=entry.match_pattern,
            status="failed",
            r2_object_count=None,
            introspected_column_count=None,
            ddl_text=None,
            duration_seconds=round(time.monotonic() - started, 2),
            error_message=str(exc),
            notes={"directive": DIRECTIVE_SLUG},
        )
        write_ledger(pg, run_id, outcome)
        return outcome


# --------------------------------------------------------------------------- #
# Ledger write
# --------------------------------------------------------------------------- #


def write_ledger(
    pg: psycopg.Connection,
    run_id: uuid.UUID,
    outcome: SourceOutcome,
) -> None:
    with pg.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.rw_source_wiring_runs (
                run_id, source_name, prefix_group, match_pattern, status,
                r2_object_count, introspected_column_count, ddl_text,
                started_at, finished_at, duration_seconds,
                error_message, notes
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                now() - make_interval(secs => %s), now(), %s,
                %s, %s::jsonb
            )
            """,
            (
                str(run_id),
                outcome.source_name,
                outcome.prefix_group,
                outcome.match_pattern,
                outcome.status,
                outcome.r2_object_count,
                outcome.introspected_column_count,
                outcome.ddl_text,
                outcome.duration_seconds,
                outcome.duration_seconds,
                outcome.error_message,
                json.dumps(outcome.notes) if outcome.notes else None,
            ),
        )
    pg.commit()


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="Introspect + apply CREATE SOURCE + smoke + ledger.")
    grp.add_argument("--dry-run", action="store_true",
                     help="Introspect + emit DDL to stdout (no apply).")
    grp.add_argument("--smoke-only", action="store_true",
                     help="For each existing source, re-run typed-column "
                          "smoke + ledger.")
    parser.add_argument("--source", default=None,
                        help="Restrict to one source by name (must be in "
                             "the hardcoded SOURCES list).")
    args = parser.parse_args()

    sources: list[SourceSpec] = list(SOURCES)
    if args.source:
        sources = [s for s in sources if s.name == args.source]
        if not sources:
            log.error("--source %s not in registry", args.source)
            return 2

    mode = "apply" if args.apply else ("dry_run" if args.dry_run else "smoke_only")
    run_id = uuid.uuid4()
    log.info(
        "directive=%s run_id=%s mode=%s sources=%d",
        DIRECTIVE_SLUG, run_id, mode, len(sources),
    )

    pg = _pg_conn()
    rw = _rw_conn()
    duck = _duck_with_r2()

    counts = {"completed": 0, "failed": 0, "skipped": 0, "dry_run": 0}
    failures: list[tuple[str, str]] = []
    try:
        for entry in sources:
            outcome = process_source(
                entry=entry,
                bucket=BUCKET,
                rw=rw,
                pg=pg,
                run_id=run_id,
                duck=duck,
                mode=mode,
            )
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            if outcome.status == "failed":
                failures.append((entry.name, outcome.error_message or ""))

        # Catalog UPDATE: run only in apply mode, only after all 6 sources
        # have either landed (completed) or already existed (skipped).
        # Failed sources block the catalog update — no "rw_source_wired"
        # claim if any source didn't actually wire.
        if mode == "apply" and counts.get("failed", 0) == 0 and not args.source:
            rows_updated, current_stage = update_catalog_lifecycle(pg)
            log.info(
                "ops.data_source_catalog UPDATE for uspto_trademarks: "
                "rows=%d, lifecycle_stage=%s",
                rows_updated, current_stage,
            )
    finally:
        pg.close()
        rw.close()
        duck.close()

    log.info(
        "DONE run_id=%s completed=%d failed=%d skipped=%d dry_run=%d",
        run_id,
        counts.get("completed", 0), counts.get("failed", 0),
        counts.get("skipped", 0), counts.get("dry_run", 0),
    )
    if failures:
        log.error("FAILURES (%d):", len(failures))
        for name, err in failures:
            log.error("  %s: %s", name, err[:300])

    return 0 if counts.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
