#!/usr/bin/env python3
"""Load PLUTO categorical lookup tables from scripts/data/pluto_lookups/*.json.

Usage:
  PYTHONPATH=. doppler run -- python scripts/ingest_pluto_lookups.py [--version 25v4]

Idempotent: each row upserts on the natural key. Re-running for the same
revision is a no-op except for refreshing source_revision and ingested_at.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from psycopg_pool import ConnectionPool  # noqa: E402

from app.config import get_settings  # noqa: E402

DATA_DIR = ROOT / "scripts" / "data" / "pluto_lookups"


def _load_json(filename: str) -> list[dict]:
    with (DATA_DIR / filename).open("r", encoding="utf-8") as f:
        return json.load(f)


def upsert_borough_codes(pool: ConnectionPool, revision: str) -> int:
    rows = _load_json("borough_codes.json")
    with pool.connection() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO lookup.pluto_borough_codes
                  (borocode, borough, borough_name, source_revision)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (borocode) DO UPDATE SET
                  borough = EXCLUDED.borough,
                  borough_name = EXCLUDED.borough_name,
                  source_revision = EXCLUDED.source_revision,
                  ingested_at = now()
                """,
                (r["borocode"], r["borough"], r["borough_name"], revision),
            )
    return len(rows)


def upsert_land_use_codes(pool: ConnectionPool, revision: str) -> int:
    rows = _load_json("land_use_codes.json")
    with pool.connection() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO lookup.pluto_land_use_codes
                  (landuse, description, source_revision)
                VALUES (%s, %s, %s)
                ON CONFLICT (landuse) DO UPDATE SET
                  description = EXCLUDED.description,
                  source_revision = EXCLUDED.source_revision,
                  ingested_at = now()
                """,
                (r["landuse"], r["description"], revision),
            )
    return len(rows)


def upsert_building_class_codes(pool: ConnectionPool, revision: str) -> int:
    rows = _load_json("building_class_codes.json")
    with pool.connection() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO lookup.pluto_building_class_codes
                  (bldgclass, description, category_code, category_name, source_revision)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (bldgclass) DO UPDATE SET
                  description = EXCLUDED.description,
                  category_code = EXCLUDED.category_code,
                  category_name = EXCLUDED.category_name,
                  source_revision = EXCLUDED.source_revision,
                  ingested_at = now()
                """,
                (
                    r["bldgclass"], r["description"],
                    r.get("category_code"), r.get("category_name"),
                    revision,
                ),
            )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default="25v4",
        help="Source revision tag to stamp on the rows (default: 25v4).",
    )
    args = parser.parse_args()

    pool = ConnectionPool(
        conninfo=get_settings().database_url,
        min_size=1,
        max_size=2,
        timeout=30.0,
    )
    n_borough = upsert_borough_codes(pool, args.version)
    n_landuse = upsert_land_use_codes(pool, args.version)
    n_bldgclass = upsert_building_class_codes(pool, args.version)
    print(f"borough_codes:        {n_borough}")
    print(f"land_use_codes:       {n_landuse}")
    print(f"building_class_codes: {n_bldgclass}")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
