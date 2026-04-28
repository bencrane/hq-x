# HANDOFF — Twilio + Vapi voice infrastructure port (Phase 1)

**Branch:** `oex-voice-phase-1-port`
**Worktree:** `.claude/worktrees/youthful-mclaren-cd8b69`
**Status:** Foundation green; voice surface partially ported; §9 E2E #1 verified.
**Source directive:** `outbound-engine-x/DIRECTIVE_HQX_VOICE_PORT.md`
**Source audit:** `outbound-engine-x/AUDIT_TWILIO_VAPI.md`

This is a faithful execution log for the voice port from `outbound-engine-x`
into `hq-x`. The port is binary per directive — it ships green or doesn't ship.
Foundation has been laid carefully so the remaining mechanical port work can
proceed without re-litigating any architectural decisions.

---

## What's done

### Schema (10/10 migrations applied to hq-x dev Supabase)

All schema in `migrations/`, all tracked in `schema_migrations`. brand-axis
throughout; no `org_id` columns. Composite `*_same_brand_fk` constraints
enforce brand isolation at the DB layer.

| Migration | Tables / changes |
|---|---|
| `0002_brands.sql` | `business.brands` — pgcrypto-encrypted Twilio creds (`twilio_account_sid_enc`, `twilio_auth_token_enc`); plus `twilio_messaging_service_sid`, `primary_customer_profile_sid`, `trust_hub_registration_id` |
| `0003_partners.sql` | `business.partners` — was OEX `companies`; FK to brand; `availability_schedule`/`availability_overrides` JSONB; transfer phone+label |
| `0004_campaigns.sql` | `business.campaigns` — was OEX `company_campaigns`; partner_id NULLABLE for shared-number campaigns; `mailer_brand_mode`, `number_strategy`, `routing_key`, `assistant_substrate` (default `vapi`) |
| `0005_voice_base.sql` | `voice_assistants`, `voice_phone_numbers` (carries Vapi + Twilio identity, plus the unique partial index from OEX 061), `call_logs` (folds OEX `voice_sessions` — `amd_result`, `business_disposition`, `dial_action_status` columns added), `transfer_territories`, `outbound_call_configs` (FK rewired call_logs in lieu of voice_sessions) |
| `0006_trust_hub_registrations.sql` | full Trust Hub state machine (one registration per brand per type) |
| `0007_sms.sql` | `sms_messages` + **NEW** `sms_suppressions` (drift fix §7.3 — STOP/HELP) |
| `0008_ivr.sql` | `ivr_flows`, `ivr_flow_steps` (`audio_url` merged from OEX 044), `ivr_phone_configs`, `ivr_sessions` (FK rewired to `call_logs`) |
| `0009_voice_ai_extras.sql` | `voice_ai_campaign_configs`, `voice_campaign_active_calls`, `voice_campaign_metrics`, `voice_assistant_phone_configs`, `do_not_call_lists`, `voice_callback_requests` (**NEW** cols: `leave_voicemail_on_no_answer`, `voicemail_script` for §7.7; `reminder_sent_at`, `reminder_sms_sid` for §7.6), `vapi_transcript_events` |
| `0010_webhook_events.sql` | adds `brand_id` to existing `webhook_events`; backfills `cal_raw_events` |

### Code

- `app/providers/twilio/{_http,client,trust_hub,twiml,webhooks}.py` —
  drop-in from OEX. Voice Access Token + Conversations token mints dropped
  (no in-repo consumer per directive).
- `app/providers/vapi/{_http,client}.py` — drop-in.
- `app/models/{voice_ai,voice_campaigns,voice_inbound,sms,trust_hub,ivr,outbound_calls}.py`
  — ported with `org_id`→`brand_id`, `company_id`→`partner_id`,
  `company_campaign_id`→`campaign_id`, `company_campaign_lead_id`→`campaign_lead_id`.
- `app/clickhouse.py` — fire-and-forget HTTP client (no SDK).
- `app/auth/flexible.py` — `require_flexible_auth` accepting either
  Trigger shared secret (system caller) or operator JWT.
- `app/services/brands.py` — pgcrypto-backed CRUD + `get_brand_id_by_phone_number`
  resolver used by Vapi/Twilio webhook receivers.
- `app/routers/brands.py` — operator CRUD: POST/GET/list + creds rotate.
- `app/config.py` — `HQX_API_BASE_URL`, `BRAND_CREDS_ENCRYPTION_KEY`,
  `VAPI_API_KEY`, `VAPI_WEBHOOK_SECRET`, `VAPI_WEBHOOK_SIGNATURE_MODE` (default `strict`),
  `TWILIO_WEBHOOK_SIGNATURE_MODE` (default `enforce`),
  `CLICKHOUSE_*`, `DEX_BASE_URL`. Production safety check refuses to boot
  with relaxed signature modes.
- `pyproject.toml` — `twilio>=9.0`, `httpx>=0.27`.

### Doppler (hq-x dev)

- `BRAND_CREDS_ENCRYPTION_KEY` set (random 48-byte base64).
- All other voice-related secrets still TODO — see "Doppler secrets remaining" below.

### E2E verified

- **§9.1 brand setup** — `create_brand` with stub Twilio creds, decrypt
  round-trip, list, phone-number resolver. Run `doppler run --project hq-x
  --config dev -- uv run python /tmp/test_brand_e2e.py` (script in this
  repo at `/tmp/test_brand_e2e.py` during port; not committed).

---

## Architectural decisions (already made — do not revisit)

1. **Schema split**: brands/partners/campaigns in `business.*`; voice/SMS/IVR/Trust Hub in `public.*`. Cross-schema FKs throughout.
2. **No `voice_sessions`**: folded into `call_logs` (added `amd_result`, `business_disposition`, `dial_action_status` columns). When porting OEX `twilio_webhooks.py`, every `voice_sessions` read/write must redirect to `call_logs`.
3. **No `voicemail_drops` table**: Vapi handles voicemail TTS dynamically via `assistantOverrides` in the callback runner.
4. **Encrypted creds**: pgcrypto `pgp_sym_encrypt` with `BRAND_CREDS_ENCRYPTION_KEY` from Doppler. Use the helpers in `app/services/brands.py`; never write plaintext to BYTEA columns directly.
5. **Single Vapi account**: `VAPI_API_KEY` is global (not per-brand). Per-brand Vapi was an OEX multi-tenant artifact.
6. **`require_flexible_auth`**: collapses the directive's "super-admin API key OR super-admin JWT OR Supabase ES256 JWT" onto trigger-secret-OR-operator-JWT in single-operator world. Routers should use this dep instead of inventing new ones.
7. **Webhook auth**: signature validation only, not auth deps. Pattern matches Cal/EmailBison receivers.
8. **`webhook_events` writes preserved** for idempotency log even though no consumer exists in hq-x Phase 1 (per directive — table accumulates events).
9. **`voice_campaign_batch.py` + `voice_campaign_retry.py`**: port the unit-of-work functions only, NOT the batch loop. A Trigger.dev task in `src/trigger/` will call them later.

---

## Remaining work — porting checklist

Each row below has the OEX source path, the hq-x destination path, the
LOC budget, and the specific transformations. Files marked **bracketed**
are intentionally deferred per directive §10 or for follow-up PR.

### Services (port from `outbound-engine-x/src/services/` → `app/services/`)

| OEX source | LOC | hq-x destination | Transformations |
|---|---:|---|---|
| `services/trust_hub.py` | 574 | `app/services/trust_hub.py` | `org_id`/`company_id`→`brand_id`; lookup creds via `app.services.brands.get_twilio_creds(brand_id)` instead of `organizations.provider_configs.twilio.*`; `_fail_registration` helper kept; the registration state machine (`draft` → `pending-review` → `twilio-approved`/`rejected`) is unchanged |
| `services/voice_ai_tools.py` | 402 | `app/services/voice_ai_tools.py` | drop `org_id` arg from all 4 handlers (`get_transfer_destination`, `log_call_outcome`, `schedule_callback`, `check_do_not_call`); use `brand_id` instead. **Drift fix §7.4: `lookup_carrier` reads `settings.DEX_BASE_URL` (not `hypertide_base_url`).** Rename to `dex_base_url` in the function body. |
| `services/voice_inbound_routing.py` | 218 | `app/services/voice_inbound_routing.py` | `org_id`→`brand_id`; lookup chain stays the same (`voice_assistant_phone_configs` by called_number) |
| `services/sms.py` | 116 | `app/services/sms.py` | `org_id`→`brand_id`; **drift fix §7.3**: before `send_message`, call `_check_suppression(brand_id, to_number)` against `sms_suppressions`. Raise `SmsSuppressedError` if hit. |
| `services/call_analytics.py` | 67 | `app/services/call_analytics.py` | `org_id`→`brand_id`; ClickHouse client = `app.clickhouse.insert_row("call_events", row)` |
| `services/recording_storage.py` | 93 | `app/services/recording_storage.py` | `org_id`/`{call_id}.wav` storage path → `{brand_id}/{call_id}.wav`; otherwise drop-in |
| `services/outbound_calls.py` | 143 | `app/services/outbound_calls.py` | `org_id`→`brand_id`; pre-create `call_logs` row instead of `voice_sessions`; FK in `outbound_call_configs.call_log_id` |
| `services/pipelines/voice.py` | 310 | `app/services/pipelines/voice.py` | 8-step provisioning. `org_id`/`company_id`→`brand_id`; credential lookup via `brands.get_twilio_creds`; otherwise verbatim |
| `services/voice_campaign_batch.py` | 456 | `app/services/voice_campaign_batch.py` | port lead-selection + retry-decision functions only. **Skip the batch loop.** Trigger.dev task in `src/trigger/` will call these as a unit-of-work. |
| `services/voice_campaign_retry.py` | 327 | `app/services/voice_campaign_retry.py` | port retry-decision logic only (callable function). **Skip scheduling.** |

### Routers (port from `outbound-engine-x/src/routers/` → `app/routers/`)

Hq-x convention: routers under `app/routers/` (no `voice/` subdir for now;
flat layout matches existing `app/routers/brands.py`). Wire each router into
`app/main.py`.

| OEX source | LOC | hq-x destination | Transformations |
|---|---:|---|---|
| `routers/twilio_webhooks.py` | 795 | `app/routers/twilio_webhooks.py` | URL path `/api/webhooks/twilio/{brand_id}`; `_resolve_twilio_credentials` calls `brands.get_twilio_creds`; **all `voice_sessions` writes redirect to `call_logs`** (folded); preserve event_key idempotency; remove `live_transcription` import (deferred); remove voicemail_drops comment |
| `routers/vapi_webhooks.py` | 1026 | `app/routers/vapi_webhooks.py` | single endpoint `POST /api/v1/vapi/webhook`; replace `_resolve_org_id` chain with `_resolve_brand_id(payload, vapi_call_id)` cascade: (1) `voice_phone_numbers.vapi_phone_number_id`→`brand_id`, (2) `voice_phone_numbers.phone_number`→`brand_id`, (3) `call_logs.vapi_call_id`→`brand_id`; 400 if all three miss; preserve 6 message types + ClickHouse dual-write fire-and-forget |
| `routers/trust_hub.py` | 374 | `app/routers/trust_hub.py` | URL path `/api/webhooks/twilio-trust-hub/{brand_id}`; **drift fix §7.1**: validate Twilio HMAC-SHA1 signature on the callback using `brands.get_twilio_creds(brand_id).auth_token` |
| `routers/sms.py` | 161 | `app/routers/sms.py` | `org_id`→`brand_id`; pre-send suppression check; **drift fix §7.2**: status-callback consumer maps `message_status` events to `sms_messages.status` updates (lookup by `message_sid`, retry once on race); **drift fix §7.5**: inbound SMS handler — STOP-keyword (`STOP|STOPALL|UNSUBSCRIBE|END|QUIT|CANCEL`, case-insensitive, body-trimmed) inserts into `sms_suppressions(brand_id, phone_number, reason='stop_keyword')`; reply matching against open `voice_callback_requests` (last 48h) logs an event row |
| `routers/voice_ai.py` | 544 | `app/routers/voice_ai.py` | assistant CRUD; `org_id`→`brand_id`; drop the 12 `.eq("org_id", auth.org_id)` filter calls — single-operator world; Vapi API key is global |
| `routers/voice_inbound.py` | 194 | `app/routers/voice_inbound.py` | `voice_assistant_phone_configs` CRUD; brand_id substitution |
| `routers/phone_numbers.py` | 226 | `app/routers/phone_numbers.py` | search/purchase/release; use `voice_phone_numbers` only (no `phone_numbers` table); creds via `brands.get_twilio_creds` |
| `routers/twiml_apps.py` | 169 | `app/routers/twiml_apps.py` | TwiML app CRUD; brand_id substitution |
| `routers/provisioning.py` | 444 | `app/routers/provisioning.py` | orchestrates the 8-step pipeline; brand_id substitution |
| `routers/outbound_calls.py` | 354 | `app/routers/outbound_calls.py` | brand_id substitution; `voice_sessions` writes → `call_logs` |
| `routers/ivr.py` | 772 | `app/routers/ivr.py` | URL paths `/api/voice/ivr/{brand_id}/...`; preserve all 6 endpoints; signature validation via `brands.get_twilio_creds` |
| `routers/ivr_config.py` | 334 | `app/routers/ivr_config.py` | flow CRUD; brand_id substitution |
| `routers/voice.py` | 692 | `app/routers/voice.py` | DROP `GET /api/voice/token` (deferred); brand_id substitution on the rest |
| `routers/voice_campaigns.py` | 206 | `app/routers/voice_campaigns.py` | port schemas + unit-of-work functions only; skip batch loop |
| `routers/voice_analytics.py` | 279 | `app/routers/voice_analytics.py` | brand_id substitution |

### Tests

Port the test files for each router/service that ports. Pattern lives in
existing `tests/` (pytest + httpx ASGI transport + monkeypatched JWT).

OEX tests to port (subset): `test_twilio_webhooks.py`, `test_vapi_webhooks.py`,
`test_trust_hub_*.py`, `test_voice_ai_*.py`, `test_voice_router.py`,
`test_sms_*.py`, `test_ivr_*.py`, `test_phase3_voice_campaigns.py`.

### Trigger.dev tasks (scaffolds to add under `src/trigger/`)

| Task | Purpose | Source pattern |
|---|---|---|
| `voice-callback-runner.ts` | fires N min before `voice_callback_requests.preferred_time`, calls hq-x `/internal/voice/callback/run` which uses `voice_campaign_batch` unit-of-work fns | new |
| `sms-callback-reminder.ts` | fires N min before each callback, sends templated reminder SMS via internal endpoint that calls `services.sms.send_sms` (which checks suppression) | new |

Pattern for task → hq-x: see `src/trigger/health-check.ts` + `src/trigger/lib/hqx-client.ts` (existing). Internal endpoints use `verify_trigger_secret` or `require_flexible_auth`.

### Doppler secrets remaining (hq-x dev/stg/prd)

```
VAPI_API_KEY=                    # global, get from Vapi dashboard
VAPI_WEBHOOK_SECRET=             # global Vapi webhook HMAC secret
DEX_BASE_URL=https://...         # data-engine-x service URL (for lookup_carrier)
HQX_API_BASE_URL=https://...     # public hq-x URL (for Twilio status callbacks + Vapi assistantRequest URL)
CLICKHOUSE_URL=                  # optional; analytics fire-and-forget
CLICKHOUSE_USER=
CLICKHOUSE_PASSWORD=
CLICKHOUSE_DATABASE=
```

Per-brand Twilio creds (account_sid + auth_token) are written via the
`PUT /admin/brands/{id}/twilio-creds` endpoint after brand creation, NOT
via Doppler.

### ClickHouse schema (deferred)

Schema lives at OEX `migrations/clickhouse/001_voice_analytics.sql`.
Run it against hq-x's ClickHouse instance (when configured). The hq-x
`call_events` schema is identical except `org_id`→`brand_id`,
`company_id`→`partner_id`, `company_campaign_id`→`campaign_id`.

### OEX deletion (gated on §9 E2E green)

After §9 passes end-to-end against a real Twilio test brand and a real
Vapi inbound call, run the deletion as a follow-up PR per directive §11.
Files to delete in OEX:
- `src/providers/{twilio,vapi,voicedrop}/` (full dirs)
- 21 voice/SMS/Trust Hub/IVR routers + 5 services + 9 model files (see directive §11)
- 14 voice/SMS/IVR/Trust Hub migrations (kept in OEX history; not re-applied)
- ~30 test files
- Voice/Twilio/Vapi/ClickHouse env vars from `src/config.py` and `.env.example`
- Doppler secrets from `outbound-engine-x` project

After deletion, OEX retains only emailbison, heyreach, smartlead, lob,
hypertide (email-infra), scaledmail, anthropic provider surfaces.

---

## §9 E2E test plan — status

| # | Test | Status |
|---|---|---|
| 1 | Brand setup test | **GREEN** (verified locally; need test against real Twilio test brand once creds rotated in) |
| 2 | Trust Hub registration end-to-end | not yet runnable — needs `services/trust_hub.py` + `routers/trust_hub.py` ports |
| 3 | Number purchase | not yet runnable — needs `routers/phone_numbers.py` + creds resolver |
| 4 | Vapi assistant attach | not yet runnable — needs `routers/voice_ai.py` + `routers/voice_inbound.py` |
| 5 | Inbound call (real) | not yet runnable — needs `routers/vapi_webhooks.py` + `services/voice_inbound_routing.py` |

The port is binary — none of these are partially shippable. All five must
go green before the OEX deletion PR opens.

---

## How to resume

```bash
cd /Users/benjamincrane/hq-x/.claude/worktrees/youthful-mclaren-cd8b69
git checkout oex-voice-phase-1-port

# Verify foundation still green
doppler run --project hq-x --config dev -- uv run python -m scripts.migrate
doppler run --project hq-x --config dev -- uv run python /tmp/test_brand_e2e.py

# Continue from "Remaining work" — services first (smallest), routers next.
# Trust Hub is the highest-value unit; recommend it second after voice_inbound_routing
# (smallest at 218 LOC, lets the inbound-call path become testable end-to-end).
```

Recommended order:
1. `voice_inbound_routing` service (218 LOC, smallest)
2. `voice_ai_tools` service (402 LOC, with §7.4 DEX fix)
3. `call_analytics` + `recording_storage` services (67 + 93 LOC)
4. `vapi_webhooks` router (1026 LOC, biggest single chunk — unblocks §9.5)
5. `voice_ai` + `voice_inbound` routers (assistant + phone-config CRUD)
6. `trust_hub` service + router (574 + 374 LOC, with §7.1 signature fix)
7. `phone_numbers` router (226 LOC) — unblocks §9.3
8. `outbound_calls` service + router
9. `twilio_webhooks` router (795 LOC, with `voice_sessions`→`call_logs` rewrite)
10. `sms` service + router (with §7.2/§7.3/§7.5 drift fixes)
11. Tests (subset for §9 paths)
12. Trigger.dev tasks (callback runner + reminder)
13. §9 full E2E run
14. OEX deletion PR
