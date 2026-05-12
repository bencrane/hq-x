"""Request-scoped data-lineage tracker (hq-x mirror of DEX).

Identical contract to ``apps/data-engine-x/app/services/lineage.py``. hq-x
maintains its own per-request tracker; ``dex_client._request`` merges the
``X-Data-Lineage`` header from each DEX response into the local tracker so
hq-x's final response carries the union of (hq-x reads) + (DEX-side reads
from every DEX call this request made).

Architecture rule (`app_responsibilities.md`): hq-x calls DEX, never the
reverse. Lineage propagates one-way: DEX → hq-x → hq-command (if applicable).
hq-x never receives lineage from anything upstream of itself.

Public API matches the DEX module:
  init_lineage_context() -> Token
  record_catalog_read(table, snapshot_id, format, queried_at=None) -> None
  get_lineage() -> list[dict]
  reset_lineage_context(token: Token) -> None
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

_lineage_context: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "data_lineage", default=None
)


def init_lineage_context() -> Token:
    return _lineage_context.set([])


def record_catalog_read(
    table: str,
    snapshot_id: str | None,
    format: str,
    queried_at: datetime | None = None,
) -> None:
    state = _lineage_context.get()
    if state is None:
        return
    if queried_at is None:
        queried_at = datetime.now(timezone.utc)
    for entry in state:
        if entry["table"] == table and entry["snapshot_id"] == snapshot_id:
            return
    state.append(
        {
            "table": table,
            "snapshot_id": snapshot_id,
            "format": format,
            "queried_at": queried_at.isoformat(),
        }
    )


def get_lineage() -> list[dict[str, Any]]:
    state = _lineage_context.get()
    return list(state) if state is not None else []


def reset_lineage_context(token: Token) -> None:
    _lineage_context.reset(token)
