# Entri integration runbook

How the Entri custom-domain integration in this repo actually works, and
what to do when it's time to turn it on for a real customer. Companion to
[entri-integration.md](entri-integration.md), which is the design-time
research doc — this one is the operator/agent-facing reference.

## What it does

Lets a DMaaS customer point a domain they own (e.g. `qr.acme.com`, or the
apex `acme.com`) at our infrastructure, so QR codes on direct mailers and
printed website links resolve to landing pages we serve. Entri provides
three things:

1. **Connect** — a modal where the customer authenticates into their DNS
   provider (GoDaddy, Cloudflare, Squarespace, …) and Entri writes the
   records for them. Falls back to manual instructions if their registrar
   isn't supported.
2. **Power** — a managed reverse proxy at `power.goentri.com`. We register
   `{customer_domain → our_origin_url}` mappings via REST. Entri proxies
   inbound traffic to our app and injects an `x-entri-forwarded-host`
   header so we can resolve the request to the right campaign.
3. **Secure** — auto-provisioned Let's Encrypt certs for those custom
   hostnames, ~3–7 seconds.

## Current state

**Built and merged. Inert.** All endpoints return 503 `entri_not_configured`
until real Entri credentials replace the placeholder `test` values in
Doppler. No live traffic is possible until then.

The frontend component (the bit that calls `window.entri.showEntri(...)`)
is **not built** — that lives in `outbound-engine-x-frontend` and is the
next piece of work.

## File map

```
app/config.py                            # Entri env-var settings
app/providers/entri/client.py            # HTTP client (httpx + retries)
app/webhooks/entri_signature.py          # HMAC-SHA256 V2 verifier
app/webhooks/entri_processor.py          # Webhook → state-machine projector
app/dmaas/entri_domains.py               # entri_domain_connections repository
app/models/entri.py                      # Pydantic request/response shapes
app/routers/entri.py                     # /api/v1/entri/* REST endpoints
app/routers/webhooks/entri.py            # POST /webhooks/entri receiver
migrations/20260429T180000_entri_domain_connections.sql
tests/test_entri_{client,signature,processor,webhook,router}.py
docs/entri-integration.md                # design research / decisions
docs/entri-runbook.md                    # this file
```

## Doppler keys

All required (no defaults in code). Pre-signup placeholder values:

| Key | Pre-signup value | Real value |
|---|---|---|
| `ENTRI_APPLICATION_ID` | `test` | from dashboard.entri.com |
| `ENTRI_SECRET` | `test` | from dashboard.entri.com |
| `ENTRI_WEBHOOK_SECRET` | `test` | from dashboard.entri.com |
| `ENTRI_CNAME_TARGET` | `test` | hostname *we* own that CNAMEs to `power.goentri.com` |
| `ENTRI_APPLICATION_URL_BASE` | `test` | origin we proxy to, e.g. `https://app.dmaas.ourcompany.com` |
| `ENTRI_API_BASE` | `https://api.goentri.com` | same |
| `ENTRI_WEBHOOK_SIGNATURE_MODE` | `disabled` | `enforce` (prd) |
| `ENTRI_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` | `300` | `300` |

`ENTRI_API_BASE`, `ENTRI_WEBHOOK_SIGNATURE_MODE`, and the tolerance are
typed so they can't take literal `"test"` — use the values shown.

In production with real credentials, `app.config.assert_production_safe()`
refuses to boot if `ENTRI_APPLICATION_ID` is set without `ENTRI_SECRET`,
`ENTRI_WEBHOOK_SECRET`, or with `ENTRI_WEBHOOK_SIGNATURE_MODE != enforce`.

## Endpoints we expose

All under `/api/v1/entri/*`. Auth is the standard Supabase JWT + active
organization context (`X-Organization-Id` header). Every endpoint returns
503 `entri_not_configured` while Doppler holds placeholder values — the
gating check is in `_ensure_configured()` at [app/routers/entri.py:46](../app/routers/entri.py).

### `POST /api/v1/entri/session`

Mints a 60-min Entri JWT and returns the bundle the frontend hands to
`window.entri.showEntri(...)`. Persists a `pending_modal` row in
`business.entri_domain_connections`.

Request:
```json
{
  "domain": "acme.com",
  "subdomain": "qr",
  "channel_campaign_step_id": "<uuid>",
  "use_root_domain": false,
  "application_url_path": null
}
```

Response keys (camelCase deliberately — pass straight to the SDK):
`session_id`, `applicationId`, `token`, `dnsRecords`, `userId`,
`applicationUrl`, `prefilledDomain`, `defaultSubdomain`, `power`,
`secureRootDomain`.

`userId` is `"<organization_id>:<step_id>"` — Entri echoes it on every
webhook payload as the `user_id` field. Sole correlation key.

### `POST /api/v1/entri/success`

Frontend calls this from its `onSuccess` listener. We mark the row
`dns_records_submitted` and idempotently re-register the Power mapping
(`PUT /power`).

Request:
```json
{
  "session_id": "<uuid from /session>",
  "domain": "qr.acme.com",
  "setup_type": "automatic",
  "provider": "godaddy",
  "job_id": "..."
}
```

### `GET /api/v1/entri/eligibility?domain=qr.acme.com`

Pre-modal check: has the customer added the CNAME / A record yet?
Mints a fresh JWT, calls `GET /power` upstream, returns `{eligible: bool}`.

### `GET /api/v1/entri/domains` and `GET /api/v1/entri/domains/{id}`

List / fetch connections for the active organization.

### `DELETE /api/v1/entri/domains/{id}`

Disconnect: calls Entri's `DELETE /power` and marks the row `disconnected`.
A 404 from Entri (mapping already gone) is treated as success.

### `POST /webhooks/entri`

Inbound from Entri. Flow:

1. Parse JSON body.
2. Verify V2 signature: `SHA256(payload.id + Entri-Timestamp + ENTRI_WEBHOOK_SECRET) == Entri-Signature-V2`, with a ±300s replay window.
3. Validate schema (`id` and `type` required).
4. Insert into `webhook_events` (provider_slug=`entri`, dedupe on `event_key`).
5. Project into `entri_domain_connections` via the state machine in [app/webhooks/entri_processor.py](../app/webhooks/entri_processor.py).
6. Mark the row `processed` or `dead_letter`.

Returns 202 even on projection failure (dead-lettered events stay in
`webhook_events` for replay). Entri retries up to 3 times on non-2xx, so
keep this fast.

## State machine

`business.entri_domain_connections.state` transitions:

```
pending_modal       --(domain.flow.completed | /success)-->  dns_records_submitted
dns_records_submitted --(domain.added, power=success, secure=success)--> live
*                   --(domain.propagation.timeout)-->  failed
live                --(domain.record_missing)-->        failed
failed              --(domain.record_restored)-->       live
*                   --(DELETE /domains/{id})-->         disconnected
```

Other event types (`domain.purchased`, `domain.transfer.*`,
`purchase.*`) are persisted to `webhook_events` but don't change state.

## DNS records the modal will write

Built by `_build_dns_records()` in [app/routers/entri.py](../app/routers/entri.py).

**Subdomain flow** (default): one CNAME pointing to `ENTRI_CNAME_TARGET`.

```json
[{
  "type": "CNAME",
  "host": "{SUBDOMAIN}",
  "value": "<ENTRI_CNAME_TARGET>",
  "ttl": 300,
  "applicationUrl": "<ENTRI_APPLICATION_URL_BASE>/lp/<step_id>"
}]
```

**Root-domain flow** (`use_root_domain: true`): A record using Entri's
anycast IPs (CNAME-at-apex is illegal on most registrars).

```json
[{
  "type": "A",
  "host": "@",
  "value": "{ENTRI_SERVERS}",
  "ttl": 300,
  "applicationUrl": "<ENTRI_APPLICATION_URL_BASE>/lp/<step_id>"
}]
```

`{SUBDOMAIN}`, `{ENTRI_SERVERS}`, `{CNAME_TARGET}` are template variables
that Entri's modal substitutes at runtime.

## Headers Entri injects on proxied requests

Our origin (`ENTRI_APPLICATION_URL_BASE`) sees these on every inbound:

- `X-Forwarded-Host` — the customer's domain (`qr.acme.com`)
- `x-entri-forwarded-host` — same, lowercase, Entri-namespaced
- `X-Forwarded-IP` — visitor IP
- `X-Entri-Auth` — claimed proof-of-Entri (verification format not
  documented; confirm with Entri support before relying on it)

The origin app reads `x-entri-forwarded-host`, looks up the row via
`entri_domains.get_by_domain(host)`, and renders the corresponding
campaign's landing page. **This origin middleware is not built yet** —
add it in whichever app actually serves landing pages.

## Limits and gotchas

- **JWT TTL: 60 minutes.** Mint per session, don't cache globally.
- **Power re-provisioning: max 5 per domain per 24h.** Don't loop retries.
- **Cert provisioning: 3–7s typical.** Don't block UI; rely on the webhook.
- **DNS propagation: up to 72h.** After that Entri fires
  `domain.propagation.timeout`. Surface this to the customer.
- **Webhook delivery: 3 retries on non-2xx, then 1 warning email/day.**
  Endpoint must return 2xx fast; heavy work goes async.
- **Webhook role gate:** Entri only delivers webhooks to dashboard users
  with Admin/Developer role. Account-config concern, not code.
- **Apex CNAMEs are illegal** at most registrars. Always use the A-record
  path for root domains.

## Day-zero turn-on (post-Entri-signup)

1. Sign up at https://dashboard.entri.com. Confirm Power + Secure are on
   your plan ($250/mo tier).
2. In the dashboard, create one application per env (dev/stg/prd). Copy
   `applicationId`, `secret`, and webhook secret for each.
3. Pick a `cname_target` you own (e.g. `domains.dmaas.ourcompany.com`).
   Create a `CNAME` record in *our* DNS pointing it to `power.goentri.com`.
   Register the same hostname in the Entri dashboard.
4. Set the dashboard's webhook URL to `https://<our-api>/webhooks/entri`
   for each env.
5. Replace the `test` placeholders in Doppler with real values:
   ```
   ENTRI_APPLICATION_ID=<from dashboard>
   ENTRI_SECRET=<from dashboard>
   ENTRI_WEBHOOK_SECRET=<from dashboard>
   ENTRI_CNAME_TARGET=domains.dmaas.ourcompany.com
   ENTRI_APPLICATION_URL_BASE=https://app.dmaas.ourcompany.com
   ENTRI_WEBHOOK_SIGNATURE_MODE=enforce         # in prd
   ```
6. Apply the migration if not already:
   ```
   doppler --project hq-x --config <env> run -- uv run python -m scripts.migrate
   ```
7. Build the frontend modal component (vanilla JS or React) per
   [entri-integration.md §6](entri-integration.md). Source it from
   `https://cdn.goentri.com/entri.js`.
8. Build the origin middleware (reads `x-entri-forwarded-host` →
   `entri_domains.get_by_domain` → campaign → render).
9. Smoke test: hit `/api/v1/entri/session` end-to-end with a test domain
   you own. Verify the webhook lands and the row flips to `live`.

## Open questions to resolve with Entri support

These are documented in the design doc but worth re-flagging:

1. **`X-Entri-Auth` verification format.** Entri injects this header but
   docs don't spell out how to verify it. Without verification, anyone
   who knows our `applicationUrl` can hit it directly and bypass our
   per-domain logic.
2. **`applicationUrl` path granularity.** Doc examples show paths
   (`/page/123`) but it isn't explicit whether per-path mappings work or
   only origin-level. We assume per-path; confirm before scale.

## How tests fit together

| File | Coverage |
|---|---|
| `test_entri_client.py` | HTTP shape, retry/backoff, error categorization |
| `test_entri_signature.py` (in test_entri_webhook.py) | V2 HMAC, replay window, mode behaviors |
| `test_entri_processor.py` | State-machine transitions for every event type we handle |
| `test_entri_webhook.py` | Webhook receiver: signature → store → project → mark, dedupe, dead-letter |
| `test_entri_router.py` | 503 gating, auth, end-to-end session→success→list→delete |

All offline (mocked HTTP, in-memory repo). Run:
```
doppler --project hq-x --config dev run -- uv run pytest tests/test_entri_*
```
