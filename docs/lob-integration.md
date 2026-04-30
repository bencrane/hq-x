# Lob integration

End-to-end flow for direct mail through the campaign hierarchy. This
document is the canonical map of the Lob send + webhook pipeline as of
migration `0023_channel_campaign_steps.sql`.

## Hierarchy

```
business.campaigns                       (umbrella outreach effort)
  └── business.channel_campaigns         (one channel + provider, e.g. direct_mail/lob)
        └── business.channel_campaign_steps   (ordered touches; 1..N per channel_campaign)
              └── Lob campaign (cmp_*)         (provider-side primitive; one per step)
                    └── direct_mail_pieces       (one per recipient mailpiece)
```

* A **campaign** is the umbrella push.
* A **channel_campaign** is one channel's leg.
* A **channel_campaign_step** is one ordered mailing inside that leg
  ("postcard at day 0", "letter at day 14", ...). For direct mail each
  step maps 1:1 to a Lob campaign object.
* A **Lob campaign** (`cmp_*`) is Lob's batch-send primitive; we create
  one per step, attach a creative + audience, and ask Lob to mail it.
* A **direct_mail_piece** is one rendered, addressed mailpiece (the
  `psc_* / ltr_* / sfm_*` Lob produces per recipient). Every piece row
  carries the full hierarchy via FKs.

## Adapter

[`app/providers/lob/adapter.py`](../app/providers/lob/adapter.py) is the
canonical orchestration layer between our domain and Lob's. The adapter
class `LobAdapter` exposes:

| Method | What it does |
|---|---|
| `activate_step(step, channel_campaign)` | Creates a Lob campaign object, tags it with the six-tuple metadata, returns `LobActivationResult(status, external_provider_id, metadata)` for the caller to persist on the step row. |
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
       (each step references a dmaas_designs row via creative_ref)
4. Operator activates a step:
       POST /api/v1/channel-campaign-steps/{step_id}/activate
   →   LobAdapter.activate_step()  →  Lob creates `cmp_*`
   →   step row updated with external_provider_id, status='scheduled'
5. (Future PR) Scheduler activates step N+1 after step N's delay window.
6. Lob mails pieces; webhook events fire into:
       POST /webhooks/lob
   →   stored in webhook_events keyed by (lob, event_key)
   →   project_lob_event() routes to the right internal entity (below)
   →   direct_mail_piece_events row appended; piece status updated;
       analytics event emitted with the full six-tuple
```

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
* Creative + audience upload inside the adapter (steps 2 + 3 of
  `activate_step` are scaffolded but not wired in this PR).
* EmailBison adapter following the same pattern.
* Vapi/Twilio adapter for voice steps.
* UI for step editing in the designer.
* Dropping `channel_campaigns.design_id` (waits until step-level
  writes are confirmed in production).

## Cleanup follow-ups

* `ALTER TABLE direct_mail_pieces ALTER COLUMN channel_campaign_step_id
  SET NOT NULL` once new sends are confirmed to always populate it.
* `ALTER TABLE business.channel_campaigns DROP COLUMN design_id` once
  the adapter has been writing `creative_ref` on the step rows for one
  full release cycle.
