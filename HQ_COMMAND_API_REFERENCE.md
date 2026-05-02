# hq-command ↔ hq-x API reference

Audience: an AI agent (or human) building hq-command, the operator-facing
frontend for managing Vapi voice agents through hq-x's FastAPI backend.

This doc is the authoritative API surface map. Pair it with your own
business-needs / UI brief to scope a feature; this file alone will not
tell you *what* to build, only *what's available* and *how it behaves*.

---

## 0. Conventions

- **Base URL:** `{HQX_API_BASE_URL}` — set per environment (dev / stg / prd).
- **Brand-scoped route shape:** `{HQX_API_BASE_URL}/api/brands/{brand_id}/...`
  Almost every Vapi-related route is brand-scoped. The frontend always
  knows the active `brand_id` from its UI state (brand picker / URL).
- **Single-tenant data model:** there is no `org_id` / `client_id`. The
  brand axis is the *only* scoping axis. See [ARCHITECTURE.md](ARCHITECTURE.md).
- **Source-of-truth files:**
  - Router files at `app/routers/*.py` define every endpoint shape.
  - Migrations at `migrations/*.sql` define every table column.
  - `app/providers/vapi/client.py` is the Vapi SDK wrapper.

When a shape detail isn't in this doc, read the matching router file —
the request/response models live at the top of each router.

---

## 1. Auth (already wired in hq-command)

Every request to hq-x sends:

```
Authorization: Bearer <supabase_jwt>
Content-Type: application/json
```

The JWT must:
1. Be issued by Supabase Auth (ES256, verified against JWKS).
2. Decode to a `business.users` row with `role = "operator"`.

`role = "client"` users get **403 on every Vapi route**. Only operators
manage voice agents.

System callers (Trigger.dev tasks, cron jobs) authenticate with the
shared `TRIGGER_SHARED_SECRET` instead — but hq-command should always
use the operator JWT. See [app/auth/flexible.py](app/auth/flexible.py).

### Get current user

```
GET /admin/me
→ 200 { auth_user_id, business_user_id, email, role, client_id }
```

Use this on app boot to verify the JWT is valid + the user is an operator.

---

## 2. Error envelopes

### Standard error (4xx from hq-x logic)

```json
HTTP 4xx
{ "detail": { "error": "<machine_key>", "message": "<human text>" } }
```

`error` is a stable machine-readable key. The frontend should branch on
`error`, not on `message`. Common keys:

| key | typical HTTP | meaning |
|---|---|---|
| `assistant_not_found` | 404 | id/brand mismatch, soft-deleted, or pointer with no `vapi_assistant_id` |
| `local_insert_failed_after_vapi_create` | 500 | Vapi assistant created but local mirror insert failed; clean up via `/vapi/assistants` list |
| `phone_number_not_found` | 404 | not in this brand or soft-deleted |
| `phone_number_not_imported_to_vapi` | 404/409 | no `vapi_phone_number_id` |
| `already_imported` | 409 | already has a Vapi mirror |
| `phone_number_not_twilio_owned` | 400 | local row has no Twilio SID |
| `idempotency_key_required` | 400 | missing `Idempotency-Key` header |
| `no fields to update` | 400 | empty PATCH body |
| `Brand has no Twilio credentials configured` | 400 | brand setup incomplete |
| `BRAND_CREDS_ENCRYPTION_KEY not configured` | 503 | server-side config issue |
| `VAPI_API_KEY not configured` | 503 | server-side config issue |
| `HQX_API_BASE_URL not configured` | 503 | server-side config issue |

### Vapi provider error (502/503)

```json
HTTP 502 (terminal) or 503 (transient)
{ "detail": {
    "type": "provider_error",
    "provider": "vapi",
    "operation": "create_call",
    "retryable": true|false,
    "message": "Vapi connectivity error: ..."
}}
```

- `retryable: true` (HTTP 503): transient — safe to auto-retry with backoff.
- `retryable: false` (HTTP 502): terminal — surface to user, do not retry.

Twilio provider errors share the same envelope with `provider: "twilio"`.

### FastAPI validation error (422)

Standard Pydantic shape. Frontend should display field-level errors.

---

## 3. Idempotency contract

**Required:** `POST /api/brands/{brand_id}/voice/calls`. Header:
`Idempotency-Key: <uuid>`. Missing → 400 `idempotency_key_required`.

Generate one UUID **per logical call attempt** in the frontend. Replays
with the same key return the cached `call_log` *without re-invoking
Vapi or re-charging*. The response shape distinguishes:

```jsonc
// First attempt
{ "call_log": {...}, "vapi_response": {...}, "idempotent_replay": false }

// Replay (same Idempotency-Key)
{ "call_log": {...}, "vapi_response": null, "idempotent_replay": true,
  "cached_vapi_call_id": "vapi_call_x" }
```

If Vapi fails on the first attempt (503), the ledger row is rolled back
and the key is **free for retry**. Don't reuse keys across different
calls — that would make the second call collapse onto the first.

No other endpoint requires idempotency keys. Vapi-side `POST /file`,
`/tool`, etc. are not idempotent server-side; if you're worried about
double-create, query the list before posting.

---

## 4. Endpoint surface — by capability

Every section below is operator-only. All `{brand_id}` is a UUID.

### 4.1 Brand setup — `app/routers/brands.py`

Top-level brand CRUD. Lives at `/admin/brands`, NOT under `/api/brands/{id}`.

```
POST   /admin/brands                                  create brand
                                                      body: { name, display_name?, domain?,
                                                              twilio_account_sid?, twilio_auth_token?,
                                                              twilio_messaging_service_sid?,
                                                              primary_customer_profile_sid? }
GET    /admin/brands                                  list brands
GET    /admin/brands/{brand_id}                       get one
PUT    /admin/brands/{brand_id}/twilio-creds          rotate Twilio creds (encrypted server-side)
```

The encrypted Twilio creds are stored via pgcrypto; plaintext never
serializes back. To "see" a brand's creds, you can't — only verify via
provisioning / inventory routes.

### 4.2 Trust Hub (10DLC compliance) — `app/routers/trust_hub.py`

```
POST   /api/trust-hub/brands/{brand_id}/register                                register a brand for 10DLC
GET    /api/trust-hub/brands/{brand_id}/registrations                           list registrations
GET    /api/trust-hub/brands/{brand_id}/registrations/{id}                      get one
POST   /api/trust-hub/brands/{brand_id}/registrations/{id}/refresh              poll Twilio for status
POST   /api/trust-hub/brands/{brand_id}/phone-numbers/assign                    assign a number to a Customer Profile
```

Required for any brand that will send SMS in the US. Not required for
voice-only flows. See `app/services/trust_hub.py` for the business rules.

### 4.3 Phone numbers — Twilio side — `app/routers/phone_numbers.py`

```
GET    /api/brands/{brand_id}/phone-numbers/search                              search Twilio inventory
        ?country_code=US&number_type=Local&area_code=415&contains=&...&limit=20
POST   /api/brands/{brand_id}/phone-numbers/purchase                            buy on Twilio + record locally
        body: { phone_number, friendly_name?, voice_application_sid?, sms_url? }
GET    /api/brands/{brand_id}/phone-numbers                                     list local rows
GET    /api/brands/{brand_id}/phone-numbers/{voice_phone_number_id}/twilio      Twilio's view of one number
PATCH  /api/brands/{brand_id}/phone-numbers/{voice_phone_number_id}/twilio      update Twilio config
DELETE /api/brands/{brand_id}/phone-numbers/{voice_phone_number_id}             release Twilio + soft-delete local
GET    /api/brands/{brand_id}/phone-numbers/twilio/inventory                    full Twilio account inventory
```

Local row identity: `voice_phone_numbers.id` (UUID). Twilio identity:
`voice_phone_numbers.twilio_phone_number_sid` (`PN...`). Vapi identity:
`voice_phone_numbers.vapi_phone_number_id` (NULL until imported, see §4.4).

### 4.4 Phone numbers — Vapi side — `app/routers/vapi_phone_numbers.py`

```
POST   /api/brands/{brand_id}/vapi/phone-numbers/import                         register a Twilio number with Vapi
        body: { voice_phone_number_id: UUID, assistant_id?: UUID }
        → 201 { local: {...}, vapi_phone_number_id, server_url, vapi_response }
GET    /api/brands/{brand_id}/vapi/phone-numbers                                list rows that have a Vapi mirror
GET    /api/brands/{brand_id}/vapi/phone-numbers/{voice_phone_number_id}        { local, vapi } combined view
PATCH  /api/brands/{brand_id}/vapi/phone-numbers/{voice_phone_number_id}/bind   re-bind: change assistant + re-assert serverUrl
        body: { assistant_id?: UUID, server_url_override?: HttpUrl }
DELETE /api/brands/{brand_id}/vapi/phone-numbers/{voice_phone_number_id}        release on Vapi only (Twilio stays)
```

`/import` does three things in one call:
1. Calls Vapi `POST /phone-number` with the brand's Twilio creds.
2. Calls Vapi `PATCH /phone-number/{id}` to set `server.url =
   {HQX_API_BASE_URL}/api/v1/vapi/webhook` + optional `assistantId`.
3. Persists `vapi_phone_number_id` (and optional `voice_assistant_id`)
   on the local row.

`/bind` is **idempotent re-bind**: re-asserts the serverUrl on every
call. Use this when you suspect Vapi-side drift, or just to change the
assistant binding.

If `assistant_id` is provided but the assistant has no
`vapi_assistant_id` (legacy / orphaned pointer): 409 `assistant_not_synced`.
For assistants created via the new POST `/voice-ai/assistants` flow this
shouldn't happen — `vapi_assistant_id` is stamped at create time.

### 4.5 Assistants — `app/routers/voice_ai.py`

**Vapi is the source of truth** for all assistant config. The local
`voice_assistants` row is a thin pointer that holds only the brand /
partner / campaign association + `vapi_assistant_id`. Reads pass through
to Vapi at request time; PATCH forwards straight to Vapi.

```
POST   /api/brands/{brand_id}/voice-ai/assistants                               create on Vapi, mirror pointer locally
        body: { name, assistant_type, system_prompt?, first_message?,
                first_message_mode?, model_config_data?, voice_config?,
                transcriber_config?, tools_config?, analysis_config?,
                max_duration_seconds?, metadata?, partner_id?, campaign_id? }
        assistant_type ∈ { "outbound_qualifier", "inbound_ivr", "callback" }
        → 201 { local: {...pointer cols...}, vapi: {...full Vapi response...} }
        All non-association fields are forwarded to Vapi unchanged.

GET    /api/brands/{brand_id}/voice-ai/assistants                               list pointers + live Vapi config
        → [ { local: {...}, vapi: {...} | null }, ... ]
        One Vapi list call per request (no N+1). `vapi: null` indicates
        drift (the local pointer references an id Vapi doesn't have).

GET    /api/brands/{brand_id}/voice-ai/assistants/{assistant_id}                get pointer + live Vapi config
        → { local: {...}, vapi: {...} | null }

PATCH  /api/brands/{brand_id}/voice-ai/assistants/{assistant_id}                forward edit to Vapi
        body (Vapi-shape only, all optional):
            { name?, system_prompt?, first_message?, first_message_mode?,
              model_config_data?, voice_config?, transcriber_config?,
              tools_config?, analysis_config?, max_duration_seconds?, metadata? }
        → { local: {...}, vapi: {...} }
        Only `updated_at` changes locally. Brand association is managed
        via a separate endpoint (in development by another workstream —
        out of scope for this doc).

DELETE /api/brands/{brand_id}/voice-ai/assistants/{assistant_id}                delete on Vapi + soft-delete pointer
        → 204
        If Vapi already 404s the assistant (already gone): proceed.
        If Vapi 5xx: 503 raised, local row left intact for retry.

GET    /api/brands/{brand_id}/voice-ai/vapi/assistants                          Vapi's full account-level list (reconciliation)
```

**Local pointer shape** (the `local` sub-object on every response):

```
id, brand_id, partner_id, campaign_id,
assistant_type, vapi_assistant_id, status,
created_at, updated_at
```

That's it. No Vapi-shape fields are returned from the local row — to
read prompt / voice / tools / model config, look at the `vapi` sub-object.

**Config sub-structures** (Vapi-shape, all optional, freeform JSON
forwarded unchanged):
- `model_config_data` → Vapi `model` block (provider/model/temperature/etc).
   `system_prompt` is folded into `model.messages[0]` server-side.
- `voice_config` → Vapi `voice` block (provider, voiceId).
- `transcriber_config` → Vapi `transcriber` block (Deepgram/etc).
- `tools_config` → array of Vapi tool defs (or `toolIds`).
- `analysis_config` → Vapi `analysisPlan` (post-call extraction).

See VAPI_API_CANONICAL.md §3 for the inner shapes (large; not duplicated here).

### 4.6 Inbound routing — `app/routers/voice_inbound.py`

Local mirror of phone→assistant mapping (used by hq-x's brand-resolution
cascade in Vapi webhooks).

```
POST   /api/brands/{brand_id}/voice/inbound/phone-configs                       map phone to assistant locally
        body: { phone_number, voice_assistant_id, phone_number_sid?, partner_id?,
                routing_mode?, first_message_mode?, inbound_config?, is_active? }
GET    /api/brands/{brand_id}/voice/inbound/phone-configs                       list
GET    /api/brands/{brand_id}/voice/inbound/phone-configs/{config_id}           get one
PATCH  /api/brands/{brand_id}/voice/inbound/phone-configs/{config_id}           update
DELETE /api/brands/{brand_id}/voice/inbound/phone-configs/{config_id}           soft-delete
```

This is *separate* from `/vapi/phone-numbers/bind` — that one pushes the
binding to Vapi. This one is hq-x-internal only (used when Vapi's
`assistant-request` webhook needs hq-x to pick an assistant
dynamically).

### 4.7 Outbound calls — Twilio-orchestrated — `app/routers/outbound_calls.py`

Use this when **Twilio places the leg** (AMD detection, voicemail drops,
TwiML-driven flows). hq-x answers Twilio's TwiML callbacks.

```
POST   /api/brands/{brand_id}/outbound-calls                                    initiate via Twilio
        body: { to, from_number, greeting_text?, voicemail_text?,
                voicemail_audio_url?, human_message_text?, record?,
                timeout?, partner_id?, campaign_id?, campaign_lead_id?,
                amd_strategy?, vapi_assistant_id?, vapi_sip_uri? }
GET    /api/brands/{brand_id}/outbound-calls/{call_sid}                         get by Twilio SID
```

The TwiML callback endpoints (`/api/voice/outbound/twiml/...`) are
internal — hq-command never calls them.

### 4.8 Outbound calls — Vapi-orchestrated — `app/routers/vapi_calls.py`

Use this when **Vapi places the leg** (no AMD complexity; assistant is
the entire call). Requires `Idempotency-Key` header.

```
POST   /api/brands/{brand_id}/voice/calls                                       initiate via Vapi
        headers: Idempotency-Key: <uuid>  ← REQUIRED
        body: { assistant_id, voice_phone_number_id, customer_number,
                customer_name?, customer_external_id?, metadata?,
                partner_id?, campaign_id?, assistant_overrides? }
        → 201 { call_log, vapi_response, idempotent_replay }
GET    /api/brands/{brand_id}/voice/calls                                       list (vapi-mirror only)
        ?assistant_id=&campaign_id=&partner_id=&status=&limit=50&before=<iso8601>
GET    /api/brands/{brand_id}/voice/calls/{call_log_id}                         { local, vapi } combined view
PATCH  /api/brands/{brand_id}/voice/calls/{call_log_id}                         only `name` is updatable per Vapi spec
        body: { name }
POST   /api/brands/{brand_id}/voice/calls/{call_log_id}/end                     ⚠ DESTRUCTIVE
```

**`/end` is destructive on Vapi.** It calls `DELETE /call/{id}` on
Vapi, which removes the Vapi-side call record (transcript / recording
references die asynchronously). The local `call_logs` row is preserved
with `status='ended'`. Fetch transcript / cost via `GET ...{id}` first
if needed.

**Validation errors (400)** distinct error keys: `assistant_not_found_in_brand`,
`assistant_not_synced`, `phone_number_not_found_in_brand`,
`phone_number_not_imported_to_vapi`, `idempotency_key_required`.

### 4.9 Tools / squads / files / knowledge-bases — passthrough

All under `/api/brands/{brand_id}/vapi/{resource}`. Vapi-account-wide
under the hood (single-operator world); the `{brand_id}` scopes auth
only, not data.

```
POST   /vapi/tools                          create tool (any Vapi tool shape)
GET    /vapi/tools                          list (bare list)
GET    /vapi/tools/{tool_id}
PATCH  /vapi/tools/{tool_id}
DELETE /vapi/tools/{tool_id}

POST   /vapi/squads                         create squad
GET    /vapi/squads                         list (bare list)
GET    /vapi/squads/{squad_id}
PATCH  /vapi/squads/{squad_id}
DELETE /vapi/squads/{squad_id}

POST   /vapi/files                          ⚠ multipart/form-data, 25 MiB cap
                                             field name: file
GET    /vapi/files                          list (bare list)
GET    /vapi/files/{file_id}
PATCH  /vapi/files/{file_id}                only { name } per Vapi spec
DELETE /vapi/files/{file_id}

POST   /vapi/knowledge-bases                ⚠ custom-webhook KB pattern only
GET    /vapi/knowledge-bases
GET    /vapi/knowledge-bases/{kb_id}
PATCH  /vapi/knowledge-bases/{kb_id}
DELETE /vapi/knowledge-bases/{kb_id}
```

**Knowledge-base note:** the modern Vapi pattern is *file-based KBs*,
which is **NOT** this resource. To create a file-based KB:

1. `POST /vapi/files` — upload one or more files.
2. `POST /vapi/tools` with body
   ```json
   { "type": "query",
     "function": { "name": "..." },
     "knowledgeBases": [{ "provider": "google", "name": "...",
                          "description": "...", "fileIds": ["...", "..."] }] }
   ```
3. `PATCH /voice-ai/assistants/{id}` with `tools_config: [{...with toolId...}]`
   then `POST .../sync` to push.

The `/vapi/knowledge-bases` resource itself is for the older
*custom-webhook* KB pattern (Vapi POSTs back to your own server).
See [vapi_knowledge_bases.py](app/routers/vapi_knowledge_bases.py) module docstring.

### 4.10 Vapi campaigns (passthrough) — `app/routers/vapi_campaigns.py`

```
POST   /vapi/campaigns                      create
GET    /vapi/campaigns                      list (PAGINATED { results, metadata })
GET    /vapi/campaigns/{campaign_id}
PATCH  /vapi/campaigns/{campaign_id}
DELETE /vapi/campaigns/{campaign_id}
```

⚠ **hq-x's own campaign system is at §4.11.** Vapi campaigns let Vapi
manage CSV-upload-driven outbound campaigns end-to-end, but the canonical
guidance (`VAPI_API_CANONICAL.md` §19) is to use hq-x's own
`/voice/campaigns` for per-lead customization + scheduling. This
passthrough is exposed for completeness; default to §4.11.

### 4.11 hq-x voice campaigns — `app/routers/voice_campaigns.py`

```
POST   /api/brands/{brand_id}/voice/campaigns/{campaign_id}/config              configure a campaign for voice
GET    /api/brands/{brand_id}/voice/campaigns/{campaign_id}/config              get config
GET    /api/brands/{brand_id}/voice/campaigns/{campaign_id}/metrics             rollup metrics
```

The `campaign_id` references `business.campaigns.id` (a brand-scoped
campaign managed in hq-command's CRM surface — separate from this doc).

### 4.12 Analytics + insights — `app/routers/vapi_analytics.py`, `vapi_insights.py`

```
POST   /vapi/analytics/query                                                    one-shot Vapi analytics query
        body: passthrough — Vapi's analytics DSL

POST   /vapi/insights                                                           save a named insight (chart def)
GET    /vapi/insights                                                           PAGINATED { results, metadata }
GET    /vapi/insights/{insight_id}
PATCH  /vapi/insights/{insight_id}                                              send full body, not partial
DELETE /vapi/insights/{insight_id}
POST   /vapi/insights/preview                                                   ⚠ ASYNC — returns run-record stub
POST   /vapi/insights/{insight_id}/run                                          run a saved insight
        body (optional): { formatPlan: { format: "raw"|"recharts" }, timeRangeOverride? }
```

**`preview` is not synchronous.** It returns
`{id, insightId, orgId, createdAt, updatedAt}` — a *run record*, not
chart-ready rows. The frontend must poll the run record (or call
`/{id}/run`) to get rendered data. Don't render preview's response
directly as a chart.

**`update` is replacement, not partial.** Send the full insight body.
Empty body returns 400 `no fields to update`.

For `recharts`-shaped output: pass `{ "formatPlan": { "format": "recharts" } }`
on `/run`. The frontend can hand the result directly to a Recharts
component.

### 4.13 hq-x voice analytics — `app/routers/voice_analytics.py`

Local rollups computed from `call_logs`. Faster than Vapi's analytics
for hq-x-specific dashboards (per-campaign, per-partner).

```
GET    /api/brands/{brand_id}/analytics/voice/summary                           total calls, qualified rate, avg duration, cost
GET    /api/brands/{brand_id}/analytics/voice/by-campaign                       per-campaign rollup
GET    /api/brands/{brand_id}/analytics/voice/daily-trend                       last-N-days trend
GET    /api/brands/{brand_id}/analytics/voice/cost-breakdown                    cost per call type / partner
GET    /api/brands/{brand_id}/analytics/voice/transfer-rate                     transfer success rate
```

Read [voice_analytics.py](app/routers/voice_analytics.py) for query params + response shapes.

### 4.14 Voice sessions / transfer territories — `app/routers/voice.py`

Live-call dispositioning + transfer-rule CRUD.

```
GET    /api/brands/{brand_id}/voice/sessions                                    list call_logs (paginated)
GET    /api/brands/{brand_id}/voice/sessions/{call_sid}                         one call log
POST   /api/brands/{brand_id}/voice/sessions/{call_sid}/disposition             set outcome
POST   /api/brands/{brand_id}/voice/sessions/{call_sid}/action                  trigger transfer/etc.

POST   /api/brands/{brand_id}/voice/transfer-territories                        define transfer rules
GET    /api/brands/{brand_id}/voice/transfer-territories
GET    /api/brands/{brand_id}/voice/transfer-territories/{territory_id}
PATCH  /api/brands/{brand_id}/voice/transfer-territories/{territory_id}
DELETE /api/brands/{brand_id}/voice/transfer-territories/{territory_id}
```

`call_sid` here is the Twilio SID (for Twilio-driven calls); for
Vapi-driven calls use the Vapi-calls router (§4.8).

### 4.15 IVR config — `app/routers/ivr_config.py`

For Twilio-substrate IVR flows (DTMF menus). Vapi-substrate flows use
assistants instead.

```
POST   /api/brands/{brand_id}/ivr-config/flows                                  create a flow
GET    /api/brands/{brand_id}/ivr-config/flows
GET    /api/brands/{brand_id}/ivr-config/flows/{flow_id}                        with steps
PUT    /api/brands/{brand_id}/ivr-config/flows/{flow_id}                        replace
DELETE /api/brands/{brand_id}/ivr-config/flows/{flow_id}

POST   /api/brands/{brand_id}/ivr-config/flows/{flow_id}/steps                  add a step
PUT    /api/brands/{brand_id}/ivr-config/flows/{flow_id}/steps/{step_id}
DELETE /api/brands/{brand_id}/ivr-config/flows/{flow_id}/steps/{step_id}

POST   /api/brands/{brand_id}/ivr-config/phone-configs                          map a number to a flow
GET    /api/brands/{brand_id}/ivr-config/phone-configs
PUT    /api/brands/{brand_id}/ivr-config/phone-configs/{config_id}
DELETE /api/brands/{brand_id}/ivr-config/phone-configs/{config_id}

POST   /api/brands/{brand_id}/ivr-config/audio                                  upload TTS-prerendered audio
GET    /api/brands/{brand_id}/ivr-config/audio
DELETE /api/brands/{brand_id}/ivr-config/audio
```

### 4.16 SMS — `app/routers/sms.py`

```
POST   /api/brands/{brand_id}/sms                                               send SMS
        body: { to, body, from_number?, messaging_service_sid? }
GET    /api/brands/{brand_id}/sms                                               list messages
GET    /api/brands/{brand_id}/sms/{message_sid}                                 one message

POST   /api/brands/{brand_id}/sms/suppressions                                  add a suppression
GET    /api/brands/{brand_id}/sms/suppressions
DELETE /api/brands/{brand_id}/sms/suppressions/{phone_number}                   un-suppress
```

STOP keywords auto-suppress via the inbound-SMS webhook handler.

### 4.17 TwiML apps — `app/routers/twiml_apps.py`

```
POST   /api/brands/{brand_id}/twiml-apps                                        create a TwiML app on Twilio
GET    /api/brands/{brand_id}/twiml-apps
GET    /api/brands/{brand_id}/twiml-apps/{app_sid}
POST   /api/brands/{brand_id}/twiml-apps/{app_sid}                              update
DELETE /api/brands/{brand_id}/twiml-apps/{app_sid}
```

Required for Twilio Voice SDK / browser calling. Skip for pure Vapi flows.

---

## 5. Common workflows

Each recipe is a sequence the frontend will run for a real user gesture.

### 5.1 Stand up a new voice agent end-to-end

```
1. POST /admin/brands                                   create brand (or use existing brand_id)
2. PUT  /admin/brands/{id}/twilio-creds                 set Twilio creds
3. POST /api/brands/{id}/voice-ai/assistants            create on Vapi → vapi_assistant_id stamped on the new pointer
4. GET  /api/brands/{id}/phone-numbers/search?...       find a Twilio number
5. POST /api/brands/{id}/phone-numbers/purchase         buy it → voice_phone_number_id
6. POST /api/brands/{id}/vapi/phone-numbers/import      register in Vapi w/ assistant binding
                                                        body: { voice_phone_number_id, assistant_id: aid }
```

After step 6: inbound calls to that number reach Vapi → assistant → hq-x's
webhook ingress → call_logs row + analytics dual-write.

### 5.2 Make an outbound call (Vapi-orchestrated)

```
1. const idemKey = crypto.randomUUID();
2. POST /api/brands/{id}/voice/calls
   headers: { 'Idempotency-Key': idemKey }
   body:    { assistant_id, voice_phone_number_id, customer_number,
              customer_name?, metadata?, partner_id?, campaign_id?,
              assistant_overrides? }
3. → poll GET /api/brands/{id}/voice/calls/{call_log_id} for status updates
   OR listen for status changes via your own dashboard refresh
```

If step 2 returns 503: same `idemKey` is safe to retry (the ledger row
was rolled back). If it returns 502: do not retry; surface to user.

### 5.3 Update an assistant's prompt / voice / tools

```
1. PATCH /api/brands/{id}/voice-ai/assistants/{aid}     { system_prompt: "..." }  (or any subset of Vapi-shape fields)
```

PATCH forwards directly to Vapi's `PATCH /assistant/{vid}`. There is no
separate sync step — Vapi is the source of truth.

### 5.4 Wire a file-based knowledge base to an assistant

```
1. POST /api/brands/{id}/vapi/files                     multipart upload (per file)
                                                        → { id: "file_abc", ... }
2. POST /api/brands/{id}/vapi/tools                     create the query tool
        body: { type: "query", function: { name: "kb_lookup" },
                knowledgeBases: [{ provider: "google", name: "...",
                                   description: "...",
                                   fileIds: ["file_abc", "file_def"] }] }
        → { id: "tool_xyz", ... }
3. PATCH /api/brands/{id}/voice-ai/assistants/{aid}     { tools_config: [{ ...existing..., toolIds: ["tool_xyz"] }] }
```

PATCH forwards straight to Vapi — no separate sync step.

To swap the file set: re-upload (step 1), then PATCH the tool to point at the new fileIds.

### 5.5 Operator dashboard — analytics

For hq-x-local rollups (fast, shaped for hq-x's tables):

```
GET /api/brands/{id}/analytics/voice/summary
GET /api/brands/{id}/analytics/voice/by-campaign
GET /api/brands/{id}/analytics/voice/daily-trend
```

For Vapi-native dashboards (richer dimensions, async-ish):

```
1. POST /api/brands/{id}/vapi/insights                  save an insight definition
2. POST /api/brands/{id}/vapi/insights/{iid}/run        body: { formatPlan: { format: "recharts" } }
3. → render result as Recharts component
```

For ad-hoc one-shot queries: `POST /api/brands/{id}/vapi/analytics/query`.

### 5.6 Re-bind an existing number to a different assistant

```
PATCH /api/brands/{id}/vapi/phone-numbers/{vpn_id}/bind
      body: { assistant_id: <new_aid> }
```

This re-asserts the serverUrl on every call (idempotent), so it also
recovers from server-URL drift on Vapi's side.

### 5.7 Forcibly hang up a live call

```
1. (optional) GET /api/brands/{id}/voice/calls/{call_log_id}    fetch transcript / cost first
2. POST /api/brands/{id}/voice/calls/{call_log_id}/end
```

Step 2 is destructive on Vapi. Step 1 is your last chance to capture the
transcript.

---

## 6. State the frontend should track / cache

Stable / cache-friendly:

- **Brand list** (`GET /admin/brands`) — invalidate on brand CRUD.
- **Assistants** (`GET /voice-ai/assistants`) — every read passes through to Vapi, so cache TTL should be short (or skip caching). Invalidate on POST/PATCH/DELETE.
- **Phone numbers** (`GET /phone-numbers` + `GET /vapi/phone-numbers`) — invalidate on purchase/import/release/bind.
- **Inbound configs** (`GET /voice/inbound/phone-configs`) — invalidate on CRUD.
- **Saved insights** (`GET /vapi/insights`) — invalidate on insight CRUD.

Volatile / fetch on demand:

- **Call list** (`GET /voice/calls`) — paginated with `before` cursor; fetch a window per dashboard view.
- **Call detail** (`GET /voice/calls/{id}`) — fetch when the user clicks in.
- **Live Vapi views** (`/voice-ai/assistants/{id}`, `/voice-ai/assistants` list, `/vapi/phone-numbers/{id}`, `/voice/calls/{id}`) — every fetch hits Vapi; don't poll aggressively.
- **Insight runs** (`/vapi/insights/{id}/run`) — fetch on dashboard refresh / manual reload button.

Local rollups (`/analytics/voice/*`) are fast SQL — fine to poll on a 10–30s cadence.

---

## 7. Sharp edges (read before shipping)

1. **Vapi outage = no assistant reads.** `GET /voice-ai/assistants` and
   `GET /voice-ai/assistants/{id}` pass through to Vapi at request time;
   if Vapi is down, those endpoints raise 503. There is no local
   fallback for assistant config (Vapi is the source of truth). The
   frontend should surface the outage rather than show stale config.

2. **Idempotency-Key.** Required on `POST /voice/calls`. Generate
   client-side. Don't reuse across calls.

3. **`/end` is destructive.** Removes the Vapi-side call record. Always
   fetch transcript first if needed.

4. **`/vapi/insights/preview` is async-ish.** Returns a run-record stub,
   not chart data. Don't render the preview response directly.

5. **`/vapi/insights` PATCH is replacement.** Send the full insight
   body, not a partial.

6. **`/vapi/campaigns` paginates** as `{ results, metadata }`. So does
   `/vapi/insights`. All other Vapi `list_*` endpoints return bare lists.

7. **Vapi assistant config is freeform JSON forwarded to Vapi.**
   `model_config_data`, `voice_config`, `transcriber_config`,
   `tools_config`, `analysis_config` are forwarded to Vapi unchanged.
   Send the full Vapi-shaped sub-document; hq-x does not transform
   fields. `system_prompt` is the one convenience: it's folded into
   `model.messages[0]` server-side.

8. **PATCH semantics across the wrap are inconsistent.** Assistant
   PATCH forwards only the supplied Vapi-shape fields. Vapi insight
   PATCH is replacement. Vapi call PATCH only accepts `name`. Read
   the per-route docstring.

9. **Multipart file upload cap is 25 MiB.** Anything larger → 413
   `file_too_large`.

10. **Knowledge-base CRUD is the older custom-webhook pattern.** For
    file-based KBs use `/vapi/files` + `/vapi/tools` (recipe §5.4).

11. **Migration 0013 must be applied** to a Supabase before
    `POST /voice/calls` works against that DB. Dev / stg / prd each
    need it independently.

12. **There is no live WebSocket / SSE.** Status updates are polled.
    Real-time call state lives on Vapi (and gets persisted via the
    `vapi_webhooks` ingress); the frontend reads `call_logs` for status.

---

## 8. NOT for hq-command

hq-command should NOT call any of these. They exist for other consumers.

| Path | Consumer |
|---|---|
| `/api/v1/vapi/webhook` | Vapi's webhook ingress |
| `/api/webhooks/twilio/{brand_id}` | Twilio's webhook ingress |
| `/api/webhooks/twilio-trust-hub/{brand_id}` | Twilio Trust Hub callbacks |
| `/webhooks/cal`, `/webhooks/emailbison`, `/webhooks/lob` | external SaaS webhook receivers |
| `/api/voice/ivr/{brand_id}/...` | Twilio IVR webhook ingress |
| `/api/voice/outbound/twiml/...` | Twilio TwiML callbacks for outbound |
| `/internal/scheduler/...` | Trigger.dev scheduled tasks |
| `/internal/voice/callback/...` | Trigger.dev voice-callback runner |
| `/health` | infra health checks |
| `/direct-mail/...` | a separate product (Lob direct mail) |

Calling these from the frontend either won't work (signature checks /
shared-secret auth) or will produce wrong behavior.

---

## 9. Migrations checklist (operator action, not API)

The frontend doesn't apply migrations, but the user managing hq-x needs
to know these are required. As of `f90dcdc`:

- All voice migrations: `0005_voice_base.sql` through `0013_vapi_outbound_idempotency.sql`.
- The new `vapi_call_idempotency` table (migration `0013`) is required for `POST /voice/calls`.

Apply via `scripts/migrate.py` against each environment's Supabase.

---

## 10. Source-of-truth file map

When this doc is too high-level, read source:

| Topic | File |
|---|---|
| Auth | `app/auth/flexible.py`, `app/auth/supabase_jwt.py` |
| Settings (env vars) | `app/config.py` |
| Vapi client wrapper | `app/providers/vapi/client.py` |
| Vapi error mapping | `app/providers/vapi/errors.py`, `app/providers/vapi/_http.py` |
| Voice tables | `migrations/0005_voice_base.sql` |
| Idempotency table | `migrations/0013_vapi_outbound_idempotency.sql` |
| Vapi spec reference | `/Users/benjamincrane/api-reference-docs-new/vapi/VAPI_API_CANONICAL.md` |
| Vapi per-endpoint shapes | `/Users/benjamincrane/api-reference-docs-new/vapi/api-reference/{resource}/{verb}.md` |
| Project conventions | `ARCHITECTURE.md` |
| Adversarial review notes | `REVIEW_FINDINGS.md` |

For any router, the request/response models are at the top of the file
and the routes follow. Always source-of-truth your shape from there.
