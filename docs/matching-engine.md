# Matching Engine (Phase 5 scaffold)

## What it is

A relationship-typed entity-to-entity scoring service. Every match is a
first-class object with a lifecycle (`identified → surfaced → viewed →
reserved → claimed`, or `dismissed`/`expired`). The engine is configured by
rows in `business.matching_relationships`, not by code — adding a new
relationship type is an INSERT, not a deploy.

Entities have no fixed supply/demand identity. Side is a function of the
match, not the entity. A trucking carrier is supply-side when matched to
insurance partners and demand-side when seeking cargo brokers — the engine
runs the same scoring loop for both, picked by relationship config.

## Schema

Three tables in the `business` schema (hq-x DB):

- **`business.matching_relationships`** — config rows. Each declares an
  `intent_source` (`paid_specs` | `preferences` | `both`), a
  `target_filter` (JSONB overlay applied to the candidate population), a
  `scoring_strategy` (JSONB — scalar/vector/recency weights), and a
  `surfacing_rule` (JSONB — channels, when-to-fire, operator-approval flag,
  auto-narrative requirement).

- **`business.matches`** — one row per (intent × relationship × target
  entity). Polymorphic `source_intent_id` + `intent_kind` lets paid specs
  and preferences share the table. `match_reasons` is the audit trail of
  per-attribute scalar hits, vector similarity, recency score.

- **`business.match_surfacings`** — one row per (match × channel) surfacing
  event. Lifecycle is `outcome` (`pending → sent → delivered → responded`
  for cold-email; `pending → partner_viewed → partner_acted` for portal; etc).

See `apps/hq-x/migrations/20260512T040000_matching_engine_substrate.sql`.

## Service layout

```
apps/hq-x/app/services/matching_engine/
  __init__.py
  models.py              ← Pydantic models for the 3 tables
  engine.py              ← evaluate_relationship, evaluate_all_active_relationships
  persistence.py         ← persist_match (idempotent), transition_match (graph-guarded)
  surfacing/
    __init__.py
    portal.py            ← in-platform feed
    operator_queue.py    ← operator dashboard
    cold_email_handoff.py ← emailbison webhook (STUB in v1)
```

## REST API

| Endpoint                                                | Purpose                                       |
| ------------------------------------------------------- | --------------------------------------------- |
| `GET /api/v1/matches/by-signing/{signing_id}`           | Matches for a paid signed spec                |
| `GET /api/v1/matches/by-preference/{preference_id}`     | Matches for a preference (placeholder in v1)  |
| `POST /api/v1/matches/{match_id}/transition`            | Update match lifecycle status                 |
| `GET /api/v1/operator/match-queue`                      | Operator triage queue                         |
| `POST /api/v1/operator/match-queue/{surfacing_id}/approve` | Approve a pending surfacing (flip to 'sent') |
| `POST /api/v1/operator/match-queue/{surfacing_id}/dismiss` | Dismiss a pending surfacing                   |
| `POST /api/v1/internal/matching-engine/evaluate-all`    | Trigger.dev daily cron entry                  |

All public endpoints require `require_flexible_auth` (operator JWT or
trigger shared secret). The `/internal/...` endpoint requires
`TRIGGER_SHARED_SECRET` only.

`X-Data-Lineage` is auto-stamped via the existing `LineageMiddleware`.

## Daily cron

`apps/hq-x/src/trigger/matching-engine-daily.ts` registers
`matching-engine-daily` in hq-x's Trigger.dev project
(`proj_khmvxxrpyloqmnivdetu`) on cron `0 8 * * *` UTC. The cron POSTs to
`/api/v1/internal/matching-engine/evaluate-all`, which iterates every
enabled relationship row and persists ranked matches + surfacings.

Order with Phase 4: the embedding-emit cron fires at 07:45 UTC, so the
matching engine reads from fresh embeddings at 08:00 UTC.

## How to add a new relationship type

INSERT a row into `business.matching_relationships`:

```sql
INSERT INTO business.matching_relationships (
    name, description, intent_source, target_filter,
    scoring_strategy, surfacing_rule, enabled
)
VALUES (
    'lender_borrower_discovery_v1',
    'Match small-business preferences to lender capital pools',
    'preferences',
    '{"naics_prefix": "44"}'::jsonb,
    '{"scalar_weight": 1.0, "vector_weight": 1.5, "recency_boost_weight": 0.1}'::jsonb,
    '{"channels": ["operator_queue", "cold_email_handoff"], "when": "on_match", "operator_approval_required": true, "auto_narrative": "required"}'::jsonb,
    true
);
```

No code change. Next daily cron picks it up.

## How to tune scoring weights

`UPDATE business.matching_relationships SET scoring_strategy = '...' WHERE name = '...'`.
The engine reads `scoring_strategy` fresh on every evaluation. The three
weights operate on:

- `scalar_weight × |attributes in candidate that match the spec's filter|`
- `vector_weight × cosine(query_centroid, target_embedding)`
- `recency_boost_weight × 1 / (1 + days_since_target_last_update)`

Sum is the match score. v1 uses placeholder weights `{1.0, 1.0, 0.2}` —
operator tunes empirically.

## How to extend a surfacing channel

A channel is a Python module under `app/services/matching_engine/surfacing/`
that exports `async def surface_match(match, rule, intent) -> Surfacing`.
The engine's `_apply_surfacing_rule` dispatches by name from
`surfacing_rule.channels`. To add a fourth channel (e.g., `slack_alert`):

1. Create `surfacing/slack_alert.py` exporting `surface_match`.
2. Add the channel name to the CHECK constraint in
   `business.match_surfacings.channel` via a new migration.
3. Register the handler in `engine._apply_surfacing_rule`'s `handlers` dict.
4. Update `models.SurfacingChannel` typing.

## Cold-email handoff (STUB)

`cold_email_handoff.surface_match` currently builds a placeholder narrative
and logs the would-be webhook payload — it does NOT call emailbison. The
surfacing row is persisted with `outcome='pending'`. The real webhook call
moves into the operator-approve flow:

`POST /api/v1/operator/match-queue/{surfacing_id}/approve` will (in the
production version) actually fire the emailbison webhook, set
`outcome='sent'`, and let the emailbison side drive delivery /
response signals via inbound webhooks that update the same surfacing row.

Out-of-scope follow-ups:

- LLM-generated auto-narrative (real Anthropic call against the matched
  entity's data-lake profile + partner signed spec).
- Operator UI in hq-command (REST endpoints exist; UI is follow-up).
- Real emailbison webhook integration.

## Smoke test

`apps/hq-x/scripts/smoke_matching_engine.py`. Runs against prod DB, exercises
the full evaluate-persist-surface path with a synthetic intent. Cleans up
on completion. 5 verification gates:

1. Three `business.matching_*` tables present.
2. Seed relationship row present.
3. `evaluate_relationship` persists ≥1 match.
4. ≥1 portal surfacing per match.
5. Transition-graph guard correct.

## Related cycles

- **Phase 2** (signed audience specs): `business.audience_spec_signings`
  is the canonical intent source for `intent_source = paid_specs`. v1
  scaffold tolerates that schema being absent — synthesizes a signing for
  the smoke test, returns `[]` for `load_eligible_intents` if the table
  doesn't exist.
- **Phase 3** (cohort drift): drift events flow through
  `business.audience_spec_deliveries`; orthogonal to the matching engine
  in v1.
- **Phase 4** (vector primitives): the engine reads from embedding
  datasets via the existing `app.services.audience_spec.vector_query`
  module for candidate retrieval. v1 scaffold uses a synthetic candidate
  set; tuning cycle wires the real Lance read.
