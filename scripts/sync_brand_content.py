"""Sync data/brands/<slug>/*.{md,json} into business.brand_content.

Disk is source of truth. This script upserts a row per file, keyed by
(brand_id, content_key). content_key is the filename without extension;
brand.json lands as content_key='brand' with content_format='json'.

Usage:
    doppler --project hq-x --config dev run -- \\
        uv run python -m scripts.sync_brand_content capital-expansion

Resolves brand_id by reading data/brands/<slug>/brand.json and looking up
business.brands by name (matching brand.json's "name" field).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
BRANDS_DIR = REPO_ROOT / "data" / "brands"


def _conn_str() -> str:
    raw = os.environ.get("HQX_DB_URL_POOLED")
    if not raw:
        print("HQX_DB_URL_POOLED not in env (run via doppler run --)", file=sys.stderr)
        sys.exit(2)
    # psycopg accepts the URI directly; just verify it parses.
    parsed = urlparse(raw)
    if not parsed.scheme:
        print(f"HQX_DB_URL_POOLED malformed: {raw[:30]}...", file=sys.stderr)
        sys.exit(2)
    return raw


def _file_to_row(path: Path) -> tuple[str, str, str, str]:
    """Return (content_key, content_format, content, source_path)."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    name = path.stem  # filename without extension
    if path.suffix == ".json":
        return (name, "json", path.read_text(), rel)
    if path.suffix == ".md":
        return (name, "md", path.read_text(), rel)
    if path.suffix in (".yaml", ".yml"):
        return (name, "yaml", path.read_text(), rel)
    return (name, "txt", path.read_text(), rel)


def sync(slug: str) -> None:
    brand_dir = BRANDS_DIR / slug
    if not brand_dir.is_dir():
        print(f"brand dir not found: {brand_dir}", file=sys.stderr)
        sys.exit(2)

    brand_json_path = brand_dir / "brand.json"
    if not brand_json_path.is_file():
        print(f"brand.json missing in {brand_dir}", file=sys.stderr)
        sys.exit(2)
    brand_meta = json.loads(brand_json_path.read_text())
    brand_name = brand_meta.get("name")
    if not brand_name:
        print(f"brand.json missing 'name' field", file=sys.stderr)
        sys.exit(2)

    files: list[Path] = sorted(
        p for p in brand_dir.iterdir()
        if p.is_file() and p.suffix in (".md", ".json", ".yaml", ".yml", ".txt")
    )
    if not files:
        print(f"no content files in {brand_dir}", file=sys.stderr)
        sys.exit(2)

    with psycopg.connect(_conn_str()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM business.brands WHERE name = %s AND deleted_at IS NULL",
                (brand_name,),
            )
            row = cur.fetchone()
            if row is None:
                print(f"no business.brands row for name={brand_name!r}", file=sys.stderr)
                sys.exit(2)
            brand_id = row[0]
            print(f"brand_id={brand_id} ({brand_name})")

            inserted = 0
            updated = 0
            for path in files:
                content_key, content_format, content, source_path = _file_to_row(path)
                cur.execute(
                    """
                    INSERT INTO business.brand_content (
                        brand_id, content_key, content_format, content, source_path
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (brand_id, content_key) DO UPDATE
                        SET content        = EXCLUDED.content,
                            content_format = EXCLUDED.content_format,
                            source_path    = EXCLUDED.source_path,
                            updated_at     = NOW()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (str(brand_id), content_key, content_format, content, source_path),
                )
                was_inserted = cur.fetchone()[0]
                if was_inserted:
                    inserted += 1
                else:
                    updated += 1
                print(f"  {content_key:30s} {content_format:5s} {len(content):>6d} chars  ({'new' if was_inserted else 'updated'})")
        conn.commit()

    print(f"\nsynced {inserted + updated} files for {slug}: {inserted} new, {updated} updated")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="brand slug — directory name under data/brands/")
    args = parser.parse_args()
    sync(args.slug)


if __name__ == "__main__":
    main()
