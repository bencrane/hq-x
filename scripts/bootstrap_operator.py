"""Create the initial operator user.

Idempotent: safe to re-run. Behaviors:
  * If neither auth.users nor business.users has a row → create both.
  * If auth.users has a row but business.users does not → insert
    business.users using the existing auth_user_id.
  * If business.users already has a row for the email → no-op.

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


def _business_user_exists(conn: psycopg.Connection, email: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM business.users WHERE email = %s", (email,))
        return cur.fetchone() is not None


def _insert_business_user(
    conn: psycopg.Connection, *, auth_user_id: UUID, email: str
) -> UUID:
    business_user_id = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO business.users (id, auth_user_id, email, role, client_id)
            VALUES (%s, %s, %s, 'operator', NULL)
            """,
            (str(business_user_id), str(auth_user_id), email),
        )
    conn.commit()
    return business_user_id


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
        if _business_user_exists(conn, OPERATOR_EMAIL):
            print("business.users row already present — nothing to do")
            return 0
        business_user_id = _insert_business_user(
            conn, auth_user_id=auth_user_id, email=OPERATOR_EMAIL
        )
        print(f"inserted business.users row: {business_user_id}")

    print("done")
    print(f"  auth_user_id={auth_user_id}")
    print(f"  email={OPERATOR_EMAIL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
