# Directive — DMaaS foundation: Lob send path + Dub conversion analytics

**For:** an implementation agent shipping the foundational gaps that block hq-x from operating as a paid DMaaS platform. Worktree path: `/Users/benjamincrane/hq-x` (or any worktree under it). Branch from `main`.

This is the FIRST of two DMaaS-product directives:

* **Directive 1 (this file)** — fixes what's plumbed but doesn't actually work end-to-end: Lob can't currently send pieces at scale, and Dub clicks don't flow into analytics. After this lands, the platform sends + tracks the full direct-mail loop using a customer-supplied destination URL.
* **Directive 2 (later)** — the value-add layer: hosted landing pages, custom domain provisioning via entri, opinionated single-call DMaaS API. Drafted after this lands.

---

## 0. Why this directive exists

Two ship-blockers were surfaced when designing the customer-facing flow:

1. **Lob send path is a no-op for non-trivial campaigns.** [`app/providers/lob/adapter.py:142`](app/providers/lob/adapter.py:142) `activate_step()` creates a Lob campaign object via `POST /v1/campaigns` and stops there. The actual sending — uploading the audience CSV via `/v1/uploads` so Lob mints pieces server-side — is a `# Future PR` comment ([adapter.py:153](app/providers/lob/adapter.py:153)). Today, activating a 5,000-recipient step creates a Lob campaign object with **zero pieces printed**. The fallback (per-piece routes in [`app/routers/direct_mail.py`](app/routers/direct_mail.py)) is 5,000 sequential calls — exactly the scale problem the recent Dub bulk-mint work just solved.
2. **Dub click webhooks don't flow through `emit_event()`.** [`app/webhooks/dub_processor.py`](app/webhooks/dub_processor.py) writes to `dmaas_dub_events` via `project_dub_event` but never calls `emit_event()`. Net effect: clicks are not fanned out to RudderStack, are not visible in any analytics endpoint, and are not in the recipient timeline. Dub minting happens at scale but the click event — the headline conversion signal for direct mail — is invisible to the rest of the system.

After this directive ships, both gaps close. The platform sends real pieces + tracks the full delivered → clicked funnel.

---

## 1. Architectural decisions locked in (do not relitigate)

These were settled in design conversation. Treat as constraints.

### 1.1 No reusable Lob templates

Each step push is self-contained: render the `dmaas_designs` row to a creative artifact at push time, send it inline as the Lob campaign's creative payload (HTML or asset URL). **Do NOT add a `dmaas_designs.lob_template_id` column.** Do NOT call Lob's `/v1/templates` endpoints in this work. This keeps the lifecycle simple — one creative per step, regenerated each push, no template state on Lob's side that can drift from our DB.

The Lob client's template wrappers ([`client.py:814-1013`](app/providers/lob/client.py:814)) stay. They're available if a future use case wants them. This directive doesn't use them.

### 1.2 Step ↔ Lob campaign 1:1 (already canonical)

This is from [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) — restated for emphasis:

```
business.campaigns               (umbrella outreach)
  └── business.channel_campaigns (one per channel run)
        └── business.channel_campaign_steps   ← each = ONE Lob campaign
              └── per-step: ONE /v1/campaigns + ONE /v1/uploads
                            → N pieces minted server-side by Lob
                            → per-piece webhooks come back tagged
```

A single hq-x umbrella `campaign` with three direct-mail steps under one channel_campaign produces **three** Lob campaign objects. Lob has no concept of multi-touch sequences — that's our orchestration layer.

### 1.3 Renderer is opaque

The boundary is `dmaas_designs.id → [renderer] → creative artifact (HTML or asset URL)`. Whatever the existing DMaaS path produces today is what the Lob adapter consumes. **Do NOT introduce a new renderer abstraction or wire Remotion / headless-Chrome / anything else in this directive.** A future renderer swap is a one-module change behind the same boundary.

If the existing path doesn't produce a Lob-shaped artifact yet, build the smallest shim that turns whatever it does produce into Lob's required `front_html` / `back_html` (for postcards/letters) or asset URL (for PDFs). Document any assumptions in the PR.

### 1.4 No hosted landing pages, no custom domains, no single-call API

All deferred to Directive 2. For this directive:

* Customers (or the operator on a customer's behalf) supply a `destination_url` in `channel_specific_config.landing_page_url` per step — same as today.
* Dub minting uses that URL as the click destination — same as today.
* No new endpoints for "create campaign in one call." The existing 5-call flow stays.

### 1.5 ClickHouse stays out of scope

Same as the analytics directive. All new analytics columns are Postgres-only. Response payloads carry `"source": "postgres"`.

### 1.6 Only surface analytics fields we actually have data for

Don't add `leads_total` / `sales_total` (or any other always-zero placeholder) to the conversion shape in Directive 1. Surfacing zero fields implies visibility we don't have. `leads_total` lands in Directive 2 with the form-submit pipeline. `sales_total` is not on the roadmap — it requires the customer to wire their CRM into Dub's `track_sale`, and we don't promise that as a platform feature.

---

## 2. Hard rules (carry forward)

1. **Six-tuple is sacred.** Every `emit_event()` call carries `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel, provider)`. Per-recipient events also carry `recipient_id`.
2. **Org isolation tested per endpoint.** Any new or modified endpoint gets a "user from org A asking about org B's resource → 404" test.
3. **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, `track()` (RudderStack) never raise into the caller.
4. **No silent assignment.** If a query needs an id that doesn't belong to the caller's org, return 404. Don't fall back.
5. **Recipients are organization-scoped only.** Recipient lookups MUST combine `recipient_id` AND `organization_id` in the same WHERE clause — never two-step.
6. **Provider adapters are the emit chokepoint.** Don't bypass `emit_event()`.
7. **Mind the four-level naming.** `campaign_id` = umbrella; `channel_campaign_id` = channel-typed execution unit; `channel_campaign_step_id` = ordered step (= one Lob campaign for direct_mail); `recipient_id` = identity.
8. **Idempotency for external API calls.** Re-running `activate_step` after a partial failure must not double-create Lob campaigns or double-upload audiences. Use `app/providers/lob/idempotency.py` for keys.
9. **No new Postgres migrations** unless absolutely necessary. If a slice tempts you toward one, stop and surface it in the PR description first.
10. **No frontend, no doc-site updates.** Backend only.

---

## 3. Slices to ship (in order)

Each slice is one commit + one PR against `main`. PRs may stack on the same branch or each branch from `main` — your call. Land each before opening the next.

### Slice 1 — Lob audience upload wired into `activate_step`

**Goal:** activating a direct_mail step actually causes Lob to print pieces. Today it doesn't.

**File touchpoints:**

* [`app/providers/lob/adapter.py`](app/providers/lob/adapter.py) — extend `activate_step()`. After the existing Dub mint + Lob `POST /v1/campaigns`, attach the rendered creative to the campaign + upload the audience CSV.
* [`app/providers/lob/client.py`](app/providers/lob/client.py) — verify `create_upload` ([:1624](app/providers/lob/client.py:1624)) handles all required fields. Likely small or no edits.
* New: `app/services/lob_audience_csv.py` — pure builder that turns a step's memberships + recipients + Dub links into Lob-shaped CSV rows.
* New (or inline in adapter if trivial): `app/services/lob_creative_render.py` — adapter from `dmaas_designs.id` to the Lob creative payload (HTML or asset URL, depending on what the existing DMaaS path produces).
* `app/providers/lob/idempotency.py` — reuse the existing pattern; key on `(channel_campaign_step_id, "audience_upload")` so retries don't double-upload.

**Required behavior:**

1. **Pre-flight checks** stay as today: step is `pending`, channel is `direct_mail`, `creative_ref` is set, `landing_page_url` is set.
2. **Dub minting** stays first. The CSV needs the per-recipient Dub URLs.
3. **Render the creative** from `dmaas_designs.id` — investigate the existing renderer in `app/dmaas/` or wherever it lives. If nothing produces a Lob-shaped artifact yet, build the smallest shim that does. The output is either:
   * `{front_html, back_html}` for postcards/letters, OR
   * `{front_pdf_url, back_pdf_url}` for asset-based pieces.
   Whatever the existing path supports.
4. **Create the Lob campaign** with the rendered creative inline in the payload. Currently the campaign is created with metadata only — extend the payload.
5. **Build the audience CSV** — one row per `channel_campaign_step_recipients` membership in `pending` status. Each row has at minimum:
   * Recipient name (from `recipients.display_name`)
   * Mailing address fields (from `recipients.mailing_address` JSONB — line1, line2, city, state, zip, country)
   * `to_url` or merge token for the recipient's Dub link (looked up from `dmaas_dub_links` by `(channel_campaign_step_id, recipient_id)`)
   * Any other merge tokens the creative references (TBD — check what `dmaas_designs` outputs)
6. **POST `/v1/uploads`** linked to the freshly-created Lob campaign. Use an idempotency key like `step_<step_id>_audience` so retries are safe.
7. **Persist the upload id** on the step row — extend `external_provider_metadata` to include `lob_upload_id`. Do NOT add a new column for this.
8. **Handle partial failure correctly:**
   * Dub mint succeeds, Lob campaign create fails → step stays `pending`, retry the whole thing (Dub mint is idempotent already; Lob campaign create needs its own idempotency key).
   * Dub mint succeeds, Lob campaign create succeeds, Lob upload fails → step is left in an `activating` state (or similar — see the existing status enum). Retry should re-attempt the upload only, not re-create the campaign.
   * All succeed → step transitions to `scheduled`, memberships flip pending → scheduled (existing behavior).
9. **Webhook side needs no changes.** Lob mints pieces from the upload server-side and sends per-piece webhooks tagged with the metadata we put on the upload row. The existing projector ([`app/webhooks/lob_processor.py`](app/webhooks/lob_processor.py)) already handles them via `external_piece_id` resolution.

**Tests:**

* `tests/test_lob_audience_csv.py` — pure tests for the CSV builder. Verify per-recipient row shape, address fields from `mailing_address` JSONB, Dub URL substitution, behavior when `mailing_address` is missing fields.
* `tests/test_lob_adapter_audience_upload.py` — full `activate_step` flow with the Lob HTTP client mocked. Verify:
  * Successful activation: Dub mint → Lob campaign create with creative → Lob audience upload → step transitions to `scheduled`.
  * Idempotent retry from "Lob campaign created, upload failed" state: re-running `activate_step` only re-attempts the upload.
  * Failure to render creative → step stays `pending`, `LobActivationResult(status="failed")`.
  * Partial Lob upload failure (Lob returns a per-row error in the upload response) → step stays `activating`, error surfaced in `LobActivationResult.metadata`.
* If `app/services/lob_creative_render.py` (or similar) ends up doing meaningful work, give it its own pure tests.

**Verification step (in PR description):**

Run a manual end-to-end smoke against Lob test mode:

* Create a campaign + channel_campaign + step with a real `dmaas_designs.id` and a 3-recipient audience.
* Activate.
* Confirm in Lob's dashboard that the campaign exists with the creative attached and 3 pieces are queued.
* Confirm the step row has `external_provider_id` (Lob `cmp_*`), `external_provider_metadata.lob_upload_id`, and status `scheduled`.
* Confirm the per-recipient `direct_mail_pieces` rows arrive via webhook within a minute or two, all carrying the right `channel_campaign_step_id` + `recipient_id`.

Document the test campaign id + upload id in the PR description so the verification is reproducible.

---

### Slice 2 — Dub clicks → `emit_event()`

**Goal:** every Dub webhook (click, lead, sale) becomes a fully-tagged analytics event flowing through `emit_event()` → log + RudderStack + future ClickHouse.

**File touchpoints:**

* [`app/webhooks/dub_processor.py`](app/webhooks/dub_processor.py) — after the existing `dmaas_dub_events` insert via `project_dub_event`, resolve the `dub_link_id` to step + recipient and call `emit_event()`.
* [`app/services/analytics.py`](app/services/analytics.py) — if there's an event-name allowlist, add `dub.click`, `dub.lead`, `dub.sale` (or whatever event-name shape Dub uses; investigate the existing `dmaas_dub_events.event_type` values to align). If no allowlist exists, no change.

**Required behavior:**

1. After `project_dub_event` succeeds, look up the `dub_link_id` in `dmaas_dub_links` to get `(channel_campaign_step_id, recipient_id, channel_campaign_id, campaign_id, brand_id, organization_id)`. The `dmaas_dub_links` row carries the full hierarchy from the original mint.
2. If the `dub_link_id` is NOT in `dmaas_dub_links` (e.g., a link minted outside our DMaaS flow, or operator-created via the bulk routes), DO NOT call `emit_event` — just write the dub_event and log a debug-level "unattributed Dub event" line. Return successfully (don't fail the webhook).
3. If the `dub_link_id` IS in `dmaas_dub_links`, call:
   ```python
   await emit_event(
       event_name=f"dub.{normalized_event_type}",  # "dub.click", "dub.lead", "dub.sale"
       channel_campaign_step_id=link.channel_campaign_step_id,
       recipient_id=link.recipient_id,
       properties={
           "dub_link_id": link.dub_link_id,
           "dub_event_id": dub_event_id,
           "click_url": event.url,         # the URL clicked (= the destination_url at the time)
           "country": event.country,        # whatever Dub's event payload includes
           "device": event.device,
           "browser": event.browser,
           # ... whatever metadata is useful for downstream analytics
       },
   )
   ```
4. Do NOT raise if `emit_event` fails — log and continue. The webhook receiver should still write `dmaas_dub_events` and return 200 to Dub even if the analytics emit hits a transient error.

**Tests:**

* Extend (or create) `tests/test_dub_processor.py`:
  * Click webhook for a `dmaas_dub_links`-attributed link → `emit_event` is called once with the right step + recipient + properties.
  * Click webhook for an unattributed link → `emit_event` is NOT called; the dub_event row is still written; receiver returns 200.
  * Lead and sale webhooks follow the same pattern.
  * `emit_event` raises an exception → the receiver still returns 200 and the dub_event is still written.

**Verification step:**

After deploy: trigger a real Dub click on a test link minted from a real step, then check RudderStack's Live Events tab for the source `hq-x-server`. The `dub.click` event should appear within a few seconds with the full six-tuple + recipient_id in the properties.

---

### Slice 3 — Conversion columns in analytics endpoints

**Goal:** every analytics endpoint that rolls up direct_mail data surfaces the click funnel as first-class columns. Customer dashboards need this to show "delivered → clicked → (conversion)" — the headline metric.

**File touchpoints:**

* [`app/services/campaign_analytics.py`](app/services/campaign_analytics.py)
* [`app/services/channel_campaign_analytics.py`](app/services/channel_campaign_analytics.py)
* [`app/services/step_analytics.py`](app/services/step_analytics.py)
* [`app/services/direct_mail_analytics.py`](app/services/direct_mail_analytics.py)
* [`app/models/analytics.py`](app/models/analytics.py) — extend response models with the new fields.
* Tests: extend each corresponding `tests/test_*_analytics.py`.

**Required new fields (per scope level):**

For every endpoint that rolls up direct_mail (campaign, channel_campaign, step, direct_mail funnel):

```json
{
  "conversions": {
    "clicks_total": 0,         // total dub.click events in the window for this scope
    "unique_clickers": 0,      // distinct recipients who clicked at least once
    "click_rate": 0.0          // unique_clickers / unique_recipients_total (0.0 if denom is 0)
  }
}
```

* The denominator for `click_rate` is **unique recipients touched** (recipients with a delivered or in-transit piece in the window), NOT total clicks or total pieces. A recipient who scans the QR ten times counts once.
* Only surface fields we actually have data for. `leads_total` gets added in Directive 2 (when `track_lead` is wired via the hosted landing-page form-submit pipeline). `sales_total` is intentionally not on the roadmap — we don't have visibility into customers' downstream revenue outcomes unless they explicitly wire their CRM into Dub's `track_sale`. Surface zero fields would imply visibility we don't have; better to add them only when there's real data behind them.

**Where to read from:**

* `dmaas_dub_events` — append-only event log of every Dub webhook. Filter by `event_type IN ('click', 'lead', 'sale')` and join through `dmaas_dub_links` for attribution.
* For step scope: filter `dmaas_dub_links.channel_campaign_step_id = ?`.
* For channel_campaign scope: filter via the step's `channel_campaign_id`.
* For campaign rollup: filter via the step's `campaign_id`.
* For direct_mail funnel (which is brand-scoped): filter via the step's `organization_id` (always required) + optional `brand_id` / `channel_campaign_id` / `channel_campaign_step_id` (from the existing endpoint signature).
* All queries MUST filter by `organization_id` from auth context — same single-WHERE-clause guarantee as recipient lookups.

**Tests:**

For each analytics service test file:

* Property test: `unique_clickers <= clicks_total`.
* Property test: `unique_clickers <= unique_recipients_total` (you can't have more clickers than recipients).
* Click counts roll up correctly across step → channel_campaign → campaign.
* `click_rate` is `0.0` when `unique_recipients_total` is `0` (divide-by-zero guard).
* Cross-org leakage test: a click event for a recipient in org B does not surface in org A's analytics.

---

### Slice 4 — Recipient timeline includes Dub clicks

**Goal:** `GET /api/v1/analytics/recipients/{recipient_id}/timeline` shows Dub clicks (and future leads/sales) interleaved chronologically with piece events. "They got the postcard AND scanned it" is THE story for the dashboard's per-recipient drill-down.

**File touchpoints:**

* [`app/services/recipient_analytics.py`](app/services/recipient_analytics.py) — extend the events stream to UNION `dmaas_dub_events` (filtered to this recipient via `dmaas_dub_links`).
* `app/models/analytics.py` — `RecipientTimelineEvent` schema already supports the `channel`, `provider`, `event_type`, `artifact_kind`, `artifact_id`, `metadata` fields. Verify shape; no schema change expected.
* `tests/test_recipient_analytics.py` — extend.

**Required behavior:**

For each `dmaas_dub_events` row attributable to this recipient (via `dmaas_dub_links`), surface a timeline event with:

```json
{
  "occurred_at": "<from dmaas_dub_events.created_at or event.timestamp>",
  "channel": "direct_mail",
  "provider": "dub",
  "event_type": "dub.click",          // or dub.lead, dub.sale
  "campaign_id": "...",
  "channel_campaign_id": "...",
  "channel_campaign_step_id": "...",
  "artifact_id": "<dub_link_id>",
  "artifact_kind": "dub_link",
  "metadata": {
    "click_url": "...",
    "country": "...",
    "device": "...",
    "browser": "..."
  }
}
```

* All events (piece events + dub events + memberships) sorted by `occurred_at DESC`.
* Pagination (`limit` / `offset`) applies to the merged stream.
* The `summary.by_channel.direct_mail` count includes both piece events AND dub events.
* `summary.total_events` includes both.

**Tests:**

* A recipient with a delivered piece + 2 clicks → timeline shows 3 events in chronological order.
* Pagination across the merged stream works correctly.
* Cross-org guard: clicks for a recipient in org B never appear in org A's view (already enforced by the existing `recipient_id + organization_id` single-WHERE-clause lookup; just need to make sure the new join also goes through it).

---

## 4. Definition of done (whole directive)

* All 4 slices merged to `main`.
* `uv run pytest -q` green at every step. The post-analytics-buildout baseline is **582 passing**; this directive adds substantially more.
* `uv run ruff check` clean on every file you touch.
* For Slice 1: a documented manual verification (Lob test-mode campaign id, upload id, screenshot of pieces in Lob's dashboard) in the PR description.
* For Slice 2: a documented Live Events confirmation (RudderStack source `hq-x-server` showing a `dub.click` event with the right six-tuple) in the PR description.
* Update [`docs/lob-integration.md`](docs/lob-integration.md) to remove the "Creative + audience CSV upload to Lob (`/v1/uploads`) is scaffolded but deferred" note and replace with a description of the actual flow.
* Add a short post-ship summary at `docs/dmaas-foundation-pr-notes.md` describing what shipped, what's deferred to Directive 2, and any caveats / follow-ups.
* Mark the `# Future PR` comment in [`app/providers/lob/adapter.py`](app/providers/lob/adapter.py) as resolved (delete it; the future PR is now the past).

---

## 5. Working order (recommended)

1. **Read** the canonical hierarchy doc [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) and [`docs/lob-integration.md`](docs/lob-integration.md) end to end.
2. **Read** the recently-shipped analytics work — at minimum [`app/services/recipient_analytics.py`](app/services/recipient_analytics.py) and [`tests/test_reliability_analytics.py`](tests/test_reliability_analytics.py) — to internalize the test pattern (in-memory cursor fake, dependency_overrides for auth, single-WHERE-clause org isolation).
3. **Investigate** the existing `dmaas_designs` renderer surface — what does it produce today? Does it output Lob-shaped artifacts (HTML / asset URL), or does the Lob adapter need a small shim? Surface findings in Slice 1's PR description.
4. **Investigate** Lob's `/v1/uploads` API contract — read [`app/providers/lob/client.py:1624`](app/providers/lob/client.py:1624) (`create_upload`) and the Lob API docs at [`/Users/benjamincrane/api-reference-docs-new/lob/api-reference`](/Users/benjamincrane/api-reference-docs-new/lob/api-reference) to confirm the CSV row shape, idempotency-key support, and per-row error handling.
5. **Build Slice 1** (Lob audience upload). This is the largest and most consequential slice. Take it slow. Manual smoke against Lob test mode before opening the PR.
6. **Build Slice 2** (Dub clicks → emit_event). Smallest slice. After this lands, smoke against a real Dub test webhook + RudderStack Live Events.
7. **Build Slice 3** (conversion columns). Touches multiple analytics services. Add the columns in one PR, not four.
8. **Build Slice 4** (recipient timeline includes dub clicks). Smallest analytics-side change.
9. **Write the post-ship summary** + update `docs/lob-integration.md`.

If you hit a real architectural snag — especially around the renderer or Lob's per-row error behavior on uploads — STOP and surface it in the PR description rather than improvising. The directive should be enough, but the renderer is the most uncertain part.

---

## 6. Style + conventions

* Follow ruff config in `pyproject.toml` — line length 100, target py312, lint set `["E", "F", "I", "W", "UP", "B"]`.
* File header docstrings explain why the module exists, what's in scope, what's deferred.
* No new emojis in code, comments, or commit messages.
* Commit messages: short imperative subject under 72 chars. Blank line. 1–3 paragraphs. End with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
* PR descriptions: include sections for Summary, Six-tuple integrity, Cross-org leakage, Verification (where applicable), Test plan.

---

## 7. Reference paths cheat sheet

| What | Where |
|---|---|
| Canonical hierarchy doc | [docs/campaign-rename-pr-notes.md](docs/campaign-rename-pr-notes.md) |
| Direct-mail integration depth | [docs/lob-integration.md](docs/lob-integration.md) |
| Tenancy + auth | [docs/tenancy-model.md](docs/tenancy-model.md), [app/auth/roles.py](app/auth/roles.py) |
| Lob API reference (local copy) | `/Users/benjamincrane/api-reference-docs-new/lob/api-reference` |
| Lob HTTP client (has all wrappers) | [app/providers/lob/client.py](app/providers/lob/client.py) |
| Lob adapter (where Slice 1 happens) | [app/providers/lob/adapter.py](app/providers/lob/adapter.py) |
| Lob webhook projector | [app/webhooks/lob_processor.py](app/webhooks/lob_processor.py) |
| Lob idempotency helpers | [app/providers/lob/idempotency.py](app/providers/lob/idempotency.py) |
| Dub HTTP client | [app/providers/dub/client.py](app/providers/dub/client.py) |
| Dub webhook receiver (where Slice 2 happens) | [app/webhooks/dub_processor.py](app/webhooks/dub_processor.py) |
| `dmaas_dub_links` repo | [app/dmaas/dub_links.py](app/dmaas/dub_links.py) |
| Step minting (Dub side, already done) | [app/dmaas/step_link_minting.py](app/dmaas/step_link_minting.py) |
| Six-tuple emit chokepoint | [app/services/analytics.py](app/services/analytics.py) |
| Step context resolver | [app/services/channel_campaign_steps.py](app/services/channel_campaign_steps.py) (`get_step_context`) |
| Recipient + memberships | [app/services/recipients.py](app/services/recipients.py) |
| Slice 1a analytics scaffold (test pattern reference) | [tests/test_reliability_analytics.py](tests/test_reliability_analytics.py) |
| Recipient timeline (Slice 4 base) | [app/services/recipient_analytics.py](app/services/recipient_analytics.py) |
| Campaign rollup (Slice 3 base) | [app/services/campaign_analytics.py](app/services/campaign_analytics.py) |
| Prior analytics buildout post-ship notes | [docs/analytics-buildout-pr-notes.md](docs/analytics-buildout-pr-notes.md) |
| Dub-at-scale post-ship notes | (linked from [hq-x#44](https://github.com/bencrane/hq-x/pull/44)) |

---

**End of directive.** Four slices, four PRs. After this lands, the foundation is in place for Directive 2 (hosted landing pages + custom domains + opinionated single-call DMaaS API), which builds the actual $25K/mo value-add layer on top.
