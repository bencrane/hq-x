#!/usr/bin/env python3
"""SEC EDGAR submissions-JSON CIK metadata → RisingWave source wiring
(Stage 4 — metadata-only).

Wires ``source_sec_edgar_cik_metadata`` as a RisingWave
``CREATE SOURCE ... FORMAT PLAIN ENCODE PARQUET`` catalog entry over the
ZSTD Parquet parts at
``s3://dex-raw-landing-zone/sec-edgar/cik-metadata/snapshot=*/part-*.parquet``.

Source is metadata-only — RW does not read any Parquet until an MV consumes
from the source. Cost: near-zero CU.

Per-source flow (mirrors ``apply_sec_edgar_form_4_def_14a_rw_sources.py``):

  1. R2 ground-truth (L8): boto3 list_objects_v2 over the match_pattern;
     require ≥ 1 matching object.
  2. DuckDB-on-R2 ``DESCRIBE`` introspection on ONE sample part. Captures
     (col_name, duckdb_type) pairs.
  3. Map DuckDB types → RW DDL types (per L2). Build CREATE SOURCE DDL.
  4. Idempotency: ``SELECT 1 FROM pg_class WHERE relname = ?`` — if hit, skip.
  5. DDL apply via psycopg.
  6. pg_class smoke (L6): metadata-only, no count(*).
  7. Typed-column smoke (the L2 mitigation): for every non-VARCHAR column,
     run ``SELECT <col> ... LIMIT 5`` and verify ≥ 1 non-NULL.
     Hard-fail on (cik, address_business_state, address_mailing_state) —
     these MUST be populated. Soft-warn otherwise.
  8. Insert ledger row into ``ops.rw_source_wiring_runs`` (PR #249 ledger;
     no new migration needed). The directive slug is stamped into
     ``notes->>'directive'``.

Reference: directive
~/Desktop/hq/directives/2026-05-10-sec-edgar-cik-metadata-submissions-json-ingest.md.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with boto3 --with duckdb --with psycopg python3 \\
    scripts/apply_sec_edgar_cik_metadata_rw_source.py --apply
  doppler run --project hq-all --config prd -- \\
    uv run --with boto3 --with duckdb --with psycopg python3 \\
    scripts/apply_sec_edgar_cik_metadata_rw_source.py --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run --with boto3 --with duckdb --with psycopg python3 \\
    scripts/apply_sec_edgar_cik_metadata_rw_source.py --smoke-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

import boto3
import duckdb
import psycopg


DIRECTIVE_SLUG = "2026-05-10-sec-edgar-cik-metadata-submissions-json-ingest"
BUCKET = "dex-raw-landing-zone"
SOURCE_NAME = "source_sec_edgar_cik_metadata"
PREFIX_GROUP = "sec_edgar_cik_metadata"
MATCH_PATTERN = "sec-edgar/cik-metadata/snapshot=*/part-*.parquet"

# Hard-fail typed-column smoke on these columns — they MUST be populated on
# every row per the writer's contract. All-NULL in 5 rows = DDL type mismatch
# (L2 footgun).
HARD_FAIL_COLS: frozenset[str] = frozenset({
    "cik",
    "address_business_state",
    "address_mailing_state",
})


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-sec-edgar-cik-metadata-rw-source")


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
# DuckDB type → RisingWave DDL type
# --------------------------------------------------------------------------- #

_DUCKDB_TO_RW: dict[str, str] = {
    "VARCHAR":   "CHARACTER VARYING",
    "TEXT":      "CHARACTER VARYING",
    "STRING":    "CHARACTER VARYING",
    "BOOLEAN":   "BOOLEAN",
    "BOOL":      "BOOLEAN",
    "TINYINT":   "SMALLINT",
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
    "TIMESTAMP_NS":             "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP WITH TIME ZONE",
    "TIMESTAMPTZ":              "TIMESTAMP WITH TIME ZONE",
    "VARCHAR[]": "CHARACTER VARYING[]",
    "TEXT[]":    "CHARACTER VARYING[]",
}


def _map_duckdb_type_to_rw(t: str) -> str:
    """Map a DuckDB DESCRIBE type string to a RisingWave DDL type."""
    base = t.upper().strip()
    # Match arrays before stripping parameterization.
    if base.endswith("[]"):
        inner = base[:-2].strip()
        if "(" in inner:
            inner = inner.split("(")[0].strip()
        if inner in _DUCKDB_TO_RW:
            return _DUCKDB_TO_RW[inner] + "[]"
        if inner.startswith("DECIMAL") or inner.startswith("NUMERIC"):
            return "NUMERIC[]"
        raise RuntimeError(f"unmapped DuckDB array type: {t!r}")
    if "(" in base:
        base = base.split("(")[0].strip()
    if base in _DUCKDB_TO_RW:
        return _DUCKDB_TO_RW[base]
    if base.startswith("DECIMAL") or base.startswith("NUMERIC"):
        return "NUMERIC"
    raise RuntimeError(f"unmapped DuckDB type: {t!r}")


# --------------------------------------------------------------------------- #
# R2 ground-truth + introspection
# --------------------------------------------------------------------------- #

def _glob_to_search_prefix(match_pattern: str) -> str:
    for ch in ("*", "?", "["):
        idx = match_pattern.find(ch)
        if idx >= 0:
            match_pattern = match_pattern[:idx]
    return match_pattern


def list_r2_keys(bucket: str, match_pattern: str) -> list[str]:
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


def introspect_typed(
    s3_uri: str, duck: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str]]:
    rows = duck.execute(
        f"DESCRIBE SELECT * FROM "
        f"read_parquet('{s3_uri}', hive_partitioning=FALSE);"
    ).fetchall()
    if not rows:
        raise RuntimeError(f"DESCRIBE returned 0 columns for {s3_uri}")
    return [(r[0], r[1]) for r in rows]


# --------------------------------------------------------------------------- #
# DDL build
# --------------------------------------------------------------------------- #

def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def build_create_source_ddl(
    *, source_name: str, typed_cols: list[tuple[str, str]],
    bucket: str, match_pattern: str,
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
# Idempotency + smokes
# --------------------------------------------------------------------------- #

def source_exists_in_rw(rw: psycopg.Connection, source_name: str) -> bool:
    cur = rw.execute(
        "SELECT 1 FROM pg_class WHERE relname = %s LIMIT 1",
        (source_name,),
    )
    return cur.fetchone() is not None


def typed_column_smoke(
    rw: psycopg.Connection, source_name: str,
    typed_cols: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """For every non-VARCHAR column, fetch 5 rows and classify nullness.

    Returns ``(hard_failures, soft_warnings)``.
    """
    hard_failures: list[tuple[str, str]] = []
    soft_warnings: list[tuple[str, str]] = []

    for col_name, rw_type in typed_cols:
        is_dense_required = col_name in HARD_FAIL_COLS
        if rw_type == "CHARACTER VARYING" and not is_dense_required:
            continue

        try:
            cur = rw.execute(
                f"SELECT {_quote_ident(col_name)} FROM {source_name} "
                f"WHERE {_quote_ident(col_name)} IS NOT NULL LIMIT 5"
            )
            rows = cur.fetchall()
        except Exception as exc:
            hard_failures.append((col_name, f"query_error:{exc}"))
            continue

        non_null_count = sum(1 for r in rows if r[0] is not None)
        if non_null_count == 0:
            reason = "no_non_null_values_in_first_5"
            if is_dense_required:
                hard_failures.append((col_name, reason))
            else:
                soft_warnings.append((col_name, reason))

    return hard_failures, soft_warnings


# --------------------------------------------------------------------------- #
# Ledger write
# --------------------------------------------------------------------------- #

def write_ledger(
    pg: psycopg.Connection, *, run_id: uuid.UUID, status: str,
    r2_object_count: int | None, introspected_column_count: int | None,
    ddl_text: str | None, duration_seconds: float,
    error_message: str | None, notes: dict[str, Any] | None,
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
                str(run_id), SOURCE_NAME, PREFIX_GROUP, MATCH_PATTERN, status,
                r2_object_count, introspected_column_count, ddl_text,
                duration_seconds, duration_seconds,
                error_message,
                json.dumps(notes) if notes else None,
            ),
        )
    pg.commit()


# --------------------------------------------------------------------------- #
# Main orchestrator
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="Introspect + apply CREATE SOURCE + smoke + ledger.")
    grp.add_argument("--dry-run", action="store_true",
                     help="Introspect + emit DDL to stdout (no apply).")
    grp.add_argument("--smoke-only", action="store_true",
                     help="Re-run typed-column smoke on existing source.")
    args = parser.parse_args()

    mode = "apply" if args.apply else ("dry_run" if args.dry_run else "smoke_only")
    run_id = uuid.uuid4()
    started = time.monotonic()
    log.info(
        "directive=%s run_id=%s mode=%s source=%s match_pattern=%s",
        DIRECTIVE_SLUG, run_id, mode, SOURCE_NAME, MATCH_PATTERN,
    )

    pg = _pg_conn()
    rw = _rw_conn()
    duck = _duck_with_r2()

    try:
        # Idempotency check (apply mode only).
        if mode == "apply" and source_exists_in_rw(rw, SOURCE_NAME):
            log.info("[%s] already in pg_class — skipping", SOURCE_NAME)
            write_ledger(
                pg, run_id=run_id, status="skipped",
                r2_object_count=None, introspected_column_count=None,
                ddl_text=None,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=None,
                notes={"directive": DIRECTIVE_SLUG, "reason": "already_exists_in_pg_class"},
            )
            return 0

        # R2 ground-truth (L8).
        all_keys = list_r2_keys(BUCKET, MATCH_PATTERN)
        if not all_keys:
            raise RuntimeError(
                f"R2 ground-truth FAIL — 0 objects match {MATCH_PATTERN!r}"
            )
        sample_key = all_keys[-1]  # latest snapshot's last part
        log.info("[%s] R2 ground-truth: %d objects; sample=%s",
                 SOURCE_NAME, len(all_keys), sample_key)

        # Smoke-only path.
        if mode == "smoke_only":
            if not source_exists_in_rw(rw, SOURCE_NAME):
                raise RuntimeError("source not in pg_class — cannot smoke")
            samples = introspect_typed(f"s3://{BUCKET}/{sample_key}", duck)
            typed_cols = [(c, _map_duckdb_type_to_rw(t)) for c, t in samples]
            hard, soft = typed_column_smoke(rw, SOURCE_NAME, typed_cols)
            status = "failed" if hard else "completed"
            err = (
                f"smoke_failed:{','.join(c for c, _ in hard)}"
                if hard else None
            )
            notes = {
                "directive": DIRECTIVE_SLUG,
                "mode": "smoke_only",
                "r2_object_count": len(all_keys),
                "smoke_hard_failures": [list(p) for p in hard],
                "smoke_soft_warnings": [list(p) for p in soft],
            }
            write_ledger(
                pg, run_id=run_id, status=status,
                r2_object_count=len(all_keys),
                introspected_column_count=len(typed_cols),
                ddl_text=None,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=err, notes=notes,
            )
            log.info("[%s] smoke-only %s — hard=%d soft=%d",
                     SOURCE_NAME, status, len(hard), len(soft))
            return 0 if not hard else 1

        # Apply / dry-run path: introspect.
        samples = introspect_typed(f"s3://{BUCKET}/{sample_key}", duck)
        typed_cols = [(c, _map_duckdb_type_to_rw(t)) for c, t in samples]
        log.info("[%s] introspected %d cols: types=%s",
                 SOURCE_NAME, len(typed_cols),
                 sorted({t for _, t in typed_cols}))

        ddl = build_create_source_ddl(
            source_name=SOURCE_NAME, typed_cols=typed_cols,
            bucket=BUCKET, match_pattern=MATCH_PATTERN,
        )

        if mode == "dry_run":
            log.info("[%s] DRY-RUN — DDL emitted", SOURCE_NAME)
            print(f"-- {SOURCE_NAME}")
            print(ddl + ";")
            write_ledger(
                pg, run_id=run_id, status="dry_run",
                r2_object_count=len(all_keys),
                introspected_column_count=len(typed_cols),
                ddl_text=ddl,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=None,
                notes={"directive": DIRECTIVE_SLUG, "sample_key": sample_key},
            )
            return 0

        # Apply DDL.
        with rw.cursor() as cur:
            cur.execute(ddl)
        rw.commit()
        log.info("[%s] CREATE SOURCE applied", SOURCE_NAME)

        # pg_class smoke (L6).
        if not source_exists_in_rw(rw, SOURCE_NAME):
            raise RuntimeError(
                "DDL applied but source not visible in pg_class — RW catalog desync?"
            )
        log.info("[%s] pg_class smoke OK", SOURCE_NAME)

        # Typed-column smoke (L2 footgun safety net).
        hard, soft = typed_column_smoke(rw, SOURCE_NAME, typed_cols)
        if hard:
            log.error(
                "[%s] TYPED-COLUMN SMOKE FAILED: %d hard failures: %s",
                SOURCE_NAME, len(hard), hard,
            )
        if soft:
            log.warning(
                "[%s] typed-column smoke soft warnings: %d cols: %s",
                SOURCE_NAME, len(soft), soft,
            )
        if not hard and not soft:
            log.info("[%s] typed-column smoke OK", SOURCE_NAME)

        notes = {
            "directive": DIRECTIVE_SLUG,
            "sample_key": sample_key,
            "smoke_hard_failures": [list(p) for p in hard],
            "smoke_soft_warnings": [list(p) for p in soft],
        }
        status = "failed" if hard else "completed"
        write_ledger(
            pg, run_id=run_id, status=status,
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
        return 0 if not hard else 1

    except Exception as exc:
        try:
            rw.rollback()
        except Exception:
            pass
        log.exception("[%s] FAILED", SOURCE_NAME)
        write_ledger(
            pg, run_id=run_id, status="failed",
            r2_object_count=None, introspected_column_count=None,
            ddl_text=None,
            duration_seconds=round(time.monotonic() - started, 2),
            error_message=str(exc)[:1000],
            notes={"directive": DIRECTIVE_SLUG},
        )
        return 1
    finally:
        pg.close()
        rw.close()
        duck.close()


if __name__ == "__main__":
    sys.exit(main())
