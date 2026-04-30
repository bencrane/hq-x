# Campaigns in hq-x — canonical reference

This is the canonical reference for the campaigns hierarchy in hq-x as
it stands today. Read this document first if you are an AI agent or
engineer landing in this codebase and need to understand how a "send"
flows from a customer's intent down to a Lob postcard, a Vapi call, or
a Twilio SMS.

Companion docs:
* [`docs/campaign-model.md`](campaign-model.md) — the conceptual model
  (campaign vs. channel_campaign, channel/provider matrix, REST surface).
* [`docs/lob-integration.md`](lob-integration.md) — the direct-mail
  pipeline in depth (adapter, webhooks, two-phase lifecycle).
* [`docs/tenancy-model.md`](tenancy-model.md) — organizations, brands,
  memberships, two-axis roles.

History (older PR notes, useful for archaeology only):
* [`docs/campaign-model-pr-notes.md`](campaign-model-pr-notes.md) — the
  original two-layer ship under the `gtm_motions` / `campaigns` names.
  Outdated; superseded by this file.
* [#22](https://github.com/bencrane/hq-x/pull/22) — rename pass
  (`gtm_motions → campaigns`, old `campaigns → channel_campaigns`).
* [#28](https://github.com/bencrane/hq-x/pull/28) —
  `channel_campaign_steps` (per-touch ordered execution under a
  channel_campaign) + Lob retrofit.
* [#29](https://github.com/bencrane/hq-x/pull/29) — `recipients` +
  `channel_campaign_step_recipients` (channel-agnostic identity layer).

## The hierarchy in one picture

```
business.organizations
  └── business.brands                     (one organization → many brands)
        └── business.campaigns            (the umbrella outreach effort)
              └── business.channel_campaigns          (one per channel run)
                    └── business.channel_campaign_steps     (ordered touches)
                          ├── business.channel_campaign_step_recipients
                          │       (audience: which recipients are scheduled
                          │        for this step, with status)
                          └── provider primitive (e.g. Lob campaign cmp_*)
                                └── per-recipient artifact rows:
                                      direct_mail_pieces
                                      call_logs
                                      sms_messages
                                      (each carries recipient_id when
                                       generated from a step audience)

business.recipients ◄────── channel-agnostic identity (org-scoped)
                            referenced by step memberships AND by the
                            per-recipient artifact rows above.
```

Five layers of concept, one identity sibling table:

1. **organization** — the tenant.
2. **brand** — the customer's "from" identity (e.g. domain, sender name).
3. **campaign** — the umbrella push. Channel-agnostic, count-agnostic.
   "Q2 outreach to lapsed insurance MCs."
4. **channel_campaign** — one channel × one provider underneath the
   campaign. ("direct_mail via Lob," "voice_outbound via Vapi.")
5. **channel_campaign_step** — one ordered touch within a
   channel_campaign. ("postcard at day 0," "letter at day 14.") For
   direct_mail each step maps 1:1 to a Lob campaign object.

Plus:

* **recipient** — a channel-agnostic, org-scoped stable identity for a
  business / property / person we contact. Referenced by per-recipient
  artifact rows so the same target rolls up across channels.
* **step membership** (`channel_campaign_step_recipients`) — the
  audience-targeting layer: which recipients are scheduled to be
  contacted by which step, with lifecycle status.

## Tables and key columns

### `business.campaigns` — umbrella

| Column | Notes |
|---|---|
| `id` | PK |
| `organization_id` | FK → `business.organizations`. Strict org scope. |
| `brand_id` | FK → `business.brands`. |
| `name`, `description`, `metadata` | Free-form. |
| `status` | `draft \| active \| paused \| completed \| archived` |
| `start_date` | Anchor for downstream step scheduling. |
| `created_by_user_id` | FK → `business.users` (set null on user delete). |
| `created_at`, `updated_at`, `archived_at` | Lifecycle. |

### `business.channel_campaigns` — per-channel execution

| Column | Notes |
|---|---|
| `id` | PK |
| `campaign_id` | FK → `business.campaigns` (ON DELETE RESTRICT). |
| `organization_id`, `brand_id` | Denormalized from parent. App-layer keeps in sync. |
| `name` | |
| `channel` | `direct_mail \| email \| voice_outbound \| sms` |
| `provider` | `lob \| emailbison \| twilio \| vapi \| manual` |
| `audience_spec_id` | Opaque UUID into data-engine-x `audience_specs` (no FK — separate DB). |
| `audience_snapshot_count` | Frozen size at materialization time, optional. |
| `status` | `draft \| scheduled \| sending \| sent \| paused \| failed \| archived` |
| `start_offset_days`, `scheduled_send_at` | Scheduling. |
| `schedule_config`, `provider_config`, `metadata` | JSONB. |
| `design_id` | Legacy; for direct_mail the canonical creative pointer is now `channel_campaign_steps.creative_ref`. Slated to drop once steps are fully wired. |

`(channel, provider)` is constrained at the application layer
(`VALID_CHANNEL_PROVIDER_PAIRS` in
[`app/models/campaigns.py`](../app/models/campaigns.py)). The DB CHECK
is permissive.

### `business.channel_campaign_steps` — ordered touches

| Column | Notes |
|---|---|
| `id` | PK |
| `channel_campaign_id` | FK → parent (CASCADE on delete). |
| `campaign_id`, `organization_id`, `brand_id` | Denormalized for query / webhook routing. |
| `step_order` | INT ≥ 1, UNIQUE within `channel_campaign_id`. |
| `name`, `metadata` | Free-form. |
| `delay_days_from_previous` | INT ≥ 0. Step 1 = 0. |
| `scheduled_send_at` | Computed: see `compute_step_scheduled_send_at`. |
| `creative_ref` | Polymorphic — for `direct_mail` it's `dmaas_designs.id`, brand-scope-checked at app layer. NULL for non-direct-mail. |
| `channel_specific_config` | JSONB. For Lob: `test_mode`, `schedule_date`, `use_type`, `billing_group_id`. |
| `external_provider_id` | Provider's id for the step (Lob `cmp_*` for direct_mail). NULL until activation. Indexed for webhook lookup. |
| `external_provider_metadata` | Raw response from provider on activation. |
| `status` | `pending \| scheduled \| activating \| sent \| failed \| cancelled \| archived` |
| `activated_at` | Set on first successful activation. |

### `business.channel_campaign_step_recipients` — step audience memberships

| Column | Notes |
|---|---|
| `id` | PK |
| `channel_campaign_step_id` | FK → step (CASCADE on delete). |
| `recipient_id` | FK → `business.recipients` (RESTRICT on delete). |
| `organization_id` | Denormalized for org-scoped queries. |
| `status` | `pending \| scheduled \| sent \| failed \| suppressed \| cancelled` |
| `scheduled_for`, `processed_at`, `error_reason` | Lifecycle. |
| `metadata` | JSONB. |

`UNIQUE(channel_campaign_step_id, recipient_id)` — a recipient appears
at most once per step. State machine documented in
[`docs/lob-integration.md`](lob-integration.md#membership-status-state-machine).

### `business.recipients` — channel-agnostic identity

| Column | Notes |
|---|---|
| `id` | PK |
| `organization_id` | FK. **Strictly org-scoped.** Same DOT in two orgs = two recipient rows. No cross-org sharing under any circumstances. |
| `recipient_type` | `business \| property \| person \| other`. Top-level CHECK column so audience queries can filter on it. |
| `external_source` | Source system: `'fmcsa'` (DOT), `'nyc_re'` (BBL), `'manual_upload'` (row hash), … |
| `external_id` | Source system's id for this entity. |
| `display_name`, `mailing_address` (JSONB), `phone`, `email` | Mutable identity attributes. |
| `metadata` | JSONB free-form. |
| `created_at`, `updated_at`, `deleted_at` | Lifecycle. |

`UNIQUE (organization_id, external_source, external_id)` is the natural
key. Application-layer normalization is the caller's responsibility —
audience source adapters (FMCSA, NYC RE, manual upload) lowercase /
strip-pad / canonicalize before calling
`app/services/recipients.py:upsert_recipient`.

### Per-recipient artifact tables (children of step + recipient)

| Table | Channel | Carries `recipient_id`? |
|---|---|---|
| `direct_mail_pieces` | direct_mail / lob | Yes (nullable; required for new step-driven sends) |
| `call_logs` | voice_outbound / vapi+twilio | Not yet — future PR follows the same pattern |
| `sms_messages` | sms / twilio | Not yet — future PR follows the same pattern |
| (future) `email_messages` | email / emailbison | Will add at port time |

Every artifact table also carries `channel_campaign_step_id`,
`channel_campaign_id`, `campaign_id` (denormalized hierarchy tagging).

## Application surface

### Modules

| Layer | Path |
|---|---|
| Pydantic models (campaigns) | [`app/models/campaigns.py`](../app/models/campaigns.py) |
| Pydantic models (recipients) | [`app/models/recipients.py`](../app/models/recipients.py) |
| Service: campaigns (umbrella) | [`app/services/campaigns.py`](../app/services/campaigns.py) |
| Service: channel_campaigns | [`app/services/channel_campaigns.py`](../app/services/channel_campaigns.py) |
| Service: channel_campaign_steps | [`app/services/channel_campaign_steps.py`](../app/services/channel_campaign_steps.py) |
| Service: recipients + step memberships | [`app/services/recipients.py`](../app/services/recipients.py) |
| Service: analytics emit | [`app/services/analytics.py`](../app/services/analytics.py) |
| Router: campaigns | [`app/routers/campaigns.py`](../app/routers/campaigns.py) — `/api/v1/campaigns` |
| Router: channel_campaigns | [`app/routers/channel_campaigns.py`](../app/routers/channel_campaigns.py) — `/api/v1/channel-campaigns` |
| Lob adapter (canonical entry point) | [`app/providers/lob/adapter.py`](../app/providers/lob/adapter.py) |
| Lob webhook projector | [`app/webhooks/lob_processor.py`](../app/webhooks/lob_processor.py) |

### REST endpoints (current)

```
POST   /api/v1/campaigns                        create campaign (umbrella)
GET    /api/v1/campaigns/{id}
PATCH  /api/v1/campaigns/{id}
POST   /api/v1/campaigns/{id}/activate
POST   /api/v1/campaigns/{id}/archive

POST   /api/v1/channel-campaigns                create channel_campaign
GET    /api/v1/channel-campaigns/{id}
PATCH  /api/v1/channel-campaigns/{id}

POST   /api/v1/channel-campaigns/{cc_id}/steps  create step
PATCH  /api/v1/channel-campaign-steps/{step_id}
POST   /api/v1/channel-campaign-steps/{step_id}/activate
POST   /api/v1/channel-campaign-steps/{step_id}/cancel
```

Audience materialization for a step is a service-layer call today
(`materialize_step_audience`); the operator-facing REST endpoint for it
lands with the audience-builder UI work.

The legacy brand-axis voice URL prefix
`/api/brands/{brand_id}/voice/campaigns/...` is preserved for
back-compat but the path param inside is `channel_campaign_id`.

### Pydantic key types

```python
# app/models/campaigns.py
Channel  = Literal["direct_mail", "email", "voice_outbound", "sms"]
Provider = Literal["lob", "emailbison", "twilio", "vapi", "manual"]
CampaignStatus            = Literal["draft", "active", "paused", "completed", "archived"]
ChannelCampaignStatus     = Literal["draft", "scheduled", "sending", "sent", "paused", "failed", "archived"]
ChannelCampaignStepStatus = Literal["pending", "scheduled", "activating", "sent", "failed", "cancelled", "archived"]

# app/models/recipients.py
RecipientType        = Literal["business", "property", "person", "other"]
StepRecipientStatus  = Literal["pending", "scheduled", "sent", "failed", "suppressed", "cancelled"]
```

## End-to-end send flow (direct_mail / Lob)

This is the worked example. Other channels follow the same shape; only
the provider adapter and per-recipient artifact table differ.

### 1. Customer/operator authoring

```
POST /api/v1/campaigns                        → campaign (umbrella)
POST /api/v1/channel-campaigns                → channel_campaign(channel='direct_mail', provider='lob')
POST /api/v1/channel-campaigns/{cc}/steps     → channel_campaign_step(creative_ref=<dmaas_designs.id>)
                                                (one or many; ordered)
```

At this point:
* Step `status='pending'`.
* No Lob calls have been made.
* No memberships exist.

### 2. Audience materialization (configuration phase)

```python
await materialize_step_audience(
    step_id=step_id,
    organization_id=org_id,
    recipients=[RecipientSpec(external_source='fmcsa', external_id='123456'), ...],
    replace_existing=True,  # default
)
```

Internally:
1. `bulk_upsert_recipients` — dedupes input by natural key; for each
   unique `(external_source, external_id)`, upserts
   `business.recipients`. On conflict: `recipient_type` overwritten;
   scalars `COALESCE(new, existing)`; `mailing_address` replaced when
   non-empty; `metadata` JSONB shallow-merged (`existing || new`).
2. If `replace_existing`: `DELETE FROM
   channel_campaign_step_recipients WHERE step=… AND status='pending'`.
   (Non-pending memberships are never touched; you can't edit an
   audience after activation.)
3. Inserts one row per recipient with `status='pending'`.

The customer can iterate freely on the audience spec while the step is
still `pending`.

### 3. Activation (send-execution phase)

```
POST /api/v1/channel-campaign-steps/{step_id}/activate
```

`activate_step`:
1. Validates step is `pending` and channel is `direct_mail`.
2. Calls `LobAdapter.activate_step(step, channel_campaign)`:
   * `POST /v1/campaigns` to Lob with metadata-tagged payload (six-tuple
     attached: `organization_id, brand_id, campaign_id,
     channel_campaign_id, channel_campaign_step_id`).
   * Returns `LobActivationResult(status, external_provider_id, metadata)`.
3. Persists `external_provider_id` + `external_provider_metadata` on
   the step row, sets `status='scheduled'`, `activated_at=NOW()`.
4. Calls `bulk_update_pending_to_scheduled(step_id)` → flips every
   `pending` membership for the step to `scheduled`.

Creative + audience CSV upload to Lob (`/v1/uploads`) is scaffolded but
deferred — pieces today are still created via the per-piece routes in
[`app/routers/direct_mail.py`](../app/routers/direct_mail.py). When the
upload path lands, pieces will be created server-side by Lob and arrive
via webhooks tagged with `channel_campaign_step_id` + `recipient_id`
in metadata.

### 4. Webhook projection

[`app/webhooks/lob_processor.py:project_lob_event`](../app/webhooks/lob_processor.py)
handles each Lob webhook:

1. **Parse** via `LobAdapter.parse_webhook_event(payload)` →
   `(event_type, lob_campaign_id, lob_piece_id, raw_event_name)`.
2. **Resolve** to internal entities, in order:
   * `direct_mail_pieces WHERE external_piece_id = <lob_piece_id>` →
     gives the full hierarchy + `recipient_id`.
   * Fallback: `channel_campaign_steps WHERE external_provider_id =
     <lob_campaign_id>` for campaign-level events or piece events that
     fire before the piece row was written.
3. **Update state**:
   * Per-piece events → append `direct_mail_piece_events` audit row;
     update `direct_mail_pieces.status` if mapped; write
     `suppressed_addresses` row on suppression-triggering events.
   * Per-step events → conservative status mapping (`failed` → failed,
     `deleted`/`cancel` → cancelled).
4. **Membership transition** — if the resolved piece carries a
   `recipient_id` and the event is in:
   * `_PIECE_TERMINAL_SENT` (`piece.mailed`, `piece.in_transit`,
     `piece.in_local_area`, `piece.processed_for_delivery`,
     `piece.delivered`, certified variants) → membership → `sent`.
   * `_PIECE_TERMINAL_FAILED` (`piece.failed`, `piece.rejected`,
     `piece.returned`, `piece.certified.returned`) → membership → `failed`.
   * Terminal statuses are sticky; the projector never overwrites them.
5. **Emit analytics** via `app/services/analytics.py:emit_event`
   carrying the six-tuple plus `recipient_id` when present.
6. **Mark `webhook_events`** row: `processed` / `orphaned` / `dead_letter`.

## Tagging contract (the six-tuple)

Every Lob campaign object created via the adapter and every analytics
event we emit carries:

```
organization_id
brand_id
campaign_id
channel_campaign_id
channel_campaign_step_id
channel + provider     (resolved from channel_campaign)
```

Plus `recipient_id` when the event is per-recipient.

`emit_event()` enforces this in code: callers supply
`channel_campaign_step_id` (preferred) or `channel_campaign_id`; the
helper resolves the rest from `get_step_context` /
`get_channel_campaign_context`. Untagged emits fail with
`AnalyticsContextMissing`.

## Recipient identity rules (must-follow)

1. **Organization-scoped only.** Never resolve a recipient across orgs.
   The same business in two orgs is two recipient rows. The natural-key
   UNIQUE constraint includes `organization_id`.
2. **Natural key normalization.** External_source/external_id values are
   stored as given. Audience adapters MUST canonicalize before upsert
   (lowercase, strip whitespace, zero-pad, etc.) so dedupe is reliable.
3. **Recipient type is identity, not workflow state.** Don't overload
   `recipient_type` with engagement/lead status. (No `leads` table —
   engagement state stays on the artifact rows.)
4. **Per-recipient artifact rows MUST carry `recipient_id`** when
   generated from a step audience. Ad-hoc operator sends through the
   per-piece routes legitimately leave it NULL — that's why the column
   stays nullable for now.

## Decisions worth flagging (cumulative across #18, #22, #28, #29)

1. **No deprecation aliases for the old GTM URL surface.** When #22
   renamed `gtm_motions → campaigns`, the old URLs were dropped clean.
2. **`campaigns_legacy` was dropped in #22, not deferred.** Audit
   confirmed zero rows + zero needs-review entries before the drop.
3. **Voice/SMS rows don't denormalize the umbrella `campaign_id`.**
   Only `direct_mail_pieces` carries both `channel_campaign_id` and
   `campaign_id`. Voice resolves the umbrella via the FK chain. Revisit
   if cross-channel analytics queries need a fast umbrella join.
4. **Per-step `creative_ref` is polymorphic, no DB-level FK.** For
   `direct_mail` it points at `dmaas_designs.id` and the application
   layer enforces brand-scope. Future channels reference different
   tables; the column stays UUID without an FK.
5. **Two-phase lifecycle separation.** Audience materialization is a
   *configuration* concern (synchronous, reviewable); activation is a
   *send-execution* concern (Lob calls, status transitions). Don't
   conflate them.
6. **Organization-scoped recipients only.** Cross-org recipient sharing
   is intentionally not supported.
7. **`recipient_type` is a top-level column, not buried in metadata.**
   So queries can filter on it.
8. **Audience modification before activation = delete-and-recreate of
   pending memberships.** Simplest model; nothing has happened yet.

## Out of scope (still future work)

* Lob audience CSV upload via `/v1/uploads` inside the adapter.
* Multi-step scheduler that activates step N+1 after step N's
  `delay_days_from_previous` window.
* EmailBison adapter following the Lob adapter pattern.
* Vapi/Twilio adapter for voice steps; recipient_id on `call_logs` and
  `sms_messages`.
* Frontend UI for campaign + channel_campaign + step authoring,
  audience builder, and step activation review.
* Per-recipient suppression rules (do-not-mail at the recipient level).
* Cross-channel suppression (don't call a recipient who unsubscribed
  via email).
* Recipient enrichment pipelines.
* Lead scoring / engagement-derived status on recipients.
* A separate `leads` workflow table layered on top of recipients.
* Cross-organization recipient sharing — intentionally not supported.

## Cleanup follow-ups

* `ALTER TABLE direct_mail_pieces ALTER COLUMN channel_campaign_step_id
  SET NOT NULL` once new sends are confirmed always populating it.
* `ALTER TABLE direct_mail_pieces ALTER COLUMN recipient_id SET NOT
  NULL` once new step-driven sends are confirmed always populating it
  (legacy ad-hoc operator sends will keep it NULL — that path doesn't
  have a recipient to bind, so we'd need to either backfill from
  address or exclude that route first).
* `ALTER TABLE business.channel_campaigns DROP COLUMN design_id` once
  the adapter has been writing `creative_ref` on step rows for one
  full release cycle.
* `ALTER TABLE direct_mail_pieces ALTER COLUMN
  channel_campaign_id / campaign_id SET NOT NULL` — pending real-data
  backfill audit confirming zero orphans on production sends.

## Migration provenance

| Migration | What it did |
|---|---|
| `0021_gtm_motions_and_campaigns.sql` | Initial two-layer ship under the names `gtm_motions` / `campaigns`. Renamed pre-existing single-layer `business.campaigns` → `campaigns_legacy` and built the new shape on top. |
| `0022_rename_campaigns_hierarchy.sql` | Dropped `campaigns_legacy`. Renamed `gtm_motions → campaigns` and old `campaigns → channel_campaigns`. Renamed `gtm_motion_id → campaign_id` (parent FK on channel_campaigns). On every child table, renamed `campaign_id → channel_campaign_id`. On `direct_mail_pieces`, swapped two columns (old `campaign_id` → `channel_campaign_id`; old `gtm_motion_id` → `campaign_id`). |
| `0023_channel_campaign_steps.sql` | Created `business.channel_campaign_steps`. Added nullable `channel_campaign_step_id` FK to `direct_mail_pieces`. Backfilled one default step per existing channel_campaign. |
| `20260429T120000_recipients.sql` | Created `business.recipients` (org-scoped identity, natural-keyed) and `business.channel_campaign_step_recipients` (audience memberships). Added nullable `recipient_id` to `direct_mail_pieces`. |

Migration filename convention: new migrations use a UTC-timestamp prefix
(`YYYYMMDDTHHMMSS_<slug>.sql`) rather than a numeric prefix. Lex-sorts
correctly after the legacy `00NN_*` files and avoids collision when
multiple agents work in parallel.
