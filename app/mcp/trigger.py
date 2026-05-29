"""MCP server exposing Trigger.dev task control as MCP tools.

Mounted under `/mcp/trigger` on the FastAPI app via FastMCP's `http_app()`
(see app/main.py). Managed agents connect via standard MCP HTTP transport;
transport auth is the shared MCP bearer (DMAAS_MCP_BEARER_TOKEN) enforced by
the ASGI wrapper at the mount boundary.

Each tool is a thin wrapper around `app.services.trigger_dev_client`, which
talks to the Trigger.dev v3 Management API (https://api.trigger.dev) using
TRIGGER_SECRET_KEY — scoped to hq-x's canonical Trigger project. Tool calls
that fail (misconfigured key, API error) return a structured `{"error": ...}`
dict rather than raising, so a single bad call never tears down the session.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from app.services import trigger_dev_client as td

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="hq-x Trigger.dev",
    instructions=(
        "Control Trigger.dev background tasks in hq-x's project: enqueue a task "
        "run (trigger_task), check status/output (get_run), list recent runs "
        "(list_runs), and request cancellation (cancel_run). Run ids look like "
        "'run_...'. Use get_run to poll a run you triggered until it reaches a "
        "terminal status (COMPLETED / FAILED / CANCELED)."
    ),
)


async def _guard(coro) -> dict[str, Any]:
    try:
        result = await coro
        return result if isinstance(result, dict) else {"result": result}
    except td.TriggerDevError as e:
        return {"error": "trigger_api_error", "status_code": e.status_code, "body": e.body}
    except Exception as e:  # noqa: BLE001 — surface config/transport errors as data
        return {"error": "trigger_call_failed", "message": str(e)}


@mcp.tool
async def trigger_task(
    task_identifier: str,
    payload: dict[str, Any] | None = None,
    delay: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Enqueue a run of the Trigger.dev task `task_identifier` in hq-x's project.

    `payload` is the JSON body handed to the task's `run()` (defaults to `{}`).
    `delay` is an optional human duration string Trigger understands, e.g.
    "30s", "5m", "1h". `idempotency_key` makes repeat calls return the same run
    instead of spawning a duplicate. Returns the run record (includes its `id`)."""
    return await _guard(
        td.trigger_task(
            task_identifier, payload, delay=delay, idempotency_key=idempotency_key
        )
    )


@mcp.tool
async def get_run(run_id: str) -> dict[str, Any]:
    """Fetch one Trigger.dev run by id (e.g. 'run_...'). Returns its current
    status, output, error, and attempt history. Poll this to watch a run you
    triggered reach a terminal status."""
    return await _guard(td.get_run(run_id))


@mcp.tool
async def list_runs(
    limit: int = 20,
    status: str | None = None,
    task_identifier: str | None = None,
) -> dict[str, Any]:
    """List recent runs in hq-x's Trigger.dev project, newest first. Optionally
    filter by `status` (e.g. EXECUTING, COMPLETED, FAILED, CANCELED) or by
    `task_identifier`. `limit` is clamped to 1..100."""
    return await _guard(
        td.list_runs(limit=limit, status=status, task_identifier=task_identifier)
    )


@mcp.tool
async def cancel_run(run_id: str) -> dict[str, Any]:
    """Request cancellation of an in-flight Trigger.dev run by id. No-op if the
    run already reached a terminal status."""
    return await _guard(td.cancel_run(run_id))
