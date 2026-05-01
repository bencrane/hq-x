"""Per-piece direct-mail activation primitive.

The substrate for owned-brand initiatives that need bespoke per-recipient
HTML/PDF on every direct-mail piece in the sequence (see
docs/strategic-direction-owned-brand-leadgen.md §4).

Bypasses Lob's Campaigns API entirely — every piece is a single Print &
Mail API call. The Campaigns API path
(`app/services/dmaas_campaign_activation.py`,
`app/services/lob_audience_csv.py`) is **untouched** and continues to
serve audience-shared-creative DMaaS sends. This module is the audience-
size-1 / per-recipient path.

Single provider in this file (Lob). PostGrid notes live in
`docs/research/postgrid-print-mail-api-notes.md`. There is intentionally
no `DirectMailProvider` ABC / Protocol — provider abstraction lands when
a second provider is actually being integrated. Until then the canonical
`piece.*` event vocabulary in `app/webhooks/lob_normalization.py` is the
boundary that survives the eventual port; see
`docs/research/canonical-piece-event-taxonomy.md`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.config import settings
from app.direct_mail.addresses import (
    address_hash_for,
    is_address_suppressed,
)
from app.direct_mail.persistence import UpsertedPiece, upsert_piece
from app.providers.lob import client as lob_client
from app.providers.lob.client import LobProviderError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec discriminated union — one shape per piece type. extra=forbid catches
# cross-type field-name confusion at construction time (e.g. `front` on a
# letter, `file` on a self-mailer) before Lob would.
# ---------------------------------------------------------------------------


class _PieceSpecBase(BaseModel):
    """Fields every piece type carries."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    # Back-references. Optional today (the step-activation directive will
    # set them). The primitive round-trips them onto direct_mail_pieces.metadata
    # under reserved keys (_recipient_id, _channel_campaign_step_id,
    # _membership_id) so they remain queryable via JSONB without touching
    # the persistence layer's signature or FK columns.
    recipient_id: UUID | None = None
    channel_campaign_step_id: UUID | None = None
    membership_id: UUID | None = None

    # Recipient. Either an existing adr_id or an inline address dict.
    to: str | dict[str, Any]
    # From. Either an adr_id or inline. Lob requires this to be set.
    from_: str | dict[str, Any] = Field(alias="from")

    # Common Lob controls.
    use_type: Literal["marketing", "operational"] = "marketing"
    mail_type: Literal["usps_first_class", "usps_standard"] | None = None
    merge_variables: dict[str, Any] | None = None
    send_date: str | None = None
    metadata: dict[str, str] | None = None
    description: str | None = None

    # Stable seed → sha256 → Idempotency-Key. Same seed = same key across
    # processes and restarts. Re-running activate_pieces_batch with the
    # same seeds is a safe no-op: Lob returns the previously-created piece
    # and upsert_piece reconciles it idempotently.
    idempotency_seed: str = Field(min_length=1)


class PostcardSpec(_PieceSpecBase):
    piece_type: Literal["postcard"]
    front: str
    back: str
    # Lob's postcard_size enum: "4x6" | "6x9" | "6x11".
    size: Literal["4x6", "6x9", "6x11"] = "4x6"


class SelfMailerSpec(_PieceSpecBase):
    piece_type: Literal["self_mailer"]
    inside: str
    outside: str
    # Lob's self_mailer_size enum (17.75x9_trifold is in beta).
    size: Literal[
        "6x18_bifold", "11x9_bifold", "12x9_bifold", "17.75x9_trifold"
    ] = "11x9_bifold"


class LetterSpec(_PieceSpecBase):
    piece_type: Literal["letter"]
    file: str
    color: bool = False
    double_sided: bool = True
    address_placement: Literal["top_first_page", "insert_blank_page"] = (
        "top_first_page"
    )
    # Letter add-ons (all optional, all pass through to Lob untouched).
    extra_service: Literal[
        "certified", "registered", "certified_return_receipt"
    ] | None = None
    return_envelope: str | None = None
    perforated_page: int | None = None
    cards: list[str] | None = None
    buckslips: list[str] | None = None


class SnapPackSpec(_PieceSpecBase):
    piece_type: Literal["snap_pack"]
    inside: str
    outside: str
    size: Literal["8.5x11"] = "8.5x11"
    color: bool = False


class BookletSpec(_PieceSpecBase):
    piece_type: Literal["booklet"]
    file: str
    size: Literal["8.375x5.375", "8.25x5.5", "8.5x5.5"] = "8.375x5.375"


PieceSpec = Annotated[
    PostcardSpec | SelfMailerSpec | LetterSpec | SnapPackSpec | BookletSpec,
    Field(discriminator="piece_type"),
]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class PieceResult(BaseModel):
    spec_index: int
    piece_type: Literal[
        "postcard", "self_mailer", "letter", "snap_pack", "booklet"
    ]
    status: Literal["created", "skipped_suppressed", "failed"]
    piece_id: UUID | None = None
    external_piece_id: str | None = None
    error_code: str | None = None
    error_detail: dict[str, Any] | None = None


class ActivationBatchResult(BaseModel):
    correlation_id: str
    total: int
    created: int
    skipped: int
    failed: int
    results: list[PieceResult]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_MAX_INFLIGHT = 8


def _compute_idempotency_key(seed: str) -> str:
    """Hash the caller-supplied stable seed into a Lob-acceptable key.

    Same seed → same key across processes. The "hqx-pma-v1-" prefix lets
    Lob support flag this primitive's sends if they ever need to.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"hqx-pma-v1-{digest[:48]}"


def _api_key_for(test_mode: bool) -> str:
    if test_mode:
        key = settings.LOB_API_KEY_TEST
        if not key:
            raise RuntimeError(
                "LOB_API_KEY_TEST is not set; cannot run activation in test_mode"
            )
        return key
    key = settings.LOB_API_KEY
    if not key:
        raise RuntimeError("LOB_API_KEY is not set; cannot run activation")
    return key


def _build_postcard_payload(spec: PostcardSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "to": spec.to,
        "from": spec.from_,
        "front": spec.front,
        "back": spec.back,
        "size": spec.size,
        "use_type": spec.use_type,
    }
    _attach_common(payload, spec)
    return payload


def _build_self_mailer_payload(spec: SelfMailerSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "to": spec.to,
        "from": spec.from_,
        "inside": spec.inside,
        "outside": spec.outside,
        "size": spec.size,
        "use_type": spec.use_type,
    }
    _attach_common(payload, spec)
    return payload


def _build_letter_payload(spec: LetterSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "to": spec.to,
        "from": spec.from_,
        "file": spec.file,
        "color": spec.color,
        "double_sided": spec.double_sided,
        "address_placement": spec.address_placement,
        "use_type": spec.use_type,
    }
    if spec.extra_service is not None:
        payload["extra_service"] = spec.extra_service
    if spec.return_envelope is not None:
        payload["return_envelope"] = spec.return_envelope
    if spec.perforated_page is not None:
        payload["perforated_page"] = spec.perforated_page
    if spec.cards is not None:
        payload["cards"] = spec.cards
    if spec.buckslips is not None:
        payload["buckslips"] = spec.buckslips
    _attach_common(payload, spec)
    return payload


def _build_snap_pack_payload(spec: SnapPackSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "to": spec.to,
        "from": spec.from_,
        "inside": spec.inside,
        "outside": spec.outside,
        "size": spec.size,
        "color": spec.color,
        "use_type": spec.use_type,
    }
    _attach_common(payload, spec)
    return payload


def _build_booklet_payload(spec: BookletSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "to": spec.to,
        "from": spec.from_,
        "file": spec.file,
        "size": spec.size,
        "use_type": spec.use_type,
    }
    _attach_common(payload, spec)
    return payload


def _attach_common(payload: dict[str, Any], spec: _PieceSpecBase) -> None:
    if spec.mail_type is not None:
        payload["mail_type"] = spec.mail_type
    if spec.merge_variables is not None:
        payload["merge_variables"] = spec.merge_variables
    if spec.send_date is not None:
        payload["send_date"] = spec.send_date
    if spec.metadata is not None:
        payload["metadata"] = spec.metadata
    if spec.description is not None:
        payload["description"] = spec.description


_DISPATCH: dict[
    str,
    tuple[
        Any,  # builder: (spec) -> dict
        Any,  # lob create fn name on lob_client
    ],
] = {
    "postcard": (_build_postcard_payload, "create_postcard"),
    "self_mailer": (_build_self_mailer_payload, "create_self_mailer"),
    "letter": (_build_letter_payload, "create_letter"),
    "snap_pack": (_build_snap_pack_payload, "create_snap_pack"),
    "booklet": (_build_booklet_payload, "create_booklet"),
}


def _backref_metadata(spec: _PieceSpecBase) -> dict[str, Any]:
    """Reserved-key encoding of back-references into JSONB metadata.

    Read by analytics rollups and by the seed/test surfaces. Persisted as
    strings so JSONB extraction (`->>`) returns a string round-trippable
    to UUID. The persistence layer's dedicated FK columns
    (recipient_id, channel_campaign_step_id) are deliberately left NULL —
    those are FK-constrained and the activation primitive may run with
    membership IDs whose targets exist only in metadata-land (e.g.
    activation tests, ad-hoc operator runs).
    """
    out: dict[str, Any] = {}
    if spec.recipient_id is not None:
        out["_recipient_id"] = str(spec.recipient_id)
    if spec.channel_campaign_step_id is not None:
        out["_channel_campaign_step_id"] = str(spec.channel_campaign_step_id)
    if spec.membership_id is not None:
        out["_membership_id"] = str(spec.membership_id)
    return out


async def _check_suppressed(spec: _PieceSpecBase) -> dict[str, Any] | None:
    """Pre-Lob suppression check. Returns the suppression row if the
    recipient is on the suppression list, else None.

    When `to` is a string (Lob saved-address id), suppression cannot be
    checked locally — return None and let Lob handle.
    """
    if isinstance(spec.to, str):
        return None
    if not isinstance(spec.to, dict):
        return None
    address_hash = address_hash_for(spec.to)
    return await is_address_suppressed(address_hash)


async def _dispatch_one(
    *,
    spec_index: int,
    spec: _PieceSpecBase,
    api_key: str,
    test_mode: bool,
    correlation_id: str,
    semaphore: asyncio.Semaphore,
    lob_module: Any,
) -> PieceResult:
    """End-to-end: suppression-check → Lob create → upsert. One spec in,
    one PieceResult out. Never raises — every error becomes a result row."""
    piece_type: str = spec.piece_type  # type: ignore[attr-defined]

    blocking = await _check_suppressed(spec)
    if blocking is not None:
        return PieceResult(
            spec_index=spec_index,
            piece_type=piece_type,  # type: ignore[arg-type]
            status="skipped_suppressed",
            error_code="suppressed",
            error_detail={"reason": blocking.get("reason")},
        )

    builder, create_fn_name = _DISPATCH[piece_type]
    payload = builder(spec)
    idempotency_key = _compute_idempotency_key(spec.idempotency_seed)
    create_fn = getattr(lob_module, create_fn_name)

    async with semaphore:
        try:
            provider_piece = await asyncio.to_thread(
                create_fn,
                api_key,
                payload,
                idempotency_key=idempotency_key,
            )
        except LobProviderError as exc:
            logger.warning(
                "print_mail_activation.lob_error type=%s spec_index=%d corr=%s err=%s",
                piece_type,
                spec_index,
                correlation_id,
                str(exc)[:200],
            )
            return PieceResult(
                spec_index=spec_index,
                piece_type=piece_type,  # type: ignore[arg-type]
                status="failed",
                error_code=exc.category,
                error_detail={"message": str(exc)[:300]},
            )
        except Exception as exc:  # noqa: BLE001 — never break the batch
            logger.exception(
                "print_mail_activation.unexpected type=%s spec_index=%d corr=%s",
                piece_type,
                spec_index,
                correlation_id,
            )
            return PieceResult(
                spec_index=spec_index,
                piece_type=piece_type,  # type: ignore[arg-type]
                status="failed",
                error_code="internal_error",
                error_detail={"message": str(exc)[:300]},
            )

    if not isinstance(provider_piece, dict) or not provider_piece.get("id"):
        return PieceResult(
            spec_index=spec_index,
            piece_type=piece_type,  # type: ignore[arg-type]
            status="failed",
            error_code="provider_bad_response",
            error_detail={"message": "Lob create returned no piece id"},
        )

    metadata = _backref_metadata(spec)
    if spec.metadata:
        metadata = {**spec.metadata, **metadata}

    try:
        persisted: UpsertedPiece = await upsert_piece(
            piece_type=piece_type,
            provider_piece=provider_piece,
            deliverability=None,
            created_by_user_id=None,
            is_test_mode=test_mode,
            metadata=metadata or None,
            provider_slug="lob",
        )
    except Exception as exc:  # noqa: BLE001 — must surface to caller as a result
        logger.exception(
            "print_mail_activation.persistence_failed type=%s ext=%s corr=%s",
            piece_type,
            provider_piece.get("id"),
            correlation_id,
        )
        return PieceResult(
            spec_index=spec_index,
            piece_type=piece_type,  # type: ignore[arg-type]
            status="failed",
            error_code="persistence_failed",
            error_detail={
                "message": str(exc)[:300],
                "lob_external_piece_id": provider_piece.get("id"),
            },
        )

    return PieceResult(
        spec_index=spec_index,
        piece_type=piece_type,  # type: ignore[arg-type]
        status="created",
        piece_id=persisted.id,
        external_piece_id=persisted.external_piece_id,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def activate_pieces_batch(
    *,
    organization_id: UUID,
    pieces: list[PieceSpec],
    test_mode: bool = False,
    correlation_id: str | None = None,
) -> ActivationBatchResult:
    """Mint one Lob Print & Mail piece per spec, in parallel (bounded).

    Per-piece isolation: a failure on piece N never aborts the batch.
    Every spec produces exactly one PieceResult.

    Suppression is pre-checked against `suppressed_addresses` before
    calling Lob. Suppressed pieces return status='skipped_suppressed'
    with no Lob call.

    Idempotency: the spec's `idempotency_seed` hashes deterministically
    into Lob's `Idempotency-Key`. Same `(organization_id, pieces)`
    re-run is safe — Lob returns the previously-created piece, and
    `upsert_piece` reconciles via `(provider_slug, external_piece_id)`.

    `organization_id` is the activating org. It is currently only logged
    — back-references that scope rows to the org go through metadata
    (see `_backref_metadata`). The parameter is required so the public
    contract matches the eventual step-activation surface (which will
    use it for billing / quota / audit) without a signature change later.
    """
    correlation = correlation_id or uuid.uuid4().hex
    api_key = _api_key_for(test_mode=test_mode)
    semaphore = asyncio.Semaphore(_MAX_INFLIGHT)

    logger.info(
        "print_mail_activation.batch_start org=%s n=%d test_mode=%s corr=%s",
        organization_id,
        len(pieces),
        test_mode,
        correlation,
    )

    coros = [
        _dispatch_one(
            spec_index=idx,
            spec=spec,
            api_key=api_key,
            test_mode=test_mode,
            correlation_id=correlation,
            semaphore=semaphore,
            lob_module=lob_client,
        )
        for idx, spec in enumerate(pieces)
    ]
    results = list(await asyncio.gather(*coros))

    created = sum(1 for r in results if r.status == "created")
    skipped = sum(1 for r in results if r.status == "skipped_suppressed")
    failed = sum(1 for r in results if r.status == "failed")

    logger.info(
        "print_mail_activation.batch_done org=%s total=%d created=%d "
        "skipped=%d failed=%d corr=%s",
        organization_id,
        len(results),
        created,
        skipped,
        failed,
        correlation,
    )

    return ActivationBatchResult(
        correlation_id=correlation,
        total=len(results),
        created=created,
        skipped=skipped,
        failed=failed,
        results=results,
    )
