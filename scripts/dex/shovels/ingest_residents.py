"""Shovels residents ingest CLI → shovels_residents_lance.

Entity: resident (ResidentsRead, §6.5, PII). PK/BTREE: synthesized
``resident_key`` (see ``entity_specs.resident_key``) — residents have NO natural
id, so we key deterministically on ``address_geo_id`` + a hash of
name/email/phone. Same person at same address on a re-fetch ⇒ identical key ⇒
dedup collapses to one row.

Endpoint: ``addresses/{geo_id}/residents`` — fanned out over a list of address
geo_ids. Requires ADDRESS-type geo_ids specifically (§13.8): a city/county
geo_id 422s. A scheduler sources these from a prior permits pull
(``permit.address_id`` == ``geo_ids.address_id``). Billed 1/record.

Query spec (required): ``address_geo_ids`` (non-empty).

    python -m scripts.shovels.ingest_residents \\
        --query-spec '{"address_geo_ids":["BJJzSSI7MWQ"],"size":10}' \\
        --snapshot-date 2026-05-29 --apply
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.shovels._cli import run_entity_cli  # noqa: E402
from scripts.shovels._client import ShovelsAPIError, ShovelsClient  # noqa: E402
from scripts.shovels.entity_specs import RESIDENT_SPEC, resident_key  # noqa: E402
from scripts.shovels.query_spec import ShovelsQuerySpec  # noqa: E402

LOG = logging.getLogger("shovels.ingest.residents")
SOURCE_ENDPOINT = "addresses/{geo_id}/residents"


def build_records(client: ShovelsClient, spec: ShovelsQuerySpec) -> Iterator[dict]:
    address_ids = [a for a in (spec.address_geo_ids or []) if a]
    if not address_ids:
        raise SystemExit("FAIL: residents ingest requires query-spec 'address_geo_ids' (non-empty)")
    for geo_id in address_ids:
        try:
            count = 0
            for rec in client.paginate(
                f"/addresses/{geo_id}/residents",
                base_params=[],
                size=spec.size,
                max_pages=spec.max_pages,
            ):
                # Augment with the deterministic key + the address geo_id so the
                # spec's extractors (_resident_key / _address_geo_id) can read
                # them. The raw record itself is preserved verbatim in raw_json.
                rec["_address_geo_id"] = geo_id
                rec["_resident_key"] = resident_key(address_geo_id=geo_id, raw=rec)
                count += 1
                yield rec
            LOG.info("address %s -> %d resident(s)", geo_id, count)
        except ShovelsAPIError as exc:
            LOG.warning("address %s residents fetch failed (%s) — skipping", geo_id, exc)
            continue


if __name__ == "__main__":
    raise SystemExit(
        run_entity_cli(
            table="residents",
            spec=RESIDENT_SPEC,
            source_endpoint=SOURCE_ENDPOINT,
            record_builder=build_records,
            doc="Shovels residents (ResidentsRead, PII) per address — verbatim raw + typed; PK=deterministic resident_key.",
        )
    )
