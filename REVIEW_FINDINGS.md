# Adversarial review of hq-x voice port — findings

**Reviewed:** 2026-04-28
**Scope:** `origin/main` @ `268a388` (PR #4 head; PRs #2/#3/#4 all landed)
**Verdict:** SHIP-WITH-FIXES

## Summary

The voice port is substantially complete. Schema is on the brand axis with composite `*_same_brand_fk` constraints actually enforced, all eight §7 drift fixes (§7.1–§7.8) are wired and exercise correctly, the §3 skip list is honored (no `voicedrop`, `voicemail_drops` table, `voice_sessions`, post-call Intelligence, live-transcription utterances, pipeline-tick endpoints, voice-token endpoint, conversations router), the production safety check refuses to boot in `prd` with relaxed signature modes, encryption boundary is intact (pgcrypto in-DB, plaintext never serialized), idempotency dedupes on `webhook_events.(provider_slug, event_key)`, and §9.1 (brand setup + creds round-trip) verified against dev Supabase. Trigger.dev tasks compile and post to the correct internal endpoints. The smoke test (`tests/smoke_voice_phase2.py`) is green.

What's *not* clean: the provisioning pipeline dropped OEX's persistent ledger (`company_provisioning_runs` / `provisioning_run_steps`) without authorization in the directive — re-running after a partial failure on step 3 (number purchase) will burn money on duplicate Twilio purchases. The `add_live_transcription` / `build_outbound_connect_response_with_transcription` TwiML helpers shipped despite §3 skip-listing live transcription (no consumer; dead code). The Trust Hub callback URL reconstruction differs from the main Twilio webhook receiver (uses `request.url` not `_reconstruct_url`) and will misvalidate signatures behind a TLS-terminating proxy in `enforce` mode. A handful of doc-rot items in `HANDOFF_VOICE_PORT.md`.

§9.2–§9.5 are gated on real Twilio + Vapi creds the reviewer does not have; runbooks are below.

---

## P0 findings (production-breaking / spec-violating / will-cause-data-loss)

None.

---

## P1 findings (will cause incidents under foreseeable conditions)

### P1-1 — Provisioning pipeline has no idempotency ledger; partial failures double-charge

**Files:** `app/services/pipelines/voice.py:137-404`, `app/routers/provisioning.py:86-164`

**Behavior:** `execute_voice_pipeline` runs steps 1→7 and returns a structured result dict but persists nothing about which steps succeeded. If step 3 (`STEP_PHONE_PURCHASE`) succeeds for 2 of 3 requested numbers and then fails (Twilio rate-limit, network blip, etc.), the operator's only recovery option is to re-run the same `POST /api/brands/{brand_id}/provisioning/voice` payload — and on re-run, `twilio_client.search_available_numbers` returns *different* candidate numbers (Twilio's inventory changes minute-to-minute) and `purchase_phone_number` is called for `phone_count` numbers again. The `_record_phone_number` helper has `ON CONFLICT (phone_number, brand_id) WHERE deleted_at IS NULL DO UPDATE` — but the *Twilio purchase itself* is not idempotent. The operator pays for 2+3 = 5 numbers when they wanted 3.

The directive (§3 "Port + rebuild") lists "Voice provisioning pipeline (`services/pipelines/voice.py` 8-step orchestrated flow)" as a port-required item with no language authorizing the ledger drop. The implementer's docstring (`app/services/pipelines/voice.py:4-9`) self-justifies dropping it: *"hq-x has no companies, no provisioning ledger, and a single brand axis — so this version returns a structured result dict instead of writing to a ledger."* The single-brand axis is irrelevant to whether you need a ledger to recover from partial failure; the rationalization §4 of the review directive flagged ("single-operator world makes it redundant") is exactly this pattern.

**Reproduction (dry-run sketch — would burn live $; do not actually run):**
1. `POST /api/brands/{brand_id}/provisioning/voice` with `{"phone_config": {"count": 3}, ...}`.
2. Twilio purchase succeeds for numbers 1 and 2; on number 3 it fails with `21452: no available numbers in this area`.
3. Pipeline returns 200 with `result["steps"]["phone_purchase"] = {"status": "failed", ...}`. Numbers 1 and 2 are in `voice_phone_numbers`.
4. Operator inspects, decides to retry. `POST` again with the same body.
5. `search_available_numbers` returns a fresh page of 3 candidates and `purchase_phone_number` is invoked for all 3. Final state: 2 + 3 = 5 numbers in `voice_phone_numbers`, 5 numbers being billed by Twilio.

**Recommended fix:** Either (a) restore a minimal `provisioning_runs(id, brand_id, status, completed_steps[])` ledger keyed on a client-supplied `idempotency_key`, OR (b) require the caller to pass a list of *exact* `phone_numbers_to_purchase` (rather than a count + search), so retries are deterministic, OR (c) document the no-recovery semantics loudly in the route docstring and have the route refuse re-runs that would trigger another purchase phase. The directive §10 deferred list doesn't include "provisioning ledger" so a partial deferral is also acceptable, but it has to be conscious.

---

### P1-2 — Trust Hub callback URL reconstruction breaks Twilio signature behind a proxy

**Files:** `app/routers/trust_hub.py:400` vs `app/routers/twilio_webhooks.py:157-162` (`_reconstruct_url`)

**Behavior:** `trust_hub_status_callback` validates Twilio's HMAC by computing `url = str(request.url)` (line 400). The main Twilio receiver (`twilio_webhooks.py`) goes through `_reconstruct_url(request)`, which honors `X-Forwarded-Proto` and `X-Forwarded-Host`. Twilio signs the *public* HTTPS URL it called (e.g., `https://api.hqx.example/api/webhooks/twilio-trust-hub/{brand_id}`); `request.url` inside FastAPI behind Railway's TLS-terminating proxy will be `http://internal-host:8000/...`. The signature will mismatch, and in `enforce` mode (production default) the callback gets a 403 — Trust Hub status updates will fail to write back to `trust_hub_registrations`. The bug is dormant in dev (no proxy) and triggers only in prd.

This is exactly the §7.1 drift fix: the directive said "validate Twilio's HMAC-SHA1 signature on this endpoint using the brand's `twilio_auth_token`, identical to the main Twilio webhook." The signature validation is *present*, but the URL reconstruction is *not identical* and will silently reject in production.

**Reproduction:** Set `X-Forwarded-Proto: https` and `X-Forwarded-Host: api.hqx.example` headers on a request whose `request.url` is `http://localhost:8000/api/webhooks/twilio-trust-hub/{brand_id}`; sign the *forwarded* URL. The Trust Hub callback returns 403 because `str(request.url)` is the internal URL, not the signed one. Repeat with `X-Forwarded-*` removed (dev path) and it passes. Both inputs come from the same Twilio invocation pattern.

**Recommended fix:** Replace `url = str(request.url)` at `trust_hub.py:400` with `url = _reconstruct_url(request)`, importing the helper from `app.routers.twilio_webhooks` (or moving it to a shared module). This is a one-line change.

---

### P1-3 — Live-transcription TwiML helpers shipped despite §3 skip-list (dead-code latent risk)

**Files:** `app/providers/twilio/twiml.py:264-330` (`add_live_transcription`, `build_outbound_connect_response_with_transcription`)

**Behavior:** The directive §3 explicitly skips live transcription: *"Live transcription utterances (`services/live_transcription.py`, migration `043_live_transcription_utterances.sql`). Niche; would only matter for a call-monitoring UI that doesn't exist."* The migration and service are correctly absent. But the TwiML helper that *injects* `<Start><Transcription>` into outbound TwiML responses is present. `grep` shows zero in-tree consumers (`app/providers/twilio/twiml.py:315` is the only call, inside `build_outbound_connect_response_with_transcription` itself). It's dead code, but worse: the TwiML it produces references a `status_callback_url` that will land in `webhook_events` with an `event_type` no consumer reacts to — *unless* a future agent imports this helper thinking "transcription is already wired" and ships a hot path that tells Twilio to start streaming utterances we never persist.

This is P1 not P0 because nothing calls it today. Promoting to P0 if a consumer is ever wired without re-reading the directive.

**Reproduction:** `grep -rn "add_live_transcription\|build_outbound_connect_response_with_transcription" app/` returns only the definitions. No router or service calls these helpers. Confirmed dead.

**Recommended fix:** Delete both functions from `twiml.py`. If the operator decides later to wire a call-monitoring UI, restoring them is mechanical from OEX `src/providers/twilio/twiml.py`.

---

## P2 findings (drift, dead code, doc rot, future-trap)

### P2-1 — `HANDOFF_VOICE_PORT.md` is stale (was written between PR #2 and PR #3/#4)

**Files:** `/Users/benjamincrane/hq-x/HANDOFF_VOICE_PORT.md`

The handoff doc lists the IVR engine, voice CRUD, voice campaigns, Trigger.dev tasks, etc., as *remaining work*. PR #3 (`e6d9420`) and PR #4 (`268a388`) landed those surfaces. A future reviewer reading this doc would think the port is half-done. The file describes itself as *"Foundation green; voice surface partially ported"* in line 5 — that statement is no longer true. PR #3 added voice-campaign UoW + Trigger.dev tasks + §7.5; PR #4 added IVR + provisioning + outbound + voice CRUD.

**Recommended fix:** Either delete `HANDOFF_VOICE_PORT.md` (work is done) or rewrite the "What's done" / "Remaining" sections to reflect the post-PR-#4 state. If keeping it, mark it *archive* and add a "see commit log for current state" pointer.

---

### P2-2 — `_REMINDER_SUPPRESSED_SENTINEL = "__suppressed__"` smells; no schema constraint

**Files:** `app/routers/internal/voice_callbacks.py:43`, `203-217`

When `send_sms` raises `SmsSuppressedError` from a callback-reminder send, the code stamps `voice_callback_requests.reminder_sms_sid = "__suppressed__"` (a sentinel string) so the row isn't picked up again. This works because no Twilio status callback will ever arrive with that SID (Twilio SIDs are `MM...`-prefixed UUIDs). But the column has no CHECK constraint preventing a real `MM...` SID from colliding with the sentinel, and any analytics SQL that joins `voice_callback_requests.reminder_sms_sid` to `sms_messages.message_sid` will return zero rows for suppressed reminders — fine for now, surprise later. Low blast radius; documenting for future cleanup.

**Recommended fix:** Add a separate `reminder_status TEXT CHECK (reminder_status IN ('sent','suppressed','failed') OR reminder_status IS NULL)` column rather than overloading the SID. Or use NULL + a `reminder_suppressed_at TIMESTAMPTZ` column.

---

### P2-3 — Vapi `tool-calls` events are not idempotent

**Files:** `app/routers/vapi_webhooks.py:701-705`, `_RESPONSE_HANDLERS`

`_RESPONSE_HANDLERS` (which includes `tool-calls`) bypasses the `_persist_event_or_skip_duplicate` guard. If Vapi retries a tool-calls webhook (e.g., its 7.5s timeout fires and Vapi reposts), the underlying tool handler runs twice. In practice the consequences are bounded: `schedule_callback` uses `INSERT ... ON CONFLICT DO NOTHING` against the unique `(brand_id, source_vapi_call_id, preferred_time, timezone)` index; `log_call_outcome` is an `UPDATE`; `check_do_not_call` and `lookup_carrier` are reads; `get_transfer_destination` is a read. So the worst case is a duplicate tool response sent back to Vapi, not data corruption.

This is P2 because the existing row-level uniqueness constraints save the day. If a future tool handler is added that lacks an idempotency key, it will be quietly broken under retry pressure.

**Recommended fix:** Add the tool-calls path through `_persist_event_or_skip_duplicate` keyed on `vapi:{call_token}:tool-calls:{tool_call_id}` for each `toolCalls[*].id`, and short-circuit the dispatch on duplicate. Or document the "tool handlers MUST be idempotent at row level" rule in `voice_ai_tools.py`.

---

### P2-4 — `_handle_amd_result` UPDATE is silent if `call_logs` row doesn't yet exist

**Files:** `app/routers/twilio_webhooks.py:234-247`

If the `amd_result` event arrives before the corresponding `call_status` event has inserted the `call_logs` row (rare but possible — Twilio webhook delivery is unordered), the `UPDATE call_logs SET amd_result = ... WHERE twilio_call_sid = ...` matches zero rows and the AMD signal is lost. `_handle_call_status` does upsert-by-fallback-insert, but `_handle_amd_result` does not. The webhook still returns 200 (`webhook_events` row was inserted by the dispatch loop) so Twilio won't retry.

In practice Twilio's standard sequence is `initiated → ringing → in-progress → answered_by → completed`, so `amd_result` lands after `call_status=in-progress`. The reverse-order race is bounded.

**Recommended fix:** Either upsert in `_handle_amd_result` the same way `_handle_call_status` does, or sequence the handlers (call_status writes always before amd_result) — but Twilio's webhook order isn't guaranteed, so an upsert is the only safe choice.

---

### P2-5 — `phone_numbers.py` route ordering: `/twilio/inventory` after `/{voice_phone_number_id}/twilio`

**Files:** `app/routers/phone_numbers.py:183, 298`

`@router.get("/{voice_phone_number_id}/twilio")` (line 183) is registered before `@router.get("/twilio/inventory")` (line 298). Because `voice_phone_number_id` is typed `UUID`, FastAPI rejects a literal `"twilio"` and falls through to the inventory route — verified at runtime (both endpoints return the expected 400 on no-creds against the fmcsa-stub brand). Currently safe. If a future maintainer drops the `UUID` type annotation (perhaps to support an int-id surrogate, or `str`), `/twilio/inventory` would be silently captured by the wildcard. Documenting because the implementer flagged the SMS variant of this trap (`sms.py:165, 201, 221`) and got it right there.

**Recommended fix:** Either reorder (move `/twilio/inventory` above `/{voice_phone_number_id}/twilio`), or add a path constraint (`Path(..., regex="^[0-9a-fA-F-]{36}$")`). One-line fix.

---

### P2-6 — `outbound_calls.py` retains `voicemail_drop` route names from OEX

**Files:** `app/routers/outbound_calls.py:243-260, 305-322`; `app/providers/twilio/twiml.py:232 (build_voicemail_drop_response)`

The HTTP routes `POST /api/voice/outbound/twiml/voicemail-drop/{call_log_id}` and `POST /api/voice/outbound/twiml/ai-voicemail-drop/{call_log_id}` are present. They don't read or write a `voicemail_drops` table (the table is correctly skipped per directive); they read the `voicemail_audio_url` and `voicemail_text` columns on `outbound_call_configs` and emit TwiML. This is acceptable functionally — Twilio-substrate AMD voicemail drop does still need a TwiML payload — but the naming overlap with the *forbidden* `voicemail_drops` table will cause a future agent to grep for the table, hit these routes, and panic. The `ai-voicemail-drop` route docstring at line 312-314 explicitly disclaims the table dependency, but the route itself is dead code unless a Twilio-substrate (non-Vapi) campaign is wired (and the directive defaults new campaigns to Vapi). Per §12 ("No scaffolding without a consumer"), this is borderline.

**Recommended fix:** Either rename the routes to `/twiml/voicemail-message/...` to avoid the lexical collision, or delete the AI-voicemail-drop route until a Twilio-substrate campaign needs it. Lower priority than the live-transcription dead code (P1-3) because at least these routes are wired into the assistant_substrate=twiml path.

---

### P2-7 — Migration filename collision: two `0011_*.sql` files

**Files:** `migrations/0011_direct_mail_lob.sql`, `migrations/0011_voice_callback_inbound_link.sql`; `migrations/0012_fmcsa_ivr_seed.sql`

Two migrations share the `0011_` prefix. `scripts/migrate.py` likely orders alphabetically — which works (`0011_direct_mail_lob` < `0011_voice_callback_inbound_link` < `0012_fmcsa_ivr_seed`) but is fragile. A future migration named `0011_a_xxx.sql` would jump ahead and could break dependency order. The 0012 file's first comment line says *"-- Migration 0011: Seed the FMCSA Carrier Qualification IVR flow"* but the filename is `0012_*` — also internally inconsistent.

**Recommended fix:** Rename `0011_voice_callback_inbound_link.sql` to (the next free index) and `0012_fmcsa_ivr_seed.sql`'s header comment to match its filename. Or pick a date-based scheme to avoid further collisions.

---

## Items the directive listed but I could not verify (and why)

### §9.2 — Trust Hub registration end-to-end

Requires real Twilio test-environment account_sid/auth_token wired into a brand row and a real Twilio test-environment Customer Profile bundle. Code path verified statically: `app/services/trust_hub.py:register_customer_profile` exists, `POST /api/trust-hub/brands/{brand_id}/register` decrypts brand creds and calls into the Trust Hub service, which orchestrates Twilio Customer Profiles, EndUsers, supporting documents, evaluation polling. The status-callback at `/api/webhooks/twilio-trust-hub/{brand_id}` lives in `trust_hub.py:webhook_router` and does signature-validate (§7.1 drift fix landed) — but see **P1-2** for the URL reconstruction bug that would make it 403 in `enforce` mode in prd.

**Runbook to actually run it:**
1. Doppler `hq-x dev`: ensure `BRAND_CREDS_ENCRYPTION_KEY` set (it is).
2. Operator creates a test brand: `POST /api/brands` with stub creds via `create_brand` (works — verified §9.1 path).
3. Operator manually creates a Twilio test Customer Profile in the dashboard and grabs its SID.
4. Set `brands.primary_customer_profile_sid` on the test brand.
5. `POST /api/trust-hub/brands/{brand_id}/register` with the body shape in `RegisterBrandRequest`.
6. Verify `trust_hub_registrations` row appears with `status='draft' → 'pending-review'`.
7. Wait for Twilio's status callback to hit `/api/webhooks/twilio-trust-hub/{brand_id}` — **and ensure Railway's `X-Forwarded-Proto/Host` headers are honored or fix P1-2 first**, otherwise the 403 in enforce mode silently drops the update.

### §9.3 — Number purchase

Requires real Twilio test creds. Code path: `POST /api/brands/{brand_id}/phone-numbers/purchase` (`phone_numbers.py:110`) → `twilio_client.purchase_phone_number` → `INSERT INTO voice_phone_numbers`. Trivially callable when creds exist; the `_record_phone_number` insert is correct. The provisioning-pipeline retry hazard (**P1-1**) bites here — direct CRUD does not.

### §9.4 — Vapi assistant attach

Requires real Vapi API key + a Vapi-created assistant. Code path: `POST /api/brands/{brand_id}/voice-ai/assistants` (`voice_ai.py:113`) creates a `voice_assistants` row. `voice_assistant_phone_configs` insert via `voice_ai.py` for inbound mapping. All static SQL inspected and matches schema. No findings.

### §9.5 — Real inbound call

Requires (a) real Twilio purchased number pointing at hq-x's `/api/v1/vapi/webhook`, (b) real Vapi assistant with phoneNumberId attached, (c) real call placed. Brand resolution cascade verified statically: `_resolve_brand_id` (`vapi_webhooks.py:157-222`) uses the directive's three-step cascade (vapi_phone_number_id → phone_number → vapi_call_id). ClickHouse dual-write fire-and-forget at `vapi_webhooks.py:603-623`. End-of-call-report writes to `call_logs` via the directive-specified UPSERT on `(vapi_call_id)`. Idempotency dedup via `vapi:{call_token}:end-of-call-report:{ended_at}` event_key. Everything wired correctly; just not exercised against a real call.

**Runbook to actually run it:** Set Doppler `hq-x dev`'s `VAPI_API_KEY` + `VAPI_WEBHOOK_SECRET` + `HQX_API_BASE_URL=https://<your-tunnel>.example`. Configure a Vapi assistant in the Vapi dashboard with `serverUrl` pointing at `${HQX_API_BASE_URL}/api/v1/vapi/webhook`. Buy a real Twilio number for the brand, register it in Vapi via `import_phone_number`, set `voice_assistant_phone_configs(phone_number, voice_assistant_id, brand_id)` to the new assistant. Place a real call. Verify in Postgres: `call_logs` row by `vapi_call_id`, `vapi_transcript_events` rows, `webhook_events` rows. Verify in ClickHouse: `analytics.call_completions` row.

### Auth boundary spot checks (§3.7)

I did not exhaustively curl every protected route with five credential states. The internal voice-callback endpoints are tested for 401 / wrong-secret rejection in `tests/test_internal_voice_callbacks_auth.py` and pass. The smoke test exercises all major routes with the trigger secret. JWT validation logic lives in `app/auth/flexible.py` and `app/auth/jwt.py`; tests in `tests/test_supabase_jwt.py` pass. Sampled — not exhaustive.

---

## Confirmed correct (sampled — not exhaustive)

- **Schema integrity (§3.1).** All required tables exist in the right schema (`business.{brands,partners,campaigns,users}`, public.{voice_*, sms_*, ivr_*, trust_hub_*, do_not_call_lists, vapi_transcript_events, voice_callback_requests, webhook_events}). Forbidden tables absent: `voice_sessions`, `voicemail_drops`, `phone_numbers` (singular), `call_transcripts`, `live_transcription_utterances`. The four §7.6/§7.7 `voice_callback_requests` columns (`leave_voicemail_on_no_answer`, `voicemail_script`, `reminder_sent_at`, `reminder_sms_sid`) present. The §7.5 columns (`last_inbound_sms_at`, `last_inbound_sms_sid`) present in migration `0011_voice_callback_inbound_link.sql`. `sms_suppressions` has `(brand_id, phone_number)` UNIQUE and `reason CHECK IN ('stop_keyword','manual','bounce')`. `voice_phone_numbers.vapi_phone_number_id` partial unique present.

- **Composite FK enforcement (§3.3).** Tested via direct SQL inserts: `business.campaigns` rejects a `partner_id` belonging to a different `brand_id` with `campaigns_same_brand_fk`; `voice_phone_numbers` rejects partner-from-different-brand with `voice_phone_numbers_partner_same_brand_fk`. Constraints fire as designed.

- **§7.1 Trust Hub callback signature validation.** Code present at `trust_hub.py:389-409`. Signature mismatch → 403 in enforce mode; missing creds → 403 in enforce mode. *(But see P1-2 for the URL-reconstruction bug.)*

- **§7.2 SMS status-callback persistence.** `_handle_message_status` at `twilio_webhooks.py:282-291` calls `sms_svc.update_status_from_callback` which `UPDATE sms_messages SET status = ... WHERE message_sid = ... RETURNING id`. Verified path.

- **§7.3 SMS STOP suppression.** Pre-send check at `sms.py:91-96` raises `SmsSuppressedError`. Inbound STOP → `add_suppression` at `twilio_webhooks.py:309-323` with `ON CONFLICT (brand_id, phone_number) DO NOTHING`. STOP keyword regex at `sms.py:28-31` matches `stop|stopall|unsubscribe|end|quit|cancel` case-insensitive trimmed.

- **§7.4 `lookup_carrier` mis-wiring fix.** `voice_ai_tools.py:60-107` reads `settings.DEX_BASE_URL`; returns `"error: carrier lookup service not configured"` when unset (verified by direct Python invocation with `DEX_BASE_URL` unset).

- **§7.5 Inbound SMS reply matching.** `link_inbound_sms_to_callback` at `sms.py:200-239` updates the most-recent open callback row matching `(brand_id, customer_number)` within 48h, stamps `last_inbound_sms_at` + `last_inbound_sms_sid`. Wired into `_handle_inbound_message` at `twilio_webhooks.py:325-342`.

- **§7.6 SMS callback-reminder.** Internal endpoint `POST /internal/voice/callback/send-reminders` at `voice_callbacks.py:64-148` requires flexible auth, returns shape `{processed, sent, suppressed, errors}`. Trigger.dev task `src/trigger/voice-callback-reminders.ts` runs `*/5 * * * *`, calls `callHqx()`. TS compiles clean (`npx tsc --noEmit -p tsconfig.json` exit 0).

- **§7.7 Voicemail-on-callback runner.** Internal endpoint `POST /internal/voice/callback/run-due-callbacks` at `voice_callbacks.py:225-342` uses `WITH due AS (... FOR UPDATE SKIP LOCKED) UPDATE ... FROM due` for atomic claim. Sets `assistantOverrides.voicemailMessage` + `voicemailDetection` on Vapi `create_call`. TS task at `src/trigger/voice-callback-runner.ts` runs `* * * * *`. Compiles.

- **§7.8 `VAPI_WEBHOOK_SIGNATURE_MODE` defaults.** `config.py:46` defaults `"strict"`; `config.py:91-104` `_strict_signature_modes()` is called at module import (`config.py:107`) and raises `RuntimeError` if `APP_ENV='prd'` and either VAPI mode != strict or TWILIO mode != enforce. **Verified at runtime**: setting `APP_ENV=prd VAPI_WEBHOOK_SIGNATURE_MODE=permissive_audit` and importing `app.config` raises `RuntimeError: VAPI_WEBHOOK_SIGNATURE_MODE must be 'strict' in production (got 'permissive_audit')`.

- **brand_id substitution audit (§3.2).** `grep -rn "org_id\|company_id" app/ migrations/ src/trigger/ tests/` returns only docstrings/comments documenting the OEX→hq-x substitution; no live code reads `auth.org_id` or filters by `org_id`. `from src.` imports: zero. `company_campaign*` references: zero. `voicedrop` references: zero.

- **Skip-list audit (§3.5).** All ten skip-list items absent: `voicedrop` (any form), `voicemail_drops` table (column dropped from `0005_voice_base.sql`), Twilio Conversational Intelligence (`providers/twilio/intelligence.py` not present), Live transcription service (absent — but see **P1-3** for the TwiML-helper exception), `voice_sessions` (absent), older `phone_numbers` (absent), `pipeline_endpoints.py` / `voice_campaigns_internal.py` / `orchestrator.py` (all absent), `routers/conversations.py` (absent), Voice Access Token endpoint (absent — confirmed by grep `voice/token`).

- **Idempotency (§3.8).** Verified by inserting the same event twice into `webhook_events`: first returns `is_new=True`, second returns `is_new=False` (UNIQUE on `(provider_slug, event_key)` enforces). `_build_info_event_key` produces `vapi:{call_token}:status-update:{status}` for status-update events as the directive requires.

- **Encryption boundary (§3.9).** Created a brand with stub creds via `create_brand`, decrypted via `get_twilio_creds`, confirmed round-trip (`ACtest_acct` → ciphertext (77 bytes, plaintext bytes not present) → `ACtest_acct`). `BrandCredsKeyMissing` raised when `BRAND_CREDS_ENCRYPTION_KEY` unset; callers map it to 503 (e.g., `provisioning.py:101-102`, `trust_hub.py:122-123`).

- **§9.1 brand setup test.** Passes against dev Supabase. Smoke test (`tests/smoke_voice_phase2.py`) green: lists brands, finds `fmcsa-stub`, lists/CRUDs IVR flows, all no-creds-brand routes return correct 400/404, voice analytics summary returns shape, voice campaign metrics 404s for unknown campaign.

- **Trigger.dev task health (§3.11).** TypeScript compiles clean. Cron expressions correct (`*/5 * * * *` reminders, `* * * * *` runner). `callHqx` injects `Authorization: Bearer ${TRIGGER_SHARED_SECRET}` and posts to the right hq-x endpoint paths.

- **Voice campaign UoW pure tests.** `tests/test_voice_campaign_batch_pure.py` + `tests/test_voice_campaign_retry_pure.py` (31 tests) all pass without DB.

---

**End of findings.**
