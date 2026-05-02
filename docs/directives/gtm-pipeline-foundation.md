# Directive: GTM-initiative pipeline foundation

**Status:** Active. Supersedes [docs/directives/gtm-initiative-strategy-pipeline.md](gtm-initiative-strategy-pipeline.md) for the post-payment pipeline.

**Context:** You are working in `hq-x` and `managed-agents-x` (sibling repo at `/Users/benjamincrane/managed-agents-x`). Read the following before starting:

- [CLAUDE.md](../../CLAUDE.md)
- [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md)
- [docs/HANDOFF_GTM_PIPELINE_2026-05-01.md](../HANDOFF_GTM_PIPELINE_2026-05-01.md)
- [docs/handoff-pre-payment-pipeline-2026-04-30.md](../handoff-pre-payment-pipeline-2026-04-30.md)
- `managed-agents-x/README.md`
- `managed-agents-x/scripts/setup_dmaas_scaffold_author.py` (the closest precedent for "register one MAGS agent")
- `managed-agents-x/scripts/setup_orchestrator.py` (multi-MCP wiring pattern)
- `managed-agents-x/app/anthropic_client.py` (the only existing Managed Agents API client in the codebase)

**Scope clarification on autonomy:** Make strong engineering calls within the scope below. What you must not do: drift outside this scope, run any deploy commands, modify the existing DMaaS / Lob / Dub / Vapi / voice / SMS / Entri surfaces, touch the pre-payment pipeline (target_accounts / brand factory / outreach_brief / audience slicer), or build any of the explicitly out-of-scope subagents listed in §11.

---

## 1. Why this directive exists

The post-payment pipeline (the 13-node diagram the operator sketched: payment → sequence definer → research → strategy → brand → creative → adversarial → ship) is currently:

- Partially built as a single `app/services/strategy_synthesizer.py` Anthropic-Messages-API service (V1 active, V2 sandboxed, V3 planned)
- Untestable in isolation — operator cannot debug intermediate outputs, cannot edit prompts without re-deploying, cannot replay individual steps
- Architecturally on the wrong runtime: prior directive locked Messages API direct; this directive **reverses that decision** for the post-payment pipeline. The new runtime is Anthropic's Managed Agents API via `managed-agents-x` ("MAGS").

The architectural commitments locked for this build:

- **Managed Agents API** (`/v1/agents` with `managed-agents-2026-04-01` beta header) is the runtime for every LLM call in this pipeline. Each subagent is a separately-registered MAGS agent.
- **Trigger.dev is the orchestrator.** A single workflow `gtm.run-initiative-pipeline` sequences subagents, calls MAGS via HTTP, persists per-step run rows. Optional manual gates between steps via `wait.forSignal()`.
- **Sub-squad pattern: actor + verdict.** Every subagent in this directive ships as a *pair* of MAGS agents — an actor that produces output and a verdict that decides ship/redo. Sub-squad critic split is deferred (verdict carries critic-style reasoning inline for v0).
- **Prefer LLM agents over deterministic logic.** Where the diagram suggests a deterministic check (e.g. economics arithmetic in #1, DSL validation in #12b), implement as a MAGS agent that calls deterministic tools rather than as a hardcoded post-check. Iteration headroom over speed.
- **Prompts: Anthropic-as-live, DB-as-history.** Each "activate" call snapshots the current Anthropic state into `business.agent_prompt_versions` *before* pushing the new prompt. This preserves rollback targets we'd otherwise lose to the destructive POST `/v1/agents/{id}` semantics.
- **Disk-mirrored prompts.** Each agent's system prompt also lives at `managed-agents-x/data/agents/<slug>/system_prompt.md` for git versioning. The setup script reads the `.md` at registration time. The `.md` and the active Anthropic prompt can drift between activations; that's acceptable — DB versions are the canonical history.
- **Default model: `claude-opus-4-7`.** Cost is acceptable for this pipeline; this is the productized service. Per-agent model overrides allowed via the registry table.
- **Run capture is the spine of the debugging story.** Every actor + verdict invocation writes a `business.gtm_subagent_runs` row capturing input, output, prompt snapshot, cost, status. The frontend reads this table.
- **Frontend → MAGS via hq-x backend proxy.** No MAGS keys in the browser. The browser hits hq-x with a normal Supabase JWT; hq-x forwards to MAGS using its own M2M credentials.

---

## 2. Foundation slice — what this directive ships

This is the platform on which all subsequent subagent registrations slot in mechanically.

1. **Three migrations** (run capture, prompt versions, org doctrine) + one agent registry migration.
2. **Two doctrine docs** authored from the operator's stated guidance, mirrored into DB.
3. **MAGS subagent registrations for the diagram's three load-bearing nodes:** #1 sequence definer, #7 master strategist, #11 per-recipient creative. Each ships as actor + verdict (6 MAGS agents total). For v0, intermediate inputs from missing nodes (#3, #6, #8, #10) are inlined inside #7's prompt — concretely, #7's user message bundles the audience descriptor + the existing partner-research Exa output + brand context directly. Same for #11 reading #7 + brand_content directly.
4. **hq-x backend** — prompt versioning surface, run-capture surface, orchestrator kickoff endpoint, MAGS proxy, internal callbacks for Trigger.dev.
5. **Trigger.dev workflow** — `gtm.run-initiative-pipeline` sequencing the three actors + three verdicts. Gating mode flag honored.
6. **Frontend admin** — initiative list, per-initiative run drilldown, agent-prompt editor + version history, rerun-step button.
7. **End-to-end exercise** — script that fires the pipeline against the existing DAT initiative and validates that all six run rows persist + frontend can render them.

After this directive ships, adding a new subagent is: write its `system_prompt.md`, write its `setup_<slug>.py`, register it, add its slug to the Trigger.dev step list, and the frontend renders it automatically.

---

## 3. Existing-state facts to verify before starting

- `business.gtm_initiatives` row for DAT exists: id `bbd9d9c3-c48e-4373-91f4-721775dca54e`, status `strategy_ready`. Use this initiative as the end-to-end fixture. Reset it to `awaiting_strategy_synthesis` (or whatever the appropriate "re-run" state is — define in §4 if needed) before each pipeline run.
- `business.brand_content` for Capital Expansion is populated (10 rows). Brand id `1c570a63-eac3-436a-8b52-bf0a2e1818e4`. Disk source at `data/brands/capital-expansion/`.
- `app/services/strategy_synthesizer.py` (V1 Messages-API direct) exists. **Do not delete it in this directive.** Leave it dormant; a follow-up directive removes it once the MAGS pipeline supersedes it in production. The router endpoint `POST /api/v1/initiatives/{id}/synthesize-strategy` likewise stays (calls the old synthesizer); add a sibling endpoint `POST /api/v1/admin/initiatives/{id}/start-pipeline` that fires the new pipeline. Old surface coexists; the operator can compare.
- `managed-agents-x` has `app/anthropic_client.py` with `list_agents`, `get_agent`, `update_agent` (POST /v1/agents/{id}). It does **not** currently have a `create_agent`; you will add one. It does not have a session-creation method; you will add one. Beta header `managed-agents-2026-04-01` already set.
- `managed-agents-x` has no business logic / routers yet ("deployable skeleton"). Wiring this directive's hq-x-→-MAGS proxy means hq-x calls Anthropic *through* a thin MAGS HTTP client written in hq-x — you don't need to add API endpoints to MAGS itself. MAGS at HEAD is just the holder of agent definitions and prompt files; hq-x is the only caller of Anthropic's API for the post-payment pipeline.

> Decision implication: hq-x grows its own copy of the Anthropic Managed Agents API client (mirroring the MAGS one). This avoids cross-service HTTP for agent invocation. The MAGS repo's role in this directive is reduced to: holds the `setup_<slug>.py` scripts + `data/agents/<slug>/system_prompt.md` files. hq-x calls Anthropic directly. Future directives may move the API client into a shared package; for now, duplication is fine.

- Doppler project for hq-x: `hq-x`. Doppler config: `dev` for local. The new env vars introduced by this directive:
  - **`ANTHROPIC_MANAGED_AGENTS_API_KEY`** — the only new secret. Set in hq-x Doppler (`dev` + `prd`). The operator has already populated `dev`. Read in `app/config.py` via `require("anthropic_managed_agents_api_key")`. **Distinct from the existing `ANTHROPIC_API_KEY`** used by the dormant V1 synthesizer — keep them separate even if they end up holding the same Anthropic workspace key, so the two paths can be billed/scoped/rotated independently later.
  - **Trigger.dev environment requires no new secret.** Per §8 below, Trigger.dev does NOT call Anthropic directly. Trigger.dev only needs `TRIGGER_SHARED_SECRET` + the hq-x base URL it already has. Anthropic invocation lives entirely server-side in hq-x.

---

## 4. Migrations

Filename convention: UTC timestamp prefix per `CLAUDE.md`. Use one timestamp per logical schema change; create them in lexical order.

### 4.1 `<ts>_gtm_agent_registry.sql`

```sql
CREATE TABLE business.gtm_agent_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_slug TEXT NOT NULL UNIQUE,
    anthropic_agent_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('actor', 'verdict', 'critic', 'orchestrator')),
    parent_actor_slug TEXT,        -- non-null for role='verdict' or 'critic'; references agent_slug of paired actor
    model TEXT NOT NULL DEFAULT 'claude-opus-4-7',
    description TEXT,
    deactivated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX gtm_agent_registry_role_idx ON business.gtm_agent_registry (role) WHERE deactivated_at IS NULL;
```

### 4.2 `<ts>_agent_prompt_versions.sql`

```sql
CREATE TABLE business.agent_prompt_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_slug TEXT NOT NULL,
    anthropic_agent_id TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    version_index INT NOT NULL,
    activation_source TEXT NOT NULL CHECK (activation_source IN ('setup_script', 'frontend_activate', 'rollback', 'snapshot')),
    parent_version_id UUID REFERENCES business.agent_prompt_versions(id),
    activated_by_user_id UUID,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_slug, version_index)
);
CREATE INDEX agent_prompt_versions_slug_idx ON business.agent_prompt_versions (agent_slug, version_index DESC);
```

`activation_source='snapshot'` rows are written by the activate endpoint to capture the Anthropic-current state *before* it gets overwritten. `activation_source='frontend_activate'` is the new prompt being pushed. Each activate creates two rows: one snapshot, one new.

### 4.3 `<ts>_gtm_subagent_runs.sql`

```sql
CREATE TABLE business.gtm_subagent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    initiative_id UUID NOT NULL REFERENCES business.gtm_initiatives(id) ON DELETE CASCADE,
    agent_slug TEXT NOT NULL,
    run_index INT NOT NULL,
    parent_run_id UUID REFERENCES business.gtm_subagent_runs(id),  -- verdicts link to their actor's run
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'superseded')),
    input_blob JSONB NOT NULL,
    output_blob JSONB,
    output_artifact_path TEXT,                          -- on-disk path for large markdown outputs
    system_prompt_snapshot TEXT NOT NULL,               -- verbatim prompt active at run start
    prompt_version_id UUID REFERENCES business.agent_prompt_versions(id),
    anthropic_agent_id TEXT NOT NULL,
    anthropic_session_id TEXT,
    anthropic_request_ids JSONB,                        -- array of request ids returned by Anthropic
    mcp_calls JSONB,                                    -- structured trace of MCP tool invocations
    cost_cents INT,
    model TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error_blob JSONB,
    UNIQUE (initiative_id, agent_slug, run_index)
);
CREATE INDEX gtm_subagent_runs_initiative_idx ON business.gtm_subagent_runs (initiative_id, started_at DESC);
CREATE INDEX gtm_subagent_runs_status_idx ON business.gtm_subagent_runs (status, started_at DESC);
CREATE INDEX gtm_subagent_runs_slug_idx ON business.gtm_subagent_runs (agent_slug, started_at DESC);
```

Replay semantics: rerunning a step means inserting a new row with `run_index = max + 1`. The previous row stays as `succeeded` (or whatever); downstream rows' status is set to `superseded` by the orchestrator before it re-fires.

### 4.4 `<ts>_org_doctrine.sql`

```sql
CREATE TABLE business.org_doctrine (
    organization_id UUID PRIMARY KEY REFERENCES business.organizations(id) ON DELETE CASCADE,
    doctrine_markdown TEXT NOT NULL,                    -- canonical free-form policy doc
    parameters JSONB NOT NULL DEFAULT '{}',             -- structured numeric overrides; see §5.2
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by_user_id UUID
);
```

`parameters` JSONB shape (typed at the application layer, validated in `app/services/org_doctrine.py`):

```json
{
  "target_margin_pct": 0.40,
  "max_capital_outlay_pct_of_revenue": 0.50,
  "min_per_piece_cents": 100,
  "max_per_piece_cents": 800,
  "default_touch_count_by_audience_size_bucket": {
    "0_500": 4,
    "500_2500": 3,
    "2500_10000": 3,
    "10000_plus": 2
  },
  "model_tier_by_step_type": {
    "default": "claude-opus-4-7"
  },
  "gating_mode_default": "auto"
}
```

### 4.5 Optional: extend `business.gtm_initiatives`

Add columns:
- `gating_mode TEXT NOT NULL DEFAULT 'auto' CHECK (gating_mode IN ('auto', 'manual'))`
- `pipeline_status TEXT` — separate from the existing high-level `status` enum. Tracks pipeline-internal state: `'idle' | 'running' | 'gated' | 'completed' | 'failed'`.
- `last_pipeline_run_started_at TIMESTAMPTZ`

If column-add migration introduces ENUM conflicts with the existing status check constraint, just use TEXT with CHECK constraints.

---

## 5. Doctrine documents

### 5.1 Independent-brand doctrine

Path: `data/brands/_meta/independent-brand-doctrine.md`. Mirrored to `business.brand_content` with `brand_id IS NULL`, `content_key='_meta:independent-brand-doctrine'`. Extend `scripts/sync_brand_content.py` to handle the brand-id-NULL meta case (or write a sibling `scripts/sync_meta_doctrine.py`).

Content seed — the operator articulated this verbatim; encode it into the doc:

- **The illusion contract.** The brand presents as an independent operator providing the value (cash advance / financing / capital / etc.) directly. Never as an aggregator, marketplace, broker-of-brokers, or partner-finder.
- **Channel-tier framing rules** — what each surface CAN say:
  - **Postcard** (shallowest, lowest dwell time): act as the offering. "Need cash fast?" "Same-day funding for established carriers." No partner talk. No brand-bridge language. The headline IS the value-prop.
  - **Letter** (medium dwell, per-recipient leverage allowed): can lean on Exa/Claygent-derived recipient-specific signals. Examples: "we noticed your authority just hit 90 days active," "saw you won a 2026 govt contract for X," "12 power units in a 50-mile radius means…". The brand still speaks as the operator, but with recipient-specific framing.
  - **Landing page** (deeper surface, can carry partner-bridge language): primary copy still acts as the offering; secondary CTA can include "want us to connect you with a specialist?" or "we partner with vetted operators in [vertical]."
  - **Voice agent** (deepest, conversational, partner-bridge explicit): "We work with partners who specialize in [recipient-derived vertical from Exa]. Based on what you've told me, want me to connect you with someone who can quote this today?"
  - **Email** (between letter and landing): per-recipient lean in body, never partner talk in subject/preview, partner-bridge mention OK as soft body CTA only.
- **Anti-rules** (literal prohibitions):
  - No "marketplace," "network of partners," "we connect you with...," or similar meta-positioning on print or email subject lines.
  - No mention of brand age, founding year, or any patina-related claim.
  - No claims of partner-side capability the brand can't fulfill (the brand should always sound like it CAN do the thing — even if the actual fulfillment routes through the partner).
- **Per-recipient detail provenance.** Per-recipient personalization comes from Exa/Claygent research output stored against the audience and the recipient's data points from DEX. The partner provides only routing config (phone, hours, qualification rules, intro email). The partner does NOT supply per-recipient text. Throughput depends on this — if any creative step requires partner input per recipient, the throughput goal fails.
- **Voice agent partner-bridge wording template** (referenced by #11 and the voice-agent-instantiator in a later directive): "We work with partners who specialize in [vertical_from_exa]. They have experience with [pain_signal_from_exa]. Want me to connect you?"

### 5.2 acq-eng operator doctrine ("my margin")

Path: `data/orgs/acq-eng/doctrine.md`. Mirrored to `business.org_doctrine` for the acq-eng organization (id `4482eb19-f961-48e1-a957-41939d042908`). Both the markdown body and the `parameters` JSONB are populated.

Content seed:

- **Margin floor.** Target margin per initiative is 40% of partner payment after capital outlay. Below 30% triggers a hard reject in #1's reasoning. Below 40% but above 30% requires explicit operator override flag on the contract.
- **Capital outlay cap.** Default cap is 50% of partner payment, applied in addition to any contract-level `max_capital_outlay_cents`. Whichever is smaller binds.
- **Per-piece outlay guardrails.** Floor $1.00 per piece (sanity — no postcards cheaper than this in practice). Ceiling $8.00 per piece (booklets / heavy self-mailers; flag anything above for human review).
- **Default touch counts by audience size bucket** — see `parameters` JSONB above. These are heuristic priors for #1; #1 may override with explicit reasoning.
- **Model tier policy.** Default Opus across all subagents in this initial build. After end-to-end output quality is validated, the operator will dial down per-step (e.g. #4 directive writer → Sonnet, #6 output shaper → Sonnet) to reduce cost. All overrides flow through `parameters.model_tier_by_step_type`.
- **Gating mode default.** `auto` for v0 — straps end-to-end without manual intervention so failure cascades surface naturally. Operator flips to `manual` per-initiative to gate-debug specific runs.

The markdown body is the prose version; the `parameters` JSONB is what #1 actually reads. Both must be authored.

---

## 6. MAGS subagent registrations

For each subagent listed below: author the system prompt at `managed-agents-x/data/agents/<slug>/system_prompt.md`, write `managed-agents-x/scripts/setup_<slug>.py`, run the setup script, capture the returned `agent_id`, INSERT into `business.gtm_agent_registry`. Setup scripts mirror `setup_dmaas_scaffold_author.py` style (no `agent_defaults` row needed — sessions are started by hq-x backend with explicit env/vault context).

Six MAGS agents in this directive:

| Slug | Role | Parent actor | MCPs (initial) | Description |
|---|---|---|---|---|
| `gtm-sequence-definer` | actor | — | dex-mcp | Reads partner_contract + acq-eng doctrine + audience descriptor; outputs JSON channel mix + per-touch type + per-touch delay + economics justification |
| `gtm-sequence-definer-verdict` | verdict | `gtm-sequence-definer` | — | Reads actor's draft + same inputs; returns `{ship: bool, issues: [...], redo_with: string\|null}` |
| `gtm-master-strategist` | actor | — | exa, dex-mcp | Reads audience descriptor + sample members + partner research + sequence-definer output + acq-eng doctrine + independent-brand doctrine; outputs Master Strategy markdown (per-touch frame, conceptual, NOT literal copy) |
| `gtm-master-strategist-verdict` | verdict | `gtm-master-strategist` | — | Verdicts the Master Strategy doc against schema + doctrine adherence + voice loyalty |
| `gtm-per-recipient-creative` | actor | — | dex-mcp | Reads master strategy + brand_content + recipient data points; outputs per-piece copy + design DSL JSON for each (recipient × DM step) |
| `gtm-per-recipient-creative-verdict` | verdict | `gtm-per-recipient-creative` | — | Verdicts per-piece output against zone-binding/MediaBox feasibility (calls deterministic checker via dmaas tools when registered; for v0, reasons over the DSL JSON shape directly) |

For the `mcp_servers` list in each setup script, follow the `setup_orchestrator.py` pattern (URL-named MCP entries). Verdicts get no MCPs — they reason over what the actor produced plus the original inputs.

System prompt seeds (author each `.md` to be the v0 starting point — operator will iterate via the frontend prompt editor afterward):

- **gtm-sequence-definer**: "Given a partner contract (amount, duration, max outlay), an audience descriptor (size, key attributes), and acq-eng's operator doctrine (margin floor, outlay cap, per-piece pricing guardrails, touch-count heuristics), produce a JSON output specifying: channels (always direct_mail + email; voice for inbound only), touch count per channel, per-touch mailer type (postcard / letter / self_mailer / snap_pack / booklet), per-touch delay days, per-touch estimated cost, total estimated outlay, projected margin, and a justification paragraph. Honor the doctrine's hard constraints. If the contract's economics violate the doctrine's margin floor, output `{decision: 'reject_economics', reason: ...}` instead."
- **gtm-master-strategist**: "Read the audience descriptor + 5-10 sample members + partner research + sequence-definer output + brand_content + independent-brand doctrine. Produce a Master Strategy markdown document with sections: audience-frame (who they are, in operator voice), pain-and-trigger map (what's hurting them, what observable signals are available per recipient), per-touch frame (conceptual angle for each touch in the sequence — NOT literal copy), voice rules from brand + doctrine, anti-framings (what NOT to say). The frame for each touch is what #11 will render into per-recipient copy."
- **gtm-per-recipient-creative**: "Read the Master Strategy doc + brand_content (positioning, voice, audience-pain, creative-directives) + the recipient's data points (DOT#, power_units, authority_granted_at, state, MC#, etc. — whatever DEX exposes). For each (recipient × direct-mail step) in the sequence, produce: a piece-type-specific JSON object containing the per-zone copy (headline, body, CTA, address-block, postage-area, etc. as the piece type requires) and a design DSL specifying layout, brand colors, font weights. The design DSL must be compatible with the existing dmaas zone-binding / MediaBox invariants (validator will check). Per-recipient personalization MUST come from the recipient's data + Exa-research-derived audience signals — never from partner-supplied text."

Each verdict prompt: "Read the actor's output and the original inputs. Identify any violations of doctrine, schema, or voice. Return strict JSON: `{ship: bool, issues: [{severity: 'block'|'warn', area: string, detail: string}], redo_with: string|null}`. Ship only when no `block`-severity issues remain."

---

## 7. hq-x backend

### 7.1 New module: `app/services/anthropic_managed_agents.py`

Thin httpx async client mirroring `managed-agents-x/app/anthropic_client.py` plus the methods MAGS doesn't yet have:

- `async def get_agent(agent_id: str) -> dict`
- `async def update_agent_system_prompt(agent_id: str, system_prompt: str) -> dict` — POST `/v1/agents/{id}` with `{system: system_prompt}` (note: depending on Anthropic's actual schema, the field name may be `system_prompt`; verify against `update_agent` in MAGS — same field name used there)
- `async def create_session(agent_id: str, vault_ids: list[str], environment_id: str) -> dict` — POST `/v1/agents/{id}/sessions`
- `async def send_message(session_id: str, content: str | list) -> dict` — POST against the session endpoint per Anthropic's session API; verify request shape against the API docs at request-time
- `async def get_session(session_id: str) -> dict`

Auth: `ANTHROPIC_MANAGED_AGENTS_API_KEY` from Doppler. Beta header: `managed-agents-2026-04-01`. Wrap exceptions in a `ManagedAgentsError` class.

### 7.2 New module: `app/services/agent_prompts.py`

```python
async def activate_prompt(
    agent_slug: str,
    new_system_prompt: str,
    activated_by_user_id: UUID | None,
    notes: str | None,
) -> dict:
    """
    1. Resolve anthropic_agent_id from gtm_agent_registry by slug
    2. GET /v1/agents/{id} from Anthropic — capture current system_prompt
    3. INSERT row into agent_prompt_versions with activation_source='snapshot' (the soon-to-be-overwritten state)
    4. POST /v1/agents/{id} with new_system_prompt
    5. INSERT row into agent_prompt_versions with activation_source='frontend_activate'
    6. Return {snapshot_version, new_version}
    """

async def rollback_prompt(
    agent_slug: str,
    version_index: int,
    activated_by_user_id: UUID | None,
) -> dict:
    """
    Same shape as activate_prompt, but the new prompt is read from
    agent_prompt_versions where (agent_slug, version_index) matches.
    activation_source='rollback' on the new row.
    """

async def get_current(agent_slug: str) -> dict
async def list_versions(agent_slug: str, limit: int, offset: int) -> list[dict]
```

### 7.3 New module: `app/services/gtm_pipeline.py`

The orchestration boundary: Trigger.dev sequences steps; hq-x runs each step. Each step = one HTTP call from Trigger.dev to `/internal/gtm/initiatives/{id}/run-step`. Inside that single request, hq-x: resolves agent + prompt + input, INSERTs a `gtm_subagent_runs` row, opens an Anthropic session, sends the message, collects the final response, parses it, updates the row to terminal state, and returns the structured output. **Trigger.dev never holds the Anthropic key and never sees Anthropic.**

```python
async def kickoff_pipeline(initiative_id: UUID, gating_mode: str = "auto") -> str:
    """Update initiative.pipeline_status='running'; fire Trigger.dev task; return Trigger run id."""

async def run_step(
    initiative_id: UUID,
    agent_slug: str,
    hint: str | None,                       # populated by Trigger.dev only on retry-with-hint
    upstream_outputs: dict | None = None,   # actor outputs the verdict needs to read; passed by Trigger.dev
) -> dict:
    """
    Single-call execution of one agent. Sequence:
      1. Resolve agent_slug -> registry row (anthropic_agent_id, role, model)
      2. Resolve current active prompt from agent_prompt_versions (latest)
      3. Assemble input_blob from initiative state + upstream_outputs + hint
      4. run_index = max(existing for this initiative+slug) + 1
      5. Mark all gtm_subagent_runs for this initiative downstream of this slug as 'superseded'
      6. INSERT gtm_subagent_runs row (status='running', input_blob, prompt snapshot, etc.)
      7. anthropic.create_session(anthropic_agent_id, vault_ids, environment_id)
      8. anthropic.send_message(session_id, content=<rendered input>)
         - Wait for terminal assistant turn (poll/stream — see §7.1)
         - Collect anthropic_request_ids, mcp_calls trace, token usage
      9. Parse the final assistant text into output_blob (per-agent schema)
     10. UPDATE the run row: status='succeeded' (or 'failed' on parse/API error), output_blob,
         output_artifact_path (if large markdown), anthropic_session_id, mcp_calls, cost_cents
     11. Return: {
              run_id, status, output_blob, output_artifact_path,
              prompt_version_id, anthropic_session_id, cost_cents
         }

    Failure modes:
      - Anthropic API error: status='failed', error_blob populated, raises RunStepError
      - Output parse error: status='failed', error_blob has the raw text, raises RunStepError
      - Verdict ship=false is NOT a failure — that's a normal succeeded run with output_blob.ship=false;
        Trigger.dev decides what to do based on the structured output.
    """

async def request_rerun(initiative_id: UUID, from_agent_slug: str) -> str:
    """
    Marks the run for from_agent_slug + everything downstream as 'superseded'.
    Re-fires the pipeline via Trigger.dev with start_from=from_agent_slug.
    Returns the new Trigger.dev run id.
    """
```

`run_step` is the only seam between Trigger.dev and Anthropic. If we later move actor/verdict loop logic into hq-x or change runtimes, this is the only function that needs to evolve.

### 7.4 New module: `app/services/org_doctrine.py`

CRUD for the doctrine table. The frontend doctrine editor calls these; the gtm-sequence-definer reads the parameters JSONB at run start.

### 7.5 Routers

New file: `app/routers/admin_agents.py`. Mounted at `/api/v1/admin/agents`. All endpoints under `verify_supabase_jwt` + a new `require_platform_operator` dependency (use the existing `platform_operator` role check from the tenancy model — see `docs/tenancy-model.md`).

```
GET    /api/v1/admin/agents                            -> list registry rows
GET    /api/v1/admin/agents/{slug}                     -> {agent_slug, anthropic_agent_id, model, current_system_prompt, latest_version_index, latest_version_at}
POST   /api/v1/admin/agents/{slug}/activate            -> body: {system_prompt, notes?}; returns {snapshot_version, new_version}
POST   /api/v1/admin/agents/{slug}/rollback            -> body: {version_index}; returns {new_version}
GET    /api/v1/admin/agents/{slug}/versions            -> paginated; query params: limit, offset
```

New file: `app/routers/admin_initiatives.py`. Mounted at `/api/v1/admin/initiatives`.

```
GET    /api/v1/admin/initiatives                                            -> list initiatives + pipeline_status
GET    /api/v1/admin/initiatives/{id}                                       -> initiative + recent runs summary
POST   /api/v1/admin/initiatives/{id}/start-pipeline                        -> body: {gating_mode?: 'auto'|'manual'}; returns {trigger_run_id, pipeline_status}
GET    /api/v1/admin/initiatives/{id}/runs                                  -> paginated; filter by agent_slug
GET    /api/v1/admin/initiatives/{id}/runs/{run_id}                         -> single run with full input/output/prompt/mcp_calls
POST   /api/v1/admin/initiatives/{id}/runs/{slug}/rerun                     -> 202 + new trigger_run_id
POST   /api/v1/admin/initiatives/{id}/advance                               -> fires Trigger.dev signal to advance past current gate
```

New file: `app/routers/admin_doctrine.py`. Mounted at `/api/v1/admin/doctrine`.

```
GET    /api/v1/admin/doctrine/{org_id}                 -> {markdown, parameters, updated_at}
POST   /api/v1/admin/doctrine/{org_id}                 -> body: {markdown, parameters}
```

New file: `app/routers/internal/gtm_pipeline.py`. Mounted at `/internal/gtm`. Uses `TRIGGER_SHARED_SECRET` per existing pattern (mirror `app/routers/internal/exa_jobs.py`).

```
POST   /internal/gtm/initiatives/{id}/run-step                              -> body: {agent_slug, hint?, upstream_outputs?}
                                                                                returns: {run_id, status, output_blob, output_artifact_path,
                                                                                          prompt_version_id, anthropic_session_id, cost_cents}
                                                                                Synchronous from Trigger.dev's POV — blocks for the full
                                                                                Anthropic session. Set the FastAPI/uvicorn read timeout
                                                                                generously (suggest 600s). Inside, calls
                                                                                gtm_pipeline.run_step.
POST   /internal/gtm/initiatives/{id}/pipeline-completed                    -> final cleanup; sets pipeline_status='completed'
POST   /internal/gtm/initiatives/{id}/pipeline-failed                       -> body: {failed_at_slug, reason}; sets pipeline_status='failed'
```

Single endpoint, single source of truth for an agent invocation. No `start`/`complete`/`fail` split — that pattern leaks orchestration responsibility into Trigger.dev's TS layer and risks divergence between "Anthropic responded" and "DB row reflects Anthropic response." With one endpoint, the DB write is in the same transaction boundary as the Anthropic call.

---

## 8. Trigger.dev workflow

New file: `src/trigger/gtm-run-initiative-pipeline.ts`. Mirror the structure of existing tasks (`src/trigger/exa-process-research-job.ts`). Trigger.dev sequences actor/verdict pairs and decides retry-vs-advance based on the structured output of each `/run-step` call. **It does not call Anthropic directly. It does not hold the Anthropic key. It does no business logic beyond the loop.**

```typescript
// Adapt to actual Trigger.dev SDK; this is the structural shape, not literal code.

import { task, wait } from "@trigger.dev/sdk/v3";
import { hqxPost } from "./lib/hqx-client";  // existing helper; uses TRIGGER_SHARED_SECRET

type StepResult = {
  run_id: string;
  status: "succeeded" | "failed";
  output_blob: any;
  output_artifact_path: string | null;
  prompt_version_id: string;
  anthropic_session_id: string | null;
  cost_cents: number | null;
};

type VerdictOutput = { ship: boolean; issues: any[]; redo_with: string | null };

const STEPS: { actor: string; verdict: string }[] = [
  { actor: "gtm-sequence-definer",       verdict: "gtm-sequence-definer-verdict"       },
  { actor: "gtm-master-strategist",      verdict: "gtm-master-strategist-verdict"      },
  { actor: "gtm-per-recipient-creative", verdict: "gtm-per-recipient-creative-verdict" },
];

const MAX_VERDICT_RETRIES = 1;  // v0: no retry-with-hint; first verdict-fail = step fails

export const runInitiativePipeline = task({
  id: "gtm.run-initiative-pipeline",
  run: async ({ initiativeId, gatingMode, startFrom }: {
    initiativeId: string;
    gatingMode: "auto" | "manual";
    startFrom?: string;
  }) => {
    const startIdx = startFrom ? STEPS.findIndex(s => s.actor === startFrom) : 0;
    if (startIdx < 0) throw new Error(`unknown startFrom: ${startFrom}`);

    for (let i = startIdx; i < STEPS.length; i++) {
      const { actor, verdict } = STEPS[i];

      if (gatingMode === "manual" && i > startIdx) {
        await wait.forSignal({ id: `advance:${initiativeId}:${actor}`, timeout: "24h" });
      }

      let actorOutput: any = null;
      let lastVerdict: VerdictOutput | null = null;

      for (let attempt = 0; attempt <= MAX_VERDICT_RETRIES; attempt++) {
        // 1. Run actor (with hint from prior verdict if retrying)
        const actorRun = await callRunStep(initiativeId, actor, {
          hint: attempt > 0 ? lastVerdict?.redo_with ?? null : null,
        });
        if (actorRun.status === "failed") {
          await hqxPost(`/internal/gtm/initiatives/${initiativeId}/pipeline-failed`, {
            failed_at_slug: actor, reason: "actor_run_failed",
          });
          throw new Error(`actor ${actor} failed`);
        }
        actorOutput = actorRun.output_blob;

        // 2. Run verdict against actor output
        const verdictRun = await callRunStep(initiativeId, verdict, {
          upstream_outputs: { [actor]: actorOutput },
        });
        if (verdictRun.status === "failed") {
          await hqxPost(`/internal/gtm/initiatives/${initiativeId}/pipeline-failed`, {
            failed_at_slug: verdict, reason: "verdict_run_failed",
          });
          throw new Error(`verdict ${verdict} failed`);
        }
        lastVerdict = verdictRun.output_blob as VerdictOutput;

        if (lastVerdict.ship) break;
      }

      if (!lastVerdict?.ship) {
        await hqxPost(`/internal/gtm/initiatives/${initiativeId}/pipeline-failed`, {
          failed_at_slug: actor,
          reason: "verdict_block_after_retries",
        });
        throw new Error(`actor ${actor} did not pass verdict after ${MAX_VERDICT_RETRIES + 1} attempts`);
      }
    }

    await hqxPost(`/internal/gtm/initiatives/${initiativeId}/pipeline-completed`, {});
  },
});

async function callRunStep(
  initiativeId: string,
  agentSlug: string,
  body: { hint?: string | null; upstream_outputs?: Record<string, any> },
): Promise<StepResult> {
  // Single HTTP call. hq-x blocks for the full Anthropic round-trip.
  // Configure the Trigger HTTP client with a 600s timeout for this endpoint.
  return await hqxPost<StepResult>(
    `/internal/gtm/initiatives/${initiativeId}/run-step`,
    { agent_slug: agentSlug, ...body },
    { timeoutMs: 600_000 },
  );
}
```

### Notes for the implementer

- `MAX_VERDICT_RETRIES = 1` for v0 means: actor runs, verdict runs, if verdict says `ship: false` the pipeline fails at that step. Retry-with-hint capability is in the loop already (the `attempt > 0` branch); flipping the constant to >1 in a follow-up directive enables it without restructuring.
- Each `callRunStep` is a single, durable Trigger.dev HTTP call. Trigger.dev's task durability covers crashes / retries at the network layer. The TS task itself owns *zero* state — every state mutation lands in the hq-x DB before `run-step` returns.
- Anthropic session lifecycle is owned end-to-end inside `gtm_pipeline.run_step` (§7.3): create session, send message, await terminal turn, parse, persist, close. Trigger.dev never sees the session id except as a returned field.
- `hqx-client.ts` (`src/trigger/lib/hqx-client.ts`) already exists in the codebase per existing Trigger pattern; extend its options to accept a per-call `timeoutMs` if it doesn't already.
- Vault and environment ids passed to `anthropic.create_session` inside `run_step`: use the same constants as `managed-agents-x/scripts/setup_orchestrator.py` for v0. Hardcoded constants in `app/services/gtm_pipeline.py` are fine for now; extracting to per-org config is a follow-up.

---

## 9. Frontend admin

The existing admin frontend (Next.js) has cards for Audience Builder, Audiences, Voice Agents, DMaaS, Scaffolds. Add two new top-level cards: **GTM Initiatives** and **Agents & Prompts**. Each card links to its index page.

### 9.1 Pages

- `/admin/initiatives` — index. Table of initiatives with columns: brand, partner, audience-spec name, pipeline_status, last run, gating_mode. Row click → drilldown.
- `/admin/initiatives/[id]` — drilldown. Sections:
  - Header: initiative metadata (brand, partner, contract terms, audience spec descriptor)
  - "Pipeline" timeline showing each step's most recent run: agent_slug, status, started_at, duration, cost, expand-to-show input/output/prompt-snapshot
  - Per-run buttons: "View full output" (modal), "Rerun this step" (kicks off `POST /api/v1/admin/initiatives/{id}/runs/{slug}/rerun`), "Rerun from here" (re-fires pipeline with `startFrom=slug`)
  - "Advance gate" button when `pipeline_status='gated'`
  - "Start pipeline" button when `pipeline_status='idle'` — also offers `gating_mode` selector
- `/admin/agents` — index. Table of registry rows: slug, role, parent_actor_slug, model, current version_index, last activated.
- `/admin/agents/[slug]` — agent editor. Shows current `system_prompt` in a large textarea, "Activate" button writes new version. Below: version history table with "Rollback to this version" button per row. Read-only fields: anthropic_agent_id, role, model, MCPs configured (from registry).
- `/admin/doctrine` — single page since acq-eng is the only org for v0. Two text areas: markdown body + JSON parameters. Save POSTs to `/api/v1/admin/doctrine/{org_id}`.

### 9.2 Auth + proxy

All admin routes are gated to platform_operator role server-side. The Next.js API routes (or Next.js server actions, depending on the existing app's pattern) call hq-x with the user's Supabase JWT. hq-x's `require_platform_operator` dependency validates the role from the JWT claims — if the existing tenancy model doesn't surface `platform_operator` directly in claims, fall back to a server-side check against the user's row in `business.platform_operators` (or whatever the existing table is — verify per `docs/tenancy-model.md`).

Frontend never holds the MAGS key. All MAGS API calls flow: Frontend → hq-x admin endpoints → hq-x services → Anthropic Managed Agents API.

### 9.3 Live updates

For v0, the drilldown page polls `/api/v1/admin/initiatives/{id}/runs` every 3 seconds while `pipeline_status='running'`. Live updates via Supabase Realtime are a follow-up enhancement — polling is sufficient and avoids subscribing to a table the frontend doesn't otherwise know about.

---

## 10. End-to-end exercise

New script: `scripts/seed_dat_gtm_pipeline_foundation.py`. Runs against the dev DB + Anthropic + Trigger.dev (uses real test creds — no mocks for the Anthropic side).

Sequence:

1. Resolve the DAT initiative row (id `bbd9d9c3-c48e-4373-91f4-721775dca54e`).
2. Reset its `pipeline_status` to `'idle'` and any prior `gtm_subagent_runs` rows to `'superseded'`.
3. Verify the six MAGS agents are registered in `gtm_agent_registry`. If missing, exit non-zero with a clear "run setup_<slug>.py first" message.
4. Verify acq-eng `org_doctrine` row exists with non-empty `parameters`. Same fail-fast pattern.
5. Verify independent-brand doctrine row exists in `brand_content`. Same.
6. POST `/api/v1/admin/initiatives/{id}/start-pipeline` with `gating_mode='auto'`. Capture trigger_run_id.
7. Poll the runs endpoint every 3 seconds. Terminate when initiative `pipeline_status` reaches `'completed'` or `'failed'`, or after a 15-minute wall-clock timeout.
8. Always pretty-print the full run history (every actor + verdict row, in order, with cost_cents and output_blob summaries). Write a markdown summary to `docs/initiatives-archive/<initiative_id>/foundation_e2e_<timestamp>.md` containing: each run's slug / status / cost / parsed output / verdict ship-decision and issues blob if any.
9. Exit 0 if `pipeline_status='completed'`. Exit 0 *also* if `pipeline_status='failed'` — a verdict-blocked failure is a *successful* foundation smoke run; it proves the pipeline runs end-to-end and surfaces a real iteration target. Print a banner clearly distinguishing the two cases. Exit non-zero only on Anthropic API errors, network failures, missing prerequisites, or the wall-clock timeout.

This is the smoke gate. Run it before declaring the directive shipped:

```
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_dat_gtm_pipeline_foundation
```

Expected first-run outcome: with `MAX_VERDICT_RETRIES = 1` (no retry-with-hint), it's likely some verdict will reject its actor's draft on real DAT data and the pipeline will land in `pipeline_status='failed'`. That is *the desired outcome of a foundation smoke run* — the platform works, the loop runs, real outputs landed in the DB, and the operator now has a concrete failure to iterate against in the frontend prompt editor. The script's job is to surface that visibly, not to declare prompts good.

Retry-with-hint, configurable per-initiative `pipeline_max_retries`, and graduated retry budgets are explicitly out of scope here (see §11). Don't add them to make this script "pass."

---

## 11. Out of scope for this directive

Defer to follow-up directives:

- Subagents #2 (partner Exa research — for v0, `gtm-master-strategist` reads any existing partner research from the linked exa_research_jobs row directly), #3, #4, #5, #6, #8, #9, #10, #voice, #12a (adversary), #12b (DSL validator).
- Sub-squad critic split (just verdict for now; verdict carries critic-style reasoning inline).
- Retry-with-hint loop (verdict fail = step fail in v0; richer redo handling later).
- Cost tracking population (column exists, leave NULL until a follow-up directive wires it).
- Multi-org doctrine (acq-eng only; the table supports per-org but the frontend ships with acq-eng hardcoded).
- Supabase Realtime subscriptions for live updates (polling for v0).
- Voice-agent instantiation and routing manifest.
- Brand-factory automation (still pre-payment scope, separate directive).
- Removing the existing `app/services/strategy_synthesizer.py` (leave dormant; deletion in a separate directive after the new pipeline is validated).

---

## 12. Tests / acceptance

Pytest tests required (mirror existing per-router test layout):

- `tests/test_admin_agents_router.py` — activate, rollback, version history endpoints. Mock Anthropic client at the service layer; assert DB rows written correctly.
- `tests/test_admin_initiatives_router.py` — start-pipeline (mock Trigger.dev enqueue), runs list, runs drill, rerun.
- `tests/test_internal_gtm_pipeline.py` — `/internal/gtm/initiatives/{id}/run-step` end-to-end with the Anthropic client mocked. Verify the row goes from `running` → `succeeded` (or `failed` on Anthropic error) inside one request, that prompt snapshot is captured, that `superseded` marking on downstream rows fires before the new row inserts, and that the returned StepResult shape matches what Trigger.dev consumes.
- `tests/test_agent_prompts_service.py` — activate creates two version rows (snapshot + new). Rollback resolves prior version correctly. Both push to Anthropic (mocked).
- `tests/test_gtm_pipeline_service.py` — `kickoff_pipeline`, `run_step` (Anthropic mocked), `request_rerun` (downstream supersede behavior).
- `tests/test_org_doctrine.py` — CRUD + schema validation on parameters JSONB.

End-to-end test (network-dependent; gated by env flag `RUN_E2E_GTM=1`):

- `tests/test_dat_gtm_pipeline_e2e.py` — runs `seed_dat_gtm_pipeline_foundation` programmatically, asserts the final run rows. Skip on default `pytest -q`; opt-in via env.

Acceptance:

- `uv run pytest -q` baseline passes (917 + new test count).
- `RUN_E2E_GTM=1 uv run pytest tests/test_dat_gtm_pipeline_e2e.py -v` passes against dev DB + real Anthropic.
- Frontend: navigate to `/admin/initiatives/bbd9d9c3-c48e-4373-91f4-721775dca54e`, see all six runs render with input/output/prompt visible. Click "Rerun this step" on `gtm-master-strategist`, observe new run row with `run_index=2` and downstream `gtm-per-recipient-creative` run marked `superseded`. Edit `gtm-master-strategist`'s prompt at `/admin/agents/gtm-master-strategist`, hit Activate, see two new rows in version history (snapshot + new), confirm Anthropic side updated by re-running the step and observing the new prompt in the run row's `system_prompt_snapshot`.

---

## 13. Sequencing within the directive

Suggested order of work (each step independently testable):

1. Migrations 4.1 → 4.5 + run them locally.
2. `app/services/anthropic_managed_agents.py` + `app/services/agent_prompts.py` + their tests.
3. Doctrine docs + sync scripts + `app/services/org_doctrine.py` + tests.
4. `managed-agents-x/data/agents/<slug>/system_prompt.md` × 6 + `setup_<slug>.py` × 6. Run the setup scripts; capture agent_ids; INSERT registry rows. Commit the .md files + setup scripts to MAGS.
5. `app/services/gtm_pipeline.py` + `app/routers/internal/gtm_pipeline.py` + tests.
6. `app/routers/admin_initiatives.py` + `app/routers/admin_agents.py` + `app/routers/admin_doctrine.py` + tests.
7. `src/trigger/gtm-run-initiative-pipeline.ts` + Trigger.dev deploy.
8. Frontend pages — start with `/admin/agents/[slug]` (smallest surface, validates the prompt-versioning flow end-to-end), then initiatives list/drilldown, then doctrine.
9. `scripts/seed_dat_gtm_pipeline_foundation.py` + iterate until it surfaces a real first-run failure mode. Document the failure in the archived markdown summary.
10. Open the PR. Title: `feat(gtm): pipeline foundation — managed-agents runtime + run-capture + frontend command center`.

PR description must include: which prompts the operator should review and likely-edit-first via the frontend (almost certainly the per-recipient creative one, given V1 strategy synthesizer history).

---

## 14. Notes on what this enables

After this ships, the operator can:

1. Open `/admin/initiatives/<id>`, click Start Pipeline, watch six runs land with input/output/prompt visible.
2. See where the cascade failed (almost certainly in #11 verdict reject on first end-to-end pass).
3. Open `/admin/agents/gtm-per-recipient-creative`, edit the prompt, hit Activate.
4. Click "Rerun this step" on the relevant initiative's #11 run. New run uses the new prompt automatically.
5. Iterate until #11 ships clean.
6. Once #11 lands, prior steps' weaknesses become observable upstream (#7 framing was too thin; #1 misjudged touch count). Iterate those next.

The platform now exists. Each subsequent subagent registration is one `system_prompt.md` + one `setup_<slug>.py` + one entry in the Trigger.dev step list.
