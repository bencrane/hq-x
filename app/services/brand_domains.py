"""Brand-level custom-domain bindings (Dub link host + landing-page host).

Two JSONB columns on `business.brands` carry these bindings:

  * `dub_domain_config`         — the Dub-served short-link host. Setting
    this calls Dub `POST /domains` to register the host on the workspace
    and persists the returned domain object id. After this is set,
    step-link minting will pass `domain=<config.domain>` to Dub when
    creating links so recipients see `track.acme.com/abc` instead of the
    workspace default `dub.sh/abc`.

  * `landing_page_domain_config` — the host customers' recipients see
    AFTER they click. Backed by an existing
    `business.entri_domain_connections` row (Entri Power proxies the
    customer's hostname to our backend + auto-provisions Let's Encrypt).
    Setting this links a brand to an entri connection and stamps the
    connection row with `brand_id` for reverse lookups.

Both bindings are independent: a brand may configure either, both, or
neither. Re-registering a domain that's already configured is a no-op +
returns the existing config (idempotent on the customer's side).

Org isolation is enforced by every read/write taking the caller's
`organization_id` and joining brand → org. Cross-org access surfaces as
`BrandNotFoundError` (caller maps to 404).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import get_db_connection
from app.dmaas import entri_domains as entri_repo
from app.observability import incr_metric
from app.providers.dub import client as dub_client
from app.providers.dub.client import DubProviderError

logger = logging.getLogger(__name__)


class BrandNotFoundError(Exception):
    """Brand does not exist or is not in the caller's org."""


class EntriConnectionNotFoundError(Exception):
    """Referenced entri_connection_id does not exist or is in a different org."""


class DubNotConfiguredError(Exception):
    """DUB_API_KEY is not set; cannot register a Dub domain."""


@dataclass(frozen=True)
class DubDomainBinding:
    domain: str
    dub_domain_id: str
    verified_at: datetime


@dataclass(frozen=True)
class LandingPageDomainBinding:
    domain: str
    entri_connection_id: UUID
    verified_at: datetime


@dataclass(frozen=True)
class BrandDomainConfigs:
    brand_id: UUID
    dub: DubDomainBinding | None
    landing_page: LandingPageDomainBinding | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _load_brand_org_and_configs(
    *, brand_id: UUID, organization_id: UUID
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    """Return (brand_exists_in_org, dub_config_jsonb, landing_config_jsonb).

    The first element is True when a brand row matches (id, organization_id)
    and is not soft-deleted, regardless of whether the JSONB columns are
    populated. Callers raise BrandNotFoundError when False.
    """
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT dub_domain_config, landing_page_domain_config
            FROM business.brands
            WHERE id = %s AND organization_id = %s AND deleted_at IS NULL
            """,
            (str(brand_id), str(organization_id)),
        )
        row = await cur.fetchone()
    if row is None:
        return False, None, None
    return True, row[0], row[1]


def _to_dub_binding(jsonb: dict[str, Any] | None) -> DubDomainBinding | None:
    if not jsonb:
        return None
    domain = jsonb.get("domain")
    dub_id = jsonb.get("dub_domain_id")
    verified = jsonb.get("verified_at")
    if not (
        isinstance(domain, str)
        and isinstance(dub_id, str)
        and isinstance(verified, str)
    ):
        return None
    return DubDomainBinding(
        domain=domain,
        dub_domain_id=dub_id,
        verified_at=datetime.fromisoformat(verified),
    )


def _to_landing_binding(
    jsonb: dict[str, Any] | None,
) -> LandingPageDomainBinding | None:
    if not jsonb:
        return None
    domain = jsonb.get("domain")
    entri_id = jsonb.get("entri_connection_id")
    verified = jsonb.get("verified_at")
    if not (
        isinstance(domain, str)
        and isinstance(entri_id, str)
        and isinstance(verified, str)
    ):
        return None
    return LandingPageDomainBinding(
        domain=domain,
        entri_connection_id=UUID(entri_id),
        verified_at=datetime.fromisoformat(verified),
    )


async def get_brand_domain_configs(
    *, brand_id: UUID, organization_id: UUID
) -> BrandDomainConfigs:
    exists, dub_jsonb, landing_jsonb = await _load_brand_org_and_configs(
        brand_id=brand_id, organization_id=organization_id
    )
    if not exists:
        raise BrandNotFoundError(f"brand {brand_id} not found in org {organization_id}")
    return BrandDomainConfigs(
        brand_id=brand_id,
        dub=_to_dub_binding(dub_jsonb),
        landing_page=_to_landing_binding(landing_jsonb),
    )


# ---------------------------------------------------------------------------
# Dub link-host binding
# ---------------------------------------------------------------------------


async def register_dub_domain_for_brand(
    *,
    brand_id: UUID,
    organization_id: UUID,
    domain: str,
) -> DubDomainBinding:
    """Register `domain` as a Dub link host for the brand.

    Idempotent: if the brand already has a `dub_domain_config` for the
    same domain, returns the existing binding without re-registering. If
    the same domain is already registered on Dub's side under a different
    workspace path (rare), we treat the existing Dub row as authoritative
    and persist its id.
    """
    if settings.DUB_API_KEY is None:
        raise DubNotConfiguredError("DUB_API_KEY is not set")
    api_key = settings.DUB_API_KEY.get_secret_value()
    base_url = settings.DUB_API_BASE_URL

    configs = await get_brand_domain_configs(
        brand_id=brand_id, organization_id=organization_id
    )

    if configs.dub is not None and configs.dub.domain.lower() == domain.lower():
        incr_metric("brand_domains.dub.idempotent_hit")
        return configs.dub

    # Check if Dub already knows this domain (in another binding) — re-use.
    existing = dub_client.get_domain_by_slug(api_key=api_key, slug=domain, base_url=base_url)
    if existing is None:
        try:
            created = dub_client.create_domain(
                api_key=api_key, slug=domain, base_url=base_url
            )
        except DubProviderError as exc:
            incr_metric(
                "brand_domains.dub.create_error",
                category=exc.category,
                status=str(exc.status) if exc.status is not None else "none",
            )
            raise
        dub_id = str(created.get("id") or "")
        if not dub_id:
            raise DubProviderError("Dub create_domain returned no id")
    else:
        dub_id = str(existing.get("id") or "")
        if not dub_id:
            raise DubProviderError("Dub domain object missing id")
        incr_metric("brand_domains.dub.reused_existing")

    binding = DubDomainBinding(
        domain=domain,
        dub_domain_id=dub_id,
        verified_at=datetime.now(UTC),
    )
    await _persist_dub_config(brand_id=brand_id, organization_id=organization_id, binding=binding)
    incr_metric("brand_domains.dub.registered")
    return binding


async def deregister_dub_domain_for_brand(
    *,
    brand_id: UUID,
    organization_id: UUID,
) -> bool:
    """Remove the brand's Dub binding. Returns True if a binding was present."""
    configs = await get_brand_domain_configs(
        brand_id=brand_id, organization_id=organization_id
    )
    if configs.dub is None:
        return False
    if settings.DUB_API_KEY is not None:
        api_key = settings.DUB_API_KEY.get_secret_value()
        base_url = settings.DUB_API_BASE_URL
        try:
            dub_client.delete_domain(
                api_key=api_key, slug=configs.dub.domain, base_url=base_url
            )
        except DubProviderError as exc:
            # 404 = already gone on Dub's side; that's fine.
            if exc.status != 404:
                incr_metric(
                    "brand_domains.dub.delete_error",
                    category=exc.category,
                    status=str(exc.status) if exc.status is not None else "none",
                )
                raise
    await _persist_dub_config(brand_id=brand_id, organization_id=organization_id, binding=None)
    incr_metric("brand_domains.dub.deregistered")
    return True


async def _persist_dub_config(
    *,
    brand_id: UUID,
    organization_id: UUID,
    binding: DubDomainBinding | None,
) -> None:
    if binding is None:
        payload = None
    else:
        payload = {
            "domain": binding.domain,
            "dub_domain_id": binding.dub_domain_id,
            "verified_at": binding.verified_at.astimezone(UTC).isoformat(),
        }
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.brands
                SET dub_domain_config = %s::jsonb, updated_at = NOW()
                WHERE id = %s AND organization_id = %s AND deleted_at IS NULL
                """,
                (
                    None if payload is None else _jsonb(payload),
                    str(brand_id),
                    str(organization_id),
                ),
            )
        await conn.commit()


# ---------------------------------------------------------------------------
# Landing-page-host binding
# ---------------------------------------------------------------------------


async def register_landing_page_domain_for_brand(
    *,
    brand_id: UUID,
    organization_id: UUID,
    entri_connection_id: UUID,
) -> LandingPageDomainBinding:
    """Bind a brand to an existing entri_domain_connections row.

    Validates that the entri row exists and is in the same org. Stamps
    the entri row's `brand_id` for reverse lookup. Idempotent: re-binding
    the same connection returns the existing binding.
    """
    connection = await entri_repo.get_by_id(entri_connection_id)
    if connection is None or connection.organization_id != organization_id:
        raise EntriConnectionNotFoundError(
            f"entri_connection {entri_connection_id} not found in org {organization_id}"
        )

    configs = await get_brand_domain_configs(
        brand_id=brand_id, organization_id=organization_id
    )
    if (
        configs.landing_page is not None
        and configs.landing_page.entri_connection_id == entri_connection_id
    ):
        incr_metric("brand_domains.landing.idempotent_hit")
        return configs.landing_page

    binding = LandingPageDomainBinding(
        domain=connection.domain,
        entri_connection_id=entri_connection_id,
        verified_at=datetime.now(UTC),
    )
    await _persist_landing_config(
        brand_id=brand_id, organization_id=organization_id, binding=binding
    )
    await _stamp_entri_brand(connection_id=entri_connection_id, brand_id=brand_id)
    incr_metric("brand_domains.landing.registered")
    return binding


async def deregister_landing_page_domain_for_brand(
    *,
    brand_id: UUID,
    organization_id: UUID,
) -> bool:
    configs = await get_brand_domain_configs(
        brand_id=brand_id, organization_id=organization_id
    )
    if configs.landing_page is None:
        return False
    await _persist_landing_config(
        brand_id=brand_id, organization_id=organization_id, binding=None
    )
    await _stamp_entri_brand(
        connection_id=configs.landing_page.entri_connection_id, brand_id=None
    )
    incr_metric("brand_domains.landing.deregistered")
    return True


async def _persist_landing_config(
    *,
    brand_id: UUID,
    organization_id: UUID,
    binding: LandingPageDomainBinding | None,
) -> None:
    if binding is None:
        payload = None
    else:
        payload = {
            "domain": binding.domain,
            "entri_connection_id": str(binding.entri_connection_id),
            "verified_at": binding.verified_at.astimezone(UTC).isoformat(),
        }
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.brands
                SET landing_page_domain_config = %s::jsonb, updated_at = NOW()
                WHERE id = %s AND organization_id = %s AND deleted_at IS NULL
                """,
                (
                    None if payload is None else _jsonb(payload),
                    str(brand_id),
                    str(organization_id),
                ),
            )
        await conn.commit()


async def _stamp_entri_brand(*, connection_id: UUID, brand_id: UUID | None) -> None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE business.entri_domain_connections
            SET brand_id = %s
            WHERE id = %s
            """,
            (str(brand_id) if brand_id is not None else None, str(connection_id)),
        )
        await conn.commit()


def _jsonb(payload: dict[str, Any]) -> str:
    """psycopg sends JSONB best as a JSON-encoded string with explicit cast."""
    import json as _json

    return _json.dumps(payload)


# ---------------------------------------------------------------------------
# Read helpers used by step minting + landing page render (Slice 3+)
# ---------------------------------------------------------------------------


async def get_brand_dub_domain(*, brand_id: UUID) -> str | None:
    """Returns the brand's configured Dub link-host domain, or None.

    No org check — minting is server-internal and trusts caller-resolved
    brand_id. The brand row carries organization_id; this helper only
    reads the dub_domain_config column.
    """
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT dub_domain_config FROM business.brands
            WHERE id = %s AND deleted_at IS NULL
            """,
            (str(brand_id),),
        )
        row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    binding = _to_dub_binding(row[0])
    return binding.domain if binding is not None else None


async def get_brand_landing_page_domain(*, brand_id: UUID) -> str | None:
    """Returns the brand's configured landing-page host domain, or None."""
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT landing_page_domain_config FROM business.brands
            WHERE id = %s AND deleted_at IS NULL
            """,
            (str(brand_id),),
        )
        row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    binding = _to_landing_binding(row[0])
    return binding.domain if binding is not None else None


__all__ = [
    "BrandDomainConfigs",
    "BrandNotFoundError",
    "DubDomainBinding",
    "DubNotConfiguredError",
    "EntriConnectionNotFoundError",
    "LandingPageDomainBinding",
    "deregister_dub_domain_for_brand",
    "deregister_landing_page_domain_for_brand",
    "get_brand_domain_configs",
    "get_brand_dub_domain",
    "get_brand_landing_page_domain",
    "register_dub_domain_for_brand",
    "register_landing_page_domain_for_brand",
]
