# State of hq-x — 2026-04-30

A snapshot of what the platform is, what's been built, what works end-to-end today, and what's next. Audience: future me, future agents, future collaborators picking this up cold.

---

## 1. What hq-x is

A multi-channel outreach platform with a focused near-term product: **DMaaS** (direct-mail-as-a-service) for enterprise customers. The platform handles the entire direct-mail lifecycle on the customer's behalf — print + ship via Lob, recipient-scannable QR / short links via Dub, hosted landing pages on the customer's custom domain via Entri, lead capture into our DB, full attribution analytics, webhook fan-out to whatever the customer's downstream stack is.

The non-DMaaS surface (voice via Vapi/Twilio, SMS via Twilio, email via EmailBison) is wired but not the immediate revenue focus. Paying DMaaS customers are explicitly not expected to use email or voice.

The value-prop sentence: **"You hand us recipients + a creative; we send the postcards, host the landing pages, capture the leads, and show you the funnel — all on your branded domain."**

---

## 2. What's been built (by capability, not chronology)

### 2.1 The campaigns hierarchy

Five layers of concept, organization-scoped, fully attribution-traceable:

```
business.organizations
  └── business.brands
        └── business.campaigns                    (umbrella outreach effort)
              └── business.channel_campaigns      (one per channel × provider)
                    └── business.channel_campaign_steps   (ordered touches; 1:1 with provider primitive)
                          ├── business.channel_campaign_step_recipients   (audience + status)
                          └── per-recipient artifact rows (direct_mail_pieces, dmaas_dub_links, etc.)

business.recipients ◄── channel-agnostic identity (org-scoped)
```

Every analytics event carries the canonical six-tuple `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel, provider)` plus `recipient_id` for per-recipient events. Enforced in code at [`app/services/analytics.py:emit_event`](app/services/analytics.py) — the chokepoint that no emit site bypasses.

Canonical reference: [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md). Read it first if you're new.

### 2.2 The DMaaS send loop

End-to-end direct-mail send via Lob:

1. Operator (or customer-facing API) creates the campaign + channel_campaign + step + audience.
2. Step activation (now async — see §2.6):
   - **Dub bulk-mints per-recipient short links** (≤100/req, ~15s for 5,000 recipients). One folder per channel_campaign, tags for step/campaign/brand. Idempotent retry.
   - **Lob audience upload via `/v1/uploads`** with operator-supplied creative HTML (`channel_specific_config.lob_creative_payload`). Step ↔ Lob campaign 1:1.
   - Lob mints pieces server-side. Per-piece webhooks arrive tagged with our metadata.
3. Lob webhooks update `direct_mail_pieces` state machine (`queued → processed → in_transit → delivered | returned | failed`). Membership state on `channel_campaign_step_recipients` transitions in lockstep.
4. Recipient scans QR. Dub records click. Dub webhook hits us → `dmaas_dub_events` insert + `emit_event("dub.click", ...)` fan-out.
5. Recipient hits the hosted landing page (see §2.3).

Adapter is [`app/providers/lob/adapter.py`](app/providers/lob/adapter.py); webhook projector is [`app/webhooks/lob_processor.py`](app/webhooks/lob_processor.py); Dub minting is [`app/dmaas/step_link_minting.py`](app/dmaas/step_link_minting.py). Detailed flow in [`docs/lob-integration.md`](docs/lob-integration.md).

### 2.3 Hosted landing pages on customer-owned domains

The $25K/mo enterprise value-add. We host the page; Entri Power proxies the customer's custom subdomain to our backend; Entri Secure provisions Let's Encrypt TLS automatically.

- Each brand carries a `landing_page_domain_config` JSONB (Entri-managed) and a `dub_domain_config` JSONB (Dub-managed link host). Two subdomains per brand by convention (e.g., `track.<brand-domain>` and `pages.<brand-domain>`).
- Landing page render: [`app/routers/landing_pages.py`](app/routers/landing_pages.py) at `GET /lp/{step_id}/{short_code}`. Server-side Jinja2. Per-brand theme (`business.brands.theme_config`: logo / colors / fonts) + per-step content (`business.channel_campaign_steps.landing_page_config`: headline / body / CTA / form_schema). Personalization via `{recipient.display_name}` style substitution.
- Page view tracked via `emit_event("page.viewed", ...)` on every render (with per-IP rate-limit dedup at the application layer).
- Form submission at `POST /lp/{step_id}/{short_code}/submit` validates against the step's form_schema, persists to `business.landing_page_submissions` (form_data as JSONB), fires `emit_event("page.submitted", ...)` + Dub `track_lead` for attribution.
- Honeypot + per-IP-per-step rate limit. IPs hashed before storage (no raw PII).

Entri integration spec: [`docs/entri-integration.md`](docs/entri-integration.md).

### 2.4 Conversion analytics

Six analytics endpoints under `/api/v1/analytics/`, all org-scoped, all Postgres-backed. Every direct-mail rollup carries the conversion funnel:

```json
"conversions": {
  "clicks_total": N, "unique_clickers": N, "click_rate": 0.0,
  "leads_total": N, "unique_leads": N, "lead_rate": 0.0
}
```

| Endpoint | What |
|---|---|
| `/reliability` | Webhook ingestion health rolled up per provider (events/replays/by_status) |
| `/campaigns/{id}/summary` | Umbrella campaign rollup (per-channel + per-channel_campaign + per-step + unique recipients) |
| `/channel-campaigns/{id}/summary` | Per-channel_campaign drilldown with channel-specific extensions |
| `/channel-campaign-steps/{id}/summary` | Per-step drilldown with membership funnel |
| `/recipients/{id}/timeline` | Per-recipient event stream (piece events + membership transitions + Dub clicks + page views/submits) |
| `/direct-mail` | Direct-mail piece funnel (queued → processed → in_transit → delivered → returned / failed) with brand / channel_campaign / step filters |
| `/campaigns/{id}/leads` + `/leads` | Customer-facing leads list with full form_data |

All cross-org isolation tested per endpoint (single-WHERE-clause recipient lookups to avoid timing leaks).

`sales_total` is intentionally not surfaced — we don't have CRM visibility unless a customer wires `track_sale` themselves. Surfacing zero would imply visibility we don't have.

### 2.5 Event fan-out

Every `emit_event()` call fans out to:

1. **Stdout logs** (always) — operational firehose into whatever log aggregator is wired.
2. **ClickHouse** (no-op today; cluster intentionally not provisioned) — `clickhouse_table` parameter is honored when env vars are set; silent skip otherwise. Future-ready, not future-blocking.
3. **RudderStack** — wired to source `hq-x-server` on the substrate-tools workspace. Today destinations are unconfigured (RudderStack is the firehose; hooking up Mixpanel / Snowflake / customer warehouses is a config exercise per customer).
4. **Customer webhook subscriptions** (after Directive 3) — per-org first-class subscribe/list/update/delete API, HMAC-SHA256 signing, retry schedule 1m/5m/30m/2h/12h then dead-letter. The standard SaaS webhooks pattern.

Chokepoint enforced in [`app/services/analytics.py`](app/services/analytics.py). New emit sites must go through it; the directive series carried this as a hard rule throughout.

### 2.6 Async orchestration via Trigger.dev

Long-running operations moved off the request thread:

- **`POST /api/v1/dmaas/campaigns` is async-only.** Returns 202 with `job_id`. State lives in `business.activation_jobs` (Postgres = source of truth). Customers poll `GET /api/v1/dmaas/jobs/{id}` or subscribe to `job.succeeded` / `job.failed` webhooks.
- **Multi-step scheduler.** When ALL members of step N reach terminal status, the step completes; if step N+1 exists in the same channel_campaign, a Trigger.dev task with `wait.for(delay_days_from_previous)` is scheduled. Pause/archive cascades cancel queued jobs via Trigger.dev's run-cancel API.
- **Five reconciliation crons** (each Doppler-flag-gated for instant kill): stale jobs, Lob piece reconciliation, Dub click drift detection, webhook replay backstop, customer webhook delivery retry sweep.
- **TS task layer.** Tasks live in [`src/trigger/`](src/trigger). Each task is a thin shim that calls a hq-x `/internal/*` endpoint via [`src/trigger/lib/hqx-client.ts`](src/trigger/lib/hqx-client.ts) using `TRIGGER_SHARED_SECRET`. All real business logic stays in Python. No direct DB access from TS.

### 2.7 Multi-tenancy, auth, and identity

- **Organizations** as the top tenant; **brands** as the customer-facing identity (a single org can have multiple brands). Two-axis roles: platform-level (`platform_operator`) and per-org (`admin | member`). [`docs/tenancy-model.md`](docs/tenancy-model.md).
- **Recipients** are channel-agnostic identities, **strictly org-scoped** (cross-org sharing is never supported). Natural-keyed by `(organization_id, external_source, external_id)`.
- **Auth flavors**:
  - Customer-facing endpoints: Supabase ES256 JWT verified via JWKS; org context resolved via `X-Organization-Id` header (`require_org_context`).
  - Internal Trigger.dev callbacks: shared-secret bearer (`TRIGGER_SHARED_SECRET`).
  - Webhook receivers: provider-specific signature verification (Lob HMAC, Dub HMAC, Entri JWT, Twilio signature, Vapi signature). Each provider has a strict / permissive_audit / disabled mode; production refuses to boot in anything but strict (per `assert_production_safe`).

### 2.8 Other capabilities (built, not the focus)

- Voice outbound via Vapi + Twilio (call_logs, voice_assistants, IVR, callback workflow).
- SMS via Twilio (sms_messages, suppression list, STOP/HELP).
- EmailBison adapter + webhook projector + reconciliation crons (built but explicitly not part of the DMaaS roadmap).
- DMaaS scaffold authoring (managed-agent-driven design generation against Lob mailer specs; produces dmaas_designs rows that step.creative_ref points at).
- A lot of voice infrastructure carried over from the OEX port that doesn't intersect DMaaS but is preserved for future use.

---

## 3. The architecture in three pictures

### 3.1 Send-loop dataflow

```
operator/customer
       │
       ▼
POST /api/v1/dmaas/campaigns (async)
       │
       ▼
business.activation_jobs (Postgres) ──── job_id returned to caller (202)
       │
       │ Trigger.dev enqueue
       ▼
TS task: dmaas.process-activation-job
       │
       │ POST /internal/dmaas/process-job
       ▼
hq-x service: dmaas_campaign_activation
       │
       ├── recipients bulk-upsert
       ├── audience materialization
       ├── Dub bulk-mint links (per-recipient, ≤100/req)
       └── LobAdapter.activate_step
              ├── Lob POST /v1/campaigns (creative inline)
              └── Lob POST /v1/uploads (audience CSV)
                     │
                     ▼
              Lob mints pieces server-side
                     │
                     │ webhooks per piece event
                     ▼
       lob_processor projects → direct_mail_pieces + emit_event(...)
                     │
                     ▼
       fan-out: log + ClickHouse(no-op) + RudderStack + customer webhooks
```

### 3.2 Click + landing dataflow

```
recipient scans QR (URL: track.<brand>.com/<short>)
       │
       ▼
Dub serves the link host (custom domain via Dub's POST /domains)
       │
       │ records click, 302 → destination URL
       ▼
destination URL: pages.<brand>.com/lp/<step>/<short>  (Entri Power → our backend)
       │
       ▼
landing_pages.py renders Jinja2 template
       │   (theme from brand.theme_config; content from step.landing_page_config;
       │    personalization from recipient row)
       │
       ├── emit_event("page.viewed", ...) → fan-out
       └── form HTML returned to recipient
              │
              │ recipient submits form
              ▼
       POST /lp/<step>/<short>/submit
              │
              ├── validate against form_schema
              ├── insert business.landing_page_submissions (form_data JSONB)
              ├── emit_event("page.submitted", ...) → fan-out
              └── Dub track_lead (optional)
```

### 3.3 Orchestration layer

```
sync request handlers
       │
       └─── (only for fast operations: CRUD, GETs, idempotent toggles)

async via Trigger.dev tasks (TS) → /internal/* (Python)
       │
       ├── dmaas.process-activation-job   (DMaaS campaign creation)
       ├── dmaas.scheduled-step-activation  (multi-step scheduler — wait.for(N days))
       ├── customer-webhook.deliver       (per-delivery retry / dead-letter)
       └── reconciliation crons (daily, feature-flag-gated):
              ├── stale activation jobs
              ├── Lob piece reconciliation
              ├── Dub click drift detection
              ├── webhook event replay backstop
              └── customer webhook delivery retries

shared secret: TRIGGER_SHARED_SECRET (Doppler) — same value in Trigger.dev env + hq-x env
job state truth: business.activation_jobs (Postgres) — Trigger.dev run state is operational metadata only
```

---

## 4. What works end-to-end today

A complete, demoable customer journey:

1. **Onboard a brand** — create brand record, configure custom domains via Entri (DNS + TLS auto-provisioned), register Dub link domain via `POST /domains`.
2. **Create a campaign** — single call to `POST /api/v1/dmaas/campaigns` with `{name, brand_id, recipients[], creative.lob_creative_payload, landing_page.{headline, body, form_schema}, send_date}`. Returns 202 + `job_id`.
3. **Track activation** — poll `GET /api/v1/dmaas/jobs/{job_id}` until `succeeded`, OR receive `job.succeeded` webhook to subscribed URL.
4. **Lob prints + ships** the pieces. Webhooks land per-piece. Direct-mail funnel updates in real time.
5. **Recipient scans the QR**. Dub records click. Landing page renders on the brand's domain with theme + personalization. Page view tracked.
6. **Recipient submits the form**. Lead captured in `landing_page_submissions`. `page.submitted` webhook fires to customer's CRM endpoint. Lead appears in `GET /api/v1/analytics/campaigns/{id}/leads`.
7. **Multi-step drip** — when step 1 completes, Trigger.dev's `wait.for(delay_days)` schedules step 2. Customer doesn't need to do anything.
8. **Customer dashboard** reads from the analytics endpoints — campaign rollup, conversion funnel, per-recipient timeline, leads list.

Tested via 917 passing pytest cases plus per-PR manual smoke against Lob test mode + real Dub events + Entri test domain.

---

## 5. What's NOT built (gaps + future work)

### 5.1 Customer-facing frontend

There is no UI. Everything above is API-first. A customer dashboard frontend (Next.js / React / something) consuming these endpoints is a separate workstream. The endpoints are dashboard-shaped — built with the customer view in mind — but the actual rendering is greenfield.

### 5.2 dmaas_designs → Lob creative renderer

Today the operator/customer manually prepares creative HTML and pastes it into `channel_specific_config.lob_creative_payload` per step. The `dmaas_designs.id` referenced by `creative_ref` is preserved as metadata for a future renderer. Building that renderer (HTML synthesis + CSS positioning + font/asset hosting + panel-aware self-mailer geometry) is its own multi-PR project. Possibly Remotion-powered. Not blocking but limits how scaffold-author-driven the platform can be without it.

### 5.3 Billing / metering / usage tracking

Trigger.dev charges per task invocation. Lob charges per piece. Dub has rate limits and tiers. None of this is metered per org for billing purposes. No invoice generation. No usage caps. All operator-managed today.

### 5.4 Customer self-serve onboarding

Brand creation, domain provisioning, theme configuration are all operator endpoints today. A self-serve signup → create org → add brand → configure domain flow doesn't exist.

### 5.5 Voice/SMS step + recipient wiring

`call_logs` and `sms_messages` don't yet carry `channel_campaign_step_id` or `recipient_id`. Voice/SMS rollups in the analytics endpoints use a "synthetic step" fallback (`voice_step_attribution: "synthetic"`). Wiring these would let voice/SMS join the same per-recipient timeline as direct mail. Lower priority since DMaaS customers don't use these channels.

### 5.6 ClickHouse cluster

Not provisioned. `emit_event()` fan-out to ClickHouse is a no-op. The wide-events DDL exists conceptually in the directive history but was never written as a doc. When/if you provision a cluster and want sub-second cross-channel analytics at scale, that's the path. Postgres handles current scale fine.

### 5.7 Sales attribution

`track_sale` Dub wrapper exists but is uncalled. Surfacing `sales_total` requires customers to wire their CRM into Dub themselves. Not on the roadmap as a platform feature; available as a documented integration recipe if a customer asks.

### 5.8 Email pipeline

EmailBison adapter is built but DMaaS customers explicitly don't use email. If that changes, the pieces are in place; if not, treat as latent infrastructure.

### 5.9 Customer-defined automations / workflows

Trigger.dev tasks today are platform-defined. A "customer can write their own follow-up logic" surface (Zapier-like) doesn't exist.

### 5.10 Analytics caching / materialized views

Every analytics request hits live Postgres. At scale, caching the rollups (Redis, materialized views, or eventually ClickHouse) would matter. Not at current scale.

---

## 6. Where to start if you're picking this up cold

1. **Read** [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) end to end. It's the canonical model. Everything else makes sense once you have this.
2. **Read** [`docs/lob-integration.md`](docs/lob-integration.md) for the direct-mail send loop in depth.
3. **Read** the post-ship docs in order to understand the build history:
   - [`docs/dmaas-foundation-pr-notes.md`](docs/dmaas-foundation-pr-notes.md)
   - [`docs/dmaas-hosted-pages-pr-notes.md`](docs/dmaas-hosted-pages-pr-notes.md)
   - [`docs/dmaas-orchestration-pr-notes.md`](docs/dmaas-orchestration-pr-notes.md)
4. **Read** the directive .md files at repo root to understand the why behind the architecture decisions:
   - [`DIRECTIVE_HQX_DMAAS_FOUNDATION.md`](DIRECTIVE_HQX_DMAAS_FOUNDATION.md)
   - [`DIRECTIVE_HQX_DMAAS_HOSTED_PAGES.md`](DIRECTIVE_HQX_DMAAS_HOSTED_PAGES.md)
   - [`DIRECTIVE_HQX_DMAAS_ORCHESTRATION.md`](DIRECTIVE_HQX_DMAAS_ORCHESTRATION.md)
5. **Run the tests** — `uv run pytest -q` (917 passing baseline). Confirms your env is set up and the codebase is healthy.
6. **Smoke an end-to-end campaign in dev** — there's a smoke script at `scripts/smoke_dmaas_lob_end_to_end.sh` (or similar — check `scripts/`). Lob test mode + real Dub + Entri test domain. Watch a job go from `queued → running → succeeded`, see test pieces queued in Lob's dashboard, scan a test QR, see the page render.

---

## 7. Conventions that hold across the codebase

- **Six-tuple is sacred.** Every analytics emit goes through `emit_event()` and carries the canonical hierarchy. New emit sites that bypass it = bug.
- **Org isolation via single-WHERE-clause lookups.** Recipient lookups combine `recipient_id` AND `organization_id` in the same WHERE clause to avoid timing leaks.
- **Cross-org access returns 404, not 403.** Don't leak existence across orgs.
- **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, RudderStack `track()`, customer webhook deliver — none ever raise into the caller.
- **No silent assignment.** If a query needs an id that doesn't belong to the caller's org, 404. Don't fall back.
- **Provider adapters are the single chokepoint** for outbound API calls. `app/providers/lob/adapter.py`, `app/providers/dub/client.py`, etc. Don't add new HTTP call sites in routers or services.
- **Trigger.dev tasks call hq-x via `/internal/*` only.** Real logic stays in Python. TS files are thin shims.
- **Job state in Postgres is the source of truth.** Trigger.dev run state is operational metadata.
- **Migration filenames use timestamp prefix** (`YYYYMMDDTHHMMSS_<slug>.sql`).
- **Ruff config:** line length 100, target py312, lint set `["E", "F", "I", "W", "UP", "B"]`.
- **No emojis** in code, comments, commit messages, or docs unless explicitly requested.

---

## 8. Numbers

- **917 passing pytest cases** (up from 422 at the start of the analytics buildout — +495 net new).
- **39 routers** in `app/routers/` (28 customer-facing + internal/admin/webhooks subrouters).
- **35+ services** in `app/services/`.
- **6 provider adapters**: Dub, Lob, Entri, EmailBison, Twilio, Vapi.
- **8 Trigger.dev TS tasks** in `src/trigger/` (DMaaS orchestration + voice callbacks + health check).
- **26 Postgres migrations**, with the timestamp convention adopted from `20260429T120000_recipients.sql` onward.
- **8 directive .md files** at repo root + **16 docs** under `docs/` covering the canonical model, integration depth, post-ship summaries, and runbooks.
- **3 DMaaS-product directives shipped** (Foundation, Hosted Pages, Orchestration) — the trilogy that defines the current product surface.

---

## 9. Bottom line

The DMaaS platform is **production-grade and sellable** as of this snapshot. Every piece of the value-prop loop works end-to-end behind a single async API call. Custom domains, hosted pages, lead capture, customer webhooks, multi-step drip sequences, reconciliation — all live.

The remaining gaps are **non-blocking for first paying customers** but become important at the second / fifth / fiftieth: customer-facing frontend, dmaas_designs renderer, billing/metering, self-serve onboarding. Each is its own bounded workstream and can be picked up independently without touching the platform substrate.

The codebase is **conventionally-styled, well-tested, well-documented**, and the directive-driven build pattern (write directive → dispatch agent → ship slices → post-ship doc) has shipped three trilogy directives plus the prior analytics workstream cleanly.

Next step is yours to pick. Common candidates: customer-facing dashboard frontend; dmaas_designs renderer; first paying customer onboarding; billing/metering; voice/SMS step+recipient wiring.
