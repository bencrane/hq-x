# DMaaS Backend — API + MCP Overview

## What this document is

A summary of the direct-mail backend that landed in [`ec3ab52`](https://github.com/bencrane/hq-x/commit/ec3ab52): the canonical Lob print-spec data, the DMaaS scaffold/design/solver layer, and the MCP server that exposes it to managed agents. Use this as the briefing for whoever builds the **direct mail campaign designer managed agent**.

## The architecture in one paragraph

Print specs (Lob's published bleed/trim/safe/zone geometry) live in `direct_mail_specs` as the substrate. **Scaffolds** are durable layout templates with a JSON constraint DSL that references those zones. **Designs** are per-mailer content filling a scaffold's slots, with solver-resolved x/y/w/h cached on save. A pure Cassowary solver (kiwisolver, same algorithm as `@lume/kiwi`) translates the DSL → linear constraints; non-linear rules (overlap, contrast, grid) run as a post-solve validator phase. Every operation is exposed as both a REST endpoint and an MCP tool, so a managed agent can drive the whole loop without HTML-scraping our app.

## Data layer (Supabase)

| Table | What it holds |
|---|---|
| `direct_mail_specs` | 22 rows. Canonical Lob print specs across 9 categories (postcard, letter, self_mailer, snap_pack, booklet, check, card_affix, buckslip, letter_envelope). Bleed/trim dims, named zones with absolute coordinates, paper, DPI, source URLs. |
| `direct_mail_design_rules` | 5 rows. Universal design rules (300 DPI, 0.125" bleed, USPS scan zone rule, indicia clearance). |
| `dmaas_scaffolds` | Versioned layout templates. `compatible_specs` (which specs the scaffold renders against), `prop_schema` (JSON Schema for content_config), `constraint_specification` (the DSL). |
| `dmaas_designs` | Per-mailer designs. FK → scaffold + composite FK → `direct_mail_specs`. Caches `resolved_positions` from the solver. |
| `dmaas_scaffold_authoring_sessions` | Audit trail of LLM-assisted scaffold creation. Every prompt + proposed DSL stored, accepted/rejected flagged. |

## The constraint DSL

Pydantic-validated JSON. 15 constraint types in two families:

**Linear (kiwi-solvable):** `inside`, `vertical_gap`, `horizontal_gap`, `min_size`, `max_size`, `max_width_percent_of_zone`, `max_height_percent_of_zone`, `horizontal_align`, `vertical_align`, `anchor`, `size_ratio`

**Validator (post-solve):** `no_overlap`, `no_overlap_with_zone`, `color_contrast` (WCAG AA), `grid_align`

Every constraint has a `strength` (`required`/`strong`/`medium`/`weak`) routed verbatim to the solver. Failures come back as structured `ConstraintConflict` records — never a bare "unsatisfiable".

## REST API surface

**Mailer specs** (read-only, gated on `require_operator`):

| Method | Path | What it does |
|---|---|---|
| GET | `/direct-mail/specs` | List all specs (filterable by `?category=`) |
| GET | `/direct-mail/specs/categories` | Categories + variant counts (drives navigation) |
| GET | `/direct-mail/specs/design-rules` | Universal design rules |
| GET | `/direct-mail/specs/{category}/{variant}` | One full spec |
| POST | `/direct-mail/specs/{category}/{variant}/validate` | Pre-flight artwork dimensions vs spec |

**Scaffolds:**

| Method | Path | Auth |
|---|---|---|
| GET | `/api/v1/dmaas/scaffolds` | any user |
| GET | `/api/v1/dmaas/scaffolds/{slug}` | any user |
| POST | `/api/v1/dmaas/scaffolds/validate-constraints` | any user |
| POST | `/api/v1/dmaas/scaffolds` | operator only |
| PATCH | `/api/v1/dmaas/scaffolds/{slug}` | operator only |
| POST | `/api/v1/dmaas/scaffolds/{slug}/preview` | any user |

**Designs:**

| Method | Path | Auth |
|---|---|---|
| POST | `/api/v1/dmaas/designs` | any user |
| GET | `/api/v1/dmaas/designs` | any user (filterable by `?brand_id=`, `?audience_template_id=`, `?scaffold_id=`) |
| GET | `/api/v1/dmaas/designs/{id}` | any user |
| PATCH | `/api/v1/dmaas/designs/{id}` | any user |
| POST | `/api/v1/dmaas/designs/{id}/validate` | any user |

**Authoring sessions:**

| Method | Path | Auth |
|---|---|---|
| POST | `/api/v1/dmaas/scaffold-authoring-sessions` | operator only |
| GET | `/api/v1/dmaas/scaffold-authoring-sessions` | operator only |

## MCP server

Mounted at `/mcp/dmaas`, served by FastMCP on the same FastAPI process as the REST API. Managed agents connect via standard MCP HTTP transport.

10 tools, each a thin wrapper around the corresponding service-layer call (no HTTP hop):

| Tool | Wraps | When the agent calls it |
|---|---|---|
| `list_scaffolds(format?, vertical?, spec_category?)` | GET /scaffolds | "What layouts are available for this campaign?" |
| `get_scaffold(slug)` | GET /scaffolds/:slug | "Show me the prop_schema and constraint DSL of this layout." |
| `validate_constraints(spec_category, spec_variant, constraint_specification, sample_content)` | POST /scaffolds/validate-constraints | Tight inner loop while authoring a new scaffold — no save. |
| `preview_scaffold(slug, spec_category, spec_variant, placeholder_content)` | POST /scaffolds/:slug/preview | "What does this layout look like with placeholder text?" |
| `create_scaffold(...)` | POST /scaffolds | "I'm satisfied with the constraint set; persist it." |
| `update_scaffold(slug, ...)` | PATCH /scaffolds/:slug | Refine a saved scaffold. |
| `create_design(scaffold_id, spec_category, spec_variant, content_config, brand_id?, audience_template_id?)` | POST /designs | "Generate a mailer for this campaign with this content." |
| `get_design(id)` | GET /designs/:id | "Show me the resolved positions for this design." |
| `update_design_content(id, content_config)` | PATCH /designs/:id | "Re-fill this design with new content." |
| `validate_design(id)` | POST /designs/:id/validate | "Does this design still resolve after a scaffold update?" |

Each tool's docstring is the LLM-facing description — the agent picks the right tool based on what it's being asked to do.

## What this unlocks for a direct mail campaign designer managed agent

The managed agent can run the full design loop end-to-end **without ever leaving MCP**.

### Day 1: pick a layout for a campaign

```
agent: list_scaffolds(format="postcard", vertical="trucking")
→ ["hero-headline-postcard", ...]

agent: get_scaffold(slug="hero-headline-postcard")
→ {prop_schema: {headline: {...}, subhead: {...}, cta: {...}},
   constraint_specification: {...},
   compatible_specs: [{category: "postcard", variant: "6x9"}, ...]}
```

The agent now knows *exactly* what content fields to fill and what specs the scaffold supports. No guessing about layout.

### Day 1: generate a mailer for a specific recipient list

```
agent: create_design(
  scaffold_id="...",
  spec_category="postcard",
  spec_variant="6x9",
  content_config={
    "headline": {"text": "Reduce factoring fees", "color": "#111111", "intrinsic": {...}},
    "subhead": {"text": "Save up to 30% on your invoice factoring", ...},
    "cta": {"text": "Call 555-0100", ...},
    "background": {"color": "#ffffff"}
  },
  brand_id="...",
  audience_template_id="..."
)
→ {id: "...", resolved_positions: {headline: {x, y, w, h}, ...}}
```

The solver runs server-side. The agent gets back exact pixel coordinates. If content_config violates `prop_schema` (missing field, wrong type) or doesn't solve (text too long, contrast too low, overlap detected), the call returns a structured error the agent can act on:

```
{"error": "design_does_not_solve",
 "conflicts": [
   {"constraint_type": "color_contrast", "phase": "validator",
    "message": "contrast ratio 2.3:1 below minimum 4.5:1 (headline.color=#cccccc vs background.color=#ffffff)"}]}
```

The agent reads that, picks a darker headline color, retries. Self-correcting.

### Day 7: scaffold author flow (operator-supervised)

```
agent: validate_constraints(
  spec_category="postcard",
  spec_variant="4x6",
  constraint_specification={proposed DSL},
  sample_content={...})
→ {is_valid: false, conflicts: [{constraint_type: "min_size", phase: "linear",
    message: "unsatisfiable: required min_width 9999 exceeds zone width 1700"}]}

agent: (refines the DSL, tries again)
agent: validate_constraints(...)
→ {is_valid: true, positions: {...}}

agent: create_scaffold(slug="cta-bar-postcard", ...)
→ {id: "...", slug: "cta-bar-postcard", ...}
```

Tight feedback loop. The agent never persists a broken scaffold because `create_scaffold` re-runs the solver against every entry in `compatible_specs` and refuses to save if anything fails.

### Day 30: bulk content generation across an audience

```
for recipient in audience:
    agent: create_design(
      scaffold_id=picked_scaffold_id,
      spec_category="postcard",
      spec_variant="6x9",
      content_config=personalize(template, recipient),
      brand_id=campaign.brand_id,
      audience_template_id=campaign.audience_template_id
    )
```

Every design's `resolved_positions` is cached on save. The render → Lob send pipeline (separate workstream) reads them straight out of `dmaas_designs`. No re-solving on send.

### Day 60: scaffold update doesn't break old designs

```
agent: update_scaffold(slug="hero-headline-postcard",
                      constraint_specification={tweaked DSL})
→ {id: "...", version_number: 2}

# Sweep over existing designs:
for design_id in stale_design_ids:
    agent: validate_design(id=design_id)
    → {is_valid: bool, conflicts: [...]}
```

A new constraint-spec version doesn't auto-rewrite designs (designs are immutable history). The agent uses `validate_design` to identify which ones still solve and which need re-rendering with the new constraints.

## What's intentionally NOT here

The following are out of scope for this backend layer (each is a separate workstream):

- **Frontend canvas** — the React/visual editor that consumes these endpoints. Will use the same DSL JSON via a client-side TypeScript port (likely `@lume/kiwi`), so layout decisions made in-browser stay consistent with server-side validation.
- **The managed agents themselves** — both the *scaffold authoring agent* and the *content generation agent* will be created in the managed-agent system separately. They consume this MCP; they're not in this repo.
- **PDF rendering / Lob send pipeline** — turning `resolved_positions` + `content_config` into the actual PDF that gets uploaded to Lob. Lives in `app/providers/lob/` already; the integration with DMaaS designs is a future task.
- **Brand assets / asset library** — brand color palette, logo upload, font loading. Brands already exist (`business.brands`); the agent will read brand identity in to inform `content_config`, but the brand asset system itself isn't part of DMaaS.

## Cross-references

- DSL Pydantic models: [`app/dmaas/dsl.py`](../app/dmaas/dsl.py)
- Solver: [`app/dmaas/solver.py`](../app/dmaas/solver.py)
- Service (spec binding + content intrinsics): [`app/dmaas/service.py`](../app/dmaas/service.py)
- Repository: [`app/dmaas/repository.py`](../app/dmaas/repository.py)
- REST router: [`app/routers/dmaas.py`](../app/routers/dmaas.py)
- MCP tools: [`app/mcp/dmaas.py`](../app/mcp/dmaas.py)
- Migrations: [`migrations/0016_lob_mailer_specs.sql`](../migrations/0016_lob_mailer_specs.sql), [`0017_lob_mailer_specs_seed.sql`](../migrations/0017_lob_mailer_specs_seed.sql), [`0018_dmaas_scaffolds_designs.sql`](../migrations/0018_dmaas_scaffolds_designs.sql)
- Seed scaffolds: [`data/dmaas_seed_scaffolds.json`](../data/dmaas_seed_scaffolds.json) + [`scripts/seed_dmaas_scaffolds.py`](../scripts/seed_dmaas_scaffolds.py)
- Sync verifier: [`scripts/sync_lob_specs.py`](../scripts/sync_lob_specs.py)
- Tests: [`tests/test_dmaas_*.py`](../tests/), [`tests/test_direct_mail_specs.py`](../tests/test_direct_mail_specs.py) — 73 tests covering solver determinism, DSL validation, REST endpoints + auth gating, MCP dispatch
