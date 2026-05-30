"""Shovels employees ingest CLI → shovels_employees_lance.

Entity: employee (Employees, §6.4, PII). PK/BTREE: ``id`` (+ ``contractor_id``).
Endpoint: ``contractors/{id}/employees`` — fanned out over a list of
contractor_ids from the query spec. Billed 1/record; many small/owner-operator
contractors return an empty list (§6.4) — handled as zero rows for that id.

Query spec (required): ``contractor_ids`` (non-empty). A scheduler typically
sources these from a prior contractors pull (e.g. each contractor ``id`` landed
in shovels_contractors_lance).

    python -m scripts.shovels.ingest_employees \\
        --query-spec '{"contractor_ids":["1tVwtM9LC0"],"size":10}' \\
        --snapshot-date 2026-05-29 --apply
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.shovels._cli import run_entity_cli  # noqa: E402
from scripts.shovels._client import ShovelsAPIError, ShovelsClient  # noqa: E402
from scripts.shovels.entity_specs import EMPLOYEE_SPEC  # noqa: E402
from scripts.shovels.query_spec import ShovelsQuerySpec  # noqa: E402

LOG = logging.getLogger("shovels.ingest.employees")
SOURCE_ENDPOINT = "contractors/{id}/employees"


def build_records(client: ShovelsClient, spec: ShovelsQuerySpec) -> Iterator[dict]:
    contractor_ids = [c for c in (spec.contractor_ids or []) if c]
    if not contractor_ids:
        raise SystemExit("FAIL: employees ingest requires query-spec 'contractor_ids' (non-empty)")
    for cid in contractor_ids:
        try:
            count = 0
            for rec in client.paginate(
                f"/contractors/{cid}/employees",
                base_params=[],
                size=spec.size,
                max_pages=spec.max_pages,
            ):
                # Ensure contractor_id is present even if the upstream omits it
                # for some rows (the path id is authoritative).
                rec.setdefault("contractor_id", cid)
                count += 1
                yield rec
            LOG.info("contractor %s -> %d employee(s)", cid, count)
        except ShovelsAPIError as exc:
            # 404/empty is "no employees" for that contractor — skip, don't abort
            # the whole fan-out.
            LOG.warning("contractor %s employees fetch failed (%s) — skipping", cid, exc)
            continue


if __name__ == "__main__":
    raise SystemExit(
        run_entity_cli(
            table="employees",
            spec=EMPLOYEE_SPEC,
            source_endpoint=SOURCE_ENDPOINT,
            record_builder=build_records,
            doc="Shovels employees (Employees, PII) per contractor — verbatim raw + typed; PK=employee id.",
        )
    )
