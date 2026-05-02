# State of hq-x — 2026-05-01

A snapshot of what the platform is, what's been built, what works end-to-end today, and what's next. Audience: future you, future agents, future collaborators picking this up cold. **Supersedes the 2026-04-30 snapshot, which framed hq-x as self-serve DMaaS — that framing is dead.**

---

## 1. What hq-x is

The internal platform substrate for an **owned-brand lead-gen business**. Operator owns the platform. External companies (factoring co's, insurance agencies, wholesale RE, etc.) become **demand-side partners** who pay for 90-day exclusive flow of qualified leads produced by hq-x running outreach **under brands the operator owns**, against audiences sliced from data-engine-x.

Old framing (DMaaS-as-customer-product) is **dormant**. Customer-webhook subscriptions still work but are no longer a public API. The DMaaS Campaigns API send path is preserved for legacy rows; new owned-brand work uses the per-piece Print & Mail path.

The value-prop sentence (internal): **"A demand-side partner pays. The pipeline materializes channels + audience + creative + voice agent under one of our brands. Recipients get a touch sequence; live transfers route to the partner via the AI agent."**

Read first for the business model: [`docs/strategic-direction-owned-brand-leadgen.md`](docs/strategic-direction-owned-brand-leadgen.md).

---

## 2. What's been built (by capability, not chronology)

### 2.1 The post-payment GTM pipeline (the centerpiece)

The headline workstream. A demand-side partner pays → a `business.gtm_initiatives` row gets created → a multi-step actor+verdict pipeline runs to produce every artifact needed for outreach: channel/step plan, audience materialization, master strategy, per-recipient creative.

**Runtime:** Anthropic Managed Agents API (MAGS) via `managed-agents-x`'s registered agents. Each subagent is a separately-registered Anthropic agent. Trigger.dev sequences the actor → verdict loop. hq-x is the only seam between Trigger.dev and Anthropic.

**Pipeline (5 actor+verdict pairs = 10 MAGS agents):**

```
gtm-sequence-definer            → economics-aware channel + touch plan (JSON)
gtm-channel-step-materializer   → JSON plan; hq-x writes campaigns/channel_campaigns/steps
gtm-audience-materializer       → JSON plan; hq-x pages DEX audience, upserts
                                  recipients + memberships + manifest, mints Dub links
gtm-master-strategist           → Master Strategy markdown (per-touch frames, NOT copy)
gtm-per-recipient-creative      → per-piece copy + design DSL JSON, fanned out
                                  per (recipient × DM step) via Trigger.dev batchTrigger
```

Verdict (paired with each actor): returns strict `{ship: bool, issues: [...], redo_with: string|null}`. Verdict-block triggers actor retry with hint (one retry budget in v0). Pipeline fails cleanly at `verdict_block_after_retries` if a verdict can't be satisfied.

**Run capture is the spine.** Every actor + verdict invocation writes a `business.gtm_subagent_runs` row capturing input, output, prompt snapshot, mcp_calls trace, anthropic_session_id, cost. Frontend reads from this table.

**Prompt versioning.** Snapshot-then-overwrite — every activate writes two rows to `business.agent_prompt_versions` (old state + new state). Anthropic holds the live prompt; DB is the durable history with full rollback.

Read for operational detail: [`docs/handoff-gtm-pipeline-foundation-2026-05-01.md`](docs/handoff-gtm-pipeline-foundation-2026-05-01.md). Specs that produced this: [`docs/directives/gtm-pipeline-foundation.md`](docs/directives/gtm-pipeline-foundation.md), [`docs/directives/gtm-pipeline-materializer.md`](docs/directives/gtm-pipeline-materializer.md), [`docs/directives/gtm-initiative-attribution.md`](docs/directives/gtm-initiative-attribution.md).

### 2.2 Frontend command center (hq-command repo)

`https://app.opsinternal.com` (Railway-deployed Next.js). Three admin surfaces:

- **`/admin/initiatives`** — list of initiatives + per-initiative drilldown showing every actor + verdict run with input / output / prompt-snapshot / mcp-calls / error-blob. Polls every 3s while pipeline_status='running'. Per-step "Rerun from here" button.
- **`/admin/agents`** — registry list + `/admin/agents/[slug]` prompt editor with Activate (writes two version rows) and per-version Rollback.
- **`/admin/doctrine`** — single-page editor for the operator-org doctrine markdown body + parameters JSON.

All proxied through hq-x backend (`/api/v1/admin/*`); no MAGS keys in browser.

Out of scope for the v0 admin: fanout aggregate view (renders 5000+ per-recipient runs as a flat list today). Backend has `runs/aggregated` endpoint shipped; frontend catches up in a follow-up hq-command directive.

### 2.3 The campaigns hierarchy (still load-bearing)

```
business.organizations
  └── business.brands
        └── business.campaigns                        (initiative_id NULL=legacy, set=owned-brand)
              └── business.channel_campaigns          (one per channel × provider; carries initiative_id)
                    └── business.channel_campaign_steps   (ordered touches; 1:1 with provider primitive)
                          ├── business.channel_campaign_step_recipients   (audience + status)
                          └── per-recipient artifact rows (direct_mail_pieces, dmaas_dub_links, etc.)

business.recipients ◄── channel-agnostic identity (org-scoped)
business.gtm_initiatives ─── parents campaigns 1:many for owned-brand work
business.initiative_recipient_memberships ─── manifest of "what was paid for" per (initiative, recipient)
```

Every analytics event carries `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel, provider, initiative_id)`. Enforced in [`app/services/analytics.py:emit_event`](app/services/analytics.py) — the chokepoint that no emit site bypasses. `initiative_id` was added by PR #81 (the attribution slice).

Canonical reference for the older five-layer model: [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md). Read it for the foundational concept of channel × provider × step.

### 2.4 Per-piece direct-mail submission (Lob Print & Mail)

The **active path for owned-brand initiatives.** Each recipient's piece is one Lob `POST /v1/{postcards|letters|self_mailers|snap_packs|booklets}` call carrying that recipient's bespoke creative. Bypasses Lob's Campaigns API entirely (which only allows one shared creative per campaign). Independent and additive — the Campaigns API `LobAdapter.activate_step` path remains for legacy DMaaS rows.

Substrate: [`app/services/print_mail_activation.py`](app/services/print_mail_activation.py) (PR #71). Per-piece isolation: a failure on piece N never aborts the batch. Discriminated-union `PieceSpec` (one Pydantic class per Lob type, `extra='forbid'`) catches cross-type field-shape misuse at construction time. `direct_mail_pieces` rows carry `_recipient_id` / `_channel_campaign_step_id` / `_membership_id` back-references in metadata.

Provider abstraction is intentionally absent today; PostGrid is documented in `docs/research/postgrid-print-mail-api-notes.md` for when it lands.

### 2.5 Hosted landing pages on owned-brand domains

Same Entri Power + Dub custom-domain plumbing as before, just on owned brands now. `pages.<brand>.com/lp/<step>/<short_code>` proxies to backend; render uses brand theme + step `landing_page_config` + recipient personalization. Honeypot, per-IP rate-limit dedup, IP hashing.

What changes for the new model (per the independent-brand doctrine): channel-tier framing rules per surface — postcards/letters never carry partner-bridge talk; landing pages are where the partner-bridge CTA lives; voice agents are explicit about partner routing. Per-recipient personalization deepens beyond `{recipient.display_name}` once per-recipient creative starts emitting per-recipient landing-page copy.

Render: [`app/routers/landing_pages.py`](app/routers/landing_pages.py).

### 2.6 Voice (inbound)

Recipients call the AI agent's number printed on direct mail / surfaced on landing page. The AI agent qualifies and live-transfers to the partner per `partner_contracts.qualification_rules` + `demand_side_partners.primary_phone`. **Voice in the new model is inbound only** — outbound calls aren't part of the active GTM loop.

Substrate is built — `voice_assistants` table holds Vapi assistant config (system_prompt + model_config + voice_config + tools_config + analysis_config), `voice_phone_numbers` maps phone ↔ assistant. Service code at `app/services/voice_*` and provider client at `app/providers/vapi/`.

What's NOT built yet: the `gtm-voice-agent-instantiator` subagent that mints a Vapi assistant per initiative from brand context + partner contract. Slot exists in the pipeline diagram; agent + integration is a follow-up directive (parallel to render-and-submit).

### 2.7 Event fan-out

Every `emit_event()` call carries the canonical eight-tuple (six-tuple + initiative_id + provider) and fans out to:

1. **Stdout logs** (always)
2. **ClickHouse** (no-op today; cluster intentionally not provisioned)
3. **RudderStack** — wired to source `hq-x-server`; destinations now point at the operator's analytics stack (was customer-warehouse-pluggable, now operator-only)
4. **Customer webhook subscriptions** — code still works but **no longer a public API**. Internal-only post-pivot. Available if the operator wants to wire Slack alerts off events.

Chokepoint enforced in [`app/services/analytics.py`](app/services/analytics.py). New emit sites must go through it.

### 2.8 Async orchestration via Trigger.dev

Two primary task surfaces:

**GTM-pipeline orchestrator** (post-payment):
- `gtm.run-initiative-pipeline` — main workflow; sequences actor/verdict pairs; routes per-recipient fanout to a child task
- `gtm.run-per-recipient-creative` — child task batched via `batchTrigger` over (recipient × DM step) tuples; concurrency capped at 50

**Legacy DMaaS orchestration** (still live for legacy rows):
- `dmaas.process-activation-job` (DMaaS Campaigns API path)
- `dmaas.scheduled-step-activation` (multi-step scheduler — `wait.for(N days)`)
- 5 reconciliation crons (each Doppler-flag-gated): stale jobs, Lob piece reconciliation, Dub click drift, webhook replay backstop, customer webhook delivery retries

**TS task layer.** TS files in [`src/trigger/`](src/trigger) are thin shims that call hq-x `/internal/*` endpoints via [`src/trigger/lib/hqx-client.ts`](src/trigger/lib/hqx-client.ts) using `TRIGGER_SHARED_SECRET`. All real business logic stays in Python. Anthropic invocation is server-side in hq-x, never in TS.

### 2.9 Multi-tenancy, auth, identity

- **Organizations** as the top tenant; **brands** as the customer-facing identity. The platform org (`acq-eng`) owns the active brands; `business.demand_side_partners` rows model the paying partners separately.
- **Recipients** are channel-agnostic identities, **strictly org-scoped**. Natural-keyed by `(organization_id, external_source, external_id)`.
- **Auth flavors** — same as before:
  - Customer-facing endpoints: Supabase ES256 JWT verified via JWKS; org context via `X-Organization-Id` header.
  - Internal Trigger.dev callbacks: `TRIGGER_SHARED_SECRET` bearer.
  - Webhook receivers: provider-specific signatures (Lob HMAC, Dub HMAC, Entri JWT, Twilio sig, Vapi sig). Strict / permissive_audit / disabled per provider; production refuses to boot in anything but strict.

### 2.10 Other infrastructure (built, not the centerpiece)

- DEX (data-engine-x) audience reservations — `business.org_audience_reservations` couples a paying org to a frozen DEX `ops.audience_specs` row. Read path at `/api/audience-reservations/{id}/audience` returns `{reservation, descriptor, count}`.
- Exa research prototype — `POST /api/v1/exa/jobs` + Trigger.dev task `exa.process_research_job` + `exa.exa_calls` raw archive. Used by the master-strategist's partner-research read inline (no MAGS subagent for partner Exa yet).
- DMaaS scaffold authoring (managed-agent-driven design generation against Lob mailer specs).
- EmailBison adapter + webhook projector + reconciliation crons (built but not on the active GTM critical path).
- SMS via Twilio (built; not in active outreach).

---

## 3. The architecture in two pictures

### 3.1 Post-payment pipeline (what runs after `gtm_initiatives` is created)

```
gtm_initiatives row created (paid, frozen audience spec)
       │
       │ POST /api/v1/admin/initiatives/{id}/start-pipeline
       ▼
Trigger.dev: gtm.run-initiative-pipeline
       │
       │ for each step in PIPELINE_STEPS:
       │     callRunStep(actor)  → hq-x /run-step → MAGS agent → run row
       │     callRunStep(verdict) → hq-x /run-step → MAGS agent → run row
       │     if verdict.ship == false and attempts < MAX_VERDICT_RETRIES:
       │         retry actor with redo_with hint
       │     else if verdict.ship == false:
       │         pipeline-failed
       │
       │ on per-recipient step:
       │     batchTrigger N×K child tasks (concurrency 50)
       │     each child: actor + verdict on (recipient × step)
       │
       ▼
all artifacts persisted to gtm_subagent_runs + downstream tables
       │
       ▼
operator iterates prompts via /admin/agents/<slug> editor
       │
       ▼
"Rerun from here" → re-fires Trigger.dev with startFrom=slug
```

### 3.2 Per-piece direct-mail send (executed once creative is ready)

```
per-recipient-creative output (DSL + copy per recipient × DM step)
       │
       ▼
render-and-submit (FUTURE directive — not built yet):
   per-piece DSL → final HTML/PDF
       │
       ▼
activate_pieces_batch(specs)  [app/services/print_mail_activation.py]
       │
       ├── LobAdapter per piece type (postcard, letter, self_mailer, snap_pack, booklet)
       └── direct_mail_pieces row + Dub link backref + manifest backref
              │
              │ Lob webhooks per piece event
              ▼
       lob_processor projects → direct_mail_pieces state machine + emit_event(...)
              │
              ▼
       fan-out: log + ClickHouse(no-op) + RudderStack + internal webhooks
```

---

## 4. What works end-to-end today

**The pipeline runs.** Verified 2026-05-01 against the DAT initiative on prd:

1. `gtm_initiatives` row exists for DAT (id `bbd9d9c3-c48e-4373-91f4-721775dca54e`).
2. `start-pipeline` fires → Trigger.dev workflow runs.
3. Sequence-definer / channel-step-materializer / audience-materializer all complete: `business.campaigns` + `channel_campaigns` + `channel_campaign_steps` + 100 `recipients` + memberships + manifest rows + Dub-link skip-on-plan-tier.
4. Master-strategist + verdict run against full inputs.
5. Run rows visible at `/admin/initiatives/<id>` with input/output/prompt-snapshot/mcp-calls per row.
6. Per-recipient creative fanout exists in code; not yet exercised end-to-end (master-strategist's verdict blocked the first smoke run, which is the desired iteration target).

**Operator iteration loop works.** Edit prompt at `/admin/agents/<slug>` → Activate (snapshot-then-overwrite, two version rows written) → "Rerun from here" → new run uses new prompt automatically. Rollback works at any version.

**Schema migrations applied to dev + prd.** 58 SQL migrations, latest = `20260501T060000_channel_campaigns_allow_voice_inbound.sql`.

**1068+ passing pytest cases** as of the last clean baseline (foundation + materializer test suites both ship).

---

## 5. What's NOT built (gaps + future work)

### 5.1 `gtm-voice-agent-instantiator` subagent

Mints a Vapi assistant per initiative from brand voice + partner routing. Substrate (`voice_assistants` table, Vapi provider client) is built. The agent + the integration that wires inbound numbers to per-initiative attribution doesn't exist yet. Parallel directive — doesn't block direct-mail loop.

### 5.2 Render-and-submit pipeline

The bridge from per-recipient-creative DSL output → final HTML/PDF → `activate_pieces_batch` against Lob test mode. Substrate exists on both sides (creative emits DSL; print_mail_activation accepts piece specs). The transformer in between is its own directive.

### 5.3 Audience-Exa subagents (#5 / #6 / #8)

Master-strategist currently reads Exa partner research inline via `_fetch_exa_payload`. Splitting the audience-specific Exa run + output shaper + brand context loader into separate MAGS subagents is a downstream iteration directive.

### 5.4 Per-recipient creative scale-out — actually running it

Code path exists. Has not run end-to-end against materialized recipients yet (master-strategist's verdict blocked the first smoke run). First clean run will exercise this; expect 100s–1000s of fanout rows depending on audience cap.

### 5.5 hq-command frontend fanout aggregate view

Backend ships `GET /api/v1/admin/initiatives/{id}/runs/aggregated`. Frontend still renders flat list of all runs — OK at current pipeline depth, will choke at full audience fanout. Sibling hq-command directive once foundation iteration stabilizes.

### 5.6 Frontend prospect-video player

Backend directive landed at `docs/directives/prospect-video-rendering.md` (Remotion Lambda + DB tracking + Dub link minting). Frontend `/v/<short_code>` player page is a separate hq-command directive.

### 5.7 Stripe / partner-payment automation

`partner_contracts` rows exist as the contract record regardless of payment provenance. Stripe customer/payment-method/charge tables aren't built. Partner payments are operator-managed today.

### 5.8 Customer self-serve onboarding

Brand creation, domain provisioning, theme configuration, partner-contract creation — all operator-side. No self-serve flow.

### 5.9 Cost tracking population

`gtm_subagent_runs.cost_cents` is NULL for all rows. Anthropic usage tokens are returned in run output but not converted to cents. Per-model rate table + cost computation is a follow-up.

### 5.10 Multi-org doctrine

`business.org_doctrine` table supports per-org rows; only acq-eng populated. Multi-org would matter if other orgs run owned-brand initiatives in parallel.

### 5.11 Sub-squad critic split

Verdicts carry critic-style reasoning inline today. Splitting actor + critic + verdict into three separate MAGS agents per pipeline step is a future iteration directive — not blocking.

### 5.12 Retry-with-hint loop richness

`MAX_VERDICT_RETRIES = 1` in `src/trigger/gtm-run-initiative-pipeline.ts`. Tunable when operator wants graduated retry budgets.

### 5.13 Customer-facing dashboard / public API

Old DMaaS API surface is dormant (still live for legacy rows). Building a customer dashboard is not on the roadmap — operator tooling is the focus.

### 5.14 ClickHouse cluster

Same as before — not provisioned. `emit_event()` fan-out to ClickHouse is a no-op. Postgres handles current scale.

### 5.15 Standalone-script DB-pool init

`scripts/register_gtm_agent.py` and `scripts/seed_dat_gtm_pipeline_foundation.py` use `get_db_connection()` which expects FastAPI lifespan to have init'd the pool. Standalone runs hit `RuntimeError: DB pool is not initialized`. A `/tmp/run_smoke_gate.py` wrapper exists as a workaround. Real fix: wrap each script's `_amain` with `init_pool()` / `close_pool()`. Trivial follow-up PR.

---

## 6. Where to start if you're picking this up cold

The 4-5 .md files to read in order, briefed for a new agent:

1. **[`STATE_OF_HQ_X.md`](STATE_OF_HQ_X.md)** (this doc) — what hq-x is right now, what works, what's next.
2. **[`docs/strategic-direction-owned-brand-leadgen.md`](docs/strategic-direction-owned-brand-leadgen.md)** — the business model. Read before touching any architecture decision.
3. **[`docs/handoff-gtm-pipeline-foundation-2026-05-01.md`](docs/handoff-gtm-pipeline-foundation-2026-05-01.md)** — operational reality of the GTM-pipeline foundation slice (post-payment pipeline) including the unblock sequence and current iteration target.
4. **[`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md)** — canonical campaigns hierarchy (channel × provider × step). Foundational concept; everything from analytics to materialization references it.
5. **[`CLAUDE.md`](CLAUDE.md)** — Doppler patterns, migration filename convention, async-job patterns, how to run scripts and tests.

Then run `uv run pytest -q` to confirm env. Then look at [`docs/handoff-gtm-pipeline-foundation-2026-05-01.md`](docs/handoff-gtm-pipeline-foundation-2026-05-01.md) §5 for the unblock sequence (migrations + doctrine sync + agent registration + Trigger.dev deploy + smoke gate). All of §5 is done as of this snapshot — but the procedure is documented there for replay.

---

## 7. Conventions that hold across the codebase

- **Eight-tuple is sacred.** Every analytics emit goes through `emit_event()` and carries the canonical hierarchy plus `initiative_id`. New emit sites that bypass it = bug.
- **Org isolation via single-WHERE-clause lookups.** Recipient lookups combine `recipient_id` AND `organization_id` in the same WHERE clause to avoid timing leaks.
- **Cross-org access returns 404, not 403.** Don't leak existence across orgs.
- **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, RudderStack `track()` — none ever raise into the caller.
- **Provider adapters are the single chokepoint** for outbound API calls. `app/providers/lob/adapter.py`, `app/providers/dub/client.py`, `app/services/anthropic_managed_agents.py`. Don't add new HTTP call sites in routers or services.
- **Trigger.dev tasks call hq-x via `/internal/*` only.** Real logic stays in Python. TS files are thin shims. Anthropic invocation lives entirely in hq-x.
- **Job state in Postgres is the source of truth.** Trigger.dev run state is operational metadata.
- **MAGS prompts: Anthropic-as-live, DB-as-history.** Activate snapshots current Anthropic state into `agent_prompt_versions` before pushing the new prompt. Two version rows per activate.
- **Migration filenames use timestamp prefix** (`YYYYMMDDTHHMMSS_<slug>.sql`). New migrations go in lex-order after the existing rows.
- **Ruff config:** line length 100, target py312, lint set `["E", "F", "I", "W", "UP", "B"]`.
- **No emojis** in code, comments, commit messages, or docs unless explicitly requested.
- **Doctrine docs live on disk first.** `data/orgs/<slug>/doctrine.md` + `parameters.json` mirror to `business.org_doctrine`. `data/brands/<slug>/*.md` mirror to `business.brand_content`. Disk is canonical; DB is queryable mirror.

---

## 8. Numbers

- **58 migrations** in `migrations/`.
- **63 routers** across `app/routers/`, `app/routers/admin/`, `app/routers/internal/`, `app/routers/webhooks/`.
- **52 services** in `app/services/`.
- **6 provider adapters**: Anthropic Managed Agents, Dub, Lob, Entri, EmailBison, Vapi.
- **15 Trigger.dev TS tasks** in `src/trigger/` (GTM orchestration + DMaaS legacy orchestration + reconciliation crons + voice callbacks + health check).
- **13 directives** in `docs/directives/` covering the GTM pipeline trilogy (foundation / attribution / materializer), prospect-video rendering, DMaaS scaffold authoring, plus pre-pivot DMaaS directives kept for archaeology.
- **10 MAGS agents** registered in `business.gtm_agent_registry` (5 actor + 5 verdict pairs).
- **3 hq-command admin pages** live in production: initiatives, agents, doctrine.
- **1068+ passing pytest cases** at last clean baseline (was 917 in the 2026-04-30 snapshot — +151 net new).

---

## 9. Bottom line

The platform pivoted from "self-serve DMaaS API for external customers" to **internal tooling for owned-brand lead-gen**. The post-payment GTM-initiative pipeline is the centerpiece, runs on Anthropic Managed Agents, has been verified end-to-end on prd, and surfaces real prompt-iteration targets through the admin command center.

What remains is downstream work — render-and-submit, voice-agent instantiation, frontend fanout view, prospect-video frontend, Stripe — each its own directive. The substrate is in place; each remaining piece slots in mechanically.

**Next concrete step:** iterate the `gtm-master-strategist` system prompt at `/admin/agents/gtm-master-strategist` based on the verdict-block issues from the first prd smoke run, rerun, and unblock the per-recipient creative fanout. Once that fans clean, the path to first paid initiative going live is render-and-submit + voice-agent instantiation + first inbound call.
