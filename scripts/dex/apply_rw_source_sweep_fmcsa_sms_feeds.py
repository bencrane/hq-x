#!/usr/bin/env python3
"""FMCSA SMS family feeds → RisingWave source wiring.

Wires 7 FMCSA SMS R2 prefixes as RisingWave ``CREATE SOURCE ... FORMAT
PLAIN ENCODE PARQUET`` catalog entries. Sources are metadata-only — RW
does not read any Parquet until an MV consumes from the source. Cost:
near-zero CU, no barrier hydration competition with other in-flight work.

SMS = FMCSA's Compliance, Safety, Accountability (CSA) per-BASIC
percentile scoring + intervention thresholds. Substantively richer than
``carrier_essentials.safety_rating`` (single-letter grade) — load-bearing
for insurance / safety GTM motions.

Sources wired by this applier:

  source_fmcsa_sms_ab_pass                (33 cols)
  source_fmcsa_sms_ab_pass_property       (21 cols)
  source_fmcsa_sms_c_pass                 (33 cols)
  source_fmcsa_sms_c_pass_property        (21 cols)
  source_fmcsa_sms_input_inspection       (39 cols)
  source_fmcsa_sms_input_motor_carrier_census (42 cols)
  source_fmcsa_sms_input_violation        (13 cols)

Per-source flow: identical to apply_rw_source_sweep_fmcsa_event_streams.py
(PR #299) — R2 ground-truth → DuckDB DESCRIBE → all-VARCHAR validation
→ build DDL → idempotent apply → smoke (rw_catalog only, no count(*))
→ ledger.

Reuses ``ops.rw_source_wiring_runs`` (PR #249); no migration. Directive
slug stamped into ``notes->>'directive'`` for queryability.

Pre-flight invariants:

  - L0: worktree-path discipline.
  - L1: Doppler bash -c wrapper for shell expansion deferral.
  - L2: every introspected column is VARCHAR; DDL declares CHARACTER
        VARYING. Pre-verified for all 7 SMS prefixes via DuckDB DESCRIBE
        on 2026-05-10 (audit reports/2026-05-10-fmcsa-data-state-audit.md).
  - L4: ledger CHECK includes {completed, failed, skipped, dry_run}.
  - L6: smoke = rw_catalog.rw_sources only. No count(*).
  - L7: column names with mixed case / underscores are double-quoted.
  - L8: R2 ground-truth before DDL; fail-loud if 0 matches.
  - L40: NON-APPLICABLE — SMS files are PAR1 plain Parquet (no .zst
         extension wrapper, just internal column-chunk compression).
  - L42: writer-side hygiene out of scope for this directive.
  - L45: COMPLIANT — date-stamped UUID-suffixed keys, no key collisions.

Usage:
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_sms_feeds.py --apply
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_sms_feeds.py --dry-run
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_sms_feeds.py \\
          --source source_fmcsa_sms_input_violation --apply
  doppler run --project hq-all --config prd -- \\
      uv run python3 scripts/apply_rw_source_sweep_fmcsa_sms_feeds.py --smoke-only

Per-source debug budget: 5 min wall-clock.
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


DIRECTIVE_SLUG = "2026-05-10-rw-source-sweep-fmcsa-7-sms-feeds"
BUCKET = "dex-raw-landing-zone"
PER_SOURCE_BUDGET_SEC = 5 * 60


# --------------------------------------------------------------------------- #
# Source registry — 7 SMS sources (per directive)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceSpec:
    name: str
    prefix_group: str
    match_pattern: str


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "source_fmcsa_sms_ab_pass",
        "fmcsa_sms",
        "fmcsa/SMS AB Pass/*/*.parquet",
    ),
    SourceSpec(
        "source_fmcsa_sms_ab_pass_property",
        "fmcsa_sms",
        "fmcsa/SMS AB PassProperty/*/*.parquet",
    ),
    SourceSpec(
        "source_fmcsa_sms_c_pass",
        "fmcsa_sms",
        "fmcsa/SMS C Pass/*/*.parquet",
    ),
    SourceSpec(
        "source_fmcsa_sms_c_pass_property",
        "fmcsa_sms",
        "fmcsa/SMS C PassProperty/*/*.parquet",
    ),
    SourceSpec(
        "source_fmcsa_sms_input_inspection",
        "fmcsa_sms",
        "fmcsa/SMS Input - Inspection/*/*.parquet",
    ),
    SourceSpec(
        "source_fmcsa_sms_input_motor_carrier_census",
        "fmcsa_sms",
        "fmcsa/SMS Input - Motor Carrier Census/*/*.parquet",
    ),
    SourceSpec(
        "source_fmcsa_sms_input_violation",
        "fmcsa_sms",
        "fmcsa/SMS Input - Violation/*/*.parquet",
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
    return logging.getLogger("apply-fmcsa-sms-rw-sources")


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
    """Convert an s3-connector glob to a boto3-friendly prefix."""
    for ch in ("*", "?", "["):
        idx = match_pattern.find(ch)
        if idx >= 0:
            match_pattern = match_pattern[:idx]
    return match_pattern


def list_r2_keys(bucket: str, match_pattern: str) -> list[str]:
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


def introspect_columns(
    s3_uri: str, duck: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str]]:
    """Return ordered ``[(col_name, duckdb_type)]`` for one Parquet file."""
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
    columns: list[tuple[str, str]],
    bucket: str,
    match_pattern: str,
) -> str:
    """Build the full ``CREATE SOURCE`` DDL. Every column → CHARACTER VARYING."""
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

    if mode == "smoke_only":
        if not source_exists_in_rw(rw, entry.name):
            log.warning("[%s] not in rw_catalog.rw_sources — cannot smoke", entry.name)
            outcome = SourceOutcome(
                source_name=entry.name,
                prefix_group=entry.prefix_group,
                match_pattern=entry.match_pattern,
                status="failed",
                r2_object_count=None,
                introspected_column_count=None,
                ddl_text=None,
                duration_seconds=round(time.monotonic() - started, 2),
                error_message="source_not_in_rw_catalog",
                notes={"directive": DIRECTIVE_SLUG, "mode": "smoke_only"},
            )
            write_ledger(pg, run_id, outcome)
            return outcome

        all_keys = list_r2_keys(bucket, entry.match_pattern)
        outcome = SourceOutcome(
            source_name=entry.name,
            prefix_group=entry.prefix_group,
            match_pattern=entry.match_pattern,
            status="completed",
            r2_object_count=len(all_keys),
            introspected_column_count=None,
            ddl_text=None,
            duration_seconds=round(time.monotonic() - started, 2),
            error_message=None,
            notes={
                "directive": DIRECTIVE_SLUG,
                "mode": "smoke_only",
                "r2_object_count_total": len(all_keys),
            },
        )
        write_ledger(pg, run_id, outcome)
        log.info("[%s] smoke-only OK — r2_objects=%d", entry.name, len(all_keys))
        return outcome

    if mode == "apply" and source_exists_in_rw(rw, entry.name):
        log.info("[%s] already in rw_catalog.rw_sources — skipping", entry.name)
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
                "reason": "already_exists_in_rw_catalog",
            },
        )
        write_ledger(pg, run_id, outcome)
        return outcome

    try:
        budget_ok("R2 list")
        all_keys = list_r2_keys(bucket, entry.match_pattern)
        if not all_keys:
            raise RuntimeError(
                f"R2 ground-truth FAIL — 0 objects match "
                f"{entry.match_pattern!r}"
            )
        sample_key = all_keys[-1]
        log.info(
            "[%s] R2 ground-truth: %d total objects; sample=%s",
            entry.name, len(all_keys), sample_key,
        )

        budget_ok("introspect")
        columns = introspect_columns(f"s3://{bucket}/{sample_key}", duck)
        log.info(
            "[%s] introspected %d cols", entry.name, len(columns),
        )

        non_varchar = [(c, t) for c, t in columns if t.upper().strip() != "VARCHAR"]
        if non_varchar:
            raise RuntimeError(
                f"L2 contract violation — non-VARCHAR columns found: "
                f"{non_varchar}. Directive's all-VARCHAR premise is wrong; "
                f"surface to operator before applying DDL."
            )

        ddl = build_create_source_ddl(
            source_name=entry.name,
            columns=columns,
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

        budget_ok("apply")
        with rw.cursor() as cur:
            cur.execute(ddl)
        rw.commit()
        log.info("[%s] CREATE SOURCE applied", entry.name)

        if not source_exists_in_rw(rw, entry.name):
            raise RuntimeError(
                "DDL applied but source not visible in rw_catalog.rw_sources"
                " — RW catalog desync?"
            )
        log.info("[%s] rw_catalog.rw_sources smoke OK", entry.name)

        notes = {
            "directive": DIRECTIVE_SLUG,
            "sample_key": sample_key,
        }
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
                     help="For each existing source, re-check "
                          "rw_catalog.rw_sources visibility + R2 inventory.")
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
