"""Reconcile the live gtm-agent to the code-owned shape in ``agents.yaml``.

Single source of truth, read-merge-write. Replaces the two prior bump
scripts (``bump.py`` / ``bump_agent.py``), both of which built an
``mcp_servers`` array from constants and could strip the 6 non-gtm MCP
bindings (and ``bump.py`` additionally dropped every ``mcp_toolset`` and
downgraded the prompt).

Design — why this can't clobber live state
-------------------------------------------
Anthropic's ``POST /v1/agents/{id}`` (SDK ``agents.update``) is destructive
ONLY on the fields it receives; omitted fields are left untouched. The SDK
makes omission first-class (each mutable field defaults to ``anthropic.Omit``).
This reconciler exploits that:

  * It GETs the live agent first and treats that read as the base.
  * ``mcp_servers`` is MERGED by ``name``: managed entries take the manifest's
    ``{type,url}``; ``reconcile: never`` and unmanaged-live entries are passed
    through VERBATIM from the live read. So even on a write, ``railway`` (whose
    ``mcp_oauth`` binding is browser-minted and not reproducible headlessly)
    survives byte-for-byte. We NEVER build an mcp_servers array from constants
    alone.
  * Each field is diffed independently against the live read. A field is sent
    ONLY when it actually differs. On a true no-op the POST is skipped entirely.
  * ``tools`` is code-owned (it carries ``present_result``) and full-replaced
    when it changes — but it is diffed against a captured fixture
    (``fixtures/gtm_agent_live.json``), not reconstructed-and-compared, so an
    Anthropic-side field-population change (the auto-added ``configs`` /
    ``default_config`` on toolset entries) can't masquerade as a code change
    and trigger a spurious clobber.

Usage::

    # dry-run: real object diff, zero writes (the default safe mode)
    doppler run --project hq-all --config prd -- \
        uv run python -m scripts.managed_agents.reconcile --dry-run

    # apply: writes only the fields that actually changed (CAS on version)
    doppler run --project hq-all --config prd -- \
        uv run python -m scripts.managed_agents.reconcile

    # re-capture the ground-truth fixture from live (after an intentional change)
    doppler run --project hq-all --config prd -- \
        uv run python -m scripts.managed_agents.reconcile --capture
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic

from scripts.managed_agents.result_types import (
    build_full_system_prompt,
    present_result_tool_def,
)

_HERE = Path(__file__).resolve().parent
MANIFEST_PATH = _HERE / "agents.yaml"
FIXTURE_PATH = _HERE / "fixtures" / "gtm_agent_live.json"

# Anthropic auto-populates these on toolset entries (agent_toolset_*, mcp_toolset)
# after a successful write. They are part of the captured fixture, not part of
# the code-owned semantic shape, so the reconciler does not author them.
_DEFAULT_TOOLSET_CONFIG = {
    "configs": [],
    "default_config": {
        "enabled": True,
        "permission_policy": {"type": "always_allow"},
    },
}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Manifest + fixture
# ---------------------------------------------------------------------------


def load_manifest() -> dict[str, Any]:
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def load_fixture() -> dict[str, Any]:
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def _normalize(obj: Any) -> Any:
    """Coerce SDK models / nested objects into plain JSON-able structures.

    ``exclude_none`` drops unset optionals so they don't count as diffs.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Desired shape (built from manifest ⊕ live read)
# ---------------------------------------------------------------------------


def desired_mcp_servers(manifest: dict[str, Any], live_servers: list[dict]) -> list[dict]:
    """Merge the manifest's managed entries onto the live read BY NAME.

    * managed   → manifest's ``{name, type, url}`` (authoritative transport config)
    * never     → live entry passed through verbatim (asserted present)
    * unmanaged → any live entry not named in the manifest is appended verbatim

    The result is the FULL intended array (it always contains every live
    entry, never-reconciled ones byte-for-byte), so sending it can only
    correct a managed drift — it can never drop ``railway`` or an unknown
    server someone added out-of-band.
    """
    live_by_name = {s["name"]: s for s in live_servers}
    out: list[dict] = []
    manifest_names: set[str] = set()

    for entry in manifest.get("mcp_servers", []):
        name = entry["name"]
        manifest_names.add(name)
        mode = entry.get("reconcile", "managed")
        if mode == "never":
            live = live_by_name.get(name)
            if live is None:
                sys.exit(
                    f"ERROR: mcp_server {name!r} is reconcile:never but is ABSENT "
                    f"from the live agent. It carries non-reproducible state "
                    f"(e.g. mcp_oauth) and cannot be recreated headlessly. "
                    f"Restore it via the Anthropic console before reconciling."
                )
            out.append(dict(live))  # verbatim pass-through
        elif mode == "managed":
            out.append({"name": name, "type": entry["type"], "url": entry["url"]})
        else:
            sys.exit(f"ERROR: unknown reconcile mode {mode!r} for mcp_server {name!r}.")

    # Preserve any live server the manifest doesn't know about (out-of-band add).
    for s in live_servers:
        if s["name"] not in manifest_names:
            out.append(dict(s))

    return out


def desired_tools(manifest: dict[str, Any], server_names: list[str]) -> list[dict]:
    """Code-owned tools array.

    Order is fixed and deterministic: agent_toolset, present_result, then one
    always-allow ``mcp_toolset`` per server (in the desired mcp_servers order,
    which already includes never-reconciled + unmanaged entries). The toolset
    entries carry the same default config Anthropic auto-populates, so a
    full-replace round-trips to the identical live shape.
    """
    toolset_type = manifest["tools"]["agent_toolset_type"]
    tools: list[dict] = [
        {"type": toolset_type, **_DEFAULT_TOOLSET_CONFIG},
        present_result_tool_def(),
    ]
    for name in server_names:
        tools.append({
            "type": "mcp_toolset",
            "mcp_server_name": name,
            **_DEFAULT_TOOLSET_CONFIG,
        })
    return tools


def desired_model(manifest: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in manifest["model"].items() if v is not None}


def desired_system(manifest: dict[str, Any]) -> str:
    return build_full_system_prompt(
        polaris_enabled=bool(manifest["system"]["polaris_enabled"])
    )


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _obj_diff(label: str, current: Any, desired: Any) -> str | None:
    """Return a unified diff of two JSON-able objects, or None if identical."""
    cur = json.dumps(_normalize(current), indent=2, sort_keys=True).splitlines()
    des = json.dumps(_normalize(desired), indent=2, sort_keys=True).splitlines()
    if cur == des:
        return None
    diff = difflib.unified_diff(
        cur, des, fromfile=f"live.{label}", tofile=f"desired.{label}", lineterm=""
    )
    return "\n".join(diff)


def _tools_signature(tools: list[dict]) -> list[tuple]:
    """The code-owned identity of the tools array, ignoring server-populated
    config. For custom tools we fold in description + input_schema so a
    present_result schema change is detected; toolset bindings reduce to
    (type, name)."""
    sig: list[tuple] = []
    for t in _normalize(tools):
        ttype = t.get("type")
        if ttype == "custom":
            sig.append((
                "custom",
                t.get("name"),
                json.dumps(t.get("input_schema"), sort_keys=True),
                t.get("description"),
            ))
        elif ttype == "mcp_toolset":
            sig.append(("mcp_toolset", t.get("mcp_server_name")))
        else:
            sig.append((ttype, None))
    return sig


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture(client: Anthropic, agent_id: str) -> None:
    live = client.beta.agents.retrieve(agent_id)
    fixture = {
        "_comment": (
            "Ground-truth snapshot of the live gtm-agent, captured to anchor "
            "reconcile.py diffs. Anthropic auto-populates configs/default_config "
            "on toolset entries; capturing the full arrays here lets the "
            "reconciler distinguish a real code-owned change from server-side "
            "field population. Contains URLs and tool schemas only — no secrets "
            "(auth is vault-injected at session creation, scoped by URL). "
            "Regenerate via: scripts/managed_agents/reconcile.py --capture."
        ),
        "agent_id": live.id,
        "name": live.name,
        "version": live.version,
        "model": _normalize(live.model) if live.model else None,
        "mcp_servers": [_normalize(m) for m in (live.mcp_servers or [])],
        "tools": [_normalize(t) for t in (live.tools or [])],
    }
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FIXTURE_PATH, "w") as f:
        json.dump(fixture, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"captured live v{live.version} -> {FIXTURE_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a real object diff of every field that would change; no writes.",
    )
    p.add_argument(
        "--capture",
        action="store_true",
        help="Re-dump the ground-truth fixture from the live agent and exit "
             "(use after an intentional console-side change).",
    )
    args = p.parse_args()

    client = Anthropic(api_key=_api_key())
    agent_id = _agent_id()

    if args.capture:
        capture(client, agent_id)
        return 0

    manifest = load_manifest()
    fixture = load_fixture()

    live = client.beta.agents.retrieve(agent_id)
    live_version = live.version
    live_mcp = [_normalize(m) for m in (live.mcp_servers or [])]
    live_tools = [_normalize(t) for t in (live.tools or [])]
    live_system = live.system or ""
    live_model = _normalize(live.model) if live.model else {}

    des_mcp = desired_mcp_servers(manifest, live_mcp)
    des_tools = desired_tools(manifest, [s["name"] for s in des_mcp])
    des_system = desired_system(manifest)
    des_model = desired_model(manifest)

    print(f"# reconcile gtm-agent ({agent_id})")
    print(f"#   live version: {live_version}")
    print(f"#   mcp_servers (live): {[s['name'] for s in live_mcp]}")
    print()

    changes: dict[str, Any] = {}
    diffs: list[str] = []

    # ── mcp_servers ──────────────────────────────────────────────────────
    mcp_diff = _obj_diff("mcp_servers", live_mcp, des_mcp)
    if mcp_diff is not None:
        changes["mcp_servers"] = des_mcp
        diffs.append(mcp_diff)

    # ── tools ────────────────────────────────────────────────────────────
    # Fixture-anchored: if the live tools equal the captured fixture AND the
    # code-owned signature still matches that fixture, there is no code change
    # — even though Anthropic populated configs/default_config. Only a genuine
    # signature divergence (a server added/removed, or present_result schema
    # changed) is a real change and triggers a full-replace.
    fixture_tools = fixture.get("tools", [])
    live_eq_fixture = _normalize(live_tools) == _normalize(fixture_tools)
    sig_unchanged = _tools_signature(des_tools) == _tools_signature(fixture_tools)
    if not (live_eq_fixture and sig_unchanged):
        tools_diff = _obj_diff("tools", live_tools, des_tools)
        if tools_diff is not None:
            changes["tools"] = des_tools
            diffs.append(tools_diff)

    # ── system ───────────────────────────────────────────────────────────
    if live_system != des_system:
        changes["system"] = des_system
        diff = difflib.unified_diff(
            live_system.splitlines(),
            des_system.splitlines(),
            fromfile="live.system",
            tofile="desired.system",
            lineterm="",
        )
        diffs.append("\n".join(diff))

    # ── model ────────────────────────────────────────────────────────────
    model_diff = _obj_diff("model", live_model, des_model)
    if model_diff is not None:
        changes["model"] = des_model
        diffs.append(model_diff)

    # ── Report ───────────────────────────────────────────────────────────
    if not changes:
        rail = next((s for s in live_mcp if s["name"] == "railway"), None)
        print("=== NO-OP: live agent already matches the manifest ===")
        print(f"#   fields checked: mcp_servers, tools, system, model — all match")
        print(f"#   railway preserved: {json.dumps(rail)}")
        print(f"MANAGED_AGENT_ID_GTM={agent_id}")
        print(f"AGENT_VERSION={live_version}")
        return 0

    print(f"CHANGED FIELDS: {sorted(changes.keys())}")
    print()
    for d in diffs:
        print(d)
        print()

    if args.dry_run:
        print("--dry-run: no write performed.")
        return 0

    # Write ONLY the changed fields. ``version`` is the optimistic-concurrency
    # CAS token — we assert the version we just read. Omitted fields (anything
    # not in ``changes``) are left untouched by Anthropic.
    updated = client.beta.agents.update(agent_id, version=live_version, **changes)

    print("=" * 64)
    print("UPDATED")
    print("=" * 64)
    print(f"MANAGED_AGENT_ID_GTM={agent_id}")
    print(f"AGENT_VERSION={updated.version}    # was {live_version}")
    print(f"fields written: {sorted(changes.keys())}")
    print()
    print("Re-run with --capture to refresh the fixture if this change was intentional.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
