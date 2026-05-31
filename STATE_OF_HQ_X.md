# State of hq-x — 2026-05-31

A snapshot of what the platform is, what's been built, what works end-to-end today, and what's next. Audience: future you, future agents, future collaborators picking this up cold. **Supersedes the 2026-05-01 snapshot.** The headline change since then: the data-ingest infrastructure was re-architected onto the **Gen-3 fleet** (Universal Dispatcher + Trigger.dev v4 + DuckDB→Lance→R2), the entire Gen-2 fleet was frozen, and the legacy Modal-cron / legacy-Trigger control plane was retired.

hq-x is now two systems under one repo:

- **The Platform (product):** the owned-brand lead-gen GTM platform — FastAPI on Railway, the MAGS agent pipeline, campaigns hierarchy, direct-mail, voice. Sections 1–7 below.
- **The Gen-3 Data & Compute Fleet (infrastructure):** how data feeds are scheduled, routed, and ingested. Governed by [`ARCHITECTURE.md`](ARCHITECTURE.md) Part I (authoritative). Summarized in §2.11 and §10 below.

---

## 1. What hq-x is

The internal platform substrate for an **owned-brand lead-gen business**. Operator owns the platform. External companies (factoring co's, insurance agencies, wholesale RE, etc.) become **demand-side partners** who pay for 90-day exclusive flow of qualified leads produced by hq-x running outreach **under brands the operator owns**, against audiences sliced from the data fleet.

Old framing (DMaaS-as-customer-product) is **dormant**. Customer-webhook subscriptions still work but are no longer a public API. The DMaaS Campaigns API send path is preserved for legacy rows; new owned-brand work uses the per-piece Print & Mail path.

The value-prop sentence (internal): **"A demand-side partner pays. The pipeline materializes channels + audience + creative + voice agent under one of our brands. Recipients get a touch sequence; live transfers route to the partner via the AI agent."**

Read first for the business model: [`docs/strategic-direction-owned-brand-leadgen.md`](docs/strategic-direction-owned-brand-leadgen.md).

---

## 2. What's been built (by capability, not chronology)

### 2.1 The post-payment GTM pipeline (the centerpiece)

The headline workstream. A demand-side partner pays → a `business.gtm_initiatives` row gets created → a multi-step actor+verdict pipeline runs to produce every artifact needed for outreach: channel/step plan, audience materialization, master strategy, per-recipient creative.

**Runtime:** Anthropic Managed Agents API (MAGS). Each subagent is a separately-registered Anthropic agent. hq-x is the only seam between the orchestrator and Anthropic.

**Pipeline (5 actor+verdict pairs = 10 MAGS agents):**

```
gtm-sequence-definer            → economics-aware channel + touch plan (JSON)
gtm-channel-step-materializer   → JSON plan; hq-x writes campaigns/channel_campaigns/steps
gtm-audience-materializer       → JSON plan; hq-x pages the audience, upserts
                                  recipients + memberships + manifest, mints Dub links
gtm-master-strategist           → Master Strategy markdown (per-touch frames, NOT copy)
gtm-per-recipient-creative      → per-piece copy + design DSL JSON, fanned out
                                  per (recipient × DM step)
```

Verdict (paired with each actor): returns strict `{ship: bool, issues: [...], redo_with: string|null}`. Verdict-block triggers actor retry with hint (one retry budget in v0). Pipeline fails cleanly at `verdict_block_after_retries` if a verdict can't be satisfied.

**Run capture is the spine.** Every actor + verdict invocation writes a `business.gtm_subagent_runs` row capturing input, output, prompt snapshot, mcp_calls trace, anthropic_session_id, cost. Frontend reads from this table.

**Prompt versioning.** Snapshot-then-overwrite — every activate writes two rows to `business.agent_prompt_versions` (old state + new state). Anthropic holds the live prompt; DB is the durable history with full rollback.

> **Control-plane note (changed since 2026-05-01):** the GTM pipeline's *orchestration* previously ran as a set of bespoke Trigger.dev TS tasks (`gtm.run-initiative-pipeline`, `gtm.run-per-recipient-creative`, etc.). Those legacy tasks were **retired** in the SAM.gov-canonical / "retire all legacy Trigger schedules" change. The pipeline's actor/verdict logic (Python, MAGS) is intact; its Trigger.dev sequencing is being rebuilt on the Gen-3 v4 durable-callback substrate (§2.11), same as every data feed. Until that rewiring lands, treat GTM-pipeline orchestration as in-transition, not live-scheduled.

### 2.2 Frontend command center (hq-command repo)

`https://app.opsinternal.com` (Railway-deployed Next.js). Three admin surfaces:

- **`/admin/initiatives`** — list of initiatives + per-initiative drilldown showing every actor + verdict run with input / output / prompt-snapshot / mcp-calls / error-blob. Per-step "Rerun from here".
- **`/admin/agents`** — registry list + `/admin/agents/[slug]` prompt editor with Activate (writes two version rows) and per-version Rollback.
- **`/admin/doctrine`** — single-page editor for the operator-org doctrine markdown body + parameters JSON.

All proxied through hq-x backend (`/api/v1/admin/*`); no MAGS keys in browser.

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

Every analytics event carries `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel, provider, initiative_id)`. Enforced in [`app/services/analytics.py:emit_event`](app/services/analytics.py) — the chokepoint that no emit site bypasses.

Canonical reference for the older five-layer model: [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md).

### 2.4 Per-piece direct-mail submission (Lob Print & Mail)

The **active path for owned-brand initiatives.** Each recipient's piece is one Lob `POST /v1/{postcards|letters|self_mailers|snap_packs|booklets}` call carrying that recipient's bespoke creative. Bypasses Lob's Campaigns API entirely. Independent and additive — the Campaigns API `LobAdapter.activate_step` path remains for legacy DMaaS rows.

Substrate: [`app/services/print_mail_activation.py`](app/services/print_mail_activation.py). Per-piece isolation: a failure on piece N never aborts the batch. Discriminated-union `PieceSpec` (one Pydantic class per Lob type, `extra='forbid'`) catches cross-type field-shape misuse at construction time. `direct_mail_pieces` rows carry `_recipient_id` / `_channel_campaign_step_id` / `_membership_id` back-references in metadata.

Provider abstraction is intentionally absent today; PostGrid is documented in `docs/research/postgrid-print-mail-api-notes.md` for when it lands.

### 2.5 Hosted landing pages on owned-brand domains

Entri Power + Dub custom-domain plumbing on owned brands. `pages.<brand>.com/lp/<step>/<short_code>` proxies to backend; render uses brand theme + step `landing_page_config` + recipient personalization. Honeypot, per-IP rate-limit dedup, IP hashing. Render: [`app/routers/landing_pages.py`](app/routers/landing_pages.py).

### 2.6 Voice (inbound)

Recipients call the AI agent's number printed on direct mail / surfaced on landing page. The AI agent qualifies and live-transfers to the partner per `partner_contracts.qualification_rules` + `demand_side_partners.primary_phone`. **Voice in the new model is inbound only.**

Substrate is built — `voice_assistants` table holds Vapi assistant config, `voice_phone_numbers` maps phone ↔ assistant. Service code at `app/services/voice_*` and provider client at `app/providers/vapi/`. Not yet built: the `gtm-voice-agent-instantiator` subagent that mints a Vapi assistant per initiative.

### 2.7 Event fan-out

Every `emit_event()` call carries the canonical eight-tuple and fans out to:

1. **Stdout logs** (always)
2. **ClickHouse** (no-op today; cluster intentionally not provisioned)
3. **RudderStack** — source `hq-x-server`; destinations point at the operator's analytics stack
4. **Customer webhook subscriptions** — code still works but **no longer a public API**

Chokepoint enforced in [`app/services/analytics.py`](app/services/analytics.py).

### 2.8 Async orchestration via Trigger.dev (Gen-3 v4)

**The control plane is now Trigger.dev v4 only, governed by [`ARCHITECTURE.md`](ARCHITECTURE.md) Part I.** All legacy Trigger TS tasks (the GTM-pipeline orchestrator, the DMaaS activation/step/reconciliation crons, voice callbacks, health check — ~15 tasks + `lib/hqx-client.ts` in the 2026-05-01 snapshot) were **retired** in the SAM.gov-canonical migration.

- `src/trigger/` today holds the single canonical v4 task: [`sam_opps_bulk_dispatcher.ts`](src/trigger/sam_opps_bulk_dispatcher.ts) — `schedules.task` + `wait.createToken()` / `wait.forToken()` durable callback against the Universal Dispatcher.
- **`modal.Cron` is forbidden.** Cadence lives in `ops.scheduled_tasks` (seeded by `scripts/seed_scheduled_tasks.py`).
- The GTM-pipeline and DMaaS-legacy orchestration tasks are pending rebuild on this substrate.

### 2.9 Multi-tenancy, auth, identity

- **Organizations** as the top tenant; **brands** as the customer-facing identity. The platform org (`acq-eng`) owns the active brands; `business.demand_side_partners` rows model the paying partners separately.
- **Recipients** are channel-agnostic identities, **strictly org-scoped.** Natural-keyed by `(organization_id, external_source, external_id)`.
- **Auth flavors:** Customer-facing endpoints use Supabase ES256 JWT verified via JWKS (org context via `X-Organization-Id`). Internal callbacks use a shared-secret bearer. Webhook receivers use provider-specific signatures (Lob HMAC, Dub HMAC, Entri JWT, Twilio sig, Vapi sig); production refuses to boot in anything but strict.

### 2.10 Other infrastructure (built, not the centerpiece)

- Audience reservations — `business.org_audience_reservations` couples a paying org to a frozen `ops.audience_specs` row. Read path at `/api/audience-reservations/{id}/audience`.
- Exa research prototype — `POST /api/v1/exa/jobs` + `exa.exa_calls` raw archive. Used by the master-strategist's partner-research read inline.
- DMaaS scaffold authoring (managed-agent-driven design generation against Lob mailer specs).
- EmailBison adapter + webhook projector (built; not on the active GTM critical path). SMS via Twilio (built; not in active outreach).

### 2.11 The Gen-3 Data & Compute Fleet (data ingest)

The data-ingest infrastructure, re-architected. **Authoritative spec: [`ARCHITECTURE.md`](ARCHITECTURE.md) Part I.** Shape:

```
Trigger.dev v4 task (src/trigger/*.ts)
   wait.createToken() → POST Universal Dispatcher → wait.forToken()
        │
        ▼
core/modal_dispatcher.py  (Modal app "universal-dispatcher" — the ONLY web endpoint, proxy-authed)
   modal.Function.from_name(app_name, function_name).spawn(**kwargs, trigger_callback_url=...)
        │
        ▼
domain-grouped worker  (scripts/ingest/<domain>/…, Modal app e.g. "sam-gov-pipelines", no web endpoint)
   DuckDB read_csv(all_varchar) + TRY_CAST → lance.write_dataset(s3://…, v2.0) → R2
   on terminal: psycopg write ops.* → POST trigger_callback_url
```

**Status:**
- **Live (Gen-3):** SAM.gov Contract Opportunities (active). Files: dispatcher, `src/trigger/sam_opps_bulk_dispatcher.ts`, worker `scripts/ingest/sam_gov/sam_opps_bulk_canonical.py`, view `views/sam_gov/opps_active_latest_lance.sql`, state `ops.sam_opps_canonical_runs`.
- **Frozen (Gen-2):** the entire prior fleet — **104 Modal apps** in `modal/_archived_gen2/` + **391 DEX scripts** in `scripts/dex/_archived_gen2/`. Read-only reference only; not deployed, imported, or scheduled. Each feed is rebuilt onto Gen-3 one at a time.
- **Forbidden:** Iceberg, Polaris, `modal.Cron`, per-feed Modal web endpoints.

---

## 3. The post-payment pipeline (what runs after `gtm_initiatives` is created)

```
gtm_initiatives row created (paid, frozen audience spec)
       │  POST /api/v1/admin/initiatives/{id}/start-pipeline
       ▼
orchestrator sequences PIPELINE_STEPS:
   callRunStep(actor)  → hq-x /run-step → MAGS agent → run row
   callRunStep(verdict)→ hq-x /run-step → MAGS agent → run row
   if verdict.ship == false and attempts < MAX_VERDICT_RETRIES: retry actor with redo_with
   else if verdict.ship == false: pipeline-failed
   on per-recipient step: fan out N×K child runs (recipient × step)
       ▼
all artifacts persisted to gtm_subagent_runs + downstream tables
       ▼
operator iterates prompts via /admin/agents/<slug> → "Rerun from here"
```

(The orchestration driver is mid-migration onto the Gen-3 Trigger v4 substrate; see §2.8.)

---

## 4. What works end-to-end today

- **Gen-3 SAM.gov ingest** is the live, canonical reference pipeline (deployed `universal-dispatcher` + `sam-gov-pipelines` on Modal, 2026-05-30).
- **GTM pipeline (actor/verdict logic):** last verified end-to-end on prd **2026-05-01** against the DAT initiative (sequence-definer / channel-step-materializer / audience-materializer / master-strategist + verdicts all completed; run rows visible at `/admin/initiatives/<id>`). Its Trigger orchestration has since been retired and is pending rebuild (§2.8) — re-verify after rewiring.
- **Operator iteration loop** works: edit prompt → Activate (two version rows) → Rerun → new prompt picked up automatically; Rollback at any version.
- **Schema migrations** applied to dev + prd: **88** SQL migrations.
- **121** pytest test files at last clean baseline.

---

## 5. What's NOT built (gaps + future work)

- **Gen-3 feed rebuilds.** 95+ Gen-2 feeds remain frozen in `_archived_gen2/`, each awaiting a Gen-3 rebuild (Trigger v4 task + domain worker + `ops.scheduled_tasks` row). SAM.gov opps is the only one done.
- **GTM-pipeline orchestration on Gen-3.** The retired TS orchestrator needs rebuilding on the v4 durable-callback substrate.
- `gtm-voice-agent-instantiator` subagent (substrate built; agent + wiring not).
- Render-and-submit pipeline (per-recipient-creative DSL → HTML/PDF → `activate_pieces_batch`).
- Per-recipient creative scale-out — code path exists, not yet run end-to-end against materialized recipients.
- hq-command fanout aggregate view (backend `runs/aggregated` ships; frontend renders flat list).
- Stripe / partner-payment automation; customer self-serve onboarding; cost-tracking population (`gtm_subagent_runs.cost_cents` NULL); ClickHouse cluster (no-op).

---

## 6. Where to start if you're picking this up cold

1. **[`STATE_OF_HQ_X.md`](STATE_OF_HQ_X.md)** (this doc) — what hq-x is right now.
2. **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — Part I (Gen-3 data-fleet standards, authoritative) + Part II (FastAPI platform patterns).
3. **[`docs/strategic-direction-owned-brand-leadgen.md`](docs/strategic-direction-owned-brand-leadgen.md)** — the business model.
4. **[`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md)** — campaigns hierarchy (channel × provider × step).
5. **[`CLAUDE.md`](CLAUDE.md)** — Doppler patterns, migration convention, how to run scripts/tests.

Then `uv run pytest -q` to confirm env.

---

## 7. Conventions that hold across the codebase

- **Gen-3 data fleet is governed by `ARCHITECTURE.md` Part I.** Trigger v4 + Universal Dispatcher + domain-grouped workers + DuckDB→Lance→R2 + worker-owns-state. No `modal.Cron`, no Iceberg, no Polaris, no per-feed endpoints.
- **Eight-tuple is sacred.** Every analytics emit goes through `emit_event()` and carries the canonical hierarchy plus `initiative_id`.
- **Org isolation via single-WHERE-clause lookups.** Recipient lookups combine `recipient_id` AND `organization_id` in one WHERE clause. **Cross-org access returns 404, not 403.**
- **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, RudderStack `track()` never raise into the caller.
- **Provider adapters are the single chokepoint** for outbound API calls.
- **Job state in Postgres is the source of truth.** Trigger.dev run state is operational metadata.
- **MAGS prompts: Anthropic-as-live, DB-as-history.** Activate snapshots current Anthropic state before pushing the new prompt.
- **Migration filenames use a timestamp prefix** (`YYYYMMDDTHHMMSS_<slug>.sql`).
- **Ruff:** line length 100, target py312, lint `["E", "F", "I", "W", "UP", "B"]`.
- **No emojis** in code, comments, commit messages, or docs unless explicitly requested.
- **Doctrine docs live on disk first.** `data/orgs/<slug>/doctrine.md` + `parameters.json` mirror to `business.org_doctrine`; `data/brands/<slug>/*.md` mirror to `business.brand_content`. Disk is canonical.

---

## 8. Numbers

- **88 migrations** in `migrations/`.
- **95 router files** across `app/routers/` (incl. `admin/`, `internal/`, `webhooks/`).
- **106 services** in `app/services/`.
- **121 pytest test files** in `tests/`.
- **6 provider adapters:** Anthropic Managed Agents, Dub, Lob, Entri, EmailBison, Vapi.
- **1 canonical Trigger v4 task** in `src/trigger/` (the SAM.gov dispatcher) — the legacy ~15-task TS layer was retired.
- **9 directives** in `docs/directives/`.
- **10 MAGS agents** registered (5 actor + 5 verdict pairs).
- **Gen-3 fleet:** 1 live feed (SAM.gov opps); **104** Gen-2 Modal apps + **391** Gen-2 DEX scripts frozen in `_archived_gen2/`.

---

## 9. Bottom line

hq-x is two systems: the **owned-brand lead-gen GTM platform** (the product — FastAPI, MAGS agent pipeline, direct-mail, voice) and the **Gen-3 data & compute fleet** (the infrastructure — Universal Dispatcher + Trigger v4 + DuckDB→Lance→R2).

The data fleet was re-architected: one canonical pattern, one proxy-authed endpoint, worker-owned state, no Iceberg/Polaris/`modal.Cron`. SAM.gov opps is live on it; the rest of the fleet is frozen as read-only reference, rebuilt one feed at a time. The GTM platform's actor/verdict logic is intact and was verified on prd 2026-05-01; its Trigger orchestration is being rebuilt on the same Gen-3 substrate.

**Next concrete steps:** (1) rebuild the GTM-pipeline orchestration on Trigger v4; (2) port the highest-value Gen-2 feeds onto the Gen-3 dispatcher pattern; (3) render-and-submit + voice-agent instantiation toward first paid initiative going live.
