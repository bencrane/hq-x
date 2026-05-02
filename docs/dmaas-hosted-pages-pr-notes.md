# DMaaS Directive 2 — post-ship notes

Closes the directive at
[`docs/directives/2026-04-30-dmaas-hosted-pages-and-opinionated-api.md`](directives/2026-04-30-dmaas-hosted-pages-and-opinionated-api.md)
(in-tree under `.claude/worktrees/compassionate-torvalds-9defdd`
during development; pulled into `docs/directives/` on land).

After these six PRs the platform hosts the entire conversion funnel
on customer-owned domains: piece sent → piece delivered → QR scanned
→ Dub link fired → branded landing page rendered → form submitted →
lead captured → conversion analytics surfaced. The opinionated
single-call API collapses what was a 5-step setup flow into one
request, and leads land in our DB rather than the customer's
mailbox. Together this is the value-add layer that justifies the
$25K/mo enterprise tier.

## What shipped

| PR | Slice | Headline change |
|---|---|---|
| #51 | 1 | Brand-level Dub link host (POST /domains) + landing-page host (Entri Power) bindings on `business.brands`. Step minting reads the brand's Dub domain when no explicit override is supplied. |
| #52 | 2 | `business.brands.theme_config` + `business.channel_campaign_steps.landing_page_config` JSONB columns with Pydantic validators (BrandTheme, FormField, FormSchema, LandingPageCta, StepLandingPageConfig). |
| #53 | 3 | `GET /lp/{step_id}/{short_code}` server-side Jinja2 render with brand theme + per-step content + `{ns.path}` personalization tokens. Fires `page.viewed` through the existing `emit_event` chokepoint. New `business.landing_page_views` table feeds the recipient timeline. |
| #54 | 4 | `POST /lp/{step_id}/{short_code}/submit` validates against the step's form_schema, persists to `business.landing_page_submissions`, fires `page.submitted` (field NAMES only — values stay in our DB), redirects or renders themed thank-you page. Honeypot + 30s rate limit. |
| #55 | 5 | `leads_total` / `unique_leads` / `lead_rate` added to every direct-mail conversion rollup. Recipient timeline includes `page.viewed` + `page.submitted`. New `GET /api/v1/analytics/leads` and `GET /api/v1/analytics/campaigns/{id}/leads` dashboard endpoints. |
| #56 | 6 | `POST /api/v1/dmaas/campaigns` — one call replaces the 5-call create+activate flow. Existing surface stays as-is; this is purely additive. |

Stacked PR chain: 51 → 52 → 53 → 54 → 55 → 56. Each slice merges in
order against `main`.

## Conversion funnel

After Directive 1 + Directive 2:

```
piece.sent              ← Lob webhook (Directive 1)
piece.delivered         ← Lob webhook (Directive 1)
dub.click               ← Dub webhook (Directive 1)
page.viewed             ← landing-page render (Slice 3)
page.submitted          ← form submit (Slice 4)
dub.lead                ← Dub track_lead (Directive 1, plumbed but
                          not auto-fired by submit handler in V1)
```

`leads_total` analytic counts `page.submitted` rows directly;
`dub.lead` events stay separate so customers who wire their CRM into
`track_lead` themselves don't get double-counted.

## What's deferred (and why it's deferred, not skipped)

### Trigger.dev async orchestration

Directive 3. The opinionated single-call API stays synchronous in V1
— for ~5,000 recipients, a request takes 15–60s (Dub bulk mint + Lob
audience upload). The directive accepts this; the followup directive
splits the create + activate stages into a Trigger.dev task graph
(scheduler, reconciliation crons, customer status webhooks).

### Multi-step scheduler

Step N+1 still has to be activated by hand after step N's
`delay_days_from_previous` window. Multi-touch orchestration is part
of the Trigger.dev directive.

### `sales_total` analytics field

Stays out per Directive 1's reasoning — we don't have CRM visibility
unless a customer wires `track_sale` themselves. Surfacing zero would
imply visibility we don't have.

### Custom landing-page templates per step

V1 ships one Jinja2 layout (mobile-first single-column, theme
variables). Operators can override CSS via
`brand.theme_config.custom_css` (10 KB cap). Multi-template support
(quiz, multi-step form, video CTA, etc.) is its own directive.

### A/B testing on landing-page content

Out of scope. Future PR could add a `landing_page_variants` JSONB
keyed by recipient bucket; for now, run two campaigns.

### Form file uploads

V1 form_schema accepts text, email, tel, url, textarea, select,
checkbox. File upload + signature pad require S3 / R2 storage which
hq-x doesn't have today.

### ClickHouse backing for analytics

ClickHouse stays unprovisioned (out of scope per Directive 1).
RudderStack write fan-out from Directive 1 carries the new event
types automatically because they go through the same chokepoint.

## Personalization token vocabulary

Recognized tokens (all render as empty string when missing —
defensive, never raise):

| Token | Source |
|---|---|
| `{recipient.display_name}` | `business.recipients.display_name` |
| `{recipient.email}` | `business.recipients.email` |
| `{recipient.phone}` | `business.recipients.phone` |
| `{recipient.mailing_address.<key>}` | nested keys (e.g. `city`, `state`, `postal_code`) |

Future tokens: `{step.name}`, `{brand.name}` are reserved (lookup
returns empty for now). Adding them requires extending
`landing_page_render._resolve_token_path`'s context dict.

## Manual verification still needed

Each slice's PR ships with a manual-smoke checkbox unchecked. Two
end-to-end smokes need real provider credentials and were not run as
part of the merged PRs:

1. **Brand custom-domain end-to-end** (Slices 1 + 3 + 4):
   - Register a real domain via `POST /api/v1/brands/{id}/domains/dub`
     and `POST /api/v1/brands/{id}/domains/landing-page`.
   - Confirm Dub dashboard shows the new link host.
   - Confirm Entri Power's CNAME resolves to our app.
   - Mint a step's links, confirm short URLs use `track.<brand>.com/...`.
   - Visit the rendered landing page, confirm theme + personalization.
   - Submit the form, confirm a `landing_page_submissions` row + a
     `page.submitted` event in RudderStack Live Events.

2. **Opinionated-API end-to-end** (Slice 6):
   - `POST /api/v1/dmaas/campaigns` with 3 test-mode recipients.
   - Confirm all four hierarchy rows (campaign, channel_campaign,
     step, step_recipients) exist.
   - Confirm Lob test-mode pieces queued.
   - Confirm Dub links minted with the brand's domain.
   - Confirm landing page renders for one recipient.

Both should be run before announcing the directive is "done."

## Caveats / follow-ups

* **In-process rate limiter for form submissions** is a Python dict
  keyed by `(ip_hash, step_id)`. Sufficient for V1 single-process
  deployment; if hq-x ever scales horizontally, swap for a
  distributed primitive (Redis bucket, Upstash, etc.).
* **`landing_page_views.source_metadata`** stores hashed IPs only.
  The salt is per-env (`LANDING_PAGE_IP_HASH_SALT`); never copy
  hashes between environments — they'll deanon nothing.
* **`landing_page_submissions.form_data`** can carry arbitrary JSONB
  shapes per campaign. The customer dashboard needs to be aware
  there's no shared schema across campaigns; surface the per-campaign
  schema alongside the leads list.
* **Honeypot field name (`company_website`)** is hardcoded in the
  template + submit handler. If this leaks and bots learn to skip
  it, rotate the field name (one-line change in both files) and
  invalidate any current bot fingerprint.
* **`StepLandingPageConfig.font_family` is restricted to a known
  list** in `app/models/brands.py`. Operators who want a custom
  webfont must extend `_KNOWN_FONT_FAMILIES`; the render path falls
  through to system-ui regardless.
* **Slice 6 imports `brand_domains_svc` lazily** so it can land
  independently of Slice 1 if PR ordering shifts. Once both are
  merged the conditional is harmless; remove on a future cleanup
  pass if the codebase stabilizes.

## Reference paths cheat sheet

| What | Where |
|---|---|
| Brand-domain bindings | [app/services/brand_domains.py](../app/services/brand_domains.py) |
| Brand-domain endpoints | [app/routers/brand_domains.py](../app/routers/brand_domains.py) |
| Brand theme model | [app/models/brands.py](../app/models/brands.py) |
| Step landing-page config models | [app/models/campaigns.py](../app/models/campaigns.py) (FormField, FormSchema, LandingPageCta, StepLandingPageConfig) |
| Landing-page render | [app/services/landing_page_render.py](../app/services/landing_page_render.py) |
| Landing-page Jinja2 template | [app/services/landing_page_template.py](../app/services/landing_page_template.py) |
| Landing-page router | [app/routers/landing_pages.py](../app/routers/landing_pages.py) |
| Form submission validation | [app/services/landing_page_submissions.py](../app/services/landing_page_submissions.py) |
| Lead analytics endpoints | [app/routers/analytics.py](../app/routers/analytics.py) (org_leads / campaign_leads) |
| Single-call DMaaS API | [app/routers/dmaas_campaigns.py](../app/routers/dmaas_campaigns.py) |
| Conversions Pydantic | [app/models/analytics.py](../app/models/analytics.py) |
| Migrations | `migrations/20260430T1[6-9]0000_*.sql` |
