"""Tests for app.services.agent_prompts.

Mocks the Anthropic Managed Agents client at the service-layer boundary
(get_agent / update_agent_system_prompt) and the in-process DB connection
so we can assert the snapshot-then-overwrite invariant: every activate
inserts TWO version rows (snapshot, then frontend_activate). Rollback
follows the same pattern but with activation_source='rollback' on the
new row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import agent_prompts
from app.services import anthropic_managed_agents as mags

SLUG = "gtm-test-actor"
ANTHROPIC_AGENT_ID = "agt_test_123"
USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def fake_db(monkeypatch):
    """In-memory DB substitute. Tracks registry rows + version rows; the
    service module's get_db_connection / cursor calls all flow through this.

    Returns a state dict with `registry`, `versions`, `mags_calls` so
    individual tests can inspect what landed.
    """

    state: dict[str, Any] = {
        "registry": {
            SLUG: {
                "id": uuid4(),
                "agent_slug": SLUG,
                "anthropic_agent_id": ANTHROPIC_AGENT_ID,
                "role": "actor",
                "parent_actor_slug": None,
                "model": "claude-opus-4-7",
                "description": None,
                "deactivated_at": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
        },
        "versions": [],  # newest last
        "anthropic_current_prompt": "PROMPT_BEFORE_ACTIVATE",
        "mags_get_calls": 0,
        "mags_update_calls": [],
    }

    async def fake_get_registry(slug: str) -> dict[str, Any] | None:
        return state["registry"].get(slug)

    async def fake_get_version_by_index(slug: str, idx: int):
        for v in state["versions"]:
            if v["agent_slug"] == slug and v["version_index"] == idx:
                return v
        return None

    async def fake_get_latest_version(slug: str):
        rows = [v for v in state["versions"] if v["agent_slug"] == slug]
        if not rows:
            return None
        return max(rows, key=lambda v: v["version_index"])

    async def fake_list_versions(slug: str, *, limit: int = 50, offset: int = 0):
        rows = sorted(
            (v for v in state["versions"] if v["agent_slug"] == slug),
            key=lambda v: v["version_index"],
            reverse=True,
        )
        return rows[offset : offset + limit]

    async def fake_insert_version(
        *,
        conn,
        agent_slug,
        anthropic_agent_id,
        system_prompt,
        activation_source,
        parent_version_id,
        activated_by_user_id,
        notes,
    ):
        existing = [v for v in state["versions"] if v["agent_slug"] == agent_slug]
        next_idx = max((v["version_index"] for v in existing), default=0) + 1
        row = {
            "id": uuid4(),
            "agent_slug": agent_slug,
            "anthropic_agent_id": anthropic_agent_id,
            "system_prompt": system_prompt,
            "version_index": next_idx,
            "activation_source": activation_source,
            "parent_version_id": parent_version_id,
            "activated_by_user_id": activated_by_user_id,
            "notes": notes,
            "created_at": datetime.now(UTC),
        }
        state["versions"].append(row)
        return row

    class _FakeConn:
        async def commit(self):  # type: ignore[no-untyped-def]
            pass

    class _FakeConnCM:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_):
            return None

    def fake_get_db_connection():
        return _FakeConnCM()

    async def fake_mags_get_agent(agent_id: str) -> dict[str, Any]:
        state["mags_get_calls"] += 1
        return {
            "id": agent_id,
            "name": "test-agent",
            "model": "claude-opus-4-7",
            "system": state["anthropic_current_prompt"],
            "version": 1,
        }

    async def fake_mags_update(agent_id: str, prompt: str):
        state["mags_update_calls"].append((agent_id, prompt))
        state["anthropic_current_prompt"] = prompt
        return {"id": agent_id, "system": prompt}

    monkeypatch.setattr(agent_prompts, "get_registry_row", fake_get_registry)
    monkeypatch.setattr(agent_prompts, "get_version_by_index", fake_get_version_by_index)
    monkeypatch.setattr(agent_prompts, "get_latest_version", fake_get_latest_version)
    monkeypatch.setattr(agent_prompts, "list_versions", fake_list_versions)
    monkeypatch.setattr(agent_prompts, "_insert_version", fake_insert_version)
    monkeypatch.setattr(agent_prompts, "get_db_connection", fake_get_db_connection)
    monkeypatch.setattr(mags, "get_agent", fake_mags_get_agent)
    monkeypatch.setattr(mags, "update_agent_system_prompt", fake_mags_update)
    monkeypatch.setattr(agent_prompts.mags, "get_agent", fake_mags_get_agent)
    monkeypatch.setattr(
        agent_prompts.mags, "update_agent_system_prompt", fake_mags_update
    )
    return state


# ── activate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activate_inserts_snapshot_and_new_version(fake_db):
    result = await agent_prompts.activate_prompt(
        agent_slug=SLUG,
        new_system_prompt="NEW_PROMPT_TEXT",
        activated_by_user_id=USER_ID,
        notes="iteration 7",
    )

    # Two rows landed in version history: snapshot first, then the new one.
    assert len(fake_db["versions"]) == 2
    snapshot, new_row = fake_db["versions"]
    assert snapshot["activation_source"] == "snapshot"
    assert snapshot["system_prompt"] == "PROMPT_BEFORE_ACTIVATE"
    assert snapshot["version_index"] == 1
    assert new_row["activation_source"] == "frontend_activate"
    assert new_row["system_prompt"] == "NEW_PROMPT_TEXT"
    assert new_row["version_index"] == 2
    assert new_row["parent_version_id"] == snapshot["id"]

    # Anthropic was hit exactly once for get + once for update.
    assert fake_db["mags_get_calls"] == 1
    assert fake_db["mags_update_calls"] == [(ANTHROPIC_AGENT_ID, "NEW_PROMPT_TEXT")]
    assert result["snapshot_version"]["id"] == snapshot["id"]
    assert result["new_version"]["id"] == new_row["id"]


@pytest.mark.asyncio
async def test_activate_raises_for_unknown_slug(fake_db):
    with pytest.raises(agent_prompts.AgentNotRegistered):
        await agent_prompts.activate_prompt(
            agent_slug="not-a-real-slug",
            new_system_prompt="...",
            activated_by_user_id=USER_ID,
        )
    assert fake_db["versions"] == []
    assert fake_db["mags_update_calls"] == []


# ── rollback ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_resolves_target_version_and_pushes_to_anthropic(fake_db):
    # Seed three prior versions: setup_script(v1), snapshot(v2), frontend(v3)
    seed = [
        ("setup_script", "PROMPT_V1_SETUP"),
        ("snapshot", "PROMPT_V1_SETUP"),
        ("frontend_activate", "PROMPT_V3_BAD"),
    ]
    for src, prompt in seed:
        await agent_prompts._insert_version(
            conn=None,
            agent_slug=SLUG,
            anthropic_agent_id=ANTHROPIC_AGENT_ID,
            system_prompt=prompt,
            activation_source=src,
            parent_version_id=None,
            activated_by_user_id=USER_ID,
            notes=None,
        )
    # Pretend Anthropic currently holds the bad v3 prompt.
    fake_db["anthropic_current_prompt"] = "PROMPT_V3_BAD"

    result = await agent_prompts.rollback_prompt(
        agent_slug=SLUG,
        version_index=1,  # roll back to PROMPT_V1_SETUP
        activated_by_user_id=USER_ID,
        notes="bad copy in v3",
    )

    # Two more rows landed: snapshot of v3, then rollback.
    assert len(fake_db["versions"]) == 5
    new_snapshot = fake_db["versions"][-2]
    rollback_row = fake_db["versions"][-1]
    assert new_snapshot["activation_source"] == "snapshot"
    assert new_snapshot["system_prompt"] == "PROMPT_V3_BAD"
    assert rollback_row["activation_source"] == "rollback"
    assert rollback_row["system_prompt"] == "PROMPT_V1_SETUP"
    # Anthropic state was overwritten with v1's prompt.
    assert fake_db["anthropic_current_prompt"] == "PROMPT_V1_SETUP"
    assert (ANTHROPIC_AGENT_ID, "PROMPT_V1_SETUP") in fake_db["mags_update_calls"]
    assert result["new_version"]["activation_source"] == "rollback"


@pytest.mark.asyncio
async def test_rollback_raises_for_missing_version(fake_db):
    with pytest.raises(agent_prompts.VersionNotFound):
        await agent_prompts.rollback_prompt(
            agent_slug=SLUG,
            version_index=999,
            activated_by_user_id=USER_ID,
        )
    assert fake_db["mags_update_calls"] == []


# ── get_current_for_admin ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_current_for_admin_returns_composite(fake_db):
    # Seed one version row so latest is non-None.
    await agent_prompts._insert_version(
        conn=None,
        agent_slug=SLUG,
        anthropic_agent_id=ANTHROPIC_AGENT_ID,
        system_prompt="PROMPT_BEFORE_ACTIVATE",
        activation_source="setup_script",
        parent_version_id=None,
        activated_by_user_id=None,
        notes=None,
    )

    composite = await agent_prompts.get_current_for_admin(SLUG)
    assert composite is not None
    assert composite["registry"]["agent_slug"] == SLUG
    assert composite["current_system_prompt"] == "PROMPT_BEFORE_ACTIVATE"
    assert composite["latest_version"]["version_index"] == 1
    assert composite["anthropic_state"]["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_get_current_for_admin_none_for_unknown(fake_db):
    assert await agent_prompts.get_current_for_admin("not-a-real-slug") is None
