# Handoff — pre-payment pipeline state, 2026-04-30

Purpose: an AI agent dropped into this codebase fresh should be able to read this doc and know (a) what has shipped, (b) what is drafted but not executed, (c) what remains to build, and (d) the architectural decisions already locked. Scope: the owned-brand lead-gen pipeline, with emphasis on the pre-payment / outreach phase. Companion to [STATE_OF_HQ_X.md](../STATE_OF_HQ_X.md) (platform-wide snapshot) and [strategic-direction-owned-brand-leadgen.md](strategic-direction-owned-brand-leadgen.md) (the strategic frame).

This is a working snapshot. The platform is moving fast. Cross-check git log + migration filenames before assuming any specific file still has the shape described here.

---

## 1. Strategic frame (10-second version)

The hq-x platform substrate runs internally for Ben's lead-gen business. External demand-side partners pay 90-day reservations to receive qualified leads produced by hq-x running multi-channel outreach (direct mail + email; voice agent for inbound) under brands Ben owns. Partner pays $25K-class up front, gets warm-transferred leads during their hours of operation. After 90 days, Ben decides whether to renew.

The build target is **throughput**: maximize the number of `(audience-spec → brand → multichannel sequence → live transfer)` initiatives Ben can run in parallel, with the per-initiative operator surface as close to a single command as possible. See [strategic-direction-owned-brand-leadgen.md](strategic-direction-owned-brand-leadgen.md) for the full frame.

---

## 2. Pipeline architecture, end-to-end

```
PRE-PAYMENT (Phase 0–2)                    POST-PAYMENT (Phase 3–5)
─────────────────────                      ───────────────────────
target_accounts                            gtm_initiatives row created
  ↓ Claygent / Clay enrichment             (brand × partner × contract × audience_spec)
  research_blob populated                    ↓
  ↓ outreach_brief generator (LLM)         strategic context research (Exa)
  outreach_brief.markdown                    ↓
  ↓                                        campaign strategy synthesis
  ┌─── audience slicer (LLM + dex-mcp)     (managed-agent — subagent 2)
  │      ↓                                   ↓
  │      audience_slice_candidates         channel_campaigns + channel_campaign_steps materialized
  │      ↓                                   ↓
  │    audience-fit scorer (LLM)           recipients materialized + memberships created
  │      ↓                                   ↓
  └──→ ranked slices                       per-recipient creative generated
       ↓                                     ↓
       brand factory (LLM)                  Lob Print & Mail per-piece activation
         ├─ gestalt                         (already shipped)
         ├─ fit-check existing brands         ↓
         └─ reuse OR instantiate brand     EmailBison email sequence
       ↓                                     ↓
   outreach email sent                     Vapi voice-agent inbound
   (mentions brand + audience pitch)         ↓
       ↓                                   live transfer to partner
   sales call / partner agrees             (90-day window)
       ↓
   contract signed → enters POST-PAYMENT
```

Two distinct halves. **Pre-payment** is reactive, JIT, runs every time Ben prepares a new outreach. **Post-payment** is strict orchestration, runs once per signed contract.

---

## 3. What has shipped to `main`

Most-recent-first. Cross-check via `git log --oneline` for ground truth.

### 3.1 Per-piece direct-mail activation (PR #71, commit `063120a`)

The `app/services/print_mail_activation.py` primitive: `activate_pieces_batch` takes a list of `PieceSpec`s (discriminated union over postcard / self_mailer / letter / snap_pack / booklet) and emits one `POST /v1/postcards|/letters|/self_mailers|/snap_packs|/booklets` per recipient — Lob Print & Mail per piece. Per-piece isolation, idempotency via existing `lob_client.build_idempotency_material`, suppression-list pre-check, persistence via `direct_mail.persistence.upsert_piece(provider_slug='lob')`.

This is the substrate for owned-brand per-recipient bespoke creative — **not** Lob's Campaigns API. The Campaigns API path (`app/services/dmaas_campaign_activation.py`) still exists for non-owned-brand DMaaS use cases but is not used by owned-brand initiatives.

Includes [docs/research/postgrid-print-mail-api-notes.md](research/postgrid-print-mail-api-notes.md) (research only, no PostGrid client built) and [docs/research/canonical-piece-event-taxonomy.md](research/canonical-piece-event-taxonomy.md) (canonical `piece.*` event vocabulary audit; conclusion: vocabulary is sufficient for PostGrid drop-in as-is, with one optional cosmetic rename).

### 3.2 GTM-initiative pipeline schema (commit before #71)

Migration [migrations/20260430T235220_gtm_initiatives.sql](../migrations/20260430T235220_gtm_initiatives.sql) added three tables (post-payment surface):

- **`business.demand_side_partners`** — partner identity (name, domain, primary contact, phone, hours_of_operation_config, intro_email)
- **`business.partner_contracts`** — pricing model (`flat_90d` | `per_lead` | `residual_pct` | `hybrid`), amount_cents, duration_days, max_capital_outlay_cents, qualification_rules JSONB, terms_blob, status enum
- **`business.gtm_initiatives`** — central coupling: brand_id × partner_id × partner_contract_id × `data_engine_audience_id` (DEX cross-DB ref). Status state machine includes `awaiting_strategic_research`, `strategic_research_ready`, `awaiting_strategy_synthesis`, `strategy_ready`, `materializing`, `ready_to_launch`, `active`, `completed`. Has pointers to partner_research_ref (exa.exa_calls row) + strategic_context_research_ref (exa.exa_calls) + campaign_strategy_path (disk path).

These were shipped by a parallel agent. Use them as the post-payment substrate. The pre-payment counterpart (`target_accounts`) is **not yet built** — see §5.

### 3.3 Brand content storage (commit `20260501T010000_brand_content.sql`)

Migration [migrations/20260501T010000_brand_content.sql](../migrations/20260501T010000_brand_content.sql) added **`business.brand_content`** — disk + DB mirror pattern for brand bundles, keyed by `(brand_id, content_key)`. Source of truth: on-disk markdown tree at `data/brands/<slug>/*.md` (positioning.md, voice.md, audience-pain.md, capital-types.md, creative-directives.md, industries.md, proof-and-credibility.md, value-props.md, README.md, brand.json). DB is a queryable copy synced via `scripts/sync_brand_content.py`.

This **resolves §9.4 of the strategic doc** (brand-context storage decision). The earlier proposal of `business.brand_context_documents` (single-doc + version-rows) is **superseded** — use the multi-doc bundle pattern.

### 3.4 Exa research orchestration (PR #69)

`POST /api/v1/exa/jobs` async-202 surface, Trigger.dev task `exa.process_research_job`, raw archive in `exa.exa_calls` with destination flag `hqx | dex`. See [docs/dmaas-orchestration-pr-notes.md](dmaas-orchestration-pr-notes.md) for the orchestration pattern; [scripts/seed_exa_research_demo.py](../scripts/seed_exa_research_demo.py) is the smoke gate. Used post-payment for partner research / strategic context research.

### 3.5 hq-x ↔ DEX audience-spec reservations (PR #68)

Migration `20260430T220819_org_audience_reservations.sql` added `business.org_audience_reservations` — couples a paying `business.organizations` row to a frozen DEX `ops.audience_specs` row. The DEX spec id IS the `data_engine_audience_id` (no second identifier minted). Used by `gtm_initiatives.data_engine_audience_id`.

### 3.6 Earlier substrate (already shipped, see [STATE_OF_HQ_X.md](../STATE_OF_HQ_X.md))

DMaaS scaffolds + designs + solver + MCP, multi-step scheduler with Trigger.dev `wait.for(delay_days)`, Lob/EmailBison/Vapi adapters, hosted landing pages, customer webhook subscriptions, reconciliation crons (stale jobs / Lob piece reconciliation / Dub click drift / webhook replay / customer webhook deliveries).

---

## 4. Drafted directives (NOT yet executed — sitting in this worktree)

### 4.1 [docs/directives/brand-factory-jit.md](directives/brand-factory-jit.md) — superseded, needs revision

The original brand factory directive proposed `business.brand_context_documents` (single-doc + version-rows). **That table doesn't exist and shouldn't be built** — the parallel agent shipped `business.brand_content` instead, which is the disk + DB multi-doc bundle pattern.

This directive needs to be **rewritten** to:
- Drop `brand_context_documents` (use `brand_content` instead)
- Brand instantiation produces a *bundle* of files under `data/brands/<slug>/` then syncs via `scripts/sync_brand_content.py`
- Add the new pre-step: `outreach_brief` generation (from `target_accounts.research_blob`) — see §5
- Adjust the LLM primitive contract: instantiation produces multiple markdown files (positioning, voice, audience-pain, etc.), not one BrandContext doc

Don't execute this directive as-written. Rewrite first.

### 4.2 [docs/directives/dex-mcp-audience-slicer-prep.md](directives/dex-mcp-audience-slicer-prep.md) — ready to execute

Three deliverables in one commit, against the `data-engine-x` repo:

1. **MCP tool expansion** — ~25–35 new tools wrapping FMCSA carriers (search + variants), FMCSA pre-canned audiences (new-entrants-90d, authority-grants, insurance-lapses, high-risk-safety, insurance-renewal-window, recent-revocations), generic audiences resolution (criteria-schema / resolve / count / entity-lookup), audience templates + specs read surface, and entities. Read-only — no mutating endpoints.

2. **`DEX_OVERVIEW.md`** — strategic-guidance document for LLM agents. Covers what each dataset is, attributes sliceable per dataset, when to use pre-canned audiences vs raw filters, when to count vs enumerate, how to combine via entity resolution. ~600–1000 lines. Retrievable in one MCP call via new `dex_overview()` tool.

3. **`AUDIENCE_SLICE_SCHEMA.md`** — locked JSON schema for the audience-slicer agent's output (slice_name, dataset, must_be_true_filters, weighted_signals, disqualifiers, estimated_total_size, size_breakdown_by_filter, audience_template_slug, outreach_brief_pain_refs, confidence, rationale). Retrievable via `audience_slice_schema()` tool.

Execute against `data-engine-x`. Note: a sibling NYC-audiences MCP expansion exists in worktree `awesome-diffie-89ccdd` — do not duplicate; this directive covers FMCSA + audiences + entities only.

### 4.3 [docs/prompts/claygent-target-account-research.md](prompts/claygent-target-account-research.md) — operational, not a build directive

Reusable Claygent prompt for pre-outreach target-account research. Output is a 6-section markdown document (Identity / What they offer / Audiences they serve [with per-audience pain→proxy mapping] / Where they're growing / Brand classification keys / Confidence). Dual-consumer (brand factory + audience slicer). Replace `<<TARGET_URL>>` on line 1 of the prompt block to reuse for any prospect.

Sample test data already populated at [data-test/](../data-test/):
- `dat-clay-company-enrichment.json` — Clay firmographic enrichment for DAT
- `dat-claygent-1.json`, `dat-claygent-2.json` — two Claygent runs

Use these as fixture inputs for the outreach_brief generator's first test run.

---

## 5. What remains to build (priority order)

### 5.1 PREREQUISITE — execute the dex-mcp directive (§4.2)

Before any agent can validate that the outreach_brief shape is sufficient for slicing, dex-mcp needs FMCSA + audiences + entities tools. This is the smallest unblock. Not building this first means the slicer agent can only see DealBridge data, which isn't what most outreach briefs target.

### 5.2 `business.target_accounts` table + `outreach_brief` columns

New migration. Schema sketch (subject to revision):

```sql
CREATE TABLE business.target_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id),
    domain TEXT NOT NULL,
    company_name TEXT NOT NULL,
    research_blob JSONB NOT NULL,                   -- raw Claygent / Clay output
    research_source TEXT NOT NULL,                   -- 'claygent' | 'clay_enrichment' | 'manual'
    -- The fused canonical brief, generated from research_blob via LLM:
    outreach_brief_markdown TEXT,
    outreach_brief_keys JSONB,                       -- extracted top-level keys (vertical, core_need, voice_register, confidence)
    brief_generated_at TIMESTAMPTZ,
    brief_model TEXT,
    notes TEXT,
    created_by_user_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, domain)
);
```

`target_accounts` is the **pre-payment** counterpart to `demand_side_partners`. A `target_account` becomes a `demand_side_partner` only after payment. **Do not collapse these tables.** Different lifecycle stages.

### 5.3 outreach_brief generator (LLM service)

Service that takes a `target_account_id` (with research_blob populated) and produces the canonical 6-section markdown brief. Uses the Anthropic Messages API. Idempotent — re-running with newer research updates the brief.

The brief structure is locked:
1. Identity
2. What they offer (in detail — products with what-it-does / natural-customer / pain-resolved / audience-signal per offering)
3. Audiences they serve — **separate sub-block per audience type** (primary / secondary / tertiary), each with archetype, pain → observable proxies (with **trigger** field per pain row — concrete event / status change / data-state observable in DB / dex), DAT products this audience uses, growth signal
4. Where they're growing
5. Brand classification keys (`vertical`, `core_need_category`, `voice_register`)
6. Confidence: high | medium | low + gaps

Each pain row needs a `signal` / `trigger` field (e.g. "MC# went active in last 30 days", "policy expiration date within 90 days"). The audience-slicer reads each trigger and asks "does our DB have anyone for whom this fired recently?"

### 5.4 Inline validation loop — does the brief work?

Before building the audience slicer as a managed-agent: run the validation loop inline. Use Claude Code or any MCP-aware client. Steps:

1. Drop dex-mcp tools loaded.
2. Paste an outreach brief into context (use one generated from `data-test/dat-claygent-*.json` via the brief generator from §5.3, or hand-author a brief from the DAT example in this conversation).
3. Call `dex_overview()` and `audience_slice_schema()` to load the strategic guidance.
4. Ask the agent: "produce 5–6 candidate audience slices for this outreach brief."
5. Verify the slices validate against the schema.
6. Iterate the brief shape if slices are shallow; iterate the system prompt if the brief is fine but the agent flails.

**This validation is the gate before §5.5.** No point building a managed-agent for the slicer until the brief shape is proven sufficient.

### 5.5 Audience slicer — codify into a managed-agent

Once the inline loop converges, codify into managed-agents-x:

- System prompt: load `dex_overview` markdown + `audience_slice_schema` JSON
- Task instruction: "Read `business.target_accounts.id={id}`'s outreach_brief_markdown. Produce 5–6 candidate slices in the schema. Persist to `business.audience_slice_candidates`."
- New table `business.audience_slice_candidates` (target_account_id, slice payload, score, status)
- Trigger.dev task wrapping the agent invocation

### 5.6 Audience-fit scorer

Separate LLM call. Takes the slate of 5–6 candidates from the slicer and ranks them for fit against the target account's specific positioning. May be combinable with the slicer in one prompt — TBD during validation. The scorer output orders the candidates so Ben can show up to a sales meeting with 1–6 ranked options.

### 5.7 Brand factory — rewritten on top of `brand_content`

Three sequential LLM stages, JIT-triggered:

1. **Gestalt** — derive pairing-shape `(audience_archetype, need_pain_framing, voice_register, firmographic_vertical, core_need_category)` from `(audience_spec_descriptor, target_account.outreach_brief)`.
2. **Fit-check** — given pairing-shape and existing brands' pairing-shapes, decide reuse-vs-instantiate. Hallucination guard rejects an LLM-returned brand_id not in the candidate list.
3. **Instantiate** (only if fit-check returned `instantiate`) — generate the full brand bundle: `data/brands/<slug>/positioning.md`, `voice.md`, `audience-pain.md`, `capital-types.md` (or whatever the brand's core-need analog is), `creative-directives.md`, `industries.md`, `proof-and-credibility.md`, `value-props.md`, `README.md`, `brand.json`. Write to disk, sync via `scripts/sync_brand_content.py`.

Async-202 surface: `POST /api/v1/brands/factory` taking `(audience_spec_id, target_account_id)` → returns job_id. Trigger.dev task drives the work. Result: `(decision, result_brand_id)`.

Capital Expansion is the first concrete brand. Reverse-engineer from the live site (`capitalexpansion.com`); place under `data/brands/capital-expansion/` and sync.

Models: Sonnet 4.6 for gestalt + fit-check (analytical), Opus 4.7 for instantiation (voice-laden). Both env-configurable.

### 5.8 GTM-initiative wiring (post-payment integration)

Once `target_accounts` flows into `demand_side_partners` on payment, and `audience_slice_candidates` flows into a chosen `data_engine_audience_id`, and brand factory has produced a `brand_id` — the existing `business.gtm_initiatives` row gets created with all four FK pointers set. The state machine then drives the post-payment work (strategic context research, campaign strategy synthesis, channel campaign materialization).

This wiring layer is **mostly already designed in the gtm_initiatives state-machine schema**. The work is connecting the pre-payment outputs to the gtm_initiatives row at contract close.

### 5.9 Per-recipient creative generator

Downstream of brand factory + audience slicer. Generates bespoke HTML/PDF per recipient × per direct-mail step. Validates against the dmaas zone-binding / MediaBox invariants. Consumes `brand_content` (voice, positioning, audience-pain, creative-directives) + recipient data points from DEX. Produces per-piece `front`/`back` HTML strings or PDF URLs for `activate_pieces_batch`.

This is the §4 first-priority Add from the strategic doc. Has upstream deps (brand factory + audience slicer). Don't start until those land.

### 5.10 Marketing-site bootstrap pipeline

Domain registration (Entri), Vercel deploy, brand-themed marketing site rendering off `brand_content`, landing-page-config wiring, Dub link host. Built on top of brand factory.

### 5.11 Voice-agent persona

Vapi assistant per gtm_initiative (Ben's stated preference). Persona inherits from `brand_content` voice files. Routing manifest inherits qualification_rules + hours_of_operation from partner_contract. Recipient-lookup-by-code (DOT# / BBL / etc.).

### 5.12 Launch-readiness predicate + `ready_to_launch` state

Predicate over (DNS verified + voice agent healthy + partner phone verified + all step-1 creative passes invariants). When predicate flips true, gtm_initiatives.status moves `materializing` → `ready_to_launch`. Operator triggers launch from there.

---

## 6. Locked architectural decisions

These are settled. Don't relitigate without a strong reason.

| Decision | Resolution | Where |
|---|---|---|
| Lob path for owned-brand initiatives | **Print & Mail per piece**, not Campaigns API | PR #71 |
| Provider abstraction across direct-mail providers | **Not built ahead of demand**. Lob-only adapter; canonical event taxonomy is the only boundary worth getting right up-front. PostGrid notes exist for when needed. | PR #71 + research notes |
| Brand-context storage (§9.4) | **`business.brand_content` disk + DB mirror, multi-doc bundle**. Source of truth: `data/brands/<slug>/*.md`. NOT `brand_context_documents`. | Migration `20260501T010000_brand_content.sql` |
| Brand creation trigger | **JIT — at outreach prep time**, not speculative-pre-stage. | Conversation 2026-04-30 |
| Brand keying unit | **(audience archetype, need/pain framing, voice register)**. Lightweight classification keys: `(vertical, core_need_category, voice_register)`. Brands are reusable across many `(audience_spec, partner_contract)` instances when pairing-shape fits. | Conversation 2026-04-30 |
| `target_accounts` vs `demand_side_partners` | **Separate tables, separate lifecycles**. Pre-payment vs post-payment. | This doc §5.2 |
| Intermediate `outreach_brief` artifact | **Yes** — fused, canonical, dual-consumer (brand factory + audience slicer). Decouples downstream agents from research-blob shape churn. Stored on `target_accounts`. | Conversation 2026-04-30 |
| Audience slicer scope | **Pre-canned audiences preferred over raw filters** (slicer checks `dex_audience_templates_list` early). Each slice must be sized via dex-mcp count call. Each slice must be traceable to a brief pain-row. | dex-mcp directive §D |
| LLM provider for hq-x | **Anthropic Messages API direct**, not Managed Agents API. Sonnet 4.6 analytical, Opus 4.7 generative. | Brand factory directive (to-be-rewritten) |
| Outreach brief structure | **Six sections, per-audience-type sub-blocks**, each pain row carries a `trigger` / `signal` field. | Conversation 2026-04-30 |

---

## 7. Locked output schemas / contracts

These are interface contracts. Changing them requires touching every consumer.

### 7.1 `PieceSpec` (5-type discriminated union)

Lives in `app/services/print_mail_activation.py`. Variants: `PostcardSpec`, `SelfMailerSpec`, `LetterSpec`, `SnapPackSpec`, `BookletSpec`. Each carries the right artwork field(s) per Lob's API shape (postcard `front`+`back`, self_mailer `inside`+`outside`, snap_pack `inside`+`outside` at 8.5x11, letter single `file`, booklet single multipage `file`). `extra=forbid` per Pydantic. Per-piece `idempotency_seed` is required.

### 7.2 Canonical `piece.*` event vocabulary

Lives in `app/webhooks/lob_normalization.py`. Provider-neutral. Audited in [docs/research/canonical-piece-event-taxonomy.md](research/canonical-piece-event-taxonomy.md). Sufficient for PostGrid drop-in as-is; one optional cosmetic rename (`piece.rendered_pdf` + `piece.rendered_thumbnails` → `piece.rendered`) flagged for when PostGrid lands.

### 7.3 Audience slice JSON schema

Locked in `data-engine-x/app/mcp_server/AUDIENCE_SLICE_SCHEMA.md` (after the dex-mcp directive executes). Required fields: `slice_name`, `slice_description`, `dataset` (enum), `must_be_true_filters` (each: attribute / operator / value / rationale), `weighted_signals`, `disqualifiers`, `estimated_total_size` (must be derived from a real dex-mcp count tool call), `size_breakdown_by_filter`, `audience_template_slug` (if pre-canned audience covers it), `outreach_brief_pain_refs` (≥1 required), `confidence`, `rationale`. Validation rules: zero-size → reject; >500K → flag; no traceable brief pain → reject.

### 7.4 outreach_brief markdown shape

Six-section canonical structure. Per-audience-type sub-blocks. Each pain row has a `trigger` field. See [docs/prompts/claygent-target-account-research.md](prompts/claygent-target-account-research.md) field guide for what consumers read.

### 7.5 `business.brand_content` row shape

Per [migrations/20260501T010000_brand_content.sql](../migrations/20260501T010000_brand_content.sql). Keyed `(brand_id, content_key)`. Content keys: `positioning`, `voice`, `audience-pain`, `capital-types`, `creative-directives`, `industries`, `proof-and-credibility`, `value-props`, `README`, `brand` (for brand.json). Source of truth: disk. DB is mirror.

---

## 8. Open architectural decisions

These are still on Ben's plate. An agent should not unilaterally resolve any of these.

| Decision | Where it bites | Default if unspecified |
|---|---|---|
| Slice-and-fit one prompt or two | Audience slicer + audience-fit scorer | Two — easier to debug |
| Where `audience_slice_candidates` rows live (hq-x table vs ephemeral artifact in gtm_initiatives.metadata) | Audience slicer codification | New hq-x table |
| Whether brand factory's instantiation should produce all 9 markdown files or a minimum subset | Brand factory directive | All 9 — match existing brand_content keys |
| Earmark expiry policy + reservation conflict resolution | When prospect A is earmarked an audience and prospect B comes through 3 days later for an overlapping spec | Per §9.3 of strategic doc — Ben's call |
| Voice agent scope (per-initiative vs per-brand) | Phase 4 step 6 | Per-initiative (Ben's stated preference per §5 strategic doc) |
| Per-recipient creative gen sync vs async during instantiation | Brand factory ↔ creative gen handoff timing | Async — instantiation returns when structural tree exists, creative gen runs to completion separately, launch predicate gates final flip |

---

## 9. Where to look (file-reference index)

### Strategic context
- [docs/strategic-direction-owned-brand-leadgen.md](strategic-direction-owned-brand-leadgen.md) — full strategic frame (§3 brand, §4 per-recipient creative, §5 lifecycle, §7 priorities, §8 new objects, §9 open decisions)
- [STATE_OF_HQ_X.md](../STATE_OF_HQ_X.md) — platform-wide snapshot

### Active directives
- [docs/directives/brand-factory-jit.md](directives/brand-factory-jit.md) — **needs rewrite** before execution (uses obsolete `brand_context_documents`)
- [docs/directives/dex-mcp-audience-slicer-prep.md](directives/dex-mcp-audience-slicer-prep.md) — **ready to execute** against `data-engine-x`

### Operational artifacts
- [docs/prompts/claygent-target-account-research.md](prompts/claygent-target-account-research.md) — Claygent prompt template, reusable per-target-account
- [data-test/dat-claygent-1.json](../data-test/dat-claygent-1.json), [dat-claygent-2.json](../data-test/dat-claygent-2.json), [dat-clay-company-enrichment.json](../data-test/dat-clay-company-enrichment.json) — test fixtures for the outreach_brief generator

### Recent migrations to know about
- `20260430T235220_gtm_initiatives.sql` — demand_side_partners + partner_contracts + gtm_initiatives
- `20260430T234909_direct_mail_pieces_metadata_gin.sql` — GIN index for back-references on direct_mail_pieces
- `20260430T222800_exa_research_jobs.sql` — exa orchestration table
- `20260430T220819_org_audience_reservations.sql` — hq-x ↔ DEX reservation tie
- `20260501T010000_brand_content.sql` — brand_content multi-doc bundle table

### Service layer
- `app/services/print_mail_activation.py` — per-piece direct mail (5 types)
- `app/services/exa_research_jobs.py` — exa orchestration
- `app/services/exa_client.py` — exa HTTP client
- `app/services/activation_jobs.py` — generic orchestration-job pattern (mirror this for new orchestration tables)
- `app/services/dex_client.py` — DEX HTTP client (reservation + audience descriptor)

### Provider clients
- `app/providers/lob/client.py` — Lob Print & Mail (all 5 types) + idempotency helper
- `app/webhooks/lob_normalization.py` — canonical `piece.*` event taxonomy + status mapping
- `managed-agents-x/app/anthropic_client.py` — pattern reference for httpx-based Anthropic API client (Managed Agents API, NOT Messages API)

### dex-mcp
- `data-engine-x/app/mcp_server/dex_server.py` — current 7 tools (DealBridge only)
- `data-engine-x/app/mcp_server/README.md` — server overview + "intentionally not here" rule
- After the dex-mcp directive lands: `DEX_OVERVIEW.md` + `AUDIENCE_SLICE_SCHEMA.md` in the same directory

---

## 10. The shortest path forward

If you're an agent picking up this work cold, the single most leveraged thing you can do next:

1. **Execute the dex-mcp directive (§4.2)** against `data-engine-x`. ~25–35 new MCP tools + DEX overview doc + locked slice schema. Single commit. ~1–2 hours of agent work.
2. After that lands, **run the inline validation loop (§5.4)** in any Claude-Code-like client with dex-mcp loaded: paste a hand-authored outreach brief in, ask for 5–6 audience slices, see if they validate. This is the gate. If slices are good, the brief shape is sufficient and you can confidently build the upstream brief generator. If not, tune the brief shape and re-run.
3. Only then build §5.2 (`target_accounts` table + outreach_brief columns) and §5.3 (brief generator).
4. Then §5.5 codifies the audience slicer into a managed-agent.
5. The brand factory rewrite (§5.7) can run in parallel with §5.5 since both consume `outreach_brief` independently.

Don't start with the brand factory. Don't start with the per-recipient creative generator. Don't start with marketing site bootstrap. Each of those has uncovered upstream dependencies that will whiplash if validated late.

The validation gate (§5.4) is the single most important step. Everything else is downstream of "is the outreach brief sufficient?"
