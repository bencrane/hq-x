# Directive — DMaaS orchestration: async activation, multi-step scheduler, reconciliation, customer webhooks

**For:** an implementation agent shipping the production-grade orchestration layer that turns the DMaaS platform from "works for early customers willing to retry" into "works at scale with SLOs." Worktree path: `/Users/benjamincrane/hq-x` (or any worktree under it). Branch from `main`.

This is the **third and final** of the DMaaS-product directives:

* **Directive 1** ([`DIRECTIVE_HQX_DMAAS_FOUNDATION.md`](DIRECTIVE_HQX_DMAAS_FOUNDATION.md)) — Lob send + Dub conversion analytics. Merged.
* **Directive 2** ([`DIRECTIVE_HQX_DMAAS_HOSTED_PAGES.md`](DIRECTIVE_HQX_DMAAS_HOSTED_PAGES.md)) — hosted landing pages, custom domains, opinionated single-call API. Merged.
* **Directive 3 (this file)** — Trigger.dev orchestration: every long-running operation moves from synchronous request handling into durable Trigger.dev tasks. Customer-facing status webhooks. Reconciliation crons. Multi-step campaign scheduling.

---

## 0. Why this directive exists

The platform sends + tracks + captures leads end to end after Directives 1 and 2. But it's all synchronous and point-in-time. Five real production problems remain:

1. **`POST /api/v1/dmaas/campaigns` ties up an HTTP worker for 15–60 seconds** on a 5,000-recipient activation (Dub mint + Lob upload). One slow customer's campaign blocks every other request on that worker. A deploy mid-activation leaves state inconsistent (Lob campaign created, audience upload partial, no clean recovery).
2. **No way to fire step N+1 N days after step N.** The canonical doc has flagged the multi-step scheduler as out-of-scope since #28 because there was no orchestrator. Customers paying for direct mail expect drip sequences ("postcard day 0, letter day 14, postcard day 30"). This is a missing core capability.
3. **No reconciliation.** Lob webhooks occasionally drop. Dub clicks occasionally don't fire our receiver. Today there's no daily "ask the providers what they actually have, fill in the gaps" pass. Drift accumulates silently.
4. **No customer-facing webhook subscriptions.** Customer dashboards refresh by polling. For event-driven integrations (CRM sync, Slack notifications, custom workflows), customers need first-class webhook subscriptions with HMAC signing — not "go set up RudderStack destinations against our shared workspace."
5. **No durability for failed jobs.** When activation fails partway, there's no job record to retry from, no operator inspection surface, no automated alerting.

All five resolve to the same architectural move: **route long-running and time-based work through Trigger.dev**, with hq-x exposing internal endpoints that Trigger.dev tasks call back into. The wiring already exists (creds in Doppler, `src/trigger/` set up with `health-check.ts` / `voice-callback-runner.ts` as reference patterns, `app/routers/internal/scheduler.py` and friends as the auth shape). This directive extends the established pattern across the DMaaS surface.

---

## 1. Architectural decisions locked in (do not relitigate)

### 1.1 Trigger.dev tasks live in TypeScript at `src/trigger/`

The existing convention. TS tasks are thin shims that call hq-x's `/internal/*` Python routes via the existing [`src/trigger/lib/hqx-client.ts`](src/trigger/lib/hqx-client.ts) `callHqx()` helper. **All real business logic lives in Python.** TS code is for: scheduling, durable sleep, retry orchestration, fan-out. Nothing more.

This means:
* Every new TS task in this directive is a 30-line file that calls one or more hq-x internal endpoints.
* Every new internal endpoint in `app/routers/internal/` does the actual work, gated by `verify_trigger_secret`.
* DB writes happen Python-side. TS never touches the DB directly.

### 1.2 Job state lives in Postgres, not in Trigger.dev

Trigger.dev has its own run state, but it's not our source of truth. Every async operation creates a row in `business.activation_jobs` (or sibling tables for other job kinds) with status / payload / result / error columns. The Python code reads + writes this row; the TS task is just the executor that fires the call. If Trigger.dev loses a run, the Postgres row is the recovery anchor.

Reasons:
* Decouples customer-visible job status from Trigger.dev's internal state.
* Reconciliation tasks (Slice 3) can scan our DB without depending on Trigger.dev's API.
* Operator inspection happens in Postgres, not in a third-party dashboard.

### 1.3 Async by default for long-running endpoints

`POST /api/v1/dmaas/campaigns` (and the other slow operations) become **async-only** in this directive. They return `202 Accepted` with a `job_id` and the customer polls `GET /api/v1/dmaas/jobs/{job_id}` (or subscribes to a customer webhook for completion).

**No sync compatibility shim.** This is a breaking change for any caller that was relying on sync return semantics, but the customer count today is zero — accepting the breaking change now beats supporting both modes forever. Document the migration explicitly in the post-ship doc.

### 1.4 Customer webhooks are first-class, not a RudderStack workaround

RudderStack remains the firehose for events going to destinations the customer manages (their warehouse, Mixpanel, etc.). Customer webhook subscriptions are a different product surface: explicit subscribe/list/update/delete API, HMAC-signed deliveries, retry with backoff, dead-lettering. This is the standard SaaS webhooks pattern (Stripe, GitHub, Linear).

Both coexist. Don't try to consolidate.

### 1.5 Trigger.dev's `wait.for()` for the multi-step scheduler

Use Trigger.dev's durable sleep primitive (`wait.for(duration)`) for "schedule step N+1 in X days." Don't build a polling cron that scans for due steps. The durable-sleep approach is exactly what Trigger.dev is built for — survives deploys, survives restarts, no polling overhead, time-accurate.

Cancellation requires explicit support. When a campaign is paused/archived, the in-flight scheduled task must be cancelled. Trigger.dev supports this via run cancellation API; document the pattern.

### 1.6 No new external dependencies

Trigger.dev SDK is already pinned at `4.4.4`. Don't upgrade. Don't add new TypeScript dependencies. Don't add new Python dependencies unless absolutely necessary (e.g., HMAC library for webhook signing — Python's stdlib `hmac` is enough, so probably nothing).

### 1.7 Reconciliation is read-only-ish

Reconciliation crons read provider state (Lob campaigns, Dub analytics) and **fill gaps** in our DB. They never mutate provider state. They emit `reconciliation.drift_found` events with structured detail so operators can monitor.

### 1.8 ClickHouse + RudderStack stays as-is

Same as Directive 2. ClickHouse unprovisioned. RudderStack write fan-out continues — every new event from this directive (`step.activated`, `step.scheduled`, `customer_webhook.delivered`, `reconciliation.drift_found`, etc.) goes through `emit_event()` and fans out automatically.

---

## 2. Hard rules

1. **Trigger.dev tasks call hq-x via `/internal/*` only.** No direct DB access from TS. No bypassing `verify_trigger_secret`.
2. **Job state is the source of truth in Postgres.** Trigger.dev run state is operational metadata, not the customer-visible truth.
3. **Six-tuple is sacred.** Every async operation that emits events carries the full hierarchy + recipient_id where applicable. Job tasks resolve and emit the same canonical six-tuple as their sync predecessors.
4. **Org isolation tested per endpoint.** Customer-facing endpoints (`POST /jobs`, `GET /jobs/{id}`, webhook subscriptions) get the standard cross-org guard tests.
5. **Customer webhook delivery failures retry max 5x over 24h** with exponential backoff (1m, 5m, 30m, 2h, 12h). After the 5th failure, mark subscription as `delivery_failing` and emit `customer_webhook.failing` event so the operator can investigate.
6. **HMAC-SHA256 for customer webhook signing.** Header `X-HQX-Signature: sha256=<hex>` over the raw body. Document the verification recipe in the customer-facing API docs.
7. **Idempotency end-to-end.** `Idempotency-Key` header on async endpoints maps to a natural key on `activation_jobs`. Replays return the same `job_id`. Job processing itself uses job-scoped keys for downstream Lob/Dub calls.
8. **Cancellation is explicit.** When a campaign is paused/archived, scheduled tasks for its steps are cancelled via Trigger.dev's run cancellation API + the job row marked `cancelled`. Don't rely on the task discovering cancellation mid-flight.
9. **Reconciliation is opt-in per cron.** Every reconciliation task has a feature flag in env config (`DMAAS_RECONCILE_LOB_ENABLED`, etc.) that defaults to `true` but can be disabled instantly without a deploy.
10. **No silent retries.** Every retry attempt on a job logs a structured `job.retry` event with the attempt number and reason. Customers see these in `GET /jobs/{id}` (`history` array).
11. **Trigger.dev tasks must be deterministic in their hq-x callouts.** Same job → same internal endpoint → same parameters. Helps with debug + replay.
12. **No new emojis in code, comments, commit messages, or docs.**

---

## 3. Slices to ship (in order)

Each slice = one commit + one PR against `main`. Land each before opening the next.

### Slice 1 — Async activation foundation

**Goal:** the long-running `POST /api/v1/dmaas/campaigns` flow becomes async. Returns 202 with a `job_id`. Trigger.dev task processes the job. Customer polls `GET /api/v1/dmaas/jobs/{job_id}`.

This is the keystone slice. Every later slice builds on the job pattern established here.

**File touchpoints:**

* New migration: `migrations/<timestamp>_activation_jobs.sql`:

  ```sql
  CREATE TABLE business.activation_jobs (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
      brand_id UUID NOT NULL REFERENCES business.brands(id) ON DELETE RESTRICT,
      kind TEXT NOT NULL CHECK (kind IN (
          'dmaas_campaign_activation',
          'step_activation',
          'step_scheduled_activation'
      )),
      status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
          'queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead_lettered'
      )),
      idempotency_key TEXT,
      payload JSONB NOT NULL,
      result JSONB,
      error JSONB,
      history JSONB NOT NULL DEFAULT '[]'::jsonb,
      trigger_run_id TEXT,
      attempts INT NOT NULL DEFAULT 0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      started_at TIMESTAMPTZ,
      completed_at TIMESTAMPTZ,
      dead_lettered_at TIMESTAMPTZ
  );

  CREATE UNIQUE INDEX idx_aj_org_idempotency
      ON business.activation_jobs (organization_id, idempotency_key)
      WHERE idempotency_key IS NOT NULL;
  CREATE INDEX idx_aj_status ON business.activation_jobs (status);
  CREATE INDEX idx_aj_org_created ON business.activation_jobs (organization_id, created_at DESC);
  ```

* Modify [`app/routers/dmaas_campaigns.py`](app/routers/dmaas_campaigns.py) — `POST /api/v1/dmaas/campaigns`:
  * Validates request as today.
  * Inserts an `activation_jobs` row with kind=`dmaas_campaign_activation`, status=`queued`, payload=request body, idempotency_key from header.
  * If idempotency_key matches an existing row, returns the existing job_id without creating a new row.
  * Calls Trigger.dev's HTTP API to enqueue `dmaas.process-activation-job` with `{job_id}`. Use `@trigger.dev/sdk` Python alternative: hit Trigger.dev's `/api/v1/tasks/{taskIdentifier}/trigger` endpoint directly with `TRIGGER_ACCESS_TOKEN`. (No Python SDK exists; raw HTTP is fine.)
  * Returns `202 Accepted` with `{job_id, status: "queued"}`.
  * On Trigger.dev enqueue failure: mark job as `failed` with error=enqueue error, return 503.

* New endpoint `GET /api/v1/dmaas/jobs/{job_id}`:
  * Org-scoped via `require_org_context` — 404 if job's `organization_id` doesn't match auth.
  * Returns full job row: status, payload, result (if succeeded), error (if failed), history, timestamps.

* New endpoint `POST /api/v1/dmaas/jobs/{job_id}/cancel`:
  * Marks job `cancelled` if status is `queued` or `running` (best-effort for `running`).
  * Calls Trigger.dev's run cancellation API.
  * Returns updated job row.

* New internal endpoint `app/routers/internal/dmaas_jobs.py`:
  * `POST /internal/dmaas/process-job` — gated by `verify_trigger_secret`. Body: `{job_id, trigger_run_id}`.
  * Loads job row, transitions to `running`, persists `started_at` + `trigger_run_id`.
  * Dispatches by `kind` — for `dmaas_campaign_activation`, runs the existing inline DMaaS campaign creation flow (the one Directive 2's Slice 6 ships) by importing the service function and calling it with the persisted payload.
  * On success: transitions to `succeeded`, persists `result` + `completed_at`. Emits `job.succeeded` event.
  * On failure: transitions to `failed`, persists `error` + `completed_at`. Appends to `history`. Emits `job.failed` event. Re-raises so Trigger.dev marks the run as failed (allows the SDK's retry policy to take over for retryable errors).

* New TS task `src/trigger/dmaas-process-activation-job.ts`:
  * `task({ id: "dmaas.process_activation_job", retry: { maxAttempts: 3, ... }, run: async ({ job_id }, { ctx }) => callHqx("/internal/dmaas/process-job", { job_id, trigger_run_id: ctx.run.id }) })`.
  * That's basically the entire file. Thin shim.

* Pydantic models in `app/models/activation_jobs.py`:
  * `ActivationJobStatus`, `ActivationJobKind`, `ActivationJobResponse`, `ActivationJobHistoryEntry`.

* Helper in `app/services/activation_jobs.py`:
  * `create_job(...)`, `get_job(...)`, `transition_job(...)`, `append_history(...)`, `cancel_job(...)`.
  * `enqueue_via_trigger(job, task_identifier)` — wraps the raw Trigger.dev HTTP API call.

**Required behavior:**

1. Customers see immediate 202 instead of waiting 60s.
2. Job rows are durable across deploys/restarts.
3. Trigger.dev SDK retries (via TS task config) handle transient hq-x failures (network, DB blip). Persistent failures get the job marked `failed` after `maxAttempts` exhausted.
4. After 24h in `failed` state with no retry, daily reconciliation cron (Slice 3) marks as `dead_lettered`.
5. Cross-org isolation: `GET /jobs/{id}` for a job in org B from an org A user → 404.
6. Idempotency-Key replays return the original job_id, never create a duplicate.

**Tests:**

* `tests/test_activation_jobs_service.py` — pure service tests for create/get/transition/cancel.
* `tests/test_activation_jobs_router.py` — endpoint tests (POST creates job + enqueues, GET returns row, cancel transitions, cross-org guard).
* `tests/test_dmaas_jobs_internal.py` — the `/internal/dmaas/process-job` endpoint, including success/failure transitions and history appending.
* Integration smoke (manual): trigger a real activation in dev, watch the job progress through `queued → running → succeeded`, verify Lob test pieces queued and Dub links minted as in Directive 2.

**Verification (in PR description):**

* Trigger a real `POST /api/v1/dmaas/campaigns` against dev with 3 recipients.
* Confirm 202 returned with `job_id`.
* Poll `GET /api/v1/dmaas/jobs/{job_id}` until status=succeeded (~30s).
* Confirm Lob test campaign created + pieces queued, Dub links minted, landing page renders.
* Document the job_id and Trigger.dev run URL in the PR.

---

### Slice 2 — Customer status webhooks

**Goal:** customers subscribe to event types ("page.submitted", "step.completed", etc.) and we POST HMAC-signed payloads to their URL with retries. The standard SaaS webhook pattern.

**File touchpoints:**

* New migration: `migrations/<timestamp>_customer_webhook_subscriptions.sql`:

  ```sql
  CREATE TABLE business.customer_webhook_subscriptions (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      organization_id UUID NOT NULL REFERENCES business.organizations(id) ON DELETE RESTRICT,
      brand_id UUID REFERENCES business.brands(id) ON DELETE CASCADE,
      url TEXT NOT NULL,
      secret_hash TEXT NOT NULL,           -- sha256 of secret + per-env salt
      event_filter TEXT[] NOT NULL,        -- ['step.completed', 'page.submitted', '*']
      state TEXT NOT NULL DEFAULT 'active' CHECK (state IN (
          'active', 'paused', 'delivery_failing'
      )),
      consecutive_failures INT NOT NULL DEFAULT 0,
      last_delivery_at TIMESTAMPTZ,
      last_failure_at TIMESTAMPTZ,
      last_failure_reason TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );

  CREATE TABLE business.customer_webhook_deliveries (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      subscription_id UUID NOT NULL REFERENCES business.customer_webhook_subscriptions(id) ON DELETE CASCADE,
      event_name TEXT NOT NULL,
      event_payload JSONB NOT NULL,
      attempt INT NOT NULL DEFAULT 1,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
          'pending', 'succeeded', 'failed', 'dead_lettered'
      )),
      response_status INT,
      response_body TEXT,
      attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      next_retry_at TIMESTAMPTZ
  );

  CREATE INDEX idx_cws_org ON business.customer_webhook_subscriptions (organization_id);
  CREATE INDEX idx_cwd_subscription ON business.customer_webhook_deliveries (subscription_id, attempted_at DESC);
  CREATE INDEX idx_cwd_pending ON business.customer_webhook_deliveries (next_retry_at) WHERE status = 'pending';
  ```

* New customer-facing endpoints in `app/routers/customer_webhooks.py`:
  * `POST /api/v1/dmaas/webhooks` — body: `{url, event_filter[], brand_id?}`. Returns subscription with one-time-revealed `secret` (the customer must store it; we only store the hash).
  * `GET /api/v1/dmaas/webhooks` — list org's subscriptions.
  * `PATCH /api/v1/dmaas/webhooks/{id}` — update url / event_filter / state.
  * `DELETE /api/v1/dmaas/webhooks/{id}` — soft-delete (set state=paused).
  * `POST /api/v1/dmaas/webhooks/{id}/rotate-secret` — generate new secret, return once, store new hash.
  * `GET /api/v1/dmaas/webhooks/{id}/deliveries` — paginated delivery log (status, response, retry attempts).

* Modify [`app/services/analytics.py`](app/services/analytics.py) `emit_event()`:
  * After existing log + ClickHouse + RudderStack writes, query `customer_webhook_subscriptions` matching this event's organization_id + event_name (or `'*'`) + active state.
  * For each match, insert a `customer_webhook_deliveries` row with status=`pending` and enqueue a Trigger.dev task to deliver.
  * Fire-and-forget — never raise.

* New internal endpoint `app/routers/internal/customer_webhooks.py`:
  * `POST /internal/customer-webhooks/deliver` — body: `{delivery_id}`. Loads delivery row + subscription. POSTs HMAC-signed payload to subscription.url. Updates delivery status based on response. On failure, schedules next retry (or dead-letters after 5 attempts).

* New TS task `src/trigger/customer-webhook-deliver.ts`:
  * Receives `{delivery_id}`, calls `/internal/customer-webhooks/deliver`. Trigger.dev's retry policy handles transient hq-x failures; the internal endpoint handles HTTP-target retries.

* HMAC signing helper `app/services/customer_webhook_signing.py`:
  * `sign_payload(secret, body) -> str` returns `sha256=<hex>`.
  * `verify_signature(secret, body, signature) -> bool` for testing.

**Event vocabulary** (the agent should choose the canonical set; recommended):

* Job lifecycle: `job.queued`, `job.started`, `job.succeeded`, `job.failed`, `job.cancelled`, `job.dead_lettered`
* Step lifecycle: `step.scheduled_for_activation`, `step.activated`, `step.completed`, `step.failed`
* Per-piece (direct mail): `piece.mailed`, `piece.in_transit`, `piece.delivered`, `piece.returned`, `piece.failed`
* Conversion: `dub.click`, `dub.lead`, `page.viewed`, `page.submitted`
* Campaign lifecycle: `campaign.activated`, `campaign.completed`, `campaign.paused`, `campaign.archived`
* Reconciliation: `reconciliation.drift_found` (operator-only — recommend not surfacing to customers by default)
* Wildcard: `*` matches all

**Required behavior:**

1. Subscription `secret` returned ONCE on creation; we store only `secret_hash`. Verify on rotation.
2. HMAC over raw request body, header `X-HQX-Signature: sha256=<hex>`.
3. Delivery payload shape:
   ```json
   {
     "id": "wh_evt_...",
     "subscription_id": "...",
     "event": "page.submitted",
     "occurred_at": "2026-04-30T...",
     "organization_id": "...",
     "data": { /* event-specific properties; full six-tuple included */ }
   }
   ```
4. Delivery retry schedule: 1m, 5m, 30m, 2h, 12h. After 5 failed attempts → status=`dead_lettered`, subscription state=`delivery_failing` (after 10 consecutive failures).
5. Customers can replay a dead-lettered delivery via `POST /api/v1/dmaas/webhooks/{id}/deliveries/{delivery_id}/retry`.
6. Cross-org: webhook subscriptions are strictly org-scoped. Brand_id optional further filter (only fire for events in that brand).
7. Form_data values in `page.submitted` events ARE included in the webhook payload (customer owns this data, opposite of the Directive 2 RudderStack stance — RudderStack got field NAMES only because the destination is a third-party tool the customer didn't explicitly authorize for PII).

**Tests:**

* `tests/test_customer_webhook_subscriptions_service.py` — CRUD, secret hashing, secret rotation.
* `tests/test_customer_webhook_subscriptions_router.py` — endpoint tests + cross-org guard.
* `tests/test_customer_webhook_signing.py` — HMAC sign/verify with fixed test vectors.
* `tests/test_customer_webhook_delivery.py` — internal endpoint mocked HTTP target, success path + retry-after-failure path + dead-letter path.
* `tests/test_emit_event_webhook_fanout.py` — verify `emit_event` enqueues deliveries for matching subscriptions; doesn't enqueue for non-matching event filters or wrong-org subscriptions.

**Verification:**

* Subscribe via test customer to `*`, target a webhook.site URL.
* Trigger any DMaaS event (form submission, manual emit).
* Observe delivery on webhook.site within seconds; verify HMAC signature.
* Document the subscription_id + delivery_id + screenshot.

---

### Slice 3 — Reconciliation crons

**Goal:** daily background tasks pull provider state and fill gaps in our DB, catching dropped webhooks and stale jobs.

**File touchpoints:**

* New TS tasks (each ~30 lines, all calling internal endpoints):
  * `src/trigger/dmaas-reconcile-stale-jobs.ts` — daily, calls `/internal/dmaas/reconcile/stale-jobs`.
  * `src/trigger/dmaas-reconcile-lob-pieces.ts` — daily, calls `/internal/dmaas/reconcile/lob`.
  * `src/trigger/dmaas-reconcile-dub-clicks.ts` — daily, calls `/internal/dmaas/reconcile/dub`.
  * `src/trigger/dmaas-reconcile-webhook-replays.ts` — daily, calls `/internal/dmaas/reconcile/webhook-replays`.
  * `src/trigger/dmaas-reconcile-customer-webhook-deliveries.ts` — every 15m, retries `pending` deliveries past `next_retry_at`.

* New internal router `app/routers/internal/dmaas_reconcile.py`:
  * `POST /internal/dmaas/reconcile/stale-jobs` — finds `activation_jobs` in `running` >2h. Marks as `failed` with error=`stale_running_state`. After 24h in `failed` with no retry → `dead_lettered`. Emits `reconciliation.drift_found` per row touched.
  * `POST /internal/dmaas/reconcile/lob` — finds `channel_campaign_steps` in status `scheduled` or `sending` whose `scheduled_send_at` is past. Calls Lob's `GET /v1/campaigns/{id}` to get current state. For each piece returned that we don't have in `direct_mail_pieces`, inserts the missing row. Emits `reconciliation.drift_found` with detail=`{kind: 'missing_piece', step_id, lob_piece_id}`.
  * `POST /internal/dmaas/reconcile/dub` — for each step active in the last 24h, calls Dub's `GET /analytics?linkId=...` for each `dmaas_dub_links` row. Compares click count to our `dmaas_dub_events` count. Doesn't fully reconstruct missed events (Dub's analytics endpoint is aggregated), but logs drift counts. Optional V2: if the gap is >0, surface a "click count drift" event so operator knows to investigate.
  * `POST /internal/dmaas/reconcile/webhook-replays` — finds `webhook_events` in non-terminal status >1h old. Calls the existing replay machinery (LOB_WEBHOOK_REPLAY_* config already exists; wire it through Trigger.dev instead of whatever schedule it has today).
  * `POST /internal/dmaas/reconcile/customer-webhook-deliveries` — finds pending deliveries whose `next_retry_at` is past. Re-enqueues `customer_webhook.deliver` for each.

* Feature flags in `app/config.py`:
  * `DMAAS_RECONCILE_STALE_JOBS_ENABLED: bool = True`
  * `DMAAS_RECONCILE_LOB_ENABLED: bool = True`
  * `DMAAS_RECONCILE_DUB_ENABLED: bool = True`
  * `DMAAS_RECONCILE_WEBHOOK_REPLAYS_ENABLED: bool = True`
  * `DMAAS_RECONCILE_CUSTOMER_WEBHOOKS_ENABLED: bool = True`

  Each internal endpoint short-circuits with a no-op if its flag is `false`. Allows instant kill via Doppler without a deploy.

* Reconciliation services in `app/services/reconciliation/`:
  * One module per kind (`stale_jobs.py`, `lob_pieces.py`, `dub_clicks.py`, `webhook_replays.py`, `customer_webhook_deliveries.py`).
  * Each exposes `async def reconcile(*, organization_id: UUID | None = None) -> ReconciliationResult` returning counts (rows scanned, rows touched, drift found).
  * `organization_id=None` runs platform-wide; specifying scopes to one org (useful for ad-hoc operator runs).

**Required behavior:**

1. All reconciliation tasks idempotent — running twice in 5 minutes is a no-op the second time.
2. Each task emits `reconciliation.run_completed` with the counts so the dashboard can show "X drift items detected this week."
3. Drift events include enough detail for an operator to manually investigate (provider IDs, our row IDs, the specific gap).
4. Customer-facing webhook deliveries are NOT emitted from reconciliation results by default (these are operator concerns).
5. Tasks scoped to provider — Lob reconciliation never touches Dub data and vice versa. Failures in one don't block others.

**Tests:**

* `tests/test_reconcile_stale_jobs.py` — pure tests with DB fakes: scenarios with running >2h, recent running, already failed, etc.
* `tests/test_reconcile_lob.py` — Lob HTTP client mocked; verify drift detection logic for missing pieces.
* `tests/test_reconcile_dub.py` — Dub HTTP client mocked.
* `tests/test_reconcile_webhook_replays.py` — verify replay invocation pattern.
* `tests/test_reconcile_customer_webhook_deliveries.py` — verify pending deliveries past next_retry_at get re-enqueued.

**Verification:**

* Manually create a stale `running` job (dev DB), run the cron, verify it transitions to `failed`.
* Manually delete a `direct_mail_pieces` row (dev), run lob reconciliation, verify the row is restored.
* Document each scenario in the PR.

---

### Slice 4 — Multi-step scheduler

**Goal:** when step N completes successfully, automatically schedule step N+1's activation for `delay_days_from_previous` days from now. Customers get drip sequences without writing any code.

This is the most complex slice — durable sleep, cancellation, idempotency. Build last so the surrounding patterns are well-established.

**File touchpoints:**

* New service `app/services/step_scheduler.py`:
  * `schedule_next_step(*, completed_step_id) -> ScheduledNextStep | None`
    * Finds the next step in same channel_campaign by `step_order`.
    * Returns None if no next step.
    * Computes activation time = NOW + `next_step.delay_days_from_previous` days.
    * Creates an `activation_jobs` row with kind=`step_scheduled_activation`, status=`queued`, payload=`{step_id, scheduled_for}`.
    * Calls Trigger.dev to enqueue `dmaas.scheduled-step-activation` task with the delay.
    * Persists `trigger_run_id` on the job.
    * Emits `step.scheduled_for_activation` with target time.
    * Idempotent: re-running for the same completed_step_id returns the existing scheduled job without re-creating.
  * `cancel_scheduled_step(*, step_id, reason) -> CancelResult`
    * Finds the scheduled-activation job for this step in status=`queued`.
    * Calls Trigger.dev's run cancellation API.
    * Marks job `cancelled`. Emits `step.scheduled_activation_cancelled`.

* New TS task `src/trigger/dmaas-scheduled-step-activation.ts`:
  * Uses `wait.for(delay)` for durable sleep.
  * After sleep, calls `/internal/dmaas/process-job` (same endpoint as Slice 1 — this is just another job kind).
  * Cancellable via Trigger.dev's run cancellation API.

* Integration with step lifecycle in [`app/webhooks/lob_processor.py`](app/webhooks/lob_processor.py):
  * Hook into the existing membership-transition logic. When ALL members of a step transition to a terminal status (sent + failed + suppressed + cancelled count == total), the step is "completed."
  * Mark the step's status=`sent` (or `failed` if all members failed; document the rule).
  * Call `schedule_next_step(completed_step_id=step.id)`.
  * Emit `step.completed` event.

* Cancellation hooks:
  * When a `channel_campaign` is paused or archived, find all `activation_jobs` for its steps in status=`queued` and cancel them.
  * When a `campaign` is paused or archived, cascade to all child channel_campaigns.
  * Add to the existing pause/archive flows in `app/services/campaigns.py` and `app/services/channel_campaigns.py`.

* Internal endpoint extension (`app/routers/internal/dmaas_jobs.py`):
  * The `/internal/dmaas/process-job` endpoint already dispatches by kind. Add the `step_scheduled_activation` branch — calls `LobAdapter.activate_step` (or the generic `activate_step` path) for the persisted step_id.

**Required behavior:**

1. Step completion is unambiguous: ALL recipients in terminal status. Partial completion does not trigger the next step.
2. Failed-step handling: if step N fails (most members failed), DO NOT auto-schedule step N+1. Operator must manually intervene. Emits `step.failed` event for customer notification.
3. Pause cascades: pausing a channel_campaign cancels all queued scheduled-activation jobs for its steps. Resume re-schedules from "now + remaining delay" (or simpler: requires manual re-activation; document the V1 behavior).
4. Archive cancels permanently. No re-schedule.
5. Multi-step idempotency: if `schedule_next_step` is called twice for the same completed_step_id (race in the membership-transition handler), the second call is a no-op.
6. Time accuracy: scheduled tasks fire within a 5-minute window of the target time. Trigger.dev's `wait.for` is accurate to within seconds in practice.

**Tests:**

* `tests/test_step_scheduler_service.py` — pure tests for `schedule_next_step` including the "no next step" case, idempotency, cancellation.
* `tests/test_step_scheduler_completion_hook.py` — verify the lob_processor hook fires correctly when all members reach terminal status.
* `tests/test_step_scheduler_cancellation.py` — verify pause/archive cascades cancel queued jobs.
* `tests/test_step_scheduler_failed_step.py` — verify failed step does NOT auto-schedule next.
* Integration smoke (manual): create a 2-step campaign in dev with delay_days=0 on step 2, complete step 1, observe step 2 auto-activates within 1 minute.

**Verification:**

* Build a 2-step campaign, set delay_days_from_previous=0 on step 2 for fast verification.
* Activate step 1 (which uses Slice 1's async path).
* When step 1 reaches `sent`, observe `step.scheduled_for_activation` event + new `activation_jobs` row for step 2.
* Within ~1m, observe step 2's `activation_jobs` row transition to `running` then `succeeded`.
* Document the full lifecycle in the PR.

---

## 4. Definition of done

* All 4 slices merged to `main`.
* `uv run pytest -q` green at every step.
* `uv run ruff check` clean on every file you touch.
* Each slice has a documented manual verification in its PR description.
* `package.json` + `trigger.config.ts` reflect any new TS task files (no SDK version bumps).
* New post-ship summary at `docs/dmaas-orchestration-pr-notes.md` describing what shipped, breaking changes (sync → async on `POST /api/v1/dmaas/campaigns`), customer-facing API changes (jobs endpoint, webhooks endpoint), feature flags shipped, and any caveats.
* Update [`CLAUDE.md`](CLAUDE.md) with the new verification scripts (`uv run python -m scripts.smoke_async_activation` if you build one, etc.).

---

## 5. Working order (recommended)

1. **Read** the existing Trigger.dev task examples ([`src/trigger/health-check.ts`](src/trigger/health-check.ts), [`voice-callback-runner.ts`](src/trigger/voice-callback-runner.ts), [`lib/hqx-client.ts`](src/trigger/lib/hqx-client.ts)) and the existing internal routers ([`app/routers/internal/scheduler.py`](app/routers/internal/scheduler.py), [`voice_callbacks.py`](app/routers/internal/voice_callbacks.py), [`emailbison.py`](app/routers/internal/emailbison.py)) end to end.
2. **Read** the canonical hierarchy doc and the prior directives' post-ship notes ([`docs/dmaas-foundation-pr-notes.md`](docs/dmaas-foundation-pr-notes.md), [`docs/dmaas-hosted-pages-pr-notes.md`](docs/dmaas-hosted-pages-pr-notes.md)).
3. **Investigate** Trigger.dev's HTTP API for triggering tasks from outside the SDK. The Python side will POST to Trigger.dev's `/api/v3/tasks/{taskIdentifier}/trigger` (or whatever the v4 SDK exposes — check their docs at `/Users/benjamincrane/api-reference-docs-new/` if mirrored, otherwise [trigger.dev/docs](https://trigger.dev/docs)). Document the exact endpoint + auth in Slice 1's PR.
4. **Build Slice 1** (async activation foundation). The keystone. Manual smoke before opening PR.
5. **Build Slice 2** (customer webhooks). Big LOC volume but mostly mechanical. The HMAC + retry pattern is well-trodden territory.
6. **Build Slice 3** (reconciliation crons). Pure additive. Lowest risk. Builds operator confidence.
7. **Build Slice 4** (multi-step scheduler). Most complex — durable sleep, cancellation, idempotency. Build last when patterns are established.
8. **Write the post-ship summary** + update `CLAUDE.md` with new manual verification scripts.

If you hit a real architectural snag — especially around Trigger.dev's run cancellation semantics or membership-completion detection — STOP and surface it in the PR description rather than improvising.

---

## 6. Style + conventions

* Follow ruff config in `pyproject.toml` — line length 100, target py312, lint set `["E", "F", "I", "W", "UP", "B"]`.
* TS files follow the existing `src/trigger/` style (single export per file, explicit types, `callHqx` for all hq-x calls).
* File header docstrings explain why the module exists, what's in scope, what's deferred.
* No new emojis in code, comments, or commit messages.
* Commit messages: short imperative subject under 72 chars. Blank line. 1–3 paragraphs. End with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
* PR descriptions: Summary, Six-tuple integrity, Cross-org leakage, Verification, Test plan.
* Migration filenames use timestamp prefix (`YYYYMMDDTHHMMSS_<slug>.sql`).

---

## 7. Out of scope (deferred to future directives)

* **Async refactor of step activation outside DMaaS opinionated path.** The legacy `POST /api/v1/channel-campaign-steps/{step_id}/activate` endpoint stays sync for V1 — the customer-facing surface is `/api/v1/dmaas/campaigns` which is async-only. Internal/operator activation can stay sync.
* **Cost tracking per org per Trigger.dev invocation.** Useful for future billing but not needed for V1.
* **Per-task observability dashboards** beyond Trigger.dev's built-in. If customers ask for "show me my recent jobs and their outcomes," that's a V2 dashboard endpoint.
* **Webhook delivery detail beyond status + last response** — full delivery log with request headers, etc., is operator-only debug surface, not a customer endpoint.
* **Replay for arbitrary historical events** to a webhook subscription. V1 supports replaying dead-lettered deliveries; V2 could allow "replay all events from this campaign to my subscription."
* **Trigger.dev v5 migration.** Stay on v4.4.4. Upgrade is its own effort.
* **Self-hosted Trigger.dev.** Use the cloud offering. Self-host is a future cost-optimization concern.

---

## 8. Reference paths cheat sheet

| What | Where |
|---|---|
| Canonical hierarchy doc | [docs/campaign-rename-pr-notes.md](docs/campaign-rename-pr-notes.md) |
| Directive 1 post-ship notes | [docs/dmaas-foundation-pr-notes.md](docs/dmaas-foundation-pr-notes.md) |
| Directive 2 post-ship notes | [docs/dmaas-hosted-pages-pr-notes.md](docs/dmaas-hosted-pages-pr-notes.md) |
| Trigger.dev config | [trigger.config.ts](trigger.config.ts) |
| Existing TS tasks (reference patterns) | [src/trigger/health-check.ts](src/trigger/health-check.ts), [src/trigger/voice-callback-runner.ts](src/trigger/voice-callback-runner.ts), [src/trigger/voice-callback-reminders.ts](src/trigger/voice-callback-reminders.ts) |
| TS → Python HTTP shim | [src/trigger/lib/hqx-client.ts](src/trigger/lib/hqx-client.ts) |
| Trigger.dev shared secret auth | [app/auth/trigger_secret.py](app/auth/trigger_secret.py) |
| Existing internal routers | [app/routers/internal/](app/routers/internal) — `scheduler.py`, `voice_callbacks.py`, `emailbison.py` |
| Six-tuple emit chokepoint | [app/services/analytics.py](app/services/analytics.py) |
| Lob webhook projector (membership transitions) | [app/webhooks/lob_processor.py](app/webhooks/lob_processor.py) |
| Dub HTTP client | [app/providers/dub/client.py](app/providers/dub/client.py) |
| Lob HTTP client | [app/providers/lob/client.py](app/providers/lob/client.py) |
| DMaaS opinionated single-call API (Directive 2) | [app/routers/dmaas_campaigns.py](app/routers/dmaas_campaigns.py) |
| Channel campaign step service | [app/services/channel_campaign_steps.py](app/services/channel_campaign_steps.py) |
| Recipient + memberships | [app/services/recipients.py](app/services/recipients.py) |

---

**End of directive.** Four slices, four PRs. After this lands, the DMaaS platform is durable, observable, time-aware, and customer-integrable. The orchestration foundation supports any future workstream that needs async or scheduled execution — campaign reporting cadence, automated A/B testing, customer-defined automations, etc.
