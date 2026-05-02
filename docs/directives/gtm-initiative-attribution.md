# Directive: GTM-initiative attribution — campaigns coupling, analytics dimension, recipient manifest

**Status:** Active. Independent of (and runs alongside) [docs/directives/gtm-pipeline-foundation.md](gtm-pipeline-foundation.md). Either can land first.

**Context:** Read [CLAUDE.md](../../CLAUDE.md), [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md), and [STATE_OF_HQ_X.md](../../STATE_OF_HQ_X.md) §2.1 (campaigns hierarchy) before starting.

**Scope clarification on autonomy:** Make strong engineering calls within scope. Do not modify the DMaaS / Lob / Dub adapters' internals beyond the small targeted changes called out here. Do not build the audience-materializer subagent (that's a separate directive). Do not touch Stripe / payment integration — `partner_contracts` rows already exist as the contract record, regardless of how payment is captured; this directive's manifest links to `partner_contracts.id`, full stop.

---

## 1. Why this directive exists

The pivot to owned-brand lead-gen makes `gtm_initiatives` the unit of partner economics — one paid 90-day reservation, one audience, one brand, one outreach engine. But schema-wise today:

- `gtm_initiatives` has no relationship to `business.campaigns`. They're sibling concepts under `business.brands`. No FK either direction.
- `business.channel_campaigns` has no `initiative_id`. Every `emit_event` call returns the canonical six-tuple `(org, brand, campaign, channel_campaign, step, channel, provider)` — no initiative dimension.
- `business.recipients` has no path to `partner_contracts`. There's no manifest of "these recipients were paid-for under this contract." Voice-agent routing and billing reconciliation both need this path; today it doesn't exist.

This directive establishes:

1. **`gtm_initiatives` 1:many `business.campaigns`** — one initiative can have multiple campaigns under it. Schema permits it explicitly; application code populates and enforces.
2. **`initiative_id` flowing through analytics** — denormalized onto `channel_campaigns` so `resolve_channel_campaign_context` returns it; `emit_event` includes it in every event's properties; Dub mints carry an `initiative:<id>` tag.
3. **`business.initiative_recipient_memberships`** — the manifest. One row per (initiative, recipient) tuple. Captures `partner_contract_id` + `data_engine_audience_id` denormalized for fast lookup.

What this directive does NOT do:
- Build the audience-materializer subagent that *populates* the manifest. Schema only here. Population is the next directive.
- Touch the channel/step materializer subagent (also next directive).
- Build or integrate Stripe. Payment capture is a separate workstream.
- Migrate or backfill any legacy DMaaS rows. Those keep `initiative_id = NULL` and roll up cleanly under "non-initiative."

---

## 2. Existing-state facts to verify before starting

- `business.campaigns` exists with FKs to `business.brands` and `business.organizations`. No `initiative_id` column today.
- `business.channel_campaigns` exists with FK to `business.campaigns`. No `initiative_id` column today.
- `business.gtm_initiatives` has FKs to `organizations`, `brands`, `demand_side_partners`, `partner_contracts` and a `data_engine_audience_id UUID NOT NULL` (cross-DB ref to DEX).
- `business.recipients` is org-scoped, channel-agnostic, naturally keyed `(organization_id, external_source, external_id)`.
- `app/services/analytics.py` exposes `emit_event(channel_campaign_step_id | channel_campaign_id, event_name, properties, ...)` and resolves the six-tuple via `resolve_channel_campaign_context` / `resolve_channel_campaign_step_context`.
- `app/dmaas/step_link_minting.py` builds Dub bulk-mint payloads with tags for step/campaign/brand. Folder = channel_campaign.
- The new `channel_campaigns.initiative_id` column is **load-bearing for the foundation directive's analytics rendering** (the run-row drilldown and per-initiative views) — but the foundation directive doesn't materialize channel_campaigns. It only creates `gtm_subagent_runs` rows. So this directive is cleanly independent of foundation in flight: no coordination needed.

---

## 3. Migrations

Filename convention: UTC-timestamp prefix per `CLAUDE.md`. One timestamp per logical change.

### 3.1 `<ts>_campaigns_initiative_link.sql`

```sql
ALTER TABLE business.campaigns
    ADD COLUMN initiative_id UUID NULL
        REFERENCES business.gtm_initiatives(id) ON DELETE RESTRICT;

CREATE INDEX idx_campaigns_initiative
    ON business.campaigns (initiative_id)
    WHERE initiative_id IS NOT NULL;

COMMENT ON COLUMN business.campaigns.initiative_id IS
    'When set, this campaign belongs to a GTM initiative (owned-brand lead-gen). '
    'Multiple campaigns may share an initiative_id (1:many). Legacy DMaaS rows '
    'predating the owned-brand pivot have initiative_id IS NULL. '
    'Invariant (application-enforced): when set, campaigns.brand_id MUST equal '
    'gtm_initiatives.brand_id. The materializer is the only writer; no DB '
    'trigger enforces this.';
```

### 3.2 `<ts>_channel_campaigns_initiative_denorm.sql`

```sql
ALTER TABLE business.channel_campaigns
    ADD COLUMN initiative_id UUID NULL
        REFERENCES business.gtm_initiatives(id) ON DELETE RESTRICT;

CREATE INDEX idx_channel_campaigns_initiative
    ON business.channel_campaigns (initiative_id)
    WHERE initiative_id IS NOT NULL;

COMMENT ON COLUMN business.channel_campaigns.initiative_id IS
    'Denormalized from parent campaigns.initiative_id for fast emit_event '
    'resolution (avoids one join per analytics event). Application-maintained: '
    'whatever code sets campaigns.initiative_id MUST also set the same value '
    'on every child channel_campaigns row. The materializer is the only such '
    'writer in the owned-brand pipeline.';
```

Backfill: not required. New owned-brand work writes both columns concurrently from day one.

### 3.3 `<ts>_initiative_recipient_memberships.sql`

```sql
CREATE TABLE business.initiative_recipient_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    initiative_id UUID NOT NULL
        REFERENCES business.gtm_initiatives(id) ON DELETE RESTRICT,
    partner_contract_id UUID NOT NULL
        REFERENCES business.partner_contracts(id) ON DELETE RESTRICT,
    recipient_id UUID NOT NULL
        REFERENCES business.recipients(id) ON DELETE RESTRICT,
    -- Frozen at materialization time. Denormalized from the initiative's
    -- audience spec so historical rows survive even if the initiative's
    -- spec pointer were ever rewritten.
    data_engine_audience_id UUID NOT NULL,
    -- Optional: which step / channel_campaign was the recipient first
    -- materialized into. Useful for debug; not load-bearing.
    first_seen_channel_campaign_id UUID
        REFERENCES business.channel_campaigns(id) ON DELETE SET NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Soft-delete: when a recipient is excluded from continued outreach
    -- under this initiative (eg. unsubscribe, suppression-list match,
    -- explicit operator removal). Voice-agent routing and active-contract
    -- queries filter `WHERE removed_at IS NULL`.
    removed_at TIMESTAMPTZ,
    removed_reason TEXT
);

-- Active membership uniqueness: a recipient can be in at most one ACTIVE
-- initiative-recipient row at a time. Removed rows do not block a new
-- active row, so re-adding after removal works.
CREATE UNIQUE INDEX uq_irm_active_recipient_per_initiative
    ON business.initiative_recipient_memberships (initiative_id, recipient_id)
    WHERE removed_at IS NULL;

-- Lookup-by-recipient (voice-agent inbound resolution path):
-- "find this recipient's active paid contract."
CREATE INDEX idx_irm_recipient_active
    ON business.initiative_recipient_memberships (recipient_id, partner_contract_id)
    WHERE removed_at IS NULL;

-- Lookup-by-contract (billing reconciliation, contract-fulfillment reporting):
-- "list all recipients paid-for under this contract."
CREATE INDEX idx_irm_contract_active
    ON business.initiative_recipient_memberships (partner_contract_id, added_at)
    WHERE removed_at IS NULL;

-- Lookup-by-audience-spec (anti-double-billing across initiatives that share
-- a spec id; flag conflicts at materialization time).
CREATE INDEX idx_irm_audience_spec
    ON business.initiative_recipient_memberships (data_engine_audience_id)
    WHERE removed_at IS NULL;

COMMENT ON TABLE business.initiative_recipient_memberships IS
    'Manifest of "what was paid for" per initiative. One row per '
    '(initiative, recipient) pair. Populated by the audience materializer '
    'at initiative materialization time. Read by voice-agent inbound '
    'routing, billing reconciliation, and overlap-detection logic. '
    'NOT a replacement for channel_campaign_step_recipients (which is the '
    'per-step membership) — this is the higher-grain "paid context" layer.';
```

### 3.4 No constraint trigger for the brand-consistency invariant

Skipping a DB-side trigger that enforces `campaigns.brand_id = gtm_initiatives.brand_id` when `campaigns.initiative_id IS NOT NULL`. Application-level enforcement only. The materializer is the sole writer in the owned-brand pipeline; that's a controllable surface. If a violation surfaces in practice, add the trigger then.

Same call applies to `channel_campaigns.initiative_id = parent_campaign.initiative_id` — application-maintained, no trigger.

---

## 4. Service-layer changes

### 4.1 `app/services/analytics.py` — extend the resolved context

`resolve_channel_campaign_context` and `resolve_channel_campaign_step_context` currently return a dict with the canonical six-tuple. Extend them to include `initiative_id`. Concrete shape after change:

```python
{
    "organization_id": "...",
    "brand_id": "...",
    "campaign_id": "...",
    "channel_campaign_id": "...",
    "channel_campaign_step_id": "...",  # only on the step variant
    "channel": "...",
    "provider": "...",
    "initiative_id": "..." | None,        # NEW
}
```

Reads `channel_campaigns.initiative_id` directly (denormalized — no join). Falls back to NULL for legacy rows.

`emit_event` then includes `initiative_id` in the properties payload it forwards to RudderStack + ClickHouse fan-out + customer-webhook fan-out (the customer-webhook surface is internal-only post-pivot per the strategic doc, but the fan-out machinery still works). RudderStack `track()` payload gains `initiative_id`. No new dimension table; just a new field in every event.

The chokepoint discipline holds — every emit goes through `emit_event`. New emit sites must include initiative_id automatically by virtue of going through the resolver.

### 4.2 New module: `app/services/initiative_recipient_memberships.py`

CRUD + the operationally-load-bearing query helpers. No router exposes this for v0; called only from internal services (the future audience-materializer; voice-agent inbound resolution; billing reconciliation crons).

```python
async def add_membership(
    initiative_id: UUID,
    partner_contract_id: UUID,
    recipient_id: UUID,
    data_engine_audience_id: UUID,
    first_seen_channel_campaign_id: UUID | None = None,
) -> dict:
    """
    INSERT ... ON CONFLICT (initiative_id, recipient_id) WHERE removed_at IS NULL
    DO NOTHING. Returns the row whether newly inserted or existing.
    """

async def remove_membership(
    initiative_id: UUID,
    recipient_id: UUID,
    reason: str,
) -> None:
    """Soft-delete: set removed_at + removed_reason."""

async def find_active_for_recipient(recipient_id: UUID) -> list[dict]:
    """
    Returns all active memberships for this recipient (in theory: 0 or 1, but
    the schema permits multiple if a recipient is in two non-overlapping-window
    initiatives simultaneously). Caller filters as needed.
    Voice-agent inbound resolution uses this.
    """

async def find_active_by_audience_spec(
    data_engine_audience_id: UUID,
) -> list[dict]:
    """
    Returns all active memberships sharing a frozen audience spec.
    Used by the materializer to detect 'this recipient already paid-for under
    a different initiative with the same spec' conflicts.
    """

async def list_active_for_contract(
    partner_contract_id: UUID,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict]:
    """Billing reconciliation + contract-fulfillment reporting."""

async def count_active_for_initiative(initiative_id: UUID) -> int:
    """Quick health metric for an in-flight initiative."""
```

### 4.3 `app/dmaas/step_link_minting.py` — initiative tag on Dub mints

In `_build_bulk_payload` (or the equivalent function that assembles tags), when the channel_campaign has `initiative_id`, append `initiative:<uuid>` to the tags list. One-line change. Existing tests that snapshot tag lists need their fixtures updated.

No other Dub-integration changes — the minting protocol, keying, and webhook flow stay identical.

### 4.4 `app/services/channel_campaigns.py` (or equivalent CRUD) — initiative_id pass-through

Wherever channel_campaigns get inserted, accept and persist `initiative_id` as an optional kwarg. The materializer (next directive) is the caller that will populate it. Any current insert paths that don't take it leave the column NULL — backwards-compatible.

### 4.5 `app/services/campaigns.py` — same pass-through for campaigns

Same shape: optional `initiative_id` kwarg on any campaign-creation path. Default NULL.

### 4.6 No router changes in this directive

This directive ships internal plumbing only. The frontend admin / analytics endpoint additions that surface initiative_id in customer-facing rollups belong in a follow-up — once the foundation directive's `/admin/initiatives` pages are live, they become the consumer that benefits from this work.

---

## 5. Tests / acceptance

### 5.1 Pytest

- `tests/test_migrations_initiative_link.py` — assert the three column/table additions land cleanly; assert the partial unique index on `initiative_recipient_memberships` enforces "one active row per (initiative, recipient)" but allows re-insertion after soft-delete.
- `tests/test_analytics_initiative_id.py` — new unit tests for `resolve_channel_campaign_context` returning `initiative_id` when set; returning None when null. End-to-end: `emit_event` payload to RudderStack mock includes `initiative_id`. Existing `test_analytics_*` tests need their assertion shapes updated for the new field — do not skip them.
- `tests/test_initiative_recipient_memberships_service.py` — full coverage of the service module: add, soft-delete, the four lookup helpers, the unique-index conflict behavior on duplicate active inserts.
- `tests/test_step_link_minting_initiative_tag.py` — minting payload includes `initiative:<id>` when channel_campaign has `initiative_id`; absent otherwise. Snapshot test fixtures updated.

### 5.2 Acceptance

- `uv run pytest -q` passes (917 baseline + new tests; assert no regressions on existing tests by virtue of adding a new field to event payloads).
- Manual: run a query against dev DB to confirm a freshly-INSERTed `initiative_recipient_memberships` row honors the partial unique index — INSERT a row, INSERT a duplicate (same initiative_id + recipient_id, removed_at IS NULL), see the conflict.
- Manual: emit a fake `dub.click` event against a channel_campaign with `initiative_id` set; confirm via stdout log + RudderStack debug payload that `initiative_id` is in the event properties.

---

## 6. Out of scope

Defer to follow-up directives:

- **Audience-materializer subagent.** The agent that resolves the frozen DEX audience spec → creates `business.recipients` rows → creates `channel_campaign_step_recipients` memberships → **populates `initiative_recipient_memberships`** in the same transaction. This directive ships the manifest schema empty; the materializer fills it.
- **Channel/step materializer subagent.** Ditto — creates the campaigns + channel_campaigns + steps under an initiative, setting `initiative_id` on both layers as it goes.
- **Voice-agent inbound resolution.** Will use `find_active_for_recipient` once the voice-agent instantiator subagent lands.
- **Billing reconciliation cron.** Will use `list_active_for_contract` once Stripe / partner-payment integration lands.
- **Anti-double-billing enforcement.** Logic that rejects a new initiative whose audience spec overlaps an active one — uses `find_active_by_audience_spec`. The query is here; the enforcement is materializer-time logic.
- **Stripe / payment-method tables.** Entire workstream. Whenever it lands, it adds rows to `business.partner_contracts` (or a sibling table) capturing payment provenance — manifest links to `partner_contract_id`, doesn't care how the contract was paid for.
- **Frontend surfaces** for initiative-scoped analytics rollups. The analytics endpoints in `app/routers/analytics_*.py` get an `initiative_id` filter parameter in a follow-up directive once the foundation directive's admin pages are consuming them.
- **Renaming `dmaas_*` tables.** Cosmetic; not load-bearing; not done here.

---

## 7. Sequencing within the directive

1. Migrations 3.1 → 3.3 + run them locally.
2. `app/services/initiative_recipient_memberships.py` + tests (no callers yet — pure module).
3. `app/services/analytics.py` resolver extension + emit_event field extension + tests.
4. `app/services/campaigns.py` + `app/services/channel_campaigns.py` pass-through kwargs + tests.
5. `app/dmaas/step_link_minting.py` initiative-tag addition + tests.
6. PR. Title: `feat(gtm): initiative ↔ campaigns 1:many + initiative_id in analytics + recipient manifest schema`.

PR description must include:
- A short note that `business.campaigns.initiative_id` is the link allowing 1:many initiative → campaigns
- A note that the manifest table is empty and stays empty until the materializer directive lands
- Confirmation that legacy DMaaS rows are unaffected (initiative_id IS NULL everywhere; emit_event payloads include `initiative_id: null` for them, downstream consumers must handle)

---

## 8. Notes on what this enables

After this ships:

1. The materializer directive (next) can populate `campaigns.initiative_id`, `channel_campaigns.initiative_id`, and `initiative_recipient_memberships` rows in one transaction without further schema work.
2. The foundation directive's `/admin/initiatives/{id}` page (currently in flight) gains the ability to query "events scoped to this initiative" once any analytics endpoints are extended with an initiative_id filter — but rollup queries through the analytics service can already filter by initiative_id at the SQL level today.
3. The voice-agent instantiator (separate future directive) can implement inbound-call resolution as a single `find_active_for_recipient` lookup.
4. Stripe / partner-payment integration, when it lands, plugs into `business.partner_contracts` directly — manifest already references that table.

The substrate's done. Population and consumers come in subsequent directives, each unblocked by the schema being in place.
