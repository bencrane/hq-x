"""One-shot seed: create a draft proposal in hq-x and print the URL.

Usage:
    cd apps/hq-x
    doppler run --project hq-x --config prd -- uv run python -m scripts.seed_test_proposal

Prints the public_token and the partner-platform URL the prospect would
visit. Uses the AcqEng org + 'Capital Expansion' brand + the engineered-
demand operator user (resolved by email at runtime so this stays robust
to id rotation). The audience id is a fresh random UUID — cross-DB and
not enforced as a FK, so this is fine for a render demo. The proposal
is created in 'draft' status; nothing is sent, no Stripe call is made.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID, uuid4

from app.db import close_pool, get_db_connection, init_pool
from app.services import proposals as proposals_svc

OPERATOR_EMAIL = "benjamin.crane@engineereddemand.com"
ORG_NAME = "Acquisition Engineering"
BRAND_NAME = "Capital Expansion"


async def _resolve_ids() -> tuple[UUID, UUID, UUID]:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM business.organizations WHERE name = %s LIMIT 1",
            (ORG_NAME,),
        )
        row = await cur.fetchone()
        if not row:
            raise SystemExit(f"org not found: {ORG_NAME}")
        org_id: UUID = row[0]

        await cur.execute(
            "SELECT id FROM business.brands WHERE organization_id = %s AND name = %s LIMIT 1",
            (str(org_id), BRAND_NAME),
        )
        row = await cur.fetchone()
        if not row:
            raise SystemExit(f"brand not found: {BRAND_NAME} under {ORG_NAME}")
        brand_id: UUID = row[0]

        await cur.execute(
            "SELECT id FROM business.users WHERE email = %s LIMIT 1",
            (OPERATOR_EMAIL,),
        )
        row = await cur.fetchone()
        if not row:
            raise SystemExit(f"operator user not found: {OPERATOR_EMAIL}")
        user_id: UUID = row[0]

    return org_id, brand_id, user_id


async def main() -> int:
    await init_pool()
    try:
        org_id, brand_id, user_id = await _resolve_ids()

        proposal = await proposals_svc.create_proposal(
            organization_id=org_id,
            brand_id=brand_id,
            prospect_company_name="Acme Trucking Co",
            prospect_contact_name="Sam Reed",
            prospect_contact_email="sam@acme-trucking.example",
            proposed_data_engine_audience_id=uuid4(),
            proposed_transfer_count=50,
            proposed_price_per_transfer_cents=100_000,  # $1,000 / transfer
            proposed_window_days=30,
            created_by_user_id=user_id,
            metadata={"seed": "render-demo", "created_for": "manual review"},
        )
    finally:
        await close_pool()

    token = proposal["public_token"]
    print()
    print("=" * 64)
    print("PROPOSAL CREATED")
    print("=" * 64)
    print(f"  id:           {proposal['id']}")
    print(f"  status:       {proposal['status']}")
    print(f"  total:        ${proposal['proposed_total_cents'] / 100:,.0f}")
    print(f"  public_token: {token}")
    print()
    print("OPEN ONE OF:")
    print(f"  http://localhost:3000/proposal/{token}   (run partner-platform locally)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
