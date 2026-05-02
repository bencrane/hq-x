"""Register a GTM-pipeline agent in business.gtm_agent_registry.

Companion to managed-agents-x/scripts/setup_gtm_agents.py: that script
mints the Anthropic agent + prints a copy-paste command that calls
this one. This script:

  1. Upserts a row into business.gtm_agent_registry with the slug + role
     + anthropic_agent_id + parent_actor_slug + model.
  2. Reads the system_prompt from the matching managed-agents-x file
     (resolved by checking siblings of the hq-x repo for a
     managed-agents-x/data/agents/<slug>/system_prompt.md), and seeds a
     row into business.agent_prompt_versions with
     activation_source='setup_script'. This gives the version-history UI
     a v1 entry from the moment of registration.

Usage:
  doppler --project hq-x --config dev run -- \\
      uv run python -m scripts.register_gtm_agent \\
        gtm-sequence-definer agt_xyz123 actor [--parent gtm-sequence-definer]

  --parent is required for role in {verdict, critic}.
  --model defaults to claude-opus-4-7.

Resolves the system_prompt path by trying these candidate locations
relative to the hq-x repo root:
  ../managed-agents-x/data/agents/<slug>/system_prompt.md
  ../../managed-agents-x/data/agents/<slug>/system_prompt.md  (worktree case)

If neither exists, the registry row is still inserted but the
prompt-version row is skipped with a warning.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID  # noqa: F401  (re-exported by services)

from app.services import agent_prompts


def _resolve_prompt_file(slug: str) -> Path | None:
    """Walk up to find managed-agents-x/data/agents/<slug>/system_prompt.md."""
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    for depth in (1, 2, 3, 4):
        root = here
        for _ in range(depth):
            root = root.parent
        candidates.append(
            root / "managed-agents-x" / "data" / "agents" / slug / "system_prompt.md"
        )
        candidates.append(
            root.parent / "managed-agents-x" / "data" / "agents" / slug / "system_prompt.md"
        )
    seen: set[str] = set()
    for c in candidates:
        s = str(c)
        if s in seen:
            continue
        seen.add(s)
        if c.is_file():
            return c
    return None


async def _register(
    *,
    slug: str,
    anthropic_agent_id: str,
    role: str,
    parent_actor_slug: str | None,
    model: str,
    description: str | None,
) -> None:
    row = await agent_prompts.upsert_registry_row(
        agent_slug=slug,
        anthropic_agent_id=anthropic_agent_id,
        role=role,
        parent_actor_slug=parent_actor_slug,
        model=model,
        description=description,
    )
    print(f"registry row upserted: id={row['id']} slug={row['agent_slug']}")

    prompt_path = _resolve_prompt_file(slug)
    if prompt_path is None:
        print(
            f"WARNING: could not resolve system_prompt.md for slug={slug!r}; "
            "skipping prompt-version seed",
            file=sys.stderr,
        )
        return
    prompt_text = prompt_path.read_text()
    version = await agent_prompts.record_setup_script_version(
        agent_slug=slug,
        anthropic_agent_id=anthropic_agent_id,
        system_prompt=prompt_text,
        notes=f"seeded from {prompt_path.relative_to(Path.cwd()) if prompt_path.is_absolute() else prompt_path}",
    )
    print(
        f"prompt version seeded: id={version['id']} "
        f"version_index={version['version_index']} "
        f"chars={len(prompt_text)}"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug", help="agent slug (e.g. gtm-sequence-definer)")
    p.add_argument("anthropic_agent_id", help="agent id from Anthropic (agt_*)")
    p.add_argument(
        "role", choices=("actor", "verdict", "critic", "orchestrator")
    )
    p.add_argument(
        "--parent",
        dest="parent_actor_slug",
        default=None,
        help="parent actor slug (required for verdict / critic)",
    )
    p.add_argument("--model", default="claude-opus-4-7")
    p.add_argument("--description", default=None)
    args = p.parse_args()

    if args.role in ("verdict", "critic") and not args.parent_actor_slug:
        p.error(f"role={args.role} requires --parent <actor_slug>")

    asyncio.run(
        _register(
            slug=args.slug,
            anthropic_agent_id=args.anthropic_agent_id,
            role=args.role,
            parent_actor_slug=args.parent_actor_slug,
            model=args.model,
            description=args.description,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
