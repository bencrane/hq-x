"""Pattern A enriched-cohort emit — per-state SoS entity spines.

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. Run this script manually, on demand, after a
fresh upstream state SoS pull. Invoke per-state via ``--state {CA|FL|NY|CO}``
or all four via ``--state all``. See ``apps/data-engine-x/modal/INDEX.md``
§"State SoS pipelines".

Reads each state's already-hydrated SoS entity Lance dataset, applies a
canonical column projection + DISTINCT + typed `incorporation_date`
(via `try_strptime` with a multi-format fallback array, since the source
strings span MM/DD/YYYY, MMDDYYYY, and other variants), and writes a new
Lance spine under `polaris-warehouse/spines/`.

Per Pattern A discipline (`apps/data-engine-x/CLAUDE.md §"Post-2026-05-13
substrate"` + DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A"):

  - lance.write_dataset(mode="overwrite") inside lance_commit_lock(<slug>)
  - BTREE scalar indexes on state_entity_id, physical_state, incorporation_date
  - ds.optimize.compact_files() + ds.cleanup_old_versions(7 days)
  - Polaris registration via init_polaris_lance_generic.py (separate step)

The date parser uses DuckDB `try_strptime(col, [<formats>])::DATE`. The
operator-specified fallback array `['%Y-%m-%d', '%m/%d/%Y', '%Y%m%d']`
extended with `'%m%d%Y'` to cover FL's `MMDDYYYY` source format
(probed at build-time — every FL `file_date` is 8 digits without
separators, e.g. '11301992').

Run via:
  doppler run --project hq-all --config prd -- \
    uv run python apps/data-engine-x/scripts/build_sos_state_entity_spines_lance.py [--state CA|FL|CO|all]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable

import duckdb
import lance
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

LOG = logging.getLogger("sos_spines")
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

R2_BUCKET = "dex-raw-landing-zone"

# Fallback formats tried in order by DuckDB try_strptime.
# Probed source formats:
#   CA initial_filing_date — 'MM/DD/YYYY'
#   FL file_date           — 'MMDDYYYY' (8 digits, no separators)
#   CO entityformdate      — 'MM/DD/YYYY'
DATE_FORMAT_FALLBACKS = ["%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%m%d%Y"]


@dataclass(frozen=True)
class SpineConfig:
    state: str
    source_uri: str
    source_columns: tuple[str, ...]   # projected from source Lance scanner
    select_sql: str                    # full SELECT body (post-projection)
    output_slug: str                   # used by commit_lock + Polaris registration
    output_uri: str
    btree_columns: tuple[str, ...]


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _date_array_sql() -> str:
    """Build the SQL list literal for try_strptime fallback formats."""
    formats = ", ".join(f"'{f}'" for f in DATE_FORMAT_FALLBACKS)
    return f"[{formats}]"


def _ca_config() -> SpineConfig:
    fmts = _date_array_sql()
    return SpineConfig(
        state="CA",
        source_uri=f"s3://{R2_BUCKET}/polaris-warehouse/sos/ca_entities_lance",
        source_columns=(
            "entity_num",
            "entity_name",
            "entity_status",
            "standing_sos",
            "principal_state",
            "principal_state_in_ca",
            "principal_postal_code",
            "principal_postal_code_in_ca",
            "initial_filing_date",
        ),
        select_sql=f"""
            SELECT DISTINCT
                entity_num                                                       AS state_entity_id,
                entity_name                                                      AS legal_name,
                entity_status                                                    AS entity_status,
                standing_sos                                                     AS standing_sos,
                COALESCE(principal_state, principal_state_in_ca)                 AS physical_state,
                COALESCE(principal_postal_code, principal_postal_code_in_ca)     AS physical_zip,
                try_strptime(initial_filing_date, {fmts})::DATE                  AS incorporation_date
            FROM src
        """,
        output_slug="sos_california_entities_lance",
        output_uri=f"s3://{R2_BUCKET}/polaris-warehouse/spines/sos_california_entities_lance",
        btree_columns=("state_entity_id", "physical_state", "incorporation_date"),
    )


def _fl_config() -> SpineConfig:
    fmts = _date_array_sql()
    return SpineConfig(
        state="FL",
        source_uri=f"s3://{R2_BUCKET}/polaris-warehouse/sos/fl_entities_lance",
        source_columns=(
            "entity_num",
            "entity_name",
            "status",
            "state",
            "zip",
            "file_date",
        ),
        select_sql=f"""
            SELECT DISTINCT
                entity_num                                       AS state_entity_id,
                entity_name                                      AS legal_name,
                status                                           AS entity_status,
                state                                            AS physical_state,
                zip                                              AS physical_zip,
                try_strptime(file_date, {fmts})::DATE            AS incorporation_date
            FROM src
        """,
        output_slug="sos_florida_entities_lance",
        output_uri=f"s3://{R2_BUCKET}/polaris-warehouse/spines/sos_florida_entities_lance",
        btree_columns=("state_entity_id", "physical_state", "incorporation_date"),
    )


def _co_config() -> SpineConfig:
    fmts = _date_array_sql()
    return SpineConfig(
        state="CO",
        source_uri=f"s3://{R2_BUCKET}/polaris-warehouse/sos/co_entities_lance",
        source_columns=(
            "entityid",
            "entityname",
            "entitystatus",
            "principalstate",
            "principalzipcode",
            "entityformdate",
        ),
        select_sql=f"""
            SELECT DISTINCT
                entityid                                          AS state_entity_id,
                entityname                                        AS legal_name,
                entitystatus                                      AS entity_status,
                principalstate                                    AS physical_state,
                principalzipcode                                  AS physical_zip,
                try_strptime(entityformdate, {fmts})::DATE        AS incorporation_date
            FROM src
        """,
        output_slug="sos_colorado_entities_lance",
        output_uri=f"s3://{R2_BUCKET}/polaris-warehouse/spines/sos_colorado_entities_lance",
        btree_columns=("state_entity_id", "physical_state", "incorporation_date"),
    )


def _ny_config() -> SpineConfig:
    # Pulls the pre-parsed `initial_dos_filing_date_typed` date32 column from
    # the staging Lance dataset directly, per the per-state spec. The staging
    # emit at apps/data-engine-x/scripts/run_ny_sos_active_corporations_lance_emit.py
    # now populates this column via try_strptime (was previously a TRY_CAST
    # bug that produced 100% NULL across all rows; see fix 2026-05-25 alongside
    # the manual CSV drop that replaced the corrupt upstream Socrata feed).
    return SpineConfig(
        state="NY",
        source_uri=f"s3://{R2_BUCKET}/polaris-warehouse/sos/ny_active_corporations_lance",
        source_columns=(
            "dos_id",
            "current_entity_name",
            "location_state",
            "dos_process_state",
            "ceo_state",
            "location_zip",
            "dos_process_zip",
            "ceo_zip",
            "initial_dos_filing_date_typed",
        ),
        select_sql="""
            SELECT DISTINCT
                dos_id                                                              AS state_entity_id,
                current_entity_name                                                 AS legal_name,
                CAST('ACTIVE' AS VARCHAR)                                           AS entity_status,
                COALESCE(location_state, dos_process_state, ceo_state)              AS physical_state,
                COALESCE(location_zip, dos_process_zip, ceo_zip)                    AS physical_zip,
                initial_dos_filing_date_typed                                       AS incorporation_date
            FROM src
        """,
        output_slug="sos_new_york_entities_lance",
        output_uri=f"s3://{R2_BUCKET}/polaris-warehouse/spines/sos_new_york_entities_lance",
        btree_columns=("state_entity_id", "physical_state", "incorporation_date"),
    )


def _build_one(cfg: SpineConfig) -> dict:
    LOG.info("[%s] reading source %s", cfg.state, cfg.source_uri)
    storage_options = _storage_options()
    src_ds = lance.dataset(cfg.source_uri, storage_options=storage_options)
    src_rows = src_ds.count_rows()
    LOG.info("[%s] source rows: %d", cfg.state, src_rows)

    arrow_tbl = src_ds.scanner(columns=list(cfg.source_columns)).to_table()
    LOG.info("[%s] arrow projected rows: %d cols: %d", cfg.state, arrow_tbl.num_rows, arrow_tbl.num_columns)

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=4")
    con.register("src", arrow_tbl)

    t0 = time.time()
    out_tbl = con.execute(cfg.select_sql).fetch_arrow_table()
    out_rows = out_tbl.num_rows
    LOG.info(
        "[%s] post-projection+DISTINCT rows: %d (source: %d, delta: %+d) in %.1fs",
        cfg.state, out_rows, src_rows, out_rows - src_rows, time.time() - t0,
    )

    null_inc = con.execute(
        f"SELECT count(*) FROM ({cfg.select_sql}) WHERE incorporation_date IS NULL"
    ).fetchone()[0]
    LOG.info("[%s] incorporation_date NULL rows: %d (%.2f%%)", cfg.state, null_inc, 100.0 * null_inc / max(out_rows, 1))

    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = "/tmp/lance"
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    t_write = time.time()
    with lance_commit_lock(cfg.output_slug):
        LOG.info("[%s] writing Lance dataset to %s", cfg.state, cfg.output_uri)
        ds = lance.write_dataset(
            out_tbl,
            cfg.output_uri,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t_write
        lance_rows = ds.count_rows()
        LOG.info("[%s] wrote %d rows in %.1fs (version=%s)", cfg.state, lance_rows, write_dur, ds.version)

        for col in cfg.btree_columns:
            t_idx = time.time()
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            LOG.info("[%s] BTREE on %s built in %.1fs", cfg.state, col, time.time() - t_idx)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            LOG.warning("[%s] optimize/cleanup non-fatal failure: %s", cfg.state, e)

    final_schema = {f.name: str(f.type) for f in ds.schema}
    LOG.info("[%s] final schema: %s", cfg.state, final_schema)

    return {
        "state": cfg.state,
        "source_uri": cfg.source_uri,
        "source_rows": src_rows,
        "output_uri": cfg.output_uri,
        "output_slug": cfg.output_slug,
        "spine_rows": lance_rows,
        "rows_delta_vs_source": lance_rows - src_rows,
        "incorporation_date_null_rows": null_inc,
        "btree_columns": list(cfg.btree_columns),
        "schema": final_schema,
    }


def _configs_for(states: Iterable[str]) -> list[SpineConfig]:
    builders = {
        "CA": _ca_config,
        "FL": _fl_config,
        "NY": _ny_config,
        "CO": _co_config,
    }
    return [builders[s]() for s in states]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", choices=["CA", "FL", "NY", "CO", "all"], default="all")
    args = parser.parse_args()
    states = ("CA", "FL", "NY", "CO") if args.state == "all" else (args.state,)

    results = []
    for cfg in _configs_for(states):
        results.append(_build_one(cfg))

    import json
    print("\n=== summary ===")
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
