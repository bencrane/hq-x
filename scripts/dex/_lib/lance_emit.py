"""Reusable Lance source emitter.

Extracted from ``scripts/run_fmcsa_carrier_essentials_lance_emit.py`` (the
Wave 3 Lance canary). Wave 1 of the Lance sweep parameterized this so each
new source is a config dict + ``LanceSourceEmitter(config).run()``.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

LOG = logging.getLogger(__name__)

TMP_DIR = "/tmp/lance"


@dataclass
class LanceEmitConfig:
    dataset_slug: str
    r2_bucket: str
    parquet_input_prefix: str
    parquet_file_pattern: str
    partition_mode: Literal[
        "latest_snapshot", "multi_year", "multi_year_feed", "multi_release",
        "multi_snapshot",
    ]
    lance_uri: str
    btree_column: str
    btree_optional: bool = False
    multi_year_filter: list[int] | None = None
    # multi_year_feed only: which feed= partition under year=YYYY/ to read
    # (e.g. "general" for cms-open-payments/year=YYYY/feed=general/).
    feed: str | None = None
    # Option A (reviewer 2026-05-18): configurable partition key so NY's
    # fiscal_year=YYYY layout can be globbed without renaming.  Default "year"
    # preserves backward-compat for USAspending / CMS / SBA / SAM / FMCSA
    # wrappers that don't set this field.
    partition_key: str = "year"
    # --- incremental dedup (Shovels rail, 2026-05-29) ------------------- #
    # When set, the Lance read SELECT is wrapped in a window dedup keeping the
    # latest row per `dedup_key`, ordered by `dedup_order_col` DESC. This makes
    # "append a dated snapshot partition, rebuild from the full glob, keep
    # latest-per-PK" a first-class reusable feature on top of the overwrite-only
    # writer (there is no merge_insert in the repo). Backward-compatible:
    # configs that leave dedup_key=None get the unchanged `SELECT *` behavior.
    #
    # `partition_mode="multi_snapshot"` globs every `snapshot=YYYY-MM-DD/`
    # partition under the prefix (the dated-snapshot incrementality model) and
    # is the natural companion to dedup_key, but dedup_key works with any mode.
    dedup_key: str | None = None
    dedup_order_col: str = "ingested_at"


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _ensure_tmpdir() -> None:
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR


def _connect_duckdb_to_r2():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _detect_latest_snapshot(con, bucket: str, prefix: str) -> str:
    glob_pat = f"r2://{bucket}/{prefix}/snapshot=*/*"
    rows = con.execute(f"SELECT file FROM glob('{glob_pat}')").fetchall()
    if not rows:
        raise SystemExit(
            f"FAIL: no files found under r2://{bucket}/{prefix}/snapshot=*/"
        )
    snapshots: set[str] = set()
    for (path,) in rows:
        for p in path.split("/"):
            if p.startswith("snapshot=") and len(p) == len("snapshot=YYYY-MM-DD"):
                snapshots.add(p[len("snapshot="):])
    if not snapshots:
        raise SystemExit("FAIL: no snapshot=YYYY-MM-DD dirs detected")
    return max(snapshots)


def _detect_snapshots(con, bucket: str, prefix: str, file_pattern: str) -> list[str]:
    """Detect every ``snapshot=YYYY-MM-DD/`` partition under the prefix.

    Returns sorted ISO date strings. Used by ``partition_mode='multi_snapshot'``
    — the Shovels incrementality model rebuilds the Lance dataset from the FULL
    snapshot glob (deduped to latest-per-PK), so adding a new dated snapshot
    partition and re-emitting picks up all history.
    """
    glob_pat = f"r2://{bucket}/{prefix}/snapshot=*/{file_pattern}"
    rows = con.execute(f"SELECT file FROM glob('{glob_pat}')").fetchall()
    if not rows:
        raise SystemExit(
            f"FAIL: no files found at r2://{bucket}/{prefix}/snapshot=*/{file_pattern}"
        )
    snapshots: set[str] = set()
    for (path,) in rows:
        for p in path.split("/"):
            if p.startswith("snapshot=") and len(p) == len("snapshot=YYYY-MM-DD"):
                snapshots.add(p[len("snapshot="):])
    if not snapshots:
        raise SystemExit("FAIL: no snapshot=YYYY-MM-DD dirs detected")
    return sorted(snapshots)


def _detect_years(
    con,
    bucket: str,
    prefix: str,
    file_pattern: str,
    filter_years: list[int] | None,
    feed: str | None = None,
    partition_key: str = "year",
) -> list[int]:
    sub = f"feed={feed}/" if feed else ""
    glob_pat = f"r2://{bucket}/{prefix}/{partition_key}=*/{sub}{file_pattern}"
    rows = con.execute(f"SELECT file FROM glob('{glob_pat}')").fetchall()
    if not rows:
        raise SystemExit(
            f"FAIL: no files found at r2://{bucket}/{prefix}/{partition_key}=*/{sub}{file_pattern}"
        )
    years: set[int] = set()
    pkey_eq = f"{partition_key}="
    for (path,) in rows:
        for p in path.split("/"):
            if p.startswith(pkey_eq) and p[len(pkey_eq):].isdigit():
                years.add(int(p[len(pkey_eq):]))
    if not years:
        raise SystemExit(f"FAIL: no {partition_key}=YYYY dirs detected")
    if filter_years is not None:
        years &= set(filter_years)
        if not years:
            raise SystemExit(
                f"FAIL: filter {filter_years} excluded all discovered {partition_key}= dirs"
            )
    return sorted(years)


def _detect_releases(
    con,
    bucket: str,
    prefix: str,
    file_pattern: str,
) -> list[str]:
    """Detect release=YYYYqQ partition directories under r2://<bucket>/<prefix>/release=*/*.

    Returns sorted list of release strings like ['2008q1', '2008q2', ..., '2026q1'].
    """
    import re
    glob_pat = f"r2://{bucket}/{prefix}/release=*/{file_pattern}"
    rows = con.execute(f"SELECT file FROM glob('{glob_pat}')").fetchall()
    if not rows:
        raise SystemExit(
            f"FAIL: no files found at r2://{bucket}/{prefix}/release=*/{file_pattern}"
        )
    release_re = re.compile(r"^release=(\d{4}q[1-4])$", re.IGNORECASE)
    releases: set[str] = set()
    for (path,) in rows:
        for segment in path.split("/"):
            m = release_re.match(segment)
            if m:
                releases.add(m.group(1).lower())
    if not releases:
        raise SystemExit(
            f"FAIL: no release=YYYYqQ dirs detected under "
            f"r2://{bucket}/{prefix}/release=*/{file_pattern}"
        )
    return sorted(releases)


def _build_input_uri(config: LanceEmitConfig, con) -> tuple[str, dict]:
    if config.partition_mode == "latest_snapshot":
        snapshot = _detect_latest_snapshot(
            con, config.r2_bucket, config.parquet_input_prefix
        )
        uri = (
            f"'r2://{config.r2_bucket}/{config.parquet_input_prefix}/"
            f"snapshot={snapshot}/{config.parquet_file_pattern}'"
        )
        return uri, {"snapshot": snapshot}

    if config.partition_mode == "multi_snapshot":
        snapshots = _detect_snapshots(
            con, config.r2_bucket, config.parquet_input_prefix,
            config.parquet_file_pattern,
        )
        uri_list = ", ".join(
            f"'r2://{config.r2_bucket}/{config.parquet_input_prefix}/"
            f"snapshot={s}/{config.parquet_file_pattern}'"
            for s in snapshots
        )
        uri = f"[{uri_list}]"
        return uri, {"snapshots": snapshots}

    if config.partition_mode == "multi_year":
        pkey = config.partition_key
        years = _detect_years(
            con,
            config.r2_bucket,
            config.parquet_input_prefix,
            config.parquet_file_pattern,
            config.multi_year_filter,
            partition_key=pkey,
        )
        uri_list = ", ".join(
            f"'r2://{config.r2_bucket}/{config.parquet_input_prefix}/"
            f"{pkey}={y}/{config.parquet_file_pattern}'"
            for y in years
        )
        uri = f"[{uri_list}]"
        return uri, {"years": years}

    if config.partition_mode == "multi_year_feed":
        if not config.feed:
            raise ValueError("multi_year_feed requires config.feed (e.g. 'general')")
        pkey = config.partition_key
        years = _detect_years(
            con,
            config.r2_bucket,
            config.parquet_input_prefix,
            config.parquet_file_pattern,
            config.multi_year_filter,
            feed=config.feed,
            partition_key=pkey,
        )
        uri_list = ", ".join(
            f"'r2://{config.r2_bucket}/{config.parquet_input_prefix}/"
            f"{pkey}={y}/feed={config.feed}/{config.parquet_file_pattern}'"
            for y in years
        )
        uri = f"[{uri_list}]"
        return uri, {"years": years, "feed": config.feed}

    if config.partition_mode == "multi_release":
        releases = _detect_releases(
            con,
            config.r2_bucket,
            config.parquet_input_prefix,
            config.parquet_file_pattern,
        )
        uri_list = ", ".join(
            f"'r2://{config.r2_bucket}/{config.parquet_input_prefix}/"
            f"release={r}/{config.parquet_file_pattern}'"
            for r in releases
        )
        uri = f"[{uri_list}]"
        return uri, {"releases": releases}

    raise ValueError(f"unknown partition_mode: {config.partition_mode!r}")


def _select_sql(config: LanceEmitConfig, input_uri: str) -> str:
    """Build the DuckDB SELECT feeding the Lance write / row-count.

    Without ``dedup_key`` this is the historical ``SELECT * FROM
    read_parquet(<uri>)``. With ``dedup_key`` set, it keeps the latest row per
    key via ``QUALIFY ROW_NUMBER() OVER (PARTITION BY <key> ORDER BY
    <order_col> DESC) = 1`` — the dedup-latest-per-PK rebuild over a
    ``snapshot=*`` glob. A ``WHERE <key> IS NOT NULL`` guard drops the rare
    upstream null-PK row (otherwise it would collapse to a single arbitrary
    row and pollute the BTREE).
    """
    base = f"read_parquet({input_uri})"
    if not config.dedup_key:
        return f"SELECT * FROM {base}"
    key = config.dedup_key
    order_col = config.dedup_order_col
    return (
        f"SELECT * FROM {base} "
        f"WHERE {key} IS NOT NULL "
        f"QUALIFY ROW_NUMBER() OVER ("
        f"PARTITION BY {key} ORDER BY {order_col} DESC) = 1"
    )


def emit_lance(config: LanceEmitConfig) -> dict:
    import lance

    LOG.info("=" * 60)
    LOG.info("lance emit: dataset_slug=%s", config.dataset_slug)
    LOG.info("output: %s", config.lance_uri)

    con = _connect_duckdb_to_r2()
    input_uri, input_meta = _build_input_uri(config, con)
    select_sql = _select_sql(config, input_uri)
    LOG.info("input:  %s", input_uri)
    LOG.info("input_meta: %s", input_meta)
    if config.dedup_key:
        LOG.info("dedup:  latest-per-%s ORDER BY %s DESC", config.dedup_key, config.dedup_order_col)

    parquet_count = con.execute(
        f"SELECT COUNT(*) FROM ({select_sql})"
    ).fetchone()[0]
    LOG.info("rows after dedup/select: %d", parquet_count)

    storage_options = _lance_storage_options()
    metrics: dict = {
        "dataset_slug": config.dataset_slug,
        "parquet_rows": parquet_count,
        **input_meta,
    }

    t0 = time.time()
    with lance_commit_lock(config.dataset_slug):
        reader = con.from_query(select_sql).to_arrow_reader(batch_size=100_000)
        LOG.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            config.lance_uri,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        LOG.info(
            "wrote %d rows to Lance in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )
        metrics.update(
            lance_rows=lance_count,
            lance_version=ds.version,
            write_seconds=round(write_dur, 1),
        )

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        t_idx = time.time()
        LOG.info(
            "creating BTREE scalar index on %s ...",
            config.btree_column,
        )
        try:
            ds.create_scalar_index(
                config.btree_column, index_type="BTREE", replace=True,
            )
            idx_dur = time.time() - t_idx
            LOG.info("  index built in %.1fs", idx_dur)
            metrics["index_seconds"] = round(idx_dur, 1)
        except Exception as e:
            level = "warning" if config.btree_optional else "error"
            getattr(LOG, level)(
                "  BTREE index build %s: %s",
                "failed (non-fatal)" if config.btree_optional else "FAILED",
                e,
            )
            metrics["index_seconds"] = None
            if not config.btree_optional:
                raise

        t1 = time.time()
        LOG.info("optimize: compact + cleanup_older_than=7d ...")
        try:
            stats = ds.optimize.compact_files()
            LOG.info("  compact_files: %s", stats)
        except Exception as e:
            LOG.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:
            LOG.warning("  cleanup_old_versions failed (non-fatal): %s", e)
        opt_dur = time.time() - t1
        LOG.info("optimize done in %.1fs", opt_dur)
        metrics["optimize_seconds"] = round(opt_dur, 1)

    LOG.info("=" * 60)
    return metrics


def run_cli(config: LanceEmitConfig, argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description=f"Lance emit for {config.dataset_slug}",
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="counts only")
    args = ap.parse_args(argv)

    for var in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    ):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    _ensure_tmpdir()

    con = _connect_duckdb_to_r2()
    input_uri, input_meta = _build_input_uri(config, con)
    select_sql = _select_sql(config, input_uri)
    total = con.execute(
        f"SELECT COUNT(*) FROM ({select_sql})"
    ).fetchone()[0]
    LOG.info(
        "INPUT: uri=%s meta=%s rows_after_select=%d dedup_key=%s",
        input_uri, input_meta, total, config.dedup_key,
    )
    con.close()

    if args.dry_run:
        LOG.info("DRY RUN — exiting without writing Lance dataset")
        return 0

    metrics = emit_lance(config)
    LOG.info("OK — metrics: %s", metrics)
    if metrics["parquet_rows"] != metrics["lance_rows"]:
        LOG.error(
            "FAIL: row count mismatch parquet=%d lance=%d",
            metrics["parquet_rows"], metrics["lance_rows"],
        )
        return 1
    return 0
