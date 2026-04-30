# Directive — DMaaS hosted pages, custom domains, opinionated API

**For:** an implementation agent shipping the value-add layer that justifies the $25K/mo enterprise price tier. Worktree path: `/Users/benjamincrane/hq-x` (or any worktree under it). Branch from `main`.

This is the **second** of the DMaaS-product directives. Directive 1 ([`DIRECTIVE_HQX_DMAAS_FOUNDATION.md`](DIRECTIVE_HQX_DMAAS_FOUNDATION.md)) shipped the foundation: Lob audience upload, Dub clicks → emit_event, conversion columns in analytics, recipient timeline includes Dub clicks. **Directive 1 is merged on `main`** as PRs #45–#48 (Slices 2/3/4/1, in order).

This directive builds the customer-facing product on top of that foundation: hosted landing pages on customer-owned domains, lead capture, and a one-call API that collapses the 5-call campaign creation flow. After this lands, the platform is sellable.

---

## 0. Why this directive exists

Three product gaps separate the foundation from a sellable enterprise DMaaS product:

1. **No hosted landing pages.** Recipients who scan the QR code land on whatever destination URL the operator supplies (today, that's anywhere). For a paying customer, the landing page IS the product — branded, personalized, conversion-optimized, owned by us. Hosting them ourselves means we own the entire funnel: piece sent → piece delivered → QR scanned → page rendered → page engaged → form submitted. Without our own hosted page, the conversion chain stops at "click."
2. **No custom domains for branded experience.** Recipients seeing `dub.sh/abc123` on a postcard is not enterprise-grade. Recipients seeing `track.theirbrand.com/abc123` is. The Entri integration is partially built ([`migrations/20260429T180000_entri_domain_connections.sql`](migrations/20260429T180000_entri_domain_connections.sql), [`app/providers/entri/`](app/providers/entri), [`app/routers/entri.py`](app/routers/entri.py)) — wire it through to the Dub link host + the landing page host so every recipient touchpoint lives on the brand's domain.
3. **The API surface is wrong for a DMaaS-only customer.** Today: `POST campaign` → `POST channel-campaign` → `POST step` → `materialize_audience` → `POST step/activate`. Five calls. For a customer who only does direct mail, that's wrong. One call should accept `{name, brand_id, recipients, creative, landing_page, send_date}` and produce a fully-activated campaign.

Plus: lead capture data needs to live in our DB and surface in customer dashboards. Per discussion, this is a deliberate lock-in lever — once a customer's leads are in your DB, switching costs are real.

---

## 1. Architectural decisions locked in (do not relitigate)

These were settled in design conversation. Treat as constraints.

### 1.1 We host the landing pages

The platform (not the customer) hosts the page recipients see after scanning the QR. This is the $25K/mo value prop. Customers can opt out and supply their own destination URL, but the default + the value-add path is hosted.

### 1.2 Two subdomains per brand for custom domains

Dub serves the link host directly (Dub supports custom domains via `POST /domains`); we serve the landing page on a separate subdomain via Entri Power. Naming convention is up to the agent — recommended `track.<brand-domain>` for the Dub link host and `pages.<brand-domain>` for the landing page host. The brand can also choose any other prefix combo as long as both are configured. Single-subdomain serving both Dub-redirect-and-page-render isn't an option — DNS can only point one place.

The slight UX seam (recipients hop between subdomains after click) is acceptable for V1. Both subdomains are on the brand's root domain so it stays branded.

### 1.3 Entri Power handles all TLS + reverse proxy

Per [`docs/entri-integration.md`](docs/entri-integration.md), Entri Power proxies the customer's custom domain to our backend AND auto-provisions Let's Encrypt certs (Entri Secure). **We do NOT need to run our own reverse proxy, manage TLS, or wire Caddy/nginx.** Just register `domain → applicationUrl` mappings via Entri's REST API. That work is already partially done — extend it.

### 1.4 Jinja2 server-side rendering for landing pages

Pick Jinja2. Reasons:
* Python-native, mature, battle-tested.
* Server-side rendered — fast TTFB matters for QR-scan UX.
* No JS framework overhead for what's essentially `{logo + headline + body + CTA + form}`.
* Customers won't need anything more for V1.

A future swap to React (server-side or hydrated) is possible behind the same `render_landing_page(step_id, recipient_id)` boundary, but not in this directive.

### 1.5 Form capture as flexible JSONB

Form schema lives in `channel_campaign_steps.landing_page_config.form_schema` (JSONB). Submitted form data lives in `business.landing_page_submissions.form_data` (JSONB) keyed by the same field names. Validation at submit time checks the data against the schema (required fields present, types match). This is intentional flexibility — every campaign can have its own form fields, and the dashboard reads form_data per-campaign rather than against a shared schema.

The leads displayed in customer dashboards are this table joined to `recipients`. **This is a deliberate lock-in lever** — once a customer's leads are in your DB, the cost of switching DMaaS providers includes "extract all our leads."

### 1.6 Personalization via simple `{token}` substitution

Landing page text supports tokens like `{recipient.display_name}`, `{recipient.mailing_address.city}`, `{step.name}`. Simple Python `str.format`-style substitution. No template engine inside the JSONB — Jinja2 is for the page wrapper, not user-supplied text.

Exact token vocabulary defined in the agent's implementation. Document it in the post-ship doc.

### 1.7 Synchronous activation stays synchronous

Per the Trigger.dev sequencing decision: Directive 2 stays synchronous. The opinionated single-call API does the create+materialize+activate inline. Trigger.dev refactor is Directive 3. Don't introduce async orchestration here.

### 1.8 Lead-related analytics fields ship now (filling the Directive-1 gap)

Directive 1 intentionally did NOT surface `leads_total` in conversion fields because the form-submit pipeline didn't exist. **It exists after this directive.** Add `leads_total` (and `lead_rate = unique_leads / unique_clickers`) to every conversion rollup. Recipient timeline includes `page.viewed` and `page.submitted` events.

`sales_total` stays out per Directive 1's reasoning — we don't have CRM visibility unless a customer wires `track_sale` themselves.

### 1.9 Migrations are required (relaxing Directive 1's §10)

Several slices need new tables or columns. New Postgres migrations follow the **timestamp prefix convention** (`YYYYMMDDTHHMMSS_<slug>.sql`) per the canonical doc.

### 1.10 ClickHouse + RudderStack stays as-is

ClickHouse stays unprovisioned (out of scope). RudderStack write fan-out from Directive 1 already covers the new event types — `emit_event("page.viewed", ...)` and `emit_event("page.submitted", ...)` will fan out automatically because they go through the same chokepoint.

---

## 2. Hard rules (carry forward + new)

1. **Six-tuple is sacred.** Every `emit_event()` call carries the full hierarchy + recipient_id where applicable.
2. **Org isolation tested per endpoint.** Cross-org access → 404, with negative tests.
3. **Single-WHERE-clause recipient lookups.** Never two-step.
4. **Fire-and-forget on writes.** `emit_event()`, RudderStack, etc. never raise.
5. **Provider adapters are the emit chokepoint.** Entri webhooks emit through `emit_event()` too where they carry resolvable context.
6. **Mind the four-level naming.** `campaign_id` / `channel_campaign_id` / `channel_campaign_step_id` / `recipient_id` — don't conflate.
7. **Don't surface analytics fields without data.** `sales_total` stays off until a customer wires `track_sale`.
8. **Idempotency for external API calls.** Re-running campaign creation, Entri Power registration, or Dub domain registration must not double-create.
9. **Custom domain hosts must be opt-in per brand.** A brand without a configured Dub domain still gets `dub.sh` short links; a brand without a configured landing page domain still falls back to a platform-default subdomain (e.g., `pages.opsengine.run/<brand-id>/p/<short-code>`). Default to working before custom domains are wired.
10. **Form submissions need rate limiting + spam protection.** At minimum: a honeypot field + per-IP rate limit. CAPTCHA is overkill for V1.
11. **Personalization tokens missing from recipient data render as empty string, not "None" or an exception.** Defensive substitution.

---

## 3. Slices to ship (in order)

Each slice is one commit + one PR against `main`. PRs may stack on the same branch or each branch from `main` — your call. Land each before opening the next.

### Slice 1 — Brand-domain wire-up (Dub `POST /domains` + extend Entri to per-brand)

**Goal:** every brand can have a configured Dub link host (`POST /domains`) and a configured landing page host (Entri Power). Without this, custom domains aren't actually wired through the system.

**File touchpoints:**

* New migration: `migrations/<timestamp>_brand_domains.sql` — extend `business.brands` with two nullable JSONB columns OR create a `business.brand_domains` table linking 1:N. Recommendation: extend `business.brands` with `dub_domain_config` and `landing_page_domain_config` JSONB columns. Simpler than a new table; brands have at most one of each.

  ```sql
  ALTER TABLE business.brands
    ADD COLUMN IF NOT EXISTS dub_domain_config JSONB,
    ADD COLUMN IF NOT EXISTS landing_page_domain_config JSONB;
  ```

  Each JSONB shape:
  ```json
  {
    "domain": "track.acme.com",
    "dub_domain_id": "dom_xxx",         // for dub_domain_config
    "entri_connection_id": "uuid",       // for landing_page_domain_config
    "verified_at": "2026-04-30T..."
  }
  ```

* Possibly extend [`migrations/20260429T180000_entri_domain_connections.sql`](migrations/20260429T180000_entri_domain_connections.sql) to add a nullable `brand_id` column if it doesn't already have one. Check first — don't double-add.

* Modify [`app/services/channel_campaigns_dub.py`](app/services/channel_campaigns_dub.py) and [`app/dmaas/step_link_minting.py`](app/dmaas/step_link_minting.py): when a step's brand has a configured `dub_domain_config`, mint links using that domain (Dub `POST /links` accepts a `domain` field). Otherwise default to the workspace default (`dub.sh`).

* New service: `app/services/brand_domains.py` — wraps:
  - `register_dub_domain_for_brand(brand_id, domain) -> dub_domain_id` (calls Dub `POST /domains` + persists)
  - `register_landing_page_domain_for_brand(brand_id, entri_connection_id) -> None` (links an existing entri row to the brand)
  - Idempotent: re-registering a domain that's already configured is a no-op + returns the existing config.

* New router endpoints in [`app/routers/brands.py`](app/routers/brands.py) (or a new `app/routers/brand_domains.py`):
  - `POST /api/v1/brands/{brand_id}/domains/dub` — body `{domain: "track.acme.com"}`, calls `register_dub_domain_for_brand`.
  - `POST /api/v1/brands/{brand_id}/domains/landing-page` — body `{entri_connection_id: "..."}`, links it.
  - `GET /api/v1/brands/{brand_id}/domains` — returns both configs.
  - `DELETE /api/v1/brands/{brand_id}/domains/dub` and `/landing-page` — deactivate.

**Required behavior:**

1. Brand-level domain config is independent: a brand can have a Dub domain with no landing page domain (links work, customer-supplied destination URL still wins) or vice versa.
2. Step minting reads `brand.dub_domain_config.domain` if set; defaults to `dub.sh` (or whatever the workspace default is).
3. Landing page render endpoint (built in Slice 3) reads `brand.landing_page_domain_config` to construct the URL it expects to be accessed under.
4. Org isolation: a user can only register/list/delete domains for brands in their org.

**Tests:**

* `tests/test_brand_domains_service.py` — pure service tests for register/list/delete, idempotency on re-registration.
* `tests/test_brand_domains_router.py` — endpoint auth + cross-org guard tests.
* `tests/test_step_link_minting_brand_domain.py` — verify step minting uses the brand's domain when configured, falls back when not.

---

### Slice 2 — Per-brand theme + per-step landing page config schemas

**Goal:** the data layer for landing page rendering. Brands carry visual theme; steps carry page content + form schema.

**File touchpoints:**

* New migration: `migrations/<timestamp>_brand_theme_and_step_landing_page.sql`:

  ```sql
  ALTER TABLE business.brands
    ADD COLUMN IF NOT EXISTS theme_config JSONB;

  ALTER TABLE business.channel_campaign_steps
    ADD COLUMN IF NOT EXISTS landing_page_config JSONB;
  ```

* `business.brands.theme_config` JSONB shape:
  ```json
  {
    "logo_url": "https://...",
    "primary_color": "#FF6B35",
    "secondary_color": "#1A1A1A",
    "background_color": "#FFFFFF",
    "text_color": "#222222",
    "font_family": "Inter",
    "custom_css": null
  }
  ```

* `business.channel_campaign_steps.landing_page_config` JSONB shape:
  ```json
  {
    "headline": "Your appointment is ready, {recipient.display_name}",
    "body": "We've reserved a spot for you...",
    "cta": {
      "type": "form",
      "label": "Confirm now",
      "form_schema": {
        "fields": [
          {"name": "name", "label": "Your name", "type": "text", "required": true},
          {"name": "email", "label": "Email", "type": "email", "required": true},
          {"name": "phone", "label": "Phone", "type": "tel", "required": false},
          {"name": "company", "label": "Company", "type": "text", "required": false}
        ]
      },
      "thank_you_message": "Thanks! We'll be in touch within 24 hours.",
      "thank_you_redirect_url": null
    }
  }
  ```

* Service layer: extend [`app/services/brands.py`](app/services/brands.py) and [`app/services/channel_campaign_steps.py`](app/services/channel_campaign_steps.py) with theme / landing_page_config get + update functions. Don't add new files unless services grow large.

* Pydantic models: extend [`app/models/brands.py`](app/models/brands.py) and [`app/models/campaigns.py`](app/models/campaigns.py) with `BrandTheme` and `StepLandingPageConfig` classes. Add validation:
  - Theme: hex colors must match `#[0-9A-Fa-f]{6}`. Logo URL must be HTTPS. Custom CSS capped at 10KB.
  - Landing page: form_schema field names match `[a-z][a-z0-9_]*`. Field types restricted to `text | email | tel | url | textarea | select | checkbox`. Headline + body capped at 500 chars each (sanity).

* PATCH endpoints on [`app/routers/brands.py`](app/routers/brands.py) and [`app/routers/channel_campaign_steps.py`](app/routers/channel_campaign_steps.py) to update theme / landing_page_config respectively. Org isolation tested.

**Tests:**

* Pure tests for the Pydantic validators.
* Service tests for get/update with org isolation.
* Endpoint tests for the PATCH routes including cross-org guard.

---

### Slice 3 — Landing page render endpoint + page view tracking

**Goal:** when Entri Power proxies a request from `pages.acme.com/p/abc123` to our backend, we render a fully themed, personalized landing page server-side and emit `page.viewed`.

**File touchpoints:**

* New router: `app/routers/landing_pages.py`. Routes:
  - `GET /lp/{step_id}/{short_code}` — renders the landing page. Resolves `(step_id, short_code) → recipient via dmaas_dub_links`. Loads brand theme + step landing_page_config. Renders Jinja2 template. Calls `emit_event("page.viewed", channel_campaign_step_id=step_id, recipient_id=recipient_id, properties={user_agent, referrer})`. Returns HTML.
  - `GET /lp/{step_id}/_default` — the brand's default page when there's no recipient context (just the brand themed empty page; for testing).

* The Entri Power `applicationUrl` per the existing entri integration is `https://app.opsengine.run/lp/<step_id>` — so the path prefix `/lp/` is already aligned.

* New module: `app/services/landing_page_render.py`:
  - `render_landing_page(*, step_id, short_code) -> str` (returns rendered HTML).
  - Resolves recipient + brand + theme + page config, applies personalization, returns Jinja2-rendered HTML.
  - Personalization: simple `str.format(**{"recipient": {...}, "step": {...}, "brand": {...}})`. Missing keys render as empty string (defensive).

* New module: `app/services/landing_page_template.py`:
  - The Jinja2 template itself. One template for V1 — single column, logo top, headline, body, form below CTA. Theme variables substitute into CSS.
  - Template lives as a string constant or in a `templates/landing_page.html` file (your call).

* `emit_event()` wires the `page.viewed` event with full six-tuple resolved from the step row + recipient_id.

* Mount the router in [`app/main.py`](app/main.py).

**Required behavior:**

1. URL `/lp/{step_id}/{short_code}` resolves the recipient via `dmaas_dub_links` joined to step.
2. If `step_id` doesn't exist or `short_code` doesn't match a `dmaas_dub_links` row for that step → return a generic "page not found" themed to the brand (or 404 if brand can't be resolved either).
3. If recipient lookup succeeds but the recipient is in a different org than the step's brand's org → return 404. (This shouldn't happen given how minting works, but enforce defensively.)
4. Page render is server-side; no JS framework needed for V1. Form posts to `/lp/{step_id}/{short_code}/submit` (built in Slice 4).
5. `emit_event("page.viewed", ...)` fires on every render; rate-limit duplicate views from the same IP within 60 seconds at the application layer (don't double-count rapid refreshes).
6. Performance target: sub-300ms TTFB for a cold render. Cache brand theme + step config per request via the existing service-layer query patterns; don't re-query for static pieces.

**Tests:**

* `tests/test_landing_page_render.py` — pure render tests (template + personalization substitution).
* `tests/test_landing_page_router.py` — endpoint tests including missing recipient, missing step, cross-brand recipient.
* `tests/test_landing_page_emit.py` — verify `emit_event("page.viewed", ...)` is called with the right six-tuple + recipient_id.

---

### Slice 4 — Form submission + landing_page_submissions + lead capture

**Goal:** when a recipient fills the form, we validate against the step's form_schema, persist the submission, fire `page.submitted` + Dub `track_lead`, and return a thank-you response.

**File touchpoints:**

* New migration: `migrations/<timestamp>_landing_page_submissions.sql`:

  ```sql
  CREATE TABLE business.landing_page_submissions (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
      brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE RESTRICT,
      campaign_id UUID NOT NULL REFERENCES business.campaigns(id) ON DELETE RESTRICT,
      channel_campaign_id UUID NOT NULL REFERENCES business.channel_campaigns(id) ON DELETE RESTRICT,
      channel_campaign_step_id UUID NOT NULL REFERENCES business.channel_campaign_steps(id) ON DELETE RESTRICT,
      recipient_id UUID NOT NULL REFERENCES business.recipients(id) ON DELETE RESTRICT,
      form_data JSONB NOT NULL,
      source_metadata JSONB,             -- IP (hashed), user_agent, referrer, geo (if available)
      submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );

  CREATE INDEX idx_lps_org_brand ON business.landing_page_submissions(organization_id, brand_id);
  CREATE INDEX idx_lps_step ON business.landing_page_submissions(channel_campaign_step_id);
  CREATE INDEX idx_lps_recipient ON business.landing_page_submissions(recipient_id);
  CREATE INDEX idx_lps_submitted_at ON business.landing_page_submissions(submitted_at DESC);
  ```

* Add to `app/routers/landing_pages.py`: `POST /lp/{step_id}/{short_code}/submit`:
  - Validates submitted form_data against the step's `landing_page_config.cta.form_schema`.
  - Persists to `landing_page_submissions`.
  - Calls `emit_event("page.submitted", channel_campaign_step_id=..., recipient_id=..., properties={form_data, submission_id})`.
  - Calls Dub's `track_lead(click_id=..., customer_external_id=str(recipient_id), customer_name=form_data.get("name"), customer_email=form_data.get("email"))` if Dub click context is available (recovered from the dmaas_dub_links row's recent click events).
  - Returns JSON `{ok: true, thank_you_message: "..."}` for the frontend to render the thank-you state.

* New service: `app/services/landing_page_submissions.py`:
  - `record_submission(*, step_id, recipient_id, form_data, source_metadata) -> SubmissionResponse`.
  - `list_submissions_for_campaign(*, organization_id, campaign_id, limit, offset) -> list[SubmissionResponse]`.
  - `list_submissions_for_org(*, organization_id, brand_id?, channel_campaign_id?, limit, offset, from?, to?) -> list[SubmissionResponse]`.

* Pydantic models in `app/models/landing_page.py`:
  - `FormSchema`, `FormField`, `LandingPageSubmissionCreate`, `LandingPageSubmissionResponse`.

* Form validation:
  - Required fields present.
  - Field types match (`email` → valid email regex; `tel` → digit-and-formatting tolerant; `url` → valid URL).
  - No extra fields beyond schema (or store extras under a `_extras` key — your call; document).
  - Honeypot field (a hidden form field that bots fill but humans don't) — if non-empty, silently 200 without persisting + log a metric.

* Rate limiting: per-IP-per-step, 1 submission per 30 seconds. Use whatever rate-limit primitive hq-x already has (check for `slowapi` or similar; if none, in-memory dict keyed on `(ip_hash, step_id)` is fine for V1).

**Required behavior:**

1. Form_data persisted as JSONB matches the step's form_schema field names exactly. Extra fields rejected (or quarantined under `_extras`).
2. Source IP is **hashed** (SHA-256 with a per-env salt) before persisting. We never store raw IPs.
3. Honeypot rejection is silent (200 OK) — no signal to the bot that submission failed.
4. `emit_event("page.submitted", ...)` carries the form field NAMES in `properties.form_field_names` so RudderStack-side filtering can route based on which fields were filled, but does NOT carry the form_data values themselves (PII). Field VALUES live in our DB only.
5. Dub `track_lead` failure (Dub timeout, etc.) does NOT fail the submission. Log + continue.

**Tests:**

* `tests/test_landing_page_submission_service.py` — pure service tests: validate form_data against schema, persist, emit_event called.
* `tests/test_landing_page_submission_router.py` — endpoint tests: happy path, validation failure (missing required field, wrong type), honeypot trip (silent 200), rate limit (second submission in 30s rejected).
* `tests/test_landing_page_pii.py` — verify IP is hashed, raw IP never logged or persisted.

---

### Slice 5 — Lead analytics + dashboard endpoints

**Goal:** Lead capture surfaces in every analytics rollup and gets two new dashboard-shaped endpoints for the customer's "leads" view.

**File touchpoints:**

* Modify analytics services from Directive 1:
  - [`app/services/campaign_analytics.py`](app/services/campaign_analytics.py)
  - [`app/services/channel_campaign_analytics.py`](app/services/channel_campaign_analytics.py)
  - [`app/services/step_analytics.py`](app/services/step_analytics.py)
  - [`app/services/direct_mail_analytics.py`](app/services/direct_mail_analytics.py)
  - [`app/services/recipient_analytics.py`](app/services/recipient_analytics.py)

  Add to the `conversions` block in every direct_mail rollup:
  ```json
  "conversions": {
    "clicks_total": 0,
    "unique_clickers": 0,
    "click_rate": 0.0,
    "leads_total": 0,           // NEW: count of landing_page_submissions
    "unique_leads": 0,          // NEW: distinct recipients who submitted at least once
    "lead_rate": 0.0            // NEW: unique_leads / unique_clickers (0.0 if denom is 0)
  }
  ```

* Recipient timeline includes `page.viewed` and `page.submitted` events:
  - `page.viewed` from emit_event log (resolved through ClickHouse later or — for now — synthesized from `dmaas_dub_events` if click → page_view inference is reliable, OR add a `landing_page_views` table).
  - **Decision point for the agent:** simplest is a small `business.landing_page_views` table (id, step_id, recipient_id, viewed_at, source_metadata) populated by the render emit. Pure additive. Migration in Slice 5.
  - `page.submitted` reads from `landing_page_submissions`.

* New analytics endpoints in [`app/routers/analytics.py`](app/routers/analytics.py):
  - `GET /api/v1/analytics/campaigns/{campaign_id}/leads?from=&to=&limit=&offset=` — paginated submissions for the campaign with full form_data.
  - `GET /api/v1/analytics/leads?brand_id=&channel_campaign_id=&channel_campaign_step_id=&from=&to=&limit=&offset=` — org-wide leads with optional drilldown filters.

* Pydantic models in `app/models/analytics.py`: `LeadsListResponse`, `LeadResponse` (includes recipient summary + form_data).

**Required behavior:**

1. `lead_rate` divides by `unique_clickers`, not `unique_recipients_total` (a lead can only happen if a click happened first, so clickers is the right denominator).
2. Cross-org guard tested: leads for a recipient in org B never appear in org A's queries.
3. `form_data` returned in lead responses verbatim — it's the customer's data, they own it.
4. Pagination defaults `limit=100`, max `limit=500`, ordered by `submitted_at DESC`.

**Tests:**

* Conversion field property tests: `unique_leads <= leads_total`, `unique_leads <= unique_clickers`.
* Recipient timeline includes page events in chronological order.
* Leads endpoints with cross-org guard, pagination, drilldown filters.

---

### Slice 6 — Opinionated single-call DMaaS API

**Goal:** one endpoint that accepts everything needed to launch a campaign and does the full create+activate inline. The customer-facing surface that justifies "we run your direct mail."

**File touchpoints:**

* New router: `app/routers/dmaas_campaigns.py`. Mount at `/api/v1/dmaas`.

* `POST /api/v1/dmaas/campaigns` — request body:

  ```json
  {
    "name": "Q2 lapsed insurance MCs",
    "brand_id": "...",
    "send_date": "2026-05-15",
    "creative": {
      "lob_creative_payload": {
        "front_html": "...",
        "back_html": "..."
      }
    },
    "landing_page": {
      "headline": "Your premium audit is ready, {recipient.display_name}",
      "body": "We pulled your DOT and...",
      "cta": {
        "type": "form",
        "label": "Schedule",
        "form_schema": {"fields": [...]},
        "thank_you_message": "..."
      }
    },
    "use_landing_page": true,
    "destination_url_override": null,
    "recipients": [
      {"external_source": "fmcsa", "external_id": "123456", "display_name": "...", "mailing_address": {...}, "phone": "...", "email": "..."},
      ...
    ]
  }
  ```

  Response:

  ```json
  {
    "campaign_id": "...",
    "channel_campaign_id": "...",
    "step_id": "...",
    "external_provider_id": "cmp_...",
    "scheduled_send_at": "...",
    "recipient_count": 5000,
    "landing_page_url": "https://pages.acme.com/lp/<step_id>"
  }
  ```

* Internal flow (one transaction or multi-stage saga; document):
  1. Validate brand belongs to caller's org.
  2. Validate recipients (limit on count? — set a reasonable cap like 50,000 for V1).
  3. Validate `use_landing_page=true` requires `landing_page` block; `use_landing_page=false` requires `destination_url_override`. Mutually exclusive.
  4. Create `business.campaigns` row.
  5. Create `business.channel_campaigns` row (channel='direct_mail', provider='lob').
  6. Create `business.channel_campaign_steps` row with `creative_ref=null` (operator-supplied creative path), `channel_specific_config.lob_creative_payload=<from request>`, `landing_page_config=<from request>`.
  7. Bulk upsert recipients via the existing `recipients.bulk_upsert_recipients`.
  8. Materialize step audience (existing `materialize_step_audience`).
  9. Activate the step (existing `LobAdapter.activate_step` from Directive 1 — does Dub mint + Lob upload).
  10. Return the response payload.

* If `use_landing_page=true`, the destination URL minted with each Dub link is computed as `https://<brand.landing_page_domain_config.domain>/lp/<step_id>/<short_code>` (or the platform default if no custom domain is configured).
* If `use_landing_page=false`, the destination URL is `destination_url_override` verbatim.

* Idempotency: support an optional `Idempotency-Key` header. Persist key + initial response; replay returns the same response.

**Required behavior:**

1. The 5-call flow MUST still work — this is purely an additive convenience. Existing `/api/v1/campaigns`, `/api/v1/channel-campaigns`, `/api/v1/channel-campaign-steps`, `/materialize_audience`, `/activate` endpoints stay as-is.
2. Failures partway through return a structured error indicating which stage failed and which underlying ids were created (so retry can resume — though for V1, a clean retry from the start is acceptable; document).
3. Org isolation: `brand_id` must be in caller's org or 404.
4. Timeout: this call may take 15-60s for 5,000 recipients (Dub mint + Lob upload). Document that. Trigger.dev async refactor in Directive 3.
5. Audit trail: write the request body to `business.audit_log` (or whatever logging primitive exists) with `request_id` for observability.

**Tests:**

* `tests/test_dmaas_campaigns_api.py` — happy path with mocked Dub + Lob clients; verify all hierarchy rows are created with correct relationships; verify the response payload.
* Validation tests: missing `landing_page` when `use_landing_page=true`; missing `destination_url_override` when `false`; both supplied → 400.
* Idempotency test: same Idempotency-Key returns the same response without re-creating.
* Cross-org guard: brand in org B → 404.
* Recipient cap: 50,001 recipients → 400.

---

## 4. Definition of done (whole directive)

* All 6 slices merged to `main`.
* `uv run pytest -q` green at every step. Baseline before this directive is whatever the current main has after Directive 1 lands (~600+ tests).
* `uv run ruff check` clean on every file you touch.
* For Slice 1: a documented manual verification — register a real test domain via Entri Power + Dub `POST /domains`, confirm DNS resolves, confirm Dub serves the link, confirm Entri proxies to your local app.
* For Slice 3: a documented manual screenshot of a rendered landing page with theme + personalization substituted.
* For Slice 4: a documented manual form submission flow ending in a `landing_page_submissions` row + a `page.submitted` event in RudderStack Live Events.
* For Slice 6: a documented manual end-to-end — POST `/api/v1/dmaas/campaigns` with 3 recipients, see all hierarchy rows created, Lob test-mode pieces queued, Dub links minted, landing page renders for one recipient.
* New post-ship summary at `docs/dmaas-hosted-pages-pr-notes.md` describing what shipped, what's deferred to Directive 3 (Trigger.dev orchestration), any caveats / follow-ups (e.g., personalization token vocabulary, default landing page template appearance, rate-limit primitive choice).
* Update [`docs/entri-integration.md`](docs/entri-integration.md) to remove "pre-implementation" status and reflect the actual brand-domain wire-up.

---

## 5. Working order (recommended)

1. **Read** the canonical hierarchy doc [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md), [`docs/lob-integration.md`](docs/lob-integration.md), [`docs/entri-integration.md`](docs/entri-integration.md), and the Directive 1 post-ship notes [`docs/dmaas-foundation-pr-notes.md`](docs/dmaas-foundation-pr-notes.md) end to end.
2. **Read** the existing analytics services (slice 1 templates) and the existing Entri scaffolding ([`app/routers/entri.py`](app/routers/entri.py), [`app/providers/entri/`](app/providers/entri), [`app/models/entri.py`](app/models/entri.py), [`migrations/20260429T180000_entri_domain_connections.sql`](migrations/20260429T180000_entri_domain_connections.sql)).
3. **Investigate** what the existing `business.brands` row looks like — schema, columns, existing services. Slice 1 modifies it.
4. **Investigate** what rate-limiting primitive (if any) exists in hq-x today — check [`app/main.py`](app/main.py) middleware, look for `slowapi` in `pyproject.toml`. If nothing, the in-memory dict approach is fine for V1.
5. **Build Slice 1** (brand-domain wire-up). Smallest code-volume but operationally complex (real domain registration). Manual smoke before opening PR.
6. **Build Slice 2** (theme + landing_page_config schema). Pure data model + service work. Quick.
7. **Build Slice 3** (landing page render). Largest unknown — Jinja2 setup, template design, personalization. Manual screenshot in PR description.
8. **Build Slice 4** (form submission + capture). Builds on Slice 3.
9. **Build Slice 5** (lead analytics). Touches multiple existing analytics services. Single PR.
10. **Build Slice 6** (opinionated single-call API). The customer-facing surface. End-to-end smoke against test mode.
11. **Write the post-ship summary** + update [`docs/entri-integration.md`](docs/entri-integration.md).

If you hit a real architectural snag — especially around the URL routing for hosted pages on custom domains, or the Entri Power applicationUrl shape — STOP and surface it in the PR description rather than improvising.

---

## 6. Style + conventions

* Follow ruff config in `pyproject.toml` — line length 100, target py312, lint set `["E", "F", "I", "W", "UP", "B"]`.
* File header docstrings explain why the module exists, what's in scope, what's deferred.
* No new emojis in code, comments, or commit messages.
* Commit messages: short imperative subject under 72 chars. Blank line. 1–3 paragraphs. End with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
* PR descriptions: Summary, Six-tuple integrity, Cross-org leakage, Verification (where applicable), Test plan.
* Migration filenames use timestamp prefix (`YYYYMMDDTHHMMSS_<slug>.sql`).

---

## 7. Reference paths cheat sheet

| What | Where |
|---|---|
| Canonical hierarchy doc | [docs/campaign-rename-pr-notes.md](docs/campaign-rename-pr-notes.md) |
| Direct-mail integration depth | [docs/lob-integration.md](docs/lob-integration.md) |
| Tenancy + auth | [docs/tenancy-model.md](docs/tenancy-model.md), [app/auth/roles.py](app/auth/roles.py) |
| Entri integration spec | [docs/entri-integration.md](docs/entri-integration.md) |
| Entri runbook | [docs/entri-runbook.md](docs/entri-runbook.md) |
| Directive 1 post-ship notes | [docs/dmaas-foundation-pr-notes.md](docs/dmaas-foundation-pr-notes.md) |
| Lob HTTP client | [app/providers/lob/client.py](app/providers/lob/client.py) |
| Lob adapter (Directive 1's audience upload) | [app/providers/lob/adapter.py](app/providers/lob/adapter.py) |
| Dub HTTP client (has `POST /domains` wrapper) | [app/providers/dub/client.py](app/providers/dub/client.py) |
| Dub minting (existing) | [app/dmaas/step_link_minting.py](app/dmaas/step_link_minting.py) |
| Entri provider client | [app/providers/entri/](app/providers/entri) |
| Entri router (existing) | [app/routers/entri.py](app/routers/entri.py) |
| Entri models | [app/models/entri.py](app/models/entri.py) |
| Entri domain_connections migration | [migrations/20260429T180000_entri_domain_connections.sql](migrations/20260429T180000_entri_domain_connections.sql) |
| Six-tuple emit chokepoint | [app/services/analytics.py](app/services/analytics.py) |
| Step context resolver | [app/services/channel_campaign_steps.py](app/services/channel_campaign_steps.py) |
| Recipient + memberships | [app/services/recipients.py](app/services/recipients.py) |
| Brand service | [app/services/brands.py](app/services/brands.py) |
| Brand router | [app/routers/brands.py](app/routers/brands.py) |
| Analytics services (extend in Slice 5) | [app/services/campaign_analytics.py](app/services/campaign_analytics.py), [step_analytics.py](app/services/step_analytics.py), [recipient_analytics.py](app/services/recipient_analytics.py), [direct_mail_analytics.py](app/services/direct_mail_analytics.py) |

---

**End of directive.** Six slices, six PRs. After this lands, the platform is sellable as an enterprise DMaaS product. Directive 3 (Trigger.dev orchestration: async activation, multi-step scheduler, reconciliation crons, customer status webhooks) gets drafted after this lands and the surface is locked in.
