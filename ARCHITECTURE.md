# Architecture

This is the source of truth for the foundational pattern decisions in
hq-x. New capabilities follow these patterns; deviations require updating
this file.

## Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Web framework | FastAPI + Uvicorn | Async by default. |
| HTTP client | `httpx` | Sync `httpx.Client` for provider integrations; we don't need async there. |
| DB driver | `psycopg` v3, async pool | Configured in `app/db.py`. We do **not** use `supabase-py` for queries — raw SQL via `get_db_connection()` only. |
| ORM | None | Raw SQL with parameterized statements. If a future capability needs SQLAlchemy, add it then; do not retrofit. |
| Migration tool | Numbered raw SQL in `migrations/` | Lexical-order runner at `scripts/migrate.py`. `schema_migrations` table tracks applied filenames. |
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

When the audit asked "what about company_id?" the answer here is: that's a
multi-tenant artifact. Every place OEX read or wrote `org_id`, hq-x just
operates as the single tenant. If a future capability genuinely needs
multi-tenant scoping, that's a real schema migration — don't sneak it in.

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

- Path: `POST /webhooks/{provider}` — not `/api/webhooks/{provider}`. (OEX
  used the latter; hq-x uses the bare `/webhooks` prefix because that's the
  shape Cal and EmailBison already use.)
- Signature verification posture per provider, configured via env.
- Webhook events land in the shared `webhook_events` table:
  `(provider_slug, event_key)` is unique and `status` flows
  `received → processed | dead_letter | replayed`.
- Dead-letter recovery is on-demand only. Each receiver exposes
  `POST /webhooks/{provider}/replay/{event_id}` (operator-gated) that
  re-projects a single stored event. No batch / cadence-driven replay yet.

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
6. `migrations/{NNNN}_{capability}_{slug}.sql` — single migration creating
   all tables for this capability. Don't preserve OEX's chronological
   evolution; collapse to the final shape.

## Direct-mail (Lob) specifics

- **Default key: `LOB_API_KEY`.** One per environment (dev/stg/prd uses
  whatever value is set in that Doppler config — typically a Lob test key
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
  for live and test mode, each with its own signing secret. Pieces
  created with `LOB_API_KEY` trigger webhooks signed with the LIVE
  secret; pieces created with `LOB_API_KEY_TEST` trigger TEST-signed
  webhooks. The receiver tries both — `signature_environment` on the
  stored event records which matched. LIVE is required in prd at boot.
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
  every webhook event writes one row. Reconstruct piece history without
  joining `webhook_events`.
- **Webhook event-name extraction** is at `payload.event_type.id` (Lob
  sends `event_type` as an object). Piece id is at `payload.reference_id`
  (top level) with `payload.body.id` as fallback. The OEX-derived code
  was reading `payload.type` and `payload.body.resource.id` — neither
  exists in real Lob payloads. See `app/webhooks/lob_normalization.py`.
- **Status-update vs. log-only events.** `normalize_lob_piece_status`
  returns `None` for events that should NOT change the piece's status:
  `viewed`, `informed_delivery.*`, and `return_envelope.*`. Those still
  append to `direct_mail_piece_events` for the audit log; the piece's
  `status` column stays where it was.
- **Suppression triggers.** Auto-populates `suppressed_addresses` on
  `piece.returned`, `piece.failed`, and `piece.certified.returned`.
  Engagement events (viewed, informed_delivery) never trigger suppression.

## Auth posture (current)

All `/direct-mail/*` routes are gated on `require_operator`. Pieces have no
tenant-linkage column yet — a future migration will add a brand-or-campaign
foreign key (Ben's call). Until then there is no safe way to scope pieces
per client, so client-role users get 403 on every direct-mail route.
Surface direct-mail analytics to clients via dedicated hq-x endpoints that
aggregate / filter on the linkage column once it lands.

## Verify-gate fail policy

The pre-send US address-verify gate calls Lob's `/v1/us_verifications`. If
Lob's verify endpoint is itself broken or slow, we **fail open**: log a
warning and proceed with the send. Lob does not auto-verify on piece create
— it'll happily accept whatever address you submit. Fail-closed (refuse to
send when verify is unreachable) was the alternative; rejected because one
verify outage would halt all outbound mail and cost more than the
occasional undeliverable.

## What's intentionally NOT here

- Per-org provider credentials (`provider_configs` JSONB). Single tenant.
- Multi-tenant `org_id` columns. Single tenant.
- `companies` / `clients` table. Doesn't exist yet; don't speculatively
  build it.
- `checks` (Lob piece type — financial, not marketing).
- US autocomplete, zip lookup, reverse-geocode (address-input UX helpers,
  no UI form yet).
- Identity validation (KYC; not relevant to current product).
- International address verification (US-only mailing for now).
- **NCOA** (National Change of Address). Lob exposes NCOA as a separate
  case-based workflow: submit a list of addresses, poll for results. That's
  its own polling story and needs its own scheduler integration. Deferred
  until hq-x picks a scheduler.
- Real metrics/log backend. `app/observability/` is a logging shim. Swap
  in a real facade later — the call sites stay identical.
- Scheduled / cadence-driven webhook replay. On-demand admin endpoint only.
- Orchestrator / step-executor integration. The OEX orchestrator branch was
  scheduler-driven; hq-x has no scheduler yet beyond the Trigger.dev
  health-check round-trip.

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
