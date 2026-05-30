#!/usr/bin/env python3
"""Apply IRS Form 990 RW source wiring + nonprofit-principal audience MVs.

Reads scripts/_config/irs_990_sources.yaml and:

  1. (Source phase, L2/L6/L8) For each of 7 source_irs_990_* entries:
        - Boto3 list_objects to confirm ≥1 R2 match (ground-truth).
        - DuckDB-on-R2 introspect one Parquet for column list.
        - Emit + apply CREATE SOURCE (cols=CHARACTER VARYING, FORMAT PLAIN
          ENCODE PARQUET).
        - Smoke = pg_class visibility only (no count(*)).

  2. (Aggregation MV phase, L24) For each aggregation MV:
        SET BACKGROUND_DDL = TRUE;
        DROP MATERIALIZED VIEW IF EXISTS public.<mv_name>;
        CREATE MATERIALIZED VIEW public.<mv_name> AS <select_template>;
     Wait for hydration (poll rw_ddl_progress, timeout 60min).

  3. (Audience MV phase, L24/L25/L30) For each audience MV:
        - DDL apply via BACKGROUND_DDL.
        - Wait for hydration.
        - Smoke gate: row-count-in-range + named-smoke ≥ smoke_min (HARD).
        - Upsert ops.audience_mv_specs.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] --with boto3 \\
        --with duckdb --with pydantic python \\
        apps/data-engine-x/scripts/apply_irs_990_rw_wiring.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] --with boto3 \\
        --with duckdb --with pydantic python \\
        apps/data-engine-x/scripts/apply_irs_990_rw_wiring.py --apply

    # Sources only (cheap; commit before MVs):
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] --with boto3 \\
        --with duckdb --with pydantic python \\
        apps/data-engine-x/scripts/apply_irs_990_rw_wiring.py --apply --sources-only

    # Aggregation MVs only:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] --with boto3 \\
        --with duckdb --with pydantic python \\
        apps/data-engine-x/scripts/apply_irs_990_rw_wiring.py --apply \\
        --aggregation-mvs-only

    # Audience MVs only:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] --with boto3 \\
        --with duckdb --with pydantic python \\
        apps/data-engine-x/scripts/apply_irs_990_rw_wiring.py --apply \\
        --audiences-only

    # Verify-only (re-smoke + re-seed audience_mv_specs):
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with psycopg[binary] --with boto3 \\
        --with duckdb --with pydantic python \\
        apps/data-engine-x/scripts/apply_irs_990_rw_wiring.py --verify-audiences

See directive ~/Desktop/hq/directives/2026-05-09-irs-990-rw-wiring-and-audience-mvs.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_irs_990_rw_wiring")

REPO_ROOT = Path(__file__).resolve().parents[3]
YAML_PATH = (
    REPO_ROOT
    / "apps/data-engine-x/scripts/_config/irs_990_sources.yaml"
)

# Hydration wait caps. Aggregation MVs unioning multi-year Parquet are slower.
PER_SOURCE_BUDGET_SEC = 5 * 60   # source DDL admit
AGG_MV_WAIT_TIMEOUT_S = 90 * 60   # 90 min for big-fan-in agg MVs
AUDIENCE_MV_WAIT_TIMEOUT_S = 60 * 60  # 60 min for derived audiences
POLL_INTERVAL_S = 30


# ──────────────────────────────────────────────────────────────────────────────
# Config schema (Pydantic).
# ──────────────────────────────────────────────────────────────────────────────


def _load_config() -> dict:
    import yaml

    with YAML_PATH.open() as f:
        cfg = yaml.safe_load(f)
    if "bucket" not in cfg:
        raise SystemExit("YAML missing 'bucket' key")
    if "sources" not in cfg or len(cfg["sources"]) != 7:
        raise SystemExit(
            f"Expected 7 sources in YAML, got {len(cfg.get('sources', []))}"
        )
    if "aggregation_mvs" not in cfg or len(cfg["aggregation_mvs"]) != 2:
        raise SystemExit(
            f"Expected 2 aggregation_mvs in YAML, got {len(cfg.get('aggregation_mvs', []))}"
        )
    if "audience_mvs" not in cfg or len(cfg["audience_mvs"]) != 4:
        raise SystemExit(
            f"Expected 4 audience_mvs in YAML, got {len(cfg.get('audience_mvs', []))}"
        )
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# RW + PG psql shell-out helpers (matches apply_fmcsa_audience_mvs_rw.py shape).
# ──────────────────────────────────────────────────────────────────────────────


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"FAIL: required env var {name} is not set")
    return v


def _rw_psql(sql: str, *, fetch: bool = False, timeout_s: int | None = 120) -> str:
    cmd = [
        "psql",
        "-h", _required_env("RISINGWAVE_HOST"),
        "-p", _required_env("RISINGWAVE_PORT"),
        "-U", _required_env("RISINGWAVE_USER"),
        "-d", _required_env("RISINGWAVE_DATABASE"),
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    env = {**os.environ, "PGPASSWORD": _required_env("RISINGWAVE_PASSWORD")}
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, check=False,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _rw_psql_script(sql_script: str, *, timeout_s: int | None = None) -> str:
    cmd = [
        "psql",
        "-h", _required_env("RISINGWAVE_HOST"),
        "-p", _required_env("RISINGWAVE_PORT"),
        "-U", _required_env("RISINGWAVE_USER"),
        "-d", _required_env("RISINGWAVE_DATABASE"),
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    env = {**os.environ, "PGPASSWORD": _required_env("RISINGWAVE_PASSWORD")}
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            input=sql_script,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "RW psql script timed out after %ss — DDL likely admitted "
            "(verify via SHOW JOBS / pg_class)",
            timeout_s,
        )
        return "<psql_timeout>"
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql script failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _pg_psql(sql: str, *, fetch: bool = False) -> str:
    cmd = ["psql", _required_env("DEX_DB_URL_DIRECT"),
           "--no-psqlrc", "-v", "ON_ERROR_STOP=1"]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"PG psql failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


# ──────────────────────────────────────────────────────────────────────────────
# R2 ground-truth + DuckDB introspection.
# ──────────────────────────────────────────────────────────────────────────────


def _glob_to_search_prefix(match_pattern: str) -> str:
    for ch in ("*", "?", "["):
        idx = match_pattern.find(ch)
        if idx >= 0:
            match_pattern = match_pattern[:idx]
    return match_pattern


def _list_r2_keys(bucket: str, match_pattern: str) -> list[str]:
    import fnmatch
    import boto3
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


def _duck_with_r2():
    import duckdb
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


def _duckdb_type_to_rw(duck_type: str) -> str:
    """Map a DuckDB column type to its RisingWave equivalent.

    Background: source DDL types must match the Parquet column types or the
    S3-Parquet connector silently NULLs the column at read time. Earlier
    versions of this script declared every column as CHARACTER VARYING
    (per the L2 "Parquet=VARCHAR everywhere" convention), but that only
    holds when the Parquet writer itself emits all-VARCHAR. The IRS 990
    ingest writes typed Parquet (DOUBLE / BOOLEAN / BIGINT / DATE), so the
    source DDL must mirror those types.
    """
    t = duck_type.upper()
    # Strip parameterized suffixes ("DECIMAL(18,2)" -> "DECIMAL").
    base = t.split("(")[0]
    mapping = {
        "VARCHAR": "CHARACTER VARYING",
        "TEXT": "CHARACTER VARYING",
        "STRING": "CHARACTER VARYING",
        "BIGINT": "BIGINT",
        "INTEGER": "INTEGER",
        "INT": "INTEGER",
        "SMALLINT": "SMALLINT",
        "TINYINT": "SMALLINT",
        "DOUBLE": "DOUBLE PRECISION",
        "FLOAT": "REAL",
        "REAL": "REAL",
        "DECIMAL": "NUMERIC",
        "NUMERIC": "NUMERIC",
        "BOOLEAN": "BOOLEAN",
        "BOOL": "BOOLEAN",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
    }
    if base not in mapping:
        # Unknown / nested types fall back to VARCHAR — RW will read whatever
        # the Parquet contains via string repr; better than failing the DDL.
        return "CHARACTER VARYING"
    return mapping[base]


def _introspect_columns(s3_uri: str, duck) -> list[tuple[str, str]]:
    """Return [(column_name, duck_type)] for the Parquet at s3_uri."""
    rows = duck.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_uri}');"
    ).fetchall()
    cols = [(r[0], r[1]) for r in rows]
    if not cols:
        raise SystemExit(f"DESCRIBE returned 0 columns for {s3_uri}")
    return cols


def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _build_create_source_ddl(
    *, source_name: str, columns: list[tuple[str, str]], bucket: str,
    match_pattern: str,
) -> str:
    cols_decl = ",\n    ".join(
        f"{_quote_ident(name)} {_duckdb_type_to_rw(duck_type)}"
        for name, duck_type in columns
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


# ──────────────────────────────────────────────────────────────────────────────
# Idempotency.
# ──────────────────────────────────────────────────────────────────────────────


def _rw_object_exists(name: str) -> bool:
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{name}';",
        fetch=True,
    ).strip()
    return name in out


def _rw_ddl_in_progress(name: str) -> bool:
    out = _rw_psql(
        "SELECT ddl_statement FROM rw_catalog.rw_ddl_progress;",
        fetch=True,
    ).strip()
    return f"public.{name}" in out or f" {name} " in out or f" {name}(" in out


def _wait_for_hydration(mv_name: str, *, timeout_s: int) -> str:
    """Poll rw_ddl_progress until mv_name is no longer in flight."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _rw_psql(
            f"SELECT ddl_statement FROM rw_catalog.rw_ddl_progress "
            f"WHERE ddl_statement ILIKE '%{mv_name}%';",
            fetch=True,
        ).strip()
        if not out:
            return "hydrated"
        logger.info("waiting on %s — still in rw_ddl_progress", mv_name)
        time.sleep(POLL_INTERVAL_S)
    return "timeout"


# ──────────────────────────────────────────────────────────────────────────────
# Source phase.
# ──────────────────────────────────────────────────────────────────────────────


def _apply_sources(cfg: dict, *, dry_run: bool) -> list[tuple[str, str, int, int]]:
    """Apply 7 sources. Return (name, status, r2_count, col_count) per source."""
    duck = _duck_with_r2()
    results: list[tuple[str, str, int, int]] = []

    for entry in cfg["sources"]:
        name = entry["name"]
        match_pattern = entry["match_pattern"]
        logger.info("[source] %s — pattern=%s", name, match_pattern)

        if _rw_object_exists(name):
            logger.info("[source] %s already in pg_class — skip", name)
            results.append((name, "skipped", 0, 0))
            continue

        keys = _list_r2_keys(cfg["bucket"], match_pattern)
        if not keys:
            raise SystemExit(
                f"FAIL: {name} match_pattern '{match_pattern}' "
                f"matches 0 R2 objects (L8 ground-truth)"
            )
        logger.info("[source] %s — R2 ground-truth %d sample matches "
                    "(probe=%s)", name, len(keys), keys[0])

        sample_uri = f"s3://{cfg['bucket']}/{keys[0]}"
        cols = _introspect_columns(sample_uri, duck)
        logger.info("[source] %s — introspected %d columns", name, len(cols))

        ddl = _build_create_source_ddl(
            source_name=name,
            columns=cols,
            bucket=cfg["bucket"],
            match_pattern=match_pattern,
        )

        if dry_run:
            print(f"-- {name}")
            print(ddl + ";\n")
            results.append((name, "dry_run", len(keys), len(cols)))
            continue

        try:
            _rw_psql_script(ddl + ";\n", timeout_s=PER_SOURCE_BUDGET_SEC)
        except SystemExit as exc:
            logger.error("[source] %s DDL FAILED: %s", name, str(exc)[:300])
            results.append((name, "failed", len(keys), len(cols)))
            continue

        if not _rw_object_exists(name):
            logger.error(
                "[source] %s applied but missing from pg_class — catalog desync",
                name,
            )
            results.append((name, "failed", len(keys), len(cols)))
            continue

        logger.info("[source] %s — applied + visible in pg_class", name)
        results.append((name, "completed", len(keys), len(cols)))

    duck.close()
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Aggregation MV phase.
# ──────────────────────────────────────────────────────────────────────────────


def _apply_aggregation_mvs(
    cfg: dict, *, dry_run: bool, skip_wait: bool = False
) -> list[tuple[str, str, int]]:
    results: list[tuple[str, str, int]] = []

    for spec in cfg["aggregation_mvs"]:
        mv_name = spec["mv_name"]
        select_tmpl = spec["select_template"].rstrip()

        logger.info("[agg-mv] %s", mv_name)

        if _rw_object_exists(mv_name):
            logger.info("[agg-mv] %s already in pg_class — skip DDL, just count", mv_name)
            try:
                n = int(_rw_psql(
                    f"SELECT count(*) FROM public.{mv_name};", fetch=True
                ).strip())
            except SystemExit:
                n = 0
            results.append((mv_name, "exists", n))
            continue

        ddl = (
            "SET BACKGROUND_DDL = TRUE;\n"
            f"DROP MATERIALIZED VIEW IF EXISTS public.{mv_name};\n"
            f"CREATE MATERIALIZED VIEW public.{mv_name} AS\n"
            f"{select_tmpl};\n"
        )

        if dry_run:
            print(f"-- AGG MV: {mv_name}")
            print(ddl + "\n")
            results.append((mv_name, "dry_run", 0))
            continue

        try:
            _rw_psql_script(ddl, timeout_s=180)
        except SystemExit as exc:
            logger.error("[agg-mv] %s DDL FAILED: %s", mv_name, str(exc)[:500])
            results.append((mv_name, "ddl_failed", 0))
            continue

        if skip_wait:
            logger.info("[agg-mv] %s — skip-wait, marking pending", mv_name)
            results.append((mv_name, "pending_hydrate", 0))
            continue

        wait_status = _wait_for_hydration(mv_name, timeout_s=AGG_MV_WAIT_TIMEOUT_S)
        if wait_status == "timeout":
            logger.warning("[agg-mv] %s hydration TIMEOUT", mv_name)
            results.append((mv_name, "pending_hydrate", 0))
            continue

        try:
            n = int(_rw_psql(
                f"SELECT count(*) FROM public.{mv_name};", fetch=True
            ).strip())
        except SystemExit:
            n = 0

        logger.info("[agg-mv] %s hydrated — %s rows", mv_name, f"{n:,}")
        results.append((mv_name, "hydrated", n))

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Audience MV phase.
# ──────────────────────────────────────────────────────────────────────────────


def _seed_audience_spec_row(
    spec: dict,
    *,
    smoke_status: str,
    smoke_row_count: int,
    notes: str | None,
) -> None:
    def q(s: str | None) -> str:
        if s is None:
            return "NULL"
        s2 = s.replace("'", "''")
        return f"'{s2}'"

    notes_value = q(notes) if notes else "NULL"

    sql = f"""
    INSERT INTO ops.audience_mv_specs (
        mv_name,
        filter_description,
        expected_row_count_min,
        expected_row_count_max,
        named_smoke_query,
        expected_smoke_min,
        business_use_case,
        owner_team,
        last_applied_at,
        last_smoke_status,
        last_smoke_row_count,
        notes,
        updated_at
    ) VALUES (
        {q(spec['mv_name'])},
        {q(spec['filter_description'].strip())},
        {spec['expected_row_count_min']},
        {spec['expected_row_count_max']},
        {q(spec['smoke_query'].strip())},
        {spec['smoke_min']},
        {q(spec['business_use_case'].strip())},
        'data-engine-x',
        now(),
        {q(smoke_status)},
        {smoke_row_count},
        {notes_value},
        now()
    )
    ON CONFLICT (mv_name) DO UPDATE SET
        filter_description     = EXCLUDED.filter_description,
        expected_row_count_min = EXCLUDED.expected_row_count_min,
        expected_row_count_max = EXCLUDED.expected_row_count_max,
        named_smoke_query      = EXCLUDED.named_smoke_query,
        expected_smoke_min     = EXCLUDED.expected_smoke_min,
        business_use_case      = EXCLUDED.business_use_case,
        owner_team             = EXCLUDED.owner_team,
        last_applied_at        = EXCLUDED.last_applied_at,
        last_smoke_status      = EXCLUDED.last_smoke_status,
        last_smoke_row_count   = EXCLUDED.last_smoke_row_count,
        notes                  = EXCLUDED.notes,
        updated_at             = EXCLUDED.updated_at;
    """
    _pg_psql(sql)


def _smoke_gate_audience(spec: dict) -> tuple[str, int, int]:
    """Run row-count + named smoke. Return (status, total, smoke_count)."""
    mv_name = spec["mv_name"]
    try:
        total = int(_rw_psql(
            f"SELECT count(*) FROM public.{mv_name};", fetch=True
        ).strip())
    except SystemExit as exc:
        logger.warning("count(*) failed for %s: %s", mv_name, str(exc)[:200])
        return ("pending_hydrate", 0, 0)

    if total < spec["expected_row_count_min"]:
        if total == 0:
            return ("pending_hydrate", 0, 0)
        return ("fail_count_low", total, 0)
    if total > spec["expected_row_count_max"] * 2:
        return ("fail_count_high", total, 0)

    smoke_q = spec["smoke_query"].strip()
    smoke_out = _rw_psql(smoke_q, fetch=True).strip()
    smoke_count = int(smoke_out.splitlines()[0])
    if smoke_count < spec["smoke_min"]:
        return ("fail_smoke", total, smoke_count)
    return ("pass", total, smoke_count)


def _apply_audience_mvs(
    cfg: dict, *, dry_run: bool, skip_wait: bool = False
) -> tuple[list[tuple[str, str, int, int]], list[str]]:
    results: list[tuple[str, str, int, int]] = []
    failures: list[str] = []

    for spec in cfg["audience_mvs"]:
        mv_name = spec["mv_name"]
        select_tmpl = spec["select_template"].rstrip()

        logger.info("[audience] %s", mv_name)

        if _rw_object_exists(mv_name):
            logger.info("[audience] %s already in pg_class — smoke-gate", mv_name)
            status, total, smoke = _smoke_gate_audience(spec)
            mapped = (
                "pass" if status == "pass"
                else "pending_hydrate" if status == "pending_hydrate"
                else "fail"
            )
            _seed_audience_spec_row(
                spec,
                smoke_status=mapped,
                smoke_row_count=smoke,
                notes=f"already_existed; status={status} total={total} smoke={smoke}",
            )
            results.append((mv_name, status, total, smoke))
            if mapped == "fail":
                failures.append(f"{mv_name}({status})")
            continue

        ddl = (
            "SET BACKGROUND_DDL = TRUE;\n"
            f"DROP MATERIALIZED VIEW IF EXISTS public.{mv_name};\n"
            f"CREATE MATERIALIZED VIEW public.{mv_name} AS\n"
            f"{select_tmpl};\n"
        )

        if dry_run:
            print(f"-- AUDIENCE MV: {mv_name}")
            print(ddl + "\n")
            results.append((mv_name, "dry_run", 0, 0))
            continue

        try:
            _rw_psql_script(ddl, timeout_s=180)
        except SystemExit as exc:
            logger.error("[audience] %s DDL FAILED: %s", mv_name, str(exc)[:500])
            _seed_audience_spec_row(
                spec,
                smoke_status="fail",
                smoke_row_count=0,
                notes=f"DDL apply failed: {str(exc)[:500]}",
            )
            failures.append(f"{mv_name}(ddl_failed)")
            results.append((mv_name, "ddl_failed", 0, 0))
            continue

        if skip_wait:
            _seed_audience_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes="DDL applied; --skip-wait set",
            )
            results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        wait_status = _wait_for_hydration(
            mv_name, timeout_s=AUDIENCE_MV_WAIT_TIMEOUT_S
        )
        if wait_status == "timeout":
            logger.warning("[audience] %s hydration TIMEOUT — pending", mv_name)
            _seed_audience_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes=f"hydration timeout after {AUDIENCE_MV_WAIT_TIMEOUT_S}s",
            )
            results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        status, total, smoke = _smoke_gate_audience(spec)
        logger.info(
            "[audience] %s — status=%s total=%s smoke=%s",
            mv_name, status, f"{total:,}", smoke,
        )

        if status == "pass":
            _seed_audience_spec_row(
                spec,
                smoke_status="pass",
                smoke_row_count=smoke,
                notes=f"total_rows={total}; smoke_query_returned={smoke}",
            )
        elif status == "pending_hydrate":
            _seed_audience_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes="hydration incomplete at smoke time",
            )
        else:
            _seed_audience_spec_row(
                spec,
                smoke_status="fail",
                smoke_row_count=smoke,
                notes=(
                    f"status={status} total_rows={total} smoke_returned={smoke} "
                    f"expected_min={spec['expected_row_count_min']} "
                    f"expected_max={spec['expected_row_count_max']} "
                    f"smoke_min={spec['smoke_min']}"
                ),
            )
            failures.append(f"{mv_name}({status})")

        results.append((mv_name, status, total, smoke))

    return results, failures


# ──────────────────────────────────────────────────────────────────────────────
# Verify-only path: re-smoke audience MVs and re-seed spec table.
# ──────────────────────────────────────────────────────────────────────────────


def _verify_audiences(cfg: dict) -> tuple[list[tuple[str, str, int, int]], list[str]]:
    results: list[tuple[str, str, int, int]] = []
    failures: list[str] = []

    for spec in cfg["audience_mvs"]:
        mv_name = spec["mv_name"]
        if not _rw_object_exists(mv_name):
            logger.warning("[verify] %s not in pg_class — skip", mv_name)
            results.append((mv_name, "missing", 0, 0))
            continue

        # If still in rw_ddl_progress, mark pending.
        if _rw_ddl_in_progress(mv_name):
            logger.info("[verify] %s still in rw_ddl_progress — pending", mv_name)
            _seed_audience_spec_row(
                spec,
                smoke_status="pending_hydrate",
                smoke_row_count=0,
                notes="verify-only; still in rw_ddl_progress",
            )
            results.append((mv_name, "pending_hydrate", 0, 0))
            continue

        status, total, smoke = _smoke_gate_audience(spec)
        logger.info(
            "[verify] %s — status=%s total=%s smoke=%s",
            mv_name, status, f"{total:,}", smoke,
        )
        mapped = (
            "pass" if status == "pass"
            else "pending_hydrate" if status == "pending_hydrate"
            else "fail"
        )
        _seed_audience_spec_row(
            spec,
            smoke_status=mapped,
            smoke_row_count=smoke,
            notes=(
                f"verify-only; status={status} total={total} smoke={smoke}"
            ),
        )
        results.append((mv_name, status, total, smoke))
        if mapped == "fail":
            failures.append(f"{mv_name}({status})")

    return results, failures


# ──────────────────────────────────────────────────────────────────────────────
# Main entrypoint.
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser()
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    grp.add_argument("--verify-audiences", action="store_true")
    p.add_argument("--sources-only", action="store_true")
    p.add_argument("--aggregation-mvs-only", action="store_true")
    p.add_argument("--audiences-only", action="store_true")
    p.add_argument(
        "--skip-wait", action="store_true",
        help="Apply DDL but don't wait for hydration",
    )
    args = p.parse_args()

    cfg = _load_config()
    logger.info("loaded config: %d sources, %d agg MVs, %d audience MVs",
                len(cfg["sources"]), len(cfg["aggregation_mvs"]),
                len(cfg["audience_mvs"]))

    if args.verify_audiences:
        results, failures = _verify_audiences(cfg)
        print("\n=== VERIFY AUDIENCES SUMMARY ===")
        for n, st, tot, sm in results:
            print(f"  {n:<46} {st:<18} total={tot:>14,} smoke={sm:>10,}")
        print()
        if failures:
            logger.error("HARD-FAIL audiences: %s", failures)
            return 2
        return 0

    do_sources = not (args.aggregation_mvs_only or args.audiences_only)
    do_agg = not (args.sources_only or args.audiences_only)
    do_aud = not (args.sources_only or args.aggregation_mvs_only)

    source_results: list = []
    agg_results: list = []
    aud_results: list = []
    aud_failures: list[str] = []

    if do_sources:
        source_results = _apply_sources(cfg, dry_run=args.dry_run)

    if do_agg:
        agg_results = _apply_aggregation_mvs(
            cfg, dry_run=args.dry_run, skip_wait=args.skip_wait
        )

    if do_aud:
        aud_results, aud_failures = _apply_audience_mvs(
            cfg, dry_run=args.dry_run, skip_wait=args.skip_wait
        )

    # Summary.
    print("\n=== APPLY SUMMARY ===")
    if source_results:
        print("Sources:")
        for n, st, r2c, cc in source_results:
            print(f"  {n:<40} {st:<12} r2_objs={r2c:>3} cols={cc:>3}")
    if agg_results:
        print("Aggregation MVs:")
        for n, st, total in agg_results:
            print(f"  {n:<40} {st:<18} rows={total:>14,}")
    if aud_results:
        print("Audience MVs:")
        for n, st, total, sm in aud_results:
            print(f"  {n:<46} {st:<18} total={total:>14,} smoke={sm:>10,}")
    print()

    source_failures = [n for n, st, *_ in source_results if st == "failed"]
    agg_failures = [n for n, st, *_ in agg_results if st in ("ddl_failed",)]
    if source_failures:
        logger.error("FAILED sources: %s", source_failures)
    if agg_failures:
        logger.error("FAILED agg MVs: %s", agg_failures)
    if aud_failures:
        logger.error("FAILED audiences: %s", aud_failures)

    if source_failures or agg_failures or aud_failures:
        return 2

    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
