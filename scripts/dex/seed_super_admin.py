#!/usr/bin/env python3
"""
Seed a single super admin record.

Required environment variables:
- DATA_ENGINE_DATABASE_URL
- SUPER_ADMIN_EMAIL
- SUPER_ADMIN_PASSWORD
"""

import os
import sys

import bcrypt
import psycopg


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    database_url = _required_env("DATA_ENGINE_DATABASE_URL")
    email = _required_env("SUPER_ADMIN_EMAIL").strip().lower()
    password = _required_env("SUPER_ADMIN_PASSWORD")

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode(
        "utf-8"
    )

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM super_admins WHERE email = %s LIMIT 1", (email,))
            existing = cur.fetchone()
            if existing:
                print(f"super_admin already exists for {email}")
                return 0

            cur.execute(
                """
                INSERT INTO super_admins (email, password_hash, is_active)
                VALUES (%s, %s, TRUE)
                """,
                (email, password_hash),
            )
        conn.commit()

    print(f"seeded super_admin: {email}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"seed_super_admin failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
