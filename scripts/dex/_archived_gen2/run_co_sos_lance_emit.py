"""Colorado SoS Business Entities + Trade Names: R2 Parquet -> Lance (step 2).

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Step 2 of the CO SoS ingest cycle: read the step-1 raw Parquet mirrors from R2
and emit two Pattern A Lance datasets, adding the normalized match-key columns
the downstream CO UCC bridges will join on.

Reads (step-1 output -- see run_co_sos_to_r2.py):
  s3://dex-raw-landing-zone/sos-co/release=2026-05-21/entities/data.parquet
  s3://dex-raw-landing-zone/sos-co/release=2026-05-21/trade_names/data.parquet

Writes:
  s3://dex-raw-landing-zone/polaris-warehouse/sos/co_entities_lance
  s3://dex-raw-landing-zone/polaris-warehouse/sos/co_trade_names_lance

Derived columns (the only curation -- the 35 / 25 raw columns pass through 1:1):
  co_entities_lance:
    entity_name_normalized      -- normalize_entity_name() of entityname, AFTER
                                   stripping CO's ", <status> <Month D, YYYY>"
                                   display suffix that CO appends to the names
                                   of non-Good-Standing entities.
  co_trade_names_lance:
    registrant_name_normalized  -- normalize_entity_name() of registrantorganization,
                                   or of the person name when the registrant is
                                   an individual.
    trade_name_normalized       -- normalize_entity_name() of tradenamedescription.

normalize_entity_name (scripts/_lib/entity_name_normalize.py) is the canonical
cross-source normalizer -- the same one every name+state bridge uses -- exposed
to DuckDB as a UDF per L34 so the match key is bit-identical to what the bridges
will compute.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- uv run python \\
        scripts/run_co_sos_lance_emit.py --apply [--table entities|trade_names|both]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

R2_BUCKET = "dex-raw-landing-zone"
RELEASE = "2026-05-21"
TMP_DIR = "/tmp/lance"

# CO appends ", <status-phrase> <Month> <day>, <year>" display suffix(es) to
# the entityname of non-Good-Standing entities (e.g.
# "ACME LLC, Delinquent May 1, 2015"). An entity with a status history carries
# several stacked suffixes, so (...)+$ strips the whole trailing run. The
# phrase varies and is NOT the entitystatus value -- each chunk is anchored on
# its own trailing date; rows without a suffix don't match (no-op).
CO_STATUS_SUFFIX_RE = (
    r"(,\s+[A-Za-z][^,]*\s+"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+[0-9]{1,2},\s+[0-9]{4}\s*)+$"
)

TABLES = {
    "entities": {
        "parquet_key": f"sos-co/release={RELEASE}/entities/data.parquet",
        "lance_uri": f"s3://{R2_BUCKET}/polaris-warehouse/sos/co_entities_lance",
        "slug": "co_sos_entities_lance",
        "derived": (
            "py_normalize_entity("
            f"regexp_replace(entityname, '{CO_STATUS_SUFFIX_RE}', '')"
            ") AS entity_name_normalized"
        ),
        "btree": ["entityid", "entity_name_normalized"],
    },
    "trade_names": {
        "parquet_key": f"sos-co/release={RELEASE}/trade_names/data.parquet",
        "lance_uri": f"s3://{R2_BUCKET}/polaris-warehouse/sos/co_trade_names_lance",
        "slug": "co_sos_trade_names_lance",
        "derived": (
            "py_normalize_entity(coalesce("
            "nullif(trim(registrantorganization), ''), "
            "nullif(trim(concat_ws(' ', firstname, middlename, lastname)), '')"
            ")) AS registrant_name_normalized, "
            "py_normalize_entity(tradenamedescription) AS trade_name_normalized"
        ),
        "btree": ["entityid", "registrant_name_normalized", "trade_name_normalized"],
    },
}


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _storage_options() -> dict:
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
    con.execute("SET memory_limit='6GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
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
    # L34: expose the canonical Python normalizer as a DuckDB UDF so the
    # match key is identical to what downstream bridges compute. null_handling
    # is SPECIAL because normalize_entity_name returns NULL for generic /
    # blacklisted strings (DEFAULT forbids a UDF returning NULL).
    con.create_function(
        "py_normalize_entity", normalize_entity_name, ["VARCHAR"], "VARCHAR",
        null_handling="special",
    )
    return con


def emit_one(table: str, apply: bool) -> dict:
    import lance

    cfg = TABLES[table]
    t0 = time.time()
    logger.info("=" * 64)
    logger.info("[%s] emit -> %s", table, cfg["lance_uri"])

    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _connect_duckdb()
    parquet_uri = f"r2://{R2_BUCKET}/{cfg['parquet_key']}"
    # hive_partitioning=false: the R2 key carries release=YYYY-MM-DD; without
    # this DuckDB injects a synthetic `release` column into SELECT * (L58).
    parquet_read = f"read_parquet('{parquet_uri}', hive_partitioning=false)"
    src_rows = con.execute(f"SELECT count(*) FROM {parquet_read}").fetchone()[0]
    select_sql = f"SELECT *, {cfg['derived']} FROM {parquet_read}"
    logger.info("[%s] source parquet rows: %d", table, src_rows)

    if not apply:
        sample = con.execute(select_sql + " LIMIT 5").fetchall()
        logger.info("[%s] DRY RUN -- derived SELECT compiled, %d sample rows",
                    table, len(sample))
        return {"table": table, "rows": src_rows, "applied": False}

    storage_options = _storage_options()
    with lance_commit_lock(cfg["slug"]):
        reader = con.execute(select_sql).to_arrow_reader(batch_size=100_000)
        logger.info("[%s] writing Lance dataset (mode=overwrite) ...", table)
        ds = lance.write_dataset(
            reader, cfg["lance_uri"], mode="overwrite",
            storage_options=storage_options,
        )
        lance_rows = ds.count_rows()
        logger.info("[%s] wrote %d rows (version=%s)", table, lance_rows, ds.version)
        if lance_rows != src_rows:
            raise SystemExit(
                f"FAIL [{table}]: lance {lance_rows} != parquet {src_rows}"
            )

        for col in cfg["btree"]:
            t_idx = time.time()
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("[%s] BTREE on %s: OK (%.1fs)",
                        table, col, time.time() - t_idx)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("[%s] optimize non-fatal: %s", table, e)

    logger.info("[%s] done -- %d rows, %.1fs", table, lance_rows, time.time() - t0)
    return {
        "table": table, "rows": lance_rows, "applied": True,
        "lance_uri": cfg["lance_uri"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CO SoS R2 Parquet -> Lance emit (step 2)"
    )
    ap.add_argument(
        "--table", choices=["entities", "trade_names", "both"], default="both"
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance datasets")
    grp.add_argument("--dry-run", action="store_true", help="compile + count only")
    args = ap.parse_args()

    for var in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "DEX_DB_URL_DIRECT",
    ):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set in environment", var)
            return 64

    tables = ["entities", "trade_names"] if args.table == "both" else [args.table]
    results = [emit_one(t, args.apply) for t in tables]

    logger.info("=" * 64)
    for r in results:
        logger.info(
            "DONE %s: %d rows%s", r["table"], r["rows"],
            f" -> {r['lance_uri']}" if r["applied"] else " (dry-run)",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
