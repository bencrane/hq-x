# PR notes — Rename campaigns hierarchy ([#22](https://github.com/bencrane/hq-x/pull/22))

Merged: 2026-04-30 (`b6d4cf3`).

This document is the post-ship summary for the rename pass that updated
the two-layer outreach model from #18 to use everyday language. Companion
long-form documentation: [`docs/campaign-model.md`](campaign-model.md). For
historical context on what originally shipped under the `gtm_motions` /
`campaigns` names, see the now-outdated
[`docs/campaign-model-pr-notes.md`](campaign-model-pr-notes.md).

## Why

The original two-layer model (#18) shipped under the names `gtm_motions`
(umbrella) and `campaigns` (channel-typed execution unit). In day-to-day
usage "campaign" is what people say when they mean the umbrella, and the
phrase "GTM motion" was operator-speak that didn't carry over to the
self-serve UI. The rename brings the schema/code in line with how the
team actually talks about this:

* **campaign** = the umbrella outreach effort (one per cohesive push).
* **channel campaign** = the per-channel execution under it.

Plus: drop `business.campaigns_legacy` (verified empty before drop), so
the schema no longer carries a fallback table from the original 0021
backfill that nobody is going to need.

## What changed

### Migration `0022_rename_campaigns_hierarchy.sql`

* `DROP TABLE business.campaigns_legacy` — verified zero rows + zero
  pending `audit_events` of action `campaign.legacy_migrated.review_required`
  before drop.
* `ALTER TABLE business.campaigns RENAME TO channel_campaigns` (the
  channel-typed execution unit). Renames its indexes + the design CHECK
  constraint to match.
* `ALTER TABLE business.gtm_motions RENAME TO campaigns` (the umbrella).
  Renames its indexes to match.
* `ALTER TABLE business.channel_campaigns RENAME COLUMN gtm_motion_id TO
  campaign_id` — the parent FK column.
* On every child table, `RENAME COLUMN campaign_id TO channel_campaign_id`:
  call_logs, sms_messages, voice_assistants, voice_phone_numbers,
  transfer_territories, voice_ai_campaign_configs,
  voice_campaign_active_calls, voice_campaign_metrics,
  voice_callback_requests.
* On `direct_mail_pieces` it's a *swap*, not a plain rename:
  - old `campaign_id` (channel-typed) → `channel_campaign_id`
  - old `gtm_motion_id` (umbrella)    → `campaign_id`
* Renames child-table FK constraints + indexes to match the new column
  names. Postgres preserves FK targets across the parent rename
  automatically (FKs reference table OIDs, not names).

The migration was applied against the hq-x Supabase project
(`imfwppinnfbptqdyraod`) before this PR was merged. Post-apply
verification confirmed:

* `business.campaigns` and `business.channel_campaigns` exist.
* `business.campaigns_legacy` and `business.gtm_motions` are gone.
* `direct_mail_pieces` has both `campaign_id` (umbrella) and
  `channel_campaign_id` (execution); the old `gtm_motion_id` column is
  gone.
* `call_logs.channel_campaign_id` is present.

### Application surface

* [`app/models/gtm.py`](../app/models/gtm.py) → renamed to
  [`app/models/campaigns.py`](../app/models/campaigns.py). Class renames:
  - `GtmMotionCreate / Update / Response` → `CampaignCreate / Update / Response`
  - old `CampaignCreate / Update / Response` → `ChannelCampaignCreate / Update / Response`
  - `MotionStatus` → `CampaignStatus`
  - old `CampaignStatus` → `ChannelCampaignStatus`
  - `VALID_CHANNEL_PROVIDER_PAIRS` unchanged.
* [`app/services/gtm_motions.py`](../app/services/gtm_motions.py) →
  renamed to [`app/services/campaigns.py`](../app/services/campaigns.py).
  All errors / functions renamed analogously
  (`create_motion` → `create_campaign`,
  `archive_motion` → `archive_campaign`, etc.).
* [`app/services/campaigns.py`](../app/services/campaigns.py) (the
  channel-typed one) → renamed to
  [`app/services/channel_campaigns.py`](../app/services/channel_campaigns.py).
  Functions renamed (`create_campaign` → `create_channel_campaign`, etc.).
  `get_campaign_context()` is now `get_channel_campaign_context()` and
  returns `(organization_id, brand_id, campaign_id, channel_campaign_id,
  channel, provider)` — the umbrella `campaign_id` plus the execution-unit
  id.
* [`app/services/analytics.py`](../app/services/analytics.py) —
  `emit_event()` now takes `channel_campaign_id` and resolves the
  six-tuple from there.
* [`app/routers/gtm_motions.py`](../app/routers/gtm_motions.py) →
  [`app/routers/campaigns.py`](../app/routers/campaigns.py). Mounted at
  `/api/v1/campaigns`.
* [`app/routers/campaigns_v2.py`](../app/routers/campaigns_v2.py) →
  [`app/routers/channel_campaigns.py`](../app/routers/channel_campaigns.py).
  Mounted at `/api/v1/channel-campaigns`.

### Caller updates (column rename + field-name churn)

| File | What changed |
|---|---|
| [`app/routers/voice_campaigns.py`](../app/routers/voice_campaigns.py) | `_validate_campaign_in_brand` → `_validate_channel_campaign_in_brand` (queries `business.channel_campaigns` now). Path param renamed to `channel_campaign_id`. |
| [`app/routers/direct_mail.py`](../app/routers/direct_mail.py) + [`app/direct_mail/persistence.py`](../app/direct_mail/persistence.py) | Request field renamed to `channel_campaign_id`; persistence layer takes `channel_campaign_id` (execution) and `campaign_id` (umbrella) and writes both columns on the piece row. |
| [`app/routers/voice.py`](../app/routers/voice.py), [`voice_ai.py`](../app/routers/voice_ai.py), [`voice_analytics.py`](../app/routers/voice_analytics.py), [`vapi_calls.py`](../app/routers/vapi_calls.py), [`vapi_webhooks.py`](../app/routers/vapi_webhooks.py), [`sms.py`](../app/routers/sms.py), [`outbound_calls.py`](../app/routers/outbound_calls.py) + the matching services | Every SQL `WHERE campaign_id = %s` and request-body field updated to `channel_campaign_id`. |
| [`app/main.py`](../app/main.py) | Mounts the renamed routers. |

### URL changes

* `POST /api/v1/gtm-motions` → `POST /api/v1/campaigns`
* `POST /api/v1/campaigns` → `POST /api/v1/channel-campaigns`
* All sub-routes (`{id}/archive`, `{id}/activate`, etc.) follow the
  same pattern.
* The legacy brand-axis voice URL prefix
  `/api/brands/{brand_id}/voice/campaigns/...` is preserved for
  back-compat, but the path param inside is renamed
  `/{channel_campaign_id}/config`.

### Docs

* `docs/gtm-model.md` → [`docs/campaign-model.md`](campaign-model.md),
  rewritten with the new terminology, REST surface section, and a brief
  history note pointing back at the original names.
* The previous PR notes file (originally
  `docs/gtm-model-pr-notes.md`, renamed to
  [`docs/campaign-model-pr-notes.md`](campaign-model-pr-notes.md)) is
  flagged at the top as outdated and points at this file.

### Tests

* `tests/test_gtm_motions_pure.py` → `tests/test_campaigns_pure.py`
  (17 tests; logic unchanged, identifiers updated).
* `tests/test_gtm_services_db_fake.py` →
  `tests/test_campaigns_services_db_fake.py` (16 tests; the in-memory
  DB fake updated to dispatch on the new SQL — `business.campaigns` /
  `business.channel_campaigns`, `campaign_id` parent FK on
  channel_campaigns rows, etc.).
* Full pytest suite: 352 passed.
* `ruff check` clean on every file touched in this PR. Four E501 long-line
  warnings remain in unrelated files (`vapi_webhooks.py`, `sms.py`,
  `outbound_calls.py`); those pre-date this PR and were not introduced
  by the rename.

## Decisions worth flagging

1. **Rename, not deprecation alias.** No backwards-compat shims for the
   old endpoint URLs (`/api/v1/gtm-motions`, the old
   `/api/v1/campaigns` channel-typed surface) were added. The hq-x
   project is fresh enough that no external clients depend on those
   URLs yet, and adding aliases would prolong the dual-naming era.

2. **`campaigns_legacy` dropped now, not in a follow-up.** #18's notes
   said "drop in a follow-up after read paths confirm migrated"; that
   condition was met (no callers reference the old shape) AND the
   audit query confirmed zero rows + zero needs-review entries on the
   actual database, so the safety net had nothing to protect.

3. **`direct_mail_pieces` swap was straight-line, no shadow column.**
   PG lets you `RENAME COLUMN x TO y` and `RENAME COLUMN z TO x` in
   sequence as long as the names don't collide mid-statement. We ran
   the two renames sequentially in the migration; no temp column or
   data copy was needed.

4. **Voice tables don't get a denormalized umbrella `campaign_id`.**
   Only `direct_mail_pieces` carries both `channel_campaign_id` and
   `campaign_id`. Voice/SMS rows resolve the umbrella via the FK chain
   (call_logs → channel_campaign → campaign). Out of scope for the
   rename PR; revisit if cross-channel analytics queries against
   call_logs / sms_messages start needing a fast umbrella join.

## Out of scope (still future work, unchanged from #18)

* EmailBison provider wiring.
* Frontend UI for campaigns + channel_campaigns.
* Analytics router and dashboards.
* Per-lead state across channels.
* Campaign templates / cloning.
* Cross-motion analytics rollup.

## Cleanup follow-ups

None pending from this PR. The rename is fully landed.

The follow-ups originally listed in #18's PR notes are also resolved:

* ✅ Drop `business.campaigns_legacy` — done in 0022.
* ⏳ `NOT NULL` on `direct_mail_pieces.channel_campaign_id` /
  `campaign_id` — still pending. Worth doing once a real-data backfill
  confirms zero orphans on production sends.
* ⏳ Reconciliation of `audit_events` with
  `action='campaign.legacy_migrated.review_required'` — N/A on hq-x
  (zero rows existed at apply time).
