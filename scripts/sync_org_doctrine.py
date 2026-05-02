"""Sync data/orgs/<slug>/doctrine.md + an embedded JSON block into
business.org_doctrine.

Disk is source of truth. The script reads:

  * data/orgs/<slug>/doctrine.md — the prose policy doc.
  * data/orgs/<slug>/parameters.json — the structured JSONB block.

(Two files because parsing the JSON out of fenced blocks in the
markdown is fragile. The .md doc references the parameters JSON for
documentation purposes, and the .json file is what gets ingested.)

Resolves organization_id by slug from business.organizations.

Usage:
    doppler --project hq-x --config dev run -- \\
        uv run python -m scripts.sync_org_doctrine acq-eng
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
ORGS_DIR = REPO_ROOT / "data" / "orgs"


def _conn_str() -> str:
    raw = os.environ.get("HQX_DB_URL_POOLED")
    if not raw:
        print(
            "HQX_DB_URL_POOLED not in env (run via doppler run --)",
            file=sys.stderr,
        )
        sys.exit(2)
    return raw


def sync(slug: str) -> None:
    org_dir = ORGS_DIR / slug
    if not org_dir.is_dir():
        print(f"org dir not found: {org_dir}", file=sys.stderr)
        sys.exit(2)

    doctrine_md_path = org_dir / "doctrine.md"
    if not doctrine_md_path.is_file():
        print(f"doctrine.md missing in {org_dir}", file=sys.stderr)
        sys.exit(2)

    params_path = org_dir / "parameters.json"
    if not params_path.is_file():
        print(
            f"parameters.json missing in {org_dir} — author it next to doctrine.md",
            file=sys.stderr,
        )
        sys.exit(2)

    doctrine_markdown = doctrine_md_path.read_text()
    parameters = json.loads(params_path.read_text())

    with psycopg.connect(_conn_str()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM business.organizations
                WHERE slug = %s
                """,
                (slug,),
            )
            row = cur.fetchone()
            if row is None:
                print(
                    f"no business.organizations row for slug={slug!r}",
                    file=sys.stderr,
                )
                sys.exit(2)
            org_id = row[0]
            print(f"organization_id={org_id} (slug={slug!r})")

            cur.execute(
                """
                INSERT INTO business.org_doctrine (
                    organization_id, doctrine_markdown, parameters
                )
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (organization_id) DO UPDATE
                SET doctrine_markdown = EXCLUDED.doctrine_markdown,
                    parameters = EXCLUDED.parameters,
                    updated_at = NOW()
                RETURNING (xmax = 0) AS inserted
                """,
                (str(org_id), doctrine_markdown, json.dumps(parameters)),
            )
            was_inserted = cur.fetchone()[0]
        conn.commit()
    msg = "inserted" if was_inserted else "updated"
    print(
        f"  doctrine.md     md   {len(doctrine_markdown):>6d} chars  ({msg})"
    )
    print(
        f"  parameters.json json {len(json.dumps(parameters)):>6d} chars  ({msg})"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug", help="org slug — directory name under data/orgs/")
    args = p.parse_args()
    sync(args.slug)


if __name__ == "__main__":
    main()
