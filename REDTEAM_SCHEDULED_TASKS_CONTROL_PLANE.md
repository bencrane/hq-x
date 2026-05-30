> Source: independent adversarial Opus 4.8 red-team subagent (`arch-review`), 2026-05-30. Reproduced verbatim.

---

# RED-TEAM FINDINGS: Scheduled-Task Control Plane + Dispatch Layer

Verified against code. The migration itself is cleaner than the brief implied — no double-fires, the GTM money-path is well-defended. The real exposure is in the **new control plane the prior agent shipped**: it grades the wrong thing, has zero tests, and silently hides the one event it most needs to surface.

## TOP RISKS (ranked by blast-radius × likelihood)

### 1. The status engine grades the HANDOFF, not the DATA — green is actively misleading for 62 of 89 tasks
**Failure mode.** For every `modal_dispatch` task (62 of 89), a "green" requires only that the Trigger→Modal POST returned a `call_id`. The dispatch endpoint `.spawn()`s and returns immediately (`trigger_dispatch_app.py:115-116`) — fire-and-forget, no completion tracking. The Modal job can OOM, the DuckDB cast can silently coerce garbage, the Lance emit can crash, and the dashboard stays **green**. The schema comment admits this (`20260529T210000_ops_scheduled_tasks.sql:27-28`, "green = handoff ok only"), but the roll-up summary (`scheduled_tasks.py:301-313`) presents one undifferentiated green count to the operator. An SLA breach (stale Lance feeding the direct-mail chain) will show all-green right up until a client complains.
**Evidence.** `scheduled_tasks.py:276-279` (`status=green` on Trigger run `completed`); `trigger_dispatch_app.py:114-116` (spawn + return, no result wait).
**Fix.** The truth already exists in `ops.data_source_ingest_runs` (written by `run_emit` in `pattern_a_lance_emit.py:285` and by `all_sources_verify`). Join Layer-2 status into `list_with_status`: for `modal_dispatch` rows, overlay the latest ingest-run row for that `display_name`/`source_id` and downgrade green→amber when the dispatch succeeded but no succeeded ingest-run landed in the window. Surface two columns: "dispatched" and "data advanced."
**Effort.** 1–2 days (the data plane exists; this is a join + a second status band in the existing function).

### 2. The fail-open gate + the `disabled`-returns-early bug = a disabled task can run forever, invisibly
**Failure mode.** Two interacting defects:
- (a) The gate is fail-open on *any* error including **timeout** (`scheduled-gate.ts:47-53`). If hq-x is merely *slow* (not down) — e.g. Supabase connection-pool exhaustion — every one of the 89 crons waits up to 10s (`scheduled-gate.ts:38`) and then runs anyway. The operator's kill-switch evaporates exactly when the platform is under stress, which is exactly when they'd reach for it.
- (b) `_compute_status` returns `disabled` **before** ever looking at run history (`scheduled_tasks.py:265-266`). So a task the operator disabled, which then *ran anyway* via fail-open during an hq-x blip, shows a calm `disabled` badge while it is in fact spawning Modal compute, mutating Lance, and burning money. The "disabled task that still ran" event — the single most dangerous state in a kill-switch system — is the one state rendered invisible.
**Evidence.** `scheduled-gate.ts:47-53`; `scheduled_tasks.py:265-266` (disabled short-circuits above the run-window check at 271-291).
**Fix.** (b) is the cheap, high-value one: in `_compute_status`, when `is_enabled=false`, still resolve the matured-fire window; if a run exists in it, emit a distinct loud status (`disabled_but_ran` / red) instead of `disabled`. (a): make "disabled" the one decision that is *cached and fails-closed* — let the gate read a short-TTL local cache of the disabled-set so a kill-switch survives an hq-x outage, while unknown/healthy tasks still fail-open. Reframe the gate: fail-open is right for "is this task known," fail-closed is right for "did the operator explicitly pull this."
**Effort.** (b) 2–3 hrs. (a) 1 day.

### 3. Two genuinely-chained jobs were migrated to fire-and-forget clock offsets — the comment says they weren't
**Failure mode.** `batch-a-schedules.ts:8` states "the 2 epiq bridges + bdc_soi_parse_v2 are intentionally NOT migrated (chained — stay on modal.Cron)." **They were migrated.** `epiq-bridge-ppp-borrower.daily`, `epiq-bridge-uspto-owner.daily`, and `bdc-soi-parse-v2.monthly` are live Trigger schedules in `batch-others-schedules.ts:62-66`, and their `(app,function)` pairs are in the dispatch allowlist (`trigger_dispatch_app.py:83,98-99`). These bridges *depend on* upstream completion (the 5 epiq ingest legs; `sec-bdc-soi`), but now fire on naive wall-clock offsets (bridge at `30 3`, last ingest leg `creditors` at `15 3` — **15 min** of slack for a full bankruptcy-claims ingest) with **zero completion dependency**. `bdc-soi-parse-v2` fires `0 14 9 * *`, a full day after `sec-bdc-soi` at `0 14 8 * *` — survivable, but it's now a temporal coupling masquerading as a schedule. When the upstream ingest runs >15 min late or fails, the bridge reads stale/missing R2 and silently produces a partial or empty bridge dataset. No guardrail reads ingest state before the bridge runs.
**Evidence.** `batch-a-schedules.ts:8` (the false claim); `batch-others-schedules.ts:62-66`; `trigger_dispatch_app.py:83,98-99`; cron offsets in `seed_scheduled_tasks.py:99-101` (creditors `15 3`, bridge `30 3`).
**Fix.** Either (a) revert these three to modal.Cron as the comment claims and remove them from the allowlist, or (b) commit to the migration and make the bridge dispatch *conditional*: have the bridge's Modal function check `ops.data_source_ingest_runs` for a succeeded same-day run of its upstream before proceeding, returning a `skipped_upstream_not_ready` that the status engine renders amber. The current state — migrated but documented as not-migrated, with sub-15-min implicit slack — is the worst of both.
**Effort.** (a) 1–2 hrs. (b) 1 day. At minimum, fix the lying comment now (15 min).

### 4. Unauthenticated dispatch endpoint = an unbounded Modal cost-bomb for anyone who finds the URL
**Failure mode.** `trigger_dispatch_app.py` is a public `@modal.fastapi_endpoint(method="POST")` with no auth (`:109-116`). The allowlist (`:33-100`) bounds *which* functions spawn, but not *how many times*. The URL is hardcoded in plaintext in three committed `.ts` files (`batch-a-schedules.ts:15`, etc.). Anyone with the URL can POST `{app, function}` in a loop and spawn unbounded Modal containers — many of these functions are 8–16 GB, 30-min, multi-million-row DuckDB/Lance jobs. There's no `max_containers`, no rate limit, no concurrency cap on the dispatcher (`timeout=60`, default autoscale). This is a **direct path to a five-figure Modal bill** and DB write-amplification (every spawned emit writes `ops.data_source_ingest_runs` rows). Data-corruption risk is lower — emits target fixed datasets — but a flood of concurrent emits against the same Lance dataset stresses the commit-lock.
**Evidence.** `trigger_dispatch_app.py:12-14` ("Unauthenticated for now"), `:109-116`; URL in `batch-a-schedules.ts:15`.
**Fix.** The code comment already prescribes it (`:13-14`): add `requires_proxy_auth=True` + Modal-Key/Modal-Secret, OR — simpler and consistent with the rest of the stack — require the same `TRIGGER_SHARED_SECRET` Bearer the gate uses (`trigger_secret.py` is the model) and pass it from the `.ts` `spawnModal` fetch. This is a one-function change. The "pilot posture" justification expired the moment 89 prod crons depend on it.
**Effort.** 2–4 hrs including redeploy + adding the header to the 4 dispatch call-sites.

### 5. The matured-fire stepback produces concrete false-REDs on multi-times-per-day feeds
**Failure mode.** `_matured_fire` (`scheduled_tasks.py:218-224`) steps back one fire when the last fire is within grace. For `sec-edgar-form-13f.scan` (`0 2,8,14,20 * * *`, grace 180 min): at `now=09:00`, last fire `08:00`, `now-prev=60min<180` → step back to `due_fire=02:00`, window `[01:55, 05:00]`. The engine now demands a run in the 02:00 window and **ignores the 08:00 fire that just succeeded**. If the 02:00 run has aged past the `limit=10` lookback (`scheduled_tasks.py:169`) or that one fire was genuinely missed while 08:00 succeeded, the task false-REDs despite being healthy *right now*. The stepback was designed for every-minute tasks but misfires on irregular multi-hour crons where grace > inter-fire gap. Combined with `list_runs(limit=10)`, any task firing >10×/day risks the matured fire falling off the lookback window entirely.
**Evidence.** `scheduled_tasks.py:218-224` (stepback), `:271` (window), `:169` (`limit=10`).
**Fix.** Only step back when the cron's *inter-fire interval* is smaller than grace+skew (i.e., a stall would otherwise be masked). For sparse crons, grade the actual last matured fire, not one cycle back. And size the `list_runs` limit by cadence (every-minute needs more history than weekly), or fetch by time-window filter rather than fixed count.
**Effort.** 4–6 hrs, and it needs the tests from below to validate the cron edge cases.

## WHAT'S RIGHT (signal, not noise)
- **The modal.Cron migration is clean — no double-fires.** Every migrated app has its `schedule=modal.Cron(...)` correctly commented out (`sam_opps_active_lance_emit_app.py:59`); the only 14 active modal.Cron decorators left are exactly the FMCSA set (12) + the two watchdogs (`alerter_cron`, `all_sources_verify`) that were *deliberately* kept on Modal. The hypothesized dual-schedule overlap does not exist.
- **The watchdog plane is correctly independent.** `all_sources_verify_app.py:347` runs every 15 min on Modal, reads `ops.data_source_ingest_runs` directly (data-level truth, not handoff), scans R2 for 0-byte poison files, and emits Telegram alerts — all without touching Trigger.dev or the gate. This is the Layer-2 truth the dashboard is missing (see Risk #1), and it already exists.
- **The GTM hydration fan-out is genuinely hardened.** `gtm-hydration-cascade.ts:204-217` enforces concurrency with a *hard* Modal `max_containers=1` cap plus a serial loop, and carries an explicit post-mortem of the 2026-05-26 Blitz-429 incident. It correctly requires `ack.status === "completed"` (not just `acknowledged`) so failed Modal calls don't inflate success counts (`:144`). This is mature, scar-tissue engineering.
- **Lob double-send is defended at the provider layer.** `providers/lob/idempotency.py:85-95` derives a deterministic SHA-256 idempotency key over the normalized recipient + content, excluding mutable fields exactly per Lob's guidance. The "real money / double-send" risk in the brief is already mitigated for the path that exists.
- **The seed script's split ownership is correct.** `seed_scheduled_tasks.py:284-299` refreshes only code-owned columns on conflict and preserves operator toggles (`is_enabled`, `priority`, `notes`). Re-syncing a cadence change won't clobber a disable.

## RECOMMENDATIONS

**P0**
- **Authenticate the dispatch endpoint** — add `TRIGGER_SHARED_SECRET` Bearer check to `trigger_dispatch_app.py:111` and the header to the 4 `spawnModal` call-sites. (Risk #4)
- **Fix the disabled-but-ran blind spot** — in `scheduled_tasks.py:265`, resolve the run window before returning `disabled` and emit a loud status if a run landed. (Risk #2b)
- **Fix or revert the three chained jobs** and correct the false comment in `batch-a-schedules.ts:8`. (Risk #3)
- **Write tests for the control plane** — there are currently **zero** (`grep` for `passesGate`/`_matured_fire`/`scheduled_tasks` in tests returns nothing; the existing `test_scheduler_*.py` are for the unrelated DMaaS step engine). The croniter math in `scheduled_tasks.py:218-291` is pure and trivially unit-testable: feed synthetic `now`/cron/grace/run-history and assert the status. This is the highest-ROI test surface in the repo. (Risk #5 cannot be safely fixed without it.)

**P1**
- **Overlay Layer-2 data status** from `ops.data_source_ingest_runs` into `list_with_status` so green means "data advanced," not "POST returned." (Risk #1)
- **Make the kill-switch fail-closed** via a cached disabled-set in `scheduled-gate.ts`, while keeping unknown/healthy tasks fail-open. (Risk #2a)
- **Pin croniter** — `pyproject.toml:20` has `croniter>=2.0` unpinned; a minor croniter behavior change silently corrupts every status grade. Pin to a tested range.
- **Collapse the 3-copies-of-cron drift.** The cron lives in (1) the `.ts` task def, (2) `CONFIG.cron_schedule` in the now-dead Modal decorator, and (3) `ops.scheduled_tasks.cron` via the seed manifest. (2) is dead data that will mislead the next editor (`sam_opps_active_lance_emit_app.py:45` still reads `30 12 * * *`). Add a CI check that asserts the seed-manifest cron equals the `.ts` `cron.pattern`, and delete or comment-annotate the dead CONFIG crons.

**P2**
- Size `list_runs` history by cadence rather than fixed `limit=10` (`scheduled_tasks.py:169`).
- The admin PATCH has a latent no-op: passing only `reason` (no `is_enabled`) passes the `all-None` guard at `admin/scheduled_tasks.py:54-57` but `update_task` ignores a bare `reason` — silently 200s with no change. Minor; tighten the guard.
- `all_sources_verify` writes ~3,750 rows/day with no retention prune (`all_sources_verify_app.py:16-18`) — wire the prune before it's a year of dead rows.

## THE ONE THING
**Make "green" mean the data advanced, not that the POST returned (Risk #1).** Right now the operator's entire confidence rests on a dashboard that, for 62 of 89 tasks, proves only that an HTTP call succeeded — while the actual Lance materialization that feeds the SLA-mandated direct-mail chain can be silently stale or empty. The Layer-2 truth already exists in `ops.data_source_ingest_runs` and is already maintained by the independent `all_sources_verify` watchdog; it just isn't joined into the status the operator looks at. Closing that one gap converts the control plane from a false-confidence generator into an actual early-warning system — and it's a 1–2 day join, not a rebuild.
