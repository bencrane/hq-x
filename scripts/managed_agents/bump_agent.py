"""Bump the gtm-agent to the next version with the `present_result` custom tool.

Idempotent: reads the live agent first, compares tools array + system prompt
against the desired shape, and only POSTs if anything differs. A no-op run
exits 0 with a "reused vN" line.

Usage::

    doppler run --project hq-all --config prd -- \
        uv run python -m scripts.managed_agents.bump

Anthropic's ``POST /v1/agents/{id}`` is destructive on the fields supplied
(per docs / mirroring app/services/anthropic_managed_agents.py docstring).
We always send the FULL desired shape so partial-update drift is impossible.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from anthropic import Anthropic

from scripts.managed_agents.result_types import (
    PRESENT_RESULT_INPUT_SCHEMA,
    PRESENT_RESULT_TOOL_DESCRIPTION,
    PRESENT_RESULT_TOOL_NAME,
    build_full_system_prompt,
    present_result_tool_def,
)

AGENT_TOOLSET_TYPE = "agent_toolset_20260401"
POLARIS_MCP_SERVER_NAME = "polaris"


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_MANAGED_AGENTS_API_KEY")
    if not key:
        sys.exit(
            "ERROR: ANTHROPIC_MANAGED_AGENTS_API_KEY not set. "
            "Run inside `doppler run --project hq-all --config prd -- ...`"
        )
    return key


def _agent_id() -> str:
    aid = os.environ.get("MANAGED_AGENT_ID_GTM")
    if not aid:
        sys.exit(
            "ERROR: MANAGED_AGENT_ID_GTM not set. "
            "Inject it from Doppler (provisioned by scripts/managed_agents/provision.py)."
        )
    return aid


def _polaris_mcp_url() -> str | None:
    """Optional — when set, the bumped agent gains the polaris MCP server
    + mcp_toolset binding. When unset, the agent stays at the present_result-
    only shape and a warning prints. Callers running bump_agent BEFORE
    deploying polaris-mcp get the partial shape; the bump becomes a true
    no-op once Stage 5 is fully wired."""
    return os.environ.get("GTM_MCP_URL")


def desired_mcp_servers() -> list[dict]:
    """Polaris MCP server block. Empty list if GTM_MCP_URL not set."""
    url = _polaris_mcp_url()
    if not url:
        return []
    return [
        {
            "type": "url",
            "name": POLARIS_MCP_SERVER_NAME,
            "url": url,
        }
    ]


def desired_tools() -> list[dict]:
    """Tools array — always includes agent_toolset + present_result custom
    tool. Stage 5 adds the polaris mcp_toolset binding when GTM_MCP_URL
    is set (otherwise the mcp_toolset entry would reference a non-existent
    server and Anthropic would reject the create)."""
    tools: list[dict] = [
        {"type": AGENT_TOOLSET_TYPE},
        present_result_tool_def(),
    ]
    if _polaris_mcp_url():
        tools.append({
            "type": "mcp_toolset",
            "mcp_server_name": POLARIS_MCP_SERVER_NAME,
        })
    return tools


def _mcp_servers_match(actual: list, desired: list[dict]) -> bool:
    """Subset-match for mcp_servers — same pattern as _tools_match.

    Discriminator is ``name``; we verify ``type`` + ``url`` match. Anthropic
    may auto-populate extra fields (auth scope, last health check, etc.);
    those are not part of our desired shape and are ignored.
    """
    def _normalize(items: list) -> list[dict]:
        out: list[dict] = []
        for it in items:
            if hasattr(it, "model_dump"):
                out.append(it.model_dump(exclude_none=True))
            elif isinstance(it, dict):
                out.append(it)
            else:
                out.append({k: v for k, v in vars(it).items() if not k.startswith("_")})
        return out

    actual_norm = _normalize(actual)
    if len(desired) == 0:
        # We want NO mcp_servers. Anthropic returns an empty list for that.
        return len(actual_norm) == 0

    for d in desired:
        match = next((a for a in actual_norm if a.get("name") == d.get("name")), None)
        if match is None:
            return False
        for k in ("type", "url"):
            if d.get(k) != match.get(k):
                return False
    return True


def _tools_match(actual: list, desired: list[dict]) -> bool:
    """Subset-match: every desired tool exists in actual with matching keys.

    Anthropic auto-populates fields the caller didn't set — `configs`,
    `default_config`, `permission_policy` on `agent_toolset_20260401`,
    for example. Exact equality would always be False after a successful
    update. So: for each desired tool, find an actual tool with the same
    discriminator (`type`, plus `name` for custom tools) and verify that
    every key we explicitly set matches.
    """
    def _normalize(items: list) -> list[dict]:
        out: list[dict] = []
        for it in items:
            if hasattr(it, "model_dump"):
                out.append(it.model_dump(exclude_none=True))
            elif isinstance(it, dict):
                out.append(it)
            else:
                out.append({k: v for k, v in vars(it).items() if not k.startswith("_")})
        return out

    actual_norm = _normalize(actual)

    def _find_matching(desired_tool: dict) -> dict | None:
        for cand in actual_norm:
            if cand.get("type") != desired_tool.get("type"):
                continue
            # For custom tools, also discriminate on name.
            if desired_tool.get("type") == "custom":
                if cand.get("name") != desired_tool.get("name"):
                    continue
            return cand
        return None

    def _subset_equal(desired_v: object, actual_v: object) -> bool:
        """Deep-equal: dicts match on every key we specified (subset
        semantics — actual may have extra keys); lists / scalars exact."""
        if isinstance(desired_v, dict):
            if not isinstance(actual_v, dict):
                return False
            for k, dv in desired_v.items():
                if k not in actual_v:
                    return False
                if not _subset_equal(dv, actual_v[k]):
                    return False
            return True
        # Lists are exact-equal (order + length matter for schema fields).
        return desired_v == actual_v

    for d_tool in desired:
        match = _find_matching(d_tool)
        if match is None:
            return False
        for k, v in d_tool.items():
            if not _subset_equal(v, match.get(k)):
                return False
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the desired vs. current state and exit without writing.",
    )
    args = p.parse_args()

    client = Anthropic(api_key=_api_key())
    agent_id = _agent_id()

    current = client.beta.agents.retrieve(agent_id)
    current_version = current.version
    current_tools = current.tools or []
    current_system = current.system or ""
    current_mcp_servers = getattr(current, "mcp_servers", None) or []

    desired = desired_tools()
    desired_mcp = desired_mcp_servers()
    desired_system = build_full_system_prompt(polaris_enabled=bool(_polaris_mcp_url()))
    tools_ok = _tools_match(current_tools, desired)
    system_ok = current_system == desired_system
    mcp_ok = _mcp_servers_match(current_mcp_servers, desired_mcp)

    polaris_url = _polaris_mcp_url()
    print(f"# bump gtm-agent ({agent_id})")
    print(f"#   current version: {current_version}")
    print(f"#   tools match:     {tools_ok}")
    print(f"#   system match:    {system_ok}")
    print(f"#   mcp match:       {mcp_ok}  "
          f"({'polaris @ ' + polaris_url if polaris_url else 'GTM_MCP_URL not set — polaris MCP skipped'})")
    print()

    if tools_ok and system_ok and mcp_ok:
        print(f"=== NO-OP: agent already at desired shape (version {current_version}) ===")
        print(f"MANAGED_AGENT_ID_GTM={agent_id}")
        print(f"AGENT_VERSION={current_version}")
        return 0

    if args.dry_run:
        print("WOULD UPDATE with:")
        print(f"  tools       = {json.dumps(desired, indent=2)[:600]}...")
        print(f"  mcp_servers = {json.dumps(desired_mcp, indent=2)}")
        print(f"  system      = {desired_system[:240]!r}...")
        return 0

    # `version` is an optimistic-concurrency CAS check — the caller asserts
    # which version they read from before updating. Pass the version we
    # just retrieved. ``mcp_servers=[]`` clears any existing servers (we
    # always send the full intended shape).
    updated = client.beta.agents.update(
        agent_id,
        version=current_version,
        tools=desired,
        system=desired_system,
        mcp_servers=desired_mcp,
    )

    print("=" * 64)
    print("UPDATED")
    print("=" * 64)
    print(f"MANAGED_AGENT_ID_GTM={agent_id}    # [updated]")
    print(f"AGENT_VERSION={updated.version}    # was {current_version}")
    print()
    print("Sessions opened against the bare agent ID auto-pick the latest")
    print("version. To pin to this version explicitly, pass")
    print(f"  agent={{'type': 'agent', 'id': '{agent_id}', 'version': {updated.version}}}")
    print("when creating a session.")
    print()
    print(f"present_result tool registered:")
    print(f"  name={PRESENT_RESULT_TOOL_NAME}")
    print(f"  input_schema keys={list(PRESENT_RESULT_INPUT_SCHEMA['properties'].keys())}")
    print(f"  description (first 200 chars): {PRESENT_RESULT_TOOL_DESCRIPTION[:200]!r}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
