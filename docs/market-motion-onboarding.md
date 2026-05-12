# Market-motion onboarding playbook

**Last updated:** 2026-05-12  
**Audience:** operator, engineering  
**Status:** v1 — covers Trucking (motion #1, live) and GovContracts (motion #2, pre-launch) as reference instances

---

## What is a market motion?

A market motion is a brand-scoped instance of the platform. The operator runs the business; each motion is a different front door aimed at a different industry audience — but they all share the same data lake, catalog, matching engine, and contract substrate underneath.

| Motion | Brand domain | Supply side | Demand side | Status |
|--------|-------------|-------------|-------------|--------|
| Trucking | licensedtohaul.com | FMCSA-registered carriers (DOT#) | Lenders, insurers, factors (UCC LEI) | Live |
| GovContracts | TBD | SAM-registered govcontractors (UEI) | Capital providers, teaming agencies (UEI / DUNS) | Pre-launch |
| Lending | TBD | Borrowers / lending orgs (EIN) | Fund-of-funds, capital partners | Planned |

**The architectural primitive is `partner + signed spec`, not vertical.** "Market motion" or "vertical" is a UX label in the partner-portal template gallery (for browsing/discovery) — it is not a data-architecture primitive. Sources, matching relationships, and audience specs reference entities and partners directly. There is no `vertical_id` anywhere in the schema.

**Engineering gates the motion:** The bottleneck for adding motion #3, #4, etc. is how many partner sales calls the operator can handle, not engineering. Once the substrate is in place (it is, as of 2026-05-12), adding a new motion is mostly GTM work + 1-3 days of data-source onboarding if new public sources are needed.

---

## Pre-launch (1 week before)

### 1. Define the supply side and demand side

Decide who the two sides of each match relationship are for this motion. "Supply side" and "demand side" are role labels for a specific match context — the same entity can be supply-side in one relationship and demand-side in another. Do not put `is_supply` / `is_demand` flags anywhere.

**Identify:**

| Question | Trucking example | GovContracts example |
|----------|-----------------|----------------------|
| What entity is the primary profile? | FMCSA-registered carrier | SAM-registered govcontractor |
| What is the natural key? | DOT number (`dot_number`) | UEI from SAM.gov (`uei`) |
| What secondary sources enrich the profile? | UCC filings (insurance / lien signals), PDL (people enrichment), FMCSA inspection/crash/SMS feeds | USAspending obligations, FPDS contract history, SAM.gov opportunities |
| Who are the paying demand-side partners? | Insurers, lenders, factors, brokers | Capital providers, teaming agencies, contract-bond underwriters |
| What is the demand side's natural key? | UCC LEI, EIN | UEI, DUNS |
| What relationship types apply? | `demand_side_fulfillment_paid_spec` (lender/insurer buys cohort of carriers) | `demand_side_fulfillment_paid_spec` + `opportunity_matching` (contractor matches SAM opps) |

Document the answers before touching the database. These define which sources to activate and which relationship config rows to seed.

### 2. Identify data sources

Cross-reference your supply/demand definition against `ops.data_sources` to see which sources are already ingested:

```bash
# via DEX CLAUDE.md §"Doppler shell gotcha" wrapper
doppler run --project hq-all --config prd -- bash -c \
  'psql "$DEX_DB_URL_POOLED" -c "SELECT display_name, format, status FROM ops.data_sources ORDER BY display_name;"'
```

As of 2026-05-12 the registry contains 74+ sources across R2/Parquet/Iceberg, Lance, RisingWave MVs, and Polaris generic tables. Active sources covering GovContracts specifically:

| Source | Format | Key | SLA |
|--------|--------|-----|-----|
| SAM.gov entities | R2 parquet + Polaris Iceberg | `uei` | 24h |
| SAM.gov active opportunities | R2 parquet + Lance (`sam_opps_active`) | `notice_id` | 12h |
| SAM.gov archived opportunities | R2 parquet | `notice_id` | weekly |
| USAspending contract awards (daily delta) | R2 parquet + Lance (`usaspending_contracts`) | `award_id` / `recipient_uei` | 24h |
| GLEIF LEI registry | R2 parquet | `lei` | weekly |
| SBA loans (7a, 504, PPP, EIDL) | R2 parquet | `borrower_ein` | weekly |

For sources not yet in `ops.data_sources`, flag them now and plan ingest scope (see step 5).

### 3. Pick the brand

- Register a domain (Namecheap, Cloudflare, etc.). The brand domain is stored as a partner attribute in `business.organizations`, not in the data layer.
- Supply-side outbound emails go through emailbison — configure a sender identity there for the brand domain.
- Brand routing in the partner portal is driven by a `primary_brand` or equivalent attribute on the partner/org record. The frontend renders the correct brand surface based on which domain the request arrived on. The data architecture does not change shape per brand.
- Do not create `verticals.*` namespaces in the Polaris catalog. Do not add `vertical_id` columns to any schema table.

---

## Launch (day 0)

### 4. Add the brand to partner-platform

Locate the partner-platform brand registry (see `apps/partner-platform/`). The `business.organizations` table in hq-x holds demand-side partner orgs. Seed a row for the operator's brand if it does not exist:

```sql
-- In hq-x Supabase (via DEX_DB_URL or hq-x Supabase connection)
INSERT INTO business.organizations (id, name, type, primary_brand, created_at)
VALUES (gen_random_uuid(), 'GovContracts', 'operator_brand', 'govcontracts.example.com', NOW())
ON CONFLICT DO NOTHING;
```

The portal's routing layer reads `primary_brand` to select brand assets (logo, color scheme, copy). No DDL change required for a new brand — it is a data row, not a schema migration.

### 5. Wire any new sources

For each source needed for this motion that is NOT already in `ops.data_sources`, follow the canonical ingest pattern documented in `apps/data-engine-x/CLAUDE.md §"Source ingest invariant"`. The pattern in brief:

**Standard sources (< 1M rows/ingest):**

1. Create a timestamped migration `apps/data-engine-x/supabase/migrations/YYYYMMDDHHMMSS_source_<name>.sql` that adds `entities.source_<name>` with:
   - True 1:1 column mirror of the upstream schema
   - `raw_source_row jsonb NOT NULL`
   - Full 9-column canonical provenance set (`source_provider`, `source_filename`, `source_download_url`, `source_observed_at`, `source_run_metadata`, `source_task_id`, `source_schedule_id`, `ingested_at`)
   - PK = the source's natural ID (UEI, EIN, DOT, etc.)
   - Sibling `ops.<source>_ingest_runs` table for run tracking

2. Write an ingest script at `apps/data-engine-x/scripts/run_<source>_ingest.py`. Apply via Modal cron at `apps/data-engine-x/modal/<source>_ingest_app.py`.

3. Seed the observability row:
   ```bash
   doppler run --project hq-all --config prd -- python apps/data-engine-x/scripts/seed_observability_sources.py
   ```
   Or use the generic seeder pattern from `scripts/seed_lance_observability_source.py`.

**High-volume sources (> 1M rows/ingest or > 10M cumulative) — R2 + RisingWave Fuel Tank:**

Do NOT create a Postgres `entities.source_*` table. Instead:

1. Upload ZSTD-compressed Parquet to R2 at `s3://dex-raw-landing-zone/<source-hyphenated>/year=<YYYY>/part-NNNNN.parquet`.
2. Wire via RisingWave using introspect-and-generate DDL (pattern from `scripts/apply_hmda_rw_volume_king.py`).
3. Register an audit ledger at `ops.<source>_r2_ingest_runs` in Postgres.

**Then emit to Lance (for vector/ANN-enabled sources):**

For sources whose entity profiles will participate in semantic matching, re-emit from Parquet to Lance using `LanceSourceEmitter`:

```python
# apps/data-engine-x/scripts/run_<source>_lance_emit.py
from scripts._lib.lance_emit import LanceSourceEmitter, LanceEmitConfig

config = LanceEmitConfig(
    dataset_slug="<source>_essentials",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="<source-hyphenated>/",
    parquet_file_pattern="*.parquet",
    partition_mode="latest_snapshot",
    lance_uri="s3://dex-raw-landing-zone/lance/<source>_essentials/",
    btree_column="<natural_key>",
)
LanceSourceEmitter(config).run()
```

Register in Polaris via `scripts/init_polaris_lance_generic.py` (see usage in `scripts/init_polaris_lance_fmcsa_carrier_essentials.py`).

Set the SLA in `ops.data_source_slas`. Phase 0a alerting picks up any breach automatically (the alerter cron runs every 15 minutes; 148+ Telegram subscriptions are pre-seeded for operator notification).

Ingest effort per new source following this pattern: **~10 minutes** for sources whose schema is understood; 30-60 minutes for sources with complex normalization.

### 6. Seed material attribute declarations

For each source that participates in signed cohorts, declare which attribute changes invalidate an active match. Phase 3's detection cron (`data-engine-x-material-change-cron`, runs every 6h UTC) automatically picks up declarations in `ops.material_attribute_declarations`.

```bash
# Reference: apps/data-engine-x/scripts/seed_material_declarations_fmcsa.py
doppler run --project hq-all --config prd -- python apps/data-engine-x/scripts/seed_material_declarations_fmcsa.py
```

For GovContracts, material attributes to declare on the govcontractor profile:

| Source | Attribute | Change kind | Why it matters |
|--------|-----------|-------------|----------------|
| SAM entities | `registration_status` | `value_revoked` | Excluded from federal contracting |
| SAM entities | `exclusion_status_flag` | `value_appeared` | Debarred / suspended |
| USAspending | `obligation_amount_ytd` | `threshold_crossed` | Revenue tier reclassification |
| SAM opportunities | `response_deadline` | `value_disappeared` | Opp closed/cancelled |
| SAM entities | `naics_codes` | `value_changed` | Core competency shift |

Insert rows in `ops.material_attribute_declarations` (UNIQUE on `(source_id, attribute_name)`; safe to re-run):

```sql
INSERT INTO ops.material_attribute_declarations
  (declaration_id, source_id, attribute_name, change_kind, ...)
VALUES (gen_random_uuid(), '<sam_source_id>', 'registration_status', 'value_revoked', ...)
ON CONFLICT (source_id, attribute_name) DO NOTHING;
```

The change_kind values follow the `material_change_kind` enum: `tier_change`, `value_revoked`, `value_appeared`, `value_disappeared`, `threshold_crossed`, `value_changed`.

### 7. Configure matching relationships

Decide which relationship types apply to this motion. Each relationship type is a config row — no code change needed unless a new scoring algorithm or new surfacing channel is required.

The matching engine (Phase 5 substrate) reads from `business.matching_relationships`. Seed default rows for the new motion:

**For GovContracts, two relationship types:**

```sql
-- Relationship 1: demand-side fulfillment (capital provider buys cohort of govcontractors)
INSERT INTO business.matching_relationships
  (relationship_id, motion_tag, intent_source, description, scoring_strategy, surfacing_rule, enabled)
VALUES
  (gen_random_uuid(), 'govcontracts',
   'paid_specs',
   'Capital provider / teaming agency buys cohort of govcontractors matching their criteria',
   '{"scalar_weight": 0.7, "vector_weight": 0.3, "recency_boost": true}',
   '{"channel": "portal", "when": "on_match", "with_context": "cohort_manifest"}',
   true);

-- Relationship 2: opportunity matching (govcontractor receives SAM.gov opp matches)
INSERT INTO business.matching_relationships
  (relationship_id, motion_tag, intent_source, description, scoring_strategy, surfacing_rule, enabled)
VALUES
  (gen_random_uuid(), 'govcontracts',
   'preferences',
   'Govcontractor receives matched SAM.gov opportunities based on NAICS/PSC/set-aside profile',
   '{"scalar_weight": 0.5, "vector_weight": 0.5, "recency_boost": true}',
   '{"channel": "portal", "when": "daily_digest", "with_context": "opportunity_feed"}',
   true);
```

The `motion_tag` field is a free-text label used for operator filtering in the dashboard — it does NOT create a vertical partition in the data layer.

Scoring weights (`scalar_weight`, `vector_weight`, `recency_boost`) are defaults. The operator tunes these per partner via the operator dashboard after seeing real match quality.

### 8. Seed initial audience templates (optional)

Partners (or the operator on their behalf) fork draft specs from gallery templates. Templates live as YAML files that the catalog gallery surfaces with vertical-tag filtering.

Create starter templates at `apps/hq-x/data/audience_templates/govcontracts/`:

```yaml
# apps/hq-x/data/audience_templates/govcontracts/growth_stage_cyber_contractors.yaml
name: Growth-stage cyber govcontractors
motion_tag: govcontracts
description: SAM-registered govcontractors with $1M-$10M in NAICS 541519/541512 obligations in the past 24 months
sources:
  - catalog_ref: usaspending.contracts
  - catalog_ref: sam.entities
filters:
  - source: usaspending.contracts
    field: naics_code
    op: in
    values: ["541519", "541512", "541513", "541511"]
  - source: usaspending.contracts
    field: obligation_amount_24mo
    op: gte
    value: 1000000
  - source: usaspending.contracts
    field: obligation_amount_24mo
    op: lte
    value: 10000000
  - source: sam.entities
    field: registration_status
    op: eq
    value: "Active"
  - source: sam.entities
    field: exclusion_status_flag
    op: eq
    value: null
required_freshness:
  - source: usaspending.contracts
    max_age_seconds: 86400
  - source: sam.entities
    max_age_seconds: 43200
```

The spec language (pydantic models at `apps/hq-x/app/services/audience_spec/models.py`) accepts `CatalogRef`, `ScalarPredicate`, `FreshnessRequirement`, `ExclusionRule`, `SimilarityClause`, and `SemanticPredicate`. Semantic criteria (`similar_to`, `semantic_match`) activate the Phase 4 vector primitives.

---

## First 30 days

### 9. Partner acquisition pipeline

The operator drives demand-side partner acquisition. Engineering supports by keeping the audience composer UI and matching-engine demos running. Each signed partner spec lands in `business.audience_specs` (status=`signed`) and creates an `audience_spec_signings` row with:

- `count_at_signing` — the cohort size the partner agreed to
- `catalog_snapshot_ts` — the data-as-of timestamp the cohort was frozen against
- `r2_cohort_manifest_uri` — path to the signed cohort manifest Parquet in R2

Track partner conversations + signed specs in `business.organizations` + `business.audience_specs`. The REST surface is at `apps/hq-x/app/routers/audience_specs_v1.py`:

```
POST /api/v1/audience-specs                       — create draft
POST /api/v1/audience-specs/{spec_id}/preview     — get count + sample before signing
POST /api/v1/audience-specs/{spec_id}/sign        — mint the contract
GET  /api/v1/signings/{signing_id}/replenishment  — live cohort vs signed baseline
```

Partners need predictable counts + samples before signing — `preview` runs the spec evaluator against fresh data and refuses with `FreshnessSLABreach` if source freshness SLAs are not met. This is the trust anchor for the per-lead pricing.

### 10. Supply-side onboarding

The operator invites supply-side entities (govcontractors, carriers, etc.) to the platform via emailbison. The value proposition is "we have your public-record profile accurate and live." The operator does not build email templates or delivery infrastructure — emailbison handles the outbound stack (DNS, SPF/DKIM, sender identity, cadence).

Each supply-side entity that registers gets a portal view showing:
- Their current data profile (SAM entity, USAspending history, etc.)
- Opportunities matched against their NAICS/PSC/set-aside profile (from the `opportunity_matching` relationship type)
- Inbound partner interest (from paid `demand_side_fulfillment_paid_spec` relationships)

The entity profile is built from the data lake at query time — there is no per-entity data sync pipeline. The data lake IS the source of truth.

**Supply-side trust contract:** freshness SLAs per source, enforced by Phase 0a/0c. If FMCSA goes stale beyond 24h or SAM.gov beyond 12h, the operator gets a Telegram alert (via Phase 0c's alerter cron at `apps/data-engine-x/modal/alerter_cron_app.py`).

### 11. Operator's daily workflow

1. **System-health dashboard** (`apps/hq-command/`) — check source freshness ledger. Red = SLA breach. `ops.data_source_ingest_runs` is the ground truth; the Phase 0a observability layer surfaces it via `GET /api/v1/observability/sources` with `X-Data-Lineage` header for audit trail.

2. **Cohort drift alerts** — Phase 3's material-change detection runs every 6h. Material attribute changes (safety rating tier change, SAM deregistration, authority revocation) emit to `ops.material_change_events`, which the cohort scanner at `apps/hq-x/app/services/cohort_drift_scanner.py` cross-references against active signings. Telegram alerts fire for each affected cohort. Check and triage these.

3. **Operator queue** — pending cold-email handoffs, partner spec sign-offs, and cohort burn-down alerts surface here. Triage and act.

4. **Sign partner specs** — the operator co-drives spec composition with each partner. Use the audience composer UI to draft, preview (check count + freshness), and sign. A signing mints an immutable cohort manifest in R2 and starts the replenishment tracking clock.

5. **Tune matching weights** — after seeing real match quality for a partner, update `scoring_strategy` in `business.matching_relationships` via the operator dashboard. The scoring engine reads these weights on each match cycle.

### 12. Replenishment monitoring

Per signed spec, the operator monitors cohort burn-down:

```
GET /api/v1/signings/{signing_id}/replenishment
→ {
    "count_at_signing": 4800,
    "live_count": 4612,
    "days_remaining": 74,
    "at_risk": false,
    "sources": [
      { "source": "sam.entities", "freshness_seconds": 3200, "sla_met": true }
    ]
  }
```

**Refund risk trigger:** if `live_count` falls below `count_at_signing * 0.85` (configurable threshold) before the contract expires, the operator is at refund risk. Phase 3's cohort drift scanner emits `attribute_changed` delivery rows into `business.audience_spec_deliveries` for every material change to a cohort entity. These feed the replenishment view.

The operator dashboard surfaces:
- Cohort size now vs at signing
- Days remaining on contract
- Source freshness vs SLA
- `attribute_changed` events count (how many entities in the cohort have had a material change)

At-risk contracts (cohort size trending toward the refund threshold before expiry) show a warning badge.

---

## Future iterations

### 13. Add new sources

The ingest pattern is established. Adding a net-new source for an existing motion:

1. Write a migration (`YYYYMMDDHHMMSS_source_<name>.sql`) — ~15 lines per source for standard ingest, ~30 for R2/RW Fuel Tank
2. Write an ingest script (`scripts/run_<source>_ingest.py`) — copy the closest existing script (e.g., `run_fmcsa_carrier_essentials_lance_emit.py` for Lance emitters)
3. Create a Modal cron app (`modal/<source>_ingest_app.py`) — 20 lines from the template
4. Seed the observability row — `scripts/seed_observability_sources.py` (generic) or `scripts/seed_lance_observability_source.py` (Lance-specific)
5. Declare material attributes if the source participates in signed cohorts
6. (Optional) emit to Lance if the source needs vector search

Total effort per new source: **~10 minutes** following the canary/sweep pattern documented in `apps/data-engine-x/CLAUDE.md §"Source ingest invariant"`.

Verify with the helper:

```bash
source "$(git rev-parse --show-toplevel)/apps/data-engine-x/scripts/_lib/dex.sh"
dex_provenance_check entities.source_<name>
```

### 14. Add new relationship types

Define a new `business.matching_relationships` row:

```sql
INSERT INTO business.matching_relationships (
  relationship_id, motion_tag, intent_source,
  description, scoring_strategy, surfacing_rule, enabled
) VALUES (
  gen_random_uuid(), '<motion-tag>',
  'paid_specs | preferences | both',
  '<description>',
  '{"scalar_weight": 0.6, "vector_weight": 0.4, "recency_boost": true}',
  '{"channel": "portal | operator_queue | emailbison_handoff", "when": "on_match | daily_digest", "with_context": "<context>"}',
  true
);
```

No code change unless:
- A new scoring algorithm is needed (add a strategy resolver in the matching engine)
- A new surfacing channel is needed (wire the new channel in the delivery layer)

The matching engine is configured by relationship type, not coded per motion.

### 15. Add new market motion (instance N+1)

Repeat steps 1-12. Estimated effort:

| Work | Owner | Estimated time |
|------|-------|---------------|
| Brand registration + emailbison sender setup | Operator | 1 hour |
| Partner acquisition pipeline (sales calls) | Operator | 1-4 weeks (ongoing) |
| Data source onboarding (if new sources needed) | Engineering | < 1 day per source |
| Material attribute declarations | Engineering | 30 minutes |
| Matching relationship config | Engineering | 30 minutes |
| Audience template starters | Engineering + Operator | 2-4 hours |
| **Total engineering** | Engineering | **< 1 day** |

The bottleneck is operator sales capacity, not engineering. Architecture does not gate market expansion.

---

## Reference architecture diagram

```
Data Layer (R2 / Cloudflare Object Storage)
─────────────────────────────────────────────────────────────────
 R2: dex-raw-landing-zone/
 ├── <source-hyphenated>/year=YYYY/part-NNNNN.parquet   ← Parquet (Fuel Tank sources)
 ├── fmcsa-carrier-essentials/                          ← Lance datasets
 ├── sam-opps-active/
 ├── usaspending-contracts/
 ├── polaris-warehouse/fmcsa/carrier_essentials_*/      ← Embeddings Lance
 └── audience-cohort-manifests/YYYY/MM/DD/*.parquet     ← Signed cohort manifests

                ↓ register                     ↓ register (Generic Table API)
Catalog (Apache Polaris 1.4.1 on Railway)
─────────────────────────────────────────────────────────────────
 REST: https://polaris-production-8cba.up.railway.app
 ├── Iceberg tables: sources.fmcsa_carrier_latest, sources.sam_entities, ...
 ├── Lance generic tables: fmcsa.carrier_essentials_lance, fmcsa.carrier_essentials_embeddings_lance, ...
 └── Metadata in Supabase Postgres (polaris_metadata DB)

                ↓ DuckDB-over-Iceberg / DuckDB-over-Lance Arrow bridge
Observability + Freshness (ops.* in DEX Supabase Postgres)
─────────────────────────────────────────────────────────────────
 ops.data_sources (74+ sources registered)
 ops.data_source_slas (per-source freshness contracts)
 ops.data_source_ingest_runs (per-run ledger)
 ops.material_attribute_declarations (per-source declared material attrs)
 ops.material_change_events (append-only detected-change ledger)
 ops.alert_subscriptions + ops.alert_emissions (alerter state)

 Crons:
 ├── alerter_cron_app.py       → every 15 min → Telegram breach alerts
 └── material_change_detection_app.py → every 6h → diff + cohort scan

                ↓ spec evaluator reads catalog; lineage stamped in X-Data-Lineage header
Contract Substrate (business.* in hq-x Supabase Postgres)
─────────────────────────────────────────────────────────────────
 business.organizations (partner orgs)
 business.audience_specs (partner-authored draft + revision chain)
 business.audience_spec_signings (immutable contract artifact + cohort manifest URI)
 business.audience_spec_deliveries (per-entity events: entered_cohort, attribute_changed, viewed, claimed)
 business.cohort_drift_scan_state (high-water mark for Phase 3 scanner)
 business.matching_relationships (per-motion relationship type config)

 REST: apps/hq-x/app/routers/audience_specs_v1.py
 ├── POST /api/v1/audience-specs
 ├── POST /api/v1/audience-specs/{id}/preview  (count + sample + freshness gate)
 ├── POST /api/v1/audience-specs/{id}/sign     (mint cohort manifest → R2)
 └── GET  /api/v1/signings/{id}/replenishment  (live vs signed baseline)

                ↓ compile() → CompiledQuery → DuckDB SQL
Phase 4 Vector Primitives (apps/hq-x/app/services/audience_spec/)
─────────────────────────────────────────────────────────────────
 evaluator.py       — compile/preview/sign/replenishment_status
 vector_query.py    — run_similarity_search (centroid k-NN) + run_semantic_search
 models.py          — AudienceSpec pydantic: SimilarityClause, SemanticPredicate, ...
 catalog.py         — PyIceberg RestCatalog seam (single-file swap when Polaris replaces SqlCatalog)

 Lance IVF-PQ index: cosine metric, warm top-K latency ~85-150ms
 Embedding models:
 ├── sentence-transformers/all-MiniLM-L6-v2 (384-dim, free, current default)
 └── text-embedding-3-small (1536-dim, OpenAI — swap when quota restored)

 Embedding cron: fmcsa_carrier_essentials_embedding_emit_app.py @ 07:45 UTC daily

                ↓ matching relationships config → scorer → surfacing rule
Phase 5 Matching Engine (substrate seeded)
─────────────────────────────────────────────────────────────────
 business.matching_relationships → relationship-typed scorer
 ├── demand_side_fulfillment_paid_spec  — partner's signed spec → entity cohort → portal
 ├── opportunity_matching               — entity's preferences → SAM opps → entity portal
 └── (future: teaming_candidate_match, subcontractor_discovery, ...)

Multi-channel surfacing:
 ├── portal (partner-facing: cohort view, entity cards)
 ├── operator_queue (operator-facing: pending actions, drift alerts)
 └── emailbison handoff (supply-side outbound via emailbison — NOT from this codebase)
```

---

## Appendix: substrate inventory

All components shipped in the 2026-05-12 multi-phase rebuild. Canonical entry points listed.

### Phase 0a — Observability foundation

**What it does:** Per-source freshness SLA registry. Every source declares a contract; continuous measurement against it; breach = operator alert.

| Component | Path |
|-----------|------|
| Postgres schema | `apps/data-engine-x/supabase/migrations/20260512041854_observability_foundation.sql` |
| Ledger service | `apps/data-engine-x/app/services/observability_ledger.py` |
| REST endpoints | `GET /api/v1/observability/sources` (DEX), `GET /api/v1/observability/proxy` (hq-x) |
| Seed script | `apps/data-engine-x/scripts/seed_observability_sources.py` |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-phase-0a-observability-foundation-complete.md` |

**Key tables:** `ops.data_sources`, `ops.data_source_slas`, `ops.data_source_ingest_runs`

**SLA defaults by source type:**

| Source | SLA |
|--------|-----|
| FMCSA carrier/inspection feeds | 24h |
| SAM.gov entities | 24h |
| SAM.gov active opportunities | 12h |
| USAspending daily delta | 24h |
| Polaris catalog health | 24h |
| HMDA LAR, GLEIF, SBA loans | weekly |
| USAspending historical backfill | monthly |

### Phase 0b — Per-API data lineage

**What it does:** Every HTTP response from DEX and hq-x carries an `X-Data-Lineage` header — JSON array of `{table, snapshot_id, format, queried_at}` entries. hq-x merges DEX-side lineage into the union.

| Component | Path |
|-----------|------|
| DEX middleware | `apps/data-engine-x/app/middleware/data_lineage.py` |
| hq-x merge | `apps/hq-x/app/services/dex_client.py` (merges on each DEX call) |
| hq-command display | `apps/hq-command/` (lineage in `<meta name=data-lineage>`) |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-phase-0b-per-api-data-lineage-complete.md` |

### Phase 0c — Alerting + atomic ingest

**What it does:** Telegram breach alerts on SLA miss or ingest failure. Atomic ingest wrapper that guarantees either full-success or full-rollback for every R2 write.

| Component | Path |
|-----------|------|
| Postgres schema | `apps/data-engine-x/supabase/migrations/20260512062000_alerting_substrate.sql` |
| Alerter service | `apps/data-engine-x/app/services/alerter.py` |
| Atomic ingest | `apps/data-engine-x/app/services/atomic_ingest.py` |
| Modal cron | `apps/data-engine-x/modal/alerter_cron_app.py` (schedule: `*/15 * * * *` UTC) |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-phase-0c-alerting-atomic-ingest-complete.md` |

**Key tables:** `ops.alert_subscriptions`, `ops.alert_emissions`

### Polaris catalog standup

**What it does:** Apache Polaris 1.4.1 on Railway as the catalog for Iceberg tables and Lance datasets (via Generic Table API). Supabase Postgres for catalog metadata; R2 for warehouse storage.

| Component | Path / URL |
|-----------|------------|
| Railway service | `polaris` in `hq-all` project, `https://polaris-production-8cba.up.railway.app` |
| Dockerfile | `apps/polaris/Dockerfile` |
| Railway config | `apps/polaris/railway.json` |
| Smoke test | `apps/data-engine-x/scripts/init_polaris_smoke_test.py` |
| Observability seed | `apps/data-engine-x/scripts/seed_polaris_observability_source.py` |
| Health cron | `apps/data-engine-x/modal/polaris_health_check_app.py` (schedule: `0 6 * * *` UTC) |
| Docs | `apps/data-engine-x/docs/polaris-catalog.md` |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-polaris-catalog-standup-complete.md` |

**Doppler secrets (project `hq-all`, config `prd`):** `POLARIS_PUBLIC_URL`, `POLARIS_ROOT_PRINCIPAL_ID`, `POLARIS_ROOT_PRINCIPAL_SECRET`, `POLARIS_DB_URL`, `POLARIS_R2_*`, `POLARIS_DEFAULT_REALM`, `POLARIS_DEFAULT_CATALOG_NAME`, `POLARIS_WAREHOUSE_BASE_LOCATION`

### Lance canary + Wave 1 sweep

**What it does:** Re-emits Parquet sources as Lance datasets for high-throughput vector and tabular scan. Establishes `LanceSourceEmitter` as the reusable emitter pattern.

| Component | Path |
|-----------|------|
| Emitter library | `apps/data-engine-x/scripts/_lib/lance_emit.py` |
| Commit lock | `apps/data-engine-x/scripts/_lib/lance_commit_lock.py` |
| Per-source scripts | `apps/data-engine-x/scripts/run_fmcsa_carrier_essentials_lance_emit.py`, `run_fmcsa_crash_essentials_lance_emit.py`, `run_fmcsa_authhist_essentials_lance_emit.py`, `run_sam_opps_active_lance_emit.py`, `run_usaspending_contracts_lance_emit.py` |
| DuckDB view registry | `apps/data-engine-x/app/services/lance_views.py` |
| Modal crons | `apps/data-engine-x/modal/fmcsa_carrier_essentials_lance_emit_app.py` (06:30 UTC), `fmcsa_crash_*` (06:45), `fmcsa_authhist_*` (06:50), `sam_opps_active_*` (12:30), `usaspending_contracts_*` (07:00 on 16th monthly) |
| Polaris registration | `apps/data-engine-x/scripts/init_polaris_lance_generic.py` |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-lance-sweep-wave-1-complete.md` |

**Active Lance datasets:**

| Dataset | Natural key | Cron |
|---------|------------|------|
| `fmcsa_carrier_essentials` | `dot_number` | 06:30 UTC daily |
| `fmcsa_crash_essentials` | `crash_id` | 06:45 UTC daily |
| `fmcsa_authhist_essentials` | `dot_number + hist_date` | 06:50 UTC daily |
| `sam_opps_active` | `notice_id` | 12:30 UTC daily |
| `usaspending_contracts` | `award_id` | 07:00 UTC 16th monthly |

### Phase 2 — Contract substrate scaffold

**What it does:** The audience spec layer — partner-authored draft specs, immutable signings (the contract), per-entity delivery event ledger. Spec evaluator compiles specs to DuckDB SQL and runs against the catalog.

| Component | Path |
|-----------|------|
| Postgres migration | `apps/hq-x/migrations/20260512T010000_audience_specs_substrate.sql` |
| Pydantic spec models | `apps/hq-x/app/services/audience_spec/models.py` |
| Evaluator | `apps/hq-x/app/services/audience_spec/evaluator.py` |
| Catalog seam | `apps/hq-x/app/services/audience_spec/catalog.py` |
| REST router | `apps/hq-x/app/routers/audience_specs_v1.py` |
| Smoke test | `apps/hq-x/scripts/smoke_audience_specs.py` |
| Docs | `apps/hq-x/docs/audience-specs.md` |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-phase-2-contract-substrate-scaffold-complete.md` |

**Key tables:** `business.audience_specs`, `business.audience_spec_signings`, `business.audience_spec_deliveries`

### Phase 3 — Material-change detection + cohort drift

**What it does:** Declared material attributes per source → daily diff against prior snapshot → change events → cohort drift scan against active signed specs → Telegram alerts to operator.

| Component | Path |
|-----------|------|
| Postgres migration | `apps/data-engine-x/supabase/migrations/20260512080000_material_change_substrate.sql` |
| Detection service | `apps/data-engine-x/app/services/material_change_detector.py` |
| Cohort scanner | `apps/hq-x/app/services/cohort_drift_scanner.py` |
| Modal cron | `apps/data-engine-x/modal/material_change_detection_app.py` (schedule: `0 */6 * * *` UTC) |
| FMCSA declarations seed | `apps/data-engine-x/scripts/seed_material_declarations_fmcsa.py` |
| REST endpoints | `POST /api/v1/material-changes/run-cycle` (DEX), `GET /api/v1/cohort-drift` + `GET /api/v1/cohort-drift/{signing_id}` (hq-x) |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-phase-3-material-change-detection-complete.md` |

**Key tables:** `ops.material_attribute_declarations`, `ops.material_change_events`, `ops.material_detection_runs`, `business.cohort_drift_scan_state`

### Phase 4 — Vector primitives + embeddings

**What it does:** Embedding pipeline for entity profiles. `similar_to` (k-NN centroid over seed entities) and `semantic_match` (free-text query embedding) activate in the spec evaluator. IVF-PQ index on Lance; 85-150ms warm top-K.

| Component | Path |
|-----------|------|
| Embedding library | `apps/data-engine-x/scripts/_lib/embedding_emit.py` |
| FMCSA embed script | `apps/data-engine-x/scripts/run_fmcsa_carrier_essentials_embedding_emit.py` |
| Vector query | `apps/hq-x/app/services/audience_spec/vector_query.py` |
| Spec models update | `apps/hq-x/app/services/audience_spec/models.py` (`SimilarityClause`, `SemanticPredicate`) |
| Evaluator activation | `apps/hq-x/app/services/audience_spec/evaluator.py` (Phase 4 activates vector path) |
| Modal cron | `apps/data-engine-x/modal/fmcsa_carrier_essentials_embedding_emit_app.py` (schedule: `45 7 * * *` UTC) |
| Embeddings observability | `apps/data-engine-x/scripts/seed_carrier_essentials_embeddings_observability_source.py` |
| Docs | `apps/data-engine-x/docs/embeddings-pipeline.md`, `apps/hq-x/docs/vector-spec-primitives.md` |
| Cycle report | `~/Desktop/hq/reports/2026-05-12-scope-hq-all-phase-4-vector-primitives-embeddings-complete.md` |

**Current embedding state:**

| Source | Model | Coverage | Dimension |
|--------|-------|---------|-----------|
| FMCSA carrier_essentials (active, ≥1 power unit) | sentence-transformers/all-MiniLM-L6-v2 | 300K / 1.95M eligible | 384-dim |

Switch to `text-embedding-3-small` (1536-dim) once OpenAI quota is restored: set `EMBEDDING_PROVIDER=openai` in Doppler, truncate the Lance dataset, re-run the cron.

### Phase 5 — Matching engine (substrate seeded; engine implementation follows)

**What it does:** Relationship-typed matcher that produces persistent match objects. Config-driven new relationship types. No code change to add a new relationship type unless a new scoring algorithm or surfacing channel is needed.

| Component | Path |
|-----------|------|
| Relationship config | `business.matching_relationships` table (seeded in Phase 2 migration) |
| Cycle report | Phase 5 engine implementation follows Phase 4 (see Phase 4 report for successor notes) |

---

## GovContracts launch checklist

Quick-reference for motion #2:

- [ ] 1. Domain registered + emailbison sender configured for brand domain
- [ ] 2. `ops.data_sources` — verify SAM entities, SAM opps active, USAspending contracts are present and GREEN
- [ ] 3. `business.organizations` — add operator brand row with `primary_brand` set
- [ ] 4. `ops.material_attribute_declarations` — seed govcontractor material attrs (registration_status, exclusion_status_flag, obligation_amount_ytd, naics_codes)
- [ ] 5. `business.matching_relationships` — seed `demand_side_fulfillment_paid_spec` + `opportunity_matching` rows with `motion_tag='govcontracts'`
- [ ] 6. Audience templates — create starters in `apps/hq-x/data/audience_templates/govcontracts/`
- [ ] 7. SAM opps Lance embeddings — run `run_sam_opps_active_lance_emit.py` + `seed_lance_observability_source.py` for the opps dataset; scope an embedding emit script for SAM opps (same pattern as FMCSA)
- [ ] 8. First partner call — walk partner through the audience composer; draft a spec against SAM + USAspending sources
- [ ] 9. Preview → sign — get count + sample before signing; sign when partner agrees; cohort manifest minted in R2
- [ ] 10. Monitoring — confirm replenishment endpoint returns expected `count_at_signing`; verify alerter fires correctly on first SLA check
