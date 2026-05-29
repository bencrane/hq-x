"""Closed enum + payload schemas for the gtm-agent's `present_result` custom tool.

The agent calls ``present_result`` whenever it produces a structured artifact
for the operator. Each ``result_type`` value maps to a renderer component
on the platform-app right panel AND to a payload shape documented in the
tool's description.

Adding a result_type:
  1. Add to RESULT_TYPE_PAYLOAD_SCHEMAS below.
  2. Update SYSTEM_PROMPT_APPENDIX + PRESENT_RESULT_TOOL_DESCRIPTION.
  3. Run scripts/managed_agents/bump_agent.py to mint a new agent version.
  4. Add a renderer component on platform-app.

Removing a result_type is a behavior break — historical agent_runs may
have called the old type, so renderers should keep handling them.

Validation note: we keep the tool's ``input_schema.payload`` loose
(``{"type":"object"}``) rather than encoding a discriminated union via
JSON-Schema ``if/then/else``. Anthropic's tool-input validator rejects
``additionalProperties``, conditional clauses, and other dialect
extensions; sticking to type/properties/required/enum/items keeps the
schema accepted. The agent follows the per-type shapes because (a) the
system prompt instructs it to and (b) the tool description documents
each shape verbatim. The frontend renders only when the payload matches
the per-type schema and falls back to a JSON dump otherwise — that's
the hard validation boundary.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Per-result_type payload schemas
# ---------------------------------------------------------------------------

_DATA_TABLE_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["columns", "rows"],
    "properties": {
        "columns": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "label", "type"],
                "properties": {
                    "key": {"type": "string", "minLength": 1},
                    "label": {"type": "string", "minLength": 1},
                    "type": {
                        "type": "string",
                        "enum": ["text", "number", "date", "boolean", "currency"],
                    },
                },
            },
        },
        "rows": {"type": "array", "items": {"type": "object"}},
        "total_rows": {"type": "integer", "minimum": 0},
        "source": {"type": "string"},
    },
}

_RANKED_LIST_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "scoring_method"],
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rank", "id", "label", "score", "rationale"],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1},
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "score": {"type": "number"},
                    "score_max": {"type": "number"},
                    "rationale": {"type": "string"},
                    "evidence": {"type": "object"},
                },
            },
        },
        "scoring_method": {"type": "string"},
    },
}

_METRIC_GRID_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tiles"],
    "properties": {
        "tiles": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "value", "format"],
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": ["number", "string"]},
                    "format": {
                        "type": "string",
                        "enum": ["int", "decimal", "percent", "currency"],
                    },
                    "delta": {"type": "number"},
                    "delta_label": {"type": "string"},
                },
            },
        },
    },
}

_RECOMMENDATION_CARD_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "rationale", "confidence", "inputs_used"],
    "properties": {
        "decision": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "inputs_used": {"type": "array", "items": {"type": "string"}},
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
}

_NARRATIVE_SUMMARY_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "key_points"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "key_points": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}

_SCHEMA_CARD_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dataset", "columns"],
    "properties": {
        "dataset": {"type": "string", "minLength": 1},
        "namespace": {"type": "string"},
        "uri": {"type": "string"},
        "row_count": {"type": "integer", "minimum": 0},
        "columns": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "minLength": 1},
                    "nullable": {"type": "boolean"},
                },
            },
        },
    },
}


RESULT_TYPE_PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "data_table": _DATA_TABLE_PAYLOAD_SCHEMA,
    "ranked_list": _RANKED_LIST_PAYLOAD_SCHEMA,
    "metric_grid": _METRIC_GRID_PAYLOAD_SCHEMA,
    "recommendation_card": _RECOMMENDATION_CARD_PAYLOAD_SCHEMA,
    "narrative_summary": _NARRATIVE_SUMMARY_PAYLOAD_SCHEMA,
    "schema_card": _SCHEMA_CARD_PAYLOAD_SCHEMA,
}

RESULT_TYPES: tuple[str, ...] = tuple(RESULT_TYPE_PAYLOAD_SCHEMAS.keys())


# ---------------------------------------------------------------------------
# Tool definition for create/update agent
# ---------------------------------------------------------------------------

PRESENT_RESULT_TOOL_NAME = "present_result"

# Anthropic caps tool descriptions at 1024 chars. The verbose per-type
# guidance lives in SYSTEM_PROMPT_APPENDIX below (no cap); this is the
# concise runtime hint the agent sees when deciding whether to call.
PRESENT_RESULT_TOOL_DESCRIPTION = """\
Produce a structured artifact for the operator. Rendered as a typed card in the results panel. NEVER use for narration — `agent.message` is for inline prose.

result_type values and payload shapes:

data_table: {columns:[{key,label,type:"text|number|date|boolean|currency"}], rows:object[], total_rows?, source?}

ranked_list: {items:[{rank,id,label,score,score_max?,rationale,evidence?}], scoring_method}

metric_grid: {tiles:[{label,value,format:"int|decimal|percent|currency",delta?,delta_label?}]}

recommendation_card: {decision,rationale,confidence:"low|medium|high",inputs_used:string[],next_actions?:string[]}

narrative_summary: {summary,key_points:string[],confidence?:"low|medium|high"}

schema_card: {dataset,namespace?,uri?,row_count?,columns:[{name,type,nullable?}]}

title: optional label rendered above the card.
"""

# Anthropic's tool input_schema validator accepts a constrained JSON Schema
# subset — `additionalProperties`, `oneOf`/`allOf`/conditional clauses, and
# other dialect extensions are rejected. Stick to type/properties/required/
# enum/items. Per-payload shapes are documented in description + system
# prompt; frontend hard-validates at render time.
PRESENT_RESULT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["result_type", "payload"],
    "properties": {
        "result_type": {"type": "string", "enum": list(RESULT_TYPES)},
        "title": {"type": "string"},
        "payload": {"type": "object"},
    },
}


def present_result_tool_def() -> dict[str, Any]:
    """Tool dict to embed in the agent's `tools` array."""
    return {
        "type": "custom",
        "name": PRESENT_RESULT_TOOL_NAME,
        "description": PRESENT_RESULT_TOOL_DESCRIPTION,
        "input_schema": PRESENT_RESULT_INPUT_SCHEMA,
    }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

BASE_SYSTEM_PROMPT = (
    "You are a GTM operations agent. Your job is to enrich cohorts, "
    "evaluate signals, and produce ranked action lists. Be highly "
    "analytical and concise."
)

_PRESENT_RESULT_APPENDIX = """

# Producing results

When you produce a finished artifact for the operator, you MUST call the `present_result` tool with the correct `result_type`. `agent.message` text is for narration only — never write tables, rankings, metrics, recommendations, summaries, or schemas as inline prose.

The six `result_type` values:

- `data_table`: rows of structured data with typed columns. Use for cohort listings, enriched cohorts, side-by-side comparisons of multiple records{polaris_data_table_extra}.

- `ranked_list`: ordered list of items with scores and rationales. Use for top-N recommended actions, prioritized recipients, scored options.

- `metric_grid`: KPI tiles. Use for cohort breakdowns (total, by segment, by score band), signal-evaluation aggregates (count, mean score, conversion rate).

- `recommendation_card`: singular decision artifact with rationale and confidence. Use for "run this signal in prod", "hold this signal", "amend the criteria", "do not target this segment".

- `narrative_summary`: structured end-of-turn summary distinct from inline message text. Use to persist a concise written conclusion the operator can re-read without scrolling the event log. Include 2-5 key_points.

- `schema_card`: a database schema description. Use whenever you inspect a dataset {polaris_schema_card_source} and want to surface the column list as a typed artifact rather than narrate it.

Use `title` to give the artifact a short label rendered above the card."""


_POLARIS_PROMPT_PREFIX = """

# Warehouse access via the `gtm` MCP server

You have access to the Polaris LanceDB warehouse via three MCP tools:

- `gtm.list_polaris_datasets` — discover what namespaces and Lance datasets exist.
- `gtm.get_polaris_schema(namespace, dataset_name)` — return exact columns + types + row count for one dataset.
- `gtm.execute_read_only_duckdb_query(sql_query)` — run a SELECT against the warehouse (DDL/DML rejected; 100-row cap).

**MANDATORY workflow for any warehouse query:**

1. ALWAYS call `gtm.get_polaris_schema` BEFORE writing a `gtm.execute_read_only_duckdb_query`. Never guess column names. Never rely on column names from the user's message — they may be wrong, stale, or paraphrased. The schema tool is cheap (metadata-only, no row scan) and authoritative.
2. Reference datasets in SQL by dotted identifier `<namespace>.<dataset_name>` (the server auto-resolves the Lance URI and registers it as a DuckDB view).
3. If the schema you got doesn't contain a column you need, STOP and call `gtm.list_polaris_datasets` to find the right dataset. Do NOT invent columns.
4. When surfacing a schema to the operator, use `present_result` with `result_type="schema_card"` — never narrate the column list as prose.
5. When surfacing query results, use `result_type="data_table"` — never inline rows as prose."""


def build_full_system_prompt(*, polaris_enabled: bool) -> str:
    """Render the full system prompt for the gtm-agent.

    The polaris-workflow section is included only when the polaris MCP
    server is wired (i.e. GTM_MCP_URL is set at bump time). Pre-
    Stage-5 agent versions get the present_result-only prompt; once
    Stage 5 wires the MCP toolset, the polaris workflow becomes a
    first-class part of the prompt. Bumping the prompt without the
    actual server reachable would teach the agent to call tools that
    don't exist — misleading and quietly broken.
    """
    if polaris_enabled:
        present_result = _PRESENT_RESULT_APPENDIX.format(
            polaris_data_table_extra=", and the rows returned by `gtm.execute_read_only_duckdb_query`",
            polaris_schema_card_source="via `gtm.get_polaris_schema`",
        )
        return BASE_SYSTEM_PROMPT + _POLARIS_PROMPT_PREFIX + present_result
    present_result = _PRESENT_RESULT_APPENDIX.format(
        polaris_data_table_extra="",
        polaris_schema_card_source="via a schema-introspection tool",
    )
    return BASE_SYSTEM_PROMPT + present_result


# Pre-rendered prompts for the two configurations. Importers that want a
# specific shape can pick the right one; bump_agent.py uses
# build_full_system_prompt(polaris_enabled=bool(GTM_MCP_URL)).
SYSTEM_PROMPT_WITHOUT_POLARIS = build_full_system_prompt(polaris_enabled=False)
SYSTEM_PROMPT_WITH_POLARIS = build_full_system_prompt(polaris_enabled=True)

# Back-compat: the old FULL_SYSTEM_PROMPT name resolves to the pre-polaris
# shape (what was deployed at v4). bump_agent.py reads GTM_MCP_URL and
# picks the appropriate prompt without using this constant.
FULL_SYSTEM_PROMPT = SYSTEM_PROMPT_WITHOUT_POLARIS

# Kept for back-compat with the previous version exports; equal to
# SYSTEM_PROMPT_WITHOUT_POLARIS minus the BASE prefix.
SYSTEM_PROMPT_APPENDIX = SYSTEM_PROMPT_WITHOUT_POLARIS[len(BASE_SYSTEM_PROMPT):]


__all__ = [
    "RESULT_TYPES",
    "RESULT_TYPE_PAYLOAD_SCHEMAS",
    "PRESENT_RESULT_TOOL_NAME",
    "PRESENT_RESULT_TOOL_DESCRIPTION",
    "PRESENT_RESULT_INPUT_SCHEMA",
    "present_result_tool_def",
    "BASE_SYSTEM_PROMPT",
    "SYSTEM_PROMPT_APPENDIX",
    "FULL_SYSTEM_PROMPT",
    "SYSTEM_PROMPT_WITH_POLARIS",
    "SYSTEM_PROMPT_WITHOUT_POLARIS",
    "build_full_system_prompt",
]
