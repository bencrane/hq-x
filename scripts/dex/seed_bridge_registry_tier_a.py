#!/usr/bin/env python3
"""Seed Tier-A bridge registry rows (directive #3).

Idempotent UPSERTs into ops.match_methods + ops.match_method_versions +
ops.bridges to register the Tier-A identity bridges:

  1. uei_exact (NEW method, NEW version v1.0.0) — trivial-bridge for
     SAM ↔ USAspending UEI matches. No normalization, no Parquet artifact.

  2. domain_exact (REUSED method from #2.5) — reused for SAM ↔ PDL
     domain. No new method or version row inserted; this script just
     looks up the existing IDs.

  3. ops.bridges rows (5 total):
        - sam_usaspending_uei_contracts             (uei_exact)
        - sam_usaspending_uei_contracts_historical  (uei_exact)
        - sam_usaspending_uei_contract_subawards    (uei_exact)
        - sam_usaspending_uei_assistance_subawards  (uei_exact)
        - sam_pdl_domain                            (domain_exact)

NOTE: source_usaspending_assistance does NOT exist (USAspending API
hadn't recovered when their backfill PR ran). The 5th SAM-USAspending
bridge for assistance gets added in a follow-up directive.

Trivial bridges (UEI) DO NOT produce Parquet artifacts — the MV joins
source-to-source directly with bridge_run_id from a registry subquery
(see directive L20). These bridges still get an ops.bridges row + a
single bridge_generation_run row per MV apply, so the provenance trace
through ops.bridge_generation_runs works.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/seed_bridge_registry_tier_a.py --apply

    doppler run --project hq-all --config prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/seed_bridge_registry_tier_a.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.match_method_registry import (  # noqa: E402
    register_bridge,
    register_match_method,
    register_match_method_version,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("seed_bridge_registry_tier_a")


# Bridge identities ----------------------------------------------------------
SAM_USASPENDING_BRIDGES = [
    {
        "bridge_name": "sam_usaspending_uei_contracts",
        "source_left": "source_sam_entities",
        "source_right": "source_usaspending_contracts",
        "method_name": "uei_exact",
        "r2_output_prefix": None,  # trivial-bridge: no Parquet artifact
        "description": (
            "SAM unique_entity_id = USAspending recipient_uei "
            "(daily-drip contracts stream). Trivial-bridge; no Parquet."
        ),
    },
    {
        "bridge_name": "sam_usaspending_uei_contracts_historical",
        "source_left": "source_sam_entities",
        "source_right": "source_usaspending_contracts_historical",
        "method_name": "uei_exact",
        "r2_output_prefix": None,
        "description": (
            "SAM unique_entity_id = USAspending recipient_uei "
            "(historical contracts back-catalog). Trivial-bridge; no Parquet."
        ),
    },
    {
        "bridge_name": "sam_usaspending_uei_contract_subawards",
        "source_left": "source_sam_entities",
        "source_right": "source_usaspending_contract_subawards",
        "method_name": "uei_exact",
        "r2_output_prefix": None,
        "description": (
            "SAM unique_entity_id = USAspending subawardee_uei "
            "(prime contracts → subawardees). Trivial-bridge; no Parquet."
        ),
    },
    {
        "bridge_name": "sam_usaspending_uei_assistance_subawards",
        "source_left": "source_sam_entities",
        "source_right": "source_usaspending_assistance_subawards",
        "method_name": "uei_exact",
        "r2_output_prefix": None,
        "description": (
            "SAM unique_entity_id = USAspending subawardee_uei "
            "(prime assistance → subawardees). Trivial-bridge; no Parquet."
        ),
    },
]

SAM_PDL_BRIDGE = {
    "bridge_name": "sam_pdl_domain",
    "source_left": "source_sam_entities",
    "source_right": "source_pdl_companies",
    "method_name": "domain_exact",
    "r2_output_prefix": "bridges/sam_pdl_domain/",
    "description": (
        "SAM entity_url normalized = PDL website normalized. "
        "Reuses domain_exact method (FMCSA-PDL); SAM entity_url is "
        "always business-domain so no free-mail blacklist applies."
    ),
}


def _seed_uei_exact_method(*, dry_run: bool) -> None:
    """Register uei_exact match method + v1.0.0 version (NEW)."""
    if dry_run:
        logger.info("[dry-run] would register match_method 'uei_exact'")
        logger.info("[dry-run] would register match_method_version 'uei_exact' v1.0.0")
        return

    method_id = register_match_method(
        method_name="uei_exact",
        description=(
            "Exact-equality match on the 12-character SAM Unique Entity "
            "Identifier (UEI). UEI is a canonical regulatory ID; no "
            "normalization is required (case-sensitive equality on the "
            "raw 12-char string)."
        ),
    )
    logger.info(f"  registered uei_exact method_id={method_id}")

    version_id = register_match_method_version(
        method_name="uei_exact",
        semver="1.0.0",
        normalizer_module=None,
        normalizer_version=None,
        blacklist_module=None,
        blacklist_version=None,
        tier_rule_description=(
            "platinum=all matches (UEI is canonical regulatory ID; "
            "any equality is platinum-grade)"
        ),
        rejection_rule_description="none (no fan-out collisions on canonical UEI)",
        input_columns_left=["unique_entity_id"],
        input_columns_right=["recipient_uei", "subawardee_uei"],
        output_value_description="literal 12-char SAM UEI string",
    )
    logger.info(f"  registered uei_exact v1.0.0 version_id={version_id}")


def _seed_sam_usaspending_bridges(*, dry_run: bool) -> None:
    """Register 4 SAM-USAspending bridges (1 per USAspending stream)."""
    for spec in SAM_USASPENDING_BRIDGES:
        if dry_run:
            logger.info(f"[dry-run] would register bridge {spec['bridge_name']!r}")
            continue
        bridge_id = register_bridge(
            bridge_name=spec["bridge_name"],
            source_left=spec["source_left"],
            source_right=spec["source_right"],
            method_name=spec["method_name"],
            r2_output_prefix=spec["r2_output_prefix"] or "trivial",
            description=spec["description"],
        )
        logger.info(f"  registered bridge {spec['bridge_name']!r} bridge_id={bridge_id}")


def _seed_sam_pdl_bridge(*, dry_run: bool) -> None:
    """Register sam_pdl_domain bridge (REUSES domain_exact method)."""
    spec = SAM_PDL_BRIDGE
    if dry_run:
        logger.info(f"[dry-run] would register bridge {spec['bridge_name']!r}")
        return
    bridge_id = register_bridge(
        bridge_name=spec["bridge_name"],
        source_left=spec["source_left"],
        source_right=spec["source_right"],
        method_name=spec["method_name"],
        r2_output_prefix=spec["r2_output_prefix"],
        description=spec["description"],
    )
    logger.info(f"  registered bridge {spec['bridge_name']!r} bridge_id={bridge_id}")


def _verify_domain_exact_exists() -> None:
    """Sanity check: domain_exact must already exist (from #2.5 retrofit)."""
    import os

    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT match_method_id FROM ops.match_methods WHERE method_name = %s",
                ("domain_exact",),
            )
            row = cur.fetchone()
            if row is None:
                raise SystemExit(
                    "FAIL: domain_exact method not registered. Run "
                    "scripts/backfill_fmcsa_pdl_domain_bridge_v2.py --apply first."
                )
            logger.info(f"  reused domain_exact method_id={row[0]}")
            cur.execute(
                """
                SELECT v.semver
                  FROM ops.match_method_versions v
                  JOIN ops.match_methods m USING (match_method_id)
                 WHERE m.method_name = 'domain_exact'
                """
            )
            versions = [r[0] for r in cur.fetchall()]
            if "1.0.0" not in versions:
                raise SystemExit(
                    f"FAIL: domain_exact v1.0.0 not registered (have: {versions})"
                )
            logger.info(f"  reused domain_exact v1.0.0 (existing: {versions})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="run UPSERTs")
    parser.add_argument("--dry-run", action="store_true", help="log intent, no writes")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    import os
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set")

    logger.info("seed_bridge_registry_tier_a — directive #3")
    logger.info("─" * 60)

    if args.apply:
        _verify_domain_exact_exists()

    logger.info("(1) uei_exact method + v1.0.0 (NEW)")
    _seed_uei_exact_method(dry_run=args.dry_run)

    logger.info("(2) SAM-USAspending bridges (4 trivial-bridges via uei_exact)")
    _seed_sam_usaspending_bridges(dry_run=args.dry_run)

    logger.info("(3) sam_pdl_domain bridge (REUSE domain_exact)")
    _seed_sam_pdl_bridge(dry_run=args.dry_run)

    if args.dry_run:
        logger.info("DRY RUN — no writes.")
    else:
        logger.info("OK — registry seeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
