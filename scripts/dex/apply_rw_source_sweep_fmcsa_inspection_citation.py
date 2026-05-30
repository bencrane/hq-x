#!/usr/bin/env python3
"""FMCSA 3 Granular Inspection/Citation Feeds → RisingWave source wiring
(Stage 4 — metadata-only).

Wires 3 FMCSA daily-delta R2 prefixes as RisingWave ``CREATE SOURCE ...
FORMAT PLAIN ENCODE PARQUET`` catalog entries. Sources are metadata-only —
RW does not read any Parquet until an MV consumes from the source.
Cost: near-zero CU.

Per-source flow:

  1. R2 ground-truth (L8): boto3 list_objects_v2 over the stream's
     match_pattern; require >= 1 matching object.
  2. DuckDB-on-R2 ``DESCRIBE`` introspection on ONE sample file; verify
     every column is VARCHAR (the directive's all-VARCHAR premise).
  3. Build CREATE SOURCE DDL with every column as ``CHARACTER VARYING``.
  4. Idempotency: ``SELECT 1 FROM rw_catalog.rw_sources WHERE name = ?`` --
     if hit, skip.
  5. DDL apply via psycopg.
  6. rw_sources smoke (L6): metadata-only, no count(*).
  7. Insert ledger row into ``ops.rw_source_wiring_runs``.

Reuses the ``ops.rw_source_wiring_runs`` ledger from PR #249 -- no new
migration. The directive slug is stamped into ``notes->>'directive'`` so
this batch is queryable separately.

Pre-flight invariants (per directive
2026-05-10-rw-source-sweep-fmcsa-3-inspection-citation-feeds):

  - L0: worktree-path discipline.
  - L1: Doppler bash -c wrapper for env expansion.
  - L2: source DDL types match the Parquet writer's actual types --
        all-VARCHAR for this sweep; the L2 footgun cannot fire.
  - L4: ledger CHECK includes {completed, failed, skipped, dry_run}.
  - L6: smoke = rw_sources only for metadata; no count(*) eager hydration.
  - L7: column names from upper-snake source set are double-quoted in DDL.
  - L8: R2 ground-truth before DDL; fail-loud if 0 matches.
  - L40: VERIFIED NON-APPLICABLE -- files are PAR1 with internal column-
        chunk ZSTD; RW reads natively.
  - L45: VERIFIED COMPLIANT -- date-stamped UUID-suffixed unique-per-update
        keys; match_pattern globs picks them up daily.

Usage:
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_inspection_citation.py --apply
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_inspection_citation.py --dry-run
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_inspection_citation.py \\
          --source source_fmcsa_inspections_per_unit --apply
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_inspection_citation.py --smoke-only
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


DIRECTIVE_SLUG = "2026-05-10-rw-source-sweep-fmcsa-3-inspection-citation-feeds"
BUCKET = "dex-raw-landing-zone"
PER_SOURCE_BUDGET_SEC = 15 * 60


# --------------------------------------------------------------------------- #
# Source registry -- 3 sources hardcoded (per directive)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceSpec:
    name: str
    prefix_group: str
    match_pattern: str


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "source_fmcsa_inspections_per_unit",
        "fmcsa_inspection_citation",
        "fmcsa/Inspections Per Unit/*/*.parquet.zst",
    ),
    SourceSpec(
        "source_fmcsa_inspections_and_citations",
        "fmcsa_inspection_citation",
        "fmcsa/Inspections and Citations/*/*.parquet.zst",
    ),
    SourceSpec(
        "source_fmcsa_special_studies",
        "fmcsa_inspection_citation",
        "fmcsa/Special Studies/*/*.parquet.zst",
    ),
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-fmcsa-inspection-citation-rw-sources")


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
    """Convert an s3-connector glob to a boto3-friendly prefix.

    ``list_objects_v2`` has no glob support -- strip everything from the
    first wildcard onward, leaving a directory-shaped prefix that is
    strictly broader than the glob (we filter the result via fnmatch).
    """
    for ch in ("*", "?", "["):
        idx = match_pattern.find(ch)
        if idx >= 0:
            match_pattern = match_pattern[:idx]
    return match_pattern


def list_r2_keys(bucket: str, match_pattern: str, max_keys: int | None = None) -> list[str]:
    """Return R2 keys matching the glob, sorted lexicographically.

    For these 3 daily-delta sources the result is dozens-to-hundreds of
    keys (one per day since first ingest). Caller picks one for sample
    introspection.
    """
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
                if max_keys is not None and len(matches) >= max_keys:
                    matches.sort()
                    return matches
    matches.sort()
    return matches


def introspect_columns(
    s3_uri: str, duck: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str]]:
    """Return ordered ``[(col_name, duckdb_type)]`` for one Parquet file.

    Uses DuckDB ``DESCRIBE SELECT * FROM read_parquet(...,
    hive_partitioning=FALSE)`` -- reads only the Parquet metadata header.
    """
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
    *,
    source_name: str,
    columns: list[tuple[str, str]],  # [(col_name, duckdb_type)]
    bucket: str,
    match_pattern: str,
) -> str:
    """All columns emit as CHARACTER VARYING (per directive's all-VARCHAR
    premise; verified upstream in ``introspect_columns``)."""
    cols_decl = ",\n    ".join(
        f"{_quote_ident(c)} CHARACTER VARYING" for c, _ in columns
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
        "SELECT 1 FROM rw_catalog.rw_sources WHERE name = %s LIMIT 1",
        (source_name,),
    )
    return cur.fetchone() is not None


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
    mode: str,  # 'apply' | 'dry_run' | 'smoke_only'
) -> SourceOutcome:
    started = time.monotonic()

    def budget_ok(stage: str) -> None:
        if time.monotonic() - started > PER_SOURCE_BUDGET_SEC:
            raise TimeoutError(f"per-source budget exceeded before {stage}")

    # ---- Smoke-only path. ----
    if mode == "smoke_only":
        try:
            exists = source_exists_in_rw(rw, entry.name)
            status = "completed" if exists else "failed"
            err = None if exists else "source_not_in_rw_sources"
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status=status,
                r2_object_count=None,
                introspected_column_count=None,
                ddl_text=None,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=err,
                notes={"directive": DIRECTIVE_SLUG, "mode": "smoke_only"},
            )
            write_ledger(pg, run_id, outcome)
            log.info("[%s] smoke-only %s", entry.name, status)
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

    # ---- Apply / dry-run path. ----

    # Idempotency: skip if source already exists (apply mode only).
    if mode == "apply" and source_exists_in_rw(rw, entry.name):
        log.info("[%s] already in rw_sources -- skipping", entry.name)
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
                "reason": "already_exists_in_rw_sources",
            },
        )
        write_ledger(pg, run_id, outcome)
        return outcome

    try:
        # R2 ground-truth (L8). Enumerate full set for ledger metadata; pick
        # latest key for sample introspection.
        budget_ok("R2 list")
        all_keys = list_r2_keys(bucket, entry.match_pattern)
        if not all_keys:
            raise RuntimeError(
                f"R2 ground-truth FAIL -- 0 objects match "
                f"{entry.match_pattern!r}"
            )
        sample_key = all_keys[-1]
        log.info(
            "[%s] R2 ground-truth: %d total objects; sample=%s",
            entry.name, len(all_keys), sample_key,
        )

        # Schema introspection (L2). Verify all-VARCHAR.
        budget_ok("introspect")
        sample_uri = f"s3://{bucket}/{sample_key}"
        columns = introspect_columns(sample_uri, duck)
        non_varchar = [
            (c, t) for c, t in columns
            if t.upper().split("(")[0].strip() not in ("VARCHAR", "TEXT", "STRING")
        ]
        if non_varchar:
            raise RuntimeError(
                f"directive's all-VARCHAR premise violated: {non_varchar!r}"
            )
        log.info(
            "[%s] introspected %d cols, all VARCHAR",
            entry.name, len(columns),
        )

        # Build DDL.
        ddl = build_create_source_ddl(
            source_name=entry.name,
            columns=columns,
            bucket=bucket,
            match_pattern=entry.match_pattern,
        )

        if mode == "dry_run":
            log.info("[%s] DRY-RUN -- DDL emitted", entry.name)
            print(f"-- {entry.name}")
            print(ddl + ";")
            print()
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status="dry_run",
                r2_object_count=len(all_keys),
                introspected_column_count=len(columns),
                ddl_text=ddl,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message=None,
                notes={
                    "directive": DIRECTIVE_SLUG,
                    "sample_key": sample_key,
                },
            )
            write_ledger(pg, run_id, outcome)
            return outcome

        # Apply DDL.
        budget_ok("apply")
        with rw.cursor() as cur:
            cur.execute(ddl)
        rw.commit()
        log.info("[%s] CREATE SOURCE applied", entry.name)

        # rw_sources smoke (L6).
        if not source_exists_in_rw(rw, entry.name):
            raise RuntimeError(
                "DDL applied but source not visible in rw_sources -- "
                "RW catalog desync?"
            )
        log.info("[%s] rw_sources smoke OK", entry.name)

        outcome = SourceOutcome(
            source_name=entry.name,
            prefix_group=entry.prefix_group,
            match_pattern=entry.match_pattern,
            status="completed",
            r2_object_count=len(all_keys),
            introspected_column_count=len(columns),
            ddl_text=ddl,
            duration_seconds=round(time.monotonic() - started, 2),
            error_message=None,
            notes={
                "directive": DIRECTIVE_SLUG,
                "sample_key": sample_key,
            },
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
                     help="For each existing source, verify rw_sources presence + ledger.")
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
