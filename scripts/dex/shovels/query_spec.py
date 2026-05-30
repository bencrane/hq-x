"""ShovelsQuerySpec — the Trigger-ready parameterization unit for the Shovels rail.

The table schema for each entity is fixed by the ENTITY, not by any client's
filter. The query spec is the ONLY thing a per-client scheduler varies: it
carries the geo/window/id-list/filter knobs that select WHICH rows a given run
fetches from the live Shovels API. The schema, R2 layout, Lance dataset, and
BTREE key are identical across every client; only the spec differs.

A scheduler (Trigger.dev / Modal — out of scope here) constructs one of these,
JSON-serializes it, and passes it to an entity CLI as ``--query-spec '<json>'``.

Design notes:
  * Pure dataclass (stdlib only) — no pydantic dependency at the ingest layer,
    matching the lightweight ``scripts/`` convention. Validation is intentionally
    permissive: the canonical reference (§8 tags, §7 filter enums) documents that
    Shovels itself does NOT enum-validate most filters, so we pass them through
    verbatim and let the API be the source of truth.
  * Money is integer cents downstream, dates are ``YYYY-MM-DD`` strings — see
    ``SHOVELS_API_CANONICAL_REFERENCE.md`` §2.
  * ``snapshot_date`` is NOT part of the spec — it is a per-run axis passed
    separately (``--snapshot-date``) so the same client spec can be re-run on a
    new date to land a fresh dated partition without editing the spec.

Per-entity required fields (enforced at fetch time, not here):
  * permit / contractor  → ``geo_id`` + ``permit_from`` + ``permit_to``
  * employee             → ``contractor_ids`` (non-empty)
  * resident             → ``address_geo_ids`` (non-empty)
  * geo                  → ``geo_states`` and/or ``geo_seed_*`` (catalog spec)
  * tag                  → none (static catalog pull; an empty spec is valid)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ShovelsQuerySpec:
    """Per-client query parameters for one Shovels ingest run.

    All fields optional at the dataclass level; per-entity ingests assert the
    subset they require. Unknown/extra filter keys for permit & contractor
    searches travel in ``extra_filters`` and are merged verbatim into the
    request query string (the canonical reference documents the full filter
    surface in §7).
    """

    # --- permit / contractor search axis (geo + date window) -------------- #
    geo_id: str | None = None
    permit_from: str | None = None  # YYYY-MM-DD (inclusive, on permit start_date)
    permit_to: str | None = None    # YYYY-MM-DD (inclusive)

    # --- employee axis ---------------------------------------------------- #
    contractor_ids: list[str] = field(default_factory=list)

    # --- resident axis ---------------------------------------------------- #
    address_geo_ids: list[str] = field(default_factory=list)

    # --- geo-dimension catalog axis --------------------------------------- #
    # A list of 2-letter state codes to enumerate (each → states/search detail
    # is trivial; the meat is the per-state city/county/jurisdiction pull).
    geo_states: list[str] = field(default_factory=list)
    # Optional explicit search seeds, e.g. {"cities": ["Palm Desert CA"]}.
    # Keys ∈ {cities, counties, jurisdictions, zipcodes, states}. Each value is
    # a list of free-text `q` strings handed to the matching */search endpoint.
    geo_search_seeds: dict[str, list[str]] = field(default_factory=dict)
    # Cap on geo rows materialized per state (keeps catalog pulls bounded for
    # the E2E proof; a real backfill would raise or remove this).
    geo_max_per_state: int | None = None

    # --- shared knobs ----------------------------------------------------- #
    # Page size for billable list endpoints (== credit spend per page; §3).
    size: int = 50
    # Hard cap on the number of API pages a single run will fetch (credit
    # guardrail). None = exhaust the cursor.
    max_pages: int | None = None
    # Verbatim extra filters merged into permit/contractor search query strings
    # (e.g. {"permit_tags": ["solar"], "property_type": "residential"}).
    extra_filters: dict[str, Any] = field(default_factory=dict)

    # --- bookkeeping ------------------------------------------------------ #
    # Free-text label a scheduler can stamp (e.g. the client slug) for audit.
    client_label: str | None = None

    # ------------------------------------------------------------------ #
    # serialization
    # ------------------------------------------------------------------ #
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: str) -> "ShovelsQuerySpec":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("query spec JSON must decode to an object")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShovelsQuerySpec":
        known = {f for f in cls.__dataclass_fields__}  # noqa: F841 (clarity)
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(
                f"unknown query-spec keys: {sorted(unknown)}; "
                f"put ad-hoc API filters under 'extra_filters'"
            )
        return cls(**data)

    # ------------------------------------------------------------------ #
    # per-entity request-surface helpers
    # ------------------------------------------------------------------ #
    def search_query_params(self, *, allowed_keys: set[str]) -> list[tuple[str, Any]]:
        """Flatten this spec into ``(key, value)`` query params for a
        permit/contractor *search* request, restricted to ``allowed_keys``.

        Required geo+window keys come from the typed fields; everything else is
        pulled from ``extra_filters``. List-valued filters expand to repeated
        params (Shovels AND-combines repeated ``permit_tags`` etc. — §7.4).
        """
        merged: dict[str, Any] = {
            "geo_id": self.geo_id,
            "permit_from": self.permit_from,
            "permit_to": self.permit_to,
            **self.extra_filters,
        }
        params: list[tuple[str, Any]] = []
        for key in allowed_keys:
            if key not in merged:
                continue
            value = merged[key]
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    if item is not None and item != "":
                        params.append((key, item))
            else:
                params.append((key, value))
        return params
