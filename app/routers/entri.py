"""Entri custom-domain REST router.

Mounted at `/api/v1/entri/*`. Front-end opens the Entri modal with the
config returned by POST /session, then POSTs back to /success when the
modal's `onSuccess` event fires; webhook events drive long-running state
changes asynchronously via /webhooks/entri.

When ENTRI_APPLICATION_ID is unset every endpoint returns 503
`entri_not_configured` — the integration is fully wired but inert until
we sign up for a paid Entri plan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext
from app.config import settings
from app.dmaas import entri_domains
from app.models.entri import (
    EntriDnsRecord,
    EntriDomainConnectionResponse,
    EntriDomainListResponse,
    EntriEligibilityResponse,
    EntriSessionRequest,
    EntriSessionResponse,
    EntriSuccessRequest,
)
from app.observability import incr_metric
from app.providers.entri import client as entri_client
from app.providers.entri.client import EntriProviderError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/entri", tags=["entri"])

# Asset paths the origin app needs to serve raw under Entri Power. Matches
# our Next.js / FastAPI conventions; tune in dashboard if we change apps.
_DEFAULT_POWER_ROOT_PATH_ACCESS = ["/static/", "/_next/", "/favicon.ico"]


def _ensure_configured() -> tuple[str, str]:
    """Return (application_id, secret) or 503."""
    if not settings.ENTRI_APPLICATION_ID or settings.ENTRI_SECRET is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "entri_not_configured",
                "message": (
                    "Entri credentials are not set. /api/v1/entri/* is "
                    "inert until ENTRI_APPLICATION_ID and ENTRI_SECRET are "
                    "configured in Doppler."
                ),
            },
        )
    return settings.ENTRI_APPLICATION_ID, settings.ENTRI_SECRET.get_secret_value()


def _ensure_runtime_targets() -> tuple[str, str]:
    """Return (cname_target, application_url_base) or 503."""
    cname_target = settings.ENTRI_CNAME_TARGET
    base = settings.ENTRI_APPLICATION_URL_BASE
    if not cname_target or not base:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "entri_not_configured",
                "message": (
                    "ENTRI_CNAME_TARGET and ENTRI_APPLICATION_URL_BASE must "
                    "both be set."
                ),
            },
        )
    return cname_target, base.rstrip("/")


def _raise_provider_error(operation: str, exc: EntriProviderError) -> None:
    incr_metric(
        "entri.api.error",
        operation=operation,
        category=exc.category,
        status=str(exc.status) if exc.status is not None else "none",
    )
    if exc.status in (401, 403):
        raise HTTPException(
            status_code=502,
            detail={
                "error": "entri_auth_failed",
                "message": "Entri rejected our credentials — operator must rotate ENTRI_SECRET",
            },
        )
    if exc.status == 404:
        raise HTTPException(
            status_code=404,
            detail={"error": "entri_not_found", "message": str(exc)},
        )
    raise HTTPException(
        status_code=502,
        detail={
            "error": "entri_upstream_error",
            "operation": operation,
            "status": exc.status,
            "message": str(exc),
        },
    )


def _build_dns_records(
    *,
    is_root_domain: bool,
    cname_target: str,
    application_url: str,
) -> list[EntriDnsRecord]:
    """Build the dnsRecords array Entri's modal will render.

    Subdomain → CNAME → cname_target. Root → A → {ENTRI_SERVERS} (Entri
    substitutes its anycast IPs at runtime; CNAME-at-apex is illegal on
    most registrars).
    """
    if is_root_domain:
        return [
            EntriDnsRecord(
                type="A",
                host="@",
                value="{ENTRI_SERVERS}",
                ttl=300,
                application_url=application_url,
            )
        ]
    return [
        EntriDnsRecord(
            type="CNAME",
            host="{SUBDOMAIN}",
            value=cname_target,
            ttl=300,
            application_url=application_url,
        )
    ]


def _full_hostname(domain: str, subdomain: str | None, *, is_root: bool) -> str:
    if is_root or not subdomain:
        return domain.lower().strip(".")
    return f"{subdomain.strip('.')}.{domain}".lower().strip(".")


def _resolve_application_url(
    *,
    base: str,
    explicit_path: str | None,
    step_id: UUID | None,
) -> str:
    if explicit_path:
        path = explicit_path if explicit_path.startswith("/") else f"/{explicit_path}"
        return f"{base}{path}"
    if step_id is not None:
        return f"{base}/lp/{step_id}"
    return base


@router.post("/session", response_model=EntriSessionResponse)
async def create_session(
    body: EntriSessionRequest,
    user: UserContext = Depends(require_org_context),
) -> EntriSessionResponse:
    """Mint an Entri JWT + assemble the showEntri config bundle.

    Persists a `pending_modal` row keyed on (organization_id, step_id, domain)
    that the /success endpoint and the webhook projector will mutate.
    """
    application_id, secret = _ensure_configured()
    cname_target, app_url_base = _ensure_runtime_targets()

    org_id = user.active_organization_id
    assert org_id is not None  # require_org_context guarantees this

    is_root = bool(body.use_root_domain)
    full_domain = _full_hostname(body.domain, body.subdomain, is_root=is_root)
    application_url = _resolve_application_url(
        base=app_url_base,
        explicit_path=body.application_url_path,
        step_id=body.channel_campaign_step_id,
    )
    entri_user_id = (
        f"{org_id}:{body.channel_campaign_step_id}"
        if body.channel_campaign_step_id is not None
        else f"{org_id}:none"
    )

    try:
        token_response = entri_client.mint_token(
            application_id=application_id,
            secret=secret,
            base_url=settings.ENTRI_API_BASE,
        )
    except EntriProviderError as exc:
        _raise_provider_error("mint_token", exc)
        raise  # _raise_provider_error always raises but mypy needs this

    jwt = token_response.get("auth_token")
    if not jwt:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "entri_token_response_invalid",
                "message": "Entri /token did not return auth_token",
            },
        )

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=58)

    connection = await entri_domains.insert_pending_connection(
        organization_id=org_id,
        channel_campaign_step_id=body.channel_campaign_step_id,
        domain=full_domain,
        is_root_domain=is_root,
        application_url=application_url,
        entri_user_id=entri_user_id,
        entri_token=jwt,
        entri_token_expires_at=expires_at,
    )

    dns_records = _build_dns_records(
        is_root_domain=is_root,
        cname_target=cname_target,
        application_url=application_url,
    )

    incr_metric("entri.session.created", is_root=str(is_root).lower())

    return EntriSessionResponse(
        session_id=connection.id,
        applicationId=application_id,
        token=jwt,
        dnsRecords=dns_records,
        userId=entri_user_id,
        applicationUrl=application_url,
        prefilledDomain=body.domain,
        defaultSubdomain=body.subdomain,
        power=True,
        secureRootDomain=is_root,
    )


@router.post("/success", response_model=EntriDomainConnectionResponse)
async def record_success(
    body: EntriSuccessRequest,
    user: UserContext = Depends(require_org_context),
) -> EntriDomainConnectionResponse:
    """Frontend reports `onSuccess`. Move state forward and register Power."""
    application_id, _ = _ensure_configured()

    connection = await entri_domains.get_by_id(body.session_id)
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "session_not_found"},
        )
    if connection.organization_id != user.active_organization_id:
        raise HTTPException(
            status_code=403,
            detail={"error": "session_organization_mismatch"},
        )
    if connection.entri_token is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "session_token_missing"},
        )

    # Idempotent re-register via PUT — safe whether the dashboard already
    # has the mapping or not.
    try:
        entri_client.update_power_domain(
            application_id=application_id,
            jwt=connection.entri_token,
            domain=connection.domain,
            application_url=connection.application_url,
            power_root_path_access=_DEFAULT_POWER_ROOT_PATH_ACCESS,
            base_url=settings.ENTRI_API_BASE,
        )
    except EntriProviderError as exc:
        _raise_provider_error("update_power_domain", exc)

    updated = await entri_domains.update_state(
        connection.id,
        state="dns_records_submitted",
        provider=body.provider,
        setup_type=body.setup_type,
    )
    incr_metric("entri.success.recorded")

    target = updated or connection
    return _to_response(target)


@router.get("/domains", response_model=EntriDomainListResponse)
async def list_domains(
    user: UserContext = Depends(require_org_context),
) -> EntriDomainListResponse:
    rows = await entri_domains.list_for_organization(user.active_organization_id)
    return EntriDomainListResponse(domains=[_to_response(r) for r in rows])


@router.get("/domains/{connection_id}", response_model=EntriDomainConnectionResponse)
async def get_domain(
    connection_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> EntriDomainConnectionResponse:
    row = await entri_domains.get_by_id(connection_id)
    if row is None or row.organization_id != user.active_organization_id:
        raise HTTPException(404, {"error": "domain_not_found"})
    return _to_response(row)


@router.delete("/domains/{connection_id}", response_model=EntriDomainConnectionResponse)
async def delete_domain(
    connection_id: UUID,
    user: UserContext = Depends(require_org_context),
) -> EntriDomainConnectionResponse:
    application_id, secret = _ensure_configured()

    row = await entri_domains.get_by_id(connection_id)
    if row is None or row.organization_id != user.active_organization_id:
        raise HTTPException(404, {"error": "domain_not_found"})

    try:
        token_response = entri_client.mint_token(
            application_id=application_id,
            secret=secret,
            base_url=settings.ENTRI_API_BASE,
        )
        jwt = token_response.get("auth_token")
        if not jwt:
            raise HTTPException(502, {"error": "entri_token_response_invalid"})
        entri_client.delete_power_domain(
            application_id=application_id,
            jwt=jwt,
            domain=row.domain,
            base_url=settings.ENTRI_API_BASE,
        )
    except EntriProviderError as exc:
        # 404 from Entri = already gone; that's a successful disconnect.
        if exc.status != 404:
            _raise_provider_error("delete_power_domain", exc)

    updated = await entri_domains.mark_disconnected(connection_id)
    incr_metric("entri.domain.disconnected")
    return _to_response(updated or row)


@router.get("/eligibility", response_model=EntriEligibilityResponse)
async def check_eligibility(
    domain: str = Query(..., description="Full hostname, e.g. qr.acme.com"),
    root_domain: bool = Query(False),
    user: UserContext = Depends(require_org_context),
) -> EntriEligibilityResponse:
    """Pre-modal check: has the customer added the CNAME / A record yet?"""
    application_id, secret = _ensure_configured()

    try:
        token_response = entri_client.mint_token(
            application_id=application_id,
            secret=secret,
            base_url=settings.ENTRI_API_BASE,
        )
        jwt = token_response.get("auth_token")
        if not jwt:
            raise HTTPException(502, {"error": "entri_token_response_invalid"})
        result = entri_client.check_power_eligibility(
            application_id=application_id,
            jwt=jwt,
            domain=domain,
            root_domain=root_domain,
            base_url=settings.ENTRI_API_BASE,
        )
    except EntriProviderError as exc:
        _raise_provider_error("check_power_eligibility", exc)
        raise

    eligible = bool(result.get("eligible", False))
    return EntriEligibilityResponse(domain=domain, eligible=eligible, raw=result)


def _to_response(row: Any) -> EntriDomainConnectionResponse:
    return EntriDomainConnectionResponse(
        id=row.id,
        organization_id=row.organization_id,
        channel_campaign_step_id=row.channel_campaign_step_id,
        domain=row.domain,
        is_root_domain=row.is_root_domain,
        application_url=row.application_url,
        state=row.state,
        provider=row.provider,
        setup_type=row.setup_type,
        propagation_status=row.propagation_status,
        power_status=row.power_status,
        secure_status=row.secure_status,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
