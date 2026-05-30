"""Shovels contractors ingest CLI → shovels_contractors_lance.

Entity: contractor (ContractorsRead, §6.3). PK/BTREE: ``id``.
Endpoint: ``contractors/search`` (geo + date-window). The identical schema is
also served by ``contractors?id=`` — this CLI uses the search path.

NOTE on id stability (§12): contractor ``id`` is NOT stable across Shovels
releases (regenerated in V2.1.7). The BTREE + dedup-latest-per-id are still
correct WITHIN a release window; a cross-release remap is a downstream concern,
explicitly out of scope here.

Query spec (required): ``geo_id`` + ``permit_from`` + ``permit_to``. Optional
``extra_filters`` (e.g. ``contractor_classification_derived``, ``contractor_name``,
``contractor_min_total_job_value``, ``include_tallies`` — reference §7).

    python -m scripts.shovels.ingest_contractors \\
        --query-spec '{"geo_id":"ROA3LFPdyBc","permit_from":"2024-01-01","permit_to":"2025-01-01","size":10,"extra_filters":{"include_tallies":true}}' \\
        --snapshot-date 2026-05-29 --apply
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.shovels._cli import run_entity_cli  # noqa: E402
from scripts.shovels._client import ShovelsClient  # noqa: E402
from scripts.shovels.entity_specs import CONTRACTOR_SPEC  # noqa: E402
from scripts.shovels.query_spec import ShovelsQuerySpec  # noqa: E402

SOURCE_ENDPOINT = "contractors/search"

_CONTRACTOR_SEARCH_KEYS = {
    "geo_id", "permit_from", "permit_to",
    "permit_q", "permit_tags", "permit_status",
    "property_type", "property_min_market_value",
    "contractor_classification_derived", "contractor_name", "contractor_website",
    "contractor_min_total_job_value", "contractor_min_total_permits_count",
    "contractor_min_inspection_pr", "contractor_license", "include_tallies",
}


def build_records(client: ShovelsClient, spec: ShovelsQuerySpec) -> Iterator[dict]:
    missing = [k for k in ("geo_id", "permit_from", "permit_to") if not getattr(spec, k)]
    if missing:
        raise SystemExit(f"FAIL: contractors ingest requires query-spec fields {missing}")
    base_params = spec.search_query_params(allowed_keys=_CONTRACTOR_SEARCH_KEYS)
    yield from client.paginate(
        "/contractors/search",
        base_params=base_params,
        size=spec.size,
        max_pages=spec.max_pages,
    )


if __name__ == "__main__":
    raise SystemExit(
        run_entity_cli(
            table="contractors",
            spec=CONTRACTOR_SPEC,
            source_endpoint=SOURCE_ENDPOINT,
            record_builder=build_records,
            doc="Shovels contractors (ContractorsRead) — verbatim raw + typed projection; latest-per-id over dated snapshots.",
        )
    )
