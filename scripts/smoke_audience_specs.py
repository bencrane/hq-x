#!/usr/bin/env python3
"""End-to-end smoke test for the audience-spec contract substrate (Phase 2).

Runs the full lifecycle against PROD data:
  1. Upsert a fixture partner org (slug='audience-spec-smoke').
  2. Create a draft spec ("FMCSA carriers in TX with safety_rating='S'").
  3. Preview it — expect a real row count from the live FMCSA Iceberg
     table (fmcsa.company_census_file).
  4. Sign it — write the cohort manifest parquet to R2; verify the
     signing row + manifest file exist.
  5. Fetch replenishment status — expect a non-zero live count + days
     remaining + freshness within SLA.

The script is idempotent in the sense that it always creates a NEW spec
row; it doesn't try to find or reuse a prior one. The fixture org is
created once and reused.

Run:
    doppler --project hq-all --config prd run -- \\
        uv run python -m scripts.smoke_audience_specs
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from uuid import UUID

import boto3

from app.db import close_pool, get_db_connection, init_pool
from app.services.audience_spec import evaluator as evalmod
from app.services.audience_spec.models import (
    AudienceSpec,
    CatalogRef,
    FreshnessRequirement,
    ScalarPredicate,
)

ORG_SLUG = "audience-spec-smoke"
ORG_NAME = "Audience-Spec Smoke Test (Phase 2)"


def _abort(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


async def _upsert_smoke_org() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM business.organizations WHERE slug = %s",
                (ORG_SLUG,),
            )
            row = await cur.fetchone()
            if row:
                org_id = row[0]
                print(f"[org] reusing organizations.slug='{ORG_SLUG}' id={org_id}")
                return org_id
            await cur.execute(
                """
                INSERT INTO business.organizations (name, slug, status, plan, metadata)
                VALUES (%s, %s, 'active', 'prototype', %s::jsonb)
                RETURNING id
                """,
                (ORG_NAME, ORG_SLUG, json.dumps({"smoke_test": "audience_specs_phase_2"})),
            )
            row = await cur.fetchone()
        await conn.commit()
    org_id = row[0]
    print(f"[org] inserted organizations.slug='{ORG_SLUG}' id={org_id}")
    return org_id


async def _insert_spec(partner_id: UUID, content: AudienceSpec) -> UUID:
    """Insert directly via the same SQL the router uses; bypass HTTP."""
    from uuid import uuid4
    spec_id = uuid4()
    required_freshness = [
        {"source": r.source, "max_age_seconds": r.max_age_seconds}
        for r in content.required_freshness
    ] or None

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.audience_specs (
                    spec_id, partner_id, version, parent_spec_id,
                    content, status, required_freshness, notes
                ) VALUES (
                    %s, %s, 1, NULL, %s::jsonb, 'draft', %s::jsonb, %s
                )
                """,
                (
                    str(spec_id),
                    str(partner_id),
                    content.model_dump_json(),
                    json.dumps(required_freshness) if required_freshness else None,
                    "Created by scripts/smoke_audience_specs",
                ),
            )
        await conn.commit()
    print(f"[spec] inserted draft spec_id={spec_id}")
    return spec_id


def _verify_manifest_exists(uri: str) -> int:
    """HEAD the cohort manifest parquet to confirm it exists in R2.

    Returns the byte size. Raises if the file isn't there.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"unexpected uri shape: {uri!r}")
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    head = client.head_object(Bucket=bucket, Key=key)
    return int(head["ContentLength"])


async def _main() -> int:
    await init_pool()
    try:
        # ─── 1. fixture org ───
        partner_id = await _upsert_smoke_org()

        # ─── 2. draft spec ───
        spec = AudienceSpec(
            sources=[CatalogRef(namespace="fmcsa", table="company_census_file")],
            filters=[
                ScalarPredicate(column="PHY_STATE", op="eq", value="TX"),
                ScalarPredicate(column="SAFETY_RATING", op="eq", value="S"),
            ],
            required_freshness=[
                # FMCSA snapshots are daily — give a generous SLA so the
                # smoke test doesn't false-fail on a slow ingest day.
                FreshnessRequirement(
                    source="fmcsa.company_census_file",
                    max_age_seconds=14 * 86400,  # 14 days
                ),
            ],
        )
        spec_id = await _insert_spec(partner_id, spec)

        # ─── 3. preview ───
        preview = await evalmod.preview(spec_id)
        print(
            f"[preview] count={preview.count} "
            f"sample_rows={len(preview.sample)} "
            f"sources_used={preview.sources_used} "
            f"elapsed={preview.elapsed_s}s"
        )
        for fc in preview.freshness_checks:
            print(
                f"[preview] freshness {fc.source}: "
                f"observed_age_seconds={fc.observed_age_seconds} "
                f"max={fc.max_age_seconds} ok={fc.ok}"
            )
        if preview.count == 0:
            _abort("preview returned 0 rows — expected ~12k TX-S carriers")
        if not all(c.ok for c in preview.freshness_checks):
            _abort("preview freshness check failed")

        # ─── 4. sign ───
        signing = await evalmod.sign(
            spec_id,
            partner_signature={
                "smoke_test": True,
                "scenario": "phase_2_substrate_scaffold",
            },
        )
        print(
            f"[sign] signing_id={signing.signing_id} "
            f"count_at_signing={signing.count_at_signing} "
            f"cohort_manifest_uri={signing.cohort_manifest_uri} "
            f"expires_at={signing.expires_at}"
        )
        manifest_size = _verify_manifest_exists(signing.cohort_manifest_uri)
        print(f"[sign] cohort_manifest_uri R2 HEAD ok size={manifest_size}B")

        # ─── 5. replenishment ───
        rep = await evalmod.replenishment_status(signing.signing_id)
        print(
            f"[replenishment] live_count={rep.live_count} "
            f"count_at_signing={rep.count_at_signing} "
            f"delta={rep.delta} days_remaining={rep.days_remaining} "
            f"at_risk={rep.at_risk}"
        )
        for fc in rep.freshness_now:
            print(f"[replenishment] freshness now: {fc}")

        if rep.live_count == 0:
            _abort("replenishment live_count is 0 — evaluator broken?")
        if rep.days_remaining < 89:
            _abort(
                f"replenishment days_remaining={rep.days_remaining}; "
                "expected ~90 (just signed)"
            )

        print()
        print(
            "=== smoke OK === "
            f"spec={spec_id} signing={signing.signing_id} "
            f"cohort_size={signing.count_at_signing} "
            f"manifest={signing.cohort_manifest_uri}"
        )
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
