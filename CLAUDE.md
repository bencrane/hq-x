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
