"""Vapi analytics — passthrough query endpoint.

Vapi exposes a single ``POST /analytics`` endpoint that takes an analytics
DSL (queries on the ``call`` or ``subscription`` table with optional
groupBy + timeRange + aggregation operations) and returns aggregated
results. We surface it unchanged.

The ``brand_id`` in the path is informational/auth-scoping only — Vapi's
analytics is account-wide on the single-operator Vapi account, not
per-brand. Future: if we move to a multi-account-per-brand model, layer
scoping in here.

Body shape is ``dict[str, Any]`` (extra="allow") because Vapi's analytics
DSL is large and evolves — see ``api-reference/analytics/get.md`` for the
current shape.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError
from app.providers.vapi.errors import raise_vapi_error, vapi_key

router = APIRouter(
    prefix="/api/brands/{brand_id}/vapi/analytics",
    tags=["vapi-analytics"],
)


class VapiAnalyticsQueryPayload(BaseModel):
    model_config = {"extra": "allow"}


@router.post("/query")
async def query_analytics(
    brand_id: UUID,
    body: VapiAnalyticsQueryPayload,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    api_key = vapi_key()
    try:
        return vapi_client.query_analytics(api_key, body.model_dump())
    except VapiProviderError as exc:
        raise_vapi_error("query_analytics", exc)
