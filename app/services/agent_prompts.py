"""Versioned management of MAGS-agent system prompts.

Wraps `anthropic_managed_agents.update_agent_system_prompt` with the
snapshot-then-overwrite invariant: every activate captures the current
Anthropic-side prompt as a version row BEFORE pushing the new one.
This preserves rollback targets we'd otherwise lose to Anthropic's
destructive POST /v1/agents/{id} semantics.

Two version rows per activate:
  * activation_source='snapshot' — the soon-to-be-overwritten state
  * activation_source='frontend_activate' (or 'rollback') — the new prompt

Plus one row per setup_script-driven registration so the very first
prompt also lives in version history.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.services import anthropic_managed_agents as mags

logger = logging.getLogger(__name__)


class AgentPromptError(Exception):
    pass


class AgentNotRegistered(AgentPromptError):
    pass


class VersionNotFound(AgentPromptError):
    pass


# ---------------------------------------------------------------------------
# Registry CRUD (read paths used by activate/rollback/list)
# ---------------------------------------------------------------------------


async def get_registry_row(agent_slug: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, agent_slug, anthropic_agent_id, role,
                       parent_actor_slug, model, description,
                       deactivated_at, created_at, updated_at
                FROM business.gtm_agent_registry
                WHERE agent_slug = %s
                """,
                (agent_slug,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "agent_slug": row[1],
        "anthropic_agent_id": row[2],
        "role": row[3],
        "parent_actor_slug": row[4],
        "model": row[5],
        "description": row[6],
        "deactivated_at": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


async def list_registry_rows(*, include_deactivated: bool = False) -> list[dict[str, Any]]:
    where = "" if include_deactivated else "WHERE deactivated_at IS NULL"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, agent_slug, anthropic_agent_id, role,
                       parent_actor_slug, model, description,
                       deactivated_at, created_at, updated_at
                FROM business.gtm_agent_registry
                {where}
                ORDER BY role, agent_slug
                """,
            )
            rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "id": row[0],
            "agent_slug": row[1],
            "anthropic_agent_id": row[2],
            "role": row[3],
            "parent_actor_slug": row[4],
            "model": row[5],
            "description": row[6],
            "deactivated_at": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        })
    return out


async def upsert_registry_row(
    *,
    agent_slug: str,
    anthropic_agent_id: str,
    role: str,
    parent_actor_slug: str | None = None,
    model: str = "claude-opus-4-7",
    description: str | None = None,
) -> dict[str, Any]:
    """Used by setup scripts to (re)register an agent. Mirrors the
    `INSERT ... ON CONFLICT` pattern from the existing managed-agents
    setup helpers. Always clears `deactivated_at` so a re-register
    revives a soft-deleted slug."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.gtm_agent_registry (
                    agent_slug, anthropic_agent_id, role, parent_actor_slug,
                    model, description
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (agent_slug) DO UPDATE
                SET anthropic_agent_id = EXCLUDED.anthropic_agent_id,
                    role = EXCLUDED.role,
                    parent_actor_slug = EXCLUDED.parent_actor_slug,
                    model = EXCLUDED.model,
                    description = COALESCE(EXCLUDED.description, business.gtm_agent_registry.description),
                    deactivated_at = NULL,
                    updated_at = NOW()
                RETURNING id, agent_slug, anthropic_agent_id, role,
                          parent_actor_slug, model, description,
                          deactivated_at, created_at, updated_at
                """,
                (
                    agent_slug, anthropic_agent_id, role, parent_actor_slug,
                    model, description,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return {
        "id": row[0],
        "agent_slug": row[1],
        "anthropic_agent_id": row[2],
        "role": row[3],
        "parent_actor_slug": row[4],
        "model": row[5],
        "description": row[6],
        "deactivated_at": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


# ---------------------------------------------------------------------------
# Version-row helpers
# ---------------------------------------------------------------------------


async def _next_version_index(agent_slug: str, conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COALESCE(MAX(version_index), 0) + 1
            FROM business.agent_prompt_versions
            WHERE agent_slug = %s
            """,
            (agent_slug,),
        )
        row = await cur.fetchone()
    return int(row[0])


async def _insert_version(
    *,
    conn,
    agent_slug: str,
    anthropic_agent_id: str,
    system_prompt: str,
    activation_source: str,
    parent_version_id: UUID | None,
    activated_by_user_id: UUID | None,
    notes: str | None,
) -> dict[str, Any]:
    version_index = await _next_version_index(agent_slug, conn)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO business.agent_prompt_versions (
                agent_slug, anthropic_agent_id, system_prompt,
                version_index, activation_source, parent_version_id,
                activated_by_user_id, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, agent_slug, anthropic_agent_id, system_prompt,
                      version_index, activation_source, parent_version_id,
                      activated_by_user_id, notes, created_at
            """,
            (
                agent_slug, anthropic_agent_id, system_prompt,
                version_index, activation_source,
                str(parent_version_id) if parent_version_id else None,
                str(activated_by_user_id) if activated_by_user_id else None,
                notes,
            ),
        )
        row = await cur.fetchone()
    assert row is not None
    return _version_row_to_dict(row)


def _version_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "agent_slug": row[1],
        "anthropic_agent_id": row[2],
        "system_prompt": row[3],
        "version_index": row[4],
        "activation_source": row[5],
        "parent_version_id": row[6],
        "activated_by_user_id": row[7],
        "notes": row[8],
        "created_at": row[9],
    }


async def list_versions(
    agent_slug: str, *, limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, agent_slug, anthropic_agent_id, system_prompt,
                       version_index, activation_source, parent_version_id,
                       activated_by_user_id, notes, created_at
                FROM business.agent_prompt_versions
                WHERE agent_slug = %s
                ORDER BY version_index DESC
                LIMIT %s OFFSET %s
                """,
                (agent_slug, limit, offset),
            )
            rows = await cur.fetchall()
    return [_version_row_to_dict(r) for r in rows]


async def get_version(version_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, agent_slug, anthropic_agent_id, system_prompt,
                       version_index, activation_source, parent_version_id,
                       activated_by_user_id, notes, created_at
                FROM business.agent_prompt_versions
                WHERE id = %s
                """,
                (str(version_id),),
            )
            row = await cur.fetchone()
    return _version_row_to_dict(row) if row else None


async def get_version_by_index(
    agent_slug: str, version_index: int,
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, agent_slug, anthropic_agent_id, system_prompt,
                       version_index, activation_source, parent_version_id,
                       activated_by_user_id, notes, created_at
                FROM business.agent_prompt_versions
                WHERE agent_slug = %s AND version_index = %s
                """,
                (agent_slug, version_index),
            )
            row = await cur.fetchone()
    return _version_row_to_dict(row) if row else None


async def get_latest_version(agent_slug: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, agent_slug, anthropic_agent_id, system_prompt,
                       version_index, activation_source, parent_version_id,
                       activated_by_user_id, notes, created_at
                FROM business.agent_prompt_versions
                WHERE agent_slug = %s
                ORDER BY version_index DESC
                LIMIT 1
                """,
                (agent_slug,),
            )
            row = await cur.fetchone()
    return _version_row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Activate / Rollback / Get-current
# ---------------------------------------------------------------------------


async def activate_prompt(
    *,
    agent_slug: str,
    new_system_prompt: str,
    activated_by_user_id: UUID | None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Snapshot-then-overwrite. See module docstring for the invariant.

    Returns ``{snapshot_version, new_version}``. Both are full version
    dicts (id, version_index, etc.).
    """
    registry = await get_registry_row(agent_slug)
    if registry is None:
        raise AgentNotRegistered(
            f"agent_slug={agent_slug!r} not in business.gtm_agent_registry"
        )
    anthropic_agent_id = registry["anthropic_agent_id"]

    # 1. Snapshot the current Anthropic-side state.
    current = await mags.get_agent(anthropic_agent_id)
    current_prompt = (current or {}).get("system") or ""
    snapshot_notes = (
        notes
        and f"snapshot taken before activate; activate notes: {notes[:200]}"
    ) or "snapshot taken before activate"

    async with get_db_connection() as conn:
        snapshot = await _insert_version(
            conn=conn,
            agent_slug=agent_slug,
            anthropic_agent_id=anthropic_agent_id,
            system_prompt=current_prompt,
            activation_source="snapshot",
            parent_version_id=None,
            activated_by_user_id=activated_by_user_id,
            notes=snapshot_notes,
        )
        await conn.commit()

    # 2. Push the new prompt to Anthropic.
    await mags.update_agent_system_prompt(anthropic_agent_id, new_system_prompt)

    # 3. Insert the new version row.
    async with get_db_connection() as conn:
        new_row = await _insert_version(
            conn=conn,
            agent_slug=agent_slug,
            anthropic_agent_id=anthropic_agent_id,
            system_prompt=new_system_prompt,
            activation_source="frontend_activate",
            parent_version_id=snapshot["id"],
            activated_by_user_id=activated_by_user_id,
            notes=notes,
        )
        await conn.commit()

    return {"snapshot_version": snapshot, "new_version": new_row}


async def rollback_prompt(
    *,
    agent_slug: str,
    version_index: int,
    activated_by_user_id: UUID | None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Roll back to a prior version. Same shape as activate_prompt:
    snapshot the current state first, then push the historical prompt,
    then write a new version row with activation_source='rollback'.
    """
    target = await get_version_by_index(agent_slug, version_index)
    if target is None:
        raise VersionNotFound(
            f"no version_index={version_index} for agent_slug={agent_slug!r}"
        )
    registry = await get_registry_row(agent_slug)
    if registry is None:
        raise AgentNotRegistered(
            f"agent_slug={agent_slug!r} not in business.gtm_agent_registry"
        )
    anthropic_agent_id = registry["anthropic_agent_id"]

    current = await mags.get_agent(anthropic_agent_id)
    current_prompt = (current or {}).get("system") or ""
    rollback_notes = (
        notes
        and f"snapshot taken before rollback; rollback notes: {notes[:200]}"
    ) or f"snapshot taken before rollback to version_index={version_index}"

    async with get_db_connection() as conn:
        snapshot = await _insert_version(
            conn=conn,
            agent_slug=agent_slug,
            anthropic_agent_id=anthropic_agent_id,
            system_prompt=current_prompt,
            activation_source="snapshot",
            parent_version_id=None,
            activated_by_user_id=activated_by_user_id,
            notes=rollback_notes,
        )
        await conn.commit()

    await mags.update_agent_system_prompt(
        anthropic_agent_id, target["system_prompt"]
    )

    async with get_db_connection() as conn:
        new_row = await _insert_version(
            conn=conn,
            agent_slug=agent_slug,
            anthropic_agent_id=anthropic_agent_id,
            system_prompt=target["system_prompt"],
            activation_source="rollback",
            parent_version_id=target["id"],
            activated_by_user_id=activated_by_user_id,
            notes=notes
            or f"rollback to version_index={version_index} (id={target['id']})",
        )
        await conn.commit()
    return {"snapshot_version": snapshot, "new_version": new_row}


async def record_setup_script_version(
    *,
    agent_slug: str,
    anthropic_agent_id: str,
    system_prompt: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Called by setup scripts (in managed-agents-x) immediately after
    they create the Anthropic agent + upsert the registry row, to seed
    the first version-history entry."""
    async with get_db_connection() as conn:
        version = await _insert_version(
            conn=conn,
            agent_slug=agent_slug,
            anthropic_agent_id=anthropic_agent_id,
            system_prompt=system_prompt,
            activation_source="setup_script",
            parent_version_id=None,
            activated_by_user_id=None,
            notes=notes,
        )
        await conn.commit()
    return version


async def get_current_for_admin(agent_slug: str) -> dict[str, Any] | None:
    """Composite read for the admin UI: registry row + current Anthropic
    system prompt + latest version metadata."""
    registry = await get_registry_row(agent_slug)
    if registry is None:
        return None
    current = await mags.get_agent(registry["anthropic_agent_id"])
    latest = await get_latest_version(agent_slug)
    return {
        "registry": registry,
        "current_system_prompt": (current or {}).get("system") or "",
        "anthropic_state": {
            "name": (current or {}).get("name"),
            "model": (current or {}).get("model"),
            "version": (current or {}).get("version"),
        },
        "latest_version": latest,
    }


__all__ = [
    "AgentPromptError",
    "AgentNotRegistered",
    "VersionNotFound",
    "get_registry_row",
    "list_registry_rows",
    "upsert_registry_row",
    "list_versions",
    "get_version",
    "get_version_by_index",
    "get_latest_version",
    "activate_prompt",
    "rollback_prompt",
    "record_setup_script_version",
    "get_current_for_admin",
]
