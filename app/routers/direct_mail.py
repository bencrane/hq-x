"""Public direct-mail (Lob) router.

Single-tenant: every endpoint is gated on `require_operator`. There is no
per-org credential lookup — the global LOB_API_KEY is used everywhere.

Piece-create endpoints (postcards, letters, self-mailers, snap packs,
booklets) all run through `_create_piece` which:
  1. Auto-derives an idempotency key if the caller didn't supply one.
  2. Runs the pre-send US address-verify gate (suppression check + verify).
  3. Calls Lob.
  4. Upserts into direct_mail_pieces.
  5. Returns the canonical normalized response.

Templates / addresses / buckslips / cards / campaigns / creatives / uploads /
resource-proofs / qr-code-analytics / domains / links / billing-groups are
thin proxies — they don't touch direct_mail_pieces.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext
from app.config import settings
from app.db import get_db_connection
from app.direct_mail.addresses import (
    AddressSuppressed,
    AddressUndeliverable,
    AddressVerifyResult,
    verify_or_suppress,
)
from app.direct_mail.persistence import (
    UpsertedPiece,
    get_piece_by_external_id,
    upsert_piece,
)
from app.direct_mail.specs import (
    CATEGORY_VALUES,
    MailerSpec,
    get_spec,
    list_categories,
    list_design_rules,
    list_specs,
    validate_artwork_dimensions,
)
from app.models.direct_mail import (
    DirectMailAddressCreateRequest,
    DirectMailAddressListResponse,
    DirectMailAddressResponse,
    DirectMailAddressVerificationResponse,
    DirectMailAddressVerificationUSBulkRequest,
    DirectMailAddressVerificationUSRequest,
    DirectMailBillingGroupCreateRequest,
    DirectMailBillingGroupUpdateRequest,
    DirectMailBuckslipCreateRequest,
    DirectMailBuckslipOrderCreateRequest,
    DirectMailBuckslipUpdateRequest,
    DirectMailCampaignCreateRequest,
    DirectMailCampaignUpdateRequest,
    DirectMailCardCreateRequest,
    DirectMailCardOrderCreateRequest,
    DirectMailCardUpdateRequest,
    DirectMailCreativeCreateRequest,
    DirectMailCreativeUpdateRequest,
    DirectMailDomainCreateRequest,
    DirectMailLinkCreateRequest,
    DirectMailLinkUpdateRequest,
    DirectMailPieceCancelResponse,
    DirectMailPieceCreateRequest,
    DirectMailPieceListResponse,
    DirectMailPieceResponse,
    DirectMailResourceProofCreateRequest,
    DirectMailResourceProofUpdateRequest,
    DirectMailTemplateCreateRequest,
    DirectMailTemplateListResponse,
    DirectMailTemplateResponse,
    DirectMailTemplateUpdateRequest,
    DirectMailTemplateVersionCreateRequest,
    DirectMailTemplateVersionListResponse,
    DirectMailTemplateVersionResponse,
    DirectMailTemplateVersionUpdateRequest,
    DirectMailUploadCreateRequest,
    DirectMailUploadExportCreateRequest,
    DirectMailUploadUpdateRequest,
    MailerCategoryListResponse,
    MailerCategorySummary,
    MailerDesignRule,
    MailerDesignRulesResponse,
    MailerSpecListResponse,
    MailerSpecResponse,
    MailerSpecValidationRequest,
    MailerSpecValidationResponse,
    PieceType,
)
from app.observability import incr_metric, log_event
from app.providers.lob import client as lob_client
from app.providers.lob.client import LobProviderError
from app.providers.lob.idempotency import derive_idempotency_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/direct-mail", tags=["direct-mail"])


def _api_key(test_mode: bool = False) -> str:
    if test_mode:
        key = settings.LOB_API_KEY_TEST
        if not key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "type": "provider_unconfigured",
                    "provider": "lob",
                    "reason": "LOB_API_KEY_TEST not set",
                },
            )
        return key
    key = settings.LOB_API_KEY
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "provider_unconfigured",
                "provider": "lob",
                "reason": "LOB_API_KEY not set",
            },
        )
    return key


def _raise_provider_error(operation: str, exc: LobProviderError) -> None:
    incr_metric("direct_mail.provider.error", operation=operation, category=exc.category)
    log_event(
        "direct_mail_provider_error",
        level=logging.WARNING,
        operation=operation,
        category=exc.category,
        error=str(exc)[:300],
    )
    if exc.category == "transient":
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif exc.category == "terminal":
        http_status = status.HTTP_502_BAD_GATEWAY
    else:
        http_status = status.HTTP_502_BAD_GATEWAY
    raise HTTPException(
        status_code=http_status,
        detail={
            "type": "provider_error",
            "provider": "lob",
            "operation": operation,
            "category": exc.category,
            "message": str(exc)[:300],
        },
    )


def _to_piece_response(piece: UpsertedPiece) -> DirectMailPieceResponse:
    return DirectMailPieceResponse(
        id=piece.id,
        external_piece_id=piece.external_piece_id,
        piece_type=piece.piece_type,  # type: ignore[arg-type]
        status=piece.status,
        send_date=piece.raw_payload.get("send_date")
        if isinstance(piece.raw_payload, dict)
        else None,
        cost_cents=piece.cost_cents,
        deliverability=piece.deliverability,
        is_test_mode=piece.is_test_mode,
        metadata=piece.metadata,
        raw_payload=piece.raw_payload,
        created_at=piece.created_at.isoformat() if piece.created_at else "",
        updated_at=piece.updated_at.isoformat() if piece.updated_at else "",
    )


async def _resolve_channel_campaign_for_direct_mail(
    channel_campaign_id: Any,
) -> tuple[str, str] | None:
    """Validate that ``channel_campaign_id`` is a real channel='direct_mail'
    channel_campaign.

    Returns ``(channel_campaign_id, campaign_id)`` as strings on success
    (the campaign_id is the umbrella), or None when the caller didn't
    supply a channel_campaign id. Raises 400 when the id points at a
    non-existent, archived, or wrong-channel row — we'd rather fail the
    send than persist a piece with stale tagging.
    """
    if channel_campaign_id is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, campaign_id, channel, archived_at
                FROM business.channel_campaigns
                WHERE id = %s
                """,
                (str(channel_campaign_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "channel_campaign_not_found",
                "channel_campaign_id": str(channel_campaign_id),
            },
        )
    if row[3] is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "channel_campaign_archived",
                "channel_campaign_id": str(channel_campaign_id),
            },
        )
    if row[2] != "direct_mail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "channel_campaign_channel_mismatch",
                "expected_channel": "direct_mail",
                "actual_channel": row[2],
            },
        )
    return str(row[0]), str(row[1])


async def _create_piece(
    *,
    piece_type: PieceType,
    data: DirectMailPieceCreateRequest,
    create_fn: Callable[..., dict[str, Any]],
    user: UserContext,
) -> DirectMailPieceResponse:
    operation = f"create_{piece_type}"
    api_key = _api_key(test_mode=data.test_mode)
    # campaign_tags = (channel_campaign_id, campaign_id) when caller supplied one.
    campaign_tags = await _resolve_channel_campaign_for_direct_mail(
        data.channel_campaign_id
    )

    # 1. Address gate (suppression check + Lob US verify, fail-open on Lob error).
    try:
        verify_result: AddressVerifyResult = await verify_or_suppress(
            api_key=api_key,
            payload=data.payload,
            skip=data.skip_address_verification,
        )
    except AddressSuppressed as exc:
        incr_metric("direct_mail.create.suppressed", piece_type=piece_type, reason=exc.reason)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "address_suppressed",
                "reason": exc.reason,
                "address_hash": exc.address_hash,
            },
        ) from exc
    except AddressUndeliverable as exc:
        incr_metric("direct_mail.create.undeliverable", piece_type=piece_type)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "address_undeliverable",
                "deliverability": exc.deliverability,
                "address_hash": exc.address_hash,
            },
        ) from exc

    # 2. Idempotency key auto-derivation when caller omits.
    idempotency_key = data.idempotency_key
    idempotency_location = data.idempotency_location
    if idempotency_key is None:
        idempotency_key = derive_idempotency_key(piece_type=piece_type, payload=data.payload)
        idempotency_location = idempotency_location or "header"

    # 3. Provider call.
    try:
        provider_piece = create_fn(
            api_key,
            data.payload,
            idempotency_key=idempotency_key,
            idempotency_in_query=(idempotency_location == "query"),
        )
    except LobProviderError as exc:
        _raise_provider_error(operation, exc)
        raise  # unreachable

    if not provider_piece.get("id"):
        incr_metric("direct_mail.provider.bad_response", operation=operation, reason="missing_id")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "type": "provider_bad_response",
                "provider": "lob",
                "operation": operation,
                "message": f"{piece_type.replace('_', ' ')} create did not return a piece id",
            },
        )

    # 4. Persist.
    persisted = await upsert_piece(
        piece_type=piece_type,
        provider_piece=provider_piece,
        deliverability=verify_result.deliverability,
        created_by_user_id=user.business_user_id,
        is_test_mode=data.test_mode,
        channel_campaign_id=data.channel_campaign_id,
        campaign_id=campaign_tags[1] if campaign_tags else None,
    )

    incr_metric("direct_mail.piece.created", piece_type=piece_type, test_mode=data.test_mode)
    log_event(
        "direct_mail_piece_created",
        piece_type=piece_type,
        external_piece_id=persisted.external_piece_id,
        cost_cents=persisted.cost_cents,
        deliverability=persisted.deliverability,
        is_test_mode=data.test_mode,
        idempotency_key=idempotency_key,
        channel_campaign_id=campaign_tags[0] if campaign_tags else None,
        campaign_id=campaign_tags[1] if campaign_tags else None,
    )
    return _to_piece_response(persisted)


async def _get_piece(piece_type: PieceType, piece_id: str) -> DirectMailPieceResponse:
    persisted = await get_piece_by_external_id(
        external_piece_id=piece_id,
        piece_type=piece_type,
    )
    if persisted is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "piece_not_found", "piece_type": piece_type, "piece_id": piece_id},
        )
    return _to_piece_response(persisted)


async def _cancel_piece(
    *,
    piece_type: PieceType,
    piece_id: str,
    cancel_fn: Callable[..., dict[str, Any]],
    supports_idempotency: bool = True,
) -> DirectMailPieceCancelResponse:
    operation = f"cancel_{piece_type}"
    api_key = _api_key()
    try:
        if supports_idempotency:
            provider_response = cancel_fn(api_key, piece_id)
        else:
            provider_response = cancel_fn(api_key, piece_id)
    except LobProviderError as exc:
        _raise_provider_error(operation, exc)
        raise
    incr_metric("direct_mail.piece.canceled", piece_type=piece_type)
    return DirectMailPieceCancelResponse(
        id=str(provider_response.get("id") or piece_id),
        deleted=bool(provider_response.get("deleted", True)),
        raw_payload=provider_response,
    )


# ---------------------------------------------------------------------------
# Mailer specs (canonical Lob print specifications)
#
# Read-only endpoints that surface direct_mail_specs + direct_mail_design_rules
# to the frontend and to managed-agent MCPs. The validate endpoint is the
# pre-flight every renderer / agent should call before paying Lob to print.
# ---------------------------------------------------------------------------


def _spec_to_response(spec: MailerSpec) -> MailerSpecResponse:
    return MailerSpecResponse(
        id=spec.id,
        mailer_category=spec.mailer_category,  # type: ignore[arg-type]
        variant=spec.variant,
        label=spec.label,
        bleed_w_in=spec.bleed_w_in,
        bleed_h_in=spec.bleed_h_in,
        trim_w_in=spec.trim_w_in,
        trim_h_in=spec.trim_h_in,
        safe_inset_in=spec.safe_inset_in,
        zones=spec.zones,
        folding=spec.folding,
        pagination=spec.pagination,
        address_placement=spec.address_placement,
        envelope=spec.envelope,
        production=spec.production,
        ordering=spec.ordering,
        template_pdf_url=spec.template_pdf_url,
        additional_template_urls=spec.additional_template_urls,
        source_urls=spec.source_urls,
        notes=spec.notes,
    )


@router.get("/specs", response_model=MailerSpecListResponse)
async def list_mailer_specs_route(
    category: str | None = Query(
        default=None,
        description=f"Filter by mailer category. One of: {', '.join(CATEGORY_VALUES)}.",
    ),
    _user: UserContext = Depends(require_operator),
) -> MailerSpecListResponse:
    if category is not None and category not in CATEGORY_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_category",
                "category": category,
                "allowed": list(CATEGORY_VALUES),
            },
        )
    specs = await list_specs(category=category)
    items = [_spec_to_response(s) for s in specs]
    return MailerSpecListResponse(count=len(items), specs=items)


@router.get("/specs/categories", response_model=MailerCategoryListResponse)
async def list_mailer_spec_categories_route(
    _user: UserContext = Depends(require_operator),
) -> MailerCategoryListResponse:
    rows = await list_categories()
    return MailerCategoryListResponse(
        categories=[MailerCategorySummary(**r) for r in rows],
    )


@router.get("/specs/design-rules", response_model=MailerDesignRulesResponse)
async def list_mailer_design_rules_route(
    _user: UserContext = Depends(require_operator),
) -> MailerDesignRulesResponse:
    rows = await list_design_rules()
    return MailerDesignRulesResponse(rules=[MailerDesignRule(**r) for r in rows])


@router.get(
    "/specs/{category}/{variant}",
    response_model=MailerSpecResponse,
)
async def get_mailer_spec_route(
    category: str,
    variant: str,
    _user: UserContext = Depends(require_operator),
) -> MailerSpecResponse:
    if category not in CATEGORY_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_category",
                "category": category,
                "allowed": list(CATEGORY_VALUES),
            },
        )
    spec = await get_spec(category, variant)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "spec_not_found", "category": category, "variant": variant},
        )
    return _spec_to_response(spec)


@router.post(
    "/specs/{category}/{variant}/validate",
    response_model=MailerSpecValidationResponse,
)
async def validate_mailer_spec_route(
    category: str,
    variant: str,
    body: MailerSpecValidationRequest,
    _user: UserContext = Depends(require_operator),
) -> MailerSpecValidationResponse:
    if category not in CATEGORY_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_category",
                "category": category,
                "allowed": list(CATEGORY_VALUES),
            },
        )
    spec = await get_spec(category, variant)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "spec_not_found", "category": category, "variant": variant},
        )
    report = validate_artwork_dimensions(
        spec,
        width_in=body.width_in,
        height_in=body.height_in,
        dpi=body.dpi,
        panel=body.panel,
    )
    incr_metric(
        "direct_mail.specs.validate",
        category=category,
        variant=variant,
        is_valid=report.is_valid,
    )
    return MailerSpecValidationResponse(
        spec=_spec_to_response(spec),
        is_valid=report.is_valid,
        error_count=report.error_count,
        warning_count=report.warning_count,
        checks=[
            {
                "code": c.code,
                "severity": c.severity,
                "message": c.message,
                "expected": c.expected,
                "actual": c.actual,
            }
            for c in report.checks
        ],
    )


# ---------------------------------------------------------------------------
# Address verification
# ---------------------------------------------------------------------------


@router.post("/verify-address/us", response_model=DirectMailAddressVerificationResponse)
async def verify_address_us(
    request: DirectMailAddressVerificationUSRequest,
    test_mode: bool = Query(default=False),
    _user: UserContext = Depends(require_operator),
) -> DirectMailAddressVerificationResponse:
    api_key = _api_key(test_mode=test_mode)
    try:
        result = lob_client.verify_address_us_single(api_key, request.payload)
    except LobProviderError as exc:
        _raise_provider_error("verify_address_us", exc)
        raise
    incr_metric("direct_mail.verify.requested", scope="single", test_mode=test_mode)
    return DirectMailAddressVerificationResponse(result=result)


@router.post("/verify-address/us/bulk", response_model=DirectMailAddressVerificationResponse)
async def verify_address_us_bulk(
    request: DirectMailAddressVerificationUSBulkRequest,
    test_mode: bool = Query(default=False),
    _user: UserContext = Depends(require_operator),
) -> DirectMailAddressVerificationResponse:
    api_key = _api_key(test_mode=test_mode)
    try:
        result = lob_client.verify_address_us_bulk(api_key, request.payload)
    except LobProviderError as exc:
        _raise_provider_error("verify_address_us_bulk", exc)
        raise
    incr_metric("direct_mail.verify.requested", scope="bulk", test_mode=test_mode)
    return DirectMailAddressVerificationResponse(result=result)


# ---------------------------------------------------------------------------
# Postcards
# ---------------------------------------------------------------------------


@router.post("/postcards", response_model=DirectMailPieceResponse)
async def create_postcard_route(
    data: DirectMailPieceCreateRequest,
    user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _create_piece(
        piece_type="postcard",
        data=data,
        create_fn=lob_client.create_postcard,
        user=user,
    )


@router.get("/postcards", response_model=DirectMailPieceListResponse)
async def list_postcards_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceListResponse:
    api_key = _api_key()
    try:
        result = lob_client.list_postcards(api_key, params={"limit": limit})
    except LobProviderError as exc:
        _raise_provider_error("list_postcards", exc)
        raise
    return DirectMailPieceListResponse(
        data=result.get("data", []),
        next_url=result.get("next_url"),
        previous_url=result.get("previous_url"),
        count=result.get("count"),
    )


@router.get("/postcards/{piece_id}", response_model=DirectMailPieceResponse)
async def get_postcard_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _get_piece("postcard", piece_id)


@router.post("/postcards/{piece_id}/cancel", response_model=DirectMailPieceCancelResponse)
async def cancel_postcard_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceCancelResponse:
    return await _cancel_piece(
        piece_type="postcard",
        piece_id=piece_id,
        cancel_fn=lob_client.cancel_postcard,
    )


# ---------------------------------------------------------------------------
# Letters
# ---------------------------------------------------------------------------


@router.post("/letters", response_model=DirectMailPieceResponse)
async def create_letter_route(
    data: DirectMailPieceCreateRequest,
    user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _create_piece(
        piece_type="letter",
        data=data,
        create_fn=lob_client.create_letter,
        user=user,
    )


@router.get("/letters", response_model=DirectMailPieceListResponse)
async def list_letters_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceListResponse:
    api_key = _api_key()
    try:
        result = lob_client.list_letters(api_key, params={"limit": limit})
    except LobProviderError as exc:
        _raise_provider_error("list_letters", exc)
        raise
    return DirectMailPieceListResponse(
        data=result.get("data", []),
        next_url=result.get("next_url"),
        previous_url=result.get("previous_url"),
        count=result.get("count"),
    )


@router.get("/letters/{piece_id}", response_model=DirectMailPieceResponse)
async def get_letter_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _get_piece("letter", piece_id)


@router.post("/letters/{piece_id}/cancel", response_model=DirectMailPieceCancelResponse)
async def cancel_letter_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceCancelResponse:
    return await _cancel_piece(
        piece_type="letter",
        piece_id=piece_id,
        cancel_fn=lob_client.cancel_letter,
    )


# ---------------------------------------------------------------------------
# Self-mailers
# ---------------------------------------------------------------------------


@router.post("/self-mailers", response_model=DirectMailPieceResponse)
async def create_self_mailer_route(
    data: DirectMailPieceCreateRequest,
    user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _create_piece(
        piece_type="self_mailer",
        data=data,
        create_fn=lob_client.create_self_mailer,
        user=user,
    )


@router.get("/self-mailers", response_model=DirectMailPieceListResponse)
async def list_self_mailers_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceListResponse:
    api_key = _api_key()
    try:
        result = lob_client.list_self_mailers(api_key, params={"limit": limit})
    except LobProviderError as exc:
        _raise_provider_error("list_self_mailers", exc)
        raise
    return DirectMailPieceListResponse(
        data=result.get("data", []),
        next_url=result.get("next_url"),
        previous_url=result.get("previous_url"),
        count=result.get("count"),
    )


@router.get("/self-mailers/{piece_id}", response_model=DirectMailPieceResponse)
async def get_self_mailer_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _get_piece("self_mailer", piece_id)


@router.post("/self-mailers/{piece_id}/cancel", response_model=DirectMailPieceCancelResponse)
async def cancel_self_mailer_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceCancelResponse:
    return await _cancel_piece(
        piece_type="self_mailer",
        piece_id=piece_id,
        cancel_fn=lob_client.cancel_self_mailer,
    )


# ---------------------------------------------------------------------------
# Snap packs
# ---------------------------------------------------------------------------


@router.post("/snap-packs", response_model=DirectMailPieceResponse)
async def create_snap_pack_route(
    data: DirectMailPieceCreateRequest,
    user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _create_piece(
        piece_type="snap_pack",
        data=data,
        create_fn=lob_client.create_snap_pack,
        user=user,
    )


@router.get("/snap-packs", response_model=DirectMailPieceListResponse)
async def list_snap_packs_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceListResponse:
    api_key = _api_key()
    try:
        result = lob_client.list_snap_packs(api_key, params={"limit": limit})
    except LobProviderError as exc:
        _raise_provider_error("list_snap_packs", exc)
        raise
    return DirectMailPieceListResponse(
        data=result.get("data", []),
        next_url=result.get("next_url"),
        previous_url=result.get("previous_url"),
        count=result.get("count"),
    )


@router.get("/snap-packs/{piece_id}", response_model=DirectMailPieceResponse)
async def get_snap_pack_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _get_piece("snap_pack", piece_id)


@router.post("/snap-packs/{piece_id}/cancel", response_model=DirectMailPieceCancelResponse)
async def cancel_snap_pack_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceCancelResponse:
    return await _cancel_piece(
        piece_type="snap_pack",
        piece_id=piece_id,
        cancel_fn=lob_client.cancel_snap_pack,
        supports_idempotency=False,
    )


# ---------------------------------------------------------------------------
# Booklets
# ---------------------------------------------------------------------------


@router.post("/booklets", response_model=DirectMailPieceResponse)
async def create_booklet_route(
    data: DirectMailPieceCreateRequest,
    user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _create_piece(
        piece_type="booklet",
        data=data,
        create_fn=lob_client.create_booklet,
        user=user,
    )


@router.get("/booklets", response_model=DirectMailPieceListResponse)
async def list_booklets_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceListResponse:
    api_key = _api_key()
    try:
        result = lob_client.list_booklets(api_key, params={"limit": limit})
    except LobProviderError as exc:
        _raise_provider_error("list_booklets", exc)
        raise
    return DirectMailPieceListResponse(
        data=result.get("data", []),
        next_url=result.get("next_url"),
        previous_url=result.get("previous_url"),
        count=result.get("count"),
    )


@router.get("/booklets/{piece_id}", response_model=DirectMailPieceResponse)
async def get_booklet_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceResponse:
    return await _get_piece("booklet", piece_id)


@router.post("/booklets/{piece_id}/cancel", response_model=DirectMailPieceCancelResponse)
async def cancel_booklet_route(
    piece_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailPieceCancelResponse:
    return await _cancel_piece(
        piece_type="booklet",
        piece_id=piece_id,
        cancel_fn=lob_client.cancel_booklet,
        supports_idempotency=False,
    )


# ---------------------------------------------------------------------------
# Templates + template versions (thin proxies — Lob is source of truth)
# ---------------------------------------------------------------------------


def _proxy(
    operation: str, fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except LobProviderError as exc:
        _raise_provider_error(operation, exc)
        raise


@router.post("/templates", response_model=DirectMailTemplateResponse)
async def create_template_route(
    data: DirectMailTemplateCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateResponse:
    api_key = _api_key()
    result = _proxy(
        "create_template",
        lob_client.create_template,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )
    return DirectMailTemplateResponse(raw_payload=result)


@router.get("/templates", response_model=DirectMailTemplateListResponse)
async def list_templates_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateListResponse:
    api_key = _api_key()
    result = _proxy("list_templates", lob_client.list_templates, api_key, params={"limit": limit})
    return DirectMailTemplateListResponse(raw_payload=result)


@router.get("/templates/{template_id}", response_model=DirectMailTemplateResponse)
async def get_template_route(
    template_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateResponse:
    api_key = _api_key()
    result = _proxy("get_template", lob_client.get_template, api_key, template_id)
    return DirectMailTemplateResponse(raw_payload=result)


@router.patch("/templates/{template_id}", response_model=DirectMailTemplateResponse)
async def update_template_route(
    template_id: str,
    data: DirectMailTemplateUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateResponse:
    api_key = _api_key()
    result = _proxy(
        "update_template",
        lob_client.update_template,
        api_key,
        template_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )
    return DirectMailTemplateResponse(raw_payload=result)


@router.delete("/templates/{template_id}", response_model=DirectMailTemplateResponse)
async def delete_template_route(
    template_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateResponse:
    api_key = _api_key()
    result = _proxy("delete_template", lob_client.delete_template, api_key, template_id)
    return DirectMailTemplateResponse(raw_payload=result)


@router.post("/templates/{template_id}/versions", response_model=DirectMailTemplateVersionResponse)
async def create_template_version_route(
    template_id: str,
    data: DirectMailTemplateVersionCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateVersionResponse:
    api_key = _api_key()
    result = _proxy(
        "create_template_version",
        lob_client.create_template_version,
        api_key,
        template_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )
    return DirectMailTemplateVersionResponse(raw_payload=result)


@router.get(
    "/templates/{template_id}/versions", response_model=DirectMailTemplateVersionListResponse
)
async def list_template_versions_route(
    template_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateVersionListResponse:
    api_key = _api_key()
    result = _proxy(
        "list_template_versions",
        lob_client.list_template_versions,
        api_key,
        template_id,
        params={"limit": limit},
    )
    return DirectMailTemplateVersionListResponse(raw_payload=result)


@router.get(
    "/templates/{template_id}/versions/{version_id}",
    response_model=DirectMailTemplateVersionResponse,
)
async def get_template_version_route(
    template_id: str,
    version_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateVersionResponse:
    api_key = _api_key()
    result = _proxy(
        "get_template_version",
        lob_client.get_template_version,
        api_key,
        template_id,
        version_id,
    )
    return DirectMailTemplateVersionResponse(raw_payload=result)


@router.patch(
    "/templates/{template_id}/versions/{version_id}",
    response_model=DirectMailTemplateVersionResponse,
)
async def update_template_version_route(
    template_id: str,
    version_id: str,
    data: DirectMailTemplateVersionUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateVersionResponse:
    api_key = _api_key()
    result = _proxy(
        "update_template_version",
        lob_client.update_template_version,
        api_key,
        template_id,
        version_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )
    return DirectMailTemplateVersionResponse(raw_payload=result)


@router.delete(
    "/templates/{template_id}/versions/{version_id}",
    response_model=DirectMailTemplateVersionResponse,
)
async def delete_template_version_route(
    template_id: str,
    version_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailTemplateVersionResponse:
    api_key = _api_key()
    result = _proxy(
        "delete_template_version",
        lob_client.delete_template_version,
        api_key,
        template_id,
        version_id,
    )
    return DirectMailTemplateVersionResponse(raw_payload=result)


# ---------------------------------------------------------------------------
# Saved addresses (Lob-hosted address book)
# ---------------------------------------------------------------------------


@router.post("/addresses", response_model=DirectMailAddressResponse)
async def create_address_route(
    data: DirectMailAddressCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> DirectMailAddressResponse:
    api_key = _api_key()
    result = _proxy(
        "create_address",
        lob_client.create_address,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )
    return DirectMailAddressResponse(raw_payload=result)


@router.get("/addresses", response_model=DirectMailAddressListResponse)
async def list_addresses_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> DirectMailAddressListResponse:
    api_key = _api_key()
    result = _proxy("list_addresses", lob_client.list_addresses, api_key, params={"limit": limit})
    return DirectMailAddressListResponse(raw_payload=result)


@router.get("/addresses/{address_id}", response_model=DirectMailAddressResponse)
async def get_address_route(
    address_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailAddressResponse:
    api_key = _api_key()
    result = _proxy("get_address", lob_client.get_address, api_key, address_id)
    return DirectMailAddressResponse(raw_payload=result)


@router.delete("/addresses/{address_id}", response_model=DirectMailAddressResponse)
async def delete_address_route(
    address_id: str,
    _user: UserContext = Depends(require_operator),
) -> DirectMailAddressResponse:
    api_key = _api_key()
    result = _proxy("delete_address", lob_client.delete_address, api_key, address_id)
    return DirectMailAddressResponse(raw_payload=result)


# ---------------------------------------------------------------------------
# Buckslips + buckslip orders
# ---------------------------------------------------------------------------


@router.post("/buckslips")
async def create_buckslip_route(
    data: DirectMailBuckslipCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_buckslip",
        lob_client.create_buckslip,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/buckslips")
async def list_buckslips_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("list_buckslips", lob_client.list_buckslips, api_key, params={"limit": limit})


@router.get("/buckslips/{buckslip_id}")
async def get_buckslip_route(
    buckslip_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_buckslip", lob_client.get_buckslip, api_key, buckslip_id)


@router.patch("/buckslips/{buckslip_id}")
async def update_buckslip_route(
    buckslip_id: str,
    data: DirectMailBuckslipUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "update_buckslip",
        lob_client.update_buckslip,
        api_key,
        buckslip_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.delete("/buckslips/{buckslip_id}")
async def delete_buckslip_route(
    buckslip_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("delete_buckslip", lob_client.delete_buckslip, api_key, buckslip_id)


@router.post("/buckslips/{buckslip_id}/orders")
async def create_buckslip_order_route(
    buckslip_id: str,
    data: DirectMailBuckslipOrderCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_buckslip_order",
        lob_client.create_buckslip_order,
        api_key,
        buckslip_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/buckslips/{buckslip_id}/orders")
async def get_buckslip_order_route(
    buckslip_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_buckslip_order", lob_client.get_buckslip_order, api_key, buckslip_id)


# ---------------------------------------------------------------------------
# Cards + card orders
# ---------------------------------------------------------------------------


@router.post("/cards")
async def create_card_route(
    data: DirectMailCardCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_card",
        lob_client.create_card,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/cards")
async def list_cards_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("list_cards", lob_client.list_cards, api_key, params={"limit": limit})


@router.get("/cards/{card_id}")
async def get_card_route(
    card_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_card", lob_client.get_card, api_key, card_id)


@router.patch("/cards/{card_id}")
async def update_card_route(
    card_id: str,
    data: DirectMailCardUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "update_card",
        lob_client.update_card,
        api_key,
        card_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.delete("/cards/{card_id}")
async def delete_card_route(
    card_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("delete_card", lob_client.delete_card, api_key, card_id)


@router.post("/cards/{card_id}/orders")
async def create_card_order_route(
    card_id: str,
    data: DirectMailCardOrderCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_card_order",
        lob_client.create_card_order,
        api_key,
        card_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


@router.post("/campaigns")
async def create_campaign_route(
    data: DirectMailCampaignCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_campaign",
        lob_client.create_campaign,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/campaigns")
async def list_campaigns_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("list_campaigns", lob_client.list_campaigns, api_key, params={"limit": limit})


@router.get("/campaigns/{campaign_id}")
async def get_campaign_route(
    campaign_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_campaign", lob_client.get_campaign, api_key, campaign_id)


@router.patch("/campaigns/{campaign_id}")
async def update_campaign_route(
    campaign_id: str,
    data: DirectMailCampaignUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "update_campaign",
        lob_client.update_campaign,
        api_key,
        campaign_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign_route(
    campaign_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("delete_campaign", lob_client.delete_campaign, api_key, campaign_id)


@router.post("/campaigns/{campaign_id}/send")
async def send_campaign_route(
    campaign_id: str,
    test_mode: bool = Query(default=False),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    """Hand off to Lob's Campaigns send endpoint.

    Pieces that come back from this campaign land via webhooks and are
    upserted at that point — this route does NOT _upsert_piece (mirrors the
    OEX comment "Creatives (Lob-hosted — NO _upsert_piece)").
    """
    api_key = _api_key(test_mode=test_mode)
    return _proxy("send_campaign", lob_client.send_campaign, api_key, campaign_id)


# ---------------------------------------------------------------------------
# Creatives
# ---------------------------------------------------------------------------


@router.post("/creatives")
async def create_creative_route(
    data: DirectMailCreativeCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_creative",
        lob_client.create_creative,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/creatives/{creative_id}")
async def get_creative_route(
    creative_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_creative", lob_client.get_creative, api_key, creative_id)


@router.patch("/creatives/{creative_id}")
async def update_creative_route(
    creative_id: str,
    data: DirectMailCreativeUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "update_creative",
        lob_client.update_creative,
        api_key,
        creative_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


# ---------------------------------------------------------------------------
# Uploads (bulk file ingestion)
# ---------------------------------------------------------------------------


@router.post("/uploads")
async def create_upload_route(
    data: DirectMailUploadCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("create_upload", lob_client.create_upload, api_key, data.payload)


@router.get("/uploads")
async def list_uploads_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("list_uploads", lob_client.list_uploads, api_key, params={"limit": limit})


@router.get("/uploads/{upload_id}")
async def get_upload_route(
    upload_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_upload", lob_client.get_upload, api_key, upload_id)


@router.patch("/uploads/{upload_id}")
async def update_upload_route(
    upload_id: str,
    data: DirectMailUploadUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("update_upload", lob_client.update_upload, api_key, upload_id, data.payload)


@router.delete("/uploads/{upload_id}")
async def delete_upload_route(
    upload_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("delete_upload", lob_client.delete_upload, api_key, upload_id)


@router.post("/uploads/{upload_id}/file")
async def upload_file_route(
    upload_id: str,
    file: UploadFile,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    content = await file.read()
    return _proxy(
        "upload_file",
        lob_client.upload_file,
        api_key,
        upload_id,
        file_name=file.filename or "upload.csv",
        file_content=content,
        content_type=file.content_type or "text/csv",
    )


@router.post("/uploads/{upload_id}/exports")
async def create_upload_export_route(
    upload_id: str,
    data: DirectMailUploadExportCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_upload_export", lob_client.create_upload_export, api_key, upload_id, data.payload
    )


@router.get("/uploads/{upload_id}/exports/{export_id}")
async def get_upload_export_route(
    upload_id: str,
    export_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_upload_export", lob_client.get_upload_export, api_key, upload_id, export_id)


@router.get("/uploads/{upload_id}/report")
async def get_upload_report_route(
    upload_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_upload_report", lob_client.get_upload_report, api_key, upload_id)


# ---------------------------------------------------------------------------
# Resource proofs (PDF previews of pieces before printing)
# ---------------------------------------------------------------------------


@router.post("/resource-proofs")
async def create_resource_proof_route(
    data: DirectMailResourceProofCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_resource_proof",
        lob_client.create_resource_proof,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/resource-proofs/{proof_id}")
async def get_resource_proof_route(
    proof_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_resource_proof", lob_client.get_resource_proof, api_key, proof_id)


@router.patch("/resource-proofs/{proof_id}")
async def update_resource_proof_route(
    proof_id: str,
    data: DirectMailResourceProofUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "update_resource_proof",
        lob_client.update_resource_proof,
        api_key,
        proof_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


# ---------------------------------------------------------------------------
# QR code analytics
# ---------------------------------------------------------------------------


@router.get("/qr-code-analytics")
async def list_qr_code_analytics_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "list_qr_code_analytics",
        lob_client.list_qr_code_analytics,
        api_key,
        params={"limit": limit},
    )


# ---------------------------------------------------------------------------
# Tracking domains
# ---------------------------------------------------------------------------


@router.post("/domains")
async def create_domain_route(
    data: DirectMailDomainCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_domain",
        lob_client.create_domain,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/domains")
async def list_domains_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("list_domains", lob_client.list_domains, api_key, params={"limit": limit})


@router.get("/domains/{domain_id}")
async def get_domain_route(
    domain_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_domain", lob_client.get_domain, api_key, domain_id)


@router.delete("/domains/{domain_id}")
async def delete_domain_route(
    domain_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("delete_domain", lob_client.delete_domain, api_key, domain_id)


# ---------------------------------------------------------------------------
# Trackable links
# ---------------------------------------------------------------------------


@router.post("/links")
async def create_link_route(
    data: DirectMailLinkCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_link",
        lob_client.create_link,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/links")
async def list_links_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("list_links", lob_client.list_links, api_key, params={"limit": limit})


@router.get("/links/{link_id}")
async def get_link_route(
    link_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_link", lob_client.get_link, api_key, link_id)


@router.patch("/links/{link_id}")
async def update_link_route(
    link_id: str,
    data: DirectMailLinkUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "update_link",
        lob_client.update_link,
        api_key,
        link_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.delete("/links/{link_id}")
async def delete_link_route(
    link_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("delete_link", lob_client.delete_link, api_key, link_id)


# ---------------------------------------------------------------------------
# Billing groups
# ---------------------------------------------------------------------------


@router.post("/billing-groups")
async def create_billing_group_route(
    data: DirectMailBillingGroupCreateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "create_billing_group",
        lob_client.create_billing_group,
        api_key,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )


@router.get("/billing-groups")
async def list_billing_groups_route(
    limit: int = Query(default=10, ge=1, le=100),
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "list_billing_groups",
        lob_client.list_billing_groups,
        api_key,
        params={"limit": limit},
    )


@router.get("/billing-groups/{billing_group_id}")
async def get_billing_group_route(
    billing_group_id: str,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy("get_billing_group", lob_client.get_billing_group, api_key, billing_group_id)


@router.patch("/billing-groups/{billing_group_id}")
async def update_billing_group_route(
    billing_group_id: str,
    data: DirectMailBillingGroupUpdateRequest,
    _user: UserContext = Depends(require_operator),
) -> dict[str, Any]:
    api_key = _api_key()
    return _proxy(
        "update_billing_group",
        lob_client.update_billing_group,
        api_key,
        billing_group_id,
        data.payload,
        idempotency_key=data.idempotency_key,
        idempotency_in_query=(data.idempotency_location == "query"),
    )
