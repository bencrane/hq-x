# Architecture — hq-x

Source of truth for the foundational pattern decisions in hq-x. New
capabilities follow these patterns; deviations require updating this file.

hq-x spans two architectural domains, and this document is authoritative for
both:

- **Part I — the Gen-3 Data & Compute Fleet.** How every data-ingest / compute
  worker is scheduled, routed, run, and recorded. These standards are
  **enforced and non-negotiable** for all new compute workers. This is the
  ground-truth contract for agentic development of the fleet.
- **Part II — the Platform application layer.** The live FastAPI / Railway
  product (auth, tenancy, routers, providers, direct-mail). Still load-bearing;
  unchanged by the Gen-3 migration.

---

# Part I — Gen-3 Data & Compute Fleet  ·  AUTHORITATIVE

The canonical pattern for every data feed. There is exactly one reference
implementation today — SAM.gov Contract Opportunities (active) — and it is the
template every other feed is rebuilt against.

| Layer | File | Modal app |
|---|---|---|
| Control plane | [`src/trigger/sam_opps_bulk_dispatcher.ts`](src/trigger/sam_opps_bulk_dispatcher.ts) | — |
| Router | [`core/modal_dispatcher.py`](core/modal_dispatcher.py) | `universal-dispatcher` |
| Compute worker | [`scripts/ingest/sam_gov/sam_opps_bulk_canonical.py`](scripts/ingest/sam_gov/sam_opps_bulk_canonical.py) | `sam-gov-pipelines` |
| Read view | [`views/sam_gov/opps_active_latest_lance.sql`](views/sam_gov/opps_active_latest_lance.sql) | — |

## 1. The Control Plane — Trigger.dev v4, durable callbacks

- Managed **exclusively** by Trigger.dev v4. Tasks live in `src/trigger/`
  (`trigger.config.ts` pins `dirs: ["./src/trigger"]`).
- Durable HTTP callback via **waitpoint tokens**. The task `wait.createToken()`
  mints a pre-signed callback `url` (the `callbackHash` embedded in the URL is
  the auth — no API key), POSTs the dispatcher with that url as
  `trigger_callback_url`, then suspends on `wait.forToken(token.id)`. While
  suspended the run is checkpointed: zero compute, immune to HTTP timeouts.
  - **API note:** the methods are `wait.createToken()` + `wait.forToken()`
    (complete via the token's pre-signed URL). There is **no `wait.forRequest()`**
    in the Trigger.dev v4 API — do not write it.
- The schedule registry is the Postgres table **`ops.scheduled_tasks`**, seeded
  idempotently by [`scripts/seed_scheduled_tasks.py`](scripts/seed_scheduled_tasks.py)
  (code-owned columns refreshed on conflict; operator-owned toggles preserved).
- **`modal.Cron` is strictly forbidden.** No worker carries an embedded cron.
  Cadence is owned by Trigger v4, full stop.

## 2. The Router — the Universal Dispatcher

- [`core/modal_dispatcher.py`](core/modal_dispatcher.py), Modal app
  `universal-dispatcher`. It is the **only** Modal app exposing a web endpoint,
  and that endpoint is **proxy-authenticated** (`requires_proxy_auth=True`;
  `Modal-Key` / `Modal-Secret`). One endpoint for the entire fleet,
  `MODAL_DISPATCHER_URL`, forever.
- A **stateless router**. It receives the Trigger payload
  `{app_name, function_name, kwargs, trigger_callback_url}`, resolves the target
  via `modal.Function.from_name(app_name, function_name)`, `spawn()`s it
  fire-and-forget, and returns `202`. It holds no connection and owns no state.
- A new feed = a new worker + a one-line Trigger task. **Zero new endpoints,
  zero new secrets.** This is what kills per-feed env-var bloat.

## 3. The Compute Layer — domain-grouped Modal workers

- Modal apps are grouped **strictly by domain**:
  `app = modal.App("sam-gov-pipelines")`. Workers live under
  `scripts/ingest/<domain>/` — e.g. `scripts/ingest/sam_gov/`. **All new compute
  workers MUST be placed in a domain-specific subdirectory under
  `scripts/ingest/`.**
- Workers **do not expose web endpoints.** They are reachable only by the
  dispatcher's `spawn()` (or `modal run` for manual ops). They receive
  `trigger_callback_url` as a kwarg.

## 4. The Data Plane — DuckDB → LanceDB v2.0 → R2

- **100% DuckDB for transformation.** `read_csv(..., all_varchar=true)` on
  ingest, `TRY_CAST` for every type coercion, all projection / filter / shaping
  in SQL. Python does I/O only (stream the source to `/tmp`, hand the bytes to
  DuckDB).
- Output is **LanceDB v2.0** written directly to **Cloudflare R2**:
  `lance.write_dataset(s3://<bucket>/<path>/, data_storage_version="2.0")`.
  Lance is the system of record; every load-bearing resolution key gets a
  `BTREE` scalar index.
- Parquet, where used, is **transport only**. **No Iceberg. No Polaris.** The
  worker writes Lance to R2 with no catalog round-trip.

## 5. State Management — the worker owns terminal state

- On terminal state (success **or** failure), the Modal compute worker, in
  order:
  1. writes the run row to the Postgres **`ops.*`** tables via **psycopg** — the
     compute that knows the true outcome owns the state row; and
  2. **immediately** POSTs `{status, ...}` back to the Trigger **wait-token
     callback URL**, waking the suspended run.
- Trigger.dev therefore owns true end-to-end success/failure state. **No
  polling, no heartbeat.** (SAM.gov writes `ops.sam_opps_canonical_runs`.)

## 6. The Reference Archive — Gen-2 is read-only

- The retired Gen-2 fleet lives in
  [`modal/_archived_gen2/`](modal/_archived_gen2/) (104 apps) and
  [`scripts/dex/_archived_gen2/`](scripts/dex/_archived_gen2/) (391 scripts). It
  exists **purely as read-only reference material** — API endpoints, request
  shapes, and column mappings — while each feed is rebuilt on the Gen-3 pattern.
- **Nothing under `_archived_gen2/` may be deployed, imported, or scheduled.**

## Forbidden / retired — do not reintroduce

- **Iceberg** tables and the **Polaris** REST catalog. (Polaris *is* the Iceberg
  catalog; the Gen-3 data plane writes Lance to R2 directly and needs neither.)
- **`modal.Cron`** embedded in workers — cadence belongs to Trigger v4 +
  `ops.scheduled_tasks`.
- **Per-feed Modal web endpoints** — the Universal Dispatcher is the only one.

**Migration status:** SAM.gov opps (active) is the only feed on Gen-3. Every
other feed is frozen in `_archived_gen2/` awaiting a Gen-3 rebuild. When a feed
is rebuilt, it earns a `src/trigger/*.ts` task, a domain-grouped worker under
`scripts/ingest/<domain>/`, and a row in `ops.scheduled_tasks` — then, and only
then, is its Gen-2 source archivable-complete.

---

# Part II — Platform application layer (FastAPI / Railway)

The live product backend. Unchanged by the Gen-3 data-fleet migration; these
patterns remain the source of truth for `app/`.

## Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Web framework | FastAPI + Uvicorn | Async by default. |
| HTTP client | `httpx` | Sync `httpx.Client` for provider integrations; we don't need async there. |
| DB driver | `psycopg` v3, async pool | Configured in `app/db.py`. We do **not** use `supabase-py` for queries — raw SQL via `get_db_connection()` only. |
| ORM | None | Raw SQL with parameterized statements. If a future capability needs SQLAlchemy, add it then; do not retrofit. |
| Migration tool | Raw SQL in `migrations/` | Lexical-order runner at `scripts/migrate.py`. `schema_migrations` table tracks applied filenames. Filenames use a UTC-timestamp prefix (`YYYYMMDDTHHMMSS_<slug>.sql`). |
| Settings | `pydantic-settings.BaseSettings` | `case_sensitive=True`. Field names are ALL_CAPS, matching env var names. |
| Secrets | Doppler | Project: `hq-x`. Configs: `dev`, `stg`, `prd`. Each token is scoped to a single config; `APP_ENV` is injected per config. |
| Auth | Supabase Auth (ES256 + JWKS) | `app/auth/supabase_jwt.py` resolves a `UserContext` per request. `business.users` table links `auth.users` → `(role, client_id)`. |
| Tests | `pytest` + `pytest-asyncio` (auto mode) | Flat `tests/`. `conftest.py` populates dummy env vars before app modules import. |

## Tenancy posture

**Single-tenant.** There is no `org_id`, no `company_id`, no row-level
"client_id" scoping anywhere in the data model — even though
`business.users.role` distinguishes operators from clients, the data tables
are owned by the business as a whole.

**Tenant analog for direct-mail:** none. Pieces link to the
`business.users` row that created them via `created_by_user_id`. We do not
invent a `companies` or `clients` table.

If a future capability genuinely needs multi-tenant scoping, that's a real
schema migration — don't sneak it in.

## Auth dep

```python
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.auth.roles import require_operator, require_client
```

- `verify_supabase_jwt` — base dep; verifies the bearer JWT against
  Supabase's JWKS (cached 10 min) and resolves the `business.users` row.
- `require_operator` / `require_client` — wrap `verify_supabase_jwt` and
  reject on role mismatch.

`UserContext` shape:
```python
@dataclass(frozen=True)
class UserContext:
    auth_user_id: UUID
    business_user_id: UUID
    email: str
    role: str        # "operator" | "client"
    client_id: UUID | None
```

## Router conventions

- One file per capability under `app/routers/`. Mounted in `app/main.py`
  with `app.include_router(...)`. The capability's prefix lives on the
  `APIRouter(prefix="/...")` in the file, not on the include.
- Webhook receivers go under `app/routers/webhooks/{provider}.py`. The
  `/webhooks` prefix is added at include time. Helpers (signature, parsing,
  storage) live in `app/webhooks/{module}.py`.
- Provider HTTP clients live in `app/providers/{slug}/client.py`. They take
  `api_key: str` as the first arg; no per-org credential dicts.

## Webhook conventions

- Path: `POST /webhooks/{provider}` — the bare `/webhooks` prefix (the shape
  Cal and EmailBison already use).
- Signature verification posture per provider, configured via env.
- Webhook events land in the shared `webhook_events` table:
  `(provider_slug, event_key)` is unique and `status` flows
  `received → processed | dead_letter | replayed`.
- Dead-letter recovery is on-demand only. Each receiver exposes
  `POST /webhooks/{provider}/replay/{event_id}` (operator-gated) that
  re-projects a single stored event. No batch / cadence-driven replay.

## Provider integration template

The Lob direct-mail port is the reference. To add a new provider:

1. `app/providers/{slug}/client.py` — `httpx`-based wrapper. One
   `<Provider>ProviderError` exception with a `category` property
   (`"transient" | "terminal" | "unknown"`) for the router to map to HTTP
   status codes. `_request_with_retry` covers 429/5xx with jittered
   exponential backoff.
2. `app/models/{capability}.py` — Pydantic request/response shapes.
3. `app/routers/{capability}.py` — public API. Operator-gated. No tenant
   scoping. Persistence calls go through small helpers in
   `app/{capability}/persistence.py` (or similar).
4. `app/routers/webhooks/{slug}.py` — receiver. Helpers in
   `app/webhooks/{slug}_signature.py`, `_normalization.py`, `_processor.py`.
5. `app/config.py` — settings keyed `{SLUG}_*` (uppercase). Add a guard in
   `assert_production_safe` if there's an insecure-by-default mode.
6. `migrations/{YYYYMMDDTHHMMSS}_{capability}_{slug}.sql` — single migration
   creating all tables for this capability; collapse to the final shape.

## Direct-mail (Lob) specifics

- **Default key: `LOB_API_KEY`.** One per environment (typically a Lob test key
  in dev/stg, a live key in prd).
- **Optional `LOB_API_KEY_TEST`.** When set, callers can opt in to test
  mode on the cost-bearing routes:
  - Piece creates (`/direct-mail/postcards|letters|self-mailers|snap-packs|booklets`):
    request body field `"test_mode": true`. The piece is upserted with
    `is_test_mode=true`; reports filter on this column to exclude test
    pieces.
  - Address-verify routes and `/direct-mail/campaigns/{id}/send`: query
    param `?test_mode=true`.
  - Other routes (template CRUD, list/get, QR analytics, etc.) don't
    support `test_mode` — Lob doesn't bill them.
  - When `test_mode=true` but `LOB_API_KEY_TEST` is unset, the route
    returns HTTP 503.
- **Two webhook secrets: `LOB_WEBHOOKS_SECRET_LIVE` and
  `LOB_WEBHOOKS_SECRET_TEST`.** Lob runs separate webhook subscriptions
  for live and test mode, each with its own signing secret. The receiver tries
  both — `signature_environment` on the stored event records which matched.
  LIVE is required in prd at boot.
- **Suppression list (`suppressed_addresses`)** is consulted on every
  piece-create call. Hash key: sha256 of
  `"{line1}|{line2}|{city}|{state}|{zip5}"` after lowercase + strip. Unique
  on `(address_hash, reason)`.
- **Suppression population:** webhook events of type `piece.returned` and
  `piece.failed` insert a row with reason `returned_to_sender` / `failed`,
  pulling the address out of the existing piece's `raw_payload.to`.
- **Cost in cents** is projected at upsert from the Lob `price` field
  (string dollars → integer cents).
- **Address-verify gate** is default-on: every piece-create where `to` is
  an inline address (not a saved-address ID) runs the Lob US verify
  endpoint. `undeliverable` → HTTP 422 + auto-suppression with reason
  `undeliverable_at_send`. Caller can pass `skip_address_verification=true`
  to bypass (logged as a warning).
- **Idempotency keys** are auto-derived if the caller leaves them unset.
  See `app/providers/lob/idempotency.py`. The hash subset is intentionally
  narrow (piece type + recipient + content/template) so two creates that
  differ only in mutable fields collide deliberately.
- **Per-piece event log** in `direct_mail_piece_events` — append-only,
  every webhook event writes one row.
- **Webhook event-name extraction** is at `payload.event_type.id` (Lob
  sends `event_type` as an object). Piece id is at `payload.reference_id`
  (top level) with `payload.body.id` as fallback. See
  `app/webhooks/lob_normalization.py`.
- **Status-update vs. log-only events.** `normalize_lob_piece_status`
  returns `None` for events that should NOT change the piece's status:
  `viewed`, `informed_delivery.*`, and `return_envelope.*`. Those still
  append to `direct_mail_piece_events`; the piece's `status` stays put.
- **Suppression triggers.** Auto-populates `suppressed_addresses` on
  `piece.returned`, `piece.failed`, and `piece.certified.returned`.
  Engagement events (viewed, informed_delivery) never trigger suppression.

## Auth posture (current)

All `/direct-mail/*` routes are gated on `require_operator`. Pieces have no
tenant-linkage column yet — a future migration will add a brand-or-campaign
foreign key. Until then there is no safe way to scope pieces per client, so
client-role users get 403 on every direct-mail route.

## Verify-gate fail policy

The pre-send US address-verify gate calls Lob's `/v1/us_verifications`. If
Lob's verify endpoint is itself broken or slow, we **fail open**: log a
warning and proceed with the send. Fail-closed was rejected because one
verify outage would halt all outbound mail and cost more than the
occasional undeliverable.

## What's intentionally NOT here

- Per-org provider credentials (`provider_configs` JSONB). Single tenant.
- Multi-tenant `org_id` columns. Single tenant.
- `companies` / `clients` table. Doesn't exist yet; don't speculatively build it.
- `checks` (Lob piece type — financial, not marketing).
- US autocomplete, zip lookup, reverse-geocode (address-input UX helpers).
- Identity validation (KYC; not relevant to current product).
- International address verification (US-only mailing for now).
- **NCOA** (National Change of Address) — its own case-based polling workflow;
  deferred.
- Real metrics/log backend. `app/observability/` is a logging shim.
- Scheduled / cadence-driven webhook replay. On-demand admin endpoint only.

## What's added beyond piece-create

Thin proxies (Lob is the source of truth — these don't write to local
tables): templates + versions, saved addresses, buckslips + orders, cards
+ orders, campaigns, creatives, uploads + exports + report,
**resource-proofs** (PDF previews before printing),
**qr-code-analytics** (scan tracking on printed QR codes),
**domains** (tracking domains for branded short URLs),
**links** (Lob's URL shortener for printed mailers),
**billing-groups** (cost-allocation tags for invoice splitting).

See `docs/` for capability-specific notes and follow-up TODOs.
