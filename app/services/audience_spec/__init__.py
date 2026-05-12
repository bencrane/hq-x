"""Phase 2 contract substrate — partner-authored audience specs.

Specs ARE contracts. The composed audience spec is the partner-facing
agreement that underwrites lead-transfer pricing and drives operator
sourcing motion. This package owns:

- ``models``       — pydantic spec language (forward-compatible with
                     Phase 4 vector primitives via typed placeholders).
- ``evaluator``    — compile / preview / sign / replenishment_status.
                     DuckDB-over-Iceberg via the existing PyIceberg
                     SqlCatalog reads in data-engine-x.

The REST surface lives in ``app/routers/audience_specs_v1.py``.
The DB schema lives in
``migrations/20260512T010000_audience_specs_substrate.sql``.

Architectural anchors (memory):
- audience_spec_is_the_partner_contract.md
- partner_intent_lives_in_the_spec.md
- vertical_network_platform_frame.md (no vertical_id columns)
- matches_first_class_surfacing_multichannel.md
- operator_data_anxieties_phase_0.md (freshness SLAs declared by spec)
"""
