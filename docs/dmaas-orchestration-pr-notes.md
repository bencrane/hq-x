# DMaaS orchestration — post-ship notes

Third and final directive in the DMaaS series. The platform now runs
every long-running operation through Trigger.dev and exposes
customer-facing webhook subscriptions, multi-step drip scheduling,
and reconciliation crons.

Predecessors:
- `DIRECTIVE_HQX_DMAAS_FOUNDATION.md` — Lob send + Dub conversion
- `DIRECTIVE_HQX_DMAAS_HOSTED_PAGES.md` — landing pages, custom domains, opinionated API

## Slice summary

### Slice 1 — async activation foundation

`POST /api/v1/dmaas/campaigns` is **async-only now**. Returns
`202 Accepted` with a `job_id`. Customers poll
`GET /api/v1/dmaas/jobs/{job_id}` (or subscribe to a webhook —
Slice 2) for terminal state. Cancellation is via
`POST /api/v1/dmaas/jobs/{job_id}/cancel`.

**Breaking change**: any caller relying on synchronous `201 Created`
return semantics must migrate to the 202-then-poll pattern. There is
no sync compatibility shim. Customer count today is zero.

Tables added: `business.activation_jobs` with status (queued / running
/ succeeded / failed / cancelled / dead_lettered), JSONB payload +
result + error + history, idempotency_key (per-org unique), Trigger.dev
run id.

Trigger.dev task: `dmaas.process_activation_job`. Calls
`/internal/dmaas/process-job` which dispatches by job kind.

### Slice 2 — customer status webhooks

Stripe / GitHub-style webhooks. New endpoints:

```
POST   /api/v1/dmaas/webhooks                     subscribe (returns secret once)
GET    /api/v1/dmaas/webhooks                     list
GET    /api/v1/dmaas/webhooks/{id}                read
PATCH  /api/v1/dmaas/webhooks/{id}                update url/event_filter/state
DELETE /api/v1/dmaas/webhooks/{id}                pause (soft delete)
POST   /api/v1/dmaas/webhooks/{id}/rotate-secret  mint new secret
GET    /api/v1/dmaas/webhooks/{id}/deliveries     delivery audit log
POST   /api/v1/dmaas/webhooks/{id}/deliveries/{id}/retry    re-fire dead-lettered
```

`emit_event()` fans out to matching active subscriptions: a row goes
into `customer_webhook_deliveries` and a Trigger.dev task fires
`/internal/customer-webhooks/deliver`, which HMAC-signs the body
(header `X-HQX-Signature: sha256=<hex>`), POSTs to the customer's URL,
and records the result. Retry schedule: 1m, 5m, 30m, 2h, 12h, then
dead_lettered.

Tables: `business.customer_webhook_subscriptions`,
`business.customer_webhook_deliveries`. Subscription stores both the
plaintext secret (for outbound HMAC computation) and a one-way salted
hash (for any future verify-the-secret-you-stored flow).

Event vocabulary defined in the directive — wildcard `*` supported.

### Slice 3 — reconciliation crons

Five cron-driven reconcilers (Trigger.dev `schedules.task`):

| Cron | Cadence | Purpose |
|---|---|---|
| `dmaas.reconcile_stale_jobs` | daily | Mark `running` >2h as failed; mark `failed` >24h as dead_lettered. |
| `dmaas.reconcile_lob_pieces` | daily | Compare `direct_mail_pieces` count against Lob's `get_campaign` for active steps. |
| `dmaas.reconcile_dub_clicks` | daily | Compare local `dmaas_dub_events.click` count against Dub's analytics. |
| `dmaas.reconcile_webhook_replays` | daily | Surface `webhook_events` rows stuck non-terminal >1h. |
| `dmaas.reconcile_customer_webhook_deliveries` | every 15 min | Re-enqueue pending deliveries past `next_retry_at`. |

Each is feature-flag-gated via Doppler so an operator can disable a
single noisy reconciler without a deploy.

Each reconciler returns a structured `ReconciliationResult` with
counts (rows scanned, rows touched, drift found) plus an inspectable
details list — visible in the Trigger.dev run log.

### Slice 4 — multi-step scheduler

When step N's memberships all reach a terminal status, the
`lob_processor._maybe_transition_membership` hook calls
`step_scheduler.maybe_complete_step_and_schedule_next`, which:

1. Flips the step's status to `sent` (or `failed` if zero successful sends).
2. Emits `step.completed` (or `step.failed`).
3. If the step succeeded and there's a step N+1 in `pending`, schedules it.

Scheduling shape:
- Persist `business.activation_jobs` row with kind=`step_scheduled_activation`, payload `{step_id, delay_seconds}`.
- Enqueue `dmaas.scheduled_step_activation` Trigger.dev task with the same payload.
- The task uses Trigger.dev's `wait.for(duration)` — a durable-sleep primitive that survives deploys and restarts.
- After sleep, the task calls `/internal/dmaas/process-job` (the Slice 1 endpoint), which dispatches by job kind and activates the step.

Cancellation: pausing or archiving a `channel_campaign` now identifies
all in-flight step ids and calls `step_scheduler.cancel_scheduled_step`
for each. The underlying `activation_jobs.cancel_job` calls
Trigger.dev's run-cancel API, interrupting `wait.for()`.

## Configuration changes

New `app/config.py` settings:

```
TRIGGER_API_KEY                            # tr_dev_... / tr_prod_... — for hq-x → Trigger.dev
TRIGGER_API_BASE_URL                       # default https://api.trigger.dev (override for tests)
DMAAS_RECONCILE_STALE_JOBS_ENABLED         # default true
DMAAS_RECONCILE_LOB_ENABLED                # default true
DMAAS_RECONCILE_DUB_ENABLED                # default true
DMAAS_RECONCILE_WEBHOOK_REPLAYS_ENABLED    # default true
DMAAS_RECONCILE_CUSTOMER_WEBHOOKS_ENABLED  # default true
DMAAS_RECONCILE_STALE_JOB_THRESHOLD_HOURS  # default 2
DMAAS_RECONCILE_DEAD_LETTER_DELAY_HOURS    # default 24
```

The existing `TRIGGER_SHARED_SECRET` continues to authenticate Trigger.dev
tasks calling back into hq-x's `/internal/*` routes. The new
`TRIGGER_API_KEY` is the inverse direction: hq-x → Trigger.dev, used to
enqueue tasks via `POST /api/v1/tasks/{taskIdentifier}/trigger` and
cancel runs via `POST /api/v2/runs/{runId}/cancel`.

## New migrations

```
20260430T184850_activation_jobs.sql
20260430T185749_customer_webhook_subscriptions.sql
```

## Caveats

- `test_dmaas_campaigns_api.py` was rewritten to match the new async
  contract. The previous synchronous-pipeline tests no longer apply;
  the activation pipeline itself moved to
  `app/services/dmaas_campaign_activation.py` and is exercised through
  the internal job-processing endpoint tests.
- `lob_pieces` reconciliation only surfaces drift counts; it does not
  reconstruct missing rows. Filling gaps requires a second
  `direct_mail_pieces` upsert path the Lob projector doesn't currently
  expose, so V2 work.
- `dub_clicks` reconciliation surfaces drift counts but does not
  reconstruct missed `dmaas_dub_events` rows — Dub's analytics
  endpoint is aggregated. Customers see correct totals via Dub's
  source-of-truth view.
- `webhook_replays` is V1-conservative: it surfaces non-terminal
  `webhook_events` rows but does not auto-replay. The existing
  per-provider admin replay endpoints continue to be the operator
  interface.

## Out of scope (deferred)

Per directive §7:

- Async refactor of legacy `POST /api/v1/channel-campaign-steps/{id}/activate`. Stays sync.
- Per-task observability dashboards beyond Trigger.dev's built-in.
- Replay for arbitrary historical events to a webhook subscription.
- Cost tracking per org per Trigger.dev invocation.
- Trigger.dev v5 migration.
- Self-hosted Trigger.dev.
