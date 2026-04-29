# Directive — Adversarial review of the hq-x voice infrastructure port

**Created:** 2026-04-28
**Status:** Ready for executor
**Scope:** Independent, hostile audit of the Twilio + Vapi + Trust Hub + IVR + SMS port from `outbound-engine-x` into `hq-x`.

You are an adversarial reviewer. Your job is **not** to confirm the port worked. Your job is to find what's wrong, missing, half-shipped, drifted from the spec, broken at runtime but green in CI, or quietly outside scope. Assume the implementing agent is not lying but is also not infallible — every claim needs verification.

---

## 1. Sources of truth

These are authoritative. Read all four before forming any opinion.

- **Target spec:** `/Users/benjamincrane/outbound-engine-x/DIRECTIVE_HQX_VOICE_PORT.md` (394 lines). The source of truth for *what was supposed to ship*. Pay special attention to §3 (port + rebuild items, skip-entirely list), §5 (schema sequencing), §6 (brand_id substitution contract), §7 (eight drift fixes — §7.1 through §7.8), §9 (five-test E2E plan), §10 (deferred list), §11 (OEX deletion conditions), §12 (engineering opinions to honor).
- **Audit of OEX state:** `/Users/benjamincrane/outbound-engine-x/AUDIT_TWILIO_VAPI.md` (625 lines). Authoritative for *what existed in OEX at the time of the port*. Use it to verify every "skip" decision wasn't actually a critical surface.
- **Implementer's handoff doc:** `/Users/benjamincrane/hq-x/.claude/worktrees/youthful-mclaren-cd8b69/HANDOFF_VOICE_PORT.md` — the handoff written between PR #2 and PR #3/#4. Useful for *what the implementer thought was done*; treat as a witness statement, not as truth.
- **Three landed PRs on `main`:**
  - **#2** `d5b7453` — Foundation (schema + providers + brand CRUD + 8 routers)
  - **#3** `e6d9420` (merged via `8c8d4fb`) — Voice campaign UoW + Trigger.dev + §7.5
  - **#4** `268a388` — IVR engine + provisioning pipeline + outbound calls + voice CRUD

Other relevant context:
- **Repo:** `/Users/benjamincrane/hq-x` (work on `main`).
- **Doppler:** project `hq-x`, configs `dev` / `stg` / `prd`. `BRAND_CREDS_ENCRYPTION_KEY` is set in dev.
- **DB:** Supabase Postgres for hq-x. Direct URL = `HQX_DB_URL_DIRECT`. Pooled URL = `HQX_DB_URL_POOLED`.
- **Migration runner:** `doppler run --project hq-x --config dev -- uv run python -m scripts.migrate`.

---

## 2. Mission framing

The original directive said the port is **binary** — "it works end-to-end or it doesn't ship. No phased landings of half a Trust Hub flow or half a Vapi receiver." Yet three PRs landed sequentially. Your job is to evaluate whether the *combined* state on `main` actually satisfies the directive's binary bar, OR whether the port shipped half-baked under the cover of a multi-PR sequence.

Specifically, evaluate against these questions:

1. **Did everything in §3 "Port + rebuild" actually port?** Or did the implementer redefine "port" downward (e.g. "we ported the schema but not the router")?
2. **Did the §7.1–§7.8 drift fixes actually land as code that runs, or are some only present as schema columns / partial wiring?**
3. **Are the §10 deferred items honestly deferred, or did some sneak in half-implemented?**
4. **Will §9.2–§9.5 E2E pass against real Twilio + Vapi if creds are wired tomorrow?** If not, what specifically breaks?
5. **Is brand_id substitution complete and consistent**, or are there `org_id` / `company_id` ghosts that will surface as bugs in production?
6. **Are the composite `*_same_brand_fk` constraints actually enforced and respected by INSERT paths?**
7. **Does production safety hold?** Specifically: prd boot-time signature-mode checks, encryption key handling, no plaintext creds in logs.
8. **Are the tests honest?** A passing smoke test that exercises `400 → missing creds` paths is not the same as a real E2E.

---

## 3. Required verification — do every one of these

### 3.1 Schema integrity

Run the migration set fresh:
```bash
doppler run --project hq-x --config dev -- uv run python -m scripts.migrate
```

Then for each table the directive's §5 lists, verify:
- The table exists in the right schema (`business.brands`, `business.partners`, `business.campaigns`, `business.users`; everything else in `public.*`).
- Every column the directive specifies is present with the right type, nullability, and default.
- Composite-FK pattern: child tables that reference partner or campaign have a `*_same_brand_fk` constraint that references `(id, brand_id)` not just `(id)`.
- Soft-delete columns (`deleted_at`) on the tables the directive marks soft-deleted.
- Indices that are essential for the hot paths (e.g., `voice_phone_numbers.vapi_phone_number_id` unique partial; `webhook_events (provider_slug, event_key)` unique).

For the four NEW columns on `voice_callback_requests` (per directive §5 step 8 / §7.6 / §7.7):
- `leave_voicemail_on_no_answer BOOLEAN`
- `voicemail_script TEXT`
- `reminder_sent_at TIMESTAMPTZ`
- `reminder_sms_sid TEXT`

Verify all four exist. Verify §7.5 added a column for inbound-SMS↔callback linkage (Agent B chose `last_inbound_sms_at` + `last_inbound_sms_sid` — confirm both exist).

For `sms_suppressions` (the NEW table per §7.3):
- Confirm `(brand_id, phone_number)` UNIQUE.
- Confirm `reason` CHECK constraint covers `stop_keyword`, `manual`, `bounce`.

For `call_logs`: confirm the OEX `voice_sessions` fold-in actually happened — `amd_result`, `business_disposition`, `dial_action_status` columns exist and are wired by the Twilio webhook handler. Read `app/routers/twilio_webhooks.py` and trace whether `_handle_amd_result` actually persists into `call_logs` (not into a vestigial voice_sessions table).

### 3.2 brand_id substitution audit

Run these greps from the repo root and report every finding:
```bash
grep -rn "org_id\|company_id" app/ migrations/ src/trigger/ tests/ 2>&1 | grep -v ".pyc" | grep -v "^Binary"
grep -rn "from src\." app/ tests/ 2>&1
grep -rn "company_campaign" app/ migrations/ 2>&1
grep -rn "voicemail_drop\|voice_sessions\|voicedrop" app/ migrations/ 2>&1
```

For each result:
- `org_id` / `company_id` references: every one is either (a) inside a comment explaining a design decision, (b) in a string label that's deliberately about external systems, or (c) a bug. Classify each.
- `from src.` imports: every one is a bug — the port standardized on `from app.`.
- `company_campaign*` references: every one is a bug unless it's referring to OEX docs in a comment.
- `voicemail_drop*` / `voice_sessions` / `voicedrop` references: every one is a bug per directive §3 skip list.

Do not be charitable about "harmless string literals." A `WHERE org_id = ...` that uses a column that doesn't exist will fail at runtime, not import time.

### 3.3 Composite FK enforcement test

The directive insists `*_same_brand_fk` constraints stay enforced even though "single-operator world makes them redundant — they catch app-layer bugs."

Write SQL that attempts to violate the constraint and confirm Postgres rejects it:
```sql
-- Should fail: campaign references partner from different brand.
INSERT INTO business.brands (id, name) VALUES ('11111111-...', 'a');
INSERT INTO business.brands (id, name) VALUES ('22222222-...', 'b');
INSERT INTO business.partners (id, brand_id, name) VALUES ('aaaa-...', '11111111-...', 'pa');
-- This INSERT must fail:
INSERT INTO business.campaigns (id, brand_id, partner_id, name)
  VALUES ('cccc-...', '22222222-...', 'aaaa-...', 'mismatched');
```

Run the equivalent for every child table that should have a `*_same_brand_fk` constraint. If any of these *succeed*, the constraint is missing and it's a P1 finding.

### 3.4 Drift fix individual verification

For each of §7.1 through §7.8, write a single concrete test (or curl + DB query pair) that proves the fix actually runs. Don't trust "it's in the code"; trust "it executes and changes state correctly."

- **§7.1** Trust Hub callback signature validation. POST a payload with a wrong `X-Twilio-Signature` to `/api/webhooks/twilio-trust-hub/{brand_id}` and confirm 403 in `enforce` mode. Repeat with correct signature and confirm 200 + `trust_hub_registrations.status` updates. (The brand needs creds set for this to be testable.)
- **§7.2** SMS status-callback persistence. Insert a row in `sms_messages` with status='queued', then POST a Twilio status callback with that `MessageSid` and `MessageStatus=delivered`, confirm `sms_messages.status='delivered'`.
- **§7.3** SMS STOP/HELP suppression. Two checks:
  - Pre-send check: insert `sms_suppressions(brand_id, phone)`, then call `app.services.sms.send_sms` with that phone and confirm it raises `SmsSuppressedError` *without* hitting Twilio.
  - Inbound STOP: POST a Twilio inbound message with `Body=STOP` and confirm the row was inserted in `sms_suppressions`.
- **§7.4** lookup_carrier mis-wiring. Read `app/services/voice_ai_tools.py:lookup_carrier` and confirm it reads `settings.DEX_BASE_URL`, NOT `hypertide_base_url`. Confirm it returns `"error: carrier lookup service not configured"` when `DEX_BASE_URL` is unset.
- **§7.5** Inbound SMS reply matching. Insert a `voice_callback_requests` row in the last 48h for some phone X. POST a Twilio inbound message from X. Confirm the callback row's `last_inbound_sms_at` / `last_inbound_sms_sid` got stamped.
- **§7.6** SMS callback-reminder Trigger.dev task. Two checks:
  - The internal endpoint `POST /internal/voice/callback/send-reminders` exists, requires the trigger secret, and returns the right shape.
  - `src/trigger/voice-callback-reminders.ts` exists and compiles (`npx tsc --noEmit -p tsconfig.json`).
  - Insert a `voice_callback_requests` row 10 min in the future with a brand that has Twilio creds set; hit the endpoint; confirm `reminder_sent_at` got stamped.
- **§7.7** Voicemail-on-callback runner Trigger.dev task. Equivalent: internal endpoint `POST /internal/voice/callback/run-due-callbacks` exists, requires trigger secret, atomically claims rows (`FOR UPDATE SKIP LOCKED`), calls Vapi with `assistantOverrides` when `leave_voicemail_on_no_answer=true`. The TS task compiles.
- **§7.8** `VAPI_WEBHOOK_SIGNATURE_MODE` defaults. Confirm: code default in `app/config.py` = `"strict"`, NOT `"permissive_audit"`. Confirm production safety check in config raises if `APP_ENV='prd'` and signature mode is anything other than `strict`.

### 3.5 Skip-list audit

For each item the directive §3 marks "Skip entirely":
- voicedrop (any reference at all → P0)
- voicemail_drops table or column (`outbound_call_configs.voicemail_drop_id` was deliberately omitted from `0005_voice_base.sql`; verify)
- Twilio Conversational Intelligence (`providers/twilio/intelligence.py`, `services/transcription.py`, `migrations/037_call_transcripts.sql`, `migrations/038_*`)
- Live transcription (`services/live_transcription.py`, `migrations/043_live_transcription_utterances.sql`)
- voice_sessions table (`migrations/029_voice_sessions.sql`)
- old phone_numbers table (`migrations/049_phone_numbers.sql` — should NOT have an hq-x equivalent; only `voice_phone_numbers` exists)
- webhook_events sink consumers for inbound SMS / Trust Hub callbacks — the writes are PRESERVED but no consumer exists. Verify `app/services/` has no module that polls `webhook_events` to react to these events.
- Pipeline-tick / orchestrator-tick HTTP endpoints (`pipeline_endpoints.py`, `orchestrator.py`, `voice_campaigns_internal.py` — none of these should exist as routers in hq-x)
- `routers/conversations.py` (Twilio Conversations JS-client token mint)
- Twilio Voice Access Token endpoint (`GET /api/voice/token`) — should NOT exist on the merged main

For each, do a `find` and a `grep` and report any positive finding as a P0 ("explicitly forbidden item shipped").

### 3.6 §9 E2E test plan readiness

For each of §9.1–§9.5, evaluate:
- Whether the route surface and DB state needed to run the test exist on main today.
- What ops/secrets/state preconditions must be met before the test is *actually runnable*.
- What the failure modes look like if the test were run today against a real Twilio test brand and a real Vapi inbound call.

For §9.1 (brand setup): this is the only test that's actually runnable without Twilio/Vapi creds. Run it. Either it passes or report the failure.

For §9.2–§9.5: do not run them (no real creds). Instead, **read the code path end-to-end** and write a one-paragraph runbook for each, listing every Doppler secret and DB row the operator needs to create before the test is callable. If the runbook reveals a missing piece (e.g., "the test requires `VAPI_API_KEY` AND a Vapi assistant created in Vapi's dashboard with the right webhook URL pointing at the hq-x endpoint"), surface that as a finding even if the code itself is correct.

### 3.7 Auth boundary

Try every protected route with these credential states:
- No `Authorization` header at all → expect 401
- Bearer with random garbage → expect 401
- Bearer with the trigger secret → expect 200 (system caller)
- Bearer with a valid operator JWT → expect 200
- Bearer with a valid client-role JWT (not operator) → expect 403

If any protected route accepts an unauthenticated request, that's a P0.

For webhook routes (`/api/webhooks/twilio/{brand_id}`, `/api/webhooks/twilio-trust-hub/{brand_id}`, `/api/v1/vapi/webhook`), confirm signature validation is the only auth layer (no `require_flexible_auth` dep) and that bad signatures get rejected when the corresponding signature mode is `enforce` / `strict`.

### 3.8 Idempotency

For both Vapi and Twilio webhook receivers, send the same event twice and confirm:
- Both POSTs return 200.
- Only one row appears in `webhook_events`.
- Side-effects (call_logs / sms_messages / vapi_transcript_events writes) happen exactly once, not twice.

For event types where the directive specifies a particular event_key shape (e.g. `vapi:{call_token}:{event_type}:{status}` for status-update), verify the implementation actually produces that key, not a different one that would silently allow duplicates.

### 3.9 Encryption boundary

For `business.brands.twilio_account_sid_enc` / `twilio_auth_token_enc`:
- Read the column raw (`SELECT twilio_account_sid_enc FROM business.brands ...`) and confirm the bytes are NOT plaintext.
- Confirm `pgp_sym_decrypt(twilio_account_sid_enc, '<key>')` returns the original.
- Grep the codebase for any path that logs, prints, or otherwise emits the decrypted plaintext outside the immediate Twilio call. If found, P0.
- Confirm `app.services.brands.get_twilio_creds` raises `BrandCredsKeyMissing` (not a silent return-None) when `BRAND_CREDS_ENCRYPTION_KEY` is unset, and that callers map that to 503 (not 200 with empty creds).

### 3.10 Production safety

Read `app/config.py` and confirm:
- `_strict_signature_modes()` is called at module import time AND raises if `APP_ENV='prd'` and either `VAPI_WEBHOOK_SIGNATURE_MODE != 'strict'` or `TWILIO_WEBHOOK_SIGNATURE_MODE != 'enforce'`.
- This is wired such that the app actually fails to boot in prd with relaxed modes (try setting it temporarily and confirming an `ImportError`-shaped failure or boot abort).

### 3.11 Trigger.dev task health

For the two new tasks (`src/trigger/voice-callback-reminders.ts`, `src/trigger/voice-callback-runner.ts`):
- TypeScript compiles: `npx tsc --noEmit -p tsconfig.json`.
- Schedule cron expressions are sane (`*/5 * * * *` for reminders, `* * * * *` for runner).
- They post to the right hq-x endpoint paths.
- They use `callHqx()` and pass through the `TRIGGER_SHARED_SECRET` correctly.

If you have access to deploy them: do not deploy. Just verify static correctness.

### 3.12 Route-ordering traps

The implementing agent fixed one route-ordering bug (`/sms/suppressions` was being captured by `/sms/{message_sid}`). For every router on main, scan for similar collisions:
- `app/routers/ivr_config.py` — flow CRUD with sub-paths
- `app/routers/voice_ai.py` — assistants + sub-paths
- `app/routers/trust_hub.py` — registrations + sub-paths
- `app/routers/phone_numbers.py` — search/purchase + `{voice_phone_number_id}`
- `app/routers/voice.py` — sessions + `{transfer_id}`
- `app/routers/voice_campaigns.py` — campaign + `{campaign_id}`
- `app/routers/voice_analytics.py` — readouts + brand-scoped paths

For each, list any static-vs-wildcard pair and confirm the static is registered first. Test with a TestClient request that would have surfaced the bug.

### 3.13 Provisioning pipeline scope decision (Agent A)

Agent A skipped the OEX persistent provisioning ledger (`company_provisioning_runs`, `provisioning_run_steps`). Read `app/services/pipelines/voice.py` end-to-end and answer:
- Is the new pipeline re-runnable? If a step fails midway, what state does it leave behind?
- Are there idempotency guards on each Twilio API call inside the pipeline?
- Without a ledger, what is the operator's recovery story when step 5 (A2P campaign registration) fails after step 4 (number purchase) succeeded?
- Is this defensible for single-operator world, or is it a P1 hidden gotcha?

### 3.14 Voice campaign UoW completeness (Agent B)

Agent B couldn't port `select_eligible_leads` because `company_campaign_leads` / `campaign_lead_progress` don't exist in hq-x. Read Agent B's PR end-to-end and answer:
- Are the unit-of-work functions Agent B DID port (e.g., `count_active_calls`, `is_within_call_window`, `record_call_initiated`, `process_call_outcome`) actually callable from a Trigger.dev task today? Trace each call site.
- Is the public-facing voice-campaign batch run endpoint missing or stubbed? If stubbed, confirm the directive intends it to land later (it does — see §10 #2).
- The runner endpoint `POST /internal/voice/callback/run-due-callbacks` uses `FOR UPDATE SKIP LOCKED` for atomic claim. Verify this actually serializes correctly under concurrent ticks (write a fast concurrent-call test against a seeded row).

### 3.15 Documentation drift

Compare `HANDOFF_VOICE_PORT.md` line-by-line to what's actually on main after PR #3 + #4 landed. Every "not yet ported" item that has since landed should be flagged — the doc is now stale. If the handoff doc is misleading future reviewers, that's a P2.

---

## 4. Common rationalizations to NOT accept

When the implementer says...

- *"It's a comment, not real code"* → Read the comment. If it describes a behavior (e.g., `# org_id derived from auth context`), the comment is wrong and the rest of the function might be wrong too.
- *"That's only used in dev"* → Production code that has dev-only branches has prd bugs. Verify the prd path.
- *"The test passes"* → Look at what the test actually checks. A test that asserts `status_code in {200, 400}` is not a test.
- *"It works end-to-end"* → Define the ends. If "end-to-end" excludes the actual Vapi service, it's not end-to-end.
- *"Single-operator world makes that constraint redundant"* → The directive (§12) explicitly says the composite FK pattern stays. If the constraint is missing, that's a violation, not a simplification.
- *"That migration is harmless / idempotent / additive"* → Run it twice. Run it after a partial failure. Run it on a DB where the prior migration is partially applied. Report.
- *"It's deferred per directive"* → Cite the section. If the section actually defers it, fine. If not, the deferral is unauthorized.
- *"The smoke test passed"* → A smoke test that exercises 400-on-missing-creds paths is not a smoke test of the success path.

---

## 5. What you deliver

A single `REVIEW_FINDINGS.md` placed at the repo root, structured as:

```markdown
# Adversarial review of hq-x voice port — findings

**Reviewed:** [date]
**Scope:** main @ [SHA]
**Verdict:** [SHIP / SHIP-WITH-FIXES / DO-NOT-SHIP]

## Summary
[2-3 sentences. Be honest; no marketing.]

## P0 findings (production-breaking / spec-violating / will-cause-data-loss)
[Each finding gets: title, file:line citation, exact reproduction (commands or SQL), expected vs actual, recommended fix.]

## P1 findings (will cause incidents under foreseeable conditions)
[Same format.]

## P2 findings (drift, dead code, doc rot, future-trap)
[Same format.]

## Items the directive listed but I could not verify (and why)
[Be explicit about what creds / state would unblock verification.]

## Confirmed correct (sampled — not exhaustive)
[Brief list of what you actually exercised and confirmed working. Don't pad — only list things you genuinely tested.]
```

Each finding must be **actionable**. "Code looks suspicious" is not a finding. "`app/routers/X.py:42` writes `voicemail_drop_id` to outbound_call_configs but the column was dropped from the schema in migration 0005_voice_base.sql line 198 — INSERT will fail at runtime with `column does not exist`. Reproduction: [commands]. Fix: remove the column from the INSERT body" — that's a finding.

---

## 6. Constraints

- Read-only against the merged main code. Do not modify any source file or migration. Your output is `REVIEW_FINDINGS.md` and nothing else.
- Do not run anything against `prd` or `stg` Doppler. Use `dev` only.
- Do not call out to real Twilio or Vapi. The §9 #2-#5 tests are explicitly out of scope (they require real creds the user hasn't provisioned). State which tests are gated on those creds and stop there.
- Do not write speculative findings. Every finding must be grounded in a specific file/line citation and a reproduction.
- If you find a P0, surface it immediately at the top of the document — do not bury it.
- If you finish all 15 sections in §3 and find zero P0/P1 issues, that's a valid result — report it as such. Do not invent findings to look thorough.
- Time budget: aim for 90 minutes of actual verification work. If you blow past that, stop and ship what you have.

---

**End of directive.**
