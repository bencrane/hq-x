# Handoff: GTM-pipeline foundation — 2026-05-01

This doc describes the state of the post-payment GTM pipeline AFTER the
foundation slice landed across three repos on 2026-05-01. Read
[strategic-direction-owned-brand-leadgen.md](strategic-direction-owned-brand-leadgen.md)
first for the business model, then [HANDOFF_GTM_PIPELINE_2026-05-01.md](HANDOFF_GTM_PIPELINE_2026-05-01.md)
for the prior state, then this doc for what's new.

**Companion directive:** the spec this build implemented lives in the user's
directive (titled "GTM-initiative pipeline foundation") — search the conversation
archive if needed. This handoff doc is the operational record of what shipped.

---

## TL;DR

The post-payment pipeline now runs on **Anthropic's Managed Agents API (MAGS)**
instead of the Messages API direct path. Each subagent in the diagram is a
separately-registered MAGS agent. Trigger.dev sequences the actor + verdict
loop; hq-x is the only seam between Trigger and Anthropic.

**Three actor + verdict pairs ship in this slice (six MAGS agents):**

1. `gtm-sequence-definer` (+ verdict) — economics-aware channel + touch plan
2. `gtm-master-strategist` (+ verdict) — per-touch frames (NOT literal copy)
3. `gtm-per-recipient-creative` (+ verdict) — per-piece copy + design DSL JSON

**Operator now has:**
- A frontend command center at `/admin/initiatives`, `/admin/agents`, `/admin/doctrine`
- Per-step input / output / prompt-snapshot / mcp trace visibility
- One-click prompt edit + activate (with snapshot-then-overwrite invariant)
- One-click rollback to any prior version
- One-click rerun-from-here per step

**The existing V1 `app/services/strategy_synthesizer.py` is untouched and dormant.**
Removal is deferred to a separate directive after the new pipeline is validated.

---

## 1. What landed (the three merged PRs)

| PR | Repo | Squash commit |
|---|---|---|
| [#80](https://github.com/bencrane/hq-x/pull/80) — feat(gtm): pipeline foundation | hq-x | `6fb1277` |
| [#9](https://github.com/bencrane/managed-agents-x/pull/9) — 6 MAGS agents | managed-agents-x | `51705c6` |
| [#14](https://github.com/bencrane/hq-command/pull/14) — admin command center | hq-command | `81574d8` |

All three merged to main on 2026-05-01.

---

## 2. Architecture commitments locked in this build

These are decisions encoded in the code; deviating requires rework.

| Decision | What it means |
|---|---|
| **MAGS API as runtime** | Every LLM call in the post-payment pipeline goes through `/v1/agents` with the `managed-agents-2026-04-01` beta header. Reverses the prior directive's "Messages API direct" lock for this scope (the V1 synthesizer still uses Messages API but is dormant). |
| **Trigger.dev as orchestrator** | Single workflow `gtm.run-initiative-pipeline` sequences subagents. Trigger holds zero business state — every state mutation lands in hq-x's DB before each step's call returns. |
| **Sub-squad pattern: actor + verdict** | Every actor in this slice ships paired with a verdict that decides ship/redo. Critic split deferred (verdict carries critic-style reasoning inline). |
| **Prompts: Anthropic-as-live, DB-as-history** | Activate snapshots the current Anthropic state into `business.agent_prompt_versions` BEFORE pushing the new prompt. Two version rows per activate: snapshot + frontend_activate. |
| **Disk-mirrored prompts** | Each agent's prompt also lives at `managed-agents-x/data/agents/<slug>/system_prompt.md`. The `.md` and the active Anthropic prompt can drift between activations; DB versions are the canonical history. |
| **Default model: claude-opus-4-7** | Cost is acceptable; this is the productized service. Per-agent override via `business.gtm_agent_registry.model`. |
| **Run capture is the spine** | Every actor + verdict invocation writes a `business.gtm_subagent_runs` row capturing input, output, prompt snapshot, mcp_calls trace, anthropic_session_id, cost. The frontend reads from this table. |
| **Frontend → MAGS via hq-x proxy** | No MAGS keys in the browser. Browser hits hq-x with a Supabase JWT; hq-x calls Anthropic with its own M2M creds. |

---

## 3. Schema (5 new migrations)

Lexically ordered after `20260501T010000_brand_content.sql`.

### 3.1 `business.gtm_agent_registry` — `20260501T023700_*`
Slug → anthropic_agent_id mapping plus role / parent_actor_slug / model.
Soft-delete via `deactivated_at`.

### 3.2 `business.agent_prompt_versions` — `20260501T023710_*`
Versioned prompt history. `activation_source` enum:
`setup_script | frontend_activate | rollback | snapshot`.
`UNIQUE (agent_slug, version_index)` enforces monotonic indexing.

### 3.3 `business.gtm_subagent_runs` — `20260501T023720_*`
Per-invocation run capture. Status enum:
`queued | running | succeeded | failed | superseded`.
Replay = new row with `run_index = max + 1`; downstream rows bulk-marked
`superseded` first. Captures input_blob, output_blob, output_artifact_path
(for large markdown), system_prompt_snapshot, prompt_version_id,
anthropic_agent_id, anthropic_session_id, anthropic_request_ids, mcp_calls,
cost_cents, model, started_at, completed_at, error_blob.

### 3.4 `business.org_doctrine` — `20260501T023730_*`
Per-org operator policy. `doctrine_markdown` (prose) + `parameters` JSONB
(structured numeric overrides). PRIMARY KEY (organization_id) — one row
per org; only acq-eng populated for v0.

### 3.5 `business.gtm_initiatives` extension — `20260501T023740_*`
ALTER TABLE adds three columns:
- `gating_mode TEXT NOT NULL DEFAULT 'auto'` — `auto | manual`
- `pipeline_status TEXT` — `idle | running | gated | completed | failed`
- `last_pipeline_run_started_at TIMESTAMPTZ`

Distinct from the existing `status` column (high-level lifecycle).
Pipeline status is orthogonal to status.

---

## 4. Code paths to read (in priority order)

### hq-x

1. [app/services/gtm_pipeline.py](../app/services/gtm_pipeline.py) — `run_step` is the central seam. The whole pipeline funnels through this single function. Every input assembler, output parser, supersede-then-insert lifecycle, and Anthropic round-trip lives here.
2. [app/services/anthropic_managed_agents.py](../app/services/anthropic_managed_agents.py) — MAGS HTTP client. `run_session` opens a session, posts the user message, polls events until terminal, parses the assistant text + mcp_calls trace.
3. [app/services/agent_prompts.py](../app/services/agent_prompts.py) — snapshot-then-overwrite activate / rollback. Two version rows per activate.
4. [app/services/org_doctrine.py](../app/services/org_doctrine.py) — doctrine CRUD + parameter validation (type-coerce known keys, pass unknown keys through).
5. [app/routers/internal/gtm_pipeline.py](../app/routers/internal/gtm_pipeline.py) — single `/run-step` endpoint Trigger.dev calls. Bearer-auth via `TRIGGER_SHARED_SECRET`.
6. [app/routers/admin/agents.py](../app/routers/admin/agents.py) — admin surface for agent registry + prompt versioning.
7. [app/routers/admin/initiatives.py](../app/routers/admin/initiatives.py) — admin surface for initiatives + runs + start-pipeline + rerun.
8. [app/routers/admin/doctrine.py](../app/routers/admin/doctrine.py) — admin surface for doctrine CRUD.
9. [src/trigger/gtm-run-initiative-pipeline.ts](../src/trigger/gtm-run-initiative-pipeline.ts) — the Trigger.dev workflow. Loops STEPS, calls `/run-step` per agent, decides ship/redo from verdict output, calls pipeline-completed/failed.
10. [scripts/seed_dat_gtm_pipeline_foundation.py](../scripts/seed_dat_gtm_pipeline_foundation.py) — end-to-end exercise. Mirrors the Trigger workflow inline so the operator can debug locally.
11. [scripts/register_gtm_agent.py](../scripts/register_gtm_agent.py) — companion to managed-agents-x's setup script. Upserts a registry row + seeds a v1 prompt version.

### managed-agents-x

1. [data/agents/gtm-sequence-definer/system_prompt.md](../../managed-agents-x/data/agents/gtm-sequence-definer/system_prompt.md) — read this first; it's the simplest output contract (JSON).
2. [data/agents/gtm-master-strategist/system_prompt.md](../../managed-agents-x/data/agents/gtm-master-strategist/system_prompt.md) — note the **frame vs literal-copy distinction** — this is the load-bearing rule that V1's synthesizer got wrong.
3. [data/agents/gtm-per-recipient-creative/system_prompt.md](../../managed-agents-x/data/agents/gtm-per-recipient-creative/system_prompt.md) — the brand's wedge surface; per-recipient bespoke.
4. The three `*-verdict/system_prompt.md` files — verdict outputs are always strict JSON `{ship, issues, redo_with}`.
5. [scripts/setup_gtm_agents.py](../../managed-agents-x/scripts/setup_gtm_agents.py) — registers all six agents against the Anthropic API.

### hq-command (frontend)

1. [lib/gtm.ts](../../hq-command/lib/gtm.ts) — typed client fetchers + types. The shape contract between the frontend and hq-x's admin surface.
2. [app/admin/initiatives/[id]/page.tsx](../../hq-command/app/admin/initiatives/[id]/page.tsx) — the per-initiative drilldown with timeline, expandable run cards, Start / Rerun / Advance buttons. Polls every 3s while pipeline_status='running'.
3. [app/admin/agents/[slug]/page.tsx](../../hq-command/app/admin/agents/[slug]/page.tsx) — the prompt editor with version history table. Activate writes two rows; rollback writes two rows.
4. [app/admin/doctrine/page.tsx](../../hq-command/app/admin/doctrine/page.tsx) — single-page doctrine editor (acq-eng hardcoded for v0).
5. [app/api/admin/](../../hq-command/app/api/admin/) — 13 proxy routes that forward to hq-x's `/api/v1/admin/*` surface via `proxyToHqx`.

### Doctrine docs

1. [data/brands/_meta/independent-brand-doctrine.md](../data/brands/_meta/independent-brand-doctrine.md) — the illusion contract. The brand acts as the operator, not the matchmaker. Channel-tier framing rules per surface (postcard / letter / landing / voice / email). Anti-rules.
2. [data/orgs/acq-eng/doctrine.md](../data/orgs/acq-eng/doctrine.md) + [parameters.json](../data/orgs/acq-eng/parameters.json) — operator margin floor (40%), capital outlay cap (50%), per-piece cost band ($1.00 — $8.00), default touch counts by audience size, model tier per step, gating mode default.

---

## 5. Operator next steps — the unblock sequence

To go from "PRs merged" to "smoke-gate passing," in order:

### 5.1 Apply migrations (hq-x)
```
doppler --project hq-x --config dev run -- uv run python -m scripts.migrate
```
Should pick up the 5 new migrations and apply them in lexical order.

### 5.2 Sync acq-eng operator doctrine
```
doppler --project hq-x --config dev run -- uv run python -m scripts.sync_org_doctrine acq-eng
```
Reads `data/orgs/acq-eng/doctrine.md` + `parameters.json` and upserts
into `business.org_doctrine`.

### 5.3 Set the new Doppler secret
The directive already noted dev was populated, but verify:
```
doppler --project hq-x --config dev secrets get ANTHROPIC_MANAGED_AGENTS_API_KEY
```
This is **distinct** from `ANTHROPIC_API_KEY` (which the dormant V1 synthesizer
uses) so the two paths can be rotated / billed independently.

### 5.4 Register the 6 MAGS agents

From `managed-agents-x`:
```
./scripts/doppler run -- python -m scripts.setup_gtm_agents --all
```
Each registration prints the new `agent_id` plus a copy-paste command. For each:
```
doppler --project hq-x --config dev run -- \
  uv run python -m scripts.register_gtm_agent <slug> <agent_id> <role> [--parent <slug>] --model claude-opus-4-7
```
Verify `business.gtm_agent_registry` has 6 rows (3 actors + 3 verdicts) and
`business.agent_prompt_versions` has 6 rows (one `setup_script` per agent).

### 5.5 Deploy the Trigger.dev workflow
```
npm run trigger:deploy
```
Or whatever the existing deploy step is. The new task is auto-discovered
from `src/trigger/` per `trigger.config.ts`'s `dirs` config.

### 5.6 Run the foundation E2E smoke gate
```
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_dat_gtm_pipeline_foundation
```

This bypasses Trigger.dev for ergonomics — drives the pipeline inline against
the live Anthropic + dev DB. Two acceptable outcomes:

1. **`completed`** — every actor produced output, every verdict shipped. The
   ideal first-run outcome (unlikely with raw v0 prompts).
2. **`verdict_block_after_retries`** — at least one actor's draft was rejected
   by its verdict. **This is also a successful smoke run** — the platform works,
   the loop runs, real outputs landed in the DB, and the operator now has a
   concrete failure to iterate against.

The script exits 0 on either outcome. It exits non-zero only on prerequisite-
missing, network errors, or wall-clock timeout (15 min).

The script always writes a markdown summary to
`docs/initiatives-archive/<initiative_id>/foundation_e2e_<timestamp>.md`
with every run's slug / status / cost / parsed output.

### 5.7 Iterate from the frontend
1. Open `/admin/initiatives/bbd9d9c3-c48e-4373-91f4-721775dca54e` (the DAT initiative).
2. Identify which verdict blocked (almost certainly `gtm-per-recipient-creative-verdict` first, per V1 history).
3. Open `/admin/agents/<offending-actor-slug>`, edit the prompt, hit Activate.
4. On the initiative drilldown, click "Rerun from here" on the offending step.
5. New run uses the new prompt automatically.
6. Repeat until that step ships.
7. Once the down-stream step is clean, prior steps' weaknesses become observable; iterate them next.

---

## 6. The pipeline shape (what each subagent does)

### Subagent 1 — gtm-sequence-definer

**Inputs:** partner_contract + audience_descriptor + acq-eng doctrine + per_piece_cost_table.

**Output (JSON):**
```json
{
  "decision": "ship" | "ship_with_override_required" | "reject_economics",
  "channels": {
    "direct_mail": {"enabled": true, "touches": [...]},
    "email": {"enabled": true, "touches": [...]},
    "voice_inbound": {"enabled": true}
  },
  "total_estimated_outlay_cents": <int>,
  "per_recipient_outlay_cents": <int>,
  "projected_margin_pct": <float>,
  "audience_size_assumption": <int>,
  "justification": "<one paragraph>"
}
```

**Doctrine gates** (from acq-eng parameters):
- < 30% margin → `reject_economics`
- 30%–40% → `ship_with_override_required`
- ≥ 40% → `ship`
- per-piece cost MUST fall in `[min_per_piece_cents, max_per_piece_cents]`
- total outlay MUST be ≤ `min(amount_cents × max_capital_outlay_pct_of_revenue, contract.max_capital_outlay_cents)`

### Subagent 7 — gtm-master-strategist

**Inputs:** sequence (from #1) + audience_descriptor + 5–10 sample audience members + partner_research (Exa) + brand_content (the bundle of `.md` files) + independent_brand_doctrine.

**Output (markdown with YAML front-matter):**
- `audience_frame.who_they_are`
- `pain_and_trigger_map[]` — each entry: `pain`, `why_it_hurts`, `observable_trigger`, `assets_referenced`
- `voice_rules.must_say` / `must_not_say` / `doctrine_anti_framings`
- `per_touch_frames[]` — one per direct_mail + email touch, each with `frame` (CONCEPTUAL angle, ≤30 words), `assets_referenced`, `surface_tier_constraints`
- `anti_framings[]`

**Load-bearing rule:** `frame` is a theme, not a draft sentence. The per-recipient creative author renders frames into actual copy using the recipient's specific data. **This was V1's wrong shape** — V1 emitted literal-copy directives (`headline_focus: "Name the 30-60 day broker payment gap..."`); V3 frames are conceptual (`frame: "name the cash-flow situation in operator terms"`).

The output is persisted to disk at `data/initiatives/<initiative_id>/master_strategy.<run_index>.md` for downstream consumption.

### Subagent 11 — gtm-per-recipient-creative

**Inputs:** master_strategy (from #7) + sequence (from #1) + ONE recipient's full DEX row + brand_content + independent_brand_doctrine + spec_zone_catalog.

**Output (JSON array):** one entry per direct_mail touch, each with:
- `touch_number`, `mailer_type`
- `copy` (per-mailer-type shape: postcard front+back, letter body, etc.)
- `design_dsl` (face_constraints + zones_referenced)
- `assets_used_from_recipient` (which DEX attributes were pulled)
- `frame_compliance_note` (cite the matching per_touch_frames entry)

**Load-bearing rules:**
- Voice loyalty (NEVER use `must_not_say` phrases)
- Per-recipient grounding (every body MUST reference at least one recipient attribute — DOT#, power_units, etc.)
- Channel-tier compliance (postcard NEVER carries partner-bridge language; letter can lean on per-recipient signals; etc. per the independent-brand doctrine)
- Anti-rules (no discount language, no urgency theater, no fake credibility)

**v0 simplification:** runs ONCE per pipeline against the FIRST sample recipient
(not per-recipient at scale). At scale this runs N times per initiative; the
foundation slice just proves the seam.

### Verdicts (×3)

Every actor is paired with a verdict that returns:
```json
{
  "ship": true | false,
  "issues": [{"severity": "block" | "warn", "area": "<tag>", "detail": "<one line>"}],
  "redo_with": "<concise instruction>" | null
}
```

`ship: true` requires zero `block`-severity issues. In v0 (`MAX_VERDICT_RETRIES=1`)
a verdict block fails the pipeline at that step; flipping the constant in
`src/trigger/gtm-run-initiative-pipeline.ts` to ≥2 enables retry-with-hint.

---

## 7. What's NOT built (deferred per directive §11)

Explicit out-of-scope for this slice. Each is its own follow-up directive.

- **Subagents #2 (partner Exa research as its own MAGS agent)** — for v0, the master strategist reads partner_research from the existing `exa_research_jobs` row directly via `_fetch_exa_payload`. Refactoring this into a MAGS subagent ships separately.
- **Subagents #3 (channel/step materializer), #4 (audience materializer), #5, #6 (output shaper), #8, #9 (landing pages), #10 (voice agent)** — none of these are MAGS agents yet. They're either deterministic (materializers) or LLM agents not in this slice.
- **Adversarial subagent (#12a) and DSL validator (#12b)** — not built. Per-recipient verdict reasons over the DSL JSON shape directly for v0.
- **Sub-squad critic split** — verdicts carry critic-style reasoning inline.
- **Retry-with-hint loop** — `MAX_VERDICT_RETRIES = 1` in v0. The loop structure already supports retry; flipping the constant enables it without restructuring.
- **Cost-cents population** — column exists, NULL for v0. Cost tracking lands when the operator wants spend visibility per initiative.
- **Multi-org doctrine** — table supports per-org but the frontend ships with acq-eng hardcoded.
- **Supabase Realtime** — polling every 3s for v0; subscribe-to-table when stale UX becomes a problem.
- **Manual gating mode** — frontend can set `gating_mode='manual'` and the workflow respects the flag, but the actual `wait.forToken` call is gated by `ENABLE_MANUAL_GATE = false` constant in `src/trigger/gtm-run-initiative-pipeline.ts`. The complete-token bridge from hq-x to the Trigger SDK lands in a follow-up directive.
- **Voice-agent instantiation** — out of scope.
- **Brand-factory automation** — pre-payment scope, separate directive.
- **Removing `app/services/strategy_synthesizer.py`** — left dormant. Removal in a separate directive after the new pipeline is validated.

---

## 8. Architectural decisions still open (Ben's call, not auto-resolvable)

These remain on the operator's plate. An agent should not unilaterally resolve any of these.

| Decision | Where it bites | Notes |
|---|---|---|
| Per-recipient creative scale-out | `gtm-per-recipient-creative` runs once in v0; needs to fan out per-recipient at scale | The pipeline service's `_assemble_per_recipient_creative` only loads one sample recipient. Production would loop over the full audience. |
| Cost tracking population | `gtm_subagent_runs.cost_cents` is NULL in v0 | Anthropic SDK exposes `usage.input_tokens` + `usage.output_tokens` via the events stream; converting to cents requires a per-model rate table. Defer until spend is observable enough to need attribution. |
| Manual-gate signal bridge | hq-x → Trigger SDK `completeToken` call | Currently `/api/v1/admin/initiatives/{id}/advance` records intent in initiative.history but doesn't call Trigger. The `ENABLE_MANUAL_GATE` constant in the workflow is `false` so the gate is a no-op. |
| Critic / verdict split | Sub-squad pattern called for actor + verdict + critic; v0 ships actor + verdict only | Verdict carries critic-style reasoning inline. Splitting them into separate MAGS agents is a follow-up. |
| Per-piece cost table source | `app/services/gtm_pipeline.py` hardcodes `_PER_PIECE_COST_TABLE` | Currently in code; could move to `business.org_doctrine.parameters` per-org as the operator gets pricier. |

---

## 9. Operational notes

- **Doppler:** all hq-x scripts run via `doppler --project hq-x --config dev run -- ...`. Managed-agents-x scripts use the wrapper `./scripts/doppler` to handle environment-variable shadowing.
- **Anthropic billing:** the new key `ANTHROPIC_MANAGED_AGENTS_API_KEY` is distinct from `ANTHROPIC_API_KEY`. They CAN hold the same workspace key for v0; keeping them separate lets the operator rotate / scope / bill them independently later.
- **Vault + environment id:** session creation defaults to `vlt_011CZtjQ5LjLrbAd4gX7xA6E` and `env_01T3cywTrvvtZoUQYAzxMA1D` (matching `managed-agents-x/scripts/setup_orchestrator.py`). Override via Doppler `ANTHROPIC_MAGS_DEFAULT_VAULT_ID` / `ANTHROPIC_MAGS_DEFAULT_ENVIRONMENT_ID` if a vault rotates.
- **Run artifact paths:** master_strategist outputs land at `data/initiatives/<id>/master_strategy.<run_index>.md` on disk. The path is captured in `gtm_subagent_runs.output_artifact_path`. `data/initiatives/*/` is gitignored — to preserve specific artifacts, copy to `docs/initiatives-archive/`.
- **Tests:** `uv run pytest -q` shows 1068 passed (was 917 prior to this slice + 62 new = within range; the count grew as other PRs landed in parallel). The network-dependent E2E test in `tests/test_dat_gtm_pipeline_e2e.py` is gated by `RUN_E2E_GTM=1` and skipped by default.

---

## 10. The three new admin surfaces

### `/admin/initiatives` (list + drilldown)

**List page:** every initiative across orgs. Columns: brand, partner, status, pipeline_status, gating_mode, last run.

**Drilldown:**
- Header: brand × partner × contract metadata
- Pipeline timeline: every actor + verdict run with status pill (running / succeeded / failed / superseded), expand-to-show input / output / prompt-snapshot / mcp-calls / error-blob
- Per-run buttons: "Rerun from here" (re-fires pipeline with `startFrom=slug`)
- "Start pipeline" (when idle) with gating-mode selector
- Polls `/runs` every 3s while `pipeline_status='running'`

### `/admin/agents` (registry + editor)

**List page:** every registered agent. Shows slug, role, model, parent_actor_slug, description.

**Editor page (`/admin/agents/[slug]`):**
- Big textarea showing the current Anthropic-side system prompt
- Notes input
- Activate button — pushes new prompt + writes two version rows
- Version history table — every prior version with `version_index`, `activation_source`, when, notes, char count, Rollback button (disabled for `snapshot` rows)

### `/admin/doctrine` (single-page editor, acq-eng only)

- Markdown body textarea
- Parameters JSON textarea
- Save button — validates JSON parses, validates known parameter shapes via `org_doctrine.validate_parameters`, upserts

---

## 11. Bottom line

**Done:** managed-agents runtime, run-capture spine, snapshot-then-overwrite prompt versioning, three actor + verdict pairs, full admin surface across hq-x backend + Next.js frontend, Trigger.dev workflow, end-to-end seed script, 62 new tests.

**Next concrete piece of work:** run the unblock sequence in §5. The first iteration cycle will produce the first `verdict_block_after_retries` failure; that surfaces the first prompt to iterate via the frontend editor.

**The platform now exists.** Adding subagent N is one `system_prompt.md` + one entry in `setup_gtm_agents.py`'s `AGENTS` map + one entry in `PIPELINE_STEPS` (in both `gtm_pipeline.py` and `gtm-run-initiative-pipeline.ts`) + one input-assembler in `gtm_pipeline._assemble_input`. The frontend renders new rows automatically.
