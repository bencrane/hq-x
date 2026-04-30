# Lob integration

End-to-end flow for direct mail through the campaign hierarchy. This
document is the canonical map of the Lob send + webhook pipeline as of
migration `0023_channel_campaign_steps.sql`.

## Hierarchy

```
business.campaigns                       (umbrella outreach effort)
  └── business.channel_campaigns         (one channel + provider, e.g. direct_mail/lob)
        └── business.channel_campaign_steps   (ordered touches; 1..N per channel_campaign)
              ├── business.channel_campaign_step_recipients
              │       (audience: which recipients are scheduled for this step)
              └── Lob campaign (cmp_*)         (provider-side primitive; one per step)
                    └── direct_mail_pieces       (one per recipient mailpiece;
                                                   recipient_id back-references
                                                   business.recipients)
```

`business.recipients` is a sibling identity table — the channel-agnostic
"people/businesses we know about in this organization." Pieces, calls,
and (future) emails all reference recipients so the same target across
channels rolls up to a single entity. See "Two-phase lifecycle" below.

* A **campaign** is the umbrella push.
* A **channel_campaign** is one channel's leg.
* A **channel_campaign_step** is one ordered mailing inside that leg
  ("postcard at day 0", "letter at day 14", ...). For direct mail each
  step maps 1:1 to a Lob campaign object.
* A **Lob campaign** (`cmp_*`) is Lob's batch-send primitive; we create
  one per step, attach a creative + audience, and ask Lob to mail it.
* A **direct_mail_piece** is one rendered, addressed mailpiece (the
  `psc_* / ltr_* / sfm_*` Lob produces per recipient). Every piece row
  carries the full hierarchy via FKs *and* a `recipient_id` (nullable on
  legacy rows; required on new step-driven sends).
* A **recipient** is the org-scoped channel-agnostic identity for a
  business / property / person. Identified by `(organization_id,
  external_source, external_id)`.

## Two-phase lifecycle

The send flow has two distinct phases that must not be conflated:

**Phase 1 — Step configuration (audience definition).** When a step's
audience is defined, [`app/services/channel_campaign_steps.py:materialize_step_audience`](../app/services/channel_campaign_steps.py)
runs *synchronously*:
1. Resolves the audience spec (DEX query, manual list, etc.) into
   `RecipientSpec` rows.
2. Bulk-upserts recipients via [`app/services/recipients.py:bulk_upsert_recipients`](../app/services/recipients.py)
   (natural key: `(organization_id, external_source, external_id)`).
3. Creates `channel_campaign_step_recipients` rows in status `pending`.
4. Surfaces the materialized audience for review.

The step is still `pending`. No Lob calls have been made. The customer
can iterate on the audience spec — `materialize_step_audience` defaults
to `replace_existing=True`, which **deletes pending memberships** and
re-inserts from the new spec. Memberships in any other status are
preserved (you cannot edit the audience after activation).

**Phase 2 — Step activation (send execution).** When the operator
activates a step (`POST /api/v1/channel-campaign-steps/{step_id}/activate`):
1. The Lob adapter creates the Lob campaign object (`cmp_*`) tagged with
   the six-tuple metadata.
2. Every `pending` membership for the step is bulk-flipped to
   `scheduled` via `bulk_update_pending_to_scheduled`.
3. As Lob produces per-recipient pieces and webhooks fire, the projector
   resolves each piece's `recipient_id`, transitions the membership to
   `sent` (terminal sent-family events: `mailed`, `in_transit`,
   `delivered`, etc.) or `failed` (`returned`, `failed`, `rejected`),
   and emits analytics with `recipient_id` in the payload.

Cancelling a step (`cancel_step`) also flips its `pending` and
`scheduled` memberships to `cancelled` in lockstep.

## Recipients identity model

| Field | Notes |
|---|---|
| `organization_id` | Strictly org-scoped. Same DOT in two orgs = two recipient rows. No cross-org sharing under any circumstances. |
| `(external_source, external_id)` | Natural key inside an org. Sources: `'fmcsa'` (DOT), `'nyc_re'` (BBL), `'manual_upload'` (row hash), etc. Application normalizes external_id before upsert. |
| `recipient_type` | `'business' \| 'property' \| 'person' \| 'other'` — enables audience-listing filters. |
| `mailing_address`, `phone`, `email`, `display_name` | Mutable identity attributes. Upsert merges with COALESCE for scalars; mailing_address is replaced when non-empty; metadata is JSONB shallow-merged (`existing || new`). |

## Membership status state machine

```
       (no row) ── materialize_step_audience ──►  pending
                                                      │
                                              activate_step (bulk)
                                                      ▼
                                                  scheduled
                                                      │
   ┌──────────────────────────────────────────────────┼──────────────────────────────────────────────┐
   ▼                                                  ▼                                              ▼
  sent                                             failed                                       cancelled
 (piece.delivered / mailed /                  (piece.returned /                              (cancel_step or
  in_transit / processed_for_delivery)         piece.failed / piece.rejected)                 audience replaced)
```

Terminal statuses (`sent`, `failed`, `cancelled`, `suppressed`) are
sticky — the projector will not overwrite them. `suppressed` is
reserved for future per-recipient suppression rules; no path writes it
today.

## Adapter

[`app/providers/lob/adapter.py`](../app/providers/lob/adapter.py) is the
canonical orchestration layer between our domain and Lob's. The adapter
class `LobAdapter` exposes:

| Method | What it does |
|---|---|
| `activate_step(step, channel_campaign)` | Mints per-recipient Dub links, creates the Lob campaign, attaches the operator-supplied creative, builds the audience CSV from step memberships, and submits it via `/v1/uploads`. Idempotent on retry: a second call against a step in `activating` status (with partial metadata) skips sub-steps that already succeeded. Returns `LobActivationResult(status, external_provider_id, metadata)` for the caller to persist on the step row. |
| `execute_send(step)` | Calls Lob's `send_campaign` to place the order. Local state moves to `sent`; webhooks refine real delivery state. |
| `cancel_step(step)` | Best-effort cancel against Lob. Returns whether Lob accepted the cancellation; local state is updated by the caller regardless. |
| `parse_webhook_event(payload)` | Pure function: flatten a Lob webhook payload to `(event_type, lob_campaign_id, lob_piece_id, raw_event_name)`. |

Low-level HTTP calls (multipart uploads, address verification, individual
postcard/letter/self_mailer create endpoints, etc.) still live in
[`app/providers/lob/client.py`](../app/providers/lob/client.py); the
adapter composes those primitives into step-aware operations. **All new
code that orchestrates Lob's campaign-object lifecycle must go through
the adapter**, not the low-level client directly.

### Metadata tagging contract

Every Lob campaign object created via `activate_step` carries this
metadata field:

```json
{
  "organization_id":         "<uuid>",
  "brand_id":                "<uuid>",
  "campaign_id":             "<uuid>",
  "channel_campaign_id":     "<uuid>",
  "channel_campaign_step_id":"<uuid>"
}
```

so webhook ingestion can resolve back to internal entities even if the
per-recipient `direct_mail_pieces` rows haven't been written yet (or
have been deleted).

## Send pipeline

```
1. Operator creates a campaign via POST /api/v1/campaigns
2. Operator creates a channel_campaign under it (channel='direct_mail',
   provider='lob'):
       POST /api/v1/channel-campaigns
3. Operator creates one or more steps:
       POST /api/v1/channel-campaigns/{cc_id}/steps
       (each step references a dmaas_designs row via creative_ref AND
        carries channel_specific_config.lob_creative_payload — the
        operator-supplied creative; see "Creative source" below)
4. Operator activates a step:
       POST /api/v1/channel-campaign-steps/{step_id}/activate
   →   LobAdapter.activate_step():
         a. mints per-recipient Dub links (via app/dmaas/step_link_minting)
         b. POST /v1/campaigns                  → cmp_*
         c. POST /v1/creatives                  → crv_*  (bound to cmp_*)
         d. SELECT memberships + recipients +
            dub_links → build audience CSV
         e. POST /v1/uploads                    → upl_*
         f. POST /v1/uploads/{upl_id}/file      → ships the CSV
   →   step row updated with external_provider_id (= cmp_*) and
       external_provider_metadata.{lob_creative_id, lob_upload_id};
       status='scheduled'.
5. (Future PR) Scheduler activates step N+1 after step N's delay window.
6. Lob mints per-recipient pieces server-side, webhook events fire into:
       POST /webhooks/lob
   →   stored in webhook_events keyed by (lob, event_key)
   →   project_lob_event() routes to the right internal entity (below)
   →   direct_mail_piece_events row appended; piece status updated;
       analytics event emitted with the full six-tuple
```

### Creative source

For V1 the operator (or customer) prepares creative externally
(Figma → PDF, hand-written HTML, etc.) and supplies it directly on the
step:

```json
"channel_specific_config": {
  "landing_page_url": "https://customer.example/lp",
  "lob_creative_payload": {
    "resource_type": "postcard",        // postcard | letter | self_mailer
    "front": "<html>...</html>",        // OR tmpl_*  OR https://.../front.pdf
    "back":  "<html>...</html>",
    "details": { "size": "4x6", ... },
    "from":   "adr_..."                 // OR inline address object
  }
}
```

`creative_ref` (= `dmaas_designs.id`) is preserved on the step row as
metadata for a future renderer; `LobAdapter` does not consume it. The
adapter validates `lob_creative_payload` is well-formed and fails the
activation with a structured error if it's missing or malformed —
operators see what's wrong and retry rather than getting a Lob-side
422 hours later.

Building `dmaas_designs → Lob creative HTML` is a multi-PR project on
its own (HTML synthesis, CSS positioning, font/asset hosting,
panel-aware self-mailer geometry). It gets its own directive.

### Idempotent retry

Each Lob object has a deterministic idempotency key derived from the
step id:

* `hqx-step-{step_id}-campaign` — Lob campaign create
* `hqx-step-{step_id}-creative` — Lob creative create

The upload row + file POST are gated by checking
`external_provider_metadata.lob_upload_id` rather than an idempotency
key, since Lob's `/v1/uploads` and `/v1/uploads/{id}/file` don't accept
one. A partial failure (e.g. campaign + creative succeeded, upload
failed) leaves the step in `activating` status with the ids that did
succeed persisted; a retried activation skips sub-steps that already
have an id and resumes from the next one. The `pending → activating →
scheduled` transition is allowed in `app/services/channel_campaign_steps.py`
to support this.

## Webhook events handled

The webhook receiver lives at
[`app/routers/webhooks/lob.py`](../app/routers/webhooks/lob.py) and the
projector at
[`app/webhooks/lob_processor.py`](../app/webhooks/lob_processor.py).

For each event, the projector does:

1. **Parse** via `LobAdapter.parse_webhook_event(payload)` →
   `(event_type, lob_campaign_id, lob_piece_id, raw_event_name)`.
2. **Resolve** to internal entities by lookup, in order:
   * `direct_mail_pieces` WHERE `external_piece_id` = `<lob_piece_id>`
     → gives `(organization_id, brand_id, campaign_id,
     channel_campaign_id, channel_campaign_step_id)` directly.
   * Fallback: `channel_campaign_steps` WHERE `external_provider_id` =
     `<lob_campaign_id>`. Used for campaign-level events
     (`campaign.created`, `campaign.deleted`) and for piece events that
     fire before the piece row was written.
3. **Update state**:
   * Per-piece events → append `direct_mail_piece_events` row, update
     `direct_mail_pieces.status` if the event maps to a status change,
     and write a `suppressed_addresses` row when the event is in
     `SUPPRESSION_TRIGGERS` (returned, returned_to_sender,
     certified-mail returned families).
   * Per-step events → conservative status mapping: raw event names
     containing `failed` → `failed`; raw event names containing
     `deleted` or `cancel` → `cancelled`. Anything else stays
     informational.
4. **Emit analytics** via `app/services/analytics.py:emit_event()`
   carrying the six-tuple
   `(organization_id, brand_id, campaign_id, channel_campaign_id,
   channel_campaign_step_id, channel='direct_mail', provider='lob')`.
5. **Mark webhook_events row** with the projection result:
   * `status='processed'` — projection applied successfully
   * `status='orphaned'` — payload understood but no internal entity
     matched (resource id present but unknown). Operators can dashboard
     these for manual reconciliation.
   * `status='dead_letter'` — payload could not be parsed (no resource
     id at all). Operator can replay via `POST
     /webhooks/lob/replay/{event_id}`.

### Orphan handling

`status='orphaned'` is the projector's signal that **we received a Lob
event with a usable resource id, but neither
`direct_mail_pieces.external_piece_id` nor
`channel_campaign_steps.external_provider_id` matched it**. This usually
means:

* a piece row was deleted before all webhooks for it arrived; or
* the Lob campaign was created outside our adapter (manual operator
  send via Lob dashboard).

Operators triage orphans by querying `webhook_events WHERE
status='orphaned' AND provider_slug='lob'` and either backfilling the
matching internal row + replaying, or marking as known-orphaned.

### Idempotency

Two layers:

1. The webhook receiver dedupes on `webhook_events.(provider_slug,
   event_key)` UNIQUE — a duplicate POST returns
   `status='duplicate_ignored'` without invoking the projector.
2. The projector itself is safe to replay against the same payload:
   * `direct_mail_piece_events` is append-only (every replay just adds
     an audit row, which is the desired behaviour).
   * `direct_mail_pieces.status` is only updated when the new status
     differs from the old.
   * `suppressed_addresses` upsert is idempotent on
     `(address_hash, reason)`.
   * step status updates are guarded by an old-status check.

So the operator replay endpoint (`POST /webhooks/lob/replay/{id}`) is
safe to call any number of times.

## Backfill (0023)

Migration 0023 inserted one default step per existing channel_campaign
so every channel_campaign has at least one step downstream code can
attach to. The default step has `step_order=1`,
`delay_days_from_previous=0`, `creative_ref` copied from
`channel_campaigns.design_id` for direct_mail rows (NULL for other
channels), and `status` mirroring the parent
(`sending`/`sent`→`sent`; `archived`→`archived`; everything else →
`pending`).

**At apply time on the hq-x project, zero existing channel_campaigns
existed, so the backfill INSERT was a no-op.** Future Lob sends start
clean.

## Out of scope (future work)

* Multi-step orchestration: a scheduler that activates step N+1 after
  step N's `delay_days_from_previous` window.
* `dmaas_designs → Lob creative` renderer (HTML synthesis, CSS
  positioning, font/asset hosting, panel-aware self-mailer geometry).
  Until that lands the operator supplies creative directly via
  `channel_specific_config.lob_creative_payload`.
* Hosted landing pages + custom domain provisioning (Directive 2 in
  the DMaaS foundation work).
* Vapi/Twilio adapter for voice steps.
* UI for step editing in the designer.
* Dropping `channel_campaigns.design_id` (waits until step-level
  writes are confirmed in production).

## Cleanup follow-ups

* `ALTER TABLE direct_mail_pieces ALTER COLUMN channel_campaign_step_id
  SET NOT NULL` once new sends are confirmed to always populate it.
* `ALTER TABLE direct_mail_pieces ALTER COLUMN recipient_id SET NOT
  NULL` once new step-driven sends are confirmed always populating it
  (legacy ad-hoc operator sends via the per-piece routers will keep
  `recipient_id` NULL — that path doesn't have a recipient to bind).
* `ALTER TABLE business.channel_campaigns DROP COLUMN design_id` once
  the adapter has been writing `creative_ref` on the step rows for one
  full release cycle.
