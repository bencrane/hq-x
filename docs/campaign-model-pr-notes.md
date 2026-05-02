# PR notes — GTM motions + channel-typed campaigns ([#18](https://github.com/bencrane/hq-x/pull/18))

> **⚠️ OUTDATED — superseded by [`docs/campaign-rename-pr-notes.md`](campaign-rename-pr-notes.md).**
>
> This file describes the work that originally shipped under the names
> `gtm_motions` and `campaigns`. Those tables and code have since been
> renamed:
>
> * `business.gtm_motions` → `business.campaigns` (umbrella)
> * `business.campaigns`   → `business.channel_campaigns` (channel-typed
>   execution unit)
>
> See migration `0022_rename_campaigns_hierarchy.sql` and PR
> [#22](https://github.com/bencrane/hq-x/pull/22). For current
> terminology, refer to [`docs/campaign-model.md`](campaign-model.md).
>
> The content below is preserved unmodified as a historical record of
> what shipped in #18 — read it for the "why" / "what was deferred" /
> "blast radius" context, but mentally translate every "motion" to
> "campaign" and every "campaign" to "channel campaign".

Merged: 2026-04-29 (`f1eeb35`).

This document is the post-ship summary for the work that introduced the
two-layer outreach model. Companion long-form documentation lives in
[`docs/campaign-model.md`](campaign-model.md); this file captures *what
shipped in this PR*, *what was deferred*, and *the migration's blast
radius* so a future agent picking up the cleanup work has the context.

## Why

`docs/tenancy-state.md` and migration `0020_organizations_tenancy.sql`
established `business.organizations` as the root tenant for paying-customer
operational data and `business.brands` as a child entity. The next layer
down — campaigns — was still single-channel-flavored (voice/SMS only) and
brand-scoped, with no umbrella concept for cross-channel outreach.

This PR introduces:

* **`business.gtm_motions`** — the umbrella outreach motion. Org-scoped,
  brand-bound, channel-agnostic, count-agnostic.
* **`business.campaigns`** (rebuilt) — channel-typed execution unit. One
  per channel run inside a motion.

A motion has 1..N campaigns. Self-serve customers running a single channel
(e.g. GTMDirect direct-mail-only) see only "campaign" in the UI; the motion
is created implicitly. Operator-driven multi-channel motions surface both
layers.

## What shipped

### Migration `0021_gtm_motions_and_campaigns.sql`

* `business.gtm_motions` — `(id, organization_id, brand_id, name, status,
  start_date, metadata, archived_at, …)`. Status enum is
  `draft|active|paused|completed|archived`.
* `business.campaigns` — rebuilt around
  `(channel, provider, audience_spec_id, start_offset_days,
  scheduled_send_at, schedule_config, provider_config, design_id)`.
  CHECK constraints on enums; a `campaigns_design_required_for_direct_mail`
  CHECK that requires `design_id` for any non-archived `direct_mail`
  campaign.
* The pre-existing `business.campaigns` is renamed to `campaigns_legacy`
  and the composite `(id, brand_id)` FKs from voice_*/sms_messages/call_logs
  are dropped.
* For every legacy row the migration generates one motion id and one new
  campaign id in a TEMP `_campaign_legacy_mapping` table, then:
  - Inserts the motion (`'Legacy: ' || legacy_name`, status mapped from
    legacy free-text status).
  - Inserts the new campaign with `(channel, provider)` inferred from
    which child tables reference the legacy id (`call_logs` → voice_outbound,
    `sms_messages` → sms; `assistant_substrate` picks `vapi` vs `twilio`).
  - Repoints every child table's `campaign_id` column at the new id.
  - Re-adds single-key FKs to `business.campaigns(id)`. Brand consistency
    is now enforced at the application layer instead of via composite FK.
  - Writes a `business.audit_events` row for every legacy campaign that
    was referenced by both `call_logs` AND `sms_messages` — channel
    inference is ambiguous and an operator must reconcile manually.
* Adds `direct_mail_pieces.campaign_id` and `direct_mail_pieces.gtm_motion_id`
  (both nullable; existing rows pre-date the campaign concept).

### Application surface

* [`app/models/gtm.py`](../app/models/gtm.py) — Pydantic models +
  `VALID_CHANNEL_PROVIDER_PAIRS` (the application-layer matrix; tighter
  than the DB enum).
* [`app/services/gtm_motions.py`](../app/services/gtm_motions.py) — CRUD
  with brand/org consistency check and cascade-archive to child campaigns.
  Pure `compute_scheduled_send_at(motion_start_date, offset_days)` for
  schedule arithmetic.
* [`app/services/campaigns.py`](../app/services/campaigns.py) — CRUD plus
  `activate / pause / resume / archive` with explicit status-transition
  guards. Channel-specific validation: `direct_mail` requires
  `design_id` and the design must belong to the campaign's brand. Also
  exports `get_campaign_context()` for analytics tagging.
* [`app/services/analytics.py`](../app/services/analytics.py) —
  `emit_event()` resolves the canonical six-tuple
  `(organization_id, brand_id, gtm_motion_id, campaign_id, channel,
  provider)` from the campaign id and refuses to emit untagged events.
* [`app/routers/gtm_motions.py`](../app/routers/gtm_motions.py) and
  [`app/routers/campaigns_v2.py`](../app/routers/campaigns_v2.py) —
  org-scoped REST surface gated on `require_org_context`. Platform
  operators drive other orgs by setting `X-Organization-Id`, the same
  pattern as `gtm-motions`. The motion's/campaign's `organization_id`
  is always taken from the auth context, never the request body.

### Caller updates

| File | Before | After |
|---|---|---|
| [`app/routers/voice_campaigns.py`](../app/routers/voice_campaigns.py) | queried `business.campaigns` w/ `deleted_at IS NULL` | queries new table w/ `archived_at IS NULL` AND `channel='voice_outbound'` |
| [`app/routers/direct_mail.py`](../app/routers/direct_mail.py) + [`app/direct_mail/persistence.py`](../app/direct_mail/persistence.py) | no campaign concept | optional `campaign_id` on `_create_piece`; validated against new table; both `campaign_id` and `gtm_motion_id` stamped on the piece row |
| [`app/main.py`](../app/main.py) | — | mounts `/api/v1/gtm-motions` and `/api/v1/campaigns` |
| `app/routers/vapi_campaigns.py` | passthrough to Vapi API | unchanged — does not touch `business.campaigns` |
| `app/routers/direct_mail.py` Lob `/campaigns` proxy | passthrough to Lob API | unchanged — Lob's own concept, not ours |

### Docs

* [`docs/gtm-model.md`](gtm-model.md) — hierarchy, channel/provider matrix,
  calendar-offset scheduling, design/audience references, customer-facing
  UI flattening rules, and the legacy migration story.

### Tests

* `tests/test_gtm_motions_pure.py` — 17 pure-function tests covering
  schedule arithmetic, channel/provider validation, design-required
  guard, and Pydantic model coercion.
* `tests/test_gtm_services_db_fake.py` — 16 service-level tests using
  an in-memory `get_db_connection` fake. Covers create/get/list/update/
  archive on motions, channel-specific creation validation on campaigns,
  org-isolation, and the activate → pause → resume → archive lifecycle
  including `scheduled_send_at` computation from motion start_date.
* Full suite: 343 passed.
* `ruff check` clean on every touched file.

## Decisions worth flagging

1. **Composite FKs were dropped, not migrated.** The pre-PR voice/SMS
   tables used `FOREIGN KEY (campaign_id, brand_id) REFERENCES
   business.campaigns(id, brand_id)` to enforce that a child row's
   brand matches its campaign's brand. The new `business.campaigns(id)`
   is single-key; brand consistency now lives in the application layer
   (services validate brand-org-design alignment on write). Trade-off:
   we lose DB-level brand-consistency enforcement, but we gain a single
   place to read campaigns from.

2. **`direct_mail_pieces.campaign_id` is NULLABLE.** Existing pieces
   pre-date the campaign concept, and the directive says NOT NULL is
   only safe "after backfill confirms zero orphans". A follow-up should
   tighten this once new pieces are confirmed to always carry a
   campaign id.

3. **`business.campaigns_legacy` is left in place.** Per directive:
   drop in a follow-up after all read paths confirm migrated.

4. **Channel inference on legacy backfill is best-effort.** A legacy
   campaign referenced by both `call_logs` and `sms_messages` is
   defaulted to `voice_outbound` AND audit-logged via
   `business.audit_events.action = 'campaign.legacy_migrated.review_required'`.
   Operators must reconcile each flagged row by hand.

5. **EmailBison is scaffold-only.** The `email`/`emailbison` channel-
   provider pair is accepted by the model and persists correctly, but
   no integration code wires it. This is intentional — the directive
   said schema must accommodate, wiring is future work.

## Out of scope (future work)

* EmailBison provider wiring.
* Frontend UI for motions + campaigns.
* Analytics router and dashboards (separate workstream — see
  `AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`).
* Per-lead state across channels ("Capability B" from the prior
  conversation).
* Campaign templates / cloning.
* Cross-motion analytics rollup.

## Cleanup follow-ups

When all read paths are confirmed migrated and a real-data backfill
shows zero orphans:

1. `ALTER TABLE direct_mail_pieces ALTER COLUMN campaign_id SET NOT NULL`
   (and same for `gtm_motion_id` if stamping is universal).
2. `DROP TABLE business.campaigns_legacy`.
3. Reconcile any `business.audit_events` rows with
   `action = 'campaign.legacy_migrated.review_required'`.
