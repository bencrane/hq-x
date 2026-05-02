"""Per-brand custom-domain bindings (Dub link host + landing-page host).

Mounted at `/api/v1/brands/{brand_id}/domains/*`. Org-scoped — every
endpoint requires `require_org_context` and the underlying service joins
brand → org so a user can never read or mutate another org's brand
domains. Cross-org access surfaces as a 404 from the service layer.

Two independent bindings per brand:

  * `/dub`           — register the FQDN as a Dub link host (calls Dub
    `POST /domains`). After this, step-link minting will pass `domain=…`
    to Dub when creating links so recipients see `track.acme.com/abc`.
  * `/landing-page`  — link the brand to an existing
    `business.entri_domain_connections` row that Entri Power proxies
    from the customer's hostname to our backend.

Idempotent on register: re-registering the same domain returns the
existing binding without calling Dub again.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.providers.dub.client import DubProviderError
from app.services import brand_domains as svc

router = APIRouter(prefix="/api/v1/brands", tags=["brand-domains"])

# Lightweight FQDN regex: at least one dot, labels of letters/digits/hyphens.
# Case-insensitive — the router lowercases before passing to the service.
# Not a full RFC-1035 validator — Dub will reject anything malformed.
_FQDN_PATTERN = r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$"


class DubDomainRegisterRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=253, pattern=_FQDN_PATTERN)
    model_config = {"extra": "forbid"}


class LandingPageDomainRegisterRequest(BaseModel):
    entri_connection_id: UUID
    model_config = {"extra": "forbid"}


class DubDomainBindingResponse(BaseModel):
    domain: str
    dub_domain_id: str
    verified_at: datetime


class LandingPageDomainBindingResponse(BaseModel):
    domain: str
    entri_connection_id: UUID
    verified_at: datetime


class BrandDomainsResponse(BaseModel):
    brand_id: UUID
    dub: DubDomainBindingResponse | None
    landing_page: LandingPageDomainBindingResponse | None


def _to_dub(b: svc.DubDomainBinding) -> DubDomainBindingResponse:
    return DubDomainBindingResponse(
        domain=b.domain,
        dub_domain_id=b.dub_domain_id,
        verified_at=b.verified_at,
    )


def _to_landing(b: svc.LandingPageDomainBinding) -> LandingPageDomainBindingResponse:
    return LandingPageDomainBindingResponse(
        domain=b.domain,
        entri_connection_id=b.entri_connection_id,
        verified_at=b.verified_at,
    )


def _brand_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "brand_not_found"},
    )


def _entri_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "entri_connection_not_found"},
    )


def _dub_upstream(exc: DubProviderError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "error": "dub_upstream_error",
            "status": exc.status,
            "message": str(exc),
        },
    )


@router.get("/{brand_id}/domains", response_model=BrandDomainsResponse)
async def get_brand_domains(
    brand_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> BrandDomainsResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        configs = await svc.get_brand_domain_configs(
            brand_id=brand_id, organization_id=org_id
        )
    except svc.BrandNotFoundError as exc:
        raise _brand_not_found() from exc
    return BrandDomainsResponse(
        brand_id=configs.brand_id,
        dub=_to_dub(configs.dub) if configs.dub is not None else None,
        landing_page=(
            _to_landing(configs.landing_page) if configs.landing_page is not None else None
        ),
    )


@router.post(
    "/{brand_id}/domains/dub",
    response_model=DubDomainBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_dub_domain(
    brand_id: UUID,
    body: DubDomainRegisterRequest,
    user: UserContext = Depends(require_org_context),
) -> DubDomainBindingResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        binding = await svc.register_dub_domain_for_brand(
            brand_id=brand_id,
            organization_id=org_id,
            domain=body.domain.lower(),
        )
    except svc.BrandNotFoundError as exc:
        raise _brand_not_found() from exc
    except svc.DubNotConfiguredError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "dub_not_configured", "message": str(exc)},
        ) from exc
    except DubProviderError as exc:
        raise _dub_upstream(exc) from exc
    return _to_dub(binding)


@router.delete(
    "/{brand_id}/domains/dub",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deregister_dub_domain(
    brand_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> None:
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        await svc.deregister_dub_domain_for_brand(
            brand_id=brand_id, organization_id=org_id
        )
    except svc.BrandNotFoundError as exc:
        raise _brand_not_found() from exc
    except DubProviderError as exc:
        raise _dub_upstream(exc) from exc


@router.post(
    "/{brand_id}/domains/landing-page",
    response_model=LandingPageDomainBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_landing_page_domain(
    brand_id: UUID,
    body: LandingPageDomainRegisterRequest,
    user: UserContext = Depends(require_org_context),
) -> LandingPageDomainBindingResponse:
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        binding = await svc.register_landing_page_domain_for_brand(
            brand_id=brand_id,
            organization_id=org_id,
            entri_connection_id=body.entri_connection_id,
        )
    except svc.BrandNotFoundError as exc:
        raise _brand_not_found() from exc
    except svc.EntriConnectionNotFoundError as exc:
        raise _entri_not_found() from exc
    return _to_landing(binding)


@router.delete(
    "/{brand_id}/domains/landing-page",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deregister_landing_page_domain(
    brand_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> None:
    org_id = user.active_organization_id
    assert org_id is not None
    try:
        await svc.deregister_landing_page_domain_for_brand(
            brand_id=brand_id, organization_id=org_id
        )
    except svc.BrandNotFoundError as exc:
        raise _brand_not_found() from exc
