#!/usr/bin/env python3
"""Apply 8 source-only domains as RW sources + thin essentials MVs.

Operator-overridden 2026-05-09: hydrate the screenshot-listed source-only
prefixes (HUD Multifamily, IRS BMF, NPPES, CMS PECOS, GLEIF rels, DFPI
franchise, NYC Property, USDA RD) regardless of red/yellow/green
priority. Get them wired while in the flow.

Pattern: typed source DDL (introspect Parquet types via DuckDB → emit
matching RW types per the PR #273 IRS 990 lesson). Each sub-table gets a
source AND a thin `SELECT *`-style essentials MV. The essentials MV is a
typed view downstream MVs / audience MVs can read from without re-doing
the regex CASE WHEN coercion dance.

Column curation (the "real" essentials projection — choosing 5-30 cols
out of 290 for HUD, etc.) is deferred to a follow-up. Today's goal is
queryability + uniform Pattern A shape.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with 'psycopg[binary]' --with duckdb python \\
        apps/data-engine-x/scripts/apply_source_only_essentials_rw.py --dry-run
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with 'psycopg[binary]' --with duckdb python \\
        apps/data-engine-x/scripts/apply_source_only_essentials_rw.py --apply
    # Sources only (cheap; commit before MVs):
    doppler run -p hq-all -c prd -- \\
        uv run --with pyyaml --with 'psycopg[binary]' --with duckdb python \\
        apps/data-engine-x/scripts/apply_source_only_essentials_rw.py --apply --sources-only
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_source_only_essentials_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
YAML_PATH = (
    REPO_ROOT
    / "apps/data-engine-x/scripts/_config/source_only_essentials.yaml"
)


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"FAIL: required env var {name} is not set")
    return v


def _duckdb_type_to_rw(duck_type: str) -> str:
    t = duck_type.upper()
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
        "UUID": "CHARACTER VARYING",
    }
    return mapping.get(base, "CHARACTER VARYING")


def _connect_duckdb_to_r2():
    import duckdb

    endpoint_full = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_region='auto';")
    return con


def _introspect_columns(con, s3_uri: str) -> list[tuple[str, str]]:
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_uri}');"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _build_create_source_ddl(
    *, source_name: str, columns: list[tuple[str, str]],
    bucket: str, match_pattern: str,
) -> str:
    cols_decl = ",\n    ".join(
        f"{_quote_ident(name)} {_duckdb_type_to_rw(t)}"
        for name, t in columns
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
) FORMAT PLAIN ENCODE PARQUET;"""


def _build_essentials_mv_ddl(*, mv_name: str, source_name: str) -> str:
    """Thin `SELECT *` essentials MV. Column curation is a follow-up."""
    return f"""\
SET BACKGROUND_DDL = TRUE;
DROP MATERIALIZED VIEW IF EXISTS public.{mv_name};
CREATE MATERIALIZED VIEW public.{mv_name} AS
SELECT * FROM public.{source_name};"""


def _rw_psql(sql: str, *, fetch: bool = False, timeout_s: int = 300) -> str:
    cmd = [
        "psql",
        "-h", _required_env("RISINGWAVE_HOST"),
        "-p", _required_env("RISINGWAVE_PORT"),
        "-U", _required_env("RISINGWAVE_USER"),
        "-d", _required_env("RISINGWAVE_DATABASE"),
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    cmd += ["-tAc", sql] if fetch else ["-c", sql]
    env = {**os.environ, "PGPASSWORD": _required_env("RISINGWAVE_PASSWORD")}
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          check=False, timeout=timeout_s)
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql failed (exit {proc.returncode}):\n"
            f"  STDERR: {proc.stderr}\n  STDOUT: {proc.stdout}"
        )
    return proc.stdout


def _rw_psql_script(sql: str, *, timeout_s: int = 300) -> None:
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
    proc = subprocess.run(cmd, env=env, input=sql, capture_output=True,
                          text=True, check=False, timeout=timeout_s)
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql script failed (exit {proc.returncode}):\n"
            f"  STDERR: {proc.stderr}\n  STDOUT: {proc.stdout}"
        )


def _rw_object_exists(name: str) -> bool:
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{name}';",
        fetch=True,
    ).strip()
    return name in out


def _load_config() -> dict:
    import yaml
    with YAML_PATH.open() as f:
        cfg = yaml.safe_load(f)
    if "bucket" not in cfg:
        raise SystemExit("YAML missing 'bucket' key")
    if "domains" not in cfg:
        raise SystemExit("YAML missing 'domains' key")
    return cfg


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print DDL but don't apply.")
    p.add_argument("--apply", action="store_true",
                   help="Apply sources + essentials MVs.")
    p.add_argument("--sources-only", action="store_true",
                   help="Skip the essentials MV creation phase.")
    p.add_argument("--mvs-only", action="store_true",
                   help="Only create essentials MVs; assume sources exist.")
    p.add_argument("--rebuild-sources", action="store_true",
                   help="DROP existing sources before re-creating with "
                        "typed DDL (cascades to dependents — verify "
                        "dependencies first via get_downstream_dependents).")
    args = p.parse_args()

    if not (args.dry_run or args.apply):
        p.error("specify --dry-run or --apply")

    cfg = _load_config()
    bucket = cfg["bucket"]
    domains = cfg["domains"]
    total_sub_tables = sum(len(d["sub_tables"]) for d in domains)
    logger.info(f"loaded config: {len(domains)} domains, "
                f"{total_sub_tables} sub-tables total")

    con = _connect_duckdb_to_r2()
    sources_done: list[tuple[str, int, int]] = []
    sources_failed: list[str] = []

    if not args.mvs_only:
        for d in domains:
            domain = d["domain"]
            for sub in d["sub_tables"]:
                source_name = sub["source_name"]
                sample_key = sub["sample_key"]
                match_pattern = sub["match_pattern"]
                logger.info(f"[{domain}] introspecting {source_name} "
                            f"(sample={sample_key})")

                try:
                    cols = _introspect_columns(con, f"s3://{bucket}/{sample_key}")
                except Exception as e:
                    logger.error(f"  introspect failed: {str(e)[:200]}")
                    sources_failed.append(source_name)
                    continue

                typed_cnt = sum(1 for _, t in cols
                                if t.upper() not in ("VARCHAR", "TEXT", "STRING"))
                logger.info(f"  {len(cols)} cols ({typed_cnt} typed)")

                ddl = _build_create_source_ddl(
                    source_name=source_name,
                    columns=cols,
                    bucket=bucket,
                    match_pattern=match_pattern,
                )

                if args.dry_run:
                    print(f"-- {source_name}")
                    print(ddl + "\n")
                    sources_done.append((source_name, len(cols), typed_cnt))
                    continue

                if _rw_object_exists(source_name):
                    if args.rebuild_sources:
                        logger.info(f"  --rebuild-sources: DROP CASCADE existing")
                        try:
                            _rw_psql(
                                f"DROP SOURCE IF EXISTS public.{source_name} CASCADE;"
                            )
                        except SystemExit as e:
                            logger.error(f"  drop FAILED: {str(e)[:300]}")
                            sources_failed.append(source_name)
                            continue
                    else:
                        logger.info(f"  already exists — skip "
                                    "(--rebuild-sources to force)")
                        sources_done.append((source_name, len(cols), typed_cnt))
                        continue

                try:
                    _rw_psql_script(ddl)
                    logger.info(f"  applied: {source_name}")
                    sources_done.append((source_name, len(cols), typed_cnt))
                except SystemExit as e:
                    logger.error(f"  apply FAILED: {str(e)[:300]}")
                    sources_failed.append(source_name)

    con.close()

    if args.sources_only:
        logger.info("sources-only mode — skipping MV creation")
        _print_summary(sources_done, sources_failed, [], [])
        return 0 if not sources_failed else 1

    # Essentials MV phase. Each MV is a thin SELECT * over its source.
    mvs_done: list[str] = []
    mvs_failed: list[str] = []

    for d in domains:
        domain = d["domain"]
        for sub in d["sub_tables"]:
            source_name = sub["source_name"]
            mv_name = sub["mv_name"]
            logger.info(f"[{domain}] essentials MV: {mv_name}")

            ddl = _build_essentials_mv_ddl(
                mv_name=mv_name, source_name=source_name,
            )

            if args.dry_run:
                print(f"-- {mv_name}")
                print(ddl + "\n")
                mvs_done.append(mv_name)
                continue

            if _rw_object_exists(mv_name):
                logger.info(f"  already exists — skip")
                mvs_done.append(mv_name)
                continue

            # Don't create the MV if the source doesn't exist (e.g. earlier
            # source apply failed).
            if not _rw_object_exists(source_name):
                logger.warning(f"  upstream source missing — skip")
                mvs_failed.append(mv_name)
                continue

            try:
                _rw_psql_script(ddl, timeout_s=120)
                logger.info(f"  DDL admitted (BACKGROUND build)")
                mvs_done.append(mv_name)
            except SystemExit as e:
                logger.error(f"  apply FAILED: {str(e)[:300]}")
                mvs_failed.append(mv_name)

    _print_summary(sources_done, sources_failed, mvs_done, mvs_failed)
    return 0 if not (sources_failed or mvs_failed) else 1


def _print_summary(
    sources_done, sources_failed, mvs_done, mvs_failed,
) -> None:
    print("\n=== APPLY SUMMARY ===")
    print(f"Sources: {len(sources_done)} done / {len(sources_failed)} failed")
    for name, cols, typed in sources_done:
        print(f"  ✓ {name}: {cols} cols ({typed} typed)")
    for name in sources_failed:
        print(f"  ✗ {name}")
    print(f"Essentials MVs: {len(mvs_done)} done / {len(mvs_failed)} failed")
    for name in mvs_done:
        print(f"  ✓ {name}")
    for name in mvs_failed:
        print(f"  ✗ {name}")


if __name__ == "__main__":
    raise SystemExit(main())
