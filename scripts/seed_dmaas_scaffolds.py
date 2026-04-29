#!/usr/bin/env python3
"""Seed dmaas_scaffolds from data/dmaas_seed_scaffolds.json.

Idempotent: existing rows for the same `slug` get UPDATEd (keeping
version_number); new rows get INSERTed. Validates each scaffold's
constraint_specification by running the solver against every entry in
compatible_specs; refuses to seed a row that doesn't solve.

Usage:
    doppler run -- python3 scripts/seed_dmaas_scaffolds.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from app.db import close_pool, init_pool
from app.dmaas import repository as repo
from app.dmaas.dsl import ConstraintSpecification
from app.dmaas.service import (
    derive_intrinsics_from_content,
    resolve_spec_binding,
)
from app.dmaas.solver import solve

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "dmaas_seed_scaffolds.json"


async def seed_one(entry: dict) -> bool:
    slug = entry["slug"]
    print(f"\n=== {slug} ===")

    dsl = ConstraintSpecification.model_validate(entry["constraint_specification"])
    ref_errs = dsl.validate_references()
    if ref_errs:
        print("  DSL ref errors:", ref_errs)
        return False

    placeholder = entry.get("placeholder_content", {})
    for cs in entry.get("compatible_specs", []):
        binding = await resolve_spec_binding(cs["category"], cs["variant"])
        if binding is None:
            print(f"  unknown spec: {cs}")
            return False
        result = solve(
            dsl,
            zones=binding.zones,
            intrinsics=derive_intrinsics_from_content(dsl, placeholder),
            content=placeholder,
        )
        if not result.is_valid:
            print(f"  solver FAILED for {cs}:")
            for c in result.conflicts:
                print(f"    [{c.phase}] {c.constraint_type}: {c.message}")
            return False
        print(f"  solved for {cs}: {len(result.positions)} positions")

    existing = await repo.get_scaffold_by_slug(slug)
    if existing:
        await repo.update_scaffold(
            slug=slug,
            fields={
                "name": entry["name"],
                "description": entry.get("description"),
                "compatible_specs": entry.get("compatible_specs", []),
                "prop_schema": entry.get("prop_schema", {}),
                "constraint_specification": entry["constraint_specification"],
                "preview_image_url": entry.get("preview_image_url"),
                "vertical_tags": entry.get("vertical_tags", []),
                "is_active": True,
            },
        )
        print("  UPDATED existing scaffold")
    else:
        await repo.insert_scaffold(
            slug=slug,
            name=entry["name"],
            description=entry.get("description"),
            format=entry["format"],
            compatible_specs=entry.get("compatible_specs", []),
            prop_schema=entry.get("prop_schema", {}),
            constraint_specification=entry["constraint_specification"],
            preview_image_url=entry.get("preview_image_url"),
            vertical_tags=entry.get("vertical_tags", []),
            is_active=True,
            version_number=1,
            created_by_user_id=None,
        )
        print("  INSERTED new scaffold")
    return True


async def main():
    data = json.loads(SEED_PATH.read_text())
    await init_pool()
    try:
        ok = 0
        for entry in data["scaffolds"]:
            if await seed_one(entry):
                ok += 1
        total = len(data["scaffolds"])
        print(f"\n{ok}/{total} scaffolds seeded successfully")
        if ok != total:
            sys.exit(1)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
