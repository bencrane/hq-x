# hq-x analytics buildout — PR notes

This file documents what shipped in the analytics buildout sequence
(slice 1a → slice 1f → Phase 2 RudderStack), what's deferred, and the
caveats encountered along the way.

For the original directive see
[`DIRECTIVE_HQX_ANALYTICS_REMAINDER.md`](../DIRECTIVE_HQX_ANALYTICS_REMAINDER.md)
(supersedes
[`DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md`](../DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md)).

## What shipped

Six analytics endpoints under `/api/v1/analytics`, plus the RudderStack
write fan-out behind the `emit_event()` chokepoint:

| Endpoint | Service | Source | PR |
|---|---|---|---|
| `GET /reliability` | `app/services/reliability_analytics.py` | postgres | merged in #28-area (slice 1a, commit `9fd9e02`) |
| `GET /campaigns/{campaign_id}/summary` | `app/services/campaign_analytics.py` | postgres | slice 1b |
| `GET /channel-campaign-steps/{step_id}/summary` | `app/services/step_analytics.py` | postgres | slice 1c |
| `GET /recipients/{recipient_id}/timeline` | `app/services/recipient_analytics.py` | postgres | slice 1d |
| `GET /direct-mail` | `app/services/direct_mail_analytics.py` | postgres | slice 1e |
| `GET /channel-campaigns/{channel_campaign_id}/summary` | `app/services/channel_campaign_analytics.py` | postgres | slice 1f |

Plus:

* `app/rudderstack.py` — lazy singleton wrapper around
  `rudder-sdk-python`. Wired into [`emit_event()`](../app/services/analytics.py:74)
  as the third fan-out hop (log → ClickHouse → RudderStack). FastAPI
  lifespan shutdown calls `flush()` to drain the SDK's batch queue.
  PR: Phase 2.

Every response payload carries `"source": "postgres"`. ClickHouse
remains the existing fire-and-forget no-op shim; nothing in this
buildout adds ClickHouse code paths.

## Architecture decisions captured in code

1. **Org isolation via single-WHERE-clause lookups.** Every endpoint
   that takes a resource id fetches the resource with `WHERE id = %s
   AND organization_id = %s` in the same statement. No two-step
   lookup that could leak via 200/404 timing. Tested per endpoint
   (`test_*_uses_single_where_clause`,
   `test_endpoint_uses_org_from_auth`).
2. **Org context always from auth, never from the URL.** Every
   endpoint depends on `require_org_context`; queries bind
   `user.active_organization_id` directly. There is no
   `?organization_id=` query param anywhere.
3. **Voice/SMS synthetic-step fallback.** `call_logs` and
   `sms_messages` don't carry `channel_campaign_step_id` or
   `recipient_id` today. Slice 1b and 1f surface a single synthetic
   step per voice/SMS channel_campaign and tag the cc with
   `voice_step_attribution: "synthetic"` /
   `sms_step_attribution: "synthetic"`. Slice 1c (per-step) returns
   404 for non-existent step ids — synthetic steps are not addressable.
   Slice 1d (recipient timeline) skips voice/SMS events but
   `summary.by_channel` includes those keys with zero counts so
   consumers see the forward-compat shape.
4. **OEX safety gates carry over.** Direct-mail analytics caps at
   93-day window (via the shared `_resolve_window`), 20k raw piece
   reads (raises `ValueError` → 400), and 50-row failure_reason
   breakdown.
5. **RudderStack `anonymous_id` is the org id.** hq-x has only
   platform-operator users today; the per-recipient `recipient_id`
   rides as a property inside the event payload, not as a user
   identifier.
6. **Latent bug fixed in `emit_event()`.** Phase 2 surfaced a pre-
   existing collision: `log_event(event, **fields)` takes `event`
   positionally, but the payload dict already contained an `event`
   key, so unpacking would `TypeError`. Existing call sites all
   stubbed `emit_event` in their tests, so the bug was latent. Fixed
   by building the log kwargs without the duplicate `event` key.

## What's deferred

Out of scope for this buildout, called out explicitly so future PRs
don't relitigate:

* **ClickHouse cluster provisioning.** The free trial expired and the
  product direction shifted. `app/clickhouse.py` stays as the existing
  no-op shim. Endpoints have no `?source=clickhouse` mode.
* **Voice/SMS step + recipient wiring.** Adding
  `channel_campaign_step_id` and `recipient_id` to `call_logs` and
  `sms_messages` is a separate workstream. When it lands, the slice
  1c voice-step path becomes a real query (no schema change required
  in the analytics layer; the synthetic block disappears once real
  per-step rows exist) and slice 1d's recipient timeline picks up
  voice/SMS events automatically.
* **EmailBison per-recipient analytics.** EmailBison adapter (#32)
  ships per-campaign analytics but doesn't yet emit per-recipient
  artifact rows tagged with `channel_campaign_step_id`. Slice 1f's
  `email` channel returns zeros + an empty `channel_specific.email`
  block; a future PR adds per-recipient breakdowns once the
  EmailBison projector wires the upstream context.
* **Endpoint pagination on slice 1b/1f totals.** Today the campaign
  rollup eagerly returns every channel_campaign + every step. Real
  customers will hit this fast; a future PR layers cursor pagination
  on the channel_campaigns and step lists.

## Caveats encountered

* **`call_logs.cost_total` is NUMERIC (dollars), not cents.** Slices
  1b and 1f convert to cents at aggregation time
  (`int(round(float(cost_dollars) * 100))`). When the
  `cost_breakdown` JSONB column eventually carries denormalized
  per-component cents, the conversion can move into the SQL.
* **`direct_mail_pieces` doesn't carry `organization_id` directly.**
  Org isolation flows through `business.brands.organization_id` on
  every piece query (every piece has a `brand_id`). Slice 1e's
  `_piece_filter_clause` codifies this and slice 1d's recipient
  timeline guards through both `s.organization_id` (step-tagged
  pieces) and `b.organization_id` (legacy ad-hoc operator sends).
* **Pagination shape on slice 1d.** `limit` defaults to 100, capped
  at 500 via FastAPI's `Query(le=500)`; the 422 response on overflow
  is intentional (FastAPI's default validation behavior) and the test
  asserts `status_code in (400, 422)` to remain agnostic to FastAPI
  version.

## Test count delta

Baseline before slice 1b: **489 passing** (post-rebase onto main with
EmailBison work).

After Phase 2: **571 passing**.

| PR | New tests | Cumulative |
|---|---|---|
| Slice 1b | +13 | 502 |
| Slice 1c | +12 | 514 |
| Slice 1d | +15 | 529 |
| Slice 1e | +17 | 546 |
| Slice 1f | +13 | 559 |
| Phase 2 | +12 | 571 |

Every slice was verified green at commit time; ruff clean on every
touched file.
