"""Provision the gtm-agent + gtm-env resources in Anthropic Managed Agents.

Idempotent: lists existing resources first, reuses any whose name matches
exactly; creates only when missing. Prints the resolved IDs at the end so
the operator can drop them into Doppler as ``MANAGED_AGENT_ID_GTM`` and
``MANAGED_ENVIRONMENT_ID_GTM``.

Usage::

    doppler run --project hq-all --config prd -- \
        uv run python -m scripts.managed_agents.provision

Reads ``ANTHROPIC_MANAGED_AGENTS_API_KEY`` from the environment (mirror
the existing httpx-based service at app/services/anthropic_managed_agents.py
so a single key drives all MAGS calls).

Model: ``claude-sonnet-4-6`` per the Managed Agents support matrix
(docs require Claude 4.5+; the operator-supplied ``claude-3-7-sonnet-latest``
/ ``claude-3-5-sonnet-latest`` IDs are not on the supported list).
"""
from __future__ import annotations

import argparse
import os
import sys

from anthropic import Anthropic

from scripts.managed_agents.result_types import BASE_SYSTEM_PROMPT

AGENT_NAME = "gtm-agent"
ENV_NAME = "gtm-env"
MODEL_ID = "claude-sonnet-4-6"
# Seed prompt at create time. The full prompt (polaris workflow +
# present_result guidance) is applied afterwards by reconcile.py from the
# agents.yaml manifest; this is just the base so there is a single source of
# the base text (result_types.BASE_SYSTEM_PROMPT) rather than a third copy.
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT
TOOLSET_TYPE = "agent_toolset_20260401"


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_MANAGED_AGENTS_API_KEY")
    if not key:
        sys.exit(
            "ERROR: ANTHROPIC_MANAGED_AGENTS_API_KEY not set. "
            "Run inside `doppler run --project hq-all --config prd -- ...`"
        )
    return key


def _find_by_name(items, name: str):
    """Return the single matching item by `.name`, or None. Errors on >1 match."""
    matches = [it for it in items if getattr(it, "name", None) == name]
    if len(matches) > 1:
        sys.exit(
            f"ERROR: found {len(matches)} resources named {name!r}; "
            "clean up duplicates before re-running provision."
        )
    return matches[0] if matches else None


def ensure_environment(client: Anthropic) -> tuple[str, str]:
    """Return (environment_id, action) where action in {'reused', 'created'}."""
    existing = list(client.beta.environments.list(limit=100))
    found = _find_by_name(existing, ENV_NAME)
    if found is not None:
        return found.id, "reused"
    env = client.beta.environments.create(
        name=ENV_NAME,
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
        },
    )
    return env.id, "created"


def ensure_agent(client: Anthropic) -> tuple[str, int, str]:
    """Return (agent_id, version, action) where action in {'reused', 'created'}."""
    existing = list(client.beta.agents.list(limit=100))
    found = _find_by_name(existing, AGENT_NAME)
    if found is not None:
        return found.id, found.version, "reused"
    agent = client.beta.agents.create(
        name=AGENT_NAME,
        model=MODEL_ID,
        system=SYSTEM_PROMPT,
        tools=[{"type": TOOLSET_TYPE}],
    )
    return agent.id, agent.version, "created"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List existing resources and report what would happen; no writes.",
    )
    args = p.parse_args()

    client = Anthropic(api_key=_api_key())

    print(f"# Provisioning Managed Agents resources (dry_run={args.dry_run})")
    print(f"#   environment: {ENV_NAME}")
    print(f"#   agent:       {AGENT_NAME} ({MODEL_ID})")
    print()

    if args.dry_run:
        envs = list(client.beta.environments.list(limit=100))
        agents = list(client.beta.agents.list(limit=100))
        env_match = _find_by_name(envs, ENV_NAME)
        agent_match = _find_by_name(agents, AGENT_NAME)
        print(f"environments visible: {len(envs)}")
        print(f"  {ENV_NAME}: {'EXISTS id=' + env_match.id if env_match else 'MISSING (would create)'}")
        print(f"agents visible: {len(agents)}")
        print(f"  {AGENT_NAME}: {'EXISTS id=' + agent_match.id + ' version=' + str(agent_match.version) if agent_match else 'MISSING (would create)'}")
        return 0

    env_id, env_action = ensure_environment(client)
    agent_id, agent_version, agent_action = ensure_agent(client)

    print("=" * 64)
    print("PROVISIONED")
    print("=" * 64)
    print(f"MANAGED_ENVIRONMENT_ID_GTM={env_id}    # [{env_action}]")
    print(f"MANAGED_AGENT_ID_GTM={agent_id}    # [{agent_action}] version={agent_version}")
    print()
    print("Add both to Doppler (hq-all/prd):")
    print(f"  doppler secrets set --project hq-all --config prd \\")
    print(f"    MANAGED_ENVIRONMENT_ID_GTM={env_id} \\")
    print(f"    MANAGED_AGENT_ID_GTM={agent_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
