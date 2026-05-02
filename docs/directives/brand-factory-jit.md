# Directive: JIT brand factory (target accounts → gestalt → fit-check → reuse-or-instantiate)

**Context:** You are working inside the `hq-x` repository. Read [CLAUDE.md](../../CLAUDE.md), [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md), and [docs/prompts/claygent-target-account-research.md](../prompts/claygent-target-account-research.md) before starting.

**Scope clarification on autonomy:** This directive bundles light research + a focused implementation. You have judgment over internal type shapes, prompt-template wording, and per-stage error packaging within the constraints below. You do NOT have judgment over:

- The storage shape for `brand.json`. Use a separate `business.brand_context_documents` table with row-per-version + a `canonical_brand_context_id` pointer on `business.brands`. (§9.4 of the strategic doc resolved as: separate table = versioning + diff for free + atomic mutation + audit trail.) Do not put the canonical doc on `business.brands` directly, do not use supabase storage objects.
- The trigger model. Brand instantiation is **just-in-time, not speculative**. The factory runs reactively when a caller (eventually the prospect-outreach orchestrator) needs a brand for an `(audience_spec_id, target_account_id)` pair. There is no "pre-stage N brands ahead of demand" mode in this directive. If you find yourself building one, stop.
- The dual-consumer contract. `target_accounts.research_blob` is consumed by **two** downstream agents: (1) brand factory (this directive), (2) audience-derivation agent (next directive). Do not couple the blob's shape to the brand factory specifically. The shape is documented in [docs/prompts/claygent-target-account-research.md](../prompts/claygent-target-account-research.md) and matches the Claygent prompt's output schema verbatim.
- The marketing-site, domain, voice-agent, Vercel, Entri, or Dub surfaces. None of those land in this directive. The directive ships the **brand-content layer** end-to-end: schema → storage → JIT factory → tests. Site bootstrap is the next directive.

**Background:** Per [strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md), each owned-brand initiative runs under a brand whose pairing-shape is keyed to `(audience_archetype, need_pain_framing, voice_register)`. Brands are reusable across many `(audience_spec, partner_contract)` instances when the pairing-shape fits. New brand only when no existing brand's pairing-shape covers the new pair.

The trigger for "find brand or create new" is the operational moment when a target account is being prepared for outreach. Inputs available at that moment: `audience_spec_id` (the spec the prospect would receive), `target_account_id` (the prospect company, with research already populated). Output needed: a `brand_id` (existing or new) plus the `decision` audit trail (`reused` vs `instantiated`).

The factory is three sequential LLM stages:

1. **Gestalt** — derive a `pairing_shape` descriptor from `(audience_spec_descriptor, target_account.research_blob)`. Output is canonical: `{audience_archetype, need_pain_framing, voice_register, implied_demand_pool_framing}`.
2. **Fit-check** — given the pairing-shape and the list of existing brands' pairing-shapes, decide: which existing brand fits, or "instantiate new." Returns `{decision: 'reuse'|'instantiate', brand_id?: UUID, reasoning: str}`.
3. **Instantiate** (only if fit-check returned `instantiate`) — produce the full `brand.json` from the pairing-shape, persist it as the canonical doc for a new `business.brands` row.

Capital Expansion is the **first concrete `brand.json`**, reverse-engineered from its live site (https://www.capitalexpansion.com). It serves as both the seed entry and the test fixture for fit-check decisions.

**Critical existing-state facts (verify before building):**

- `business.brands` already exists ([migrations/0002_brands.sql](../../migrations/0002_brands.sql), modified by [migrations/0020_organizations_tenancy.sql](../../migrations/0020_organizations_tenancy.sql)). Read its current schema before writing migrations. The new `canonical_brand_context_id` column is the only addition this directive makes to that table.
- The orchestration-job pattern is `business.activation_jobs` ([migrations/20260430T184850_activation_jobs.sql](../../migrations/20260430T184850_activation_jobs.sql)) and `business.exa_research_jobs` (added by the Exa directive). Mirror that pattern — same status enum, same JSONB payload/result/error/history, same trigger_run_id, same idempotency-key uniqueness.
- The Trigger.dev task pattern is [src/trigger/exa-process-research-job.ts](../../src/trigger/exa-process-research-job.ts) — read it end-to-end. The `brand-factory-job.ts` you write is its closest sibling.
- The internal-callback router pattern is [app/routers/internal/exa_jobs.py](../../app/routers/internal/exa_jobs.py) (or the dmaas equivalent). Mirror that for `/internal/brands/factory/jobs/{id}/process`.
- hq-x does **not** currently have a direct Anthropic Messages API client. It has a Managed Agents API client in `managed-agents-x/app/anthropic_client.py` — that is a different beta endpoint (agent-session-spawning), not what we need. The brand factory needs one-shot Messages API calls. You add a thin httpx wrapper in this directive (see §D5).
- The four reference projects Ben shared ([landing-page-factory](https://github.com/TheMattBerman/landing-page-factory), [claude-skills landing-page-generator](https://github.com/alirezarezvani/claude-skills), Anthropic frontend-design SKILL, [brand-system-from-website substack](https://aimaker.substack.com/p/claude-design-brand-system-skill-guide)) inform the `brand.json` schema in §A. They are research input. None of them get vendored or imported.

---

## Existing code to read before starting

In order:

1. [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md) — §3 (the independent brand), §5 Phase 4 step 4 (per-recipient creative), §7 Adds (per-recipient creative generator + brand bootstrap pipeline + strategic prompt-pack), §9.4 (storage decision — resolved as separate table here).
2. [docs/prompts/claygent-target-account-research.md](../prompts/claygent-target-account-research.md) — the Claygent prompt + the field guide. The JSON schema there IS the contract for `target_accounts.research_blob`.
3. [migrations/0002_brands.sql](../../migrations/0002_brands.sql) and the relevant ALTER blocks in [migrations/0020_organizations_tenancy.sql](../../migrations/0020_organizations_tenancy.sql) — current `business.brands` shape.
4. [migrations/20260430T184850_activation_jobs.sql](../../migrations/20260430T184850_activation_jobs.sql) — orchestration-job table precedent.
5. [app/services/activation_jobs.py](../../app/services/activation_jobs.py) — orchestration-job service helpers (`mark_running`, `mark_succeeded`, `mark_failed`, `_record_history`). Mirror this style.
6. [app/services/exa_research_jobs.py](../../app/services/exa_research_jobs.py) and [app/routers/exa_jobs.py](../../app/routers/exa_jobs.py) — the closest sibling. The Exa pattern is `POST /api/v1/exa/jobs` returning 202 + `job_id`, Trigger.dev task POSTs to internal callback, internal callback runs the work and persists. Brand factory mirrors this exactly.
7. [src/trigger/exa-process-research-job.ts](../../src/trigger/exa-process-research-job.ts) — TS Trigger task pattern.
8. [app/routers/internal/exa_jobs.py](../../app/routers/internal/exa_jobs.py) — internal callback pattern.
9. [app/auth/supabase_jwt.py](../../app/auth/supabase_jwt.py) — `UserContext` with `active_organization_id`. The factory job is org-scoped.
10. [app/config.py](../../app/config.py) — env-driven `Settings`. You add `ANTHROPIC_API_KEY`, `ANTHROPIC_API_BASE`, `ANTHROPIC_BRAND_FACTORY_MODEL_ANALYTICAL`, `ANTHROPIC_BRAND_FACTORY_MODEL_GENERATIVE`.
11. [managed-agents-x/app/anthropic_client.py](../../../../managed-agents-x/app/anthropic_client.py) — pattern reference for httpx-based Anthropic client. The brand factory's client mirrors the *style* (httpx, raw JSON, no SDK) but hits a different endpoint (Messages API, not Managed Agents API).
12. The four references: [landing-page-factory](https://github.com/TheMattBerman/landing-page-factory) (artifact-passing-between-stages pattern), [claude-skills landing-page-generator](https://github.com/alirezarezvani/claude-skills) (config.json schema for brand-context shape), Anthropic frontend-design skill (anti-slop constraint layer for downstream rendering), [brand-system-from-website substack](https://aimaker.substack.com/p/claude-design-brand-system-skill-guide) (canonical-file discipline). Synthesize in §A.

---

## Phase A — Research deliverable (notes file only, no code)

**File:** `docs/research/brand-context-schema-research.md` (new)

Spend 30–45 min reading the four references. Produce a notes file capturing:

1. **Canonical-file discipline (substack post)**: the `brand.json` single-source-of-truth pattern. One-paragraph summary + the 5–7 core fields the substack treats as load-bearing.
2. **Pipeline stage→artifact pattern (landing-page-factory)**: the 7 stages, what artifact each emits, how downstream stages consume upstream artifacts. We are not building this pipeline today — we are documenting it so the next directive (marketing-site bootstrap) builds on top of `brand.json` from this directive.
3. **Brand-context object shape (claude-skills landing-page-generator)**: the `config.json` schema. Identify which fields overlap with the substack pattern, which are additive, which are pipeline-stage-specific. Output: a merged-superset field list.
4. **Anti-slop constraints (Anthropic frontend-design SKILL)**: the constraint layer for downstream marketing-site rendering. Document which constraints are content-level (carry on `brand.json`) vs render-level (carry on the next directive's site-bootstrap surface). Only the content-level ones get fields here.
5. **`brand.json` canonical schema (this directive's contract)**: the actual schema we ship. Pydantic models in [§C1](#c1-brandjson-pydantic-schema) below — this section of the notes file is the *human-readable rationale* for each field's inclusion. Per field: name, type, why it's load-bearing, which downstream consumer uses it (creative generator / marketing site / voice-agent persona / brand-fit-check).

End with a **"Out of scope here, in scope for next directive"** section listing fields/concepts you encountered in the references that we'll need when site-bootstrap lands but aren't on `brand.json` today.

No code in Phase A. Notes file only.

---

## Phase B — Migrations

### B1. `business.target_accounts`

**File:** `migrations/<UTC_TIMESTAMP>_target_accounts.sql` (new)

```sql
CREATE TABLE business.target_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    -- Stable identifier for the prospect. Domain is the natural key; name is human-readable.
    domain TEXT NOT NULL,
    company_name TEXT NOT NULL,
    -- The Claygent / Exa research output. Schema documented at
    -- docs/prompts/claygent-target-account-research.md.
    research_blob JSONB NOT NULL,
    research_source TEXT NOT NULL CHECK (research_source IN ('claygent', 'exa', 'manual')),
    research_confidence TEXT NOT NULL CHECK (research_confidence IN ('high', 'medium', 'low')),
    notes TEXT,
    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, domain)
);

CREATE INDEX idx_target_accounts_org_created
    ON business.target_accounts (organization_id, created_at DESC);
CREATE INDEX idx_target_accounts_research_blob_gin
    ON business.target_accounts USING GIN (research_blob);
```

### B2. `business.brand_context_documents` + `canonical_brand_context_id` on `business.brands`

**File:** `migrations/<UTC_TIMESTAMP>_brand_context_documents.sql` (new)

```sql
CREATE TABLE business.brand_context_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE CASCADE,
    -- 1-indexed monotonic version per brand_id.
    version INTEGER NOT NULL,
    -- The full brand.json doc (canonical schema in app/services/brand_context_schema.py).
    document JSONB NOT NULL,
    -- 'instantiated' = factory just created this; 'edited' = human-curated; 'imported' = seed.
    authored_by TEXT NOT NULL CHECK (authored_by IN ('instantiated', 'edited', 'imported')),
    authored_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    -- For 'instantiated' rows: the factory job id that produced this doc.
    source_factory_job_id UUID,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (brand_id, version)
);

CREATE INDEX idx_bcd_brand_version
    ON business.brand_context_documents (brand_id, version DESC);
CREATE INDEX idx_bcd_document_gin
    ON business.brand_context_documents USING GIN (document);

ALTER TABLE business.brands
    ADD COLUMN canonical_brand_context_id UUID
    REFERENCES business.brand_context_documents(id) ON DELETE SET NULL;

CREATE INDEX idx_brands_canonical_context
    ON business.brands (canonical_brand_context_id)
    WHERE canonical_brand_context_id IS NOT NULL;
```

The `canonical_brand_context_id` pointer is the read-side fast path: downstream consumers (creative generator, voice-agent persona, marketing-site renderer) join `business.brands → business.brand_context_documents` once instead of doing per-version max queries.

### B3. `business.brand_factory_jobs`

**File:** `migrations/<UTC_TIMESTAMP>_brand_factory_jobs.sql` (new)

```sql
CREATE TABLE business.brand_factory_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    -- Inputs.
    audience_spec_id UUID NOT NULL,
    target_account_id UUID NOT NULL REFERENCES business.target_accounts(id) ON DELETE RESTRICT,
    -- Stages run sequentially; each stage's output is persisted in `stage_outputs` JSONB
    -- keyed by stage name ('gestalt', 'fit_check', 'instantiate').
    stage_outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Decision + result.
    decision TEXT CHECK (decision IN ('reuse', 'instantiate')),
    result_brand_id UUID REFERENCES business.brands(id) ON DELETE SET NULL,
    -- Standard orchestration fields.
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead_lettered'
    )),
    error JSONB,
    history JSONB NOT NULL DEFAULT '[]'::jsonb,
    trigger_run_id TEXT,
    idempotency_key TEXT,
    attempts INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_bfj_org_idempotency
    ON business.brand_factory_jobs (organization_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_bfj_status ON business.brand_factory_jobs (status);
CREATE INDEX idx_bfj_org_created
    ON business.brand_factory_jobs (organization_id, created_at DESC);
CREATE INDEX idx_bfj_target_account
    ON business.brand_factory_jobs (target_account_id);
CREATE INDEX idx_bfj_audience_spec
    ON business.brand_factory_jobs (audience_spec_id);
```

The `audience_spec_id` is intentionally not FK'd to a local table — DEX owns the audience spec. The hq-x reservation surface (`business.org_audience_reservations`) caches its descriptor; the factory uses that cache.

---

## Phase C — Schemas

### C1. `brand.json` Pydantic schema

**File:** `app/services/brand_context_schema.py` (new)

The canonical `brand.json` shape. Strict validation at write time. Synthesized from the four references per §A's notes.

Structure (use Pydantic v2 `BaseModel` with `model_config = {"extra": "forbid"}`):

```python
class PairingShape(BaseModel):
    """The brand's keying. This is what fit-check compares against."""
    audience_archetype: str            # who the audience is (1-2 sentences)
    need_pain_framing: str             # what pain they have, why this voice resonates (2-3 sentences)
    implied_demand_pool_framing: str   # who the buyer pool is (derived, not enumerated)
    firmographic_vertical: str         # e.g. "transportation/freight"
    core_need_category: str            # e.g. "factoring/working-capital"
    voice_register: Literal[
        "operator-to-operator",
        "institutional",
        "empathetic/disclosure-forward",
        "transactional",
        "premium/concierge",
        "self-serve/SaaS",
    ]


class BrandPositioning(BaseModel):
    """How the brand presents to the audience. Drives marketing-site copy + creative voice."""
    one_line_promise: str              # the hero headline-class promise
    elevator_pitch: str                # 2-3 sentence positioning
    proof_points: list[str]            # 3-7 short claims that back the promise
    differentiators: list[str]         # 2-5 reasons-to-believe over alternatives
    audience_objections_handled: list[str]  # what skepticism the audience walks in with


class BrandVoice(BaseModel):
    """Tone of voice rules. Drives per-recipient creative generator's copy stage."""
    register: str                      # echoes pairing_shape.voice_register but allows refinement
    sentence_rhythm: Literal["short-punchy", "measured", "long-form-narrative", "mixed"]
    vocabulary_level: Literal["plainspoken", "industry-fluent", "technical-precise"]
    do_use: list[str]                  # words/phrases this brand uses
    dont_use: list[str]                # words/phrases this brand avoids
    example_sentences: list[str]       # 3-5 reference sentences in-voice


class VisualIdentity(BaseModel):
    """Site/creative visual rules. Marketing-site renderer consumes."""
    primary_color_hex: str
    accent_color_hex: str
    background_treatment: Literal["dark-mode", "light-mode", "muted-neutral"]
    typography_serif_signal: bool      # true = serif somewhere (e.g. CapitalExpansion's italic 'capital')
    iconography_register: Literal["geometric-line", "rounded-soft", "industrial-stamp", "no-icons"]
    photography_register: Literal["product-led", "operator-portraits", "data-led-no-photography", "abstract-visual"]


class BrandContent(BaseModel):
    """Reusable content pulled into marketing-site sections + email/creative."""
    hero_headline: str
    hero_subheadline: str
    primary_cta_label: str
    secondary_cta_label: str | None = None
    problem_section_title: str
    problem_section_body: str
    what_we_do_section_title: str
    how_it_works_steps: list[dict[str, str]]   # each: {title, body}
    trust_band_text: str | None = None         # e.g. "Trusted by operators across…"
    for_partners_section_body: str | None = None


class BrandContext(BaseModel):
    """The full brand.json doc. Canonical."""
    schema_version: Literal["1"] = "1"
    brand_slug: str                    # url-safe; e.g. 'capital-expansion'
    display_name: str                  # e.g. 'Capital Expansion'
    domain: str                        # e.g. 'capitalexpansion.com'
    pairing_shape: PairingShape
    positioning: BrandPositioning
    voice: BrandVoice
    visual: VisualIdentity
    content: BrandContent
    # Free-form notes for human curation. Not consumed structurally.
    curation_notes: str | None = None
    model_config = {"extra": "forbid"}
```

Add a `validate_brand_context(payload: dict) -> BrandContext` helper that wraps `BrandContext.model_validate` and raises a typed error on failure. Storage layer calls this before insert/update.

### C2. `target_account.research_blob` Pydantic schema

**File:** `app/services/target_account_schema.py` (new)

Mirror the JSON schema at the bottom of [docs/prompts/claygent-target-account-research.md](../prompts/claygent-target-account-research.md) verbatim as Pydantic v2 models. `extra="forbid"` at the top level so unknown fields fail loudly. The blob is dual-consumer (this directive's brand factory; the next directive's audience-derivation agent), so the schema lives in its own module — neither consumer owns it.

Provide a `validate_research_blob(payload: dict) -> TargetAccountResearch` helper.

---

## Phase D — Services

### D1. `app/services/target_accounts.py` (new)

CRUD over `business.target_accounts`. Functions:

```python
async def create_target_account(
    *,
    organization_id: UUID,
    created_by_user_id: UUID | None,
    domain: str,
    company_name: str,
    research_blob: dict[str, Any],
    research_source: Literal["claygent", "exa", "manual"],
    notes: str | None,
) -> dict[str, Any]: ...

async def get_target_account(
    target_account_id: UUID,
    *,
    organization_id: UUID,
) -> dict[str, Any] | None: ...

async def list_target_accounts(
    *,
    organization_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]: ...
```

Validation: `create_target_account` must validate `research_blob` against `target_account_schema.validate_research_blob` and pull `research_confidence` from the blob (don't re-prompt the caller). UPSERT on `(organization_id, domain)` — re-running with newer research updates the blob and bumps `updated_at`.

### D2. `app/services/brand_contexts.py` (new)

CRUD over `business.brands` + `business.brand_context_documents`. Functions:

```python
async def create_brand_with_context(
    *,
    organization_id: UUID,
    created_by_user_id: UUID | None,
    brand_context: BrandContext,        # validated Pydantic model
    authored_by: Literal["instantiated", "edited", "imported"],
    source_factory_job_id: UUID | None,
    notes: str | None,
) -> dict[str, Any]:
    """Create new business.brands row + version-1 brand_context_documents row +
    set canonical_brand_context_id pointer. Returns {brand_id, document_id, version}.
    All in one transaction."""

async def append_brand_context_version(
    *,
    brand_id: UUID,
    organization_id: UUID,
    brand_context: BrandContext,
    authored_by: Literal["instantiated", "edited", "imported"],
    authored_by_user_id: UUID | None,
    source_factory_job_id: UUID | None,
    notes: str | None,
) -> dict[str, Any]:
    """Insert a new row with version = max(version)+1, update canonical pointer
    on business.brands. Returns {document_id, version}. Single transaction."""

async def get_canonical_brand_context(
    brand_id: UUID,
    *,
    organization_id: UUID,
) -> dict[str, Any] | None:
    """JOIN business.brands → business.brand_context_documents on canonical_brand_context_id.
    Returns {brand: ..., document: BrandContext-shaped dict, version}."""

async def list_brands_with_canonical_context(
    *,
    organization_id: UUID,
) -> list[dict[str, Any]]:
    """Used by fit-check to enumerate candidates. Returns list of
    {brand_id, brand_slug, display_name, pairing_shape, version}.
    Note: only pairing_shape projection — the full doc is not needed for fit-check."""
```

### D3. `app/services/brand_factory_jobs.py` (new)

Mirror [app/services/exa_research_jobs.py](../../app/services/exa_research_jobs.py) closely. Functions:

```python
async def create_job(
    *,
    organization_id: UUID,
    created_by_user_id: UUID | None,
    audience_spec_id: UUID,
    target_account_id: UUID,
    idempotency_key: str | None,
) -> dict[str, Any]: ...

async def get_job(job_id: UUID, *, organization_id: UUID) -> dict[str, Any] | None: ...

async def mark_running(job_id: UUID, trigger_run_id: str) -> None: ...
async def mark_succeeded(
    job_id: UUID,
    *,
    decision: Literal["reuse", "instantiate"],
    result_brand_id: UUID,
    stage_outputs: dict[str, Any],
) -> None: ...
async def mark_failed(job_id: UUID, error: dict[str, Any]) -> None: ...
async def append_history(job_id: UUID, event: dict[str, Any]) -> None: ...
async def write_stage_output(job_id: UUID, *, stage: str, output: dict[str, Any]) -> None: ...
```

Idempotency: `(organization_id, idempotency_key)` returns the existing row when set. Same semantics as exa_research_jobs.

### D4. `app/services/brand_factory.py` (new) — the three primitives

This is the core LLM-driven module.

```python
async def derive_pairing_shape(
    *,
    audience_spec_descriptor: dict[str, Any],
    target_account_research: TargetAccountResearch,
) -> dict[str, Any]:
    """Stage 1. LLM call. Output validated against PairingShape schema before return."""

async def fit_check_against_existing(
    *,
    pairing_shape: dict[str, Any],
    candidate_brands: list[dict[str, Any]],   # output of list_brands_with_canonical_context
) -> dict[str, Any]:
    """Stage 2. LLM call. Returns {decision: 'reuse'|'instantiate', brand_id?: UUID, reasoning: str}.
    The LLM is given each candidate's pairing_shape and the new pairing_shape; it scores
    fit and returns the best match if any exceeds threshold, else 'instantiate'."""

async def instantiate_brand_context(
    *,
    pairing_shape: dict[str, Any],
    target_account_research: TargetAccountResearch,
    audience_spec_descriptor: dict[str, Any],
) -> BrandContext:
    """Stage 3. LLM call. Returns a fully-populated BrandContext. Validated against
    the schema before return. Includes brand_slug derivation (LLM proposes, code
    sanitizes to url-safe)."""
```

Each primitive:
- Uses one `messages.create` call against the Anthropic API via `app.services.anthropic_client` (§D5).
- Has a versioned prompt-template constant declared at the top of the module: `_GESTALT_PROMPT_V1`, `_FIT_CHECK_PROMPT_V1`, `_INSTANTIATE_PROMPT_V1`. Each is a multi-line `f`-string-with-no-substitutions (the substitution happens at call time via `.format(...)` or jinja-style; pick one and use it consistently). Versioning is explicit so future tuning is trackable.
- Emits structured JSON via the API's tool-use / structured-output mechanism. **Do not parse free-text** — use Anthropic's native JSON-mode (system prompt asserting JSON-only output + parse strictly). On parse failure, raise a typed error; do not retry inside the primitive (Trigger task handles retry).
- Logs the full request/response for audit (write to `brand_factory_jobs.stage_outputs[stage_name]`).

**Models:**
- Stages 1 (gestalt) and 2 (fit-check): use the **analytical** model (Sonnet 4.6 — `claude-sonnet-4-6`). These are comparison/synthesis tasks where speed matters more than creative voice.
- Stage 3 (instantiation): use the **generative** model (Opus 4.7 — `claude-opus-4-7`). The output is voice-laden brand content that benefits from the more capable model. Cost is justified — this runs once per new brand, then is reused across many recipients.

Both models read from `settings.ANTHROPIC_BRAND_FACTORY_MODEL_ANALYTICAL` and `settings.ANTHROPIC_BRAND_FACTORY_MODEL_GENERATIVE` respectively (so they're swappable without code changes).

### D5. `app/services/anthropic_client.py` (new) — thin Messages API client

Mirror the *style* of [managed-agents-x/app/anthropic_client.py](../../../../managed-agents-x/app/anthropic_client.py) (httpx, raw JSON, no SDK, exact response stored verbatim) but for the Messages API:

```python
async def messages_create(
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """POST https://api.anthropic.com/v1/messages. Returns the parsed JSON response.
    Reads settings.ANTHROPIC_API_KEY. Raises AnthropicNotConfiguredError if unset.
    Raises AnthropicCallError(status, body, model) on non-2xx."""
```

Headers: `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`. No beta header for Messages API. Use `httpx.AsyncClient` per call (or a module-level singleton — either's fine).

Add to [app/config.py](../../app/config.py):

```python
ANTHROPIC_API_KEY: SecretStr | None = None
ANTHROPIC_API_BASE: str = "https://api.anthropic.com"
ANTHROPIC_BRAND_FACTORY_MODEL_ANALYTICAL: str = "claude-sonnet-4-6"
ANTHROPIC_BRAND_FACTORY_MODEL_GENERATIVE: str = "claude-opus-4-7"
```

Place near the other provider keys.

---

## Phase E — API surface

### E1. `app/routers/brands.py` (new), prefix `/api/v1/brands`

Auth: `verify_supabase_jwt` (org-scoped).

Two endpoints in this directive — the factory job and listing brands. (Full brand-management CRUD lands when the operator UI lands.)

```python
class CreateBrandFactoryJobRequest(BaseModel):
    audience_spec_id: UUID
    target_account_id: UUID
    idempotency_key: str | None = None
    model_config = {"extra": "forbid"}
```

`POST /api/v1/brands/factory` — create a factory job:
1. Resolve `org_id = user.active_organization_id`. 400 `organization_required` if None.
2. Verify the `target_account_id` belongs to `org_id`. 404 if not.
3. Verify the `audience_spec_id` resolves via the existing reservation/DEX lookup. 404 if not.
4. Call `brand_factory_jobs.create_job(...)`. If idempotent hit, return existing row with 200; else 202.
5. Enqueue Trigger.dev task `brand-factory.process-job` with `{job_id}`. Reuse the Trigger client helper used by `dmaas_campaigns.py` / `exa_jobs.py`.
6. Response: `{"job_id": "...", "status": "queued"}`.

`GET /api/v1/brands/factory/jobs/{job_id}` — read job status. 404 if not found OR cross-org. Returns full row including `decision`, `result_brand_id`, `stage_outputs`.

`GET /api/v1/brands` — list brands for the org. Returns `[{brand_id, brand_slug, display_name, domain, pairing_shape, latest_version, created_at}]` (no full canonical doc — keep response light).

`GET /api/v1/brands/{brand_id}` — read one brand. Returns the full canonical doc.

Wire into [app/main.py](../../app/main.py) — one import + one `include_router` line.

### E2. `app/routers/internal/brands.py` (new), prefix `/internal/brands`

Auth: shared-secret bearer (reuse the dependency from `app/routers/internal/exa_jobs.py`).

`POST /internal/brands/factory/jobs/{job_id}/process`:

1. Load job. If `status != 'queued'`, return early (idempotent re-entry).
2. `mark_running(job_id, trigger_run_id_from_body)`.
3. Load `target_account.research_blob`, validate via `target_account_schema.validate_research_blob`. Load `audience_spec_descriptor` from the cached reservation (or fetch via `dex_client.get_audience_descriptor` if the cache row is stale; copy whichever pattern is established).
4. **Stage 1: gestalt.** Call `brand_factory.derive_pairing_shape(...)`. Persist via `brand_factory_jobs.write_stage_output(stage='gestalt', output={...})`. On failure, `mark_failed` and return.
5. **Stage 2: fit-check.** Call `brand_factory.list_brands_with_canonical_context(org_id)` → `brand_factory.fit_check_against_existing(...)`. Persist stage output. On failure, `mark_failed` and return.
6. **Branch on decision:**
   - `reuse`: `mark_succeeded(decision='reuse', result_brand_id=fit_check.brand_id, stage_outputs={...})`.
   - `instantiate`: proceed to stage 3.
7. **Stage 3: instantiate.** Call `brand_factory.instantiate_brand_context(...)`. Validate the returned `BrandContext`. Persist stage output.
8. Write the new brand: `brand_contexts.create_brand_with_context(brand_context=..., authored_by='instantiated', source_factory_job_id=job_id)`. Returns `brand_id`.
9. `mark_succeeded(decision='instantiate', result_brand_id=brand_id, stage_outputs={...})`.
10. Return `{"status": "succeeded", "decision": "...", "result_brand_id": "..."}`.

On any unexpected exception, `mark_failed` with the captured traceback in `error.detail`.

Wire into [app/main.py](../../app/main.py) — one import + one `include_router` line under the existing `/internal` block.

---

## Phase F — Trigger.dev task

**File:** `src/trigger/brand-factory-job.ts` (new)

Mirror [src/trigger/exa-process-research-job.ts](../../src/trigger/exa-process-research-job.ts):

- Task id: `brand-factory.process_job`.
- Input: `{ jobId: string }`.
- POSTs to `/internal/brands/factory/jobs/{jobId}/process` with the shared-secret bearer.
- Retries: same retry config as exa-process-research-job. Internal endpoint is idempotent on non-queued status.
- Logs `decision` and `result_brand_id` on success; `error.code` and `error.detail` on failure.

Register the task wherever the existing tasks are enumerated (`src/trigger/trigger.config.ts` or equivalent — match the existing pattern).

---

## Phase G — Capital Expansion seed

**File:** `data/brand_seeds/capital_expansion.json` (new)

Hand-author the canonical `BrandContext` for Capital Expansion based on the live site (https://www.capitalexpansion.com). Use the screenshots Ben shared as reference if the live site is unreachable. Validate against `BrandContext` before committing — if validation fails, fix the seed, not the schema.

Pairing-shape (anchor values; LLM-derived later may refine):
- `audience_archetype`: business operators across trucking, manufacturing, staffing, healthcare, government contracting who need capital.
- `need_pain_framing`: hundreds of fragmented capital providers (factors, SBA, ABL, equipment finance, RBF, specialty); operators have no efficient way to know which one fits their exact situation; they end up cold-calling, getting declined for mismatched criteria, settling for the first yes.
- `implied_demand_pool_framing`: any capital provider whose underwriting box covers a specific operator situation (factors for receivables-heavy, equipment finance for asset-heavy, SBA for established credit, RBF for revenue-stable SaaS, etc.).
- `firmographic_vertical`: `multi-vertical-operators`.
- `core_need_category`: `capital`.
- `voice_register`: `operator-to-operator`.

Hero / content fields read off the screenshots:
- `hero_headline`: "The right capital finds the right operator."
- `hero_subheadline`: "We connect business operators with the capital partners who fund situations like theirs. From factoring to SBA to equipment finance — we know who funds what."
- `primary_cta_label`: "Get matched"
- `secondary_cta_label`: "How it works"
- `problem_section_title`: "Capital is fragmented."
- `how_it_works_steps`: three steps from the live site (Tell us your situation / We make the match / Warm introduction).
- `trust_band_text`: "Trusted by operators across trucking, manufacturing, staffing, healthcare, and government contracting."

Visual identity (from screenshots):
- `primary_color_hex`: dark-mode background (off-black).
- `accent_color_hex`: green (the CTA + italic 'capital').
- `background_treatment`: `dark-mode`.
- `typography_serif_signal`: `true` (italic serif on 'capital').
- `iconography_register`: `geometric-line`.
- `photography_register`: `data-led-no-photography`.

Voice guidance: `do_use` includes "operator", "match", "fund", "situation"; `dont_use` includes generic-business-website filler ("solutions", "leverage synergies", "world-class"). 3-5 example sentences.

The seed is loaded by the seed script (§I) and inserted as a `business.brands` row + version-1 `brand_context_documents` row with `authored_by='imported'`. Keep the JSON file pretty-printed and human-editable.

---

## Phase H — Tests

**File:** `tests/test_brand_context_schema.py` (new)

1. `test_brand_context_validates_capital_expansion_seed` — load `data/brand_seeds/capital_expansion.json`, assert `BrandContext.model_validate(...)` succeeds. Round-trip: dump and re-validate.
2. `test_brand_context_rejects_unknown_voice_register` — assert validation fails with a value not in the Literal.
3. `test_brand_context_rejects_extra_fields` — extra=forbid.
4. `test_brand_context_rejects_missing_required_subdoc` — drop `pairing_shape`, expect failure with a clear path.
5. `test_target_account_research_validates_field_guide_example` — paste a minimal-but-valid example matching the Claygent prompt schema (one ICP attribute, one pain signal, etc.); assert validation succeeds.

**File:** `tests/test_brand_factory.py` (new)

Mock `app.services.anthropic_client.messages_create` for every test (LLM calls do not hit the real API).

1. `test_derive_pairing_shape_returns_validated_pairing_shape` — mock returns a stub gestalt JSON; assert the primitive returns a dict that round-trips through `PairingShape.model_validate`.
2. `test_derive_pairing_shape_raises_on_malformed_json` — mock returns non-JSON; assert typed parse error.
3. `test_fit_check_returns_reuse_when_threshold_exceeded` — mock returns `{"decision":"reuse","brand_id":"...","reasoning":"..."}`; assert primitive returns the same shape; the chosen `brand_id` is one from the candidate list.
4. `test_fit_check_returns_instantiate_when_no_candidate_fits` — mock returns `{"decision":"instantiate","reasoning":"..."}`; assert `brand_id` is None on the result.
5. `test_fit_check_rejects_unknown_brand_id` — mock returns a `brand_id` not in the candidate list; primitive raises (LLM hallucination guard).
6. `test_instantiate_returns_validated_brand_context` — mock returns a full BrandContext-shaped JSON; assert `BrandContext.model_validate` succeeds on the return.
7. `test_instantiate_raises_on_invalid_brand_slug` — mock returns a slug with whitespace; primitive sanitizes (or raises — pick one and lock it).

**File:** `tests/test_brand_factory_router.py` (new)

1. `test_create_factory_job_returns_202_and_enqueues_task` — mock Trigger client, assert POST returns 202, row exists, Trigger called with `{jobId}`.
2. `test_create_factory_job_idempotent_returns_existing` — same idempotency_key returns same job_id, no duplicate Trigger enqueue.
3. `test_create_factory_job_rejects_target_account_in_other_org` — 404.
4. `test_create_factory_job_no_org_returns_400`.
5. `test_get_job_cross_org_returns_404`.
6. `test_internal_process_runs_three_stages_on_instantiate_path` — mock all three primitives (gestalt → fit_check returns instantiate → instantiate returns BrandContext); assert the new `business.brands` row exists, version-1 `brand_context_documents` row exists, `result_brand_id` populated, all three stage outputs persisted.
7. `test_internal_process_short_circuits_on_reuse_path` — mock fit_check returns `reuse` with an existing brand_id; assert no instantiate stage run, `result_brand_id == fit_check.brand_id`.
8. `test_internal_process_marks_failed_on_stage_failure` — mock derive_pairing_shape raises; job ends `status='failed'`, error captured, no later stages run.
9. `test_list_brands_returns_pairing_shape_projection` — seed two brands; assert the list endpoint returns pairing_shape but not the full canonical doc.
10. `test_get_brand_returns_full_canonical_doc` — assert `GET /api/v1/brands/{brand_id}` returns the full BrandContext.

Use pytest-asyncio + the existing DB / mocked-HTTP test fixtures. Parametrize where bodies are stage-agnostic.

---

## Phase I — Seed/exercise script

**File:** `scripts/seed_brand_factory_demo.py` (new)

End-to-end smoke test. Reads from Doppler at runtime. **Exercises both paths: reuse (for an audience-spec that fits Capital Expansion) and instantiate (for one that doesn't).**

1. Connect to hq-x DB. Look up or create test org `slug='brand-factory-demo'`.
2. Insert Capital Expansion seed via `brand_contexts.create_brand_with_context(authored_by='imported', ...)`. Capture the `brand_id`.
3. Insert a `target_accounts` row for DAT (`domain='dat.com'`) — load the Claygent JSON output Ben provides from `data/target_account_seeds/dat_com.json` (this file may not exist when the directive runs; if absent, fail with a clear message instructing where to drop the file).
4. Resolve or stub a test `audience_spec_id` for "FMCSA carriers, 5-50 power units, MC# active >12 months, US-wide" (use the existing reservation/dex test fixtures; if none exists, the script creates a minimal stub descriptor).
5. **Reuse path**: Build a synthetic target_account whose research_blob describes a generic-capital prospect (e.g. SBA-focused lender). Insert as a second `target_accounts` row. Fire a factory job for `(audience_spec_id, this_target_account_id)`. Poll until terminal. Assert `decision == 'reuse'`, `result_brand_id == capital_expansion_brand_id`.
6. **Instantiate path**: Fire a factory job for `(audience_spec_id, dat_target_account_id)`. Poll until terminal. Assert `decision == 'instantiate'`, `result_brand_id != capital_expansion_brand_id`. Print the new `BrandContext` — pairing-shape and hero headline at minimum.
7. Print the resulting `business.brands` rows + `brand_context_documents` versions for both brands.
8. Exit 0 only when both paths produce the expected decision; non-zero otherwise with a descriptive message.

Run via:

```bash
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_brand_factory_demo
```

The seed script is the smoke gate — both reuse and instantiate paths must go green for the directive to be done.

---

## Phase J — Documentation

Append to [CLAUDE.md](../../CLAUDE.md) under a new section "JIT brand factory":

- One-paragraph description of what the factory does (gestalt → fit-check → reuse-or-instantiate from `(audience_spec_id, target_account_id)`).
- The Doppler command for the seed script.
- A pointer to [docs/prompts/claygent-target-account-research.md](../prompts/claygent-target-account-research.md) (the Claygent prompt for populating `target_accounts.research_blob`).
- A pointer to [docs/research/brand-context-schema-research.md](../research/brand-context-schema-research.md) (the schema rationale + the four references it synthesizes).
- A pointer to `data/brand_seeds/capital_expansion.json` as the canonical reference for hand-authored brand contexts.

---

## What NOT to do

- Do **not** build any pre-staging / speculative-instantiation mode. Brand creation is JIT, triggered by `(audience_spec_id, target_account_id)` pairs.
- Do **not** put `brand.json` content directly on `business.brands`. Use `business.brand_context_documents` with row-per-version. Use the `canonical_brand_context_id` pointer for read access.
- Do **not** build the audience-derivation agent or the `target_accounts → audience-spec` slicer. That is the next directive. This one only consumes `audience_spec_id` (already-resolved); it does not derive specs.
- Do **not** build marketing-site rendering, domain registration, Vercel/Entri/Dub provisioning, voice-agent persona generation, or any brand-bootstrap-pipeline orchestrator. Site bootstrap is a sibling directive that consumes `brand.json` from this one.
- Do **not** modify the existing Lob, DMaaS, Exa, reservations, or dub surfaces.
- Do **not** introduce a `BrandProvider` or analogous abstraction layer. There is one provider (Anthropic API) and one factory.
- Do **not** retry inside the LLM primitives. Trigger task handles retry; per-call retry inside the primitive masks transient failures.
- Do **not** parse free-text LLM output. Use Anthropic's structured-output / JSON-mode mechanism. On parse failure, raise a typed error, not a string.
- Do **not** call the Managed Agents API. The brand factory uses the Messages API directly. The Managed Agents client in `managed-agents-x/` is a different surface.
- Do **not** vendor or import any of the four reference projects' code. They are research input, not dependencies.
- Do **not** persist secrets or PII into `target_accounts.research_blob` beyond what Claygent / Exa already returns (public-source research only).
- Do **not** add a new auth pattern. JWT for public, shared-secret for internal — copy the exa pattern.
- Do **not** cache LLM responses across jobs. Each factory job's stage outputs are scoped to that job.
- Do **not** commit the DAT target_account JSON if it contains anything beyond the Claygent prompt's schema. The seed file at `data/target_account_seeds/dat_com.json` is gitignored if it's specific to a real prospect; check `.gitignore` and add an entry if needed.

---

## Scope

Files to create or modify:

- `docs/research/brand-context-schema-research.md` (new)
- `migrations/<UTC_TIMESTAMP>_target_accounts.sql` (new)
- `migrations/<UTC_TIMESTAMP>_brand_context_documents.sql` (new — also adds the `canonical_brand_context_id` column on `business.brands`)
- `migrations/<UTC_TIMESTAMP>_brand_factory_jobs.sql` (new)
- `app/services/brand_context_schema.py` (new)
- `app/services/target_account_schema.py` (new)
- `app/services/target_accounts.py` (new)
- `app/services/brand_contexts.py` (new)
- `app/services/brand_factory_jobs.py` (new)
- `app/services/brand_factory.py` (new)
- `app/services/anthropic_client.py` (new)
- `app/config.py` (modify — add `ANTHROPIC_API_KEY`, `ANTHROPIC_API_BASE`, `ANTHROPIC_BRAND_FACTORY_MODEL_ANALYTICAL`, `ANTHROPIC_BRAND_FACTORY_MODEL_GENERATIVE`)
- `app/routers/brands.py` (new)
- `app/routers/internal/brands.py` (new)
- `app/main.py` (modify — 4 lines: 2 imports + 2 include_router)
- `src/trigger/brand-factory-job.ts` (new)
- `src/trigger/trigger.config.ts` (modify if task registration is needed there — match existing pattern)
- `data/brand_seeds/capital_expansion.json` (new)
- `tests/test_brand_context_schema.py` (new)
- `tests/test_brand_factory.py` (new)
- `tests/test_brand_factory_router.py` (new)
- `scripts/seed_brand_factory_demo.py` (new)
- `CLAUDE.md` (modify — append "JIT brand factory" section)
- `.gitignore` (modify — add `data/target_account_seeds/` if real-prospect data lives there)

**One commit. Do not push.**

Commit message:

> feat(brand-factory): JIT brand factory — gestalt → fit-check → reuse-or-instantiate
>
> Add `app/services/brand_factory.py` with three sequential LLM primitives (gestalt
> derivation, fit-check against existing brands, brand_context instantiation).
> Async-202 surface at `POST /api/v1/brands/factory` takes `(audience_spec_id,
> target_account_id)` and returns a job_id; Trigger.dev task drives the work
> end-to-end, returns `(decision, result_brand_id)`. Decision is `reuse` when an
> existing brand's pairing-shape covers the new pair, else `instantiate`.
>
> New tables: `business.target_accounts` (Claygent/Exa research blob, dual-consumer
> with the next directive's audience-derivation agent), `business.brand_context_documents`
> (row-per-version canonical brand.json), `business.brand_factory_jobs` (orchestration
> mirror of activation_jobs / exa_research_jobs). `business.brands` gains a
> `canonical_brand_context_id` pointer for the read-side fast path.
>
> Capital Expansion is seeded as the first concrete brand context, hand-authored
> from the live site. Anthropic Messages API is reached via a new thin httpx
> client (no SDK; mirrors the managed-agents-x style). Sonnet 4.6 for analytical
> stages, Opus 4.7 for generative instantiation — both env-configurable.
>
> Includes brand-context schema research notes synthesizing the four reference
> projects (landing-page-factory, claude-skills landing-page-generator, Anthropic
> frontend-design SKILL, brand-system-from-website substack post). The audience-
> derivation agent and the marketing-site bootstrap pipeline are sibling
> directives that consume from this one.

---

## When done

Report back with:

(a) Path to `docs/research/brand-context-schema-research.md` and a 4-bullet summary: which fields the substack pattern contributed, which the landing-page-factory pipeline contributed, which the claude-skills generator contributed, which the frontend-design SKILL contributed.

(b) `uv run pytest tests/test_brand_context_schema.py tests/test_brand_factory.py tests/test_brand_factory_router.py -v` — pass count + total time.

(c) Output of running the seed script end-to-end: the Capital Expansion seed insertion, both factory job results (reuse path + instantiate path), the resulting `business.brands` rows + `brand_context_documents` versions, and total runtime. Include the new brand's hero headline and pairing-shape in the printed output so the LLM-generated quality is visible at a glance.

(d) The three migration filenames you generated and the order you applied them.

(e) Confirmation that `data/target_account_seeds/dat_com.json` either (i) was provided by Ben before the run and contained valid Claygent-schema JSON, or (ii) was absent and the seed script printed the expected "drop the file at this path" message.

(f) The single commit SHA. Do not push.
