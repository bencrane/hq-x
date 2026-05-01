# Directive: GTM-initiative strategy pipeline (slice 1)

**Context:** You are working on `hq-x`. Read `CLAUDE.md` and `docs/strategic-direction-owned-brand-leadgen.md` before starting.

**Scope clarification on autonomy:** You are expected to make strong engineering decisions within the scope below. What you must not do is drift outside this scope, run deploy commands, modify any router/service not explicitly listed here, touch DMaaS / Lob / Dub / Vapi / voice / SMS / Entri / brand surfaces, or build any of the explicitly out-of-scope items (subagents 3–7, per-recipient creative, audience materialization, landing pages, voice agent config, UI, payments, self-serve, frontend). Within scope, use your best judgment.

**Background:** A demand-side partner has paid for a 90-day audience reservation. Capital Expansion (or another Ben-owned brand) is the brand under which outreach will run. The earlier Exa partner-research run produced a descriptive profile of the partner. We now need to convert that input bundle (brand + audience-spec + partner + partner-contract + partner-research) into a **campaign strategy artifact** that downstream materializers (channels, recipients, per-recipient creative) will consume.

This directive covers slice 1 only:

1. Stub the §8 data model: `demand_side_partners`, `partner_contracts`, `gtm_initiatives`. **Not** renaming `org_audience_reservations` (leave it alone).
2. Subagent 1 — **strategic-context researcher**: a second Exa research run, audience-scoped, operator-voice-sourced, fired through the existing `exa_research_jobs` pipeline.
3. Subagent 2 — **strategy synthesizer**: a single Anthropic API call that reads partner research + strategic-context research + audience descriptor + brand .md + partner contract, and emits `campaign_strategy.md`.
4. End-to-end seed against the DAT fixture.

Subagents 3–7 (channel/step materializer, audience materializer, per-recipient creative author, landing-page author, voice-agent configurator) are **separate directives**. Stop after the strategy doc lands.

**Critical existing-state facts (verify before building):**

- The Exa orchestration already exists. `POST /api/v1/exa/jobs` + the Trigger.dev task `exa.process_research_job` + `exa.exa_calls` raw archive (in both DBs) are live in main. Reuse this pipeline for subagent 1; do not build a parallel one.
- The DAT Exa partner-research run was kicked off earlier in /tmp; the fixture data lands at `/tmp/exa_dat_result.json`. For this directive, the seed script should re-fire it as a real `exa_research_jobs` row with `objective='partner_research'`, `objective_ref='partner:<uuid>'` so it lands in `exa.exa_calls` durably.
- `business.brands` row for Capital Expansion exists (id `1c570a63-eac3-436a-8b52-bf0a2e1818e4`, parented to `acq-eng` org). Brand content sits at `data/brands/capital-expansion/*.md`.
- `business.org_audience_reservations` already exists from the earlier reservations directive. Leave it alone — `gtm_initiatives.data_engine_audience_id` will carry the spec id directly. The two are independent for the prototype; reconciliation is a future directive.
- DAT audience spec id: query `dex.ops.audience_specs` for the spec named "DAT — fast-growing carriers (prototype)" (created by the reservations seed). The seed script for this directive should resolve it by name and use it.
- hq-x has no Anthropic SDK setup today. The synthesizer is the first LLM-from-hq-x call. You will add `ANTHROPIC_API_KEY` to `app/config.py` and a thin client.

---

## Existing code to read before starting

- [CLAUDE.md](../../CLAUDE.md) — migration filename convention, async-job patterns.
- [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md) — full GTM model, §5 phase semantics, §8 data-model objects, §9 open architectural calls.
- [app/services/exa_client.py](../../app/services/exa_client.py) — async Exa client. The strategic-context researcher fires through `exa_research_jobs` so it does NOT call this directly; it goes through the public job creator.
- [app/services/exa_research_jobs.py](../../app/services/exa_research_jobs.py) — orchestration job CRUD. Mirror its `_record_history` / `mark_*` style for the new initiative-level state machine.
- [app/routers/exa_jobs.py](../../app/routers/exa_jobs.py) — public async-202 router pattern. The new `/api/v1/initiatives` endpoints follow this shape exactly.
- [app/routers/internal/exa_jobs.py](../../app/routers/internal/exa_jobs.py) — internal callback the Trigger task POSTs into. Mirror for the synthesizer's internal callback.
- [src/trigger/exa-process-research-job.ts](../../src/trigger/exa-process-research-job.ts) — Trigger task pattern. Mirror for the synthesizer task.
- [migrations/20260430T184850_activation_jobs.sql](../../migrations/20260430T184850_activation_jobs.sql) — orchestration-job table shape (status enum, history, idempotency-key, attempts).
- [data/brands/capital-expansion/](../../data/brands/capital-expansion/) — brand content the synthesizer reads. Read all eight `.md` files yourself before writing the synthesizer prompt scaffold; the synthesizer's job is to be loyal to this voice.
- [app/services/dex_client.py](../../app/services/dex_client.py) (from the reservations directive, if present) — for audience-descriptor reads. If absent in main, build it minimally per the reservations directive's spec.

---

## Build 1: Data-model migration

**File:** `migrations/<UTC_TIMESTAMP>_gtm_initiatives.sql` (new)

Three new tables. All under `business.` schema.

```sql
CREATE TABLE IF NOT EXISTS business.demand_side_partners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    domain TEXT,
    primary_contact_name TEXT,
    primary_contact_email TEXT,
    primary_phone TEXT,
    intro_email TEXT,
    hours_of_operation_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX uq_dsp_org_name
    ON business.demand_side_partners (organization_id, name)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS business.partner_contracts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID NOT NULL REFERENCES business.demand_side_partners(id) ON DELETE RESTRICT,
    pricing_model TEXT NOT NULL CHECK (pricing_model IN ('flat_90d', 'per_lead', 'residual_pct', 'hybrid')),
    amount_cents BIGINT,
    duration_days INTEGER NOT NULL DEFAULT 90,
    max_capital_outlay_cents BIGINT,
    qualification_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    terms_blob TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'fulfilled', 'cancelled')),
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pc_partner_status ON business.partner_contracts (partner_id, status);

CREATE TABLE IF NOT EXISTS business.gtm_initiatives (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE RESTRICT,
    partner_id UUID NOT NULL REFERENCES business.demand_side_partners(id) ON DELETE RESTRICT,
    partner_contract_id UUID NOT NULL REFERENCES business.partner_contracts(id) ON DELETE RESTRICT,
    -- The dex `ops.audience_specs.id`. Not a FK (cross-DB). Locked at initiative creation.
    data_engine_audience_id UUID NOT NULL,
    -- Pointer to the partner-research exa_calls row. Same lightweight pointer
    -- format as exa_research_jobs.result_ref: '<destination>://exa.exa_calls/<uuid>'.
    partner_research_ref TEXT,
    -- Pointer to the strategic-context-research exa_calls row, populated by
    -- subagent 1 once the run completes.
    strategic_context_research_ref TEXT,
    -- Pointer to the campaign_strategy.md artifact path on disk. Populated by
    -- subagent 2.
    campaign_strategy_path TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft',
        'awaiting_strategic_research',
        'awaiting_strategy_synthesis',
        'strategy_ready',
        'materializing',
        'ready_to_launch',
        'active',
        'completed',
        'cancelled'
    )),
    history JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    reservation_window_start TIMESTAMPTZ,
    reservation_window_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_gtm_org_status ON business.gtm_initiatives (organization_id, status);
CREATE INDEX idx_gtm_partner ON business.gtm_initiatives (partner_id);
CREATE INDEX idx_gtm_brand ON business.gtm_initiatives (brand_id);
```

The status state machine is purely additive across slices — slice 1 only transitions `draft → awaiting_strategic_research → awaiting_strategy_synthesis → strategy_ready`. The downstream states exist in the enum so future directives don't migrate.

---

## Build 2: GTM initiatives service + router

### File 2a: Service

**File:** `app/services/gtm_initiatives.py` (new)

Mirrors `app/services/exa_research_jobs.py` style. Functions:

```python
async def create_initiative(
    *,
    organization_id: UUID,
    brand_id: UUID,
    partner_id: UUID,
    partner_contract_id: UUID,
    data_engine_audience_id: UUID,
    partner_research_ref: str | None = None,
    reservation_window_start: datetime | None = None,
    reservation_window_end: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]: ...

async def get_initiative(initiative_id: UUID, *, organization_id: UUID) -> dict[str, Any] | None: ...

async def transition_status(
    initiative_id: UUID, *, new_status: str, history_event: dict[str, Any]
) -> None: ...

async def set_strategic_context_research_ref(initiative_id: UUID, ref: str) -> None: ...
async def set_campaign_strategy_path(initiative_id: UUID, path: str) -> None: ...
```

Validate the status state machine in `transition_status` — refuse illegal transitions (e.g., `strategy_ready → awaiting_strategic_research`). Append every state change to `history`.

### File 2b: Public router

**File:** `app/routers/gtm_initiatives.py` (new), prefix `/api/v1/initiatives`

Auth: `verify_supabase_jwt`. Endpoints:

- `POST /api/v1/initiatives` — body: `{brand_id, partner_id, partner_contract_id, data_engine_audience_id, partner_research_ref?, reservation_window_start?, reservation_window_end?}`. Returns 201 with the row.
- `GET /api/v1/initiatives/{initiative_id}` — returns row or 404.
- `POST /api/v1/initiatives/{initiative_id}/run-strategic-research` — fires subagent 1. Returns 202 with `{exa_job_id, status: "queued"}`. Updates initiative status to `awaiting_strategic_research`.
- `POST /api/v1/initiatives/{initiative_id}/synthesize-strategy` — fires subagent 2. Returns 202 with `{job_id, status: "queued"}`. Updates initiative status to `awaiting_strategy_synthesis`. Refuses with 409 if `strategic_context_research_ref` is null (i.e., subagent 1 hasn't completed).

All endpoints scope to `user.active_organization_id`. Cross-org reads return 404, not 403.

Wire into [app/main.py](../../app/main.py) — 2 lines (import + include_router), nothing else.

---

## Build 3: Subagent 1 — strategic-context researcher

**File:** `app/services/strategic_context_researcher.py` (new)

This is a thin wrapper around the existing `exa_research_jobs` pipeline. It does **not** call Exa directly; it creates an `exa_research_jobs` row with the right shape and lets the existing `exa.process_research_job` Trigger task fire it.

```python
async def run_strategic_context_research(
    *,
    initiative_id: UUID,
    organization_id: UUID,
    created_by_user_id: UUID | None,
) -> dict[str, Any]:
    """
    Build the audience-scoped, operator-voice-sourced research instructions,
    create an exa_research_jobs row keyed by initiative, and return the job row.
    The existing Trigger task picks it up and persists the result to
    exa.exa_calls (destination='hqx'). The completion callback (Build 4)
    detects the result and updates the initiative.
    """
```

### Inputs the function reads

1. `gtm_initiatives` row → resolves brand_id, partner_id, partner_contract_id, data_engine_audience_id, partner_research_ref.
2. **Brand content** at `data/brands/<brand_slug>/*.md` (positioning + voice + audience-pain + capital-types + creative-directives). Resolve `<brand_slug>` from `business.brands.name` lower-kebab-cased OR via a `slug` column if you add one (don't add one in this directive — derive from name).
3. **Audience descriptor** via `dex_client.get_audience_descriptor(spec_id)` — gives the resolved_filters + attribute_schema + audience_attributes shape.
4. **Partner contract** via `partner_contracts` row → `qualification_rules` + commercial structure.
5. **Partner research** content via the `partner_research_ref` pointer → SELECT from local `exa.exa_calls.response_payload` (when ref starts with `hqx://`) or via dex client (when `dex://`).

### Output

The function constructs an Exa research instruction string that is meaningfully different from the partner-research instruction:

- **Audience-scoped**: the instruction explicitly names the audience demographic (resolved from the spec's attribute_schema + resolved_filters).
- **Operator-voice-sourced**: the instruction directs Exa toward sources where the audience itself speaks — Reddit subforums, trade press, G2 / Trustpilot reviews of the partner and its alternatives, recent rate-environment / regulatory / macro articles relevant to the audience's business — and AWAY from the partner's own marketing pages (those were already covered by the partner-research run).
- **Time-relevant**: the instruction asks for the *current* window (last 6–12 months) of operator-side discourse about pain points the partner addresses.
- **Outputs concrete language**: the instruction asks for verbatim phrases operators use, not summaries.

**Do NOT write the verbatim instruction string in this directive.** The first version of the instruction text lives in the code as a constant string (`_RESEARCH_INSTRUCTION_TEMPLATE`) with template slots for the audience descriptor / brand positioning / partner research summary / time window. Ben will iterate on the prompt directly in code; structure the function so the prompt template is the one obvious place to edit.

The function then calls `exa_research_jobs.create_job(...)` with:
- `endpoint='research'`
- `destination='hqx'`
- `objective='strategic_context_research'`
- `objective_ref=f'initiative:{initiative_id}'`
- `request_payload={'instructions': <rendered template>}`
- `idempotency_key=f'strategic-context-{initiative_id}'`

Then enqueues the Trigger task (same client the existing public exa router uses). Returns the job row.

The function MUST also call `gtm_initiatives.transition_status(initiative_id, new_status='awaiting_strategic_research', ...)`.

---

## Build 4: Subagent 1 completion callback

The existing `exa.process_research_job` Trigger task POSTs into `/internal/exa/jobs/{id}/process` and stamps the result_ref on the `exa_research_jobs` row. We need a parallel signal that updates the *initiative* once the strategic-context-research job for it completes.

Two options; pick one:

**(A) Polling.** A tiny Trigger task scheduled to run every 60 seconds: scan for initiatives in `awaiting_strategic_research` whose corresponding exa_research_job is in `succeeded`, copy the result_ref onto the initiative, transition status to a new intermediate state (`strategic_research_ready`).

**(B) Inline.** Modify the existing `process_exa_job` internal endpoint to additionally check, after marking the exa job succeeded, whether the `objective_ref` matches `initiative:<uuid>` and if so, write back to the initiative.

Pick **(B)**. Cleaner, no polling, less code. The modification is additive — extract a small `_post_process_by_objective(job)` function in `app/routers/internal/exa_jobs.py` that dispatches by `objective`. For `objective='strategic_context_research'` it calls `gtm_initiatives.set_strategic_context_research_ref(initiative_id, ref)` and transitions to a new intermediate state. **Add this state to the gtm_initiatives status enum in the migration**: `strategic_research_ready` between `awaiting_strategic_research` and `awaiting_strategy_synthesis`.

Update the migration enum check accordingly.

---

## Build 5: Subagent 2 — strategy synthesizer

This is the first LLM-from-hq-x call. Build the foundation thinly.

### File 5a: Anthropic client

**File:** `app/services/anthropic_client.py` (new)

Thin wrapper around `anthropic` SDK. Use prompt caching on the system prompt (per the global Claude API skill). Methods:

```python
async def complete(
    *,
    system: str,
    messages: list[dict[str, Any]],
    model: str = "claude-opus-4-7",
    max_tokens: int = 8192,
    temperature: float = 0.4,
) -> dict[str, Any]:
    """Returns {text, usage, model, stop_reason}. Raises AnthropicCallError on non-2xx."""
```

Add to `app/config.py`:

```python
ANTHROPIC_API_KEY: SecretStr | None = None
ANTHROPIC_DEFAULT_MODEL: str = "claude-opus-4-7"
```

`pyproject.toml` / `requirements.txt` — add `anthropic` if not present.

### File 5b: Synthesizer service

**File:** `app/services/strategy_synthesizer.py` (new)

```python
async def synthesize_initiative_strategy(
    *,
    initiative_id: UUID,
    organization_id: UUID,
) -> dict[str, Any]:
    """
    Reads all six inputs, calls Anthropic once, writes
    data/initiatives/<initiative_id>/campaign_strategy.md to disk,
    sets gtm_initiatives.campaign_strategy_path, transitions status to
    'strategy_ready'. Returns {path, model, tokens_used}.
    """
```

#### Inputs the function reads

1. Initiative row.
2. Partner research — fetched from `exa.exa_calls.response_payload` via `partner_research_ref`.
3. Strategic-context research — fetched from `exa.exa_calls.response_payload` via `strategic_context_research_ref`.
4. Audience descriptor — via `dex_client.get_audience_descriptor`.
5. Brand .md files at `data/brands/<brand_slug>/*.md` (all eight).
6. Partner contract — `partner_contracts` row + parent `demand_side_partners` row.

#### System prompt

Define as a versioned constant `_SYSTEM_PROMPT_V1` in the file. The prompt's job is:

- Establish the role (campaign strategist for an owned-brand demand-gen platform).
- Establish the brand voice constraint (loyal to the brand .md files).
- Establish the output contract (YAML front-matter + markdown body, with a fixed schema for the YAML block).
- Establish anti-fabrication discipline (no proof claims that aren't in the input research).

**Do not write the verbatim system prompt in this directive.** Ben will iterate on it. Structure the file so `_SYSTEM_PROMPT_V1` is the one obvious place to edit; future versions become `_SYSTEM_PROMPT_V2` etc. with a constant pointer at the bottom of the file naming the active version.

#### User message

A single user message that bundles the six inputs as labeled sections (`<partner_research>...</partner_research>`, `<audience>...</audience>`, etc.). Use prompt-cache breakpoints on the system prompt + the static brand-content section so iteration on the dynamic inputs is cheap.

#### Output contract

The model emits markdown of this shape:

```markdown
---
schema_version: 1
initiative_id: <uuid>
generated_at: <iso>
model: <model id>

headline_offer: <one sentence>
core_thesis: <one paragraph>
narrative_beats:
  - <beat 1>
  - <beat 2>
  - <beat 3>
channel_mix:
  direct_mail:
    enabled: true
    touches:
      - touch_number: 1
        kind: postcard
        day_offset: 0
      - touch_number: 2
        kind: letter
        day_offset: 14
      - touch_number: 3
        kind: postcard
        day_offset: 28
  email:
    enabled: true
    touches:
      - touch_number: 1
        day_offset: 3
      - touch_number: 2
        day_offset: 17
      - touch_number: 3
        day_offset: 35
  voice_inbound:
    enabled: true
capital_outlay_plan:
  total_estimated_cents: <int>
  per_recipient_estimated_cents: <int>
personalization_variables:
  - <name + how_to_pull (which spec attribute / which member field)>
anti_framings:
  - <thing the copywriter must NOT say>
---

# <human-readable strategy doc body>

## Why this audience, why this partner, why now
...

## The five narrative beats expanded
...

## Per-touch creative direction
...

## What we explicitly avoid
...
```

The synthesizer parses the YAML front-matter on its way out to confirm structural validity before writing to disk. If the model returns malformed YAML, retry once with a "your previous response had invalid YAML — produce a strict YAML front-matter" follow-up. If the second attempt also fails, mark the initiative `failed` and persist the raw output to a `<id>/failed_synthesis.md` for inspection.

### File 5c: Trigger.dev task

**File:** `src/trigger/gtm-synthesize-initiative-strategy.ts` (new)

Mirror [src/trigger/exa-process-research-job.ts](../../src/trigger/exa-process-research-job.ts). Task id `gtm.synthesize_initiative_strategy`. POSTs into `/internal/initiatives/{id}/process-synthesis`.

### File 5d: Internal callback

**File:** `app/routers/internal/gtm_initiatives.py` (new), prefix `/internal/initiatives`

Auth: `verify_trigger_secret`. One endpoint:

- `POST /internal/initiatives/{initiative_id}/process-synthesis` — calls `strategy_synthesizer.synthesize_initiative_strategy(...)`. Idempotent on terminal states.

Wire into [app/main.py](../../app/main.py).

---

## Build 6: Seed/exercise script

**File:** `scripts/seed_dat_gtm_initiative.py` (new)

End-to-end, idempotent. Steps:

1. Resolve org `acq-eng` → `organization_id`.
2. Resolve brand `Capital Expansion` → `brand_id`.
3. Upsert `demand_side_partners` row for DAT (name=DAT, domain=dat.com, primary fields stubbed).
4. Upsert `partner_contracts` row for DAT (pricing_model=`flat_90d`, amount_cents=2_500_000, duration_days=90, qualification_rules a stub like `{"power_units_min": 10, "power_units_max": 50}`).
5. Resolve the DAT audience spec id from dex (lookup by name "DAT — fast-growing carriers (prototype)").
6. Re-fire the partner-research Exa job durably — POST to `/api/v1/exa/jobs` with `endpoint='research'`, `destination='hqx'`, `objective='partner_research'`, `objective_ref=f'partner:{partner_id}'`, `request_payload={'instructions': <load from /tmp/exa_run_dat_instructions.txt>}`, idempotency_key. Wait for completion. Capture `result_ref`.
7. Create the `gtm_initiatives` row tying everything: brand + partner + partner_contract + audience_id + partner_research_ref.
8. POST `/api/v1/initiatives/{id}/run-strategic-research`. Poll until status `strategic_research_ready` (or timeout 15 min).
9. POST `/api/v1/initiatives/{id}/synthesize-strategy`. Poll until status `strategy_ready` (or timeout 5 min).
10. Print `data/initiatives/<initiative_id>/campaign_strategy.md` path + first 50 lines of the file.

Run via `doppler --project hq-x --config dev run -- uv run python -m scripts.seed_dat_gtm_initiative`.

---

## Build 7: Tests

Proportionate to a prototype. New test files:

- `tests/test_gtm_initiatives_service.py` — happy-path create / get / status transitions / illegal-transition refusal / cross-org 404.
- `tests/test_gtm_initiatives_router.py` — endpoint auth, async-202 contracts, the `synthesize-strategy` 409-when-no-research case.
- `tests/test_strategic_context_researcher.py` — mock the Exa job creator; assert the rendered instruction includes the audience descriptor + brand positioning markers + the time-window scope; assert idempotency_key shape; assert initiative status transitions.
- `tests/test_strategy_synthesizer.py` — mock the Anthropic client to return a known-good YAML+markdown blob; assert disk write at `data/initiatives/<id>/campaign_strategy.md`; assert YAML front-matter parses; assert initiative `campaign_strategy_path` populated; assert status `strategy_ready`. Add one test where the model returns invalid YAML and the retry logic kicks in. Add one test where both attempts fail and status flips to `failed`.

Do **not** write tests that hit real Exa or real Anthropic. Mock both at the client layer.

---

## What NOT to do

- Do **not** build subagents 3–7 (channel materializer, audience materializer, per-recipient creative, landing pages, voice agent). Stop at strategy doc.
- Do **not** rename `business.org_audience_reservations`. Leave it.
- Do **not** delete or modify any existing migration, router, or service except as listed (the one additive edit to `app/routers/internal/exa_jobs.py` for the post-process-by-objective dispatcher, the two `app/main.py` line additions for the new routers, the one `app/config.py` line addition for `ANTHROPIC_API_KEY`).
- Do **not** touch DMaaS / Lob / Dub / Vapi / voice / SMS / Entri / brand surfaces.
- Do **not** build a `hq-x-mcp`. Use the existing HTTP API + super-admin or operator JWT.
- Do **not** mint a new auth pattern. Existing `verify_supabase_jwt` for public, `verify_trigger_secret` for internal, super-admin bearer for cross-service.
- Do **not** persist the partner-research or strategic-context-research as standalone .md files. They live in `exa.exa_calls.response_payload` (jsonb). The synthesizer reads them from there. Only `campaign_strategy.md` lands on disk.
- Do **not** write the verbatim Exa research instruction or Anthropic system prompt. The directive specifies their *role and structure*. Ben iterates the actual text in code.
- Do **not** add a managed-agent-as-a-service abstraction in `managed-agents-x`. The synthesizer is a single Anthropic call inside hq-x for now. Managed-agent-ification is a future evolution.
- Do **not** add a polling cron for subagent-1 completion. Use the inline post-process-by-objective dispatcher (Option B in Build 4).

---

## Scope

Files to create or modify:

**New:**
- `migrations/<UTC_TIMESTAMP>_gtm_initiatives.sql`
- `app/services/gtm_initiatives.py`
- `app/services/strategic_context_researcher.py`
- `app/services/strategy_synthesizer.py`
- `app/services/anthropic_client.py`
- `app/routers/gtm_initiatives.py`
- `app/routers/internal/gtm_initiatives.py`
- `src/trigger/gtm-synthesize-initiative-strategy.ts`
- `scripts/seed_dat_gtm_initiative.py`
- `tests/test_gtm_initiatives_service.py`
- `tests/test_gtm_initiatives_router.py`
- `tests/test_strategic_context_researcher.py`
- `tests/test_strategy_synthesizer.py`
- `data/initiatives/.gitkeep` (so the directory exists in the repo)

**Modify (additive only):**
- `app/config.py` — add `ANTHROPIC_API_KEY`, `ANTHROPIC_DEFAULT_MODEL`
- `app/main.py` — 4 new lines (2 imports + 2 include_router)
- `app/routers/internal/exa_jobs.py` — extract `_post_process_by_objective` dispatcher; add `strategic_context_research` handler that calls `gtm_initiatives.set_strategic_context_research_ref` + transitions status
- `pyproject.toml` / `requirements.txt` — add `anthropic` if absent
- `CLAUDE.md` — short "GTM-initiative pipeline" section with the seed-script command

**One commit. Do not push.**

Commit message:

> feat(gtm): initiative pipeline slice 1 — strategic-context research + strategy synthesis
>
> Add `business.gtm_initiatives` + `demand_side_partners` + `partner_contracts`
> and the two-subagent pipeline that converts (brand + audience-spec + partner +
> partner-contract + partner-research) into a `campaign_strategy.md` artifact
> ready for downstream materialization. Subagent 1 reuses the existing exa
> orchestration with a new `objective='strategic_context_research'` path.
> Subagent 2 is the first hq-x → Anthropic call; thin client + prompt-cached
> single-shot synthesis, output validated by YAML-front-matter shape.

---

## When done

Report back with:

(a) Output of running `seed_dat_gtm_initiative` against dev: initiative_id, both result_refs, total runtime, path to `campaign_strategy.md`, first 50 lines of the file.
(b) Confirm the YAML front-matter of the produced strategy doc parses cleanly and contains all required keys (headline_offer, core_thesis, narrative_beats, channel_mix, capital_outlay_plan, personalization_variables, anti_framings).
(c) `uv run pytest tests/test_gtm_initiatives_service.py tests/test_gtm_initiatives_router.py tests/test_strategic_context_researcher.py tests/test_strategy_synthesizer.py` — pass count.
(d) The commit SHA. Do not push.
(e) The Anthropic token usage and total cost for the synthesis call against the DAT fixture (from the model response's usage block).
