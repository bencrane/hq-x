"""ASGI middleware that injects an X-Data-Lineage response header (hq-x).

Mirror of ``apps/data-engine-x/app/middleware/lineage.py``. Same raw-ASGI
shape so streaming responses pass through unmodified.

Lineage entries come from two sources:
  1. Local hq-x catalog reads (record_catalog_read called from hq-x service
     functions — minimal at this phase; hq-x reads its own Postgres but
     isn't a primary catalog reader).
  2. DEX-side reads merged via ``dex_client._request`` after every DEX HTTP
     call (the per-request tracker accumulates entries from each DEX
     response's X-Data-Lineage header).
"""
from __future__ import annotations

import json
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.lineage import (
    get_lineage,
    init_lineage_context,
    reset_lineage_context,
)

log = logging.getLogger(__name__)


class LineageMiddleware:
    """Inject an X-Data-Lineage response header on every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = init_lineage_context()

        async def send_with_lineage(message: Message) -> None:
            if message["type"] == "http.response.start":
                lineage = get_lineage()
                payload = json.dumps(
                    lineage, separators=(",", ":"), ensure_ascii=True
                )
                headers = list(message.get("headers") or [])
                headers.append((b"x-data-lineage", payload.encode("latin-1")))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_lineage)
        finally:
            reset_lineage_context(token)
