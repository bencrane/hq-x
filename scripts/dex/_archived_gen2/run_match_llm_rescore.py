#!/usr/bin/env python3
"""Polymorphic LLM rescore for cross-source match MVs.

Reads candidate rows from a configured source MV (e.g. EPA<->SAM low-tier),
asks Claude for a verdict (match | mismatch | ambiguous) plus a 0..1 score
plus reasoning, and persists to entities.match_llm_rescores. Idempotent on
(source_mv_fqn, source_mv_row_pk, llm_model, prompt_template).

Why the same script handles every match MV: the abstraction is a config map.
Each entry declares the source MV, its row PK columns, the candidate WHERE
clause, the prompt template, and the field set sent to the model. Adding a
new MV is one config entry; no infrastructure changes.

Two execution modes:
  - sync: per-row Messages API call. Use for smoke tests and small loads.
  - batch: Anthropic Batches API (50% discount, 24h SLA). Use for >1k rows.

Both modes use the same prompt-caching strategy: the system prompt + tool
definitions are marked cache_control=ephemeral so the cached portion costs
~10% of normal after the first warm-up call in a batch.

Setup:
  pip install -r requirements.txt   # adds 'anthropic' SDK

CLI:
  PYTHONPATH=. doppler run -- python3 scripts/run_match_llm_rescore.py \\
    {dataset-key|all} [--model claude-haiku-4-5-20251001] [--max-rows N] \\
    [--mode batch|sync] [--dry-run] [--skip-already-processed] [--confirm-cost]

Examples:
  # Dry-run rendering + token estimate (no API calls)
  ... epa-sam-low-tier --dry-run --max-rows 5

  # Sync smoke (5 rows)
  ... epa-sam-low-tier --mode sync --max-rows 5

  # HMDA full sync
  ... hmda-gleif-name-diverges --mode sync

  # EPA full batch (~95k rows; gated by --confirm-cost if estimate >$50)
  ... epa-sam-low-tier --mode batch --confirm-cost
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

try:
    import anthropic
    from anthropic import Anthropic, APIStatusError
except ImportError:
    print(
        "anthropic SDK is not installed. Run: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Pricing per million tokens (Haiku 4.5, USD). Used for *estimates only*; the
# script also captures actual token counts post-call. Keep these in sync with
# https://docs.claude.com/en/docs/about-claude/pricing — at directive time
# Haiku 4.5 is roughly $1 / $5 per MTok input/output.
PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00
# Batch tier: 50% off both rails.
BATCH_DISCOUNT = 0.5
# Cached input reads ~10% of normal price.
CACHED_INPUT_DISCOUNT = 0.1

CONFIRM_COST_THRESHOLD_USD = 50.0

BATCH_POLL_INTERVAL_SEC = 30
BATCH_TIMEOUT_SEC = 26 * 60 * 60  # SDK SLA is 24h; allow slack.

DB_INSERT_CHUNK = 500


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("match-llm-rescore")


# --------------------------------------------------------------------------- #
# Prompt templates
# --------------------------------------------------------------------------- #
#
# Each template defines a system prompt (cached across all calls in a batch)
# and a user-prompt renderer. The user prompt is rendered from the source MV
# row's `fields_to_send` config so the same template works across MVs that
# share the same kind of judgment task.

PROMPT_TEMPLATES: dict[str, dict[str, Any]] = {
    "lei-canonical-v1": {
        "system": (
            "You evaluate whether two name variants refer to the same legal entity. "
            "The two records share a stable canonical identifier (LEI), so default to "
            "'match' unless the names are decisively divergent (different industries, "
            "different parent companies, clearly different organizations). "
            "Distinguish formatting noise (NA vs National Association, abbreviations, "
            "punctuation, capitalization) from actual entity differences. "
            "Score 0..1 where 1 = certainly the same entity, 0 = certainly different. "
            "Return your verdict via the record_verdict tool. Do not reply with prose."
        ),
    },
    "fuzzy-name-state-v1": {
        "system": (
            "You evaluate whether an EPA-regulated record and a SAM.gov registered "
            "entity refer to the same real-world organization. They share a normalized "
            "name + state but no canonical ID. Use the address fields (city, zip, "
            "county, street) to disambiguate. The EPA side is sometimes a facility "
            "name (a specific site) - distinguish 'same parent entity, different site' "
            "from 'same site' carefully. The match_granularity field tells you whether "
            "the EPA name is entity-level or facility-level. "
            "Score 0..1 where 1 = certainly the same real-world organization, "
            "0 = certainly different. Return your verdict via the record_verdict tool. "
            "Do not reply with prose."
        ),
    },
}


# --------------------------------------------------------------------------- #
# Dataset configs
# --------------------------------------------------------------------------- #
#
# Polymorphism lives here: every supported source MV is one entry in this map.
# Adding a new match MV is purely additive — no script changes elsewhere.

@dataclass(frozen=True)
class DatasetConfig:
    key: str                        # CLI key, e.g. "epa-sam-low-tier"
    source_mv_fqn: str              # e.g. "entities.mv_epa_to_sam_name_state_matches"
    row_pk_columns: list[str]       # composite PK column names in the source MV
    candidate_where: str            # SQL WHERE for flagged rows in source MV
    prompt_template: str            # key into PROMPT_TEMPLATES
    fields_to_send: list[str]       # columns to render into the user prompt


DATASET_CONFIGS: dict[str, DatasetConfig] = {
    "hmda-gleif-name-diverges": DatasetConfig(
        key="hmda-gleif-name-diverges",
        source_mv_fqn="entities.mv_hmda_to_gleif_lei_resolution",
        row_pk_columns=["hmda_source", "hmda_lei", "hmda_dataset_year", "gleif_lei"],
        candidate_where="'name_diverges_review' = ANY(match_reasons)",
        prompt_template="lei-canonical-v1",
        fields_to_send=[
            "hmda_source", "hmda_lei", "hmda_respondent_name",
            "hmda_state", "hmda_city",
            "gleif_lei", "gleif_legal_name", "gleif_jurisdiction",
            "gleif_registration_status", "gleif_legal_address",
        ],
    ),
    "epa-sam-low-tier": DatasetConfig(
        key="epa-sam-low-tier",
        source_mv_fqn="entities.mv_epa_to_sam_name_state_matches",
        row_pk_columns=["epa_source", "epa_record_id", "sam_uei"],
        candidate_where="(confidence_tier = 'low' OR match_granularity = 'entity_or_individual')",
        prompt_template="fuzzy-name-state-v1",
        fields_to_send=[
            "epa_source", "match_granularity", "epa_record_id",
            "epa_name", "epa_name_field", "epa_city_lower", "epa_zip5",
            "epa_county", "epa_street", "state_lower",
            "sam_uei", "sam_legal_business_name", "sam_dba_name",
            "sam_city", "sam_zip", "sam_primary_naics",
        ],
    ),
}


# --------------------------------------------------------------------------- #
# Tool schema — Anthropic forces this with tool_choice
# --------------------------------------------------------------------------- #

RECORD_VERDICT_TOOL: dict[str, Any] = {
    "name": "record_verdict",
    "description": (
        "Record a verdict on whether two records refer to the same entity. "
        "Always call this exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["match", "mismatch", "ambiguous"],
                "description": (
                    "match = same entity, mismatch = different entities, "
                    "ambiguous = insufficient evidence either way."
                ),
            },
            "score": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence the records refer to the same entity. 1 = certainly same, 0 = certainly different.",
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation citing the specific fields that drove the verdict.",
            },
        },
        "required": ["verdict", "score", "reasoning"],
    },
}


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _direct_database_url() -> str:
    """Use direct (not pooled) for compatibility with long-running batch reads."""
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("Neither DEX_DB_URL_DIRECT nor DEX_DB_URL_POOLED is set.")
    return url


def _row_pk_string(row: dict[str, Any], cfg: DatasetConfig) -> str:
    """Canonical pipe-delimited PK form: 'col=val|col=val|...'.

    Must mirror the COALESCE-based concatenation in the per-MV views in the
    migration so SQL-side joins match Python-side rescore inserts.
    """
    parts = []
    for col in cfg.row_pk_columns:
        v = row.get(col)
        parts.append(f"{col}={'' if v is None else v}")
    return "|".join(parts)


def _row_pk_json(row: dict[str, Any], cfg: DatasetConfig) -> dict[str, Any]:
    return {col: row.get(col) for col in cfg.row_pk_columns}


def fetch_candidates(
    conn: psycopg.Connection,
    cfg: DatasetConfig,
    *,
    model: str,
    skip_already_processed: bool,
    max_rows: int | None,
) -> list[dict[str, Any]]:
    """Read candidate rows from the source MV, optionally minus already-rescored.

    Schema-qualified per memory feedback (entities.*).
    """
    select_cols = list(dict.fromkeys(cfg.row_pk_columns + cfg.fields_to_send))
    cols_sql = ", ".join(f"m.{c}" for c in select_cols)

    if skip_already_processed:
        # Anti-join to entities.match_llm_rescores on the canonical row PK.
        pk_concat_parts = []
        for col in cfg.row_pk_columns:
            pk_concat_parts.append(f"COALESCE(m.{col}::text, '')")
        pk_concat_sql = " || '|' || ".join(
            f"'{col}=' || COALESCE(m.{col}::text, '')" for col in cfg.row_pk_columns
        )

        sql = f"""
            SELECT {cols_sql}
            FROM {cfg.source_mv_fqn} m
            WHERE {cfg.candidate_where}
              AND NOT EXISTS (
                  SELECT 1
                  FROM entities.match_llm_rescores r
                  WHERE r.source_mv_fqn = %(fqn)s
                    AND r.llm_model = %(model)s
                    AND r.prompt_template = %(prompt_template)s
                    AND r.source_mv_row_pk = ({pk_concat_sql})
              )
        """
    else:
        sql = f"""
            SELECT {cols_sql}
            FROM {cfg.source_mv_fqn} m
            WHERE {cfg.candidate_where}
        """

    if max_rows:
        sql += f" LIMIT {int(max_rows)}"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            sql,
            {"fqn": cfg.source_mv_fqn, "model": model, "prompt_template": cfg.prompt_template},
        )
        return list(cur.fetchall())


def insert_rescores(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
) -> int:
    """Idempotent insert: ON CONFLICT DO NOTHING on the unique constraint.

    `rows` is a list of dicts shaped to the entities.match_llm_rescores schema.
    Returns the number of rows successfully inserted (excludes conflicts).
    """
    if not rows:
        return 0

    insert_sql = """
        INSERT INTO entities.match_llm_rescores (
            source_mv_fqn, source_mv_row_pk, source_mv_row_pk_json,
            llm_model, prompt_template,
            verdict, score, reasoning,
            prompt_input, api_response_id, batch_id,
            input_tokens, output_tokens
        ) VALUES (
            %(source_mv_fqn)s, %(source_mv_row_pk)s, %(source_mv_row_pk_json)s,
            %(llm_model)s, %(prompt_template)s,
            %(verdict)s, %(score)s, %(reasoning)s,
            %(prompt_input)s, %(api_response_id)s, %(batch_id)s,
            %(input_tokens)s, %(output_tokens)s
        )
        ON CONFLICT (source_mv_fqn, source_mv_row_pk, llm_model, prompt_template)
        DO NOTHING
    """

    inserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), DB_INSERT_CHUNK):
            chunk = rows[i:i + DB_INSERT_CHUNK]
            for r in chunk:
                cur.execute(insert_sql, r)
                inserted += cur.rowcount
            conn.commit()
    return inserted


# --------------------------------------------------------------------------- #
# Prompt rendering
# --------------------------------------------------------------------------- #


def render_user_prompt(row: dict[str, Any], cfg: DatasetConfig) -> str:
    """Deterministic JSON-ish rendering of the row's relevant fields.

    Stable ordering matters for reproducibility: re-renders of the same row
    produce identical text, which keeps cache hit rates high.
    """
    lines = []
    for col in cfg.fields_to_send:
        v = row.get(col)
        if v is None:
            continue
        if isinstance(v, list):
            v = "[" + ", ".join(str(x) for x in v) + "]"
        lines.append(f"  {col}: {v}")
    body = "\n".join(lines)
    return (
        "Evaluate whether the following two source records refer to the same entity. "
        "Use the record_verdict tool to return your answer.\n\n"
        f"Record fields:\n{body}\n"
    )


def estimate_tokens(text: str) -> int:
    """Rough approximation: ~4 chars per token for English. Used for budgeting."""
    return max(1, len(text) // 4)


def estimate_cost_usd(
    candidate_count: int,
    avg_user_prompt_tokens: int,
    system_prompt_tokens: int,
    *,
    mode: str,
    expected_output_tokens: int = 80,
) -> dict[str, float]:
    """Estimated cost using prompt caching (system cached after first call)."""
    cached_input_tokens = system_prompt_tokens * candidate_count
    fresh_input_tokens = (avg_user_prompt_tokens + 50) * candidate_count  # +50 tool-def slack
    output_tokens = expected_output_tokens * candidate_count

    cached_in_cost = (
        cached_input_tokens / 1_000_000
        * PRICE_INPUT_PER_MTOK
        * CACHED_INPUT_DISCOUNT
    )
    fresh_in_cost = fresh_input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
    out_cost = output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK

    subtotal = cached_in_cost + fresh_in_cost + out_cost
    if mode == "batch":
        subtotal *= BATCH_DISCOUNT

    return {
        "subtotal_usd": round(subtotal, 4),
        "cached_input_tokens": cached_input_tokens,
        "fresh_input_tokens": fresh_input_tokens,
        "output_tokens_est": output_tokens,
    }


# --------------------------------------------------------------------------- #
# Anthropic — sync
# --------------------------------------------------------------------------- #


def _client() -> Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment.")
    return Anthropic()


def _system_block(template_key: str) -> list[dict[str, Any]]:
    """System prompt as a single cached block. Anthropic caches by exact bytes,
    so the system text must be identical across calls in a batch."""
    return [
        {
            "type": "text",
            "text": PROMPT_TEMPLATES[template_key]["system"],
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _extract_tool_use(
    msg: anthropic.types.Message,
) -> tuple[str, float, str] | None:
    """Find the first record_verdict tool_use block; return (verdict, score, reasoning)."""
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "record_verdict":
            args = block.input or {}
            verdict = args.get("verdict")
            score = args.get("score")
            reasoning = args.get("reasoning") or ""
            if verdict in ("match", "mismatch", "ambiguous") and isinstance(score, (int, float)):
                return verdict, float(score), reasoning
    return None


def call_sync(
    client: Anthropic,
    *,
    model: str,
    system_blocks: list[dict[str, Any]],
    user_prompt: str,
    max_retries: int = 3,
) -> tuple[anthropic.types.Message, tuple[str, float, str] | None]:
    """One sync call with bounded retries on rate-limit / overload."""
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=512,
                system=system_blocks,
                tools=[RECORD_VERDICT_TOOL],
                tool_choice={"type": "tool", "name": "record_verdict"},
                messages=[{"role": "user", "content": user_prompt}],
            )
            return msg, _extract_tool_use(msg)
        except APIStatusError as e:
            last_exc = e
            if e.status_code in (429, 500, 502, 503, 529) and attempt < max_retries:
                wait = min(2 ** attempt, 30)
                log.warning("Anthropic %s; retry %s/%s in %ss", e.status_code, attempt, max_retries, wait)
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("call_sync: exhausted retries without raising")


# --------------------------------------------------------------------------- #
# Anthropic — batch
# --------------------------------------------------------------------------- #


def submit_batch(
    client: Anthropic,
    *,
    model: str,
    template_key: str,
    candidates: list[dict[str, Any]],
    cfg: DatasetConfig,
) -> tuple[str, dict[str, str]]:
    """Submit a batch and return (batch_id, custom_id->row_pk_string)."""
    system_blocks = _system_block(template_key)
    requests = []
    custom_id_to_pk: dict[str, str] = {}

    for i, row in enumerate(candidates):
        custom_id = f"row-{i:08d}"
        pk_str = _row_pk_string(row, cfg)
        custom_id_to_pk[custom_id] = pk_str

        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 512,
                "system": system_blocks,
                "tools": [RECORD_VERDICT_TOOL],
                "tool_choice": {"type": "tool", "name": "record_verdict"},
                "messages": [
                    {"role": "user", "content": render_user_prompt(row, cfg)},
                ],
            },
        })

    log.info("Submitting batch of %d requests (model=%s)", len(requests), model)
    batch = client.messages.batches.create(requests=requests)
    log.info("Batch submitted: id=%s status=%s", batch.id, batch.processing_status)
    return batch.id, custom_id_to_pk


def poll_batch_until_done(
    client: Anthropic, batch_id: str
) -> anthropic.types.messages.MessageBatch:
    """Poll until processing_status == 'ended'. Hard timeout at BATCH_TIMEOUT_SEC."""
    started = time.monotonic()
    last_status = None
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status != last_status:
            log.info(
                "Batch %s status=%s counts=%s",
                batch_id, batch.processing_status, batch.request_counts,
            )
            last_status = batch.processing_status
        if batch.processing_status == "ended":
            return batch
        if time.monotonic() - started > BATCH_TIMEOUT_SEC:
            raise TimeoutError(f"Batch {batch_id} did not complete within {BATCH_TIMEOUT_SEC}s")
        time.sleep(BATCH_POLL_INTERVAL_SEC)


def collect_batch_results(
    client: Anthropic, batch_id: str
) -> list[anthropic.types.messages.MessageBatchIndividualResponse]:
    return list(client.messages.batches.results(batch_id))


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dataset", help="Dataset key (e.g. epa-sam-low-tier) or 'all'.")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model (default: {DEFAULT_MODEL}).")
    p.add_argument("--max-rows", type=int, default=None, help="Cap candidates for smoke tests.")
    p.add_argument("--mode", choices=["sync", "batch"], default="sync", help="API mode.")
    p.add_argument("--dry-run", action="store_true", help="Render prompts + estimate cost; no API calls, no DB writes.")
    p.add_argument("--skip-already-processed", action="store_true", default=True,
                   help="Skip rows already rescored for this (model, prompt_template). Default: True.")
    p.add_argument("--no-skip-already-processed", dest="skip_already_processed", action="store_false")
    p.add_argument("--confirm-cost", action="store_true",
                   help="Acknowledge cost > $%d and proceed." % int(CONFIRM_COST_THRESHOLD_USD))
    return p.parse_args()


def datasets_for(arg: str) -> list[DatasetConfig]:
    if arg == "all":
        return list(DATASET_CONFIGS.values())
    if arg not in DATASET_CONFIGS:
        raise SystemExit(f"unknown dataset key {arg!r}; choices: {list(DATASET_CONFIGS) + ['all']}")
    return [DATASET_CONFIGS[arg]]


def run_dataset_sync(
    conn: psycopg.Connection,
    client: Anthropic,
    cfg: DatasetConfig,
    *,
    model: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sync mode: one HTTP call per row. Per-row insert; chunked commit."""
    template_key = cfg.prompt_template
    system_blocks = _system_block(template_key)

    inserted = 0
    parse_failures = 0
    api_failures = 0
    started = time.monotonic()
    rows_to_insert: list[dict[str, Any]] = []

    for i, row in enumerate(candidates):
        user_prompt = render_user_prompt(row, cfg)
        try:
            msg, parsed = call_sync(
                client,
                model=model,
                system_blocks=system_blocks,
                user_prompt=user_prompt,
            )
        except Exception as e:  # pragma: no cover — bubble bounded to rate-limit / network
            log.error("sync call failed on row %d: %s", i, e)
            api_failures += 1
            continue

        if parsed is None:
            parse_failures += 1
            log.warning("row %d: tool_use missing or malformed; stop_reason=%s", i, msg.stop_reason)
            continue

        verdict, score, reasoning = parsed
        rows_to_insert.append({
            "source_mv_fqn": cfg.source_mv_fqn,
            "source_mv_row_pk": _row_pk_string(row, cfg),
            "source_mv_row_pk_json": Jsonb(_row_pk_json(row, cfg)),
            "llm_model": model,
            "prompt_template": template_key,
            "verdict": verdict,
            "score": score,
            "reasoning": reasoning,
            "prompt_input": Jsonb({k: row.get(k) for k in cfg.fields_to_send}),
            "api_response_id": msg.id,
            "batch_id": None,
            "input_tokens": getattr(msg.usage, "input_tokens", None),
            "output_tokens": getattr(msg.usage, "output_tokens", None),
        })

        if len(rows_to_insert) >= 50:
            inserted += insert_rescores(conn, rows_to_insert)
            rows_to_insert = []

    if rows_to_insert:
        inserted += insert_rescores(conn, rows_to_insert)

    elapsed = time.monotonic() - started
    return {
        "inserted": inserted,
        "parse_failures": parse_failures,
        "api_failures": api_failures,
        "elapsed_sec": round(elapsed, 2),
        "per_row_sec": round(elapsed / max(1, len(candidates)), 3),
    }


def run_dataset_batch(
    conn: psycopg.Connection,
    client: Anthropic,
    cfg: DatasetConfig,
    *,
    model: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Batch mode: single submit, poll, collect, persist."""
    template_key = cfg.prompt_template
    started = time.monotonic()

    batch_id, custom_id_to_pk = submit_batch(
        client,
        model=model,
        template_key=template_key,
        candidates=candidates,
        cfg=cfg,
    )

    pk_to_row = {_row_pk_string(r, cfg): r for r in candidates}

    poll_batch_until_done(client, batch_id)
    results = collect_batch_results(client, batch_id)

    inserted = 0
    parse_failures = 0
    api_failures = 0
    rows_to_insert: list[dict[str, Any]] = []

    for res in results:
        custom_id = res.custom_id
        pk_str = custom_id_to_pk.get(custom_id)
        if not pk_str:
            log.warning("orphan custom_id in batch results: %s", custom_id)
            continue
        row = pk_to_row.get(pk_str)
        if row is None:
            log.warning("orphan pk in batch results: %s", pk_str)
            continue

        result = res.result
        if result.type != "succeeded":
            api_failures += 1
            err = getattr(result, "error", None)
            log.warning("batch entry %s failed: type=%s err=%s", custom_id, result.type, err)
            continue

        msg = result.message
        parsed = _extract_tool_use(msg)
        if parsed is None:
            parse_failures += 1
            log.warning("batch entry %s: tool_use missing or malformed; stop_reason=%s",
                        custom_id, msg.stop_reason)
            continue

        verdict, score, reasoning = parsed
        rows_to_insert.append({
            "source_mv_fqn": cfg.source_mv_fqn,
            "source_mv_row_pk": pk_str,
            "source_mv_row_pk_json": Jsonb(_row_pk_json(row, cfg)),
            "llm_model": model,
            "prompt_template": template_key,
            "verdict": verdict,
            "score": score,
            "reasoning": reasoning,
            "prompt_input": Jsonb({k: row.get(k) for k in cfg.fields_to_send}),
            "api_response_id": msg.id,
            "batch_id": batch_id,
            "input_tokens": getattr(msg.usage, "input_tokens", None),
            "output_tokens": getattr(msg.usage, "output_tokens", None),
        })

        if len(rows_to_insert) >= DB_INSERT_CHUNK:
            inserted += insert_rescores(conn, rows_to_insert)
            rows_to_insert = []

    if rows_to_insert:
        inserted += insert_rescores(conn, rows_to_insert)

    elapsed = time.monotonic() - started
    return {
        "inserted": inserted,
        "parse_failures": parse_failures,
        "api_failures": api_failures,
        "batch_id": batch_id,
        "elapsed_sec": round(elapsed, 2),
        "per_1k_sec": round(elapsed / max(1, len(candidates)) * 1000, 1),
    }


def run_dataset(
    conn: psycopg.Connection,
    cfg: DatasetConfig,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log.info("=== dataset=%s mode=%s model=%s ===", cfg.key, args.mode, args.model)

    candidates = fetch_candidates(
        conn,
        cfg,
        model=args.model,
        skip_already_processed=args.skip_already_processed,
        max_rows=args.max_rows,
    )
    log.info("Candidates after skip-already-processed: %d", len(candidates))

    if not candidates:
        log.info("No candidates to process for %s. Done.", cfg.key)
        return {"dataset": cfg.key, "candidate_count": 0}

    template_key = cfg.prompt_template
    system_text = PROMPT_TEMPLATES[template_key]["system"]
    sys_tokens = estimate_tokens(system_text)

    sample = candidates[0]
    sample_user_prompt = render_user_prompt(sample, cfg)
    sample_user_tokens = estimate_tokens(sample_user_prompt)
    log.info(
        "Prompt sample: system=%d toks, user=%d toks. First-row preview:",
        sys_tokens, sample_user_tokens,
    )
    log.info("\n%s", sample_user_prompt)

    cost = estimate_cost_usd(
        candidate_count=len(candidates),
        avg_user_prompt_tokens=sample_user_tokens,
        system_prompt_tokens=sys_tokens,
        mode=args.mode,
    )
    log.info("Estimated cost: $%s (mode=%s) — %s", cost["subtotal_usd"], args.mode, cost)

    if args.dry_run:
        log.info("DRY RUN — exiting before any API call or DB write.")
        return {
            "dataset": cfg.key,
            "candidate_count": len(candidates),
            "estimated_cost_usd": cost["subtotal_usd"],
            "mode": args.mode,
            "dry_run": True,
        }

    if cost["subtotal_usd"] > CONFIRM_COST_THRESHOLD_USD and not args.confirm_cost:
        raise SystemExit(
            f"Estimated cost ${cost['subtotal_usd']} exceeds threshold "
            f"${CONFIRM_COST_THRESHOLD_USD}. Re-run with --confirm-cost to proceed."
        )

    client = _client()

    if args.mode == "sync":
        result = run_dataset_sync(conn, client, cfg, model=args.model, candidates=candidates)
    else:
        result = run_dataset_batch(conn, client, cfg, model=args.model, candidates=candidates)

    summary = {
        "dataset": cfg.key,
        "candidate_count": len(candidates),
        "estimated_cost_usd": cost["subtotal_usd"],
        "mode": args.mode,
        **result,
    }
    log.info("Dataset %s done: %s", cfg.key, summary)
    return summary


def print_recon_summary(conn: psycopg.Connection) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT source_mv_fqn, llm_model, prompt_template, verdict,
                   COUNT(*) AS rows,
                   ROUND(AVG(score)::numeric, 3) AS avg_score,
                   ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY score)::numeric, 3) AS median_score
            FROM entities.match_llm_rescores
            GROUP BY 1,2,3,4
            ORDER BY 1,2,3,4
        """)
        rows = list(cur.fetchall())
    log.info("=== Recon: rescore distribution ===")
    if not rows:
        log.info("(table empty)")
        return
    for r in rows:
        log.info(
            "  %s | %s | %s | %s: rows=%d avg=%s med=%s",
            r["source_mv_fqn"], r["llm_model"], r["prompt_template"],
            r["verdict"], r["rows"], r["avg_score"], r["median_score"],
        )


def main() -> int:
    args = parse_args()
    cfgs = datasets_for(args.dataset)

    summaries = []
    with psycopg.connect(_direct_database_url(), autocommit=False) as conn:
        for cfg in cfgs:
            summaries.append(run_dataset(conn, cfg, args))
        if not args.dry_run:
            print_recon_summary(conn)

    log.info("=== run summary ===")
    for s in summaries:
        log.info("  %s", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
