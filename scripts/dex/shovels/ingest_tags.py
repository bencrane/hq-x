"""Shovels tags ingest CLI → shovels_tags_lance.

Entity: tag dimension (list/tags, §8). PK/BTREE: ``id`` (the tag slug).
Endpoint: ``list/tags`` — a static catalog pull, FREE, single page of 22 tags.
No query input. The query spec is ignored (an empty ``{}`` is the norm).

    python -m scripts.shovels.ingest_tags --snapshot-date 2026-05-29 --apply
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.shovels._cli import run_entity_cli  # noqa: E402
from scripts.shovels._client import ShovelsClient  # noqa: E402
from scripts.shovels.entity_specs import TAG_SPEC  # noqa: E402
from scripts.shovels.query_spec import ShovelsQuerySpec  # noqa: E402

SOURCE_ENDPOINT = "list/tags"


def build_records(client: ShovelsClient, spec: ShovelsQuerySpec) -> Iterator[dict]:
    # Single free page; paginate() handles the envelope (next_cursor is null).
    yield from client.paginate(
        "/list/tags",
        base_params=[],
        size=spec.size if spec.size else 50,
        max_pages=spec.max_pages,
    )


if __name__ == "__main__":
    raise SystemExit(
        run_entity_cli(
            table="tags",
            spec=TAG_SPEC,
            source_endpoint=SOURCE_ENDPOINT,
            record_builder=build_records,
            doc="Shovels permit-tag vocabulary (list/tags) — static catalog; PK=tag id.",
        )
    )
