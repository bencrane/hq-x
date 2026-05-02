"""Create the initial operator user.

Idempotent: safe to re-run. Behaviors:
  * If neither auth.users nor business.users has a row → create both.
  * If auth.users has a row but business.users does not → insert
    business.users using the existing auth_user_id.
  * If business.users already has a row for the email → reuse it.
  * Always ensures platform_role='platform_operator' on the business.users
    row and an active 'owner' membership in the operator-workspace org —
    so re-running this script repairs users created before migration 0020's
    two-axis-role backfill.

Usage:
    doppler run --project hq-x --config dev -- \\
        uv run python -m scripts.bootstrap_operator
"""

from __future__ import annotations

import getpass
import os
import sys
from uuid import UUID, uuid4

import psycopg
from supabase import Client, create_client

from app.config import settings

OPERATOR_EMAIL = "admin@acquisitionengineering.com"


def _supabase() -> Client:
    return create_client(
        str(settings.HQX_SUPABASE_URL),
        settings.HQX_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
    )


def _find_existing_auth_user(client: Client, email: str) -> UUID | None:
    page = 1
    while True:
        resp = client.auth.admin.list_users(page=page, per_page=200)
        users = resp if isinstance(resp, list) else getattr(resp, "users", [])
        if not users:
            return None
        for u in users:
            if (getattr(u, "email", None) or "").lower() == email.lower():
                return UUID(str(u.id))
        if len(users) < 200:
            return None
        page += 1


def _create_auth_user(client: Client, email: str, password: str) -> UUID:
    resp = client.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    user = getattr(resp, "user", None) or resp
    return UUID(str(user.id))


def _find_business_user(conn: psycopg.Connection, email: str) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM business.users WHERE email = %s", (email,))
        row = cur.fetchone()
        return UUID(str(row[0])) if row else None


def _insert_business_user(
    conn: psycopg.Connection, *, auth_user_id: UUID, email: str
) -> UUID:
    business_user_id = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO business.users
                (id, auth_user_id, email, role, platform_role, client_id)
            VALUES (%s, %s, %s, 'operator', 'platform_operator', NULL)
            """,
            (str(business_user_id), str(auth_user_id), email),
        )
    conn.commit()
    return business_user_id


def _ensure_platform_operator_role(
    conn: psycopg.Connection, *, business_user_id: UUID
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE business.users
            SET platform_role = 'platform_operator'
            WHERE id = %s AND platform_role IS DISTINCT FROM 'platform_operator'
            """,
            (str(business_user_id),),
        )
    conn.commit()


OPERATOR_ORG_SLUG = "acq-eng"
OPERATOR_ORG_NAME = "Acquisition Engineering"


def _ensure_operator_org(conn: psycopg.Connection) -> None:
    """Ensure the operator org exists with the desired name + slug.

    Migration 0020 created it as ('Operator Workspace', 'operator-workspace');
    this rewrites that row in place. Idempotent on re-run.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE business.organizations
            SET name = %s, slug = %s
            WHERE slug IN (%s, 'operator-workspace')
              AND (name, slug) IS DISTINCT FROM (%s, %s)
            """,
            (
                OPERATOR_ORG_NAME,
                OPERATOR_ORG_SLUG,
                OPERATOR_ORG_SLUG,
                OPERATOR_ORG_NAME,
                OPERATOR_ORG_SLUG,
            ),
        )
        cur.execute(
            """
            INSERT INTO business.organizations (name, slug, status)
            VALUES (%s, %s, 'active')
            ON CONFLICT (slug) DO NOTHING
            """,
            (OPERATOR_ORG_NAME, OPERATOR_ORG_SLUG),
        )
    conn.commit()


def _ensure_operator_workspace_membership(
    conn: psycopg.Connection, *, business_user_id: UUID
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO business.organization_memberships
                (user_id, organization_id, org_role, status)
            SELECT %s, id, 'owner', 'active'
            FROM business.organizations
            WHERE slug = %s
            ON CONFLICT (user_id, organization_id) DO NOTHING
            """,
            (str(business_user_id), OPERATOR_ORG_SLUG),
        )
    conn.commit()


def main() -> int:
    password = os.environ.get("OPERATOR_PASSWORD")
    if not password:
        password = getpass.getpass(f"Password for {OPERATOR_EMAIL}: ")
    if not password:
        print("error: empty password", file=sys.stderr)
        return 1

    client = _supabase()
    auth_user_id = _find_existing_auth_user(client, OPERATOR_EMAIL)
    if auth_user_id is None:
        print(f"creating auth user {OPERATOR_EMAIL}")
        auth_user_id = _create_auth_user(client, OPERATOR_EMAIL, password)
    else:
        print(f"auth user already exists: {auth_user_id}")

    with psycopg.connect(str(settings.HQX_DB_URL_DIRECT)) as conn:
        business_user_id = _find_business_user(conn, OPERATOR_EMAIL)
        if business_user_id is None:
            business_user_id = _insert_business_user(
                conn, auth_user_id=auth_user_id, email=OPERATOR_EMAIL
            )
            print(f"inserted business.users row: {business_user_id}")
        else:
            print(f"business.users row already present: {business_user_id}")

        _ensure_platform_operator_role(conn, business_user_id=business_user_id)
        _ensure_operator_org(conn)
        _ensure_operator_workspace_membership(
            conn, business_user_id=business_user_id
        )

    print("done")
    print(f"  auth_user_id={auth_user_id}")
    print(f"  business_user_id={business_user_id}")
    print(f"  email={OPERATOR_EMAIL}")
    print("  platform_role=platform_operator")
    print(f"  org={OPERATOR_ORG_NAME} (slug={OPERATOR_ORG_SLUG})")
    print("  membership=owner / active")
    return 0


if __name__ == "__main__":
    sys.exit(main())
