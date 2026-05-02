# hq-x Tenancy State

## 1. Auth model

- **Identity provider:** Supabase Auth (ES256 JWT, verified via JWKS).
- **JWKS endpoint:** `{HQX_SUPABASE_URL}/auth/v1/.well-known/jwks.json`.
- **Algorithm:** ES256. **Audience:** `authenticated`.
- **Token transport:** `Authorization: Bearer <jwt>`.
- **Request → user resolution:** FastAPI dependency `verify_supabase_jwt` ([app/auth/supabase_jwt.py:105](app/auth/supabase_jwt.py:105)).
  1. Extract Bearer token.
  2. Validate signature against Supabase JWKS.
  3. Read `sub` claim (Supabase `auth.users.id`).
  4. `SELECT id, email, role, client_id FROM business.users WHERE auth_user_id = %s`.
  5. Return `UserContext(auth_user_id, business_user_id, email, role, client_id)`.
- **Errors:** 401 on bad JWT; 403 if no matching `business.users` row.

## 2. Account / tenant tables

### `business.users` ([migrations/0001_business_users.sql](migrations/0001_business_users.sql))

```sql
id UUID PK
auth_user_id UUID UNIQUE        -- Supabase auth.users.id
role TEXT                       -- "operator" | "client"
client_id UUID                  -- FK → business.brands(id)
email TEXT
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
```

### `business.accounts` ([migrations/0014_accounts_contacts.sql](migrations/0014_accounts_contacts.sql))

```sql
id UUID PK
name TEXT
domain TEXT UNIQUE
industry TEXT
employee_count INT
annual_revenue_usd BIGINT
metadata JSONB
status TEXT                     -- lead|trial|active|churned|archived
created_at, updated_at, deleted_at TIMESTAMPTZ
```

Top-level. **Not** linked to `business.brands` or `business.users`. Used for sales/billing (contacts, products, purchases).

### Membership / junction table

**Does not exist.** User-to-tenant linkage is a single `business.users.client_id` FK to `business.brands` (1:1 user → brand). There is no junction between `business.users` and `business.accounts`.

## 3. Domain tables — account scoping

"Brand-scoped" = has `brand_id` FK to `business.brands`. "Account-scoped" = has `account_id` FK to `business.accounts`. The two hierarchies are disjoint.

| Table | Scoping column | FK target | Notes |
|---|---|---|---|
| `business.users` | `client_id` | `business.brands` | 1:1 user→brand |
| `business.brands` | — | (root) | Root tenant entity |
| `business.partners` | `brand_id` | `business.brands` | |
| `business.campaigns` | `brand_id`, `partner_id` | brands, partners | |
| `voice_assistants` | `brand_id`, `partner_id`, `campaign_id` | brands | |
| `voice_phone_numbers` | `brand_id`, `partner_id`, `campaign_id` | brands | |
| `call_logs` | `brand_id`, `partner_id`, `campaign_id` | brands | |
| `transfer_territories` | `brand_id`, `partner_id`, `campaign_id` | brands | |
| `outbound_call_configs` | `brand_id`, `call_log_id` | brands, call_logs | |
| `trust_hub_registrations` | `brand_id` | brands | |
| `sms_messages` | `brand_id`, `partner_id`, `campaign_id` | brands | |
| `sms_suppressions` | `brand_id` | brands | |
| `ivr_flows` | `brand_id` | brands | |
| `ivr_flow_steps` | `flow_id`, `brand_id` | ivr_flows | |
| `ivr_phone_configs` | `brand_id`, `flow_id` | brands, ivr_flows | |
| `ivr_sessions` | `brand_id`, `flow_id`, `call_log_id` | brands | |
| `voice_ai_campaign_configs` | `brand_id`, `campaign_id` | brands, campaigns | |
| `voice_campaign_active_calls` | `brand_id`, `campaign_id` | brands | |
| `voice_campaign_metrics` | `brand_id`, `campaign_id` | brands | |
| `voice_assistant_phone_configs` | `brand_id`, `voice_assistant_id` | brands | |
| `do_not_call_lists` | `brand_id`, `phone_number` | brands | |
| `voice_callback_requests` | `brand_id`, `partner_id`, `campaign_id` | brands | |
| `vapi_transcript_events` | `brand_id`, `call_log_id` | brands | |
| `webhook_events` | `brand_id` (nullable) | brands (SET NULL) | Provider webhook ingest |
| `cal_raw_events` | — | — | Legacy OEX import; no scoping |
| `direct_mail_pieces` | — | — | Only `created_by_user_id`; **no brand_id** |
| `direct_mail_piece_events` | `piece_id` | direct_mail_pieces | Transitive only |
| `suppressed_addresses` | `source_piece_id` (nullable) | direct_mail_pieces | Transitive only |
| `business.accounts` | — | (root) | Independent hierarchy |
| `business.contacts` | `account_id` | business.accounts | |
| `business.products` | — | — | Global catalog |
| `business.account_purchases` | `account_id` | business.accounts | |
| `business.audience_drafts` | `created_by_user_id` | (no FK) | User-owned, not tenant-scoped |
| `dmaas_scaffolds` | — | — | Platform-shared; only `created_by_user_id` |
| `dmaas_designs` | `brand_id` (nullable), `scaffold_id` | brands (SET NULL), scaffolds | |
| `dmaas_scaffold_authoring_sessions` | `scaffold_id` (nullable) | scaffolds | Audit trail |
| `dmaas_dub_links` | `brand_id` (nullable), `dmaas_design_id`, `direct_mail_piece_id` | brands, designs, pieces (all SET NULL) | |

Tables explicitly without scoping that are **platform-shared by design:** `business.products`, `dmaas_scaffolds`. Tables without brand scoping that are **not deliberately platform-shared:** `direct_mail_pieces` (and its dependents `direct_mail_piece_events`, `suppressed_addresses`), `cal_raw_events`, `business.audience_drafts`.

## 4. Brands

[migrations/0002_brands.sql](migrations/0002_brands.sql):

```sql
CREATE TABLE business.brands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    display_name TEXT,
    domain TEXT,
    twilio_account_sid_enc TEXT,        -- pgp_sym_encrypt
    twilio_auth_token_enc TEXT,         -- pgp_sym_encrypt
    twilio_messaging_service_sid TEXT,
    trust_hub_registration_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`business.brands` is a root tenant entity. Not scoped to `business.accounts`. Ownership of a brand is expressed by `business.users.client_id` pointing at `brands.id` (1:1).

## 5. DMaaS tables

[migrations/0018_dmaas_scaffolds_designs.sql](migrations/0018_dmaas_scaffolds_designs.sql):

### `dmaas_scaffolds` — platform-shared (not account-scoped)

```sql
id UUID PK
slug TEXT UNIQUE
name TEXT
format TEXT CHECK (format IN ('postcard','letter','self_mailer','snap_pack','booklet'))
compatible_specs JSONB
prop_schema JSONB
constraint_specification JSONB
preview_image_url TEXT
vertical_tags TEXT[]
is_active BOOLEAN
version_number INT
created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL
created_at, updated_at TIMESTAMPTZ
```

No `brand_id` / `account_id`. Operator-authored reusable templates.

### `dmaas_designs` — brand-scoped (nullable)

```sql
id UUID PK
scaffold_id UUID NOT NULL REFERENCES dmaas_scaffolds(id) ON DELETE RESTRICT
spec_category TEXT
spec_variant TEXT
content_config JSONB
resolved_positions JSONB
brand_id UUID REFERENCES business.brands(id) ON DELETE SET NULL
audience_template_id UUID
created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL
version_number INT
created_at, updated_at TIMESTAMPTZ
FOREIGN KEY (spec_category, spec_variant)
    REFERENCES direct_mail_specs (mailer_category, variant) ON DELETE RESTRICT
```

Brand scoping via nullable `brand_id`.

### `dmaas_scaffold_authoring_sessions` — not directly account-scoped

```sql
id UUID PK
scaffold_id UUID REFERENCES dmaas_scaffolds(id) ON DELETE SET NULL
prompt TEXT
proposed_constraint_specification JSONB
accepted BOOLEAN
notes TEXT
created_by_user_id UUID REFERENCES business.users(id) ON DELETE SET NULL
created_at TIMESTAMPTZ
```

Scoped only via `scaffold_id` (which itself is platform-shared) and `created_by_user_id`.

## 6. Operator vs. non-operator

Determined by the `role` column on `business.users` — values `"operator"` or `"client"`. Set at provisioning time.

[app/auth/roles.py](app/auth/roles.py):

```python
def require_operator(user: UserContext = Depends(verify_supabase_jwt)):
    if user.role != "operator":
        raise HTTPException(status_code=403, detail="Operator access required")
    return user

def require_client(user: UserContext = Depends(verify_supabase_jwt)):
    if user.role != "client":
        raise HTTPException(status_code=403, detail="Client access required")
    return user
```

Used as a FastAPI `Depends(...)` on routes in `app/routers/direct_mail.py`, `app/routers/dub.py`, `app/routers/dmaas.py`. Not a flag on `business.brands` or `business.accounts`; not hardcoded.

## 7. External provider key storage

All third-party API keys are **single-tenant** (one set per app environment), held in `app/config.py` Pydantic settings sourced from environment variables. No per-account/per-brand override exists for any of these except Twilio.

| Provider | Env vars | Scope |
|---|---|---|
| Lob | `LOB_API_KEY`, `LOB_API_KEY_TEST`, `LOB_WEBHOOKS_SECRET_LIVE`, `LOB_WEBHOOKS_SECRET_TEST`, `LOB_WEBHOOK_SIGNATURE_MODE` | Single-tenant |
| Dub.co | `DUB_API_KEY`, `DUB_DEFAULT_DOMAIN`, `DUB_DEFAULT_TENANT_ID`, `DUB_WEBHOOK_SECRET`, `DUB_API_BASE_URL` | Single-tenant |
| Vapi | `VAPI_API_KEY`, `VAPI_WEBHOOK_SECRET`, `VAPI_WEBHOOK_SIGNATURE_MODE` | Single-tenant |
| ClickHouse | `CLICKHOUSE_URL`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE` | Single-tenant |
| Rudderstack | — | Not present in codebase |
| DMaaS MCP | `DMAAS_MCP_BEARER_TOKEN` | Single-tenant |
| Twilio | `business.brands.twilio_account_sid_enc`, `business.brands.twilio_auth_token_enc`, `business.brands.twilio_messaging_service_sid` (encrypted with `BRAND_CREDS_ENCRYPTION_KEY` via `pgp_sym_encrypt`) | **Per-brand** |

Twilio is the only provider whose credentials are stored per-tenant. All others are global.
