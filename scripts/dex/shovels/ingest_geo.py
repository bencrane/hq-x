"""Shovels geo-dimension ingest CLI → shovels_geo_lance.

Entity: geo dimension (GeoEntitiesRead + detail, §6.6). PK/BTREE: ``geo_id``.
Endpoints (all FREE): ``states/search``, ``cities/search``, ``counties/search``,
``jurisdictions/search``, ``zipcodes/search`` for the rows, plus ``cities`` /
``counties`` / ``jurisdictions`` detail for the hierarchy nesting
(counties/jurisdictions/zipcodes maps). Resolver/search/detail are free, so this
catalog pull costs 0 credits.

Each row is normalized into the common geo shape and augmented with
``_geo_type`` (city|county|jurisdiction|state|zipcode) and ``_seed_state`` before
projection; the typed schema BTREEs ``geo_id``.

Query spec (catalog/seed): provide ``geo_states`` (2-letter codes — each state's
cities/counties/jurisdictions are enumerated) and/or ``geo_search_seeds``
(explicit free-text ``q`` per geo type). ``geo_max_per_state`` bounds the city
detail pulls. At least one of ``geo_states`` / ``geo_search_seeds`` is required.

    python -m scripts.shovels.ingest_geo \\
        --query-spec '{"geo_states":["VT"],"geo_max_per_state":40}' \\
        --snapshot-date 2026-05-29 --apply
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.shovels._cli import run_entity_cli  # noqa: E402
from scripts.shovels._client import ShovelsAPIError, ShovelsClient  # noqa: E402
from scripts.shovels.entity_specs import GEO_SPEC  # noqa: E402
from scripts.shovels.query_spec import ShovelsQuerySpec  # noqa: E402

LOG = logging.getLogger("shovels.ingest.geo")
SOURCE_ENDPOINT = "states|cities|counties|jurisdictions|zipcodes/search(+detail)"

_SEARCH_PATHS = {
    "states": "/states/search",
    "cities": "/cities/search",
    "counties": "/counties/search",
    "jurisdictions": "/jurisdictions/search",
    "zipcodes": "/zipcodes/search",
}
_DETAIL_PATHS = {
    "cities": "/cities",
    "counties": "/counties",
    "jurisdictions": "/jurisdictions",
}


def _search(client: ShovelsClient, geo_type: str, q: str, *, size: int, max_pages: int | None) -> list[dict]:
    path = _SEARCH_PATHS[geo_type]
    try:
        return list(client.paginate(path, base_params=[("q", q)], size=size, max_pages=max_pages))
    except ShovelsAPIError as exc:
        LOG.warning("%s search q=%r failed (%s) — skipping", geo_type, q, exc)
        return []


def _detail(client: ShovelsClient, geo_type: str, geo_id: str) -> dict | None:
    """Fetch the detail object (counties/jurisdictions/zipcodes nesting) for a
    city/county/jurisdiction geo_id. Detail is wrapped in the items envelope."""
    path = _DETAIL_PATHS.get(geo_type)
    if not path:
        return None
    try:
        body = client.get_json(path, [("geo_id", geo_id)])
    except ShovelsAPIError as exc:
        LOG.warning("%s detail geo_id=%s failed (%s)", geo_type, geo_id, exc)
        return None
    items = body.get("items") or []
    if items and isinstance(items[0], dict):
        return items[0]
    # Some detail responses are the bare object (not wrapped) — tolerate both.
    return body if body.get("geo_id") else None


def _emit_row(*, geo_type: str, seed_state: str | None, base: dict, detail: dict | None) -> dict:
    """Merge a search row + optional detail into the normalized geo record."""
    merged: dict[str, Any] = dict(base)
    if detail:
        for k in ("counties", "jurisdictions", "zipcodes", "name", "state"):
            if detail.get(k) is not None:
                merged[k] = detail[k]
    merged["_geo_type"] = geo_type
    merged["_seed_state"] = seed_state
    return merged


def build_records(client: ShovelsClient, spec: ShovelsQuerySpec) -> Iterator[dict]:
    states = [s for s in (spec.geo_states or []) if s]
    seeds = spec.geo_search_seeds or {}
    if not states and not seeds:
        raise SystemExit(
            "FAIL: geo ingest requires query-spec 'geo_states' and/or 'geo_search_seeds'"
        )

    seen: set[str] = set()
    size = spec.size if spec.size else 50

    # --- state-driven catalog: state row + its cities (with detail) -------- #
    for state in states:
        # State dimension row (states/search by the code).
        for srow in _search(client, "states", state, size=size, max_pages=spec.max_pages):
            gid = srow.get("geo_id")
            if gid and gid not in seen:
                seen.add(gid)
                yield _emit_row(geo_type="state", seed_state=state, base=srow, detail=None)

        # Cities in the state. cities/search is a fixed top-N resolver keyed on
        # `q`; using the state code as q returns that state's cities. We bound
        # the detail fan-out with geo_max_per_state.
        cities = _search(client, "cities", state, size=size, max_pages=spec.max_pages)
        cities = [c for c in cities if (c.get("state") or "").upper() == state.upper()]
        cap = spec.geo_max_per_state
        if cap is not None:
            cities = cities[:cap]
        for c in cities:
            gid = c.get("geo_id")
            if not gid or gid in seen:
                continue
            seen.add(gid)
            detail = _detail(client, "cities", gid)
            yield _emit_row(geo_type="city", seed_state=state, base=c, detail=detail)

    # --- explicit search seeds (q per geo type) ---------------------------- #
    for geo_type, queries in seeds.items():
        if geo_type not in _SEARCH_PATHS:
            LOG.warning("unknown geo_search_seeds type %r — skipping", geo_type)
            continue
        for q in queries or []:
            for row in _search(client, geo_type, q, size=size, max_pages=spec.max_pages):
                gid = row.get("geo_id")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                detail = _detail(client, geo_type, gid) if geo_type in _DETAIL_PATHS else None
                yield _emit_row(geo_type=geo_type.rstrip("s") if geo_type != "zipcodes" else "zipcode",
                                seed_state=None, base=row, detail=detail)


if __name__ == "__main__":
    raise SystemExit(
        run_entity_cli(
            table="geo",
            spec=GEO_SPEC,
            source_endpoint=SOURCE_ENDPOINT,
            record_builder=build_records,
            doc="Shovels geo dimension (states/cities/counties/jurisdictions/zipcodes + detail) — FREE catalog; PK=geo_id.",
        )
    )
