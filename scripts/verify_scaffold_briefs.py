#!/usr/bin/env python3
"""Verify every brief in `data/dmaas_scaffold_briefs/` against its scaffold.

Reads each brief, finds the matching scaffold by slug in
`data/dmaas_v1_scaffolds.json` (the agent's authoring output), constructs
the MailerSpec(s) from `data/lob_mailer_specs.json` directly (no DB),
runs the solver against placeholder_content, and re-runs the brief's
acceptance_rules against resolved positions. Exits non-zero on any
failure. This is what CI invokes to keep the brief library and the
scaffold library in sync.

This script is *file-driven by design*: it does not need a live database
or a managed-agent session to verify the library. The seed script
(`scripts/seed_dmaas_v1_scaffolds.py`) writes the scaffolds to the DB
after the same checks; verify_scaffold_briefs is the CI-time gate.

Usage:
    uv run python -m scripts.verify_scaffold_briefs
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app.direct_mail.specs import MailerSpec
from app.dmaas.briefs import ScaffoldBrief, evaluate_rules
from app.dmaas.dsl import ConstraintSpecification
from app.dmaas.service import (
    bind_spec_zones,
    derive_intrinsics_from_content,
)
from app.dmaas.solver import solve

ROOT = Path(__file__).resolve().parent.parent
SCAFFOLDS_PATH = ROOT / "data" / "dmaas_v1_scaffolds.json"
BRIEFS_DIR = ROOT / "data" / "dmaas_scaffold_briefs"
SPECS_PATH = ROOT / "data" / "lob_mailer_specs.json"


def _spec_from_json(entry: dict[str, Any]) -> MailerSpec:
    """Build a MailerSpec from one entry in lob_mailer_specs.json."""
    return MailerSpec(
        id=f"{entry['mailer_category']}::{entry['variant']}",
        mailer_category=entry["mailer_category"],
        variant=entry["variant"],
        label=entry["label"],
        bleed_w_in=entry.get("bleed_w_in"),
        bleed_h_in=entry.get("bleed_h_in"),
        trim_w_in=float(entry["trim_w_in"]),
        trim_h_in=float(entry["trim_h_in"]),
        safe_inset_in=entry.get("safe_inset_in"),
        zones=entry.get("zones") or {},
        folding=entry.get("folding"),
        pagination=entry.get("pagination"),
        address_placement=entry.get("address_placement"),
        envelope=entry.get("envelope"),
        production=entry.get("production") or {},
        ordering=entry.get("ordering") or {},
        template_pdf_url=entry.get("template_pdf_url"),
        additional_template_urls=entry.get("additional_template_urls") or [],
        source_urls=entry.get("source_urls") or [],
        notes=entry.get("notes"),
        faces=entry.get("faces"),
    )


def _load_specs() -> dict[tuple[str, str], MailerSpec]:
    data = json.loads(SPECS_PATH.read_text())
    return {
        (s["mailer_category"], s["variant"]): _spec_from_json(s)
        for s in data["specs"]
    }


def _load_briefs() -> dict[str, ScaffoldBrief]:
    out: dict[str, ScaffoldBrief] = {}
    for p in sorted(BRIEFS_DIR.glob("*.json")):
        brief = ScaffoldBrief.model_validate_json(p.read_text())
        out[brief.slug] = brief
    return out


def _verify_scaffold(
    scaffold: dict[str, Any],
    brief: ScaffoldBrief,
    specs: dict[tuple[str, str], MailerSpec],
) -> tuple[bool, dict[str, dict[str, dict[str, float]]]]:
    """Returns (ok, positions_per_spec) where positions_per_spec is
    {variant_id: {element_name: {x,y,w,h}}} for sanity inspection."""
    slug = scaffold["slug"]
    cs_dict = scaffold["constraint_specification"]
    placeholder = scaffold.get("placeholder_content", {})
    print(f"\n=== {slug} ({brief.strategy} / {brief.face} / {brief.format}) ===")

    try:
        dsl = ConstraintSpecification.model_validate(cs_dict)
    except Exception as e:
        print(f"  ✗ DSL parse error: {e}")
        return False, {}

    ref_errors = dsl.validate_references()
    if ref_errors:
        for err in ref_errors:
            print(f"  ✗ DSL ref error: {err}")
        return False, {}

    all_ok = True
    positions_per_spec: dict[str, dict[str, dict[str, float]]] = {}
    for cs in scaffold["compatible_specs"]:
        key = (cs["category"], cs["variant"])
        spec = specs.get(key)
        if spec is None:
            print(f"  ✗ unknown spec: {cs}")
            all_ok = False
            continue

        binding = bind_spec_zones(spec)
        intrinsics = derive_intrinsics_from_content(dsl, placeholder)
        result = solve(
            dsl,
            zones=binding.zones,
            intrinsics=intrinsics,
            content=placeholder,
        )
        if not result.is_valid:
            print(f"  ✗ solver failed for {cs}:")
            for c in result.conflicts:
                print(f"      [{c.phase}] {c.constraint_type}: {c.message}")
            all_ok = False
            continue
        positions = result.positions_dict()
        positions_per_spec[f"{cs['category']}/{cs['variant']}"] = positions
        print(f"  ✓ solved for {cs}: {len(positions)} elements")

        # Compare prop_schema against brief's required_slots / optional_slots
        prop_schema = scaffold.get("prop_schema") or {}
        properties = (prop_schema or {}).get("properties") or {}
        for slot in brief.required_slots:
            if slot not in properties:
                print(f"  ✗ required_slot {slot!r} missing from prop_schema.properties")
                all_ok = False

        # Run brief's acceptance_rules against this resolve.
        failures = evaluate_rules(
            brief.acceptance_rules,
            positions=positions,
            prop_schema=prop_schema,
        )
        if failures:
            for f in failures:
                print(f"  ✗ acceptance rule {f.rule_type}: {f.message}")
            all_ok = False
        else:
            print(f"  ✓ {len(brief.acceptance_rules)} acceptance rule(s) passed")

    return all_ok, positions_per_spec


def main() -> int:
    specs = _load_specs()
    briefs = _load_briefs()
    scaffolds = json.loads(SCAFFOLDS_PATH.read_text())["scaffolds"]

    if len(scaffolds) != len(briefs):
        print(f"ERROR: scaffold count ({len(scaffolds)}) != brief count ({len(briefs)})")
        return 2

    sample_positions: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    failed_slugs: list[str] = []
    for scaffold in scaffolds:
        slug = scaffold["slug"]
        brief = briefs.get(slug)
        if brief is None:
            print(f"ERROR: no brief found for scaffold {slug!r}")
            return 2
        ok, positions = _verify_scaffold(scaffold, brief, specs)
        if not ok:
            failed_slugs.append(slug)
        else:
            sample_positions[slug] = positions

    print("\n" + "=" * 60)
    if failed_slugs:
        print(f"FAILED: {len(failed_slugs)} of {len(scaffolds)} scaffolds")
        for slug in failed_slugs:
            print(f"  - {slug}")
        return 1
    print(f"OK: {len(scaffolds)}/{len(scaffolds)} scaffolds solve and meet acceptance rules")

    # Print sample positions for one postcard + one self-mailer for the PR.
    pc_sample = next(
        (s for s in sample_positions if s.endswith("postcard-front-6x9")), None
    )
    sm_sample = next(
        (s for s in sample_positions if "self-mailer" in s), None
    )
    print("\nSample resolved positions (for PR sanity):")
    for slug in (pc_sample, sm_sample):
        if slug is None:
            continue
        for variant_id, positions in sample_positions[slug].items():
            print(f"\n  {slug} @ {variant_id}:")
            for name, rect in sorted(positions.items()):
                print(
                    f"    {name:20s}  x={rect['x']:>7.1f}  y={rect['y']:>7.1f}"
                    f"  w={rect['w']:>7.1f}  h={rect['h']:>7.1f}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
