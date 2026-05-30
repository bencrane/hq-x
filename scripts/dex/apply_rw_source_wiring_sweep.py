#!/usr/bin/env python3
"""Apply RisingWave sources for every R2 prefix listed in the wiring-sweep
config. Sources are metadata-only catalog entries (CREATE SOURCE … FORMAT
PLAIN ENCODE PARQUET); no MVs, no hydration past `pg_class` visibility.

Outcome: every R2 dataset in scope is queryable in RisingWave for ad-hoc
exploration AND ready as a building block for downstream DuckDB-bridge /
identity-spine MVs (directives #2 and onward).

Usage:
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_wiring_sweep.py --apply
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_wiring_sweep.py --dry-run
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_wiring_sweep.py --source source_fmcsa_company_census --apply

Pre-flight invariants (per directive 2026-05-08-rw-source-wiring-sweep.md):
  - L0: worktree-path discipline.
  - L2: every Parquet column declared CHARACTER VARYING.
  - L4: ledger CHECK includes {completed, failed, skipped, dry_run}.
  - L6: smoke = `pg_class` row only, never count(*) (S3 sources hydrate on
        first read; this directive defers hydration to directives #2+).
  - L7: FMCSA spaces-in-prefix glob verified in production 2026-05-08.
  - L8: R2 inventory ground-truth before introspection; fail-loud if 0.

Per-source debug budget: 15 min wall-clock. If a source's R2 inventory or
schema introspection exceeds that, log `failed` and continue.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import boto3
import duckdb
import psycopg
import yaml
from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-rw-source-wiring-sweep")


log = _logger()


# --------------------------------------------------------------------------- #
# Config schema
# --------------------------------------------------------------------------- #


class SourceEntry(BaseModel):
    name: str = Field(..., min_length=1)
    prefix_group: str = Field(..., min_length=1)
    match_pattern: str = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def _name_must_be_lower_snake(cls, v: str) -> str:
        if not v.startswith("source_"):
            raise ValueError(f"source name {v!r} must start with 'source_'")
        if not all(c.islower() or c.isdigit() or c == "_" for c in v):
            raise ValueError(f"source name {v!r} must be lower_snake_case")
        return v


class SweepConfig(BaseModel):
    bucket: str = Field(..., min_length=1)
    sources: list[SourceEntry]


# --------------------------------------------------------------------------- #
# Env helpers
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


# --------------------------------------------------------------------------- #
# DuckDB-on-R2 introspection
# --------------------------------------------------------------------------- #


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


def _glob_to_search_prefix(match_pattern: str) -> str:
    """Convert an s3 connector glob to a boto3-friendly prefix.

    boto3 list_objects_v2 has no glob support — it just takes a literal
    prefix. We strip everything from the first wildcard onward, leaving
    a directory-shaped prefix that's strictly broader than the glob
    (we filter the result client-side via .endswith / fnmatch).
    """
    for ch in ("*", "?", "["):
        idx = match_pattern.find(ch)
        if idx >= 0:
            match_pattern = match_pattern[:idx]
    return match_pattern


def list_r2_keys(bucket: str, match_pattern: str) -> list[str]:
    """Return up to 5 matching R2 keys for a given match_pattern.

    We don't need the full listing — just enough to (a) prove ≥1 match
    exists (ground-truth check) and (b) pick one for schema introspection.
    """
    import fnmatch
    s3 = boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="us-east-1",
    )
    search_prefix = _glob_to_search_prefix(match_pattern)
    matches: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=search_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if fnmatch.fnmatchcase(key, match_pattern):
                matches.append(key)
                if len(matches) >= 5:
                    return matches
    return matches


def introspect_one(s3_uri: str, duck: duckdb.DuckDBPyConnection) -> list[str]:
    """Return ordered list of column names from a Parquet file. All columns
    are emitted as CHARACTER VARYING in the DDL regardless of underlying
    Parquet type (lesson L2)."""
    rows = duck.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_uri}');"
    ).fetchall()
    cols = [r[0] for r in rows]
    if not cols:
        raise RuntimeError(f"DESCRIBE returned 0 columns for {s3_uri}")
    return cols


# --------------------------------------------------------------------------- #
# DDL generation
# --------------------------------------------------------------------------- #


def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def build_create_source_ddl(
    *,
    source_name: str,
    columns: list[str],
    bucket: str,
    match_pattern: str,
) -> str:
    cols_decl = ",\n    ".join(
        f"{_quote_ident(c)} CHARACTER VARYING" for c in columns
    )
    return f"""\
CREATE SOURCE {source_name} (
    {cols_decl}
) WITH (
    connector = 's3',
    s3.region_name = 'us-east-1',
    s3.bucket_name = '{bucket}',
    s3.endpoint_url = '{_required_env("R2_ENDPOINT")}',
    s3.credentials.access = '{_required_env("R2_ACCESS_KEY_ID")}',
    s3.credentials.secret = '{_required_env("R2_SECRET_ACCESS_KEY")}',
    match_pattern = '{match_pattern}'
) FORMAT PLAIN ENCODE PARQUET"""


# --------------------------------------------------------------------------- #
# Idempotency: pg_class lookup
# --------------------------------------------------------------------------- #


def source_exists_in_rw(rw: psycopg.Connection, source_name: str) -> bool:
    cur = rw.execute(
        "SELECT 1 FROM pg_class WHERE relname = %s LIMIT 1",
        (source_name,),
    )
    return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# Per-source apply
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
    notes: dict | None


PER_SOURCE_BUDGET_SEC = 15 * 60


def process_source(
    *,
    entry: SourceEntry,
    bucket: str,
    rw: psycopg.Connection,
    pg: psycopg.Connection,
    run_id: uuid.UUID,
    duck: duckdb.DuckDBPyConnection,
    dry_run: bool,
) -> SourceOutcome:
    started = time.monotonic()

    # Skip if already wired (idempotency).
    if source_exists_in_rw(rw, entry.name):
        log.info("[%s] already exists in pg_class — skipping", entry.name)
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
            notes={"reason": "already_exists_in_pg_class"},
        )
        write_ledger(pg, run_id, outcome)
        return outcome

    try:
        # R2 ground-truth (lesson L8).
        if time.monotonic() - started > PER_SOURCE_BUDGET_SEC:
            raise TimeoutError(f"per-source budget exceeded before R2 list")
        keys = list_r2_keys(bucket, entry.match_pattern)
        if not keys:
            raise RuntimeError(
                f"R2 ground-truth FAIL — 0 objects match {entry.match_pattern!r}"
            )
        log.info(
            "[%s] R2 ground-truth: %d sample matches (probe=%s)",
            entry.name, len(keys), keys[0],
        )

        # Schema introspection on one matching file.
        if time.monotonic() - started > PER_SOURCE_BUDGET_SEC:
            raise TimeoutError(f"per-source budget exceeded before introspect")
        sample_uri = f"s3://{bucket}/{keys[0]}"
        cols = introspect_one(sample_uri, duck)
        log.info("[%s] introspected %d columns", entry.name, len(cols))

        # Build DDL.
        ddl = build_create_source_ddl(
            source_name=entry.name,
            columns=cols,
            bucket=bucket,
            match_pattern=entry.match_pattern,
        )

        if dry_run:
            log.info("[%s] DRY-RUN — DDL emitted (not applied)", entry.name)
            print(f"-- {entry.name}")
            print(ddl + ";")
            print()
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status="dry_run",
                r2_object_count=len(keys),
                introspected_column_count=len(cols),
                ddl_text=ddl,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=None,
                notes={"sample_key": keys[0]},
            )
            write_ledger(pg, run_id, outcome)
            return outcome

        # Apply DDL.
        if time.monotonic() - started > PER_SOURCE_BUDGET_SEC:
            raise TimeoutError(f"per-source budget exceeded before apply")
        with rw.cursor() as cur:
            cur.execute(ddl)
        rw.commit()
        log.info("[%s] CREATE SOURCE applied", entry.name)

        # Smoke: pg_class only (lesson L6 — never count(*)).
        if not source_exists_in_rw(rw, entry.name):
            raise RuntimeError(
                "DDL applied but source not visible in pg_class — "
                "RW catalog desync?"
            )
        log.info("[%s] smoke OK — visible in pg_class", entry.name)

        outcome = SourceOutcome(
            source_name=entry.name,
            prefix_group=entry.prefix_group,
            match_pattern=entry.match_pattern,
            status="completed",
            r2_object_count=len(keys),
            introspected_column_count=len(cols),
            ddl_text=ddl,
            duration_seconds=round(time.monotonic() - started, 2),
            error_message=None,
            notes={"sample_key": keys[0]},
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
            notes=None,
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
    import json
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


def load_config(path: Path) -> SweepConfig:
    with path.open() as f:
        raw = yaml.safe_load(f)
    return SweepConfig.model_validate(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="scripts/_config/rw_source_wiring_sweep.yaml",
        help="Path to sweep config YAML",
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="Apply DDL to RW")
    grp.add_argument("--dry-run", action="store_true", help="Emit DDL only")
    parser.add_argument(
        "--source",
        default=None,
        help="Apply only one source by name (must be present in config)",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    cfg = load_config(cfg_path)
    log.info(
        "config: bucket=%s sources=%d", cfg.bucket, len(cfg.sources)
    )

    if args.source:
        cfg.sources = [s for s in cfg.sources if s.name == args.source]
        if not cfg.sources:
            log.error("--source %s not found in config", args.source)
            return 2

    run_id = uuid.uuid4()
    log.info("run_id=%s mode=%s", run_id, "dry_run" if args.dry_run else "apply")

    pg = _pg_conn()
    rw = _rw_conn()
    duck = _duck_with_r2()

    counts = {"completed": 0, "failed": 0, "skipped": 0, "dry_run": 0}
    failures: list[tuple[str, str]] = []

    try:
        for entry in cfg.sources:
            outcome = process_source(
                entry=entry,
                bucket=cfg.bucket,
                rw=rw,
                pg=pg,
                run_id=run_id,
                duck=duck,
                dry_run=args.dry_run,
            )
            counts[outcome.status] += 1
            if outcome.status == "failed":
                failures.append((entry.name, outcome.error_message or ""))
    finally:
        pg.close()
        rw.close()
        duck.close()

    log.info(
        "DONE run_id=%s completed=%d failed=%d skipped=%d dry_run=%d",
        run_id,
        counts["completed"], counts["failed"],
        counts["skipped"], counts["dry_run"],
    )
    if failures:
        log.error("FAILURES (%d):", len(failures))
        for name, err in failures:
            log.error("  %s: %s", name, err[:200])

    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
