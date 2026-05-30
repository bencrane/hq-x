"""Lance emit configs for the 6 Shovels canonical tables.

Each composes the canonical ``LanceEmitConfig`` (``scripts/_lib/lance_emit.py``)
in ``multi_snapshot`` + ``dedup_key`` mode — the Shovels incrementality model:

  * read the FULL ``shovels/<entity>/snapshot=*/part-*.parquet`` glob,
  * dedup to latest-per-PK via ``QUALIFY ROW_NUMBER() OVER (PARTITION BY <pk>
    ORDER BY ingested_at DESC) = 1``,
  * write Lance ``mode="overwrite"`` to
    ``s3://dex-raw-landing-zone/polaris-warehouse/shovels/<table>_lance``,
  * build a BTREE scalar index on the PK,
  * compact + cleanup.

Re-running the same snapshot date overwrites that R2 partition (the ingest
driver clears it first), so the deduped Lance row count is stable across
re-runs — idempotency holds at both the R2 and Lance layers.
"""
from __future__ import annotations

from scripts._lib.lance_emit import LanceEmitConfig

_LANCE_ROOT = "s3://dex-raw-landing-zone/polaris-warehouse/shovels"


def _config(*, table: str, entity_dir: str, pk: str) -> LanceEmitConfig:
    return LanceEmitConfig(
        dataset_slug=f"shovels_{table}_lance",
        r2_bucket="dex-raw-landing-zone",
        parquet_input_prefix=f"shovels/{entity_dir}",
        parquet_file_pattern="part-*.parquet",
        partition_mode="multi_snapshot",
        lance_uri=f"{_LANCE_ROOT}/shovels_{table}_lance",
        btree_column=pk,
        dedup_key=pk,
        dedup_order_col="ingested_at",
    )


PERMITS_EMIT = _config(table="permits", entity_dir="permit", pk="id")
CONTRACTORS_EMIT = _config(table="contractors", entity_dir="contractor", pk="id")
EMPLOYEES_EMIT = _config(table="employees", entity_dir="employee", pk="id")
RESIDENTS_EMIT = _config(table="residents", entity_dir="resident", pk="resident_key")
GEO_EMIT = _config(table="geo", entity_dir="geo", pk="geo_id")
TAGS_EMIT = _config(table="tags", entity_dir="tag", pk="id")

# table name (without the shovels_ prefix / _lance suffix) → emit config
EMIT_CONFIGS = {
    "permits": PERMITS_EMIT,
    "contractors": CONTRACTORS_EMIT,
    "employees": EMPLOYEES_EMIT,
    "residents": RESIDENTS_EMIT,
    "geo": GEO_EMIT,
    "tags": TAGS_EMIT,
}

# Polaris registration coordinates: namespace + table name.
POLARIS_NAMESPACE = "shovels"
POLARIS_TABLES = {
    "permits": "shovels_permits_lance",
    "contractors": "shovels_contractors_lance",
    "employees": "shovels_employees_lance",
    "residents": "shovels_residents_lance",
    "geo": "shovels_geo_lance",
    "tags": "shovels_tags_lance",
}
