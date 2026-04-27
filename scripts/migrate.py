"""Apply SQL migrations from ./migrations in lexical order.

Tracks applied migrations in a `schema_migrations` table. Re-runs are no-ops
for already-applied files. Uses HQX_DB_URL_DIRECT (port 5432) to avoid
transaction-pooling restrictions.

Usage:
    doppler run --project hq-x --config dev -- uv run python -m scripts.migrate
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from app.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _ensure_tracking_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    conn.commit()


def _applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _apply(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (filename) VALUES (%s)",
            (path.name,),
        )
    conn.commit()


def main() -> int:
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("no migration files found")
        return 0

    with psycopg.connect(str(settings.HQX_DB_URL_DIRECT)) as conn:
        _ensure_tracking_table(conn)
        applied = _applied(conn)
        pending = [p for p in files if p.name not in applied]
        if not pending:
            print("all migrations already applied")
            return 0
        for path in pending:
            print(f"applying {path.name}")
            _apply(conn, path)
        print(f"applied {len(pending)} migration(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
