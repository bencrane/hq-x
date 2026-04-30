# DMaaS foundation — handoff for the next strategic / directive agent

> Audience: another AI agent (or engineer) drafting the next DMaaS
> directive or owning the strategic workstream. This document is the
> single source of truth for what shipped, what was deferred, and
> what's blocking end-to-end activation right now. Read this before
> drafting Directive 2.

Date: 2026-04-30. Branch: `main` (all referenced PRs merged).

---

## TL;DR

The DMaaS foundation directive
([`docs/directives/2026-04-30-dmaas-foundation-lob-send-and-dub-conversion.md`](directives/2026-04-30-dmaas-foundation-lob-send-and-dub-conversion.md))
shipped in five PRs (#45 → #49). The send-path plumbing is wired and
tested; the click-side analytics pipeline is fully fanned out
through `emit_event`. Two material surprises surfaced during the
end-to-end smoke and are flagged below — both must be resolved before
the foundation can be called "ship-ready" for paying customers.

| Status | Component |
|---|---|
| ✅ shipped + tested | Dub click/lead/sale → `emit_event` analytics fan-out |
| ✅ shipped + tested | Conversion columns on every direct-mail analytics endpoint |
| ✅ shipped + tested | Recipient timeline includes Dub events |
| ✅ shipped + tested | LobAdapter audience CSV upload flow |
| ✅ shipped + smoke-confirmed | `/v1/campaigns` create with `schedule_type` |
| ⚠️ blocked on Lob support | `/v1/creatives` returning HTTP 500 on minimal payloads |
| ⚠️ deferred to its own directive | `dmaas_designs → Lob creative` renderer |
| 📋 deferred to Directive 2 | Hosted landing pages + custom domains + single-call API |

**You cannot yet activate a step end-to-end against real Lob.** The
campaign creates fine; the creative create gets stuck on a Lob 500.
See "Open issues" below.

---

## What shipped

All merged to `main`. Test-suite baseline after the merges: 658
passing, 1 pre-existing deprecation warning unrelated to this work.

### PR #45 — Slice 2: Dub webhooks → `emit_event`

[hq-x#45](https://github.com/bencrane/hq-x/pull/45) ·
`f66b03a` · `app/webhooks/dub_processor.py`,
`tests/test_dub_webhook.py`

After projecting a Dub webhook into `dmaas_dub_events`, look the
`dub_link_id` up in `dmaas_dub_links` and call
`services/analytics.emit_event` so the event flows through the same
six-tuple-tagged pipeline as Lob piece events.

* `link.clicked → dub.click`, `lead.created → dub.lead`,
  `sale.created → dub.sale`.
* Only emits on `status='processed'`; `duplicate_skipped` paths
  already emitted.
* Unattributed links (no row in `dmaas_dub_links`, or
  `channel_campaign_step_id IS NULL`) → log debug, skip emit, return
  202 with the event still written.
* Lookup AND emit are wrapped in try/except — neither propagates up.

### PR #46 — Slice 3: Conversion columns

[hq-x#46](https://github.com/bencrane/hq-x/pull/46) · `f3e98ce` ·
`app/services/{campaign,channel_campaign,step,direct_mail}_analytics.py`,
`app/models/analytics.py`

Every direct-mail analytics endpoint returns a `conversions` block
alongside `totals`/`funnel`/`outcomes`:

```json
"conversions": {
  "clicks_total": 12,
  "unique_clickers": 4,
  "click_rate": 0.4
}
```

Sourced from `dmaas_dub_events` (filtered to `link.clicked`) joined
through `dmaas_dub_links` to `business.channel_campaign_steps`. Org
isolation flows through `s.organization_id`. The `click_rate`
denominator is recipients with a delivered/in-transit-family piece in
the same window. Divide-by-zero guarded.

`leads_total` and `sales_total` **intentionally not surfaced**.
`leads_total` requires landing-page form-submit (Directive 2).
`sales_total` requires the customer to wire their CRM into Dub's
`track_sale`, which we don't promise as a platform feature. Surfacing
zero fields would imply visibility we don't have.

### PR #47 — Slice 4: Recipient timeline includes Dub events

[hq-x#47](https://github.com/bencrane/hq-x/pull/47) · `4df56bd` ·
`app/services/recipient_analytics.py`, `tests/test_recipient_analytics.py`

`GET /api/v1/analytics/recipients/{recipient_id}/timeline` now
interleaves `dmaas_dub_events` with `direct_mail_piece_events` and
step-membership transitions in chronological order. **"They got the
postcard AND scanned it"** is now visible per recipient.

* Looks up via `dmaas_dub_events → dmaas_dub_links →
  channel_campaign_steps`. Only links bound to a step are surfaced.
* Org isolation: `s.organization_id` (defence in depth — recipient is
  already org-scoped).
* Each event gets `provider='dub'`, `artifact_kind='dub_link'`,
  `artifact_id=dub_link_id`, with click metadata in `metadata`.

### PR #48 — Slice 1: LobAdapter audience upload (Option A)

[hq-x#48](https://github.com/bencrane/hq-x/pull/48) · `452b33d` ·
`app/providers/lob/adapter.py`, `app/services/lob_audience_csv.py`,
`docs/lob-integration.md`, `docs/dmaas-foundation-pr-notes.md`

Drives the full Lob flow:

```
1. Mint per-recipient Dub links (idempotent on step + recipient)
2. POST /v1/campaigns                  → cmp_*
3. POST /v1/creatives                  → crv_*  (bound to cmp_*)
4. SELECT memberships + recipients + dub_links → audience CSV
5. POST /v1/uploads                    → upl_*
6. POST /v1/uploads/{upl_id}/file      → ships the CSV to Lob
```

Idempotency: Lob keys derived from `step.id`
(`hqx-step-{step_id}-campaign` / `-creative`); upload + file gated by
`external_provider_metadata.lob_upload_id`. Partial failures leave the
step in `activating` status with whichever ids did succeed persisted;
retry resumes from the next sub-step. The
`pending → activating → scheduled` transition is allowed in
`services/channel_campaign_steps.activate_step` for this.

### PR #49 — Smoke fix: `schedule_type` on campaign create

[hq-x#49](https://github.com/bencrane/hq-x/pull/49) · `8742fc1` ·
`app/providers/lob/adapter.py`, `scripts/smoke_lob_activate_step.py`

Real bug surfaced by the smoke. See "Architectural surprises" §1.

---

## Architectural decisions you should know about

### 1. Renderer is deferred to its own directive (Option A)

**Decision:** the Lob audience-upload path uses operator-supplied
creative (`channel_specific_config.lob_creative_payload`) for V1.
`dmaas_designs.id` (= `creative_ref` on the step) is preserved as
metadata for the future renderer; `LobAdapter` does not consume it.

**Why:** building `dmaas_designs → Lob creative HTML` is genuinely a
multi-PR project (HTML synthesis from `content_config` +
`resolved_positions`, CSS positioning at solver-computed pixel
coordinates, font + image asset hosting, panel-aware self-mailer
geometry). Inlining a half-built renderer in Slice 1 would have
violated the directive's §1.3 rule and shipped fragile code.

**Consequence for the next directive:** the customer-facing flow has
a manual creative-prep step. The honest framing: **you cannot yet do
`dmaas_designs.id → printed postcard` end-to-end without manual
creative prep.** Plumbing exists; renderer in the middle is manual.
This is the single most important deferral on the roadmap.

**Where it lands:** its own directive. Probably "DMaaS Renderer"
between Directive 1 (this) and Directive 2 (hosted landing pages).
Recommend Remotion or a server-side React renderer running in a
worker; need an asset host (S3 / Cloudflare R2 / Lob's own asset
upload) for fonts and images.

### 2. Step↔Lob campaign 1:1 is canonical

A single hq-x umbrella campaign with three direct-mail steps under one
`channel_campaign` produces three Lob `cmp_*` objects. Lob has no
concept of multi-touch sequences. Don't relitigate this in Directive 2.

### 3. ClickHouse stays out of scope

Postgres-only. All new analytics columns carry `"source": "postgres"`.
ClickHouse port is in `AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md` but not
on the foundation roadmap.

### 4. `leads_total` / `sales_total` are intentionally not surfaced

They imply visibility we don't have. `leads_total` lands with the
form-submit pipeline in Directive 2. `sales_total` is a maybe-never —
it requires customers to wire their CRM into Dub's `track_sale`.

---

## Architectural surprises discovered during smoke

These were not in the directive, the Lob API reference docs, or
existing hq-x knowledge. They surfaced when running
`scripts/smoke_lob_activate_step.py` against real Lob and **must be
called out in any future directive that touches this surface.**

### Surprise 1: Lob's campaigns API requires LIVE mode

`LOB_API_KEY_TEST` returns:

```
HTTP 403: You do not have permission to access this endpoint.
This endpoint requires live mode, but test mode was used.
```

…on `/v1/campaigns`. The directive's "Lob test-mode end-to-end smoke"
verification step is **not actually possible** as worded. There is no
test mode for the campaigns / creatives / uploads flow.

**Workaround we used:** smoke against `LOB_API_KEY` (live) but stop
short of `/v1/campaigns/{id}/send` so no pieces actually print or
bill. Created campaign + creative + upload are free artifacts that can
be deleted.

**Implication for Directive 2 / future directives:** anything that
needs end-to-end Lob verification has to plan for the live-key reality.
The smoke script in `scripts/smoke_lob_activate_step.py` documents the
pattern.

### Surprise 2: `/v1/campaigns` requires `schedule_type`

Not in the API reference doc as required, but Lob returns:

```
HTTP 422: schedule_type is required
```

…on every `POST /v1/campaigns` without it. Today the only accepted
value (without an accompanying date) is `"immediate"`.

**Fixed in #49.** Adapter now defaults to `"immediate"` with operator
override via `channel_specific_config.schedule_type`.

**This was a latent bug pre-Slice-1.** The pre-Slice-1 adapter had the
same gap, but it was masked because activation never ran end-to-end
before #48. Worth keeping in mind if the strategic agent encounters
"this used to work in some other repo" — it didn't actually work.

---

## Open issues (blockers for "foundation ship-ready")

### Issue 1: `/v1/creatives` returns HTTP 500 on minimal payloads (BLOCKER)

**Status:** unresolved. Needs Lob support engagement.

**Reproduction:**

```bash
doppler run -- bash -c 'curl -u "$LOB_API_KEY:" -X POST \
  https://api.lob.com/v1/creatives \
  -H "Content-Type: application/json" \
  -d "{
    \"campaign_id\":\"cmp_<id>\",
    \"resource_type\":\"postcard\",
    \"front\":\"<html><body>F</body></html>\",
    \"back\":\"<html><body>B</body></html>\",
    \"details\":{\"size\":\"4x6\"},
    \"from\":{\"name\":\"HQ\",\"address_line1\":\"185 Berry St\",
             \"address_city\":\"San Francisco\",
             \"address_state\":\"CA\",\"address_zip\":\"94107\"}
  }"'
```

Lob returns:

```json
{
  "error": {
    "message": "Internal Error Occurred. Please contact support@lob.com.",
    "status_code": 500,
    "code": "internal_server_error"
  }
}
```

**Variations tried, all 500:** with/without `from`; with/without
`mailing_class` in details (drops to 422 with explicit "not allowed");
empty `details: {}`; HTML payloads small and well-formed.

**Hypotheses to investigate:**

1. `details` may have undocumented required fields for postcards (e.g.
   `auto_size_to_safe_zone`, `compatible_creative`).
2. HTML payload format may need to follow Lob's specific template
   conventions (merge-variable syntax, etc.).
3. `front`/`back` may need to be PDF asset URLs rather than inline
   HTML when the campaign uses `/uploads` rather than per-piece
   creates.
4. Lob's account / brand may need a config flag for the campaigns API
   we haven't enabled.

**Next step:** open a Lob support ticket with the exact payload + the
500 response. Ask which `details` fields are required for postcard
creatives and whether HTML templates work with the campaigns API
specifically (vs. only per-piece). Until this is resolved, the
foundation **cannot do end-to-end Lob activation** — it stops at
`status='activating'` with `external_provider_id` set but no creative.

The adapter's partial-failure handling **does** correctly leave the
step in a retry-able state, so once Lob is unblocked, a retry of
`activate_step` on the existing step will resume from the creative
create. Verified during the smoke (campaign id `cmp_aa7ce5473321356d`
was created and then deleted as cleanup).

### Issue 2: Smoke can't run end-to-end without Lob support fix

`scripts/smoke_lob_activate_step.py` is the harness. It currently
demonstrates:

1. ✅ Campaign create succeeds.
2. ✅ Partial-failure semantics work (status → `activating` with
   campaign id persisted).
3. ❌ Creative create blocked on the 500 above.
4. (untested) Upload create + file post.

Once Issue 1 is unblocked, the script should produce a fully scheduled
campaign with all three Lob ids (`cmp_*`, `crv_*`, `upl_*`) and a
queued audience.

---

## Verification status

| Check | Status |
|---|---|
| Unit tests for all 5 PRs | ✅ 658 passing on main |
| `LobAdapter` unit tests with mocked Lob client | ✅ 21 passing |
| CSV builder unit tests | ✅ 14 passing |
| Real-Lob `/v1/campaigns` smoke | ✅ confirmed (`schedule_type` fix verified) |
| Real-Lob `/v1/creatives` smoke | ❌ blocked on Lob 500 (Issue 1) |
| Real-Lob `/v1/uploads` smoke | ⚠️ untested (gated behind creatives) |
| RudderStack `dub.click` Live Events | ⚠️ user-side, not run in this branch |
| Multi-org leakage tests | ✅ included in every Slice 3/4 service test |

---

## What Directive 2 should be aware of

### Hosted landing pages + custom domains (the original Directive 2 scope)

Customer flow today: customer supplies `landing_page_url` directly;
we mint Dub short URLs pointing at it. Directive 2 was originally
about taking that step over — host the landing page ourselves on a
customer-provisioned subdomain.

* Custom domains are wired via the Entri integration (PR #33).
* Landing-page form-submit pipeline is what unlocks the `leads_total`
  analytics field.
* Single-call DMaaS API (one POST that creates campaign +
  channel_campaign + step + materializes audience + activates) is
  the value-add abstraction. The 5-call flow stays underneath.

### Renderer directive (between this and Directive 2)

The Option-A deferral makes a renderer directive a hard precondition
for any meaningful customer-facing product. Don't ship Directive 2
without addressing this — otherwise customers still need to prepare
HTML / PDF creative externally and our "single-call DMaaS API" still
requires a `lob_creative_payload` blob from them, which defeats the
whole abstraction.

Recommended scoping for the renderer directive:

1. Postcard MVP only (4x6, 6x9). Defer letters and self-mailers.
2. Asset host: pick one (S3 / R2 / Lob's own
   `/v1/upload-templates` if it exists). Decide before writing the
   directive.
3. Font handling: web-safe + one custom font slot via @font-face data
   URLs in the HTML. Avoid asset-hosted fonts in V1.
4. Acceptance: an end-to-end smoke that takes `dmaas_designs.id` →
   passes through Lob → produces a printed postcard with the
   recipient's name and a working QR code. Run on Lob LIVE mode (see
   Surprise 1).

### What NOT to relitigate

* Step ↔ Lob campaign 1:1 (canonical, see `docs/campaign-rename-pr-notes.md`).
* Postgres-only analytics (the directive locked this in §1.5).
* `leads_total` / `sales_total` not surfaced (see "Architectural
  decisions" §4).
* `dmaas_designs.id` staying on the step as `creative_ref` even
  though the adapter doesn't consume it. Future-renderer hook.

---

## Reference paths cheat sheet

| What | Where |
|---|---|
| Original directive | [`docs/directives/2026-04-30-dmaas-foundation-lob-send-and-dub-conversion.md`](directives/2026-04-30-dmaas-foundation-lob-send-and-dub-conversion.md) |
| Post-ship summary | [`docs/dmaas-foundation-pr-notes.md`](dmaas-foundation-pr-notes.md) |
| This handoff | [`docs/dmaas-foundation-handoff.md`](dmaas-foundation-handoff.md) |
| Canonical campaign hierarchy | [`docs/campaign-rename-pr-notes.md`](campaign-rename-pr-notes.md) |
| Lob integration depth | [`docs/lob-integration.md`](lob-integration.md) |
| LobAdapter | [`app/providers/lob/adapter.py`](../app/providers/lob/adapter.py) |
| CSV builder | [`app/services/lob_audience_csv.py`](../app/services/lob_audience_csv.py) |
| Smoke harness | [`scripts/smoke_lob_activate_step.py`](../scripts/smoke_lob_activate_step.py) |
| Lob API reference (local copy) | `/Users/benjamincrane/api-reference-docs-new/lob/api-reference` |

## Recommended next actions for the strategic agent

In order:

1. **Open a Lob support ticket** with the `/v1/creatives` 500
   reproduction. Until this is resolved, the foundation isn't usable
   end-to-end. This is the critical path.
2. **Re-run the smoke** once Lob unblocks. Goal: a fully scheduled
   campaign with all three ids returned and a queued audience.
3. **Draft the renderer directive** (postcard MVP). The Option-A
   deferral is the biggest gap in the customer story.
4. **Then draft Directive 2** (hosted landing pages, custom domains,
   single-call API). It's not blocked by 1 or 3 from a code
   perspective, but its customer narrative falls flat without them.
5. **Confirm the RudderStack `dub.click` Live Events check** the
   directive listed for Slice 2 verification. Quick — should take
   under five minutes once a real Dub click fires on a step-minted
   link.

If you spawn a debug agent to chase the Lob 500, give it
`scripts/smoke_lob_activate_step.py` as the harness and tell it to
iterate on the `lob_creative_payload` shape until it gets a 200, then
report back what worked.
