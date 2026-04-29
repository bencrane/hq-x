# Tenancy migration — PR notes

Companion to `migrations/0020_organizations_tenancy.sql`. Covers the directive's required deliverables: `require_operator` mapping, `client_id` audit, and orphan handling.

## Mapping: existing `require_operator` usages → new dependencies

The audit found 110 `require_operator` call sites. **Every one is a system-wide admin endpoint** — none are tenant-scoped admin actions. They all map to `require_platform_operator`.

To avoid touching 110 call sites mechanically (and to keep the migration reviewable), `require_operator` is preserved in `app/auth/roles.py` as an alias:

```python
require_operator = require_platform_operator
```

This alias is intended to be removed in a follow-up after the call sites are renamed in batches.

| Module | Endpoints | Old | New |
|---|---|---|---|
| `app/routers/admin/me.py` | 1 | `require_operator` | `require_platform_operator` |
| `app/routers/dmaas.py` | 4 (scaffold authoring + design admin) | `require_operator` | `require_platform_operator` |
| `app/routers/dub.py` | 3 (write ops) | `require_operator` | `require_platform_operator` |
| `app/routers/direct_mail.py` | 95 | `require_operator` | `require_platform_operator` |
| `app/routers/webhooks/lob.py` | 1 (webhook replay) | `require_operator` | `require_platform_operator` |

`require_client` had zero call sites at directive time and is left in place unchanged.

`app/auth/flexible.py::require_flexible_auth` previously checked `user.role != "operator"`. Updated to check `user.platform_role != "platform_operator"`.

## Audit: `user.client_id` reads

```
$ grep -rn "client_id" app/ --include="*.py" | grep -v "auth/"
(no matches)
```

**No router or service code reads `user.client_id`.** The only readers are inside `app/auth/supabase_jwt.py` (row hydration). The directive's per-router rewrite section is therefore empty — there are no `client_id` → `active_organization_id` rewrites to do at the router layer in this migration.

The legacy column is preserved on `UserContext` and `business.users` so the migration is reversible without code changes. It will be removed once a future review confirms it is still unused.

## Schema changes

New tables:
* `business.organizations`
* `business.organization_memberships`  (UNIQUE on `(user_id, organization_id)`)
* `business.audit_events`              (table-only; not yet written to)

Column additions (all nullable initially):
* `business.users.platform_role` — `CHECK (platform_role IS NULL OR platform_role IN ('platform_operator'))`
* `business.brands.organization_id` → `business.organizations(id)`
* `direct_mail_pieces.brand_id` → `business.brands(id)`
* `business.audience_drafts.brand_id` → `business.brands(id)`
* `cal_raw_events.brand_id` → `business.brands(id)`

Constraints tightened post-backfill:
* `business.brands.organization_id` → NOT NULL
* `dmaas_designs.brand_id` → NOT NULL  (any legacy NULLs must be cleaned up before this migration runs in prd; the migration aborts on existing NULLs, which is intended)

Constraints **left nullable pending operator review**:
* `direct_mail_pieces.brand_id`
* `business.audience_drafts.brand_id`
* `cal_raw_events.brand_id`

## Backfill behavior

1. Insert one `organizations` row: `name='Operator Workspace'`, `slug='operator-workspace'`. Idempotent on slug.
2. `UPDATE business.brands SET organization_id = <op-workspace>` for any brand currently NULL.
3. `UPDATE business.users SET platform_role='platform_operator'` for every user with `role='operator'`.
4. Insert `organization_memberships(user_id, organization_id, org_role)` for every user — operators as `'owner'`, clients as `'member'`. Idempotent on the unique constraint.
5. Backfill `direct_mail_pieces.brand_id` and `business.audience_drafts.brand_id` by joining `created_by_user_id → business.users.client_id`.
6. `cal_raw_events.brand_id` is added but not backfilled (the table has no `created_by_user_id`); flagged for operator review.

## Orphan handling

After the join-based backfill, any rows where the user has no `client_id` (or where `created_by_user_id` is NULL) end up with `brand_id = NULL`. These are intentionally left:

* not promoted to NOT NULL,
* not auto-assigned to the operator workspace's primary brand (the directive prohibits silent assignment),
* discoverable via:

```sql
SELECT 'direct_mail_pieces' AS tbl, count(*) AS orphans
  FROM direct_mail_pieces WHERE brand_id IS NULL
UNION ALL
SELECT 'business.audience_drafts', count(*)
  FROM business.audience_drafts WHERE brand_id IS NULL
UNION ALL
SELECT 'cal_raw_events', count(*)
  FROM cal_raw_events WHERE brand_id IS NULL;
```

Operator must run this and resolve before `ALTER COLUMN brand_id SET NOT NULL` can happen in a follow-up migration. **Any user with `role='client'` whose memberships you want elsewhere than the operator workspace should be reviewed too** — the migration places them in the operator workspace because that is where their existing `client_id`-pointed brand currently lives. Existing clients are legacy; flag for manual review.

## Auth layer changes

`UserContext` now exposes:
* `platform_role: str | None`
* `active_organization_id: UUID | None`
* `org_role: str | None`

…in addition to the existing `role` and `client_id` (preserved during transition).

Per-request resolution rules (full spec in `docs/tenancy-model.md`):
* `X-Organization-Id` header → validate membership; platform operators bypass.
* No header + single membership → auto-select.
* Otherwise → leave `None`; org-required endpoints return 400.

New dependencies in `app/auth/roles.py`:
* `require_platform_operator`
* `require_org_context`
* `require_org_role(*roles)`

## Test coverage

* `tests/test_org_auth.py` — 14 tests covering org context resolution (header, single-membership, multi-membership, invalid UUID, non-member, plat-op bypass) and each new dependency.
* `tests/test_supabase_jwt.py` — fixture extended to patch `_lookup_memberships`; existing test updated to include `platform_role` in the fake row.
* `tests/test_admin_me.py` — fixture and rejection error code updated.
* All existing `UserContext(...)` constructions in tests (`test_audience_drafts.py`, `test_direct_mail_specs.py`, `test_direct_mail_endpoints.py`, `test_dmaas_endpoints.py`, `test_dub_router.py`) updated with `platform_role`/`active_organization_id`/`org_role`.

Full suite: **264 passed, 1 unrelated DeprecationWarning**.

## Out of scope (separate PRs)

* Audit-event wiring for cross-org plat-op actions.
* Customer self-serve onboarding flow (creates first organization + first membership).
* Billing on `organizations.plan`.
* Per-org branded login.
* Renaming `require_operator` → `require_platform_operator` at all 110 call sites.
* Tightening `direct_mail_pieces.brand_id` and friends to NOT NULL.
* Dropping `business.users.role` and `client_id`.
