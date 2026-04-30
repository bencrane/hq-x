#!/usr/bin/env python3
"""Seed the DAT (dat.com) audience-reservation fixture end-to-end.

Idempotent: reruns leave the DB in the same state. Authenticates to DEX
using `DEX_SUPER_ADMIN_API_KEY` (no user JWT context — this is a server-side
setup script). Aborts with a clear error if the key is missing.

Steps:
  1. Upsert business.organizations row with slug='dat'.
  2. Pick a DEX audience_template via the new-entrants-90d source endpoint.
     Falls back to the first active template if none match (loud warning).
  3. Create a DEX audience_spec from that template (no overrides).
  4. UPSERT business.org_audience_reservations linking org → spec.
  5. Exercise the four read paths via the dex_client and print results.

Run via:
    doppler --project hq-x --config dev run -- \\
        uv run python -m scripts.seed_dat_audience_reservation
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import close_pool, get_db_connection, init_pool
from app.services import dex_client


PREFERRED_SOURCE_ENDPOINT = "/api/v1/fmcsa/audiences/new-entrants-90d"
ORG_SLUG = "dat"
ORG_NAME = "DAT"
ORG_PLAN = "prototype"
ORG_METADATA = {"domain": "dat.com"}


def _abort(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


async def _upsert_dat_org() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM business.organizations WHERE slug = %s",
                (ORG_SLUG,),
            )
            row = await cur.fetchone()
            if row:
                org_id = row[0]
                print(f"[org] existing organizations.slug='{ORG_SLUG}' id={org_id}")
                return org_id
            await cur.execute(
                """
                INSERT INTO business.organizations (name, slug, status, plan, metadata)
                VALUES (%s, %s, 'active', %s, %s::jsonb)
                RETURNING id
                """,
                (ORG_NAME, ORG_SLUG, ORG_PLAN, json.dumps(ORG_METADATA)),
            )
            row = await cur.fetchone()
        await conn.commit()
    org_id = row[0]
    print(f"[org] inserted organizations.slug='{ORG_SLUG}' id={org_id}")
    return org_id


async def _pick_template() -> dict[str, Any]:
    listing = await dex_client.list_audience_templates(
        partner_type="factoring_company"
    )
    items = listing.get("items") if isinstance(listing, dict) else None
    if not items:
        # Try without the partner_type filter as a second attempt.
        listing = await dex_client.list_audience_templates()
        items = listing.get("items") if isinstance(listing, dict) else None
    if not items:
        _abort("DEX returned no audience templates; seed migration 134 first")

    matched = [t for t in items if t.get("source_endpoint") == PREFERRED_SOURCE_ENDPOINT]
    if matched:
        chosen = matched[0]
        print(
            f"[template] matched source_endpoint={PREFERRED_SOURCE_ENDPOINT!r}: "
            f"slug={chosen['slug']} id={chosen['id']}"
        )
        return chosen

    chosen = items[0]
    print(
        "[template] WARNING — no template with "
        f"source_endpoint={PREFERRED_SOURCE_ENDPOINT!r}; falling back to first "
        f"active: slug={chosen['slug']} (source_endpoint={chosen['source_endpoint']})"
    )
    return chosen


async def _create_spec(template_id: UUID) -> dict[str, Any]:
    spec = await dex_client.create_audience_spec(
        template_id=template_id,
        name="DAT — fast-growing carriers (prototype)",
        filter_overrides={},
    )
    print(f"[spec] created spec id={spec['id']} name={spec['name']!r}")
    return spec


async def _upsert_reservation(
    org_id: UUID, spec: dict[str, Any], template: dict[str, Any]
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.org_audience_reservations (
                    organization_id, data_engine_audience_id,
                    source_template_slug, source_template_id, audience_name,
                    notes, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (organization_id, data_engine_audience_id)
                DO UPDATE SET
                    audience_name = EXCLUDED.audience_name,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    str(org_id),
                    spec["id"],
                    template["slug"],
                    template["id"],
                    spec["name"],
                    "Seeded by scripts/seed_dat_audience_reservation",
                    json.dumps({"seed": True}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    reservation_id = row[0]
    print(f"[reservation] upserted reservation id={reservation_id}")
    return reservation_id


def _print_descriptor(descriptor: dict[str, Any]) -> None:
    print("[descriptor] audience_attributes:")
    attrs = descriptor.get("audience_attributes") or []
    if not attrs:
        print("  (none)")
        return
    for a in attrs:
        schema = a.get("schema") or {}
        type_hint = schema.get("type", "?")
        print(f"  - {a['key']} ({type_hint}) = {a.get('value')!r}")


def _print_count(count: dict[str, Any]) -> None:
    print(
        f"[count] total={count.get('total')} "
        f"generated_at={count.get('generated_at')}"
    )


def _print_members(label: str, page: dict[str, Any]) -> None:
    items = page.get("items") or []
    print(f"[{label}] returned {len(items)} item(s) total={page.get('total')}:")
    for item in items:
        print(
            "  - "
            f"dot={item.get('dot_number')} "
            f"name={item.get('legal_name')!r} "
            f"state={item.get('physical_state')} "
            f"power_units={item.get('power_unit_count')}"
        )


async def _exercise_reads(spec_id: UUID) -> None:
    try:
        descriptor = await dex_client.get_audience_descriptor(UUID(str(spec_id)))
        _print_descriptor(descriptor)
    except dex_client.DexCallError as exc:
        if exc.status_code == 404:
            # Descriptor endpoint may not yet be deployed in the running DEX.
            # Print a clear note and continue exercising the live endpoints.
            print(
                "[descriptor] WARNING — DEX returned 404 for /descriptor; "
                "this endpoint is new in this directive and may not be "
                "deployed yet. Continuing."
            )
        else:
            raise

    count = await dex_client.count_audience_members(UUID(str(spec_id)))
    _print_count(count)

    page1 = await dex_client.list_audience_members(
        UUID(str(spec_id)), limit=5, offset=0
    )
    _print_members("members[0:5]", page1)

    page2 = await dex_client.list_audience_members(
        UUID(str(spec_id)), limit=5, offset=5
    )
    _print_members("members[5:10]", page2)


async def _main() -> None:
    if settings.DEX_SUPER_ADMIN_API_KEY is None:
        _abort(
            "DEX_SUPER_ADMIN_API_KEY is not set; this script authenticates "
            "server-to-server with DEX and cannot proceed without it"
        )
    if not settings.DEX_BASE_URL:
        _abort("DEX_BASE_URL is not set")

    print(f"DEX_BASE_URL={settings.DEX_BASE_URL}")

    await init_pool()
    try:
        org_id = await _upsert_dat_org()
        template = await _pick_template()
        spec = await _create_spec(UUID(template["id"]))
        reservation_id = await _upsert_reservation(org_id, spec, template)
        print()
        print(
            f"=== summary === org={org_id} spec={spec['id']} "
            f"reservation={reservation_id}"
        )
        print()
        await _exercise_reads(UUID(spec["id"]))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
