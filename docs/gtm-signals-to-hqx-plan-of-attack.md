# GTM Signals → hq-x: plan of attack (assessed + execution-ordered)

Status: ready to implement. Grounded in the live code as of 2026-05-29.
Source plan assessed: `apps/hq-x/docs/gtm-signals-to-hqx-plan.md`.

---

## PART 1 — CRITICAL ASSESSMENT

### Verdict on the cut

**The cut is correct and ships as-is.** "Definition + lifecycle → hq-x; SQL execution stays near the data (DEX/gtm-mcp)" is the right boundary and it already matches three precedents in this codebase:

1. `business.agent_runs` (hq-x) holds session metadata while Anthropic owns the event history — same "config/lifecycle here, compute there" split.
2. `gtm_cohorts_v1` (DEX) already serves a **pre-materialized cohort** over HTTP behind `require_flexible_auth` + service token, and hq-x already proxies it (`gtm_cohorts.py` + `dex_client.get_cohort_primes_90d`). The new `/internal/signals/compute` endpoint is the *dynamic* sibling of that exact pattern.
3. `sba-bridges-daily` (Trigger.dev) → hq-x `/internal/sba-bridges/run-daily` → DEX `/api/internal/sba-bridges/run-daily` is the canonical "hq-x Trigger cron drives DEX compute" path. The signals cron is structurally identical.

`gtm_signals` is config-only (no FK into DEX data) so the table move is clean. hq-x acquiring zero data-stack deps (no duckdb/lance/pyarrow/boto3/R2) is preserved: all Lance/DuckDB stays behind DEX HTTP and the gtm-mcp.

### What the plan gets WRONG or leaves dangerous

1. **`gtm_signals_v1.py` is NOT a thin proxy — it owns CRUD + fire + preview + status.** The source plan (line 17, 50) says "hq-x today only PROXIES." True for hq-x, but the DEX side `apps/data-engine-x/app/routers/gtm_signals_v1.py` is the **authoritative** registry surface (list/patch/delete/fire/preview). The retire list must explicitly retire the *DEX* router's CRUD + preview (the table is leaving DEX), and the Modal `fire_endpoint`/`fire_status_endpoint` + `fire_one_signal` Modal function. The plan's "retire" section only names the cron and the compiler. **Add: DEX `gtm_signals_v1` CRUD/preview/fire, the Modal `fire_one_signal` + `MODAL_*` secrets dependency, and the hq-x→DEX signal client methods (`list/patch/delete/fire/get/preview_signal_cohort` in `dex_client.py`).**

2. **The `/run-agent` coupling is live and depends on `preview_signal_cohort`.** `apps/hq-x/app/routers/gtm_signals_v1.py::run_agent_for_signal` fetches the signal from DEX and calls `dex_client.preview_signal_cohort` (≤100s DuckDB). If you move the table to hq-x and retire the DEX preview without rewiring `/run-agent`, the agent-authoring flow breaks. The plan does not mention `/run-agent` at all. **This is the single biggest omission.** `/run-agent` must be re-pointed at: hq-x reads its own `business.gtm_signals` row → calls the new DEX `/internal/signals/compute` (capped) → builds the initial message → mints the session. (See Part 2 §H.)

3. **"Modal cron → Trigger.dev" understates the dispatch surface.** Dispatch today is TWO things: (a) the 09:00 cron `run_signals`, and (b) the operator "Fire" button → `fire_one_signal` (async spawn + status poll, with a documented duplicate-fire history). Both must be reproduced in hq-x. The plan only mentions the cron. **Add the manual-fire path** as an hq-x route that calls `/internal/signals/compute` then POSTs the webhook directly from hq-x (httpx is already a hq-x dep) — no Modal.

4. **`spine_target` format is inconsistent in the live data and must be normalized on move.** The seed rows use `usaspending.transaction_fpds_lance` (dotted, no `s3://`), the compiler hardcodes the full `s3://…` URI, and the generalized spec (plan §C) wants `<namespace.dataset_lance>`. gtm-mcp resolves `<namespace>.<dataset>` (suffix optional). **Lock the canonical form to the gtm-mcp dotted identifier `<namespace>.<dataset>` (`_lance` optional)** and store that in `business.gtm_signals.spine_target`. The compiler emits dotted identifiers; DEX `/compute` resolves them exactly as the gtm-mcp does (reuse `_dataset_uri`).

5. **Injection-safety claim needs teeth.** The plan says "must be injection-safe / parameterized over HTTP" but DuckDB-over-Arrow via `con.execute(sql, bindings)` only parameterizes *values*, never *identifiers* (column/table names). The generalized compiler interpolates column names from `criteria` directly into SQL. **Decision: column/table identifiers MUST be validated against the live schema allowlist (from `gtm.get_polaris_schema`) before interpolation, and quoted; values always go through `?` bindings.** Without identifier validation this is a SQL-injection hole the moment criteria authoring is exposed to anything but the operator. (See Part 2 §F.)

6. **`fetch_cohort_rows` returns the FULL cohort with no cap and an 8GB Modal envelope.** Over an HTTP endpoint that's a denial-of-service / OOM vector (e.g. `usaspending_net_new_100k` over 1y = ~136K rows per the router comment). The DEX request runs inside the Railway web container, not an 8GB Modal box. **The `/internal/signals/compute` endpoint MUST enforce a hard row cap and a hard byte cap, returning `truncated: true` + `matched_count` rather than the unbounded set.** (See Part 2 §G — `max_rows` default 50,000, hard ceiling, plus a `count_only` mode.)

7. **hq-x must NOT gain a `psycopg_pool.ConnectionPool` the DEX way.** The DEX router uses a lazy sync `ConnectionPool`. hq-x uses an async pool via `app.db.get_db_connection()` (async context manager) and `init_pool()`/`close_pool()` lifecycle. **All hq-x persistence must use `get_db_connection()`** (matches `recipients.py`), not a transplanted sync pool. The plan doesn't call this out; it's an easy way to violate hq-x conventions.

8. **Migration filename convention differs between repos.** DEX uses `YYYYMMDDHHMMSS_` (14-digit, no `T`). hq-x uses `YYYYMMDDTHHMMSS_` (with a literal `T`) per `apps/hq-x/CLAUDE.md` and every file in `apps/hq-x/migrations/`. The plan says "hq-x timestamp convention" but the example DDL must use the **`T` form** and live in `apps/hq-x/migrations/` (NOT `supabase/migrations/` — that's DEX). hq-x applies via `apps/hq-x/scripts/migrate.py` (tracks in `schema_migrations`, uses `HQX_DB_URL_DIRECT`).

### Open questions — resolved

**Q1. Cohort persistence grain.**
**Decision: persist a cohort header row + inline member rows in a child table (jsonb per member), capped.** `business.gtm_signal_cohorts` (one row per run) + `business.gtm_signal_cohort_members` (N rows, `member jsonb`). Rationale: signals fire daily and cohorts are small-to-medium (seeds match thousands, not millions; the cap in §G bounds the worst case); inline storage makes a cohort immediately reusable/queryable in hq-x with zero recompute and zero DEX round-trip, which is the whole point of "reusable cohort." R2 snapshotting is over-engineered for daily cohorts of ≤50K rows and would re-introduce an R2 dependency into hq-x's read path. Re-runnable spec is *also* preserved (we snapshot `criteria` into the header), so on-demand recompute remains available without making it the storage model.

**Q2. `/compute` contract — SQL string vs criteria+spine.**
**Decision: hq-x compiles criteria → a structured compile result, and sends the structured `{spine_target, where_sql, bindings, select, join, order_by, limit}` to DEX — NOT a raw free-form SQL string, and NOT raw criteria.** Rationale: sending raw SQL makes DEX a generic SQL executor reachable over the service token (blast radius = anything DuckDB can read on R2); sending raw criteria duplicates the compiler in DEX (defeats "compiler lives in hq-x"). The middle path — hq-x emits a *constrained* compiled fragment (parameterized WHERE + validated identifiers), DEX assembles the final `SELECT … FROM <resolved_uri> WHERE <fragment>` and binds values — keeps the compiler single-sourced in hq-x while DEX stays a constrained executor that only ever runs the shape it assembles. DEX re-validates identifiers against the opened Lance schema as defense-in-depth.

**Q3. Column validation compile-time vs execute-time.**
**Decision: both, with compile-time as the UX gate and execute-time as the security gate.** Authoring/preview (hq-x → gtm-mcp `get_polaris_schema`) validates at compile time for fast operator feedback. DEX `/compute` re-validates every identifier against the freshly-opened Lance dataset's Arrow schema at execute time (cheap — metadata only, no scan) and rejects unknown columns with 422 before building SQL. Never trust the compile-time check alone (schema can drift, caller can be buggy).

**Q4. Backfill of existing `ops.gtm_signals`.**
**Decision: one-shot idempotent Python backfill script in hq-x** (`apps/hq-x/scripts/backfill_gtm_signals_from_dex.py`) that reads DEX `ops.gtm_signals` over the existing `dex_client.list_gtm_signals()` (already returns the full row incl. criteria, spine_target, webhook_*), translates each `criteria` from the 4 legacy FPDS keys into the generalized spec, and UPSERTs into `business.gtm_signals`. Rationale: only 2 seeds + any operator-added rows; an API-read backfill avoids cross-DB SQL (forbidden) and is re-runnable. Legacy-criteria translation is mechanical (see §I).

**Q5. Auth + limits for DEX `/compute`.**
**Decision: super-admin (`get_current_super_admin`, i.e. `DEX_SERVICE_TOKEN`), mounted under `/api/internal` — identical to `sba_bridges_internal_v1`.** Rationale: this is a server-to-server compute endpoint, never user-facing; `/api/internal/*` in DEX is uniformly super-admin (the file's own docstring says so). Limits: `max_rows` (caller-supplied, hard-ceiled at 50,000), response byte cap, `count_only` mode, DuckDB `SET memory_limit`/`threads` as in the gtm-mcp. No new secret — reuse `DEX_SERVICE_TOKEN` (hq-x already has it).

**Q6. Cutover sequencing.**
**Decision: dual-run with hq-x as the authority from PR-3 onward, DEX cron disabled only after one clean hq-x cron cycle is verified.** Hard switch risks a dark dispatch day. Sequence in Part 2 §K.

### hq-x conventions the plan must respect (checklist)

- Migrations: `apps/hq-x/migrations/YYYYMMDDTHHMMSS_<slug>.sql`, `IF NOT EXISTS` everywhere, `business.*` schema, applied via `scripts/migrate.py`. (NOT `supabase/migrations`, NOT the DEX 14-digit prefix.)
- Persistence: `app.db.get_db_connection()` async CM, `psycopg.types.json.Jsonb` for jsonb params (see `recipients.py`).
- Internal routes: `app/routers/internal/<name>.py`, `APIRouter(prefix="/<name>", tags=["internal"])`, `Depends(verify_trigger_secret)`, registered in `app/main.py` with `prefix="/internal"`.
- Public/BFF routes: `Depends(verify_backend_x_token)` (the secret the platform-api BFF + `callHqxApi` use).
- DEX calls: only via `app/services/dex_client.py` (`_request` + service-token fallback). No new HTTP client.
- Trigger.dev: `apps/hq-x/src/trigger/<name>.ts`, `schedules.task`, call `callHqx("/internal/…", body, {timeoutMs})` from `lib/hqx-client.ts`.
- No data stack: no duckdb/lance/pyarrow/boto3/R2 creds enter `apps/hq-x/pyproject.toml`.

---

## PART 2 — CONCRETE PLAN OF ATTACK (execution-ordered)

### A. Cohort persistence model + DDL (hq-x)

**Migration file:** `apps/hq-x/migrations/20260530T120000_gtm_signals.sql`

```sql
-- 20260530T120000_gtm_signals.sql
-- GTM signal definition registry (migrated from DEX ops.gtm_signals) +
-- cohort persistence. Config-only: no FK into DEX data. Compute stays in
-- DEX/gtm-mcp; hq-x owns "what is a signal" + the resolved cohort.
-- Forward-only, IF NOT EXISTS everywhere (hq-x convention).

CREATE SCHEMA IF NOT EXISTS business;

-- ── Signal registry ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS business.gtm_signals (
    signal_slug      TEXT        PRIMARY KEY,           -- lowercase_snake_case
    display_name     TEXT        NOT NULL DEFAULT '',   -- human label (slug doubled if empty)
    spine_target     TEXT        NOT NULL,              -- gtm-mcp dotted id: <namespace>.<dataset>  (_lance optional)
    criteria         JSONB       NOT NULL,              -- generalized spec (see §C of the plan)
    webhook_test_url TEXT        NOT NULL DEFAULT '',
    webhook_prod_url TEXT        NOT NULL DEFAULT '',
    webhook_target   TEXT        NOT NULL DEFAULT 'test'
                                 CHECK (webhook_target IN ('test','prod')),
    is_active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gtm_signals_is_active_idx
    ON business.gtm_signals (is_active) WHERE is_active;

-- ── Cohort header (one row per resolved run) ─────────────────────────────
CREATE TABLE IF NOT EXISTS business.gtm_signal_cohorts (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_slug       TEXT        NOT NULL
                                  REFERENCES business.gtm_signals(signal_slug)
                                  ON DELETE CASCADE,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    criteria_snapshot JSONB       NOT NULL,             -- exact criteria used (re-runnable)
    spine_target      TEXT        NOT NULL,             -- snapshot of dotted id used
    matched_count     INTEGER     NOT NULL,             -- total pre-cap
    member_count      INTEGER     NOT NULL,             -- rows actually persisted (post-cap)
    truncated         BOOLEAN     NOT NULL DEFAULT FALSE,
    source            TEXT        NOT NULL DEFAULT 'cron'   -- 'cron' | 'manual' | 'preview'
                                  CHECK (source IN ('cron','manual','preview')),
    compute_ms        INTEGER     NULL,                 -- DEX-reported sql_elapsed_ms
    trigger_run_id    TEXT        NULL,                 -- Trigger.dev ctx.run.id when cron-driven
    dispatch          JSONB       NULL,                 -- webhook dispatch result (status/bytes) or NULL
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gtm_signal_cohorts_slug_run_at_desc_idx
    ON business.gtm_signal_cohorts (signal_slug, run_at DESC);

-- ── Cohort members (N per cohort; one resolved entity each) ───────────────
CREATE TABLE IF NOT EXISTS business.gtm_signal_cohort_members (
    cohort_id   UUID    NOT NULL
                        REFERENCES business.gtm_signal_cohorts(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,                       -- preserves order_by sort
    member      JSONB   NOT NULL,                       -- the resolved row (dataset-agnostic)
    PRIMARY KEY (cohort_id, ordinal)
);

COMMENT ON TABLE business.gtm_signals IS
    'GTM signal definitions (migrated from DEX ops.gtm_signals). Generalized '
    'criteria over any Polaris Lance dataset. hq-x owns definition + lifecycle; '
    'SQL compute runs in DEX (/api/internal/signals/compute) + gtm-mcp.';
COMMENT ON COLUMN business.gtm_signals.spine_target IS
    'gtm-mcp dotted identifier <namespace>.<dataset> (the _lance suffix is optional). '
    'DEX resolves it to the s3 Lance URI the same way the polaris MCP does.';
COMMENT ON TABLE business.gtm_signal_cohorts IS
    'One row per signal resolution. criteria_snapshot makes the cohort re-runnable.';
```

Rationale recap: header + members is queryable in hq-x with no recompute (reusability), the cap bounds storage, and `criteria_snapshot` keeps the spec re-runnable. `member jsonb` keeps it dataset-agnostic (FPDS rows and, say, FMCSA rows coexist with no schema change).

### B. Generalized criteria compiler (hq-x, pure Python, no DuckDB)

**File to CREATE:** `apps/hq-x/app/services/gtm_signal_compiler.py`

Signature + approach:

```python
class CompileError(ValueError): ...

@dataclass(frozen=True)
class CompiledCriteria:
    spine_target: str          # echoed dotted id
    where_sql: str             # parameterized fragment, '?' placeholders, identifiers quoted+validated
    bindings: list[Any]        # positional values for the '?' placeholders
    select: list[str]          # validated, quoted column identifiers (or ['*'] sentinel handled by DEX)
    join: dict | None          # {"dataset": <dotted>, "on": [l, r], "select": [...]} — all identifiers validated
    order_by: dict | None      # {"column": <validated>, "dir": "asc"|"desc"}
    limit: int | None

# allowed_columns: the set DEX/gtm-mcp reports for spine_target (and join dataset).
# Passed in by the caller after a get_polaris_schema round-trip (compile-time gate).
def compile_criteria(
    criteria: dict[str, Any],
    *,
    now: datetime,
    allowed_columns: set[str],
    allowed_join_columns: set[str] | None = None,
) -> CompiledCriteria: ...
```

SQL-generation approach (injection-safe by construction):
- **Values → `?` bindings only.** Never f-string a value into SQL. DEX binds them via `con.execute(sql, bindings)`.
- **Identifiers (columns, join keys, order_by, select) → validated then quoted.** Each identifier is checked `in allowed_columns` (raise `CompileError` otherwise) and emitted as `"col"` (double-quoted, with internal `"`→`""` escaping). A regex allowlist `^[A-Za-z_][A-Za-z0-9_]*$` is the belt; the schema-membership check is the suspenders.
- **`spine_target` / join `dataset`** validated against `^[a-z0-9_]+\.[a-z0-9_]+$` (dotted) — DEX re-resolves, never trusts.
- **Predicate ops** are a fixed enum → fixed SQL templates: `eq`→`=?`, `in`→`IN (?,?,…)`, `gte`→`>=?`, `lte`→`<=?`, `between`→`>=? AND <=?`, `is_null`→`IS NULL` (no binding), `not_null`→`IS NOT NULL`, `like`→`LIKE ?`. Any other op → `CompileError`.
- **`time_window`** → `"col" >= ? AND "col" <= ?` with two date/timestamp bindings derived from `now - hours`. Preserve the existing FPDS semantics (date-granular for `action_date`) but generalize the column.
- **Numeric coercion** for `gte/lte/between` mirrors the legacy `TRY_CAST(... AS DOUBLE)` — emit `TRY_CAST("col" AS DOUBLE) >= ?` when `value` is numeric, so string-typed Lance columns (USAspending stores obligation as text) still compare numerically. This is the one place a cast wraps the identifier; the identifier is still validated.
- **`action_type IS NULL` OR-branch** (legacy "brand-new awards") is expressible in the generalized spec as an `in` predicate whose `value` array contains JSON `null`; the compiler emits `("col" IN (?,…) OR "col" IS NULL)` when null is present. Keeps backward-compat with the 2 seeds.

This module is **pure** (no DB, no network) → unit-testable offline.

### C. Signal service / registry (hq-x persistence)

**File to CREATE:** `apps/hq-x/app/services/gtm_signals.py` (mirrors `recipients.py` style: `get_db_connection()`, `Jsonb`).

Functions:
- `async def list_signals() -> list[dict]` — `ORDER BY is_active DESC, signal_slug ASC`.
- `async def get_signal(slug) -> dict | None`.
- `async def upsert_signal(spec) -> dict` — INSERT … ON CONFLICT (signal_slug) DO UPDATE (used by backfill + future authoring).
- `async def patch_signal(slug, patch: dict) -> dict | None` — partial; same field set as DEX `SignalPatchRequest` plus `criteria`, `spine_target`, `display_name`.
- `async def delete_signal(slug) -> bool`.
- `async def list_active_signals() -> list[dict]` — for the cron.

### D. Cohort writer (hq-x persistence)

**File to CREATE:** `apps/hq-x/app/services/gtm_cohort_writer.py`.

- `async def write_cohort(*, signal_slug, criteria_snapshot, spine_target, matched_count, members: list[dict], truncated, source, compute_ms, trigger_run_id, dispatch) -> UUID` — one INSERT into `gtm_signal_cohorts` (returns id), then a batched `executemany`/`COPY`-style insert of members with `ordinal` = enumerate index. `member_count = len(members)`. Wrap both in one transaction (`get_db_connection()` gives a connection; use a single `async with conn.transaction()` if available, else one cursor).
- `async def get_cohort(cohort_id) -> dict | None` and `async def list_cohorts_for_signal(slug, limit, offset)` for the read API.

### E. Router (hq-x public/BFF surface)

**File to EDIT:** `apps/hq-x/app/routers/gtm_signals_v1.py` — convert from DEX-proxy to hq-x-native.

- `GET    /api/v1/signals` → `gtm_signals.list_signals()` (was `dex_client.list_gtm_signals()`).
- `GET    /api/v1/signals/{slug}` → `get_signal` (new; today hq-x has no per-slug GET, `dex_client.get_gtm_signal` filters the list).
- `POST   /api/v1/signals` → `upsert_signal` (new authoring entry point; `extra="forbid"` body w/ `signal_slug, display_name, spine_target, criteria, webhook_*`).
- `PATCH  /api/v1/signals/{slug}` → `patch_signal` (now writes hq-x DB, not DEX).
- `DELETE /api/v1/signals/{slug}` → `delete_signal`.
- `POST   /api/v1/signals/{slug}/fire` → **manual fire, hq-x-driven** (replaces the Modal-spawn proxy): read signal → compile → call DEX `/internal/signals/compute` (capped per body `limit`) → persist cohort (`source='manual'`) → if `webhook_<target>_url` set, httpx POST the payload → return `{cohort_id, matched_count, member_count, truncated, dispatch}`. Synchronous is fine (the cap bounds compute; no 30s Modal-spawn dance needed). Drop `/fire/status/{call_id}` entirely (Modal-only artifact).
- `POST   /api/v1/signals/{slug}/preview` → compile → DEX `/internal/signals/compute` with `count_only=False, max_rows≤200` → return rows + matched_count (no persistence, `source='preview'` optional). This is what the platform-app authoring UI calls for a fast look.
- `POST   /api/v1/signals/{slug}/run-agent` → **rewire** (see §H).
- `GET    /api/v1/signals/{slug}/cohorts` + `GET /api/v1/signals/cohorts/{cohort_id}` → cohort read API (new; the reusable-cohort payoff).

Auth stays `Depends(verify_backend_x_token)` (BFF surface) — unchanged.

### F. Authoring/preview → gtm-mcp (≤100 rows): exact mechanism

**Two distinct gtm-mcp needs; resolve them differently:**

1. **Schema validation for the compiler (`get_polaris_schema`)** — needed server-side, synchronously, inside hq-x's compile path. **Decision: direct HTTP from hq-x to the gtm-mcp** (`POST {GTM_MCP_URL}/mcp` streamable-HTTP, `Authorization: Bearer {GTM_MCP_AUTH_TOKEN}`). Justification from live config: `GTM_MCP_URL` is already in `apps/hq-x/app/config.py`, and `GTM_MCP_AUTH_TOKEN` already lives in Doppler `hq-all/prd` (consumed by `apps/hq-x/scripts/managed_agents/register_polaris_vault.py` and validated by `polaris_server.py`). The managed-agent/vault path injects that same bearer for the *agent's* tool calls, but hq-x's own compiler can't go through an agent session for a synchronous schema lookup. **Add `GTM_MCP_AUTH_TOKEN: SecretStr | None` to hq-x config** and a tiny client.
   - **File to CREATE:** `apps/hq-x/app/services/gtm_mcp_client.py` — `async def get_polaris_schema(namespace, dataset) -> set[str]` and `async def execute_read_only(sql) -> dict` (≤100 rows). Thin `httpx.AsyncClient` MCP streamable-HTTP caller (initialize → tools/call). This is NOT a data-stack dep — it's an HTTP client to a remote service, exactly like `managed_agents.py` calls Anthropic.

2. **Interactive criteria authoring by the gtm-agent** (operator chats, agent validates criteria against live schema, samples ≤100 rows) — this stays the **managed-agent session** path (`managed_agents.mint_session`, vault-injected polaris bearer). Unchanged. The agent uses `get_polaris_schema` + `execute_read_only_duckdb_query` itself. hq-x doesn't proxy those; the agent calls them through its own MCP wiring.

So: **compiler schema-gate = direct HTTP (new tiny client); human authoring sampling = existing managed-agent session.** Both hit the same deployed gtm-mcp; neither raises the 100-row cap.

### G. DEX `POST /api/internal/signals/compute` — contract + file

**File to CREATE:** `apps/data-engine-x/app/routers/signals_compute_internal_v1.py`
**Register in** `apps/data-engine-x/app/main.py`: `app.include_router(signals_compute_internal_router, prefix="/api/internal", tags=["internal"])` (mirrors `address_parse_router`, `sba_bridges_internal_router`).
**Auth:** `Depends(get_current_super_admin)` (DEX_SERVICE_TOKEN), identical to `sba_bridges_internal_v1`.

Request body (Pydantic, `extra="forbid"`):
```jsonc
{
  "spine_target": "usaspending.transaction_fpds_lance",   // dotted id; DEX resolves via _dataset_uri
  "where_sql":   "\"action_date\" >= ? AND \"action_date\" <= ? AND TRY_CAST(\"federal_action_obligation\" AS DOUBLE) >= ?",
  "bindings":    ["2026-05-28","2026-05-29",100000],
  "select":      ["recipient_uei","piid","action_date"],   // validated identifiers; [] or ["*"] → all cols
  "join":        {                                          // optional
    "dataset": "spines.sam_entities_lance",
    "on":      ["recipient_uei","uei"],                    // [spine_col, join_col]
    "select":  ["cage_code","legal_business_name"]
  },
  "order_by":    {"column": "federal_action_obligation", "dir": "desc"},  // optional
  "max_rows":    50000,            // caller cap; HARD-CEILED server-side at 50000
  "count_only":  false             // when true: returns matched_count only, no rows
}
```

Response (`DataEnvelope`):
```jsonc
{ "data": {
  "spine_target": "usaspending.transaction_fpds_lance",
  "matched_count": 137412,      // total rows the WHERE matched (pre-cap)
  "row_count": 50000,           // rows returned (post-cap)
  "truncated": true,
  "columns": ["uei","cage_code",...],
  "rows": [ {...}, ... ],       // omitted entirely when count_only
  "sql_elapsed_ms": 8123
}}
```

Behavior + how it reuses/refactors `fetch_cohort_rows`:
- **Refactor `app/services/gtm_signal_cohort.py` into a dataset-agnostic executor** `app/services/lance_cohort_exec.py` (or generalize in place). New core:
  `def execute_cohort(*, spine_target, where_sql, bindings, select, join, order_by, max_rows, count_only) -> dict`.
  - Resolve `spine_target` → URI via the **same** `_dataset_uri` logic the gtm-mcp uses (lift it to a shared helper or duplicate the 4 lines).
  - Open spine via `lance.dataset(uri, storage_options=...)`. **Re-validate** every identifier in `select`/`order_by`/join against `ds.schema` names (and the join ds schema); 422 on unknown (defense-in-depth vs the hq-x compile-time gate).
  - Optional pushdown: if `where_sql` references a known BTREE-indexed time/identity column, keep the existing pyarrow predicate-pushdown optimization for the time window (parse the lo/hi out of bindings OR — simpler and safe — have hq-x ALSO send an optional `scan_filter: {column, gte, lte}` hint that DEX turns into the pyarrow `pc.field(...)` pushdown; falls back to full scan when absent). The DuckDB WHERE still applies the full predicate.
  - Register spine (+ join) Arrow tables in DuckDB exactly as the gtm-mcp does (`con.register`), `SET threads=4; SET memory_limit='4GB'`.
  - Assemble final SQL: `SELECT <select-or-*> FROM spine [INNER JOIN join ON …] WHERE <where_sql> [ORDER BY "<col>" <dir> NULLS LAST] LIMIT <max_rows+1>`. Bind values via `con.execute(sql, bindings)`. `matched_count`: when `count_only` or when truncated, run a `SELECT count(*) … WHERE <where_sql>` (same bindings) to get the true pre-cap total; otherwise it equals `row_count`.
  - Enforce response byte cap (e.g. 64MB) — if exceeded, drop to `count_only`-style response with an error flag, never stream an unbounded body.
- The **Modal cron path is retired** (§K), so `fetch_cohort_rows`'s old FPDS-hardcoded body can be deleted once nothing imports it. Keep `lance_cohort_exec.execute_cohort` as the one true executor.

### H. hq-x `/run-agent` rewire (do NOT skip)

**File to EDIT:** `apps/hq-x/app/routers/gtm_signals_v1.py::run_agent_for_signal`.
- Replace `dex_client.get_gtm_signal(slug)` → `gtm_signals.get_signal(slug)` (hq-x DB).
- Replace `dex_client.preview_signal_cohort(...)` → compile criteria (schema-gated via `gtm_mcp_client.get_polaris_schema`) → `dex_client.compute_signal_cohort(...)` (new client method → DEX `/internal/signals/compute`, `max_rows=payload.limit`, capped at 200 for agent seeding). Build the same `preview`-shaped dict `_format_initial_user_message` expects (`rows, matched_count, limited, target, criteria, spine_target`).
- Keep `managed_agents.mint_session` + `_insert_agent_run` unchanged (those already live in hq-x).
- Optionally persist the seeded rows as a cohort (`source='preview'`) so the agent run links to a `cohort_id` — nice-to-have, not required for parity.

### I. DEX client additions/removals (hq-x)

**File to EDIT:** `apps/hq-x/app/services/dex_client.py`.
- **ADD:** `async def compute_signal_cohort(*, spine_target, where_sql, bindings, select, join, order_by, max_rows, count_only, scan_filter=None, bearer_token=None) -> dict` → `POST /api/internal/signals/compute` (service-token; `_unwrap`).
- **REMOVE (after cutover, PR-5):** `list_gtm_signals`, `get_gtm_signal`, `patch_gtm_signal`, `delete_gtm_signal`, `fire_gtm_signal`, `fire_gtm_signal_status`, `preview_signal_cohort`. These all targeted the DEX `ops.gtm_signals` surface that's being retired. (Leave `list_gtm_signals` in place through PR-4 because the backfill uses it; delete in PR-5.)

### J. Trigger.dev cron (hq-x) — replaces Modal cron

**File to CREATE:** `apps/hq-x/src/trigger/gtm-signals-daily.ts` — `schedules.task({ id: "gtm-signals-daily", cron: "0 9 * * *", maxDuration: 1800, run: ... callHqx("/internal/signals/run-daily", { trigger_run_id: ctx.run.id }, { timeoutMs: 1500_000 }) })`. Mirror `matching-engine-daily.ts` exactly.

**File to CREATE:** `apps/hq-x/app/routers/internal/gtm_signals.py` — `APIRouter(prefix="/signals", tags=["internal"])`, `POST /run-daily` `Depends(verify_trigger_secret)`. For each active signal: compile → `dex_client.compute_signal_cohort` (full cap) → `gtm_cohort_writer.write_cohort(source='cron', trigger_run_id=...)` → if `webhook_<target>_url` non-empty, httpx POST `{signal_slug, fired_at, row_count, rows}` (byte-for-byte the legacy Modal `_dispatch` payload, so n8n consumers don't change) → record `dispatch` on the cohort. Per-signal try/except (one signal failing must not abort the batch, matching `run_signals`). Return `{fired_at, signals_loaded, results:[…]}`.
**Register** in `apps/hq-x/app/main.py`: `app.include_router(internal_gtm_signals.router, prefix="/internal")`.

### K. Cutover / retire sequence (dual-run, decided)

1. **PR-1 lands** (table + backfill): `business.gtm_signals` populated; DEX `ops.gtm_signals` + Modal cron still authoritative. No behavior change.
2. **PR-3 lands** (hq-x cron + manual fire + DEX `/compute`): hq-x `gtm-signals-daily` deployed but **paused** in the Trigger.dev dashboard. Manually invoke `/internal/signals/run-daily` once with the prod webhook target pointing at a **test** sink; diff the resulting cohort vs what the DEX preview returns for the same slug. Verify row parity.
3. **Enable hq-x cron; same UTC time is fine** (both would fire) — for ONE day run both with the **DEX cron's webhook flipped to a no-op/test URL** (PATCH `ops.gtm_signals.webhook_target='test'` with an empty test URL) so only hq-x dispatches to prod. Confirm n8n receives exactly one payload per signal from hq-x.
4. **Disable the Modal cron** (`modal app stop data-engine-x-gtm-usaspending-trigger`, or comment the `schedule=` + redeploy) once one clean hq-x cycle is confirmed. Dispatch is never dark — hq-x is already firing before DEX stops.
5. **PR-5** deletes the retired DEX/​hq-x code (DEX `gtm_signals_v1` CRUD/preview/fire routers + registration, `gtm_usaspending_trigger_app.py`, `gtm_signal_cohort.py` FPDS body, `MODAL_*` secret note, hq-x `dex_client` signal methods). Drop `ops.gtm_signals` LAST (separate DEX migration, after a grace period — keep it as a read-only fallback for one cycle).

### L. Tests

- `apps/hq-x/tests/test_gtm_signal_compiler.py` — pure compiler unit tests: each op → expected `where_sql` + bindings; identifier-injection attempts (`"col"; DROP …`, unknown column, non-allowlisted op) all raise `CompileError`; legacy FPDS criteria (the 2 seeds) compile to a fragment equivalent to the old `compile_criteria` output incl. the `action_type IS NULL` OR-branch and the `TRY_CAST(... AS DOUBLE)` numeric coercion.
- `apps/hq-x/tests/test_gtm_signals_service.py` — upsert/patch/delete/list against a test DB (or the existing hq-x DB-test harness); cohort writer round-trip (header + N members, ordinal order preserved, cascade on delete).
- `apps/hq-x/tests/test_gtm_signals_router.py` — FastAPI `TestClient`: `/fire` and `/preview` with `dex_client.compute_signal_cohort` + `gtm_mcp_client.get_polaris_schema` mocked; assert cohort persisted on `/fire`, not on `/preview`; `/run-agent` mocks `managed_agents.mint_session` and asserts the seeded message carries `matched_count`.
- `apps/data-engine-x/tests/test_signals_compute_internal.py` — `/api/internal/signals/compute`: `count_only` returns no rows; `max_rows` ceiling enforced (request 1e9 → capped at 50000, `truncated:true`); unknown identifier in `select` → 422; auth rejects non-service-token; a small real-or-fixture Lance dataset returns expected rows. Assert raw bindings never appear interpolated in the executed SQL (the executor logs SQL + bindings separately — assert on that).
- Trigger task: a thin `apps/hq-x/src/trigger/__tests__/gtm-signals-daily.test.ts` only if the repo already tests tasks (most are untested) — otherwise rely on the `/internal/signals/run-daily` router test with `dex_client` mocked + per-signal-failure isolation asserted.

### M. PR breakdown (each independently shippable)

- **PR-1 — hq-x table + backfill (no behavior change).** Migration §A, `gtm_signals.py` service §C, `gtm_cohort_writer.py` §D (header/members + reads), backfill script §I/Q4, compiler tests scaffold. Ships dark: nothing reads `business.gtm_signals` yet. Verifiable: backfill populates 2+ rows; `migrate.py` applies clean.
- **PR-2 — generalized compiler + gtm-mcp client.** `gtm_signal_compiler.py` §B, `gtm_mcp_client.py` §F, `GTM_MCP_AUTH_TOKEN` config add, full compiler unit tests §L. Pure/no-wiring; safe to land alone. Verifiable: `test_gtm_signal_compiler.py` green incl. injection cases.
- **PR-3 — DEX `/compute` executor.** `signals_compute_internal_v1.py` §G + `lance_cohort_exec.py` refactor + main.py registration + `dex_client.compute_signal_cohort` §I + DEX tests §L. DEX-only; the old DEX preview/fire still work. Verifiable: runtime-probe `/api/internal/signals/compute` per the deploy-verifier (`verify_service_with_runtime_probes data-engine-x https://api.dataengine.run /api/internal/signals/compute` — expects 401 unauth = registered).
- **PR-4 — hq-x cron + manual fire + preview + run-agent rewire.** Router §E (native CRUD/fire/preview/cohort-reads), `/internal/signals/run-daily` §J, Trigger task §J (deployed paused), `/run-agent` rewire §H, router/service tests §L. This is the cutover-enabling PR; land it, then execute the dual-run steps K2–K4 operationally. Verifiable: manual `/internal/signals/run-daily` produces a cohort with parity to DEX preview.
- **PR-5 — retire.** Delete DEX `gtm_signals_v1` CRUD/preview/fire + registration, `gtm_usaspending_trigger_app.py`, `gtm_signal_cohort.py` FPDS remnants, hq-x `dex_client` signal methods, the `MODAL_*`-for-signals note in DEX CLAUDE.md. Separate trailing DEX migration to drop `ops.gtm_signals` after a one-cycle grace. Verifiable: grep shows no live import of the deleted symbols; both services boot + runtime-probe green.

Order: 1 → 2 → 3 can land in parallel-ish (2 and 3 are independent of each other; both depend on nothing but can merge in any order). 4 depends on 1+2+3. 5 depends on the dual-run verification after 4. Open all against `main` directly (no stack — squash-merge would drop later additions per the global git rule).

---

## PART 3 — RISKS + OPERATOR DECISIONS

### Risks
- **R1 — Unbounded compute over HTTP.** Mitigated by the hard 50K row cap + byte cap + `count_only` (§G). The legacy 8GB Modal envelope does NOT exist on the Railway web container; without the cap, a wide-window signal OOMs the DEX web service. Non-negotiable.
- **R2 — Identifier injection.** Mitigated by dual validation (compile-time schema allowlist + execute-time Lance-schema re-check) and quoting (§B/§F/§G). The moment criteria authoring is exposed beyond the operator, the execute-time gate is what actually protects R2 reads.
- **R3 — Dark dispatch during cutover.** Mitigated by dual-run (§K): hq-x fires before DEX stops; the only window is double-fire (both to prod), which step K3 removes by flipping the DEX cron to a test sink first. n8n payload shape is preserved byte-for-byte so downstream consumers need no change.
- **R4 — gtm-mcp as a synchronous dependency in hq-x's compile path.** If `GTM_MCP_URL`/`GTM_MCP_AUTH_TOKEN` are unset or the mcp is down, compile fails. Acceptable (loud, attributable) for authoring/preview; the **cron** should tolerate a schema-fetch failure per-signal (skip + log, don't abort the batch) — and can optionally skip the compile-time schema gate for cron runs since DEX re-validates at execute time anyway. Decide per §J: cron relies on DEX execute-time validation; gtm-mcp schema gate is authoring/preview-only. (This removes the cron's hard dependency on gtm-mcp.)
- **R5 — Lost predicate pushdown → slow/expensive scans.** The legacy code pushed `action_date` to the BTREE before materializing 107M FPDS rows. The generic executor must keep an opt-in `scan_filter` hint (§G) or it will full-scan giant datasets. Without it, `transaction_fpds_lance` compute is unusable. Build the hint in from PR-3, not later.

### Decisions that genuinely need the operator (2)
1. **Cron schedule + dual-run window.** Confirm the hq-x signals cron keeps `0 9 * * *` (matches the retired Modal cron) and approve the one-day dual-run where the DEX cron is flipped to a test sink (K3) — i.e. accept that hq-x becomes the prod dispatcher for a day before the Modal cron is stopped. (Default if silent: yes, `0 9 * * *`, dual-run one day.)
2. **`ops.gtm_signals` drop timing.** Approve dropping the DEX table in a trailing migration one cycle after PR-5 (vs keeping it indefinitely as a frozen read-only fallback). (Default if silent: drop one cycle after PR-5.)

Everything else (cohort persistence grain, `/compute` contract shape, auth model, validation strategy, backfill mechanism, PR breakdown) is decided above and needs no operator input.
