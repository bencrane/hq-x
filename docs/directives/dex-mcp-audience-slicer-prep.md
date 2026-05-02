# Directive: dex-mcp expansion + DEX overview document + audience-slice output schema

**Repo:** This directive runs against `data-engine-x` (NOT `hq-x`). All paths in this document are relative to the `data-engine-x` repo root unless otherwise noted.

**Context:** Read [app/mcp_server/README.md](../../../../data-engine-x/app/mcp_server/README.md) and [app/mcp_server/dex_server.py](../../../../data-engine-x/app/mcp_server/dex_server.py) before starting. Read the parent directive's framing in [hq-x/docs/directives/brand-factory-jit.md](../../docs/directives/brand-factory-jit.md) **for context only** — that directive lives in the hq-x repo and is not in scope here.

**Scope clarification on autonomy:** You have judgment over per-tool docstring wording and the exact organization of the overview document within the constraints below. You do NOT have judgment over:
- Which endpoints to wrap as MCP tools. The list in §B is fixed.
- The audience-slice output schema (§D). It is the contract the downstream slicer agent emits and the hq-x persistence layer consumes. Locking it now means both sides can build against it.
- Touching anything outside the MCP server module + the new docs file + tests. Do NOT modify any router, service, model, or ingest job in `data-engine-x/app/`. The MCP server is a 1:1 pass-through to existing read endpoints — if a tool can't be implemented as a thin wrapper, that means the underlying endpoint is wrong, and that's a different directive.
- Importing from `app.database`, `app.services`, or `app.routers`. The existing dex-mcp pattern enforces this rule and you keep it. Tools call the DEX HTTP API only.

**Why this exists:** The hq-x JIT brand factory + downstream audience slicer (next directive after this one) need an LLM agent that can take an outreach brief and produce candidate audience slices over the FMCSA / audiences / entities surfaces. Today the dex-mcp server exposes ONLY DealBridge tools — 7 lender/contract/recipient endpoints. Every other DEX read surface is invisible to MCP clients. An audience-slicer agent pointed at dex-mcp today cannot slice motor carriers, cannot inspect audience templates, cannot count entities — it can only see the dealbridge surface. That kills the validation loop for the brand factory before it starts.

This directive ships three things in one commit:
1. The MCP tool surface needed for the audience slicer (FMCSA carriers, FMCSA audiences, generic audiences endpoints, audience templates, entities).
2. An LLM-friendly DEX overview document — strategic guidance about what datasets exist, when to use which tool, how to count, how to combine. Loaded by clients as system-prompt prelude or via a `dex_overview` tool call.
3. The locked audience-slice output schema — the contract between the slicer agent and whatever consumes its output.

After this directive lands, an LLM client (Claude Code, a managed-agent, or any MCP-aware runtime) with dex-mcp loaded + the overview doc in context + an outreach brief can produce candidate audience slices in the locked output format. That validation loop is the prerequisite for building the upstream brief generator.

**Critical existing-state facts (verify before building):**

- The current dex-mcp server is at [app/mcp_server/dex_server.py](../../../../data-engine-x/app/mcp_server/dex_server.py). Read it end-to-end. Note: `_compact(**kwargs)` strips `None`s before posting; `post_json` and `get_json` come from `app/mcp_server/client.py`; per-tool docstrings are the LLM-facing contract.
- The 7 existing DealBridge tools are the pattern. Mirror it exactly — same `Annotated[Type, Field(default=..., description="...")]` signature shape, same `_compact` body building, same `post_json` / `get_json` calls, same docstring discipline (terse, one paragraph + optional structured-fact lines).
- A sibling NYC-audiences MCP expansion exists in worktree `awesome-diffie-89ccdd` ([tests/mcp_server/test_nyc_audiences_mcp.py](../../../../data-engine-x/.claude/worktrees/awesome-diffie-89ccdd/tests/mcp_server/test_nyc_audiences_mcp.py)). Read it as a precedent for tool-registration testing (smoke tests assert tool names appear in `mcp.list_tools()` output without hitting the live DEX API). DO NOT vendor or merge that branch — it's a parallel build. If those tools land on main before this directive runs, do not duplicate them; this directive covers FMCSA + audiences + entities only.
- Endpoint paths to wrap (verified in [app/main.py](../../../../data-engine-x/app/main.py)):
  - `/api/v1/fmcsa/carriers/*` — fmcsa_carriers_v1
  - `/api/v1/fmcsa/audiences/*` — fmcsa_audiences_v1
  - `/api/v1/audiences/*` — audiences_v1 (generic criteria-schema / resolve / count / entities/{id})
  - `/api/v1/fmcsa/audience-templates/*` and `/api/v1/fmcsa/audience-specs/*` — audience_templates_v1 (note: mounted under `/api/v1/fmcsa` per main.py)
  - `/api/v1/entities/*` — entities_v1

---

## Existing code to read before starting

In order:

1. [app/mcp_server/README.md](../../../../data-engine-x/app/mcp_server/README.md) — server overview + the "Intentionally not here" rule.
2. [app/mcp_server/dex_server.py](../../../../data-engine-x/app/mcp_server/dex_server.py) — full file. The 7 existing tools are the spec for "what a good MCP tool looks like."
3. [app/mcp_server/client.py](../../../../data-engine-x/app/mcp_server/client.py) — `get_json` / `post_json` / `AppContext` + `app_lifespan`. You use these unchanged.
4. [app/routers/fmcsa_carriers_v1.py](../../../../data-engine-x/app/routers/fmcsa_carriers_v1.py) — endpoints to wrap as `fmcsa_carriers_*` tools.
5. [app/routers/fmcsa_audiences_v1.py](../../../../data-engine-x/app/routers/fmcsa_audiences_v1.py) — pre-canned FMCSA audience queries (new-entrants-90d, authority-grants, insurance-lapses, high-risk-safety, insurance-renewal-window, recent-revocations).
6. [app/routers/audiences_v1.py](../../../../data-engine-x/app/routers/audiences_v1.py) — generic audience surface (`/criteria-schema`, `/resolve`, `/count`, `/entities/{entity_id}`).
7. [app/routers/audience_templates_v1.py](../../../../data-engine-x/app/routers/audience_templates_v1.py) — audience-template + audience-spec lifecycle.
8. [app/routers/entities_v1.py](../../../../data-engine-x/app/routers/entities_v1.py) — read endpoints on entities.
9. [.claude/worktrees/awesome-diffie-89ccdd/tests/mcp_server/test_nyc_audiences_mcp.py](../../../../data-engine-x/.claude/worktrees/awesome-diffie-89ccdd/tests/mcp_server/test_nyc_audiences_mcp.py) — test pattern reference (do not vendor).

For each router file: read its imports, the request/response Pydantic models, and the docstrings on each endpoint function. The MCP tool docstring should compress the endpoint's intent into one paragraph; field descriptions stay aligned with the FastAPI Field descriptions on the request model.

---

## Phase A — DEX overview document

**File:** `app/mcp_server/DEX_OVERVIEW.md` (new)

The LLM-friendly strategic-guidance document. The existing `mcp.instructions` string in `dex_server.py` is one paragraph — fine for tool-call gating but useless for an agent trying to reason at the dataset level. This document is the upgrade.

Required sections:

### 1. What DEX is
2-3 paragraphs. The data-engine that hq-x and adjacent agents query for sliceable structured data. List the dataset families (FMCSA motor carriers, federal contracts/registrations via SAM.gov + USA-spending, SBA loans, NYC property/HPD/DOB, USDA RD, etc.). Note that not all datasets are MCP-exposed today — only the ones whose tools are registered. Refer the reader to the live tool list (`mcp.list_tools()`) for ground truth.

### 2. Datasets, in detail
For each MCP-exposed dataset family: one subsection. Each subsection covers:

- **What it is** — one paragraph: source, granularity (per-entity / per-event / per-snapshot), update cadence, time-coverage horizon.
- **What attributes are sliceable** — bullet list. Group attributes by category (identity / size / geography / regulatory-status / time-state / ops-quality). Per attribute: name, type, value range or canonical values, gotchas (e.g. "MC# can be active or inactive — `mc_status` field").
- **What pre-canned audiences exist** — list of audience-template slugs available via `dex_audience_templates_list`, when each is the right answer.
- **How to count vs how to enumerate** — when to call the `_count` variant vs the full `_search` variant. Counts are cheap; full enumerations may paginate.
- **Combining filters** — common idioms (e.g. AND across attributes; OR via repeated calls; when to use `audiences/resolve` over a raw search).
- **Cross-dataset joins** — what entity-resolution surface exists (e.g. `entities/*` endpoints) for joining FMCSA → SAM.gov → USA-spending on the same operator.

Datasets to cover in this directive's overview (matches what gets MCP-exposed in §B):
- FMCSA motor carriers (carrier-level identity + safety + insurance + authority)
- FMCSA audiences (the 6 pre-canned signal queries: new entrants, authority grants, insurance lapses/renewals, high-risk safety, recent revocations)
- Audience templates and audience specs (the lifecycle for human-curated audiences)
- Generic audiences resolution (criteria-schema + resolve + count + entity lookup)
- Entities (cross-dataset entity-resolution + merged record reads)

NYC audiences, govcontracts audiences, SBA borrowers, SAM.gov registrations, etc. — not in this directive's scope. Note in the overview that they exist as DEX endpoints but are not yet MCP-exposed; instruct readers to refer to live `mcp.list_tools()` output.

### 3. The audience-slicer's job
2-3 paragraphs. The agent's task: given an `outreach_brief` artifact (markdown — see hq-x docs for shape), produce 5–6 candidate audience slices, each in the locked output schema (§D). The slicer should:
- Pick the right dataset(s) based on the brief's audience archetype + customer-pain mapping.
- Use `dex_audience_templates_list` to check whether a pre-canned audience already covers a slice — if so, reference its slug rather than reinventing filters.
- Use `_count` endpoints to size each candidate slice before returning it (a slice with no estimated size is rejected at validation).
- Use `_search` only sparingly — counts answer most slicing questions and are an order of magnitude cheaper.
- When the brief references a pain-→-trigger mapping (e.g. "MC# went active in last 30 days"), pick the matching pre-canned FMCSA audience (`new-entrants-90d`, `authority-grants`) rather than constructing a raw filter.

### 4. Output schema reminder
A short section pointing to §D below (or quoting it inline if the executor decides keeping the spec in one place is cleaner). Either way: the slicer's output MUST validate against the schema in §D. A slice that doesn't validate is rejected.

### 5. What this overview does NOT cover
One short section. Bullet list:
- Per-recipient creative generation (downstream of slicing, separate agent).
- Brand fit-checking (separate agent in hq-x).
- Per-tool API contract details — those live in the tool docstrings (`mcp.list_tools()`).
- Authentication / auth-token rotation — handled by the dex-mcp config.
- Data freshness SLAs per dataset — link to whatever DEX runbook covers that.

The overview should be ~600–1000 lines of markdown total. No code blocks of Python — only example tool-call sketches in pseudocode if they help. Cite tool names verbatim (e.g. "use `fmcsa_carriers_search` with `power_units_max=10`") so the agent can pattern-match against `mcp.list_tools()` output.

**Make the overview retrievable as a tool.** Add a tool `dex_overview()` (no parameters) that returns the full markdown content of `DEX_OVERVIEW.md` as a string. Implementation: read the file at module-import time into a constant; the tool returns the constant. This means any MCP client gets the overview by calling one tool — no out-of-band file shipping.

---

## Phase B — MCP tool expansion

**File:** `app/mcp_server/dex_server.py` (modify)

Add the following tool groups, in this order in the file (matching the existing organizational header-comment style):

### B1. FMCSA — motor carriers

Wraps `/api/v1/fmcsa/carriers/*`. One MCP tool per endpoint. Read [app/routers/fmcsa_carriers_v1.py](../../../../data-engine-x/app/routers/fmcsa_carriers_v1.py) for the endpoint list (you grep'd 8 endpoints earlier — wrap all of them). Tool naming convention:

- `fmcsa_carriers_search` → POST `/api/v1/fmcsa/carriers/search`
- `fmcsa_carriers_insurance_cancellations` → POST `/api/v1/fmcsa/carriers/insurance-cancellations`
- (one tool per remaining endpoint, matching path-to-snake-case pattern)

Add a `_count` variant for any search-shaped endpoint that has a count counterpart on the API. If the API doesn't expose a count endpoint for a given search, do not invent one — skip.

Each tool's docstring: one paragraph explaining intent + one structured-fact line ("Backed by `<view/table name>`.") if you can identify it from the router.

### B2. FMCSA — pre-canned audiences

Wraps `/api/v1/fmcsa/audiences/*`. Six endpoints (per the grep): `new-entrants-90d`, `authority-grants`, `insurance-lapses`, `high-risk-safety`, `insurance-renewal-window`, `recent-revocations`. Tool naming:

- `fmcsa_audience_new_entrants_90d`
- `fmcsa_audience_authority_grants`
- `fmcsa_audience_insurance_lapses`
- `fmcsa_audience_high_risk_safety`
- `fmcsa_audience_insurance_renewal_window`
- `fmcsa_audience_recent_revocations`

Add a `_count` variant for each if the API exposes one. Same docstring discipline.

### B3. Generic audiences (criteria-schema, resolve, count)

Wraps `/api/v1/audiences/*`. Tools:

- `audience_criteria_schema` → GET `/api/v1/audiences/criteria-schema`. Returns the schema describing what filter criteria the resolver accepts. The agent calls this once at the start of a session to know what slicing primitives exist.
- `audience_resolve` → POST `/api/v1/audiences/resolve`. Resolves a criteria payload into a concrete entity set.
- `audience_count` → POST `/api/v1/audiences/count`. Counts the entities matching a criteria payload (cheap; the slicer's primary sizing tool).
- `audience_entity_lookup` → GET `/api/v1/audiences/entities/{entity_id}`. Reads a single entity record by id.

### B4. Audience templates + specs lifecycle

Wraps `/api/v1/fmcsa/audience-templates*` and `/api/v1/fmcsa/audience-specs*` (per main.py, both mount under `/api/v1/fmcsa`). Tools (read endpoints only — DO NOT expose POST endpoints that mutate state in this directive):

- `dex_audience_templates_list` → GET `/api/v1/fmcsa/audience-templates`
- `dex_audience_template_get` → GET `/api/v1/fmcsa/audience-templates/{slug}`
- `dex_audience_spec_get` → GET `/api/v1/fmcsa/audience-specs/{spec_id}`
- `dex_audience_spec_descriptor` → GET `/api/v1/fmcsa/audience-specs/{spec_id}/descriptor`

POST endpoints on this surface (`/audience-templates` create, `/audience-specs` create, `/audience-specs/{spec_id}/preview`, `/audience-specs/{spec_id}/count`) — exposed via MCP only if read-side use is insufficient. For this directive's slicer-validation purpose, the slicer is reading existing templates / specs, not creating new ones. Skip the POST tools unless a test reveals they're needed (in which case, add a single tool `dex_audience_spec_count` for the count POST since count is read-equivalent semantically).

### B5. Entities

Wraps `/api/v1/entities/*`. Read [app/routers/entities_v1.py](../../../../data-engine-x/app/routers/entities_v1.py) for the endpoint list. Wrap the read endpoints (entity reads, merged-record reads, entity-relationship reads). Skip mutating endpoints. Tool naming:

- `entities_search` → POST `/api/v1/entities/search` (or whichever endpoint name)
- `entities_get` → GET `/api/v1/entities/{entity_id}` (or whichever shape)
- (etc., matching the actual endpoint shapes)

### B6. Overview retrieval

- `dex_overview` (no parameters) → returns `DEX_OVERVIEW.md` content as a string. Implementation: read at module-import time, cache as a module-level constant.

### Tool registration discipline

- All new tools use the same `_compact(**kwargs)` body-building helper for POSTs.
- All new tools use `_http(ctx)` for the HTTP client (the lifespan-managed singleton).
- All new tools have `Annotated[Type, Field(default=..., description="...")]` per parameter — the descriptions are LLM-facing; they should match the equivalent FastAPI Field descriptions on the request model.
- All new tools have one-paragraph docstrings + at most one structured-fact line. NO usage examples in docstrings — the overview document covers usage.

---

## Phase C — Tests

**File:** `tests/mcp_server/test_dex_mcp_expansion.py` (new — directory may not exist; create `tests/mcp_server/__init__.py` if needed).

Mirror the smoke-test pattern from the awesome-diffie-89ccdd worktree. Tests do NOT hit the live DEX API — they assert tool registration + body-shape correctness via mocked `post_json` / `get_json`.

Tests:

1. `test_all_fmcsa_carrier_tools_registered` — assert every tool name from §B1 appears in `mcp.list_tools()`.
2. `test_all_fmcsa_audience_tools_registered` — same for §B2.
3. `test_all_generic_audience_tools_registered` — same for §B3.
4. `test_all_audience_template_tools_registered` — same for §B4.
5. `test_all_entity_tools_registered` — same for §B5.
6. `test_dex_overview_tool_returns_markdown` — call `dex_overview` via the MCP machinery; assert the return is a non-empty string starting with `# ` (markdown H1).
7. `test_existing_dealbridge_tools_still_registered` — smoke-check no regression of the 7 existing DealBridge tools.
8. `test_fmcsa_carriers_search_posts_to_correct_path` — patch `post_json`, call the tool, assert path is `/api/v1/fmcsa/carriers/search` and body shape passes through the input parameters.
9. `test_audience_count_posts_to_correct_path` — same shape, for `/api/v1/audiences/count`.
10. `test_dex_audience_template_get_uses_path_param` — patch `get_json`, call with `slug='fmcsa-new-entrants'`, assert path is `/api/v1/fmcsa/audience-templates/fmcsa-new-entrants`.

Run via `pytest tests/mcp_server/test_dex_mcp_expansion.py -v`.

---

## Phase D — Audience slice output schema

**File:** `app/mcp_server/AUDIENCE_SLICE_SCHEMA.md` (new)

Lock the output contract for the downstream audience-slicer agent. This is a markdown document containing the JSON schema + worked example + validation rules. Lives alongside `DEX_OVERVIEW.md` so any MCP client can fetch both.

Schema (JSON):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "AudienceSliceCandidate",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "slice_name",
    "slice_description",
    "dataset",
    "must_be_true_filters",
    "estimated_total_size",
    "size_breakdown_by_filter"
  ],
  "properties": {
    "slice_name": {
      "type": "string",
      "description": "Short name, snake_case, ≤60 chars. Stable identifier for this slice within a single brief's slate of candidates.",
      "maxLength": 60
    },
    "slice_description": {
      "type": "string",
      "description": "One paragraph (≤400 chars) describing who this slice targets and why this slice fits the outreach brief's audience type."
    },
    "dataset": {
      "type": "string",
      "enum": ["fmcsa_carriers", "fmcsa_brokers", "sba_borrowers", "sam_gov_registrations", "nyc_property_owners", "govcontracts_recipients", "entities_merged"],
      "description": "Primary dataset this slice draws from. The dataset determines which dex-mcp tools enumerate it."
    },
    "must_be_true_filters": {
      "type": "array",
      "description": "Hard filters every entity in the slice must satisfy. ALL filters AND together. Each filter must map to a concrete attribute the dataset exposes.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["attribute", "operator", "value"],
        "properties": {
          "attribute": {"type": "string"},
          "operator": {"type": "string", "enum": ["equals", "not_equals", "in", "not_in", "gte", "lte", "between", "exists", "not_exists", "matches"]},
          "value": {},
          "rationale": {"type": "string", "description": "Why this filter is must-be-true for this slice. Cite the brief's pain-or-trigger row that motivates it."}
        }
      },
      "minItems": 1
    },
    "weighted_signals": {
      "type": "array",
      "description": "Optional softer filters. Each contributes to ranking within the slice but is not required. Used for overweighting growth-priority sub-segments.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["attribute", "weight"],
        "properties": {
          "attribute": {"type": "string"},
          "operator": {"type": "string"},
          "value": {},
          "weight": {"type": "number", "minimum": 0, "maximum": 1},
          "rationale": {"type": "string"}
        }
      }
    },
    "disqualifiers": {
      "type": "array",
      "description": "Hard exclusions. Same shape as must_be_true_filters but inverted semantics — entities matching any disqualifier are excluded.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["attribute", "operator", "value"],
        "properties": {
          "attribute": {"type": "string"},
          "operator": {"type": "string"},
          "value": {},
          "rationale": {"type": "string"}
        }
      }
    },
    "estimated_total_size": {
      "type": "integer",
      "description": "Estimated entity count after must_be_true_filters AND NOT(disqualifiers). MUST be derived from a real dex-mcp count tool call (e.g. `audience_count` or `fmcsa_carriers_search_count`). The slicer's reasoning trace should reference which tool it called.",
      "minimum": 0
    },
    "size_breakdown_by_filter": {
      "type": "array",
      "description": "Marginal size contribution per must_be_true_filter, computed by relaxing each filter one at a time and re-counting. Helps a human reviewer see which filters bind hardest.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["filter_attribute", "size_with_filter", "size_without_filter"],
        "properties": {
          "filter_attribute": {"type": "string"},
          "size_with_filter": {"type": "integer"},
          "size_without_filter": {"type": "integer"}
        }
      }
    },
    "audience_template_slug": {
      "type": ["string", "null"],
      "description": "If this slice is fully covered by an existing pre-canned audience template (e.g. `fmcsa-new-entrants-90d`), the slug. If yes, must_be_true_filters can be terse (the template encodes the full filter set). If null, slice was constructed from primitive filters."
    },
    "outreach_brief_pain_refs": {
      "type": "array",
      "description": "Cross-reference back to the outreach brief's pain-→-trigger rows that motivated this slice. Each item is a string excerpt (≤120 chars) from the brief's pain row.",
      "items": {"type": "string", "maxLength": 120},
      "minItems": 1
    },
    "confidence": {
      "type": "string",
      "enum": ["high", "medium", "low"],
      "description": "Slicer's self-assessed confidence that this slice will resonate with the target account."
    },
    "rationale": {
      "type": "string",
      "description": "One paragraph (≤500 chars). Why this slice is a good candidate for the target account, given the outreach brief. Should reference at least one offering from the brief's `What they offer` section that this audience would value."
    }
  }
}
```

Worked example: include one fully-populated slice in the overview document for an FMCSA-carrier audience tied to a hypothetical outreach brief about a load-board company. The example is illustrative — do not pick a real prospect.

Validation rules (these go in the document as plain English, AND are enforced by the slicer agent's prompt):

- A slice with `estimated_total_size == 0` is invalid. Reject and rebuild filters.
- A slice with `estimated_total_size > 500_000` should be flagged for the brief reviewer — likely too broad; consider tighter must_be_true_filters.
- `audience_template_slug` is preferred when one exists. The slicer should call `dex_audience_templates_list` early in its session to know what's pre-canned.
- Every must_be_true_filter's `attribute` MUST be a real attribute of the chosen `dataset`. The slicer verifies via `audience_criteria_schema` (or the dataset-specific equivalent) before emitting the slice.
- `outreach_brief_pain_refs` MUST contain at least one ref. A slice with no traceable pain motivation is rejected.

Make the schema retrievable as a tool: add a tool `audience_slice_schema` (no parameters) returning the JSON schema as a string. Same pattern as `dex_overview`.

---

## What NOT to do

- Do **not** modify any router, service, model, or migration in `data-engine-x/app/`. The MCP server is a wrapper; the underlying API is out of scope.
- Do **not** expose mutating endpoints (POST that creates / PUT / DELETE) via MCP in this directive. Audience slicer is read-only. Spec creation lands in a future directive when human curation pipelines are built.
- Do **not** vendor or import the NYC-audiences MCP tools from worktree `awesome-diffie-89ccdd`. That is a parallel branch.
- Do **not** add MCP tools for SBA borrowers, SAM.gov registrations, USDA RD, govcontracts audiences, or NYC audiences. Those are out of scope for this directive — they ship in follow-up directives keyed to specific use cases.
- Do **not** introduce caching, retries, or response transformation in any new tool. The "Intentionally not here" rule from `app/mcp_server/README.md` holds.
- Do **not** import from `app.database`, `app.services`, `app.routers`, or any in-process DEX module. Tools call `DEX_API_BASE_URL` HTTP only.
- Do **not** put usage examples or multi-paragraph guidance in tool docstrings — that bloats `list_tools()` output. Long-form guidance lives in `DEX_OVERVIEW.md`, accessed via the `dex_overview` tool.
- Do **not** invent endpoint paths. If a path you write doesn't exist on the DEX API, the tool will 404 — that's a sign you misread the router. Re-read the router file.
- Do **not** add a SDK dependency (Anthropic SDK, OpenAI SDK, etc.). The tools call DEX HTTP only.
- Do **not** modify the dex-mcp's `mcp.instructions` string. The `dex_overview` tool replaces the role of long-form guidance; the instructions stay terse.

---

## Scope

Files to create or modify (all paths relative to `data-engine-x` repo root):

- `app/mcp_server/dex_server.py` (modify — add ~25–35 new tools across §B1–B6, plus the `dex_overview` and `audience_slice_schema` retrieval tools)
- `app/mcp_server/DEX_OVERVIEW.md` (new)
- `app/mcp_server/AUDIENCE_SLICE_SCHEMA.md` (new)
- `tests/mcp_server/__init__.py` (new — empty file, only if directory does not exist)
- `tests/mcp_server/test_dex_mcp_expansion.py` (new)
- `app/mcp_server/README.md` (modify — append a "Tools added in v1" section listing the new tool groups + a one-line pointer to `DEX_OVERVIEW.md` and `AUDIENCE_SLICE_SCHEMA.md`)

**One commit. Do not push.**

Commit message:

> feat(mcp): expand dex-mcp tool surface (FMCSA + audiences + entities) + DEX overview doc + audience-slice output schema
>
> Add ~25–35 new MCP tools wrapping the FMCSA carriers, FMCSA pre-canned
> audiences, generic audience resolution, audience templates/specs read
> surface, and entities read endpoints. Each tool is a thin pass-through
> matching the existing DealBridge pattern — no caching, no retries, no
> imports from app.{database,services,routers}.
>
> Add `app/mcp_server/DEX_OVERVIEW.md` — strategic-guidance document for
> LLM clients explaining what datasets exist, when to use which tool, how
> to count vs enumerate, how to combine filters, and what the audience-
> slicer's job is. Retrievable as a single tool call via `dex_overview()`.
>
> Add `app/mcp_server/AUDIENCE_SLICE_SCHEMA.md` — locked JSON schema for
> the audience-slicer agent's output (slice_name, dataset, must_be_true_
> filters, estimated_total_size, size_breakdown_by_filter, audience_
> template_slug, outreach_brief_pain_refs, confidence, rationale).
> Retrievable via `audience_slice_schema()`.
>
> Read-only tools only; mutating endpoints (audience-template / audience-
> spec creation) intentionally deferred.

---

## When done

Report back with:

(a) The full list of tool names added, grouped by §B subsection (B1–B6).

(b) Path to `DEX_OVERVIEW.md` and a 4-bullet summary: which dataset families are documented, the total line count, the example tool-call sketches included, and any sections you added beyond the required §A list.

(c) Path to `AUDIENCE_SLICE_SCHEMA.md` and confirmation that the schema validates the worked example included in the doc (use any JSON-schema validator — `jsonschema` is fine; vendor it for the test if not already present).

(d) `pytest tests/mcp_server/test_dex_mcp_expansion.py -v` — pass count + total time.

(e) Output of `python -c "import asyncio; from app.mcp_server.dex_server import mcp; print(len(asyncio.run(mcp.list_tools())))"` — the total tool count after this directive (existing 7 + new tools).

(f) Confirmation that `dex_overview()` and `audience_slice_schema()` tools each return a non-empty string when invoked through the MCP test machinery.

(g) The single commit SHA. Do not push.
