# Directive: Exa research prototype (hq-x ↔ data-engine-x)

**Context:** You are working on **two repos in parallel**: `hq-x` (orchestration + Exa client + most of the surface) and `data-engine-x` (one write endpoint + raw archive table). Read both `CLAUDE.md` files before starting.

**Scope clarification on autonomy:** You are expected to make strong engineering decisions within the scope below. What you must not do is drift outside this scope, run deploy commands, modify any router/service not explicitly listed here, touch DMaaS / Lob / Dub / Vapi / voice / SMS / Entri / brand / reservations surfaces, or build any of the explicitly out-of-scope items (derived per-objective tables, UI, cron schedulers, Exa-MCP-as-runtime). Within scope, use your best judgment.

**Background:** We want the ability to run Exa research from hq-x and persist the raw payload to either DB. The destination (`hqx` or `dex`) is a per-run flag set by the caller, not a global config. Reasoning:

- hq-x owns paying-customer-scoped research (e.g. researching a reserved customer org). That data lives in hq-x.
- data-engine-x owns dataset-building research (e.g. enriching company entities at scale). That data lives in DEX, near the entities it enriches.
- Same raw-payload shape in both DBs — one schema, two homes.

We are intentionally **not** building per-objective normalized/derived tables in this directive. Just the raw-archive layer + orchestration. Per-objective derived tables come in follow-up directives keyed to each objective.

**Critical existing-state facts (verify before building):**

- `EXA_API_KEY` is already present in both Doppler projects (`hq-x` and `data-engine-x`). Read it via `app/config.py` in each repo (add the field; do not hardcode).
- hq-x already runs Trigger.dev. Tasks live in `src/trigger/*.ts`. The closest precedent for an async-job-with-DB-callback is [src/trigger/dmaas-process-activation-job.ts](../../src/trigger/dmaas-process-activation-job.ts) — read it before writing the new task.
- hq-x's orchestration-job pattern is `business.activation_jobs` ([migrations/20260430T184850_activation_jobs.sql](../../migrations/20260430T184850_activation_jobs.sql)). Mirror its shape for the new Exa job table — same status enum semantics, same JSONB payload/result/error/history, same trigger_run_id, same idempotency-key uniqueness pattern.
- DEX accepts the super-admin API key via the standard `Authorization: Bearer <key>` header (NOT a custom `X-Super-Admin-API-Key` header). See `data-engine-x/app/auth/super_admin.py` lines 75-86.
- DEX's address-parse endpoint ([data-engine-x/app/routers/address_parse_v1.py](../../../../data-engine-x/app/routers/address_parse_v1.py)) is the pattern to copy for the new DEX write endpoint: super-admin-only, `/api/internal/...`, persists to a DEX-owned table, returns the inserted row.
- hq-x → DEX writes are an established pattern (precedent: address-parse). The DEX CLAUDE.md "read-only from hq-x's side" prohibition is the *opposite* direction (DEX writing to hq-x). hq-x writing to DEX via DEX's own endpoints is fine.
- Exa MCP is available during development for the agent to **read Exa's API docs** and confirm endpoint shapes. It is **not** in the production data path — production code calls Exa's HTTPS API directly with `EXA_API_KEY`. Use the MCP to clarify request/response shapes at implementation time, then implement against the HTTP surface.

---

## Existing code to read before starting

**Both repos:**
- `hq-x/CLAUDE.md` — migration filename convention `YYYYMMDDTHHMMSS_<slug>.sql`, DMaaS context (DO NOT touch).
- `data-engine-x/CLAUDE.md` — auth model, `supabase/migrations/NNN_*.sql` convention, no `require_m2m`.

**hq-x — patterns to copy:**
- [app/config.py](../../app/config.py) — env-driven `Settings` shape; you'll add `EXA_API_KEY: SecretStr | None`. `DEX_BASE_URL` and `DEX_SUPER_ADMIN_API_KEY` already exist (or will, from the reservations directive — check; if not present, add `DEX_SUPER_ADMIN_API_KEY: SecretStr | None`).
- [app/db.py](../../app/db.py) — async psycopg pool via `get_db_connection()`. Use this; don't open a second pool.
- [app/auth/supabase_jwt.py](../../app/auth/supabase_jwt.py) — `UserContext` with `active_organization_id`. Job creation requires an org context.
- [app/services/activation_jobs.py](../../app/services/activation_jobs.py) — orchestration-job persistence + state-transition helpers. Mirror its `_record_history`, `mark_running`, `mark_succeeded`, `mark_failed` style for the Exa job table.
- [app/routers/dmaas_campaigns.py](../../app/routers/dmaas_campaigns.py) — async-202 router pattern. The Exa router mirrors this: `POST` returns 202 + `{job_id, status}`, `GET /{id}` returns the row.
- [src/trigger/dmaas-process-activation-job.ts](../../src/trigger/dmaas-process-activation-job.ts) — TS task that calls back into hq-x's `/internal` endpoints. Mirror this for `exa-process-research-job.ts`.
- [app/routers/internal/dmaas_jobs.py](../../app/routers/internal/dmaas_jobs.py) — internal callback endpoints called by Trigger tasks. Mirror this for the Exa internal endpoints.
- [migrations/20260430T184850_activation_jobs.sql](../../migrations/20260430T184850_activation_jobs.sql) — precedent for the new `business.exa_research_jobs` table.

**data-engine-x — patterns to copy:**
- [data-engine-x/app/routers/address_parse_v1.py](../../../../data-engine-x/app/routers/address_parse_v1.py) — exact pattern for the new DEX write endpoint: super-admin-only, `/api/internal/...`, persists to its own DB.
- [data-engine-x/app/services/address_parse.py](../../../../data-engine-x/app/services/address_parse.py) — sync psycopg pool + `dict_row` style for the DEX-side persistence helper.
- `data-engine-x/supabase/migrations/138_audience_resolver_view.sql` (most recent migration) — confirm the next migration number to use (likely `139_*` or higher; verify by listing the migrations dir).

---

## Build 1 (both DBs): Raw-archive table

**Schema is identical in both DBs.** Pick a new schema namespace `exa.` so the surface is clearly separated.

### File 1a: hq-x migration

`migrations/<UTC_TIMESTAMP>_exa_calls.sql` (new — generate timestamp at write time)

```sql
CREATE SCHEMA IF NOT EXISTS exa;

CREATE TABLE IF NOT EXISTS exa.exa_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint TEXT NOT NULL CHECK (endpoint IN (
        'search', 'contents', 'find_similar', 'research', 'answer'
    )),
    request_payload JSONB NOT NULL,
    response_payload JSONB,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'succeeded', 'failed'
    )),
    error TEXT,
    -- Echoed from Exa response when present.
    exa_request_id TEXT,
    cost_dollars NUMERIC(10, 6),
    duration_ms INTEGER,
    -- Lightweight, untyped backref so consumers can find their data.
    -- Convention: '<resource>:<id>' e.g. 'reservation:abc-123', 'company:dot:12345'.
    objective TEXT NOT NULL,
    objective_ref TEXT,
    -- Pointer back to the orchestrating hq-x job id for joinability across
    -- DBs. Same UUID lives in business.exa_research_jobs.id in hq-x.
    triggered_by_job_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_exa_calls_objective
    ON exa.exa_calls (objective, objective_ref);
CREATE INDEX IF NOT EXISTS idx_exa_calls_job
    ON exa.exa_calls (triggered_by_job_id);
CREATE INDEX IF NOT EXISTS idx_exa_calls_created
    ON exa.exa_calls (created_at DESC);
```

### File 1b: DEX migration

`data-engine-x/supabase/migrations/<NEXT_NUM>_exa_calls.sql` (new — confirm next number; e.g. `139_exa_calls.sql`)

**Identical SQL** to hq-x's migration. Same schema name `exa`, same table, same indexes. The two DBs intentionally share this shape.

---

## Build 2 (hq-x): Exa client

**File:** `app/services/exa_client.py` (new)

Thin async client wrapping Exa's HTTP API. Methods, one per supported endpoint:

```python
async def search(*, query: str, num_results: int = 10, **kwargs: Any) -> dict[str, Any]:
    """POST https://api.exa.ai/search"""

async def contents(*, urls: list[str], **kwargs: Any) -> dict[str, Any]:
    """POST https://api.exa.ai/contents"""

async def find_similar(*, url: str, num_results: int = 10, **kwargs: Any) -> dict[str, Any]:
    """POST https://api.exa.ai/findSimilar"""

async def research(*, instructions: str, **kwargs: Any) -> dict[str, Any]:
    """POST https://api.exa.ai/research/v0/tasks (or current research endpoint).
    Long-running. The client itself is sync-call-style; if Exa's research is
    poll-based, implement polling internally and return the final result.
    Use the Exa MCP to confirm the exact endpoint path and async semantics
    before implementing this method."""

async def answer(*, query: str, **kwargs: Any) -> dict[str, Any]:
    """POST https://api.exa.ai/answer"""
```

Requirements:
- `httpx.AsyncClient`, `timeout=60.0` for short endpoints, `timeout=600.0` (10 min) for `research`.
- Reads `settings.EXA_API_KEY`; raises `ExaNotConfiguredError` if unset.
- Sends `x-api-key: <EXA_API_KEY>` header (Exa's actual auth header — confirm via Exa MCP / docs before implementing).
- Returns the parsed JSON response unchanged. Do NOT unwrap or transform — the raw payload is what we persist.
- On non-2xx, raises `ExaCallError(status_code, body, endpoint)`.
- Module-level `_get_client()` lazy singleton is fine; or per-call `async with httpx.AsyncClient(...)` — either works.
- Each method also returns a `_meta` envelope alongside the response: `{"duration_ms": int, "exa_request_id": str | None, "cost_dollars": float | None}` extracted from headers/body. The Trigger task uses this when persisting.

Add to `app/config.py`:

```python
EXA_API_KEY: SecretStr | None = None
EXA_API_BASE: str = "https://api.exa.ai"
```

Place near the other provider keys.

---

## Build 3 (hq-x): Job table + service

### File 3a: Migration

`migrations/<UTC_TIMESTAMP>_exa_research_jobs.sql` (new)

```sql
CREATE TABLE business.exa_research_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
    created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL,
    endpoint TEXT NOT NULL CHECK (endpoint IN (
        'search', 'contents', 'find_similar', 'research', 'answer'
    )),
    destination TEXT NOT NULL CHECK (destination IN ('hqx', 'dex')),
    objective TEXT NOT NULL,
    objective_ref TEXT,
    request_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead_lettered'
    )),
    -- Pointer back to wherever the raw call landed.
    -- 'hqx://exa.exa_calls/<uuid>' or 'dex://exa.exa_calls/<uuid>'.
    result_ref TEXT,
    error JSONB,
    history JSONB NOT NULL DEFAULT '[]'::jsonb,
    trigger_run_id TEXT,
    idempotency_key TEXT,
    attempts INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_erj_org_idempotency
    ON business.exa_research_jobs (organization_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_erj_status ON business.exa_research_jobs (status);
CREATE INDEX idx_erj_org_created
    ON business.exa_research_jobs (organization_id, created_at DESC);
CREATE INDEX idx_erj_objective
    ON business.exa_research_jobs (objective, objective_ref);
```

### File 3b: Service

`app/services/exa_research_jobs.py` (new)

Mirror [app/services/activation_jobs.py](../../app/services/activation_jobs.py) closely. Functions:

```python
async def create_job(
    *,
    organization_id: UUID,
    created_by_user_id: UUID | None,
    endpoint: str,
    destination: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any]: ...

async def get_job(job_id: UUID, *, organization_id: UUID) -> dict[str, Any] | None: ...

async def mark_running(job_id: UUID, trigger_run_id: str) -> None: ...
async def mark_succeeded(job_id: UUID, result_ref: str) -> None: ...
async def mark_failed(job_id: UUID, error: dict[str, Any]) -> None: ...
async def append_history(job_id: UUID, event: dict[str, Any]) -> None: ...
```

Idempotency: if `idempotency_key` is set and `(organization_id, idempotency_key)` already exists, return the existing row instead of inserting (same semantics as `activation_jobs`).

---

## Build 4 (hq-x): Persistence helper for `exa.exa_calls`

**File:** `app/services/exa_call_persistence.py` (new)

Two functions — one for each destination.

```python
async def persist_exa_call_local(
    *,
    job_id: UUID,
    endpoint: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    status: str,
    error: str | None,
    exa_request_id: str | None,
    cost_dollars: float | None,
    duration_ms: int | None,
) -> UUID:
    """INSERT into hq-x's own exa.exa_calls. Returns the new row id."""

async def persist_exa_call_to_dex(
    *,
    job_id: UUID,
    endpoint: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    status: str,
    error: str | None,
    exa_request_id: str | None,
    cost_dollars: float | None,
    duration_ms: int | None,
) -> UUID:
    """POST to DEX's /api/internal/exa/calls with super-admin bearer.
    Returns the new row id from DEX's response."""
```

The DEX path uses `httpx.AsyncClient` with `Authorization: Bearer <DEX_SUPER_ADMIN_API_KEY>`. Base URL from `settings.DEX_BASE_URL`.

---

## Build 5 (hq-x): Public router

**File:** `app/routers/exa_jobs.py` (new), prefix `/api/v1/exa`

Two endpoints. Auth: `verify_supabase_jwt` (org-scoped).

### `POST /api/v1/exa/jobs`

```python
class CreateExaJobRequest(BaseModel):
    endpoint: Literal["search", "contents", "find_similar", "research", "answer"]
    destination: Literal["hqx", "dex"]
    objective: str = Field(min_length=1, max_length=200)
    objective_ref: str | None = Field(default=None, max_length=400)
    request_payload: dict[str, Any]   # forwarded to Exa as-is
    idempotency_key: str | None = None
    model_config = {"extra": "forbid"}
```

Logic:
1. Resolve `org_id = user.active_organization_id`. 400 `organization_required` if None.
2. Call `exa_research_jobs.create_job(...)`. If idempotent hit, return the existing row with status code 200; else 202.
3. Enqueue Trigger.dev task `exa-process-research-job` with `{job_id}` payload via the existing Trigger API client (look for a `trigger_client.py` or equivalent in hq-x's `app/services/`; if absent, use `httpx` against `settings.TRIGGER_API_BASE_URL` + `settings.TRIGGER_API_KEY` — see how `dmaas_campaigns.py` does this and copy that helper).
4. Response shape: `{"job_id": "...", "status": "queued"}`.

### `GET /api/v1/exa/jobs/{job_id}`

- 404 if row not found OR `organization_id != user.active_organization_id`.
- Returns the full job row including `result_ref` (use this as the pointer to fetch the actual Exa payload from whichever DB).

Add to [app/main.py](../../app/main.py):

```python
from app.routers import exa_jobs as exa_jobs_router
# ...
app.include_router(exa_jobs_router.router)
```

Place near the other `app.include_router(...)` lines. Do not modify any other line of main.py.

---

## Build 6 (hq-x): Internal callback endpoints

**File:** `app/routers/internal/exa_jobs.py` (new), prefix `/internal/exa`

Auth: shared-secret bearer (re-use whatever auth pattern `app/routers/internal/dmaas_jobs.py` uses — likely `TRIGGER_SHARED_SECRET`-based; copy that dependency).

### `POST /internal/exa/jobs/{job_id}/process`

The Trigger.dev task POSTs here to actually run the work. Logic:

1. Load job row. If `status != 'queued'`, return early (idempotent re-entry).
2. `mark_running(job_id, trigger_run_id_from_body)`.
3. Dispatch to `exa_client.<endpoint>(...)` with `request_payload`.
4. On success:
   - If `destination == 'hqx'`: call `persist_exa_call_local(...)`, get `exa_call_id`.
   - If `destination == 'dex'`: call `persist_exa_call_to_dex(...)`, get `exa_call_id`.
   - `mark_succeeded(job_id, result_ref=f"{destination}://exa.exa_calls/{exa_call_id}")`.
5. On failure: `mark_failed(job_id, error={...})`. Persist a row with `status='failed'` to whichever destination so failures are visible there too.
6. Return `{"status": "succeeded" | "failed", "result_ref": "...", "error": ...}`.

Wire into [app/main.py](../../app/main.py):

```python
from app.routers.internal import exa_jobs as internal_exa_jobs
# ...
app.include_router(internal_exa_jobs.router, prefix="/internal")
```

---

## Build 7 (hq-x): Trigger.dev task

**File:** `src/trigger/exa-process-research-job.ts` (new)

Mirror [src/trigger/dmaas-process-activation-job.ts](../../src/trigger/dmaas-process-activation-job.ts) closely:

- Task id: `exa.process_research_job`.
- Input: `{ jobId: string }`.
- POSTs to `/internal/exa/jobs/{jobId}/process` with the shared-secret bearer.
- Retries: same retry config as `dmaas-process-activation-job.ts`. The internal endpoint is idempotent (re-entry on non-queued job is a no-op).
- Logs the result_ref or error.

Confirm the task is registered in `src/trigger/trigger.config.ts` (or whatever the project uses to enumerate tasks) — copy whatever the activation-job task does.

---

## Build 8 (data-engine-x): Internal write endpoint

### File 8a: Router

`data-engine-x/app/routers/exa_internal_v1.py` (new)

```python
"""POST /api/internal/exa/calls — hq-x writes raw Exa call payloads here.

Mirrors the address-parse pattern: super-admin-only, persists to DEX's own
DB, returns the inserted row. The orchestration lives in hq-x; this is a
thin write surface so DEX-destination Exa runs can land their data in DEX
near the entities they enrich.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.models import SuperAdminContext
from app.auth.super_admin import get_current_super_admin
from app.routers._responses import DataEnvelope

router = APIRouter()


class ExaCallWriteRequest(BaseModel):
    job_id: UUID
    endpoint: str = Field(pattern="^(search|contents|find_similar|research|answer)$")
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None = None
    status: str = Field(pattern="^(pending|succeeded|failed)$")
    error: str | None = None
    exa_request_id: str | None = None
    cost_dollars: float | None = None
    duration_ms: int | None = None
    objective: str = Field(min_length=1, max_length=200)
    objective_ref: str | None = Field(default=None, max_length=400)


@router.post("/exa/calls", response_model=DataEnvelope)
async def write_exa_call_endpoint(
    payload: ExaCallWriteRequest,
    _: SuperAdminContext = Depends(get_current_super_admin),
):
    from app.services.exa_calls import persist_exa_call

    result = persist_exa_call(payload.model_dump())
    return DataEnvelope(data=result)
```

### File 8b: Service

`data-engine-x/app/services/exa_calls.py` (new)

Mirror `app/services/address_parse.py` style: sync psycopg `ConnectionPool`, `dict_row`, `_get_pool()` lazy singleton.

```python
def persist_exa_call(payload: dict[str, Any]) -> dict[str, Any]:
    """INSERT into exa.exa_calls and return the new row id + created_at."""
```

Use `INSERT ... RETURNING id, created_at`. Set `completed_at = NOW()` when `status` is `succeeded` or `failed`.

### File 8c: Mount

Mount the router in DEX's `app/main.py`. Read it first; copy whatever existing `/api/internal/...` routers do (e.g. how `address_parse_v1` is mounted) — match the prefix and tag conventions exactly.

---

## Build 9 (both repos): Tests

**hq-x:**

`tests/test_exa_client.py` (new):
1. `test_client_sends_api_key_header` — assert `x-api-key` header present (use the actual header name confirmed via Exa MCP).
2. `test_client_raises_when_api_key_missing` — `ExaNotConfiguredError`.
3. `test_client_raises_on_non_2xx` — `ExaCallError` with status code + body.
4. `test_client_returns_meta_envelope` — `_meta.duration_ms` and `_meta.exa_request_id` populated correctly.

`tests/test_exa_research_jobs_router.py` (new):
1. `test_create_job_returns_202_and_enqueues_task` — mock Trigger client, assert POST returns 202, row exists in DB, Trigger task was enqueued with `{jobId}`.
2. `test_create_job_idempotent_returns_existing` — same `idempotency_key` returns same `job_id`, no duplicate Trigger enqueue.
3. `test_create_job_no_org_returns_400`.
4. `test_get_job_cross_org_returns_404`.
5. `test_internal_process_dispatches_to_exa_and_persists_local` — mock `exa_client.search`, destination=hqx, assert row in `exa.exa_calls` and `result_ref` populated as `hqx://exa.exa_calls/<id>`.
6. `test_internal_process_dispatches_to_exa_and_persists_to_dex` — mock `exa_client.search` AND `httpx` POST to DEX, destination=dex, assert DEX call made with super-admin bearer and `result_ref` is `dex://exa.exa_calls/<id>`.
7. `test_internal_process_marks_failed_on_exa_error` — `exa_client` raises → row in destination DB has `status=failed`, job status `failed`, error captured.

**data-engine-x:**

`data-engine-x/tests/test_exa_internal_v1.py` (new):
1. `test_write_endpoint_requires_super_admin` — no auth → 401; non-super-admin JWT → 401.
2. `test_write_endpoint_inserts_row` — happy path, row appears in `exa.exa_calls`.
3. `test_write_endpoint_validates_endpoint_enum` — bad endpoint string → 422.
4. `test_write_endpoint_persists_failed_status` — `status=failed` with error string lands in the row.

---

## Build 10 (hq-x): Seed/exercise script

**File:** `scripts/seed_exa_research_demo.py` (new)

End-to-end smoke test. Reads from Doppler at runtime.

1. Connect to hq-x DB. Look up or create a test organization (e.g. `slug='exa-demo'`).
2. Create two jobs via the public API endpoint (mock the user JWT — easiest is to use a test super-admin shortcut, or directly call `exa_research_jobs.create_job` then enqueue manually):
   - Job A: `endpoint='search'`, `destination='hqx'`, `objective='demo_customer_research'`, `request_payload={"query": "DAT trucking software competitors", "num_results": 5}`.
   - Job B: `endpoint='search'`, `destination='dex'`, `objective='demo_dataset_enrichment'`, `request_payload={"query": "fast-growing FMCSA motor carriers 2026", "num_results": 5}`.
3. Wait for both jobs to complete (poll `GET /api/v1/exa/jobs/{id}` until `status` is terminal; timeout 120s).
4. Print:
   - Job A: result_ref, then SELECT from hq-x `exa.exa_calls` showing row exists, sample of `response_payload->>'results'[0]`.
   - Job B: result_ref, then SELECT from DEX `exa.exa_calls` (via psycopg using `DEX_DB_URL_POOLED` — read from doppler at script start) showing row exists, sample.
5. Exit 0 on both succeeded; non-zero with descriptive message otherwise.

Run via:

```bash
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_exa_research_demo
```

Document the script's existence in `hq-x/CLAUDE.md` under a new section "Exa research prototype" with the doppler command.

---

## What NOT to do

- Do **not** build per-objective derived tables (e.g. `customer_research_summaries`, `entities.exa_company_research`). Those come per-use-case in follow-up directives.
- Do **not** wire the Exa MCP into any production code path. The MCP is for development-time clarification of API shapes only.
- Do **not** modify any existing migration, router, or service except as explicitly listed (additions to `app/main.py` in both repos, new files only).
- Do **not** touch DMaaS / Lob / Dub / Vapi / voice / SMS / Entri / brand / reservations / audience-drafts surfaces.
- Do **not** add cron / recurring research scheduling. One-shot jobs only in this prototype.
- Do **not** introduce a new auth pattern between hq-x and DEX. Reuse the super-admin API key bearer.
- Do **not** restore `require_m2m`, `M2MContext`, `aux_m2m_server`, or `AUX_*` env vars in DEX (intentionally removed in Phase 4).
- Do **not** re-expose the Exa API as a passthrough HTTP endpoint to external callers. The only public surface is `POST /api/v1/exa/jobs` (org-authenticated, async). Exa's API key never leaves the server.
- Do **not** persist any per-user PII into `exa.exa_calls` beyond what the caller's `request_payload` and Exa's response already contain. Treat the table as a request/response audit log, not a place to staple identity data.
- Do **not** add a third destination ("both", "fanout", etc.). Exactly two: `hqx` or `dex`.
- Do **not** unwrap or transform Exa's response shape inside the client. Persist the raw payload; transformation is a per-objective consumer concern.

---

## Scope

Files to create or modify:

**hq-x:**
- `migrations/<UTC_TIMESTAMP>_exa_calls.sql` (new)
- `migrations/<UTC_TIMESTAMP>_exa_research_jobs.sql` (new)
- `app/config.py` (modify — add `EXA_API_KEY`, `EXA_API_BASE`; ensure `DEX_SUPER_ADMIN_API_KEY` present)
- `app/services/exa_client.py` (new)
- `app/services/exa_research_jobs.py` (new)
- `app/services/exa_call_persistence.py` (new)
- `app/routers/exa_jobs.py` (new)
- `app/routers/internal/exa_jobs.py` (new)
- `app/main.py` (modify — 4 lines: 2 imports + 2 include_router)
- `src/trigger/exa-process-research-job.ts` (new)
- `src/trigger/trigger.config.ts` (modify if task registration is needed there — match existing pattern)
- `scripts/seed_exa_research_demo.py` (new)
- `tests/test_exa_client.py` (new)
- `tests/test_exa_research_jobs_router.py` (new)
- `CLAUDE.md` (modify — append a short "Exa research prototype" section with the doppler seed command)

**data-engine-x:**
- `supabase/migrations/<NEXT_NUM>_exa_calls.sql` (new)
- `app/routers/exa_internal_v1.py` (new)
- `app/services/exa_calls.py` (new)
- `app/main.py` (modify — 2 lines: import + include_router)
- `tests/test_exa_internal_v1.py` (new)

**Two commits, one per repo. Do not push either.**

Commit messages:

`hq-x`:
> feat(exa): research orchestration with destination-per-run
>
> Add `business.exa_research_jobs` orchestration table and `exa.exa_calls`
> raw archive. Public `POST /api/v1/exa/jobs` (async-202) enqueues a
> Trigger.dev task that calls Exa's HTTP API and persists the raw payload
> to either hq-x's own DB or to data-engine-x via its internal write
> endpoint, based on the per-run `destination` flag.

`data-engine-x`:
> feat(exa): /api/internal/exa/calls write endpoint + raw archive
>
> Add `exa.exa_calls` raw archive and the super-admin-only write endpoint
> hq-x calls when a research run's destination is DEX. Mirrors the
> address-parse pattern.

---

## When done

Report back with:

(a) The exact Exa API auth header name and base URL you used, and how you confirmed it (Exa MCP query / docs URL).
(b) Confirm DEX accepts the super-admin API key via `Authorization: Bearer <key>` (no custom header). Quote the line in `data-engine-x/app/auth/super_admin.py` you verified against.
(c) Output of running the seed script end-to-end against dev: both job_ids, both result_refs, sample row from each DB, total runtime.
(d) `uv run pytest tests/test_exa_client.py tests/test_exa_research_jobs_router.py` (hq-x) — pass count.
(e) `uv run pytest tests/test_exa_internal_v1.py` (data-engine-x) — pass count.
(f) The two commit SHAs (one per repo). Do not push.
