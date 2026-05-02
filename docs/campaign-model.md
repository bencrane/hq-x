# Campaigns + channel_campaigns

This document describes the two-layer outreach model that lives at
`business.campaigns` (umbrella) + `business.channel_campaigns` (per-channel
execution unit), introduced in migrations
`0021_gtm_motions_and_campaigns.sql` (initial shape, called gtm_motions /
campaigns at the time) and `0022_rename_campaigns_hierarchy.sql` (rename to
the current names + drop of `campaigns_legacy`).

The application surface lives at:

* `app/services/campaigns.py`, `app/routers/campaigns.py`
* `app/services/channel_campaigns.py`, `app/routers/channel_campaigns.py`

## Hierarchy

```
business.organizations
  └── business.brands                    (brand belongs to one organization)
        └── business.campaigns           (campaign belongs to one brand)
              └── business.channel_campaigns  (one per channel run; channel + provider typed)
                    ├── direct_mail_pieces.channel_campaign_id  (channel='direct_mail', provider='lob')
                    ├── call_logs.channel_campaign_id           (channel='voice_outbound', provider in {vapi,twilio})
                    └── sms_messages.channel_campaign_id        (channel='sms', provider='twilio')
```

A **campaign** is the umbrella outreach effort. It is channel-agnostic and
count-agnostic. One per cohesive go-to-market push (e.g. "Q2 outreach to
lapsed-insurance MCs"). It carries `name`, `description`, `start_date`,
`status`, and free-form `metadata`.

A **channel_campaign** is the channel-typed execution unit underneath a
campaign. Each channel_campaign represents one channel's execution against
an audience, sent via one provider. It carries the audience reference
(`audience_spec_id`, an opaque UUID into the data-engine-x audience_specs
table — not FK-enforced because that table lives in a separate database),
the schedule (`start_offset_days` and the computed `scheduled_send_at`),
and channel-specific config in `schedule_config` and `provider_config`.

A campaign has 1..N channel_campaigns. A channel_campaign always belongs
to exactly one campaign.

## Channel / provider matrix

The `(channel, provider)` enum tuple is restricted at the application layer
(see `VALID_CHANNEL_PROVIDER_PAIRS` in `app/models/campaigns.py`):

| channel          | provider     | shipping today                             |
|------------------|--------------|--------------------------------------------|
| `direct_mail`    | `lob`        | yes — wires through `app/routers/direct_mail.py` |
| `direct_mail`    | `manual`     | reserved for ops-driven sends              |
| `email`          | `emailbison` | scaffold-only; integration is future work  |
| `email`          | `manual`     | reserved                                   |
| `voice_outbound` | `vapi`       | yes — config via `voice_ai_campaign_configs` |
| `voice_outbound` | `twilio`     | yes — TwiML IVR substrate                  |
| `sms`            | `twilio`     | yes — sms_messages.channel_campaign_id     |

The DB-level `CHECK` constraints are deliberately permissive (any of `lob`,
`emailbison`, `twilio`, `vapi`, `manual` per channel); the tighter
application-layer set keeps unsupported combinations from being created
without locking the schema in.

## Scheduling: calendar offsets within a campaign

Cross-channel calendar choreography is the only scheduling primitive — there
is no per-lead state machine and no conditional triggers. Each
channel_campaign has `start_offset_days` (a non-negative integer); on
activation the API computes:

```
scheduled_send_at = campaign.start_date + start_offset_days days  (UTC midnight)
```

If the campaign has no `start_date`, `scheduled_send_at` is left NULL — the
scheduler treats those channel_campaigns as "send immediately". A typical
staggered campaign looks like:

```
campaign: "Q2 lapsed-insurance MCs", start_date = 2026-05-01
├── channel_campaign #1: direct_mail, start_offset_days = 0   → fires 2026-05-01
├── channel_campaign #2: email,       start_offset_days = 7   → fires 2026-05-08
└── channel_campaign #3: voice_outbound, start_offset_days = 12 → fires 2026-05-13
```

Activation is the only time `scheduled_send_at` is computed; subsequent
schedule edits go through `PATCH /api/v1/channel-campaigns/{id}` and the
next `POST /api/v1/channel-campaigns/{id}/activate` (after pause/resume)
re-computes from the current campaign `start_date` + current
`start_offset_days`.

## Designs and audiences

`audience_spec_id` is an opaque UUID pointing at a row in the
`audience_specs` table in **data-engine-x**. Because that table lives in a
separate database, no foreign key is enforced; validity is checked by the
caller before activation. `audience_snapshot_count` captures the count at
channel_campaign-create time so reports can compare planned vs. actual reach.

`design_id` references `dmaas_designs(id)`. The application layer enforces
that the design's `brand_id` matches the channel_campaign's brand
(inherited from the parent campaign). The DB layer enforces that any
non-archived `channel='direct_mail'` channel_campaign has a non-NULL
`design_id` via the `channel_campaigns_design_required_for_direct_mail`
CHECK constraint.

`dmaas_scaffolds` is platform-shared (not brand-scoped). channel_campaigns
reference **designs**, not scaffolds.

## Customer-facing UI flattening

For self-serve customers running single-channel campaigns (today: GTMDirect
on direct mail), the UI hides the channel_campaign layer and presents the
campaign as the only object. The platform creates one channel_campaign
implicitly under the campaign with the same name. When the customer later
wants to add a second channel — say, follow-up emails — the
channel_campaign layer surfaces and the customer can attach a new
channel_campaign to the same campaign.

For operator-driven multi-channel campaigns (the GTMOperator side), both
layers are visible from the start.

## Analytics tagging

Every analytics event must carry the six-tuple
`(organization_id, brand_id, campaign_id, channel_campaign_id, channel,
provider)`. The tagging contract is enforced through
`app/services/analytics.py`:

```python
from app.services.analytics import emit_event

await emit_event(
    event_name="direct_mail_piece_created",
    channel_campaign_id=piece.channel_campaign_id,
    properties={"piece_type": "postcard", "cost_cents": 84},
    clickhouse_table="direct_mail_piece_events_ch",
)
```

The helper resolves the org/brand/campaign/channel/provider from the
channel_campaign id; callers cannot bypass it. Events without a resolvable
context raise `AnalyticsContextMissing` rather than emit untagged rows.

## REST surface

```
POST   /api/v1/campaigns
GET    /api/v1/campaigns
GET    /api/v1/campaigns/{campaign_id}
PATCH  /api/v1/campaigns/{campaign_id}
POST   /api/v1/campaigns/{campaign_id}/archive

POST   /api/v1/channel-campaigns
GET    /api/v1/channel-campaigns
GET    /api/v1/channel-campaigns/{channel_campaign_id}
PATCH  /api/v1/channel-campaigns/{channel_campaign_id}
POST   /api/v1/channel-campaigns/{channel_campaign_id}/activate
POST   /api/v1/channel-campaigns/{channel_campaign_id}/pause
POST   /api/v1/channel-campaigns/{channel_campaign_id}/resume
POST   /api/v1/channel-campaigns/{channel_campaign_id}/archive
```

All endpoints are organization-scoped via `require_org_context` (the active
org is resolved by `X-Organization-Id`). Platform operators can drive any
org by setting that header. The `organization_id` on rows always comes from
the auth context, never the request body, so a member of org A cannot
create a campaign in org B by tampering with the payload.

## History

The two-layer model originally shipped under different names:

* `business.gtm_motions` — the umbrella (renamed to `business.campaigns`
  in 0022).
* `business.campaigns` — the channel-typed unit (renamed to
  `business.channel_campaigns` in 0022).

The pre-0021 single-channel `business.campaigns` table was renamed to
`business.campaigns_legacy` in 0021 as a recovery fallback during the
two-layer migration; 0022 dropped it after confirming zero rows existed
in production at apply time and that no `audit_events` rows of action
`campaign.legacy_migrated.review_required` were pending operator
reconciliation.
