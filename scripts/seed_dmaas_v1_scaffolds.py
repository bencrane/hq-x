#!/usr/bin/env python3
"""Seed the v1 DMaaS scaffold library into `dmaas_scaffolds`.

Reads `data/dmaas_v1_scaffolds.json` (the agent's authoring output) +
`data/dmaas_scaffold_briefs/*.json` (the human-reviewable briefs) and:

  1. Validates each scaffold's DSL parses and references resolve.
  2. Runs the solver against every entry in `compatible_specs` using the
     scaffold's `placeholder_content`. Refuses to seed if any combination
     doesn't solve.
  3. Runs the matching brief's `acceptance_rules` against the resolved
     positions. Refuses to seed if any rule fails.
  4. Records an entry in `dmaas_scaffold_authoring_sessions` with the
     proposed DSL + outcome (audit trail).
  5. UPSERTs into `dmaas_scaffolds` (idempotent: existing slug → UPDATE,
     new slug → INSERT with version_number=1).

Failures abort that scaffold but the script continues to the next.
Exits non-zero if any scaffold failed to seed.

Usage:
    doppler run --project hq-x --config dev -- \
        uv run python -m scripts.seed_dmaas_v1_scaffolds
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.db import close_pool, init_pool
from app.dmaas import repository as repo
from app.dmaas.briefs import ScaffoldBrief, evaluate_rules
from app.dmaas.dsl import ConstraintSpecification
from app.dmaas.service import (
    derive_intrinsics_from_content,
    resolve_spec_binding,
)
from app.dmaas.solver import solve

ROOT = Path(__file__).resolve().parent.parent
SCAFFOLDS_PATH = ROOT / "data" / "dmaas_v1_scaffolds.json"
BRIEFS_DIR = ROOT / "data" / "dmaas_scaffold_briefs"


def _load_briefs() -> dict[str, ScaffoldBrief]:
    out: dict[str, ScaffoldBrief] = {}
    for p in sorted(BRIEFS_DIR.glob("*.json")):
        brief = ScaffoldBrief.model_validate_json(p.read_text())
        out[brief.slug] = brief
    return out


async def _seed_one(scaffold: dict[str, Any], brief: ScaffoldBrief) -> bool:
    slug = scaffold["slug"]
    print(f"\n=== {slug} ({brief.strategy} / {brief.face} / {brief.format}) ===")

    cs_dict = scaffold["constraint_specification"]
    placeholder = scaffold.get("placeholder_content", {})
    prop_schema = scaffold.get("prop_schema") or {}

    try:
        dsl = ConstraintSpecification.model_validate(cs_dict)
    except Exception as e:
        print(f"  ✗ DSL parse error: {e}")
        await _record_session(brief, cs_dict, accepted=False, notes=f"parse_error: {e}")
        return False

    ref_errors = dsl.validate_references()
    if ref_errors:
        for err in ref_errors:
            print(f"  ✗ DSL ref error: {err}")
        await _record_session(
            brief, cs_dict, accepted=False, notes=f"ref_errors: {ref_errors!r}"
        )
        return False

    # Solve + acceptance check across every compatible_spec.
    for cs in scaffold["compatible_specs"]:
        binding = await resolve_spec_binding(cs["category"], cs["variant"])
        if binding is None:
            print(f"  ✗ unknown spec: {cs}")
            await _record_session(
                brief, cs_dict, accepted=False, notes=f"unknown_spec: {cs!r}"
            )
            return False
        intrinsics = derive_intrinsics_from_content(dsl, placeholder)
        result = solve(
            dsl, zones=binding.zones, intrinsics=intrinsics, content=placeholder
        )
        if not result.is_valid:
            print(f"  ✗ solver failed for {cs}:")
            for c in result.conflicts:
                print(f"      [{c.phase}] {c.constraint_type}: {c.message}")
            await _record_session(
                brief,
                cs_dict,
                accepted=False,
                notes=f"unsolved on {cs!r}: {len(result.conflicts)} conflict(s)",
            )
            return False
        positions = result.positions_dict()
        failures = evaluate_rules(
            brief.acceptance_rules,
            positions=positions,
            prop_schema=prop_schema,
        )
        if failures:
            for f in failures:
                print(f"  ✗ acceptance rule {f.rule_type}: {f.message}")
            await _record_session(
                brief,
                cs_dict,
                accepted=False,
                notes=f"acceptance_failed on {cs!r}: {[f.rule_type for f in failures]}",
            )
            return False
        print(f"  ✓ solved + accepted for {cs} ({len(positions)} elements)")

    # All checks passed — UPSERT.
    existing = await repo.get_scaffold_by_slug(slug)
    if existing:
        await repo.update_scaffold(
            slug=slug,
            fields={
                "name": scaffold["name"],
                "description": scaffold.get("description"),
                "strategy": scaffold.get("strategy"),
                "compatible_specs": scaffold.get("compatible_specs", []),
                "prop_schema": prop_schema,
                "constraint_specification": cs_dict,
                "preview_image_url": scaffold.get("preview_image_url"),
                "vertical_tags": scaffold.get("vertical_tags", []),
                "is_active": True,
            },
        )
        print("  → UPDATED existing scaffold")
        saved_id = existing.id
    else:
        saved = await repo.insert_scaffold(
            slug=slug,
            name=scaffold["name"],
            description=scaffold.get("description"),
            format=scaffold["format"],
            strategy=scaffold.get("strategy"),
            compatible_specs=scaffold.get("compatible_specs", []),
            prop_schema=prop_schema,
            constraint_specification=cs_dict,
            preview_image_url=scaffold.get("preview_image_url"),
            vertical_tags=scaffold.get("vertical_tags", []),
            is_active=True,
            version_number=1,
            created_by_user_id=None,
        )
        print(f"  → INSERTED new scaffold (id={saved.id})")
        saved_id = saved.id

    await _record_session(
        brief, cs_dict, accepted=True, scaffold_id=saved_id,
        notes=f"seeded ok ({len(scaffold['compatible_specs'])} compatible spec(s))",
    )
    return True


async def _record_session(
    brief: ScaffoldBrief,
    proposed: dict[str, Any],
    *,
    accepted: bool,
    notes: str,
    scaffold_id=None,
) -> None:
    """Audit-trail record of an authoring attempt for this brief."""
    prompt = (
        f"strategy={brief.strategy} face={brief.face} format={brief.format} "
        f"slug={brief.slug} thesis={brief.thesis[:80]!r}"
    )
    await repo.insert_authoring_session(
        scaffold_id=scaffold_id,
        prompt=prompt,
        proposed_constraint_specification=proposed,
        accepted=accepted,
        notes=notes,
        created_by_user_id=None,
    )


async def main() -> int:
    scaffolds = json.loads(SCAFFOLDS_PATH.read_text())["scaffolds"]
    briefs = _load_briefs()

    if len(scaffolds) != len(briefs):
        print(
            f"ERROR: scaffold count ({len(scaffolds)}) != brief count ({len(briefs)})"
        )
        return 2

    await init_pool()
    try:
        ok_count = 0
        failed_slugs = []
        for scaffold in scaffolds:
            slug = scaffold["slug"]
            brief = briefs.get(slug)
            if brief is None:
                print(f"\nERROR: no brief for scaffold {slug!r}")
                failed_slugs.append(slug)
                continue
            if await _seed_one(scaffold, brief):
                ok_count += 1
            else:
                failed_slugs.append(slug)
        print("\n" + "=" * 60)
        print(f"seeded {ok_count}/{len(scaffolds)} scaffolds")
        if failed_slugs:
            print("FAILED:")
            for s in failed_slugs:
                print(f"  - {s}")
            return 1
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
