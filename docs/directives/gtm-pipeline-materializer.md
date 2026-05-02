# Directive: GTM-pipeline materializer — channel/step + audience + per-recipient fanout

**Status:** Active. Successor to [gtm-pipeline-foundation.md](gtm-pipeline-foundation.md). Builds on the foundation slice that landed in PRs hq-x #80 / managed-agents-x #9 / hq-command #14, plus the attribution slice in PR #81.

**Context:** Read first:
- [docs/handoff-gtm-pipeline-foundation-2026-05-01.md](../handoff-gtm-pipeline-foundation-2026-05-01.md) — operational state after foundation
- [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md) — model
- [docs/directives/gtm-pipeline-foundation.md](gtm-pipeline-foundation.md) — runtime + run-capture patterns this directive extends
- [docs/directives/gtm-initiative-attribution.md](gtm-initiative-attribution.md) — `initiative_id` propagation + `initiative_recipient_memberships` manifest
- `managed-agents-x/data/agents/gtm-sequence-definer/system_prompt.md` — the JSON output this directive's first new agent consumes
- [CLAUDE.md](../../CLAUDE.md)

**Scope clarification on autonomy:** Make strong engineering calls within scope. Do not modify the existing foundation subagents (`gtm-sequence-definer`, `gtm-master-strategist`, `gtm-per-recipient-creative`, or any of their verdicts). Do not refactor `gtm_pipeline.run_step`'s public signature — extend it. Do not build any new frontend in hq-command — frontend updates ship as a sibling directive after this lands. Do not touch DMaaS / Lob / Dub adapters' internals. Do not move the per-piece-cost table out of code (deferred per foundation's open decisions).

---

## 1. Why this directive exists

The foundation slice ships a working pipeline that produces a master strategy doc and a per-recipient creative for one fixture sample recipient. That's the seam-validation. To make the pipeline actually run an outreach campaign:

1. Channels and steps must be **materialized into DB** as `business.campaigns` + `business.channel_campaigns` + `business.channel_campaign_steps` rows under the initiative. Today nothing creates these.
2. The audience must be **resolved from the frozen DEX spec into `business.recipients` + memberships + the manifest**. Today nothing creates these.
3. Per-recipient creative must **fan out over the real audience**, not run once on a sample. Today `_assemble_per_recipient_creative` loads one sample recipient via `LIMIT 1`.

This directive fills all three gaps in one slice. After it lands, the pipeline produces every artifact needed to drive `activate_pieces_batch` in a follow-up directive — at which point the loop runs end-to-end against Lob test mode.

---

## 2. What this directive ships

| # | Component | Surface |
|---|---|---|
| 1 | `gtm-channel-step-materializer` actor + verdict | New MAGS agents in `managed-agents-x` |
| 2 | `gtm-audience-materializer` actor + verdict | New MAGS agents in `managed-agents-x` |
| 3 | Plan-execution layer in hq-x | `app/services/materializer_execution.py` |
| 4 | Per-recipient creative fanout | Trigger.dev child task + `gtm_pipeline` extension |
| 5 | `gtm_subagent_runs` schema extension | One migration — adds `recipient_id` + `channel_campaign_step_id` columns |
| 6 | Pipeline order extension | `PIPELINE_STEPS` updated in both Python + TS sides |
| 7 | Aggregate-runs endpoint | `GET /api/v1/admin/initiatives/{id}/runs/aggregated` |
| 8 | End-to-end exercise | Extended `seed_dat_gtm_pipeline_foundation.py` (or a sibling) |

Out of scope (sibling directive):
- Frontend updates in hq-command to render fanout aggregates (the new aggregate endpoint is the contract; frontend catches up after)
- Subagents #5 / #6 / #8 / #9 / #10 / #12a / #12b
- Render-and-submit pipeline (per-recipient creative DSL → final HTML/PDF → `activate_pieces_batch`)
- Voice-agent instantiation
- Email copy author (#9)

---

## 3. Existing-state facts to verify before starting

- `business.campaigns` has `initiative_id UUID NULL REFERENCES gtm_initiatives` (per attribution PR #81)
- `business.channel_campaigns` has `initiative_id UUID NULL REFERENCES gtm_initiatives` (denormalized, application-maintained)
- `business.initiative_recipient_memberships` exists, empty
- `app/services/initiative_recipient_memberships.py` ships the CRUD + lookup helpers; this directive is the first writer
- `app/services/analytics.py:resolve_channel_campaign_context` returns `initiative_id` in the context dict
- `app/dmaas/step_link_minting.py` adds `initiative:<id>` tag when `channel_campaign.initiative_id IS NOT NULL` (per PR #81)
- `app/services/dex_client.py` exposes `list_audience_members(audience_spec_id, limit, offset, bearer_token)` — verify the exact signature; this directive's audience materializer is the heaviest caller
- The `gtm-sequence-definer`'s output JSON shape is stable per the handoff §6 — that's the contract the channel-step materializer reads
- The `gtm-master-strategist` reads "5–10 sample audience members" inline today via a `LIMIT N` query against DEX; once recipients are materialized, it reads from `business.recipients` instead. This directive switches that read path.
- Foundation pipeline currently runs steps in order: `gtm-sequence-definer` → `gtm-master-strategist` → `gtm-per-recipient-creative`. This directive inserts new steps **between** sequence-definer and master-strategist.

---

## 4. Migration

Filename convention: UTC-timestamp prefix.

### 4.1 `<ts>_gtm_subagent_runs_recipient_step_columns.sql`

```sql
ALTER TABLE business.gtm_subagent_runs
    ADD COLUMN recipient_id UUID NULL
        REFERENCES business.recipients(id) ON DELETE RESTRICT,
    ADD COLUMN channel_campaign_step_id UUID NULL
        REFERENCES business.channel_campaign_steps(id) ON DELETE RESTRICT;

-- Drop the existing UNIQUE (initiative_id, agent_slug, run_index) constraint;
-- replace with one that includes recipient_id + step_id so fanout runs are
-- unique per recipient × step.
ALTER TABLE business.gtm_subagent_runs
    DROP CONSTRAINT gtm_subagent_runs_initiative_id_agent_slug_run_index_key;

CREATE UNIQUE INDEX uq_gtm_subagent_runs_per_fanout
    ON business.gtm_subagent_runs (
        initiative_id,
        agent_slug,
        COALESCE(recipient_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(channel_campaign_step_id, '00000000-0000-0000-0000-000000000000'::uuid),
        run_index
    );

-- Lookup: all per-recipient runs for an initiative (powers the aggregate endpoint).
CREATE INDEX idx_gtm_subagent_runs_recipient
    ON business.gtm_subagent_runs (initiative_id, agent_slug, recipient_id, channel_campaign_step_id)
    WHERE recipient_id IS NOT NULL;

COMMENT ON COLUMN business.gtm_subagent_runs.recipient_id IS
    'Set for per-recipient fanout agents (gtm-per-recipient-creative + verdict). '
    'NULL for initiative-scoped agents.';

COMMENT ON COLUMN business.gtm_subagent_runs.channel_campaign_step_id IS
    'Set when the run is scoped to a specific step (per-recipient creative). '
    'NULL for initiative-scoped agents (sequence-definer, master-strategist).';
```

The COALESCE in the unique index is the standard technique for "uniqueness with NULL-friendly columns." All-zero UUID is not a real recipient id, so it's a safe sentinel.

No other migrations needed. `business.campaigns` / `business.channel_campaigns` / `business.channel_campaign_steps` / `business.recipients` / `business.channel_campaign_step_recipients` / `business.initiative_recipient_memberships` all exist with the right shape.

---

## 5. MAGS subagent registrations

Four new MAGS agents — two actors, two verdicts. Each ships as a `setup_<slug>.py` script entry in `managed-agents-x/scripts/setup_gtm_agents.py`'s `AGENTS` map plus a `data/agents/<slug>/system_prompt.md` file. Default model: `claude-opus-4-7`. Pattern mirrors the existing six exactly.

### 5.1 `gtm-channel-step-materializer` (actor)

**MCPs:** none. The agent reasons over inputs and emits a plan; hq-x executes.

**Inputs (assembled by `gtm_pipeline._assemble_input`):**
- The full `gtm-sequence-definer` output (channels + touches + delay_days + estimated costs)
- The initiative row (brand_id, partner_id, partner_contract_id, data_engine_audience_id)
- acq-eng operator doctrine parameters
- The independent-brand doctrine markdown (for any framing decisions on `landing_page_config` placeholders)

**Output JSON contract:**

```json
{
  "campaign": {
    "name": "<derived from initiative + brand>",
    "description": "<one paragraph>",
    "metadata": { ... }
  },
  "channel_campaigns": [
    {
      "channel": "direct_mail",
      "provider": "lob",
      "name": "<derived>",
      "description": "<one paragraph>",
      "metadata": { ... }
    },
    { "channel": "email", "provider": "emailbison", ... },
    { "channel": "voice_inbound", "provider": "vapi", ... }
  ],
  "steps": [
    {
      "channel": "direct_mail",
      "step_index": 0,
      "name": "Touch 1 — postcard",
      "delay_days_from_previous": 0,
      "channel_specific_config": {
        "mailer_type": "postcard",
        "estimated_cost_cents": 150
      },
      "landing_page_config_placeholder": { ... },
      "metadata": { ... }
    },
    { ... per touch }
  ]
}
```

The `landing_page_config_placeholder` is a stub object with the doctrine-aware structure (see foundation directive §5.1's channel-tier framing rules). Real per-recipient/per-step landing-page personalization lands in a future subagent.

**Doctrine adherence rules:**
- Step count and channel mix MUST match the sequence-definer's plan exactly (no creative deviation; verdict will reject).
- `delay_days_from_previous` MUST match the sequence-definer's per-touch delays.
- The campaign + channel_campaign metadata MUST attribute back to the initiative_id (agent doesn't write the FK column — hq-x does — but the JSON should contain `initiative_id` for verification).

**System prompt seed:** "Translate the sequence-definer's channel + touch plan into the structural plan for materializing into hq-x's `business.campaigns`, `business.channel_campaigns`, and `business.channel_campaign_steps` tables. Do not invent additional touches, channels, or delays. Output a JSON object with `campaign`, `channel_campaigns[]`, and `steps[]` per the contract. Each step's `channel_specific_config` carries the mailer_type and estimated_cost from the sequence-definer plan, verbatim. Each step gets a `landing_page_config_placeholder` populated with the doctrine-aware shape (see independent-brand-doctrine.md for the channel-tier framing rules). The placeholder is filled in by a downstream subagent — your job is to leave the structural slot."

### 5.2 `gtm-channel-step-materializer-verdict`

**System prompt seed:** "Read the channel-step-materializer's plan and the original sequence-definer output. Verdict the plan: does the step count, channel mix, mailer types, and delays exactly match the sequence-definer's plan? Are required structural fields present per the JSON contract? Return strict `{ship: bool, issues: [...], redo_with: string|null}`. Reject (ship: false) if any structural field is missing or any deviation from the sequence-definer's plan is detected."

### 5.3 `gtm-audience-materializer` (actor)

**MCPs:** dex-mcp (for `count_audience_members`, `list_audience_members` — read-only).

**Inputs:**
- The initiative's `data_engine_audience_id`
- The sequence-definer's `audience_size_assumption` (int) and `total_estimated_outlay_cents`
- acq-eng operator doctrine parameters (capital outlay caps, sampling policy if any)
- The list of `channel_campaign_step_id`s materialized by the previous step (passed in via `upstream_outputs`)

**Output JSON contract:**

```json
{
  "decision": "materialize_full" | "materialize_capped" | "reject_size_mismatch",
  "actual_audience_size": <int>,
  "size_decision_reason": "<one paragraph>",
  "membership_plan": {
    "recipient_count": <int>,
    "memberships_per_step": [
      {"channel_campaign_step_id": "<uuid>", "expected_count": <int>}
    ]
  },
  "dub_minting_plan": {
    "channel_campaign_step_ids_requiring_links": ["<uuid>", ...],
    "expected_link_count": <int>
  }
}
```

**Decision logic:**
- If `actual_audience_size <= audience_size_assumption × 1.10` (10% slack): `materialize_full`
- If `actual_audience_size > audience_size_assumption × 1.10` AND outlay cap permits the larger size: `materialize_full` (with reasoning)
- If `actual_audience_size > audience_size_assumption × 1.10` AND outlay cap does NOT permit: `reject_size_mismatch` — verdict surfaces this as a block; pipeline halts. **No automatic sampling.** Sampling is a content decision and requires operator input.
- If `actual_audience_size < audience_size_assumption × 0.50`: `materialize_full` with a warning issue (audience smaller than budgeted; partner economics may shift)

**System prompt seed:** "Resolve the frozen DEX audience spec to a concrete count. Compare to the sequence-definer's `audience_size_assumption`. Decide `materialize_full` vs `reject_size_mismatch` per the rules in the directive. Output a JSON plan describing how many recipients to materialize, expected memberships per step, and which steps need Dub link minting (direct_mail steps only). Do NOT decide on sampling — if the actual size doesn't fit budget, reject and let the operator handle it."

### 5.4 `gtm-audience-materializer-verdict`

**System prompt seed:** "Verdict the audience-materializer's plan against the sequence-definer's `audience_size_assumption` and the doctrine's outlay constraints. Reject if the plan would materialize an audience that violates the operator doctrine's `max_capital_outlay_pct_of_revenue` or the contract's `max_capital_outlay_cents`. Reject if the plan claims to mint Dub links for non-direct-mail steps. Return strict `{ship, issues, redo_with}`."

### 5.5 Pattern adherence

All four new agents register via `managed-agents-x/scripts/setup_gtm_agents.py`'s `AGENTS` map. Bumping the foundation's six to ten. The handoff's §5.4 "register the 6 MAGS agents" step now registers ten. Each setup-script run captures the agent_id and creates the corresponding `business.gtm_agent_registry` row in hq-x via `scripts/register_gtm_agent.py`.

---

## 6. hq-x backend extensions

### 6.1 New module: `app/services/materializer_execution.py`

The plan-out → hq-x-execute pattern. Two top-level functions, one per materializer.

```python
async def execute_channel_step_plan(
    initiative_id: UUID,
    plan: dict,                        # actor's output_blob
) -> dict:
    """
    Inserts the campaigns / channel_campaigns / channel_campaign_steps rows
    in a single transaction. Sets initiative_id on both campaigns and
    channel_campaigns (denormalization per attribution directive).

    Returns:
        {
            campaign_id: UUID,
            channel_campaign_ids: {channel: UUID, ...},
            channel_campaign_step_ids: [UUID, ...] (in pipeline order)
        }

    Raises MaterializerExecutionError on FK violations, plan-shape
    mismatches, or doctrine violations the verdict somehow let through.
    """

async def execute_audience_plan(
    initiative_id: UUID,
    plan: dict,                        # actor's output_blob
    channel_campaign_step_ids: list[UUID],
) -> dict:
    """
    Pages through DEX members, executes recipients/memberships/manifest
    inserts in batches (1000 rows per transaction; not single-tx for
    the whole audience — that would lock too long).

    For each batch:
      1. Upsert business.recipients (ON CONFLICT (organization_id, external_source, external_id) DO UPDATE)
      2. Insert channel_campaign_step_recipients per (recipient × DM step)
      3. Insert initiative_recipient_memberships per recipient (ON CONFLICT DO NOTHING — partial unique handles re-runs)
      4. Bulk-mint Dub links per (DM step × recipient) via existing step_link_minting.bulk_mint_links

    Idempotent: re-running materialize_full on a partially-completed
    audience picks up where it left off. (Recipients upserted by natural
    key; memberships unique on (step, recipient); manifest unique partial
    on (initiative, recipient) WHERE removed_at IS NULL; dub_links unique
    on (step, recipient).)

    Returns:
        {
            recipient_count: int,
            membership_count: int,
            manifest_count: int,
            dub_link_count: int
        }
    """
```

DEX paging: page size 500, full audience materialization runs in N batches, each batch does the Dub bulk mint inline (Dub's batch is 100/req, so each 500-row batch fans out to 5 Dub HTTP calls).

### 6.2 `gtm_pipeline.run_step` extensions

Two extensions:

1. **For materializer agents:** after the actor returns its plan and the verdict ships, call the appropriate `materializer_execution.execute_*` function. Persist its return values into the run row's `output_blob` alongside the agent's plan, so the next step in the pipeline can read them. Concretely: `run_step` returns `output_blob = {plan: <actor_output>, executed: <execution_result>}` for materializer agents. Downstream agents read `executed.channel_campaign_step_ids` etc. via `_assemble_input`.

2. **For per-recipient agents:** `run_step` already takes the agent_slug. Add optional `recipient_id` and `channel_campaign_step_id` kwargs. When set, the function:
   - Includes them in the run row's columns
   - Includes them in the input assembly (per-recipient input loads ONLY that recipient's DEX row + the step's metadata, not all recipients)
   - Uses `recipient_id` and `channel_campaign_step_id` in the unique-index lookup so concurrent fanout invocations don't collide

### 6.3 Per-recipient creative input assembly

`_assemble_input` for `gtm-per-recipient-creative` changes:
- Input now reads ONE recipient by `recipient_id` (passed in)
- Input reads ONE step by `channel_campaign_step_id` (passed in)
- Reads `master_strategy` from the most recent succeeded `gtm-master-strategist` run for the initiative
- Reads `brand_content` for the initiative's brand
- Reads recipient's full DEX row from `business.recipients.metadata` JSONB (populated at materialization time)
- Reads the step's `channel_specific_config` for `mailer_type`

The "ONCE on first sample recipient" v0 hack in `_assemble_per_recipient_creative` is removed. Foundation's "run once" behavior is replaced by "run per (recipient × DM step)."

### 6.4 New module: `app/services/initiative_runs_aggregate.py`

```python
async def aggregate_runs(initiative_id: UUID) -> dict:
    """
    Returns per-step, per-status counts for the initiative's runs.
    Powers the aggregate endpoint without exposing 5000+ rows to the
    frontend at once.

    Shape:
        {
            agent_slug: {
                total_runs: int,
                latest_run_index: int,
                by_status: {queued: N, running: N, succeeded: N, failed: N, superseded: N},
                fanout: {
                    is_fanout: bool,
                    expected_count: int,    # populated for fanout agents from upstream materialization
                    completed_count: int,
                    failed_count: int
                }
            },
            ...
        }
    """
```

For non-fanout agents, `fanout.is_fanout = false` and the counts are 0/N/A.

For per-recipient agents, `expected_count` is read from the most recent audience-materializer run's `output_blob.executed.recipient_count × number_of_DM_steps` (i.e. how many fanout invocations the pipeline expects). `completed_count` is the count of `succeeded` rows; `failed_count` is `failed` rows.

### 6.5 Routers

Extend `app/routers/admin/initiatives.py`:

```
GET    /api/v1/admin/initiatives/{id}/runs/aggregated
       returns: aggregate_runs(initiative_id)
```

No other router changes. Existing per-run endpoints continue to work — they just see more rows.

Internal `/run-step` endpoint (`app/routers/internal/gtm_pipeline.py`) accepts new optional fields in body:

```
POST /internal/gtm/initiatives/{id}/run-step
body: {
    agent_slug: str,
    hint?: string,
    upstream_outputs?: dict,
    recipient_id?: UUID,                    # NEW (for per-recipient fanout)
    channel_campaign_step_id?: UUID         # NEW (for per-recipient fanout)
}
```

### 6.6 PIPELINE_STEPS update

In `app/services/gtm_pipeline.py`'s `PIPELINE_STEPS` constant (or wherever the order is encoded), insert:

```python
PIPELINE_STEPS = [
    StepConfig("gtm-sequence-definer",            "gtm-sequence-definer-verdict"),
    StepConfig("gtm-channel-step-materializer",   "gtm-channel-step-materializer-verdict"),    # NEW
    StepConfig("gtm-audience-materializer",       "gtm-audience-materializer-verdict"),        # NEW
    StepConfig("gtm-master-strategist",           "gtm-master-strategist-verdict"),
    StepConfig.fanout(                                                                          # NEW shape
        actor="gtm-per-recipient-creative",
        verdict="gtm-per-recipient-creative-verdict",
        fanout_kind="per_recipient_per_dm_step",
    ),
]
```

`StepConfig.fanout(...)` is a new factory that flags the step as fanout and tags the fanout pattern. The orchestrator (Trigger.dev side) reads this flag and switches behavior.

---

## 7. Trigger.dev workflow

`src/trigger/gtm-run-initiative-pipeline.ts` extends to handle fanout steps. New child task for per-recipient invocations.

### 7.1 New child task: `src/trigger/gtm-run-per-recipient-creative.ts`

```typescript
export const runPerRecipientCreative = task({
  id: "gtm.run-per-recipient-creative",
  // Concurrency limit per Trigger queue config — see §7.3 below
  run: async ({
    initiativeId,
    recipientId,
    channelCampaignStepId,
  }: {
    initiativeId: string;
    recipientId: string;
    channelCampaignStepId: string;
  }) => {
    // Same actor → verdict loop as the parent task, but scoped to one
    // (recipient × step). One MAX_VERDICT_RETRIES = 1 attempt.
    const actorResult = await callRunStep(initiativeId, "gtm-per-recipient-creative", {
      recipient_id: recipientId,
      channel_campaign_step_id: channelCampaignStepId,
    });
    if (actorResult.status === "failed") {
      // Don't fail the parent pipeline — record and proceed.
      return { recipient_id: recipientId, step_id: channelCampaignStepId, status: "actor_failed" };
    }

    const verdictResult = await callRunStep(initiativeId, "gtm-per-recipient-creative-verdict", {
      recipient_id: recipientId,
      channel_campaign_step_id: channelCampaignStepId,
      upstream_outputs: { "gtm-per-recipient-creative": actorResult.output_blob },
    });

    return {
      recipient_id: recipientId,
      step_id: channelCampaignStepId,
      status: verdictResult.output_blob?.ship ? "shipped" : "verdict_blocked",
    };
  },
});
```

### 7.2 Parent task fanout dispatch

In `src/trigger/gtm-run-initiative-pipeline.ts`, when the parent task hits a step flagged `is_fanout`:

```typescript
if (step.is_fanout) {
  // Read the upstream materializer's output to know what to fan over.
  const fanoutTargets = await hqxPost<FanoutTarget[]>(
    `/internal/gtm/initiatives/${initiativeId}/fanout-targets`,
    { agent_slug: step.actor },
  );
  // fanoutTargets = [{recipient_id, channel_campaign_step_id}, ...] for every
  // (recipient × DM step) materialized.

  const handles = await runPerRecipientCreative.batchTrigger(
    fanoutTargets.map((t) => ({
      payload: {
        initiativeId,
        recipientId: t.recipient_id,
        channelCampaignStepId: t.channel_campaign_step_id,
      },
    })),
  );

  // Wait for all child tasks to terminal (Trigger.dev's batch-await primitive).
  const results = await runs.waitForBatch(handles);

  // Pipeline ships forward whether individual fanouts succeeded or not —
  // partial failure is the expected mode. Per-recipient failures are
  // visible in the runs aggregate. Pipeline-level "failed" is reserved
  // for catastrophic batch failures.
  const failureRate = results.filter(r => r.output.status !== "shipped").length / results.length;
  if (failureRate > 0.50) {
    await hqxPost(`/internal/gtm/initiatives/${initiativeId}/pipeline-failed`, {
      failed_at_slug: step.actor,
      reason: `fanout_high_failure_rate:${(failureRate * 100).toFixed(0)}%`,
    });
    throw new Error(`per-recipient fanout failed for >50% of audience`);
  }

  continue;
}
```

50% is the v0 threshold; tunable later.

### 7.3 Concurrency controls

Trigger.dev's queue config: `gtm.run-per-recipient-creative` runs in a queue with concurrency limit `50`. Tunable via Trigger config; balances Anthropic rate limits vs wall-clock time. With 5000 recipients × 3 DM steps = 15,000 fanout invocations × ~10s each at concurrency 50 = ~1 hour wall-clock for a full audience.

Anthropic rate limits at the Managed Agents tier are TBD by the operator; document in the directive that the concurrency limit needs to be set with awareness of those limits and reduced if 429s show up in run rows.

### 7.4 New internal endpoint

`POST /internal/gtm/initiatives/{id}/fanout-targets` — body `{agent_slug}`, returns the list of `(recipient_id, channel_campaign_step_id)` tuples derived from the most recent succeeded audience-materializer run + the materialized DM step ids:

```python
async def list_fanout_targets(initiative_id: UUID, agent_slug: str) -> list[dict]:
    # Look up the most recent succeeded audience-materializer run for this initiative.
    # Read its output_blob.executed.dm_step_ids.
    # Cross-join with all initiative_recipient_memberships rows for the initiative
    # WHERE removed_at IS NULL.
    # Return [{recipient_id, channel_campaign_step_id} for r in recipients for s in dm_steps].
```

The cross-join generates exactly N (recipients) × M (DM steps) tuples — the full fanout target set.

---

## 8. End-to-end exercise

Extend `scripts/seed_dat_gtm_pipeline_foundation.py` (or create `scripts/seed_dat_gtm_pipeline_materializer.py`):

1. Resolve DAT initiative.
2. Reset its pipeline state. Verify all four new agents are registered in `gtm_agent_registry`.
3. Reset materializer-derived state: DELETE FROM `channel_campaign_step_recipients` WHERE step IN initiative's steps; DELETE FROM `initiative_recipient_memberships` WHERE initiative = DAT; soft-delete recipients tied only to DAT initiative (or skip — recipients are reusable identity rows).
4. POST `start-pipeline` with `gating_mode='auto'`.
5. Poll until terminal. Three acceptable outcomes:
   - `completed` (every step shipped)
   - `verdict_block_after_retries` at any step (smoke success — surfaces the iteration target)
   - `fanout_high_failure_rate:N%` (smoke success — surfaces per-recipient prompt iteration target)
6. Print run aggregate + the materialized state: campaign id, channel_campaign ids, step count, recipient count, manifest count, Dub link count, per-recipient run count.
7. Write summary to `docs/initiatives-archive/<id>/materializer_e2e_<ts>.md`.
8. Exit 0 on any of the three acceptable outcomes; non-zero on prerequisite-missing or wall-clock timeout (60 minutes — fanout is slow).

For dev iteration, support a `MATERIALIZER_AUDIENCE_LIMIT=25` env var that caps audience materialization to N members. Audience materializer reads it; production omits.

---

## 9. Tests / acceptance

### 9.1 Pytest

- `tests/test_materializer_execution.py` — `execute_channel_step_plan` produces the right rows in one transaction; `execute_audience_plan` is idempotent across re-runs; both honor doctrine constraints.
- `tests/test_gtm_pipeline_materializer_steps.py` — `run_step` for `gtm-channel-step-materializer` writes both the agent's plan AND the executed result into output_blob; per-recipient `run_step` honors `recipient_id` + `step_id` kwargs; supersede semantics work for fanout (re-running materializer marks all downstream per-recipient runs `superseded`).
- `tests/test_initiative_runs_aggregate.py` — fanout aggregation correctly pulls expected_count from the upstream materializer run's output_blob.
- `tests/test_internal_fanout_targets_endpoint.py` — endpoint returns the full cross-join of (recipient × DM step) tuples.
- Existing `tests/test_internal_gtm_pipeline.py` and `tests/test_gtm_pipeline_service.py` — extend to cover the new optional kwargs and new agents. Don't skip the existing tests.

E2E (gated by `RUN_E2E_GTM=1`):
- `tests/test_dat_gtm_pipeline_materializer_e2e.py` — runs the seed script with `MATERIALIZER_AUDIENCE_LIMIT=10` so it completes in dev within reasonable time. Asserts post-run state: campaigns row, channel_campaigns rows (3 channels), steps rows (per sequence-definer touch count), 10 recipients, 10×N memberships, 10 manifest rows, 10×K Dub links (K = DM step count), 10×K per-recipient runs each with terminal status.

### 9.2 Acceptance

- `uv run pytest -q` baseline passes (1068 + new tests).
- E2E test passes against dev with `MATERIALIZER_AUDIENCE_LIMIT=10`.
- Manual: hit `/api/v1/admin/initiatives/<DAT id>/runs/aggregated` and see the per-step breakdown including the fanout step's `expected_count` / `completed_count` / `failed_count`.
- Manual: confirm via SQL that one `business.campaigns` row exists for the DAT initiative with `initiative_id` set; three `business.channel_campaigns` rows; the right number of `channel_campaign_steps` rows; the manifest table is populated; Dub links table has the expected per-recipient × per-DM-step count.

---

## 10. Out of scope

Defer to follow-up directives:

- **hq-command frontend updates.** The aggregate endpoint is the contract; rendering 5000 fanout rows efficiently (collapsed rows by default, drill-down per-step, virtualized table) is a sibling hq-command directive. Backend ships the data; frontend catches up.
- **Render-and-submit pipeline** — taking the per-recipient creative DSL output and producing final HTML/PDF for `activate_pieces_batch`. This is the directive AFTER materializer ships.
- **Voice-agent instantiation** — separate parallel directive.
- **Email copy author (#9)** — separate directive.
- **Subagents #5 (audience-specific Exa) / #6 (output shaper) / #8 (brand context loader)** — the master strategist absorbs their work inline today; splitting them into separate MAGS agents is a future iteration directive.
- **Adversarial agent (#12a) and DSL validator (#12b)** — the per-recipient verdict reasons over the DSL JSON shape directly; full adversarial pass is a follow-up.
- **Sub-squad critic split** — verdicts still carry critic reasoning inline.
- **Sampling logic for over-budget audiences** — audience-materializer rejects with `reject_size_mismatch`; manual operator handling. Auto-sampling is a content decision, deferred.
- **Cost-cents population** — column still NULL for new agents; same as foundation.
- **Multi-org initiative support** — acq-eng-only assumption persists.
- **Removing `app/services/strategy_synthesizer.py`** — still dormant, still untouched.
- **`gtm-master-strategist` reading sample audience members from `business.recipients` instead of inline DEX query** — opportunistic improvement; if landing this requires touching the master strategist, defer it. The current inline-DEX-LIMIT-N path works.

---

## 11. Sequencing within the directive

1. Migration §4.1 + run locally.
2. Update `gtm_subagent_runs` repo + service code in `app/services/gtm_pipeline.py` to handle new columns; existing tests stay green.
3. `app/services/materializer_execution.py` + tests (DEX client + DB calls mocked).
4. Update `_assemble_input` and `run_step` to handle new agent slugs and per-recipient kwargs; add `output_blob` shape extension for materializers (plan + executed).
5. New routers: extend admin initiatives router with the aggregate endpoint; extend internal router with `fanout-targets`. Update internal `run-step` body schema. Tests.
6. Trigger.dev: new child task `gtm.run-per-recipient-creative`, extend parent task with fanout dispatch + batch wait. Configure concurrency on the child queue. Deploy.
7. managed-agents-x: author the four `system_prompt.md` files, extend `setup_gtm_agents.py`'s `AGENTS` map with the four new entries. Operator runs the setup script + `register_gtm_agent.py` for each.
8. Extend or write the materializer e2e seed script. Iterate until smoke passes with `MATERIALIZER_AUDIENCE_LIMIT=10`.
9. PR. Title: `feat(gtm): pipeline materializer — channel/step + audience + per-recipient fanout`.

PR description must include:
- The new pipeline order (5 steps, with the last as fanout)
- The aggregate-runs endpoint shape (so the frontend directive that follows knows what to consume)
- Confirmation that the existing foundation seed script's behavior is preserved or superseded cleanly
- A note on the `MATERIALIZER_AUDIENCE_LIMIT` dev-only knob

---

## 12. Notes on what this enables

After this ships:

1. The pipeline produces, for the DAT initiative: a real `business.campaigns` row, three `business.channel_campaigns`, N `business.channel_campaign_steps`, K `business.recipients`, K×M memberships, K manifest rows, K×L Dub links, and K×L per-recipient creative outputs (each with their verdict result).
2. Operator iteration: open a per-recipient run via the existing run-detail endpoint, see what `gtm-per-recipient-creative` produced for THAT specific recipient (their DOT#, their power_units, their state). Edit the creative agent's prompt via the existing prompt editor, hit Activate, click "Rerun this step" — new fanout fires automatically with the new prompt.
3. The substrate is in place to write the render-and-submit directive next (per-recipient DSL → final HTML/PDF → `activate_pieces_batch` against Lob test mode → end-to-end DM send loop runs against real Lob).
4. The aggregate endpoint unblocks the hq-command frontend update directive, which renders the fanout view efficiently.

The pipeline is now data-complete from `start-pipeline` through `gtm-per-recipient-creative` outputs. What's left to ship a real outreach is: render-and-submit (DM), email copy + EmailBison submit, voice-agent instantiation, landing-page personalization. Each is its own directive on top of this materializer slice.
