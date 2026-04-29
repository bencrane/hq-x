# hq-x Tenancy Model

This doc describes how tenancy works in hq-x as of migration `0020_organizations_tenancy.sql`. It supersedes `docs/tenancy-state.md` for any tables touched by that migration.

## Two hierarchies, one DB

hq-x has **two parallel hierarchies** that do not connect. Conflating them muddies both, so they are kept separate by design.

### Operational tenancy (`organizations`)

Roots all paying-customer operational data: brands, campaigns, voice/SMS/IVR, DMaaS designs, direct-mail pieces.

```
business.organizations
   └── business.brands              (organization_id, NOT NULL)
         ├── partners
         │     └── campaigns
         │           ├── voice_assistants / phone_numbers / call_logs
         │           ├── voice_ai_*
         │           ├── sms_messages / sms_suppressions
         │           ├── ivr_flows / sessions / phone_configs
         │           └── voice_callback_requests
         ├── trust_hub_registrations
         ├── do_not_call_lists
         ├── dmaas_designs           (brand_id, NOT NULL)
         ├── dmaas_dub_links
         ├── direct_mail_pieces      (brand_id, nullable — backfill in flight)
         └── business.audience_drafts (brand_id, nullable — backfill in flight)
```

`business.users` no longer has a 1:1 brand link as the canonical relationship. The legacy `business.users.client_id` column is preserved during the transition window but is read only by `verify_supabase_jwt`'s row hydration; routers do not consult it. The new join is:

```
business.users  ─┬─ business.organization_memberships ─── business.organizations
                  └── (1:N memberships)
```

### CRM hierarchy (`accounts`)

Independent. Tracks pre-paying sales prospects, contacts, and product purchases. Not part of platform tenancy.

```
business.accounts
   ├── business.contacts            (account_id, NOT NULL)
   └── business.account_purchases   (account_id, NOT NULL)
business.products                    (global catalog; not scoped)
```

`business.accounts` and `business.organizations` are **not** linked. A sales prospect (account) becomes a customer (organization) by a separate provisioning flow, not by FK.

### Platform-shared (no scoping by design)

* `dmaas_scaffolds` — operator-authored reusable templates, shared across all tenants.
* `business.products` — global product catalog.

## Two role axes

hq-x has two independent role columns:

| Axis | Column | Domain | Granted by |
|---|---|---|---|
| **Platform role** | `business.users.platform_role` | `'platform_operator'` \| `NULL` | hq-x staff provisioning |
| **Org role** | `business.organization_memberships.org_role` | `'owner'` \| `'admin'` \| `'member'` | Per-org membership |

* `platform_role = 'platform_operator'` is system-wide. It bypasses `org_role` checks and lets the holder operate against any org by passing `X-Organization-Id`. Used for hq-x staff (Ben + future ops engineers).
* `org_role` is scoped to a single organization via the `organization_memberships` row. Standard tenancy.
* The legacy `business.users.role` (`'operator' | 'client'`) column remains for backward compatibility and will be dropped in a follow-up after all callers migrate.

### Decision rules for new endpoints

1. **System-wide admin (scaffold authoring, cross-tenant ops, internal admin pages):** gate on `require_platform_operator`.
2. **Tenant-scoped admin (create campaigns, manage brands, billing):** gate on `require_org_role('owner', 'admin')`.
3. **Tenant-scoped read/write by any member:** gate on `require_org_role('owner', 'admin', 'member')` or simply `require_org_context`.
4. **Public/unauthenticated:** no dependency.

If unsure: if the action would be wrong in another tenant's data, it is org-scoped, not platform-scoped.

## Auth: organization context resolution

`verify_supabase_jwt` populates `UserContext.active_organization_id` and `org_role` per request. Resolution order:

1. **`X-Organization-Id` header present.** Validate the user has an active membership in that org. Set `active_organization_id` and the matching `org_role`. If the user is not a member but has `platform_role = 'platform_operator'`, allow with `org_role = None`. Otherwise 403 `not_a_member_of_organization`. A malformed UUID returns 400 `invalid_organization_id`.
2. **No header, exactly one membership.** Auto-select that org and its `org_role`.
3. **No header, multiple or zero memberships.** Leave both fields `None`. Endpoints requiring an org context return 400 `organization_context_required`.

## `UserContext`

```python
@dataclass(frozen=True)
class UserContext:
    auth_user_id: UUID                       # Supabase auth.users.id
    business_user_id: UUID                   # business.users.id
    email: str
    platform_role: str | None                # 'platform_operator' | None
    active_organization_id: UUID | None      # resolved per request
    org_role: str | None                     # 'owner' | 'admin' | 'member' | None
    role: str                                # legacy 'operator' | 'client'
    client_id: UUID | None                   # legacy brand FK
```

## Auth dependency reference

All exported from `app.auth.roles`.

| Dependency | Purpose | Failure modes |
|---|---|---|
| `verify_supabase_jwt` | Base: validates JWT, hydrates `UserContext`, resolves org context | 401 missing/invalid; 403 user not provisioned; 403 not member of requested org; 400 invalid org-id |
| `require_platform_operator` | Gate for system-wide admin | 403 `platform_operator_required` |
| `require_org_context` | Gate for any endpoint that needs an org (no role minimum) | 400 `organization_context_required` |
| `require_org_role(*roles)` | Gate for org_role ∈ `roles`. Platform operators bypass. | 400 `organization_context_required`; 403 `insufficient_org_role` |
| `require_operator` | **Backward-compat alias for `require_platform_operator`.** All current call sites are platform-operator-only (admin, dmaas authoring, dub writes, lob webhooks, direct mail). | Same as `require_platform_operator` |
| `require_client` | Legacy: gate on `business.users.role == 'client'`. Unused at directive time, preserved for source compat. | 403 `client_role_required` |

## Out of scope (future work)

* Wiring `business.audit_events` writes for cross-org platform-operator actions.
* Customer self-serve onboarding (creates `organizations` + first `organization_memberships` row).
* Billing on `organizations.plan`.
* Per-organization branded login or custom domains.
* Tightening `direct_mail_pieces.brand_id`, `business.audience_drafts.brand_id`, `cal_raw_events.brand_id` to NOT NULL after operator orphan review.
* Dropping `business.users.role` and `business.users.client_id` once nothing reads them.
