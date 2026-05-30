#!/usr/bin/env python3
"""Build spines/fec_individual_contributions_lance — canonical FEC contribution spine.

Pattern A, TRANSACTION grain: one row per itemized contribution (PK ``sub_id``),
the canonical JOIN AXIS for every downstream FEC person/employer bridge. No
rollups (those live in the derived spines/fec_donors_lance rolodex).

Source columns are preserved VERBATIM. Added on top:
  - Structured person-name components parsed from the single ``name`` field via
    nameparser (MIT, deterministic): name_first / _middle / _last / _suffix /
    _title / _nickname. NONE dropped, NO nickname substitution. Plus folded
    join keys name_last_key / name_first_key (lower+strip_accents only).
  - person_key = md5(last_key | first_key | state) — the rolodex grouping handle.

Parse is run over DISTINCT names only (≈12.5M distinct vs 281M rows = ~22x
fewer calls) then hash-joined back — keeps the pure-Python parser cheap at scale.

Source : s3://dex-raw-landing-zone/fec/cycle=YYYY/indiv.parquet (24 cycles 1980-2026)
Output : s3://dex-raw-landing-zone/polaris-warehouse/spines/fec_individual_contributions_lance/

BTREE (raise-on-fail): sub_id, cmte_id, name_last_key, name_first_key,
name_normalized, employer_normalized, occupation_normalized, zip5, state,
person_key, transaction_dt, cycle_year.

Usage:
  # full build (Modal):
  doppler run --project hq-all --config prd -- python3 \\
    scripts/build_fec_individual_contributions_spine_lance.py --apply
  # plan only:
  ... --dry-run
  # local one-cycle smoke (writes a local Lance dataset, no Polaris):
  ... --apply --cycles 1980 --output-uri /tmp/lance/fec_smoke_individual_contributions_lance \\
      --skip-polaris --workers 4
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
os.environ.setdefault("TMPDIR", "/tmp/lance")
Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

from scripts._lib.person_name_parse import (  # noqa: E402
    FIELDS,
    __version__ as PARSER_VERSION,
    parse_to_tuple,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout
)
log = logging.getLogger("build_fec_individual_contributions_spine_lance")

R2_BUCKET = "dex-raw-landing-zone"
SOURCE_GLOB_TMPL = f"s3://{R2_BUCKET}/fec/cycle={{cycle}}/indiv.parquet"
DEFAULT_OUTPUT_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/spines/fec_individual_contributions_lance"
)
ALL_CYCLES = tuple(range(1980, 2027, 2))  # 24 even election cycles

POLARIS_NAMESPACE = "spines"
POLARIS_TABLE = "fec_individual_contributions_lance"

BTREE_COLS = (
    "sub_id",
    "cmte_id",
    "name_last_key",
    "name_first_key",
    "name_normalized",
    "employer_normalized",
    "occupation_normalized",
    "zip5",
    "state",
    "person_key",
    "transaction_dt",
    "cycle_year",
)


def _r2_endpoint_host() -> str:
    return re.sub(r"^https?://", "", os.environ["R2_ENDPOINT"]).rstrip("/")


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _connect_duckdb():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"CREATE SECRET r2 (TYPE S3, KEY_ID '{os.environ['R2_ACCESS_KEY_ID']}', "
        f"SECRET '{os.environ['R2_SECRET_ACCESS_KEY']}', ENDPOINT '{_r2_endpoint_host()}', "
        f"REGION 'auto', URL_STYLE 'path', USE_SSL true);"
    )
    con.execute("SET threads=8; SET memory_limit='48GB';")
    con.execute("SET temp_directory='/tmp/lance'; SET max_temp_directory_size='120GB';")
    con.execute("SET preserve_insertion_order=false;")
    return con


def _input_list(cycles: tuple[int, ...]) -> str:
    return "[" + ", ".join(f"'{SOURCE_GLOB_TMPL.format(cycle=c)}'" for c in cycles) + "]"


def _parse_distinct_names(names: list[str], workers: int):
    """Parse distinct names → Arrow map table. Streams results into
    preallocated columns (no 12.5M-tuple zip materialization)."""
    import pyarrow as pa

    t = time.time()
    n = len(names)
    cols: dict[str, list] = {f: [None] * n for f in FIELDS}
    if workers <= 1:
        results = (parse_to_tuple(nm) for nm in names)
    else:
        from multiprocessing import Pool

        pool = Pool(workers)
        results = pool.imap(parse_to_tuple, names, chunksize=10_000)
    for i, tup in enumerate(results):
        for j, f in enumerate(FIELDS):
            cols[f][i] = tup[j]
    if workers > 1:
        pool.close()
        pool.join()
    log.info("  parsed %d distinct names in %.1fs (%d workers)", n, time.time() - t, workers)
    return pa.table({"name": names, **cols})


def _join_sql(input_list: str) -> str:
    return f"""
        SELECT
            r.*,
            m.name_first, m.name_middle, m.name_last, m.name_suffix,
            m.name_title, m.name_nickname, m.name_last_key, m.name_first_key,
            CASE WHEN m.name_last_key IS NOT NULL THEN
                md5(coalesce(m.name_last_key, '') || '|'
                    || coalesce(m.name_first_key, '') || '|'
                    || coalesce(upper(trim(r.state)), ''))
            END AS person_key
        FROM read_parquet({input_list}) r
        LEFT JOIN name_map m ON r.name = m.name
    """


def build(*, apply: bool, cycles: tuple[int, ...], output_uri: str,
          workers: int, max_rows_per_file: int, skip_polaris: bool,
          row_floor: int) -> int:
    import lance

    is_s3 = output_uri.startswith("s3://")
    storage_options = _lance_storage_options() if is_s3 else None
    if not is_s3:
        Path(output_uri).parent.mkdir(parents=True, exist_ok=True)

    con = _connect_duckdb()
    input_list = _input_list(cycles)
    log.info("source cycles: %s", ",".join(map(str, cycles)))
    log.info("output: %s  (max_rows_per_file=%d)", output_uri, max_rows_per_file)

    parquet_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet({input_list})").fetchone()[0]
    log.info("parquet rows: %d", parquet_rows)

    if apply:
        log.info("collecting DISTINCT names ...")
        rows = con.execute(
            f"SELECT DISTINCT name FROM read_parquet({input_list}) WHERE name IS NOT NULL"
        ).fetchall()
        names = [r[0] for r in rows]
        del rows
        log.info("  distinct names: %d (%.1f%% of rows)", len(names), 100.0 * len(names) / max(parquet_rows, 1))

    if not apply:
        log.info("DRY RUN — no Lance write. parser=person_name_parse v%s", PARSER_VERSION)
        return 0

    name_map = _parse_distinct_names(names, workers)
    # coverage at distinct-name grain (cheap, no 281M re-aggregate)
    import pyarrow.compute as pc

    n = name_map.num_rows
    for f in ("name_first", "name_last", "name_middle", "name_suffix"):
        present = pc.sum(pc.is_valid(name_map.column(f))).as_py() or 0
        log.info("  parse coverage %-12s %5.1f%% of distinct names", f, 100.0 * present / max(n, 1))

    con.register("name_map", name_map)

    log.info("streaming join + Lance write (mode=overwrite) ...")
    t_w = time.time()
    reader = con.from_query(_join_sql(input_list)).to_arrow_reader(batch_size=100_000)
    ds = lance.write_dataset(
        reader, output_uri, mode="overwrite",
        max_rows_per_file=max_rows_per_file, storage_options=storage_options,
    )
    lance_rows = ds.count_rows()
    log.info("  wrote %d rows in %.1fs (version=%s)", lance_rows, time.time() - t_w, ds.version)

    if lance_rows != parquet_rows:
        log.error("FAIL: row parity mismatch parquet=%d lance=%d", parquet_rows, lance_rows)
        return 1
    if lance_rows < row_floor:
        log.error("FAIL: lance rows %d < floor %d", lance_rows, row_floor)
        return 1

    for col in BTREE_COLS:
        t_i = time.time()
        log.info("building BTREE index on %s ...", col)
        ds.create_scalar_index(col, index_type="BTREE", replace=True)
        log.info("  BTREE(%s): OK in %.1fs", col, time.time() - t_i)

    try:
        log.info("optimize: compact_files + cleanup_old_versions(7d) ...")
        ds.optimize.compact_files()
    except Exception as e:  # noqa: BLE001
        log.warning("  compact_files failed (non-fatal): %s", e)
    try:
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:  # noqa: BLE001
        log.warning("  cleanup_old_versions failed (non-fatal): %s", e)

    if is_s3 and not skip_polaris:
        from scripts._lib.catalog_hooks import register_or_update_polaris

        log.info("registering Polaris generic-table ...")
        register_or_update_polaris(
            namespace=POLARIS_NAMESPACE,
            table_name=POLARIS_TABLE,
            s3_uri=output_uri,
            docstring=(
                "Canonical FEC individual-contributions spine (Pattern A, transaction grain, "
                "PK sub_id) — every itemized contribution 1980-2026. Source columns verbatim + "
                "structured person-name components (first/middle/last/suffix/title/nickname, "
                f"none dropped, no nickname substitution) parsed via nameparser; person_name_parse "
                f"v{PARSER_VERSION}. person_key = md5(last_key|first_key|state). The canonical join "
                "axis for FEC person/employer bridges; rollups live in spines/fec_donors_lance."
            ),
        )
    else:
        log.info("skipping Polaris registration (skip_polaris=%s, is_s3=%s)", skip_polaris, is_s3)

    log.info("OK — %s: %d rows, %d BTREE indices", POLARIS_TABLE, lance_rows, len(BTREE_COLS))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build canonical FEC individual-contributions spine (Lance)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cycles", default="", help="comma list e.g. '1980,1982'; default all 24")
    ap.add_argument("--output-uri", default=DEFAULT_OUTPUT_URI)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--max-rows-per-file", type=int, default=1_000_000)
    ap.add_argument("--skip-polaris", action="store_true")
    ap.add_argument("--row-floor", type=int, default=0)
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            log.error("FAIL: %s not set", var)
            return 64

    cycles = (
        tuple(int(c) for c in args.cycles.split(",") if c.strip()) if args.cycles else ALL_CYCLES
    )
    return build(
        apply=args.apply, cycles=cycles, output_uri=args.output_uri, workers=args.workers,
        max_rows_per_file=args.max_rows_per_file, skip_polaris=args.skip_polaris,
        row_floor=args.row_floor,
    )


if __name__ == "__main__":
    raise SystemExit(main())
