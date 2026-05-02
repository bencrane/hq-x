#!/usr/bin/env python3
"""End-to-end smoke test for `app.services.print_mail_activation`.

Mints one Lob piece of each of the five Print & Mail types (postcard,
self_mailer, letter, snap_pack, booklet) against Lob test mode in a
single batch. Each piece carries bespoke per-piece HTML so the
per-recipient creative contract is observable in the request payload.

Run via:

    doppler --project hq-x --config dev run -- \\
        uv run python -m scripts.seed_print_mail_batch_demo

Exit 0 on full green (5 created, 0 failed, 0 skipped). Non-zero
otherwise, with a per-type breakdown of failures.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from uuid import UUID, uuid4

from app.config import settings
from app.db import close_pool, get_db_connection, init_pool
from app.services.print_mail_activation import (
    BookletSpec,
    LetterSpec,
    PieceSpec,
    PostcardSpec,
    SelfMailerSpec,
    SnapPackSpec,
    activate_pieces_batch,
)

ORG_SLUG = "print-mail-demo"
ORG_NAME = "Print & Mail Per-Piece Demo"
ORG_PLAN = "prototype"

# Lob test address from their docs. Lob test mode does not actually mail
# anything; the address-validity bar is loose.
_TEST_TO = {
    "name": "Harry Zhang",
    "address_line1": "210 King St",
    "address_city": "San Francisco",
    "address_state": "CA",
    "address_zip": "94107",
    "address_country": "US",
}
_TEST_FROM = {
    "name": "Sender Demo",
    "address_line1": "185 Berry St Ste 6100",
    "address_city": "San Francisco",
    "address_state": "CA",
    "address_zip": "94107",
    "address_country": "US",
}


def _abort(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


async def _upsert_demo_org() -> UUID:
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
                VALUES (%s, %s, 'active', %s, '{}'::jsonb)
                RETURNING id
                """,
                (ORG_NAME, ORG_SLUG, ORG_PLAN),
            )
            row = await cur.fetchone()
        await conn.commit()
    org_id = row[0]
    print(f"[org] inserted organizations.slug='{ORG_SLUG}' id={org_id}")
    return org_id


def _build_specs() -> list[tuple[PieceSpec, dict[str, UUID]]]:
    """Return five specs (one per type), each with bespoke HTML and
    distinct fake back-reference UUIDs in metadata so the round-trip is
    observable on the persisted row.

    Each spec gets a distinct seed → distinct Idempotency-Key, so re-runs
    that change the seed produce new pieces; re-runs with the same seed
    are no-ops (Lob returns the previously-minted piece)."""

    base_back_refs = lambda: {  # noqa: E731
        "_recipient_id": uuid4(),
        "_channel_campaign_step_id": uuid4(),
        "_membership_id": uuid4(),
    }

    refs = [base_back_refs() for _ in range(5)]

    postcard = PostcardSpec(
        piece_type="postcard",
        to=_TEST_TO,
        **{"from": _TEST_FROM},
        front="<html><body><h1>Postcard #1 for Harry</h1><p>Bespoke per-recipient front.</p></body></html>",
        back="<html><body><h2>Postcard back</h2><p>Spec-attribute personalized.</p></body></html>",
        size="6x9",
        recipient_id=refs[0]["_recipient_id"],
        channel_campaign_step_id=refs[0]["_channel_campaign_step_id"],
        membership_id=refs[0]["_membership_id"],
        idempotency_seed=f"print-mail-demo-postcard-{int(time.time())}",
    )
    self_mailer = SelfMailerSpec(
        piece_type="self_mailer",
        to=_TEST_TO,
        **{"from": _TEST_FROM},
        inside="<html><body><h1>Self-mailer inside #2</h1><p>Bespoke inside content.</p></body></html>",
        outside="<html><body><h1>Self-mailer outside #2</h1><p>Bespoke outside content.</p></body></html>",
        size="6x18_bifold",
        recipient_id=refs[1]["_recipient_id"],
        channel_campaign_step_id=refs[1]["_channel_campaign_step_id"],
        membership_id=refs[1]["_membership_id"],
        idempotency_seed=f"print-mail-demo-sfm-{int(time.time())}",
    )
    letter = LetterSpec(
        piece_type="letter",
        to=_TEST_TO,
        **{"from": _TEST_FROM},
        file=(
            "<html><body><h1>Letter #3 for Harry</h1>"
            "<p>Per-recipient bespoke letter copy. "
            "Spec attributes appear inline.</p></body></html>"
        ),
        color=True,
        double_sided=True,
        address_placement="top_first_page",
        recipient_id=refs[2]["_recipient_id"],
        channel_campaign_step_id=refs[2]["_channel_campaign_step_id"],
        membership_id=refs[2]["_membership_id"],
        idempotency_seed=f"print-mail-demo-letter-{int(time.time())}",
    )
    snap_pack = SnapPackSpec(
        piece_type="snap_pack",
        to=_TEST_TO,
        **{"from": _TEST_FROM},
        inside=(
            "<html><body><h1>Snap-pack inside #4</h1>"
            "<p>8.5x11 inside content for Harry.</p></body></html>"
        ),
        outside=(
            "<html><body><h1>Snap-pack outside #4</h1>"
            "<p>6x18 outside content.</p></body></html>"
        ),
        color=False,
        recipient_id=refs[3]["_recipient_id"],
        channel_campaign_step_id=refs[3]["_channel_campaign_step_id"],
        membership_id=refs[3]["_membership_id"],
        idempotency_seed=f"print-mail-demo-snap-{int(time.time())}",
    )
    booklet = BookletSpec(
        piece_type="booklet",
        to=_TEST_TO,
        **{"from": _TEST_FROM},
        file=(
            "<html><body><h1>Booklet #5 for Harry</h1>"
            "<p>Multi-page bespoke booklet (default 8.375x5.375 size).</p>"
            "</body></html>"
        ),
        size="8.375x5.375",
        recipient_id=refs[4]["_recipient_id"],
        channel_campaign_step_id=refs[4]["_channel_campaign_step_id"],
        membership_id=refs[4]["_membership_id"],
        idempotency_seed=f"print-mail-demo-booklet-{int(time.time())}",
    )

    return [
        (postcard, refs[0]),
        (self_mailer, refs[1]),
        (letter, refs[2]),
        (snap_pack, refs[3]),
        (booklet, refs[4]),
    ]


async def _read_persisted_rows(
    external_ids: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT piece_type, provider_slug, external_piece_id, status,
                       is_test_mode, metadata, cost_cents, created_at
                FROM direct_mail_pieces
                WHERE external_piece_id = ANY(%s)
                ORDER BY created_at DESC
                """,
                (external_ids,),
            )
            cols = [c.name for c in cur.description] if cur.description else []
            for row in await cur.fetchall():
                rows.append(dict(zip(cols, row)))
    return rows


async def main() -> int:
    if not settings.LOB_API_KEY_TEST and not settings.LOB_API_KEY:
        _abort(
            "Neither LOB_API_KEY_TEST nor LOB_API_KEY is set. The seed script "
            "talks to Lob in test mode; supply LOB_API_KEY_TEST via Doppler."
        )
    if not settings.LOB_API_KEY_TEST:
        _abort(
            "LOB_API_KEY_TEST is not set; this script intentionally runs in "
            "Lob test mode to avoid minting real mail."
        )

    await init_pool()
    started = time.monotonic()
    try:
        org_id = await _upsert_demo_org()

        spec_pairs = _build_specs()
        specs = [pair[0] for pair in spec_pairs]
        ref_by_index: dict[int, dict[str, UUID]] = {
            i: pair[1] for i, pair in enumerate(spec_pairs)
        }

        print(f"[batch] activating {len(specs)} pieces (one of each type)…")
        result = await activate_pieces_batch(
            organization_id=org_id, pieces=specs, test_mode=True
        )

        print()
        print(
            f"=== ActivationBatchResult corr={result.correlation_id} "
            f"total={result.total} created={result.created} "
            f"skipped={result.skipped} failed={result.failed} ==="
        )

        for pr in result.results:
            print(
                f"  [{pr.spec_index}] {pr.piece_type:<12} status={pr.status:<22} "
                f"ext={pr.external_piece_id or '-':<24} err={pr.error_code or '-'}"
            )
            if pr.error_detail:
                print(f"      detail={pr.error_detail}")

        ok_creates = result.created == 5 and result.failed == 0 and result.skipped == 0

        if ok_creates:
            external_ids = [pr.external_piece_id for pr in result.results if pr.external_piece_id]
            persisted_rows = await _read_persisted_rows(external_ids)
            print()
            print("=== direct_mail_pieces rows ===")
            row_by_id = {r["external_piece_id"]: r for r in persisted_rows}
            metadata_ok = True
            for idx, pr in enumerate(result.results):
                ext_id = pr.external_piece_id
                if ext_id is None or ext_id not in row_by_id:
                    print(f"  [{idx}] {pr.piece_type:<12} MISSING ROW (ext={ext_id})")
                    metadata_ok = False
                    continue
                row = row_by_id[ext_id]
                meta = row.get("metadata") or {}
                expected_rid = str(ref_by_index[idx]["_recipient_id"])
                actual_rid = meta.get("_recipient_id")
                rid_match = "ok" if actual_rid == expected_rid else "MISMATCH"
                if rid_match != "ok":
                    metadata_ok = False
                print(
                    f"  [{idx}] type={row['piece_type']:<12} "
                    f"provider={row['provider_slug']:<6} "
                    f"ext={row['external_piece_id']:<24} "
                    f"status={row['status']:<14} "
                    f"_rid={rid_match}"
                )
                if rid_match != "ok":
                    print(f"      expected _recipient_id={expected_rid}")
                    print(f"      actual   _recipient_id={actual_rid}")
                print(f"      metadata={meta}")
            ok_creates = ok_creates and metadata_ok

        elapsed = time.monotonic() - started
        print()
        print(f"[exit] all_green={ok_creates} elapsed_seconds={elapsed:.2f}")
        return 0 if ok_creates else 1
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
