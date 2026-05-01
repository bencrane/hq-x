# hq-x — Claude Code working notes

## Verifying spec data (DMaaS / Lob mailer specs)

`data/lob_mailer_specs.json` is the canonical Lob print-spec data. Migrations
0017 + 0019 seed `direct_mail_specs` from it, and `app/dmaas/service.py`
turns rows into solver-ready zone bindings.

When you change the spec JSON or a face/folding rule, run the sync script
to verify the data still passes both PDF MediaBox checks and zone-catalog
sanity checks (non-overlap, panel-derivation, glue/fold geometry):

```
uv run python -m scripts.sync_lob_specs
```

The script:

1. Downloads each spec's `template_pdf_url` and compares MediaBox to the
   declared bleed/trim dims (±0.01" tolerance).
2. For each v1 spec (4 postcards + 3 self_mailer bifolds), runs
   `bind_spec_zones` and asserts every required zone is present, all
   `*_safe` rectangles fit inside their parent surface, and the
   directive's mutual-non-overlap invariants hold on the back face
   (postcards) / cover panel (self-mailers).

Exit code is non-zero on any failure. Run before committing migrations or
JSON edits in this area.

Pytest also exercises the same invariants:

```
uv run pytest tests/test_dmaas_spec_binding.py
```

## Verifying scaffold briefs (DMaaS v1 scaffold library)

`data/dmaas_scaffold_briefs/*.json` holds the human-reviewable briefs the
`dmaas-scaffold-author` managed agent (in `managed-agents-x`) authors
against. `data/dmaas_v1_scaffolds.json` carries the resulting scaffold
DSL + prop_schema + placeholder content, one entry per brief. The two
must stay in sync.

When you change either file (add a brief, retune a strategy, edit a DSL),
run the verifier — it runs offline (no DB / no managed-agent session
required) and is the CI gate:

```
uv run python -m scripts.verify_scaffold_briefs
```

It loads each brief, finds the matching scaffold by slug, runs the
solver against every entry in `compatible_specs`, then re-runs the
brief's `acceptance_rules` against the resolved positions. Exit
non-zero on any failure.

Pytest covers the brief library invariants statically (no DB):

```
uv run pytest tests/test_dmaas_scaffold_briefs.py tests/test_dmaas_briefs.py
```

To persist the scaffolds to `dmaas_scaffolds` (idempotent, with audit
trail rows in `dmaas_scaffold_authoring_sessions`):

```
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_dmaas_v1_scaffolds
```

## Migration filename convention

`scripts/migrate.py` applies `migrations/*.sql` in lexical order. New
migrations should use a UTC-timestamp prefix (`YYYYMMDDTHHMMSS_<slug>.sql`)
rather than a numeric prefix — timestamps avoid collisions when multiple
agents work in parallel and lex-sort cleanly after the legacy `00NN_*` files.

## DMaaS async orchestration (Trigger.dev)

`POST /api/v1/dmaas/campaigns` is **async-only**: it returns 202 with a
`job_id` and a Trigger.dev task picks the work up. The internal endpoint
that processes jobs lives at `/internal/dmaas/process-job` and is called
back into by the `dmaas.process_activation_job` task. See
`docs/dmaas-orchestration-pr-notes.md` for the full surface.

Key endpoints:

- `POST /api/v1/dmaas/campaigns` → 202 with `{job_id, status}`
- `GET  /api/v1/dmaas/jobs/{job_id}` → full job row with status / result / error / history
- `POST /api/v1/dmaas/jobs/{job_id}/cancel` → cancel queued or running job

Customer-facing webhook subscriptions:

- `POST /api/v1/dmaas/webhooks` (returns plaintext `secret` exactly once)
- `GET / PATCH / DELETE /api/v1/dmaas/webhooks/{id}`
- `POST /api/v1/dmaas/webhooks/{id}/rotate-secret`
- `GET /api/v1/dmaas/webhooks/{id}/deliveries`
- `POST /api/v1/dmaas/webhooks/{id}/deliveries/{delivery_id}/retry`

Header on outbound deliveries: `X-HQX-Signature: sha256=<hex>` over the
raw body, keyed by the subscription's plaintext secret.

Reconciliation crons (each gated by a `DMAAS_RECONCILE_*_ENABLED` flag):

- `dmaas.reconcile_stale_jobs` (daily)
- `dmaas.reconcile_lob_pieces` (daily)
- `dmaas.reconcile_dub_clicks` (daily)
- `dmaas.reconcile_webhook_replays` (daily)
- `dmaas.reconcile_customer_webhook_deliveries` (every 15 min)

Multi-step scheduler: `app/services/step_scheduler.py`. After step N
completes, `maybe_complete_step_and_schedule_next` enqueues
`dmaas.scheduled_step_activation` which uses Trigger.dev's `wait.for()`
durable sleep for N+1's `delay_days_from_previous`. Pause/archive on
the parent channel_campaign cancels the in-flight runs via Trigger.dev's
run-cancel API.

## Reserved-audience tie-in (hq-x ↔ data-engine-x)

`business.org_audience_reservations` (mig
`20260430T220819_org_audience_reservations.sql`) couples a paying
`business.organizations` row to a frozen DEX `ops.audience_specs` row.
The DEX spec id IS the `data_engine_audience_id` — hq-x does not mint a
second identifier. Cached fields (`source_template_slug`,
`source_template_id`, `audience_name`) make the row self-describing
without a DEX round-trip.

Distinct from `business.audience_drafts` (user-owned, pre-reservation,
no DEX spec yet) — reservations are org-owned and post-reservation.

Routes (all under `verify_supabase_jwt`, prefix
`/api/audience-reservations`):

- `POST /` — create-or-upsert reservation. Verifies the spec exists in
  DEX via `get_audience_descriptor` (passes the user's hq-x JWT through),
  then UPSERTs on `(organization_id, data_engine_audience_id)`.
- `GET /` — list reservations for the user's active org.
- `GET /{id}` — single reservation (cross-org returns 404, not 403).
- `GET /{id}/audience` — composite: `{reservation, descriptor, count}`.
- `GET /{id}/members?limit&offset` — paginated DEX preview passthrough.

DEX client lives at `app/services/dex_client.py`. Auth resolution per
call: caller-supplied `bearer_token` first (the user's hq-x Supabase JWT
forwarded through), otherwise `settings.DEX_SUPER_ADMIN_API_KEY`. Both
go in the `Authorization: Bearer ...` header — DEX's
`_resolve_super_admin_from_api_key` does a string compare on the bearer
token against `super_admin_api_key` (no separate header).

DAT prototype fixture seed:

```
DEX_BASE_URL=https://api.dataengine.run \
    doppler --project hq-x --config dev run -- \
    uv run python -m scripts.seed_dat_audience_reservation
```

Authenticates server-to-server via `DEX_SUPER_ADMIN_API_KEY` and exercises
`get_audience_descriptor`, `count_audience_members`, and paginated
`list_audience_members` against the live DEX dev environment.

## Exa research prototype

`POST /api/v1/exa/jobs` enqueues an async Exa research run that
persists the raw payload to either hq-x's own `exa.exa_calls` table or
to data-engine-x's mirror table, based on a per-run `destination` flag
(`hqx` | `dex`). Trigger.dev task `exa.process_research_job` drives
the work via `/internal/exa/jobs/{id}/process`.

- Public surface: `POST /api/v1/exa/jobs`, `GET /api/v1/exa/jobs/{id}`
- Internal callback (Trigger.dev → hq-x): `POST /internal/exa/jobs/{id}/process`
- Tables: `exa.exa_calls` (raw archive in both hq-x and DEX),
  `business.exa_research_jobs` (orchestration row, hq-x only)
- DEX write surface (hq-x → DEX): `POST /api/internal/exa/calls`
  (super-admin bearer)
- Client lives at `app/services/exa_client.py`; auth header is
  `x-api-key` against `https://api.exa.ai`. The research endpoint is
  poll-based — the client wraps the create+poll loop inline.

Per-objective derived tables are intentionally out of scope here; the
table is a request/response audit log, and per-use-case projections
land in follow-up directives.

End-to-end seed (hits the live Exa API + writes to both DBs):

```
DEX_BASE_URL=https://api.dataengine.run \
DEX_DB_URL_POOLED='<DEX dex/prd pooled url>' \
    doppler --project hq-x --config dev run -- \
    uv run python -m scripts.seed_exa_research_demo
```

Creates a `slug='exa-demo'` org if missing, fires one search-job at each
destination, drives both through to terminal state via the same code
path Trigger.dev would, and SELECTs the resulting `exa.exa_calls` row
from each DB. Exit 0 only when both jobs succeed and both rows exist.

## GTM-initiative pipeline (slice 1)

`business.gtm_initiatives` couples a Ben-owned brand + a paying
demand-side partner + that partner's contract + a frozen DEX audience
spec + a partner-research run into the campaign-strategy artifact that
downstream materializers consume. Two subagents drive the pre-launch
phase:

- **Subagent 1 — strategic-context researcher** (`app/services/strategic_context_researcher.py`).
  Audience-scoped, operator-voice-sourced second Exa research run.
  Reuses `business.exa_research_jobs` with
  `objective='strategic_context_research'`,
  `objective_ref='initiative:<uuid>'`. The post-process-by-objective
  dispatcher in `app/routers/internal/exa_jobs.py` flips the initiative
  to `strategic_research_ready` when the underlying exa job succeeds.
- **Subagent 2 — strategy synthesizer** (`app/services/strategy_synthesizer.py`).
  First hq-x → Anthropic call. Reads partner research +
  strategic-context research + audience descriptor + brand `.md` files
  + partner contract; emits `data/initiatives/<initiative_id>/campaign_strategy.md`
  with a YAML front-matter header. Validated for shape; one retry on
  bad YAML; `failed_synthesis.md` persisted on a second failure.

Public surface (under `verify_supabase_jwt`, prefix
`/api/v1/initiatives`):

- `POST /` — create an initiative.
- `GET /{id}` — fetch one (cross-org returns 404).
- `POST /{id}/run-strategic-research` — fires subagent 1 (202).
- `POST /{id}/synthesize-strategy` — fires subagent 2 (202). 409 if
  strategic-context-research hasn't completed.

Internal callback (Trigger.dev → hq-x):
`POST /internal/initiatives/{id}/process-synthesis`.

Subagents 3–7 (channel/step materializer, audience materializer,
per-recipient creative, landing pages, voice agent) are out of scope
for this slice.

End-to-end seed (drives the full path against dev DB + DEX + Exa +
Anthropic, bypassing Trigger for ergonomics):

```
DEX_BASE_URL=https://api.dataengine.run \
    doppler --project hq-x --config dev run -- \
    uv run python -m scripts.seed_dat_gtm_initiative
```

Pre-req: `scripts/seed_dat_audience_reservation` must already have
materialized the DAT audience spec in DEX (the gtm-initiative seed
resolves the spec id from the cached reservation row). Exit 0 only
when both subagents succeed and `data/initiatives/<id>/campaign_strategy.md`
is on disk with valid YAML front-matter.
