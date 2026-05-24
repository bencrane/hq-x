"""Read-only access to `gtm.people` for the hq-zone platform-api BFF.

Thin proxy: hq-zone-api hits us with BACKEND_X_SERVICE_TOKEN; we
forward to DEX `GET /api/internal/gtm/leads` using DEX_SERVICE_TOKEN
from this app's Doppler config. DEX owns the actual `gtm.people` table.

Per-user scoping is intentionally omitted — gtm data is operator-grade
in single-operator world.

Endpoint:
  GET /api/v1/gtm/people?source=&q=&limit=&offset=
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth.service_token import verify_backend_x_token
from app.services import dex_client

router = APIRouter(prefix="/api/v1/gtm/people", tags=["gtm-people"])


class PersonRow(BaseModel):
    id: str
    full_name: str | None
    title: str | None
    source: str | None
    company_id: str | None
    company_name: str | None
    company_domain: str | None
    company_linkedin_url: str | None


class PeoplePage(BaseModel):
    data: list[PersonRow]
    total: int
    limit: int
    offset: int


@router.get("", response_model=PeoplePage)
async def list_people(
    source: str | None = Query(None, description="Exact match on gtm.people.source"),
    q: str | None = Query(None, description="Case-insensitive substring on name/title/company"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        dex_payload = await dex_client.list_gtm_leads(
            source=source, q=q, limit=limit, offset=offset,
        )
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc

    rows = dex_payload.get("rows", [])
    data = [
        {
            "id": r.get("person_id"),
            "full_name": r.get("full_name"),
            "title": r.get("title"),
            "source": r.get("source"),
            "company_id": r.get("company_id"),
            "company_name": r.get("company_name"),
            "company_domain": r.get("company_domain"),
            "company_linkedin_url": r.get("company_linkedin_url"),
        }
        for r in rows
    ]
    return {
        "data": data,
        "total": int(dex_payload.get("total_count", 0)),
        "limit": int(dex_payload.get("limit", limit)),
        "offset": int(dex_payload.get("offset", offset)),
    }
