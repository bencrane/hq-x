"""Shovels permits ingest CLI → shovels_permits_lance.

Entity: permit (PermitsRead, §6.1). PK/BTREE: ``id``.
Endpoint: ``permits/search`` (geo + date-window). The identical schema is also
served by ``permits?id=`` and ``contractors/{id}/permits`` — this CLI uses the
search path (the canonical per-client geo/window pull); the by-id/sub-resource
feeds project into the SAME typed schema if a future caller wires them.

Query spec (required): ``geo_id`` + ``permit_from`` + ``permit_to``. Optional
filters travel in ``extra_filters`` (e.g. ``permit_tags``, ``property_type``,
``permit_status``, ``permit_min_job_value`` — full surface in reference §7).

Trigger-ready invocation (scheduler varies ONLY --query-spec):
    python -m scripts.shovels.ingest_permits \\
        --query-spec '{"geo_id":"ROA3LFPdyBc","permit_from":"2025-01-01","permit_to":"2025-02-01","size":10}' \\
        --snapshot-date 2026-05-29 --apply
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.shovels._cli import run_entity_cli  # noqa: E402
from scripts.shovels._client import ShovelsClient  # noqa: E402
from scripts.shovels.entity_specs import PERMIT_SPEC  # noqa: E402
from scripts.shovels.query_spec import ShovelsQuerySpec  # noqa: E402

SOURCE_ENDPOINT = "permits/search"

# Permit/contractor search filter surface we forward verbatim (reference §7).
_PERMIT_SEARCH_KEYS = {
    "geo_id", "permit_from", "permit_to",
    "permit_q", "permit_tags", "permit_status", "permit_has_contractor",
    "permit_min_approval_duration", "permit_min_construction_duration",
    "permit_min_inspection_pr", "permit_min_job_value", "permit_min_fees",
    "property_type", "property_min_market_value", "property_min_building_area",
    "property_min_lot_size", "property_min_story_count", "property_min_unit_count",
    "contractor_classification_derived",
}


def build_records(client: ShovelsClient, spec: ShovelsQuerySpec) -> Iterator[dict]:
    missing = [k for k in ("geo_id", "permit_from", "permit_to") if not getattr(spec, k)]
    if missing:
        raise SystemExit(f"FAIL: permits ingest requires query-spec fields {missing}")
    base_params = spec.search_query_params(allowed_keys=_PERMIT_SEARCH_KEYS)
    yield from client.paginate(
        "/permits/search",
        base_params=base_params,
        size=spec.size,
        max_pages=spec.max_pages,
    )


if __name__ == "__main__":
    raise SystemExit(
        run_entity_cli(
            table="permits",
            spec=PERMIT_SPEC,
            source_endpoint=SOURCE_ENDPOINT,
            record_builder=build_records,
            doc="Shovels permits (PermitsRead) — verbatim raw + typed projection; latest-per-id over dated snapshots.",
        )
    )
