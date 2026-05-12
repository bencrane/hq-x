# Audience specs — the contract substrate (Phase 2 scaffold)

The audience-spec layer is the partner-facing contract surface. Specs ARE
contracts: partners pay $25–45K per signed spec; the operator carries
refund risk if the cohort can't be maintained against the spec's
freshness SLA.

This document covers the Phase 2 **scaffold** — schema + spec language +
evaluator skeleton. Phase 3 wires cohort-events + match-invalidation;
Phase 4 ships the vector primitives (`similar_to`, `semantic_match`);
Phase 5 lands the matching engine; later sweep cycles migrate the ~80
existing audience surfaces inventoried in
`~/Desktop/hq/inventory/AUDIENCE-SPEC-SURFACE-INVENTORY-2026-05-11.md`.

## Lifecycle

```
   draft  ──preview──▶  preview  ──sign──▶  signed  ──revise──▶  superseded
     │                                                                │
     └─────────────────────────────────────────────────────────retired┘
```

- **draft** — partner-authored (or operator-co-authored) WIP. Mutable;
  every revision creates a new spec row pointing back at its parent.
- **preview** — has been run against the live catalog at least once;
  count + sample have been seen. Still mutable via a new revision.
- **signed** — immutable. The signing event froze the cohort to R2 as
  a parquet manifest; the partner has paid (or otherwise signaled
  intent via `partner_signature`); contract clock is running.
- **superseded** — a newer revision exists. Historical only.
- **retired** — operator closed the chain. Historical only.

## Contract semantics

**The spec IS the agreement.** A signed spec carries:

- `cohort_manifest_uri` — R2 parquet of `(entity_ref, attribute_snapshot)`
  for every entity in the cohort at the moment of signing. Immutable.
- `count_at_signing` — the cohort size the partner agreed to.
- `catalog_snapshot_ts` — the data-as-of timestamp the cohort was
  frozen against. Refund/replenishment math anchors here.
- `source_freshness_at_signing` — proof that every declared freshness
  SLA was met at the moment of signing.
- `contract_term_days` — default 90; `expires_at` is computed.

**Refund risk.** Operator owes the partner that the *live* cohort stays
healthy against the *signed* baseline for the contract term. The
`replenishment_status` endpoint surfaces `live_count` vs
`count_at_signing` + `at_risk` flag (currently `live_count < 0.95 *
count_at_signing`). Material attribute changes (per
`operator_data_anxieties_phase_0.md` concern 3) emit
`audience_spec_deliveries` rows with `event_kind='attribute_changed'`.

**Replenishment.** Operator's job between signing and expiry is to
maintain ingest cadence on the underlying sources so the cohort doesn't
drift. Per-source freshness SLAs are spec-declared and refused at
sign-time if not met today.

## Spec language

```python
class AudienceSpec(BaseModel):
    sources: list[CatalogRef]                       # required
    filters: list[ScalarPredicate]                  # AND-conjoined
    similar_to: SimilarityClause | None             # PHASE 4 placeholder
    semantic_match: SemanticPredicate | None        # PHASE 4 placeholder
    exclude: list[ExclusionRule]                    # opt-outs, blocklists
    enrich_with: list[CatalogRef]                   # join targets
    required_freshness: list[FreshnessRequirement]  # per-source SLA
```

- **CatalogRef** — names a table by `(namespace, table)`. Namespaces are
  universal (`fmcsa`, `usaspending`, etc.) or per-partner private
  (`partners.<pid>.<spec_id>`). NO `vertical_id` columns anywhere — per
  `vertical_network_platform_frame.md`.
- **ScalarPredicate** — `column`, `op` (eq/ne/in/nin/gt/gte/lt/lte/like/
  ilike/between/is_null/is_not_null), `value`.
- **FreshnessRequirement** — `(source, max_age_seconds)`. Refused at
  preview/sign if any source's latest snapshot is older.
- **SimilarityClause / SemanticPredicate** — Phase 4 placeholders. The
  evaluator raises `NotImplementedError` if used today; spec authors
  can declare them, deferring evaluation until Phase 4 ships.
- **ExclusionRule** — `entity_blocklist`, `contact_recency`,
  `source_age`, `custom`. Routing-by-kind lands with Phase 3
  (cohort-events + match-invalidation).

## REST API

All endpoints under `require_flexible_auth` (operator JWT or trigger
shared secret). All responses carry the `X-Data-Lineage` header
(Phase 0b — every catalog table the request actually queried).

```
POST /api/v1/audience-specs                      → create draft
POST /api/v1/audience-specs/{spec_id}/revisions  → new version
POST /api/v1/audience-specs/{spec_id}/preview    → count + sample
POST /api/v1/audience-specs/{spec_id}/sign       → freeze + create signing
GET  /api/v1/audience-specs/{spec_id}/signings   → signing history
GET  /api/v1/signings/{signing_id}               → signing detail
GET  /api/v1/signings/{signing_id}/replenishment → burn-down forecast
```

Status codes:

- `409 freshness_sla_breach` — preview/sign refused because a
  `required_freshness` SLA isn't met right now. Response body lists
  every failed check with `observed_age_seconds` and `max_age_seconds`.
- `501 not_implemented` — spec uses a Phase 4 primitive
  (`similar_to`, `semantic_match`) or a Phase 3 exclusion routing
  kind. Will become 200 when those phases ship.
- `404 spec_not_found / signing_not_found` — normal lookup failures.

## Data model

Three append-only tables in the `business` schema (migration
`20260512T010000_audience_specs_substrate.sql`):

- **`business.audience_specs`** — partner-authored draft + revision chain.
  Columns: `spec_id`, `partner_id` (FK `business.organizations`),
  `version`, `parent_spec_id`, `content jsonb`, `status`,
  `required_freshness jsonb`, audit cols.
- **`business.audience_spec_signings`** — immutable signing events.
  Columns: `signing_id`, `spec_id`, `signed_at`, `catalog_snapshot_ts`,
  `count_at_signing`, `cohort_manifest_uri`, `partner_signature jsonb`,
  `contract_term_days`, `expires_at` (generated), `source_freshness_at_signing jsonb`.
- **`business.audience_spec_deliveries`** — per-cohort multi-channel events.
  Columns: `delivery_id`, `signing_id`, `entity_ref`, `event_kind`
  (`entered_cohort` / `surfaced` / `viewed` / `reserved` / `claimed` /
  `dismissed` / `exited_cohort` / `attribute_changed`), `occurred_at`,
  `channel` (`portal` / `cold_email` / `operator_intro`),
  `attribute_snapshot jsonb`, `metadata jsonb`.

`partner_id` references `business.organizations` in v1; the architecture
treats partner+spec as the primitive regardless of the underlying row
shape. Per `vertical_network_platform_frame.md`, there is NO
`vertical_id` column anywhere in this schema.

## Evaluator architecture

`app/services/audience_spec/evaluator.py`:

- `compile(spec, snapshot_ts)` — produces `CompiledQuery(sql, params,
  sources, snapshot_ts)`. WHERE clause is built from `ScalarPredicate`
  by hand (single-table SELECT; sqlglot is a dep for forward
  compatibility but not load-bearing yet).
- `preview(spec_id)` — registers every source as a DuckDB view via
  PyIceberg's Arrow bridge (`table.scan().to_duckdb(...)`); runs
  `COUNT(*) + LIMIT 25 SELECT`; checks freshness BEFORE running.
- `sign(spec_id, partner_signature)` — preview-style query, but writes
  the full result to R2 as zstd parquet at
  `s3://dex-raw-landing-zone/audience-cohort-manifests/YYYY/MM/DD/<signing_id>.parquet`
  with two columns: `entity_ref TEXT`, `attribute_snapshot TEXT (JSON)`.
  Then inserts the signing row.
- `replenishment_status(signing_id)` — re-compiles + counts vs the
  at-signing baseline; computes days_remaining; flags `at_risk` if
  `live_count < 0.95 * count_at_signing`.

The catalog is the same Iceberg `SqlCatalog` DEX uses (Postgres-backed
metadata + R2-backed parquet). hq-x reads from it directly. When Polaris
ships (parallel cycle in flight), the catalog seam updates
(`app/services/audience_spec/catalog.py`) and nothing else changes.

## Smoke test

```bash
doppler --project hq-all --config prd run -- \
    uv run python -m scripts.smoke_audience_specs
```

Drives the full lifecycle against PROD:
draft a TX safety-rating-Satisfactory FMCSA spec → preview (~12k rows)
→ sign (freeze to R2) → verify the parquet exists at the signed URI →
fetch replenishment status (expect 90 days remaining, freshness OK,
non-zero live count).

## What's deferred

Not in this scaffold; staged for follow-on cycles:

- **Phase 3** — cohort-events log + match-invalidation (the
  `audience_spec_deliveries.event_kind='attribute_changed'` write
  path). Exclusion-rule routing.
- **Phase 4** — vector primitives. `similar_to` (k-NN against partner
  seed entities) and `semantic_match` (semantic-text-match). The
  pydantic placeholders are already in place; evaluator raises
  `NotImplementedError` today.
- **Phase 5** — matching engine. Persistent match objects + multi-channel
  surfacing per `matches_first_class_surfacing_multichannel.md`.
- **Sweep cycles after this** — migrating the ~80 existing audience
  surfaces (DEX `/api/v1/audiences/*`, FMCSA / govcontracts / dealbridge
  routers, hq-command audience-builder, partner-platform composer) onto
  this substrate per
  `~/Desktop/hq/inventory/AUDIENCE-SPEC-SURFACE-INVENTORY-2026-05-11.md`.

## References

- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/audience_spec_is_the_partner_contract.md`
- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/partner_intent_lives_in_the_spec.md`
- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/matching_engine_is_multi_relationship.md`
- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/vertical_network_platform_frame.md`
- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/outbound_is_emailbison_intros_are_on_platform.md`
- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/matches_first_class_surfacing_multichannel.md`
- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/operator_data_anxieties_phase_0.md`
- `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/app_responsibilities.md`
