# Entri Integration Spec — Custom Domains for DMaaS Lead Magnets

**Status:** research / pre-implementation
**Audience:** the agent who will build the API endpoints + frontend wiring
**Source docs:** https://developers.entri.com/llms.txt (full index), pages cited inline.

## 1. What we are solving

DMaaS direct mailers carry **QR codes** and **printed website links**. Those targets need
to live on a domain the *customer* controls (their brand, their lead magnet) but be
**served by our infrastructure** (so we can do landing-page rendering, attribution,
form capture, A/B, retargeting pixels, etc).

That requires two things from the customer's side:

1. **DNS records** on their domain (e.g. `qr.acme.com` CNAME → us, or apex `acme.com` A
   → us). They don't want to hand-edit records at GoDaddy/Cloudflare/Squarespace.
2. **TLS certs** for that hostname so the browser doesn't warn on QR-code clicks.

[Entri](https://www.entri.com) productizes this exact problem. There are three relevant
products:

| Entri product | What it gives us |
|---|---|
| **Entri Connect** | A modal where the customer logs into their DNS provider once and Entri writes the records for them (the "OAuth-like" flow you described). Falls back to manual DNS instructions if their registrar isn't supported. |
| **Entri Power** | A managed reverse proxy at `power.goentri.com` — customer points their domain at it, we register `domain → applicationUrl` mappings via Entri's REST API, and Entri proxies the request to us. **This is the reverse-proxy piece.** |
| **Entri Secure** | Auto-provisioned Let's Encrypt certs for those custom hostnames (3–7s typical). Bundled with Power. |

Our integration is **Connect + Power + Secure**, which is the standard SaaS-custom-domain
package.

References: [Entri Power](https://developers.entri.com/power.md),
[SSL provisioning](https://developers.entri.com/ssl-provisioning.md),
[product page](https://www.entri.com/products/connect).

## 2. End-to-end flow we are building

```
┌──────────┐     ┌────────────────┐     ┌─────────────┐     ┌──────────────┐
│ Customer │────▶│ DMaaS frontend │────▶│ Our backend │────▶│ Entri API     │
│ (browser)│     │  (Next.js)     │     │  (FastAPI)  │     │ (api.goentri) │
└──────────┘     └────────────────┘     └─────────────┘     └──────────────┘
     │                  │                      │                    │
     │  1. enter domain │                      │                    │
     │─────────────────▶│                      │                    │
     │                  │  2. POST /entri/session                    │
     │                  │─────────────────────▶│                    │
     │                  │                      │  3. POST /token    │
     │                  │                      │───────────────────▶│
     │                  │                      │◀───────────────────│ JWT (60min)
     │                  │  4. {token, applicationId, dnsRecords[]}  │
     │                  │◀─────────────────────│                    │
     │  5. entri.showEntri(...)                │                    │
     │◀─────────────────│                      │                    │
     │                                                              │
     │  6. customer authenticates into their DNS provider in Entri modal
     │─────────────────────────────────────────────────────────────▶│
     │                                                              │ writes records
     │                                                              │ provisions cert
     │                  7. onSuccess event ────▶ POST /entri/success│
     │                  │                      │                    │
     │                                       8. PUT /power (register reverse-proxy mapping)
     │                                         │───────────────────▶│
     │                                                              │
     │       9. webhook: domain.added / power_status:success        │
     │                                         │◀───────────────────│
     │                                         │ flip domain → live │
     │                                                              │
     │  10. customer's QR code resolves: acme.com → power.goentri.com → our app
```

Key insight: **the JWT, the `applicationId`, and the modal config are owned by our
backend. The frontend just calls our endpoint and renders the Entri SDK.** We never
ship the Entri `secret` to the browser.

## 3. Credentials & accounts (one-time, not per-customer)

We have **one** Entri partner account. All customer domains live under it.

1. Create org in https://dashboard.entri.com.
2. Create an "application" in the dashboard for each environment we want to isolate
   (recommend: `hq-x-dev`, `hq-x-staging`, `hq-x-prod`). The dashboard issues:
   - `applicationId` — public, can ship to frontend
   - `secret` — **server-only**, store in Doppler as `ENTRI_SECRET`
   - Configure `cname_target` (e.g. `domains.dmaas.ourcompany.com`) and create a CNAME
     `domains.dmaas.ourcompany.com → power.goentri.com` in our own DNS.
   - Configure the webhook URL → our `/api/entri/webhooks` endpoint (per env).
   - Copy the **client secret** used to sign webhooks → Doppler as
     `ENTRI_WEBHOOK_SECRET` (this is separate from the token-minting secret; both come
     off the dashboard but used in different verifiers).

Doppler config additions (`hq-x` project):

| Key | Value | Used by |
|---|---|---|
| `ENTRI_APPLICATION_ID` | from dashboard | backend + frontend |
| `ENTRI_SECRET` | from dashboard | backend only (token mint) |
| `ENTRI_WEBHOOK_SECRET` | from dashboard | backend only (webhook verify) |
| `ENTRI_CNAME_TARGET` | e.g. `domains.dmaas.ourcompany.com` | backend (build dnsRecords) |
| `ENTRI_API_BASE` | `https://api.goentri.com` | backend |

## 4. Our REST API surface (to be built)

All endpoints live under `/api/entri/*`. Auth is our normal Supabase JWT — they map
to a `customer_id` (and optionally a specific `mailer_campaign_id`).

### 4.1 `POST /api/entri/session`

Purpose: mint a fresh Entri JWT for a customer that's about to open the modal, and
return everything the frontend needs.

**Request:**
```json
{
  "domain": "acme.com",
  "subdomain": "qr",
  "campaign_id": "uuid",
  "use_root_domain": false
}
```

**Response:**
```json
{
  "applicationId": "<ENTRI_APPLICATION_ID>",
  "token": "<60-min JWT from Entri>",
  "dnsRecords": [...see §5...],
  "userId": "<customer_id>:<campaign_id>",
  "applicationUrl": "https://app.dmaas.ourcompany.com/lp/<campaign_id>",
  "prefilledDomain": "acme.com",
  "defaultSubdomain": "qr",
  "power": true,
  "secureRootDomain": false
}
```

**What it does (server-side):**
1. Validate the customer owns the campaign.
2. Build `dnsRecords` for this campaign (see §5).
3. Call Entri to mint a JWT:
   ```
   POST https://api.goentri.com/token
   Content-Type: application/json
   { "applicationId": ENTRI_APPLICATION_ID, "secret": ENTRI_SECRET }
   ```
   Response: `{ "auth_token": "..." }` — that string is the JWT.
4. Persist a `domain_connection` row (state: `pending_modal`, `expires_at = now+60m`).
5. Return the bundle to the frontend.

The token expires after **60 minutes** ([getting-started](https://developers.entri.com/getting-started.md)),
so do not cache across users; mint per session. We *can* cache for retries inside the
same session — store the JWT in the `domain_connection` row.

### 4.2 `POST /api/entri/success`

Called by the frontend from the `onSuccess` event listener. The webhook is the
source of truth, but this gives us an immediate UI flip and a place to register the
Power mapping eagerly.

**Request:**
```json
{
  "session_id": "<our domain_connection id>",
  "domain": "qr.acme.com",
  "setupType": "automatic",
  "provider": "godaddy",
  "jobId": "..."
}
```

**Server work:**
1. Update `domain_connection.state = dns_records_submitted`.
2. Call Entri Power to register the reverse-proxy mapping (idempotent):
   ```
   PUT https://api.goentri.com/power
   Authorization: <JWT>
   applicationId: <ENTRI_APPLICATION_ID>
   Content-Type: application/json
   {
     "domain": "qr.acme.com",
     "applicationUrl": "https://app.dmaas.ourcompany.com/lp/<campaign_id>",
     "powerRootPathAccess": ["/static/", "/_next/", "/favicon.ico"]
   }
   ```
3. Return `{ ok: true }`.

We could *also* skip the modal entirely for Power if the customer already added the
CNAME manually — see §6.2.

### 4.3 `POST /api/entri/webhooks`

Single endpoint that receives all event types from Entri. **No auth header from us;
verify via signature.**

Event types we care about (from [webhooks](https://developers.entri.com/webhooks.md)):

| Event | What we do |
|---|---|
| `domain.added` | DNS records propagated. Mark `domain_connection.state = live` if `power_status = success` and `secure_status = success`. |
| `domain.flow.completed` | User finished modal flow. Telemetry only. |
| `domain.propagation.timeout` | 72h with no propagation. State → `failed`, notify customer to fix DNS manually. |
| `domain.record_missing` | Monitor: customer broke their own DNS. Page-level alert + email. |
| `domain.record_restored` | Recovery. Clear alert. |
| `purchase.error`, `purchase.confirmation.expired` | Only relevant if we ever sell domains via Entri (out of scope v1). |

**Verification (V2 signature, recommended):**

Headers Entri sends:
- `Entri-Signature-V2`
- `Entri-Timestamp`
- `Entri-Signature` (V1 legacy — ignore once V2 is wired)

Algorithm:
```python
expected = hashlib.sha256(
    payload["id"].encode()
    + request.headers["Entri-Timestamp"].encode()
    + ENTRI_WEBHOOK_SECRET.encode()
).hexdigest()
hmac.compare_digest(expected, request.headers["Entri-Signature-V2"])
```

Plus: reject if `abs(now - Entri-Timestamp) > 5 minutes` (replay window).

Optional hardening: allowlist source IP `3.14.77.245` (Entri prod IP).

Retry behavior on Entri's side: up to 3 retries on non-2xx. So we **must** be
idempotent — key on `payload.id` (and store dedupe rows for ~7 days).

### 4.4 `DELETE /api/entri/domains/{domain}`

Customer disconnects a domain.

```
DELETE https://api.goentri.com/power
Authorization: <fresh JWT>
{ "domain": "qr.acme.com" }
```

Then mark our row `state = disconnected`. We can't force their DNS records away —
they still need to remove the CNAME on their side.

### 4.5 `GET /api/entri/eligibility?domain=...`

Thin proxy over Entri's eligibility check, used by the frontend before showing the
modal to detect "customer already added the CNAME" cases:
```
GET https://api.goentri.com/power?domain=qr.acme.com&rootDomain=false
Authorization: <JWT>
applicationId: <ENTRI_APPLICATION_ID>
```
Returns `{ eligible: true|false }`.

## 5. Building `dnsRecords`

Entri uses template variables: `{DOMAIN}`, `{SUBDOMAIN}`, `{SLD}`, `{TLD}`,
`{ENTRI_SERVERS}`, `{CNAME_TARGET}`. We can mix literal strings and templates.

For our DMaaS case, two scenarios:

### 5.1 Subdomain (default, e.g. `qr.acme.com`)

```json
[
  {
    "type": "CNAME",
    "host": "{SUBDOMAIN}",
    "value": "{CNAME_TARGET}",
    "ttl": 300,
    "applicationUrl": "https://app.dmaas.ourcompany.com/lp/<campaign_id>"
  }
]
```

`{CNAME_TARGET}` resolves to whatever we set in the Entri dashboard
(`ENTRI_CNAME_TARGET`).

### 5.2 Root domain (e.g. customer wants `acme.com` itself)

CNAME at apex is illegal on most registrars, so use Entri's anycast IPs:

```json
[
  {
    "type": "A",
    "host": "@",
    "value": "{ENTRI_SERVERS}",
    "ttl": 300,
    "applicationUrl": "https://app.dmaas.ourcompany.com/lp/<campaign_id>"
  }
]
```

Set `secureRootDomain: true` in `showEntri` so we get a cert for the apex
([SSL provisioning](https://developers.entri.com/ssl-provisioning.md)).

### 5.3 Conditional records (subdomain-or-root in one modal)

If we want one Entri session to handle either case:

```json
{
  "subDomain": [ { "type": "CNAME", "host": "{SUBDOMAIN}", "value": "{CNAME_TARGET}", "ttl": 300, "applicationUrl": "..." } ],
  "domain":    [ { "type": "A",     "host": "@",          "value": "{ENTRI_SERVERS}", "ttl": 300, "applicationUrl": "..." } ]
}
```

## 6. Frontend integration

### 6.1 SDK include

```html
<script src="https://cdn.goentri.com/entri.js"></script>
```

(Or load dynamically in a Next.js client component — `entri.js` exports
`window.entri.showEntri`.)

### 6.2 Open the modal

```ts
async function connectDomain(campaignId: string, domain: string) {
  const session = await fetch('/api/entri/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ campaign_id: campaignId, domain })
  }).then(r => r.json());

  window.addEventListener('onSuccess', (e: any) => {
    fetch('/api/entri/success', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: session.session_id, ...e.detail })
    });
  }, { once: true });

  window.addEventListener('onEntriClose', (e: any) => {
    // { success, domain, error, lastStatus }
  }, { once: true });

  window.entri.showEntri(session);
}
```

Events we get (from [vanilla-js](https://developers.entri.com/integration/vanilla-js.md)):
- `onSuccess` → `{ domain, setupType, provider, jobId }`
- `onEntriClose` → `{ success, domain, error, lastStatus }`
- `onEntriStepChange` → `{ step, domain, provider }` (telemetry / progress UI)

**Register listeners BEFORE calling `showEntri`** — they're `window` events, not
modal callbacks.

### 6.3 Useful `showEntri` config knobs

(Full table: [api-reference](https://developers.entri.com/api-reference.md).)

- `userId: "<customer_id>:<campaign_id>"` — echoes into every webhook payload's
  `user_id` field. Use this to route webhooks back to the right campaign.
- `prefilledDomain: "acme.com"` — skips the "what's your domain" screen.
- `defaultSubdomain: "qr"` — pre-fills the subdomain field.
- `forceManualSetup: false` — let Entri auto-detect; only force manual if the
  customer's provider is unsupported.
- `locale: "en"` — supports en/es/pt/fr/de/ja/etc.
- `power: true` — required to enable Power.
- `secureRootDomain: true` — only set for apex-domain flows.
- `whiteLabel: { ... }` — Premium+ tier; theme/logo/colors/copy. Out of scope v1.

## 7. Data model (suggested)

One new table, lives in our app db:

```sql
create table domain_connections (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid not null references customers(id),
  campaign_id uuid not null references mailer_campaigns(id),
  domain text not null,                     -- "qr.acme.com"
  is_root_domain boolean not null default false,
  application_url text not null,            -- where Entri proxies to
  state text not null,                      -- pending_modal | dns_records_submitted | live | failed | disconnected
  entri_user_id text not null,              -- "<customer_id>:<campaign_id>" — webhook correlation
  entri_token text,                         -- last minted JWT (so retries reuse)
  entri_token_expires_at timestamptz,
  provider text,                            -- "godaddy" | "cloudflare" | ... from webhook
  setup_type text,                          -- "automatic" | "manual" | "sharedLogin"
  power_status text,                        -- mirrors webhook
  secure_status text,
  propagation_status text,
  last_webhook_id text,                     -- dedupe
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index on domain_connections (domain) where state in ('pending_modal','dns_records_submitted','live');
create index on domain_connections (customer_id, campaign_id);

create table entri_webhook_events (
  id text primary key,                      -- payload.id from Entri
  type text not null,
  user_id text,
  domain text,
  payload jsonb not null,
  received_at timestamptz not null default now()
);
```

The unique index on `domain` (partial, excluding `disconnected/failed`) prevents
two campaigns from claiming the same hostname simultaneously.

## 8. Headers Entri's reverse proxy injects

When a request comes through Power to our origin
(`applicationUrl`), Entri appends ([power](https://developers.entri.com/power.md)):

- `X-Forwarded-Host` — the customer's domain (`qr.acme.com`)
- `x-entri-forwarded-host` — same, lowercase, Entri-namespaced
- `X-Forwarded-IP` — visitor IP
- `X-Entri-Auth` — proves the request came from Entri (verify against a shared
  secret if Entri exposes one — confirm with their support; doc doesn't spell it out)

Our landing-page app (`app.dmaas.ourcompany.com`) needs to read
`x-entri-forwarded-host` to know which customer's branding/landing page to render.

`powerRootPathAccess: ["/static/", "/_next/", ...]` is how we whitelist asset paths
that should be served as-is rather than rewritten — make sure Next.js asset prefixes
are listed (typically `/_next/`, `/static/`, `/favicon.ico`, plus any custom asset
roots).

## 9. Limits & failure modes

- **JWT TTL: 60 min.** Mint per-session.
- **Power re-provisioning: 5 attempts per domain per 24h.** Don't loop retries.
- **Cert provisioning: 3–7s typical, can be longer.** Don't block UI on it; rely on
  the webhook flip.
- **DNS propagation: up to 72h.** After 72h with no propagation Entri fires
  `domain.propagation.timeout` — surface this to the customer in our dashboard.
- **Webhook retries: 3 attempts.** After that, Entri sends 1 warning email/day to
  account admins. Make our endpoint return 2xx fast; do heavy work async.
- **Webhook delivery:** only to dashboard users with `Admin`/`Developer` roles
  (account-config concern, not code).
- **CNAME at apex:** not allowed. Force the A-record path for root domains.

## 10. Phasing

**v1 (MVP — what to build first):**
- Doppler keys
- `POST /api/entri/session` (subdomain only, no root)
- `POST /api/entri/webhooks` with V2 signature verify + dedupe
- `domain_connections` table + `entri_webhook_events` table
- Frontend: vanilla `entri.showEntri` integration, `onSuccess` → `POST /api/entri/success`
- Origin app reads `x-entri-forwarded-host` to resolve campaign

**v2:**
- Root-domain support (`secureRootDomain: true`, A-record dnsRecords)
- `DELETE /api/entri/domains/{domain}`
- Eligibility pre-check (`GET /power`)
- Monitor alerts (`domain.record_missing` → email + UI banner)

**v3:**
- White-label modal (Premium+ tier — separate Entri contract)
- Domain purchasing inside our app (`domain.purchased` webhook +
  [domain-purchasing](https://developers.entri.com/domain-purchasing.md))

## 11. Open questions for whoever builds this

1. **`X-Entri-Auth` shared secret** — confirm with Entri support how to verify it
   (the doc references the header but not its format). Without verification, anyone
   who knows our `applicationUrl` can hit it directly and bypass per-domain logic.
2. **Tiering:** Power and Secure require a paid Entri plan. Confirm the dashboard
   has Power/Secure enabled before relying on it in code.
3. **`applicationUrl` granularity:** can we register *one* `applicationUrl` per
   customer domain that includes a path (`/lp/<campaign_id>`), or do we need to
   register the bare origin and route by host header on our side? Doc shows path
   examples (`https://app.saascompany.com/page/123`) so per-path should work.
4. **Multiple campaigns, one customer domain:** if Acme wants `qr1.acme.com`,
   `qr2.acme.com`, `lp.acme.com` each pointing to different campaigns, that's
   N separate Entri Power registrations. Confirm rate limits don't bite during
   bulk onboarding.

## 12. Reference URLs (cited above)

- [Entri docs index (llms.txt)](https://developers.entri.com/llms.txt)
- [Getting started](https://developers.entri.com/getting-started.md) — credentials, JWT mint
- [API reference](https://developers.entri.com/api-reference.md) — `showEntri` config
- [Power](https://developers.entri.com/power.md) — reverse proxy REST API
- [SSL provisioning](https://developers.entri.com/ssl-provisioning.md) — Entri Secure
- [Webhooks](https://developers.entri.com/webhooks.md) — events, V2 signing
- [Vanilla JS integration](https://developers.entri.com/integration/vanilla-js.md)
- [DNS providers](https://developers.entri.com/integrate-with-dns-providers.md)
- [Provider list](https://developers.entri.com/provider-list.md)
- [DNS concepts](https://developers.entri.com/dns-concepts.md)
- Marketing: [Connect](https://www.entri.com/products/connect),
  [Power](https://www.entri.com/products/power)
