# GTM motions + channel-typed campaigns

This document describes the two-layer outreach model introduced in migration
`0021_gtm_motions_and_campaigns.sql` and the application surface at
`app/services/gtm_motions.py`, `app/services/campaigns.py`,
`app/routers/gtm_motions.py`, and `app/routers/campaigns_v2.py`.

## Hierarchy

```
business.organizations
  └── business.brands                 (brand belongs to one organization)
        └── business.gtm_motions      (motion belongs to one brand)
              └── business.campaigns  (campaign belongs to one motion, has one channel + provider)
                    ├── direct_mail_pieces.campaign_id (channel='direct_mail', provider='lob')
                    ├── call_logs.campaign_id          (channel='voice_outbound', provider in {vapi,twilio})
                    └── sms_messages.campaign_id       (channel='sms', provider='twilio')
```

A **gtm_motion** is the umbrella outreach unit. It is channel-agnostic and
count-agnostic. One per cohesive go-to-market push (e.g. "Q2 outreach to
lapsed-insurance MCs"). It carries `name`, `description`, `start_date`,
`status`, and free-form `metadata`.

A **campaign** is the channel-typed execution unit underneath a motion. Each
campaign represents one channel's execution against an audience, sent via one
provider. It carries the audience reference (`audience_spec_id`, an opaque
UUID into the data-engine-x audience_specs table — not FK-enforced because
that table lives in a separate database), the schedule (`start_offset_days`
and the computed `scheduled_send_at`), and channel-specific config in
`schedule_config` and `provider_config`.

A motion has 1..N campaigns. A campaign always belongs to exactly one motion.

## Channel / provider matrix

The `(channel, provider)` enum tuple is restricted at the application layer
(see `VALID_CHANNEL_PROVIDER_PAIRS` in `app/models/gtm.py`):

| channel          | provider     | shipping today                             |
|------------------|--------------|--------------------------------------------|
| `direct_mail`    | `lob`        | yes — wires through `app/routers/direct_mail.py` |
| `direct_mail`    | `manual`     | reserved for ops-driven sends              |
| `email`          | `emailbison` | scaffold-only; integration is future work  |
| `email`          | `manual`     | reserved                                   |
| `voice_outbound` | `vapi`       | yes — config via `voice_ai_campaign_configs` |
| `voice_outbound` | `twilio`     | yes — TwiML IVR substrate                  |
| `sms`            | `twilio`     | yes — sms_messages.campaign_id             |

The DB-level `CHECK` constraints are deliberately permissive (any of `lob`,
`emailbison`, `twilio`, `vapi`, `manual` per channel); the tighter
application-layer set keeps unsupported combinations from being created
without locking the schema in.

## Scheduling: calendar offsets within a motion

Cross-channel calendar choreography is the only scheduling primitive — there
is no per-lead state machine and no conditional triggers. Each campaign has
`start_offset_days` (a non-negative integer); on activation the API computes:

```
scheduled_send_at = motion.start_date + start_offset_days days  (UTC midnight)
```

If the motion has no `start_date`, `scheduled_send_at` is left NULL — the
scheduler treats those campaigns as "send immediately". A typical staggered
motion looks like:

```
motion: "Q2 lapsed-insurance MCs", start_date = 2026-05-01
├── campaign #1: direct_mail, start_offset_days = 0   → fires 2026-05-01
├── campaign #2: email,       start_offset_days = 7   → fires 2026-05-08
└── campaign #3: voice_outbound, start_offset_days = 12 → fires 2026-05-13
```

Activation is the only time `scheduled_send_at` is computed; subsequent
schedule edits go through `PATCH /api/v1/campaigns/{id}` and the next
`POST /api/v1/campaigns/{id}/activate` (after pause/resume) re-computes from
the current motion `start_date` + current `start_offset_days`.

## Designs and audiences

`audience_spec_id` is an opaque UUID pointing at a row in the
`audience_specs` table in **data-engine-x**. Because that table lives in a
separate database, no foreign key is enforced; validity is checked by the
caller before activation. `audience_snapshot_count` captures the count at
campaign-create time so reports can compare planned vs. actual reach.

`design_id` references `dmaas_designs(id)`. The application layer enforces
that the design's `brand_id` matches the campaign's brand (inherited from
the parent motion). The DB layer enforces that any non-archived
`channel='direct_mail'` campaign has a non-NULL `design_id` via the
`campaigns_design_required_for_direct_mail` CHECK constraint.

`dmaas_scaffolds` is platform-shared (not brand-scoped). Campaigns reference
**designs**, not scaffolds.

## Customer-facing UI flattening

For self-serve customers running single-channel campaigns (today: GTMDirect
on direct mail), the UI hides the motion layer and presents the campaign as
the only object. The platform creates the motion implicitly with the same
name as the campaign and sets `motion.start_date = NULL`. When the customer
later wants to add a second channel — say, follow-up emails — the motion
surfaces and the customer can attach a new campaign to the same motion.

For operator-driven multi-channel motions (the GTMOperator side), both
layers are visible from the start.

## Analytics tagging

Every analytics event must carry the six-tuple
`(organization_id, brand_id, gtm_motion_id, campaign_id, channel, provider)`.
The tagging contract is enforced through `app/services/analytics.py`:

```python
from app.services.analytics import emit_event

await emit_event(
    event_name="direct_mail_piece_created",
    campaign_id=piece.campaign_id,
    properties={"piece_type": "postcard", "cost_cents": 84},
    clickhouse_table="direct_mail_piece_events_ch",
)
```

The helper resolves the org/brand/motion/channel/provider from the
campaign id; callers cannot bypass it. Events without a resolvable campaign
context raise `AnalyticsContextMissing` rather than emit untagged rows.

## Legacy migration

The pre-0021 `business.campaigns` table was voice/SMS-flavored and uses a
composite (id, brand_id) FK from voice_*/sms_messages tables. Migration 0021:

1. Drops the composite FK constraints on every child table.
2. Renames the existing table to `business.campaigns_legacy`.
3. Creates the new channel-typed `business.campaigns`.
4. For every legacy row, inserts one new motion + one new campaign with
   `channel`/`provider` inferred from which child tables reference the
   legacy id (`call_logs` → `voice_outbound`, `sms_messages` → `sms`).
5. Repoints every child table's `campaign_id` column at the new ids via a
   per-row mapping built in a TEMP table.
6. Adds new single-key FKs from each child table to `business.campaigns(id)`
   (brand consistency now enforced at the app layer instead of via composite
   FK).
7. Logs a `business.audit_events` row for every legacy campaign that was
   referenced by both `call_logs` and `sms_messages` — the channel
   inference is ambiguous and an operator must reconcile manually.

`business.campaigns_legacy` is left in place; a follow-up PR drops it after
all read paths are confirmed migrated.
