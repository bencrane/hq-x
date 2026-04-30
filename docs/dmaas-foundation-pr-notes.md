# DMaaS foundation — post-ship notes

Closes the directive at
[`docs/directives/2026-04-30-dmaas-foundation-lob-send-and-dub-conversion.md`](directives/2026-04-30-dmaas-foundation-lob-send-and-dub-conversion.md).

After these four PRs the platform sends real direct-mail pieces at
scale, tracks the click side of the funnel, and surfaces "delivered →
clicked" as a first-class metric — the prerequisites for the
customer-facing DMaaS product. Order shipped: **2 → 3 → 4 → 1** (the
analytics slices ship first because they have no operational risk; the
Lob send-path slice followed once the renderer-deferral decision was
made).

## What shipped

| PR | Slice | Headline change |
|---|---|---|
| #45 | 2 | Dub webhooks fan out clicks/leads/sales through `services/analytics.emit_event` so they ride the same six-tuple-tagged pipeline as Lob piece events. |
| #46 | 3 | Every direct-mail analytics endpoint (campaign / channel_campaign / step / direct-mail funnel) returns a `conversions` block (`clicks_total`, `unique_clickers`, `click_rate`). |
| #47 | 4 | The recipient timeline interleaves `dmaas_dub_events` with piece events — "they got the postcard AND scanned it" is now visible per recipient. |
| #48 | 1 | `LobAdapter.activate_step` mints Dub links, creates the Lob campaign + creative, builds the audience CSV from step memberships, and submits it via `/v1/uploads`. Idempotent on retry. |

## What's deferred (and why it's deferred, not skipped)

### Creative rendering — the central deferral

The Lob audience-upload path uses **operator-supplied creative**
(`channel_specific_config.lob_creative_payload`) for V1. The
`dmaas_designs.id` referenced by `creative_ref` is preserved on the
step row as metadata for the future renderer wiring; `LobAdapter` does
not consume it.

Building `dmaas_designs → Lob creative HTML` end-to-end is a multi-PR
project on its own:

* HTML synthesis from `content_config` + `resolved_positions`
* CSS positioning at solver-computed pixel coordinates with bleed/safe
  insets
* Per-face composition (postcard front+back; self-mailer
  outside/inside, panel-aware)
* Font + image asset hosting (no asset storage layer exists in the
  repo today)
* Self-mailer fold/glue handling against Lob's panel HTML conventions

That work gets its own directive. Until then, the operator/customer
prepares creative externally (Figma → PDF, hand HTML, etc.) and
supplies it on the step — same pattern the legacy per-piece routes in
`app/routers/direct_mail.py` already use.

The honest framing for users: **you cannot yet do
`dmaas_designs.id → printed postcard` end-to-end without manual
creative prep.** The plumbing is there; the render step in the middle
is manual.

### Multi-step scheduler

Step N+1 still has to be activated by hand after step N's
`delay_days_from_previous` window. The directive's scope was the
Lob-send and Dub-conversion gaps; multi-touch orchestration is its own
piece.

### Hosted landing pages + custom domains

Directive 2 (drafted post-Slice-1). Today the customer supplies
`landing_page_url` directly and we mint Dub links pointing at it.

### `leads_total` and `sales_total` on the analytics surface

Intentionally not surfaced. `leads_total` requires landing-page
form-submit (Directive 2). `sales_total` requires the customer to wire
their CRM into Dub's `track_sale`, which we don't promise as a
platform feature. Surfacing zero fields here would imply visibility
we don't have.

## Manual verification still needed

Both manual smokes need live provider credentials and were not run as
part of the merged PRs:

1. **Lob test-mode end-to-end smoke (Slice 1).** Create a campaign +
   channel_campaign + step with a real `dmaas_designs.id`, a 3-recipient
   audience, and a postcard `lob_creative_payload`. Activate. Confirm
   in Lob's dashboard that the campaign exists with the creative
   attached and 3 pieces are queued. Confirm the step row carries
   `external_provider_id`, `external_provider_metadata.lob_upload_id`,
   and status `scheduled`. Confirm per-piece webhooks arrive within a
   minute or two carrying the right `channel_campaign_step_id` +
   `recipient_id`.
2. **RudderStack Live Events confirmation (Slice 2).** Trigger a
   real Dub click on a step-minted link; confirm a `dub.click` event
   appears on source `hq-x-server` within a few seconds with the full
   six-tuple in `properties`.

Both should be run before announcing the foundation is "done."

## Caveats / follow-ups

* **`/v1/uploads` and `/v1/uploads/{id}/file` don't take an
  idempotency key.** The adapter gates them by checking
  `external_provider_metadata.lob_upload_id` instead. If a network
  blip lands two `POST /v1/uploads` requests, Lob will create two
  upload rows (only the second one will be referenced from our DB).
  The orphan upload is harmless — it stays in `Draft` state forever —
  but a future cleanup tool could prune them.
* **Self-mailer creative payloads** are accepted by the adapter
  (`resource_type=self_mailer`) but the operator must supply the
  full Lob-shaped payload (panels + folding metadata). We don't
  validate panel-specific shape; Lob will reject it server-side if
  it's wrong. Tightening that validation is its own PR.
* **`external_provider_metadata.lob_upload_file_response`** stores the
  raw `POST /file` response on success. That field can grow if Lob
  ever returns a fat response payload; right now the response is
  trivial (`{"message": "..."}`). Worth pruning if it ever balloons.
* **Migration from per-piece routes**. Operators are still using the
  per-piece routes in `app/routers/direct_mail.py` for ad-hoc sends;
  those keep working. The step-driven path should become the default
  for any campaign with > 1 recipient since it scales (per-piece is
  N sequential calls; step-driven is one upload).
