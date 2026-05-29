"""One-shot backfill: DEX ``ops.gtm_signals`` -> hq-x ``business.gtm_signals``.

Reads the legacy signals from DEX over the existing ``dex_client`` HTTP API
(no cross-DB SQL — forbidden), translates each legacy 4-key FPDS criteria into
the generalized spec, and UPSERTs into ``business.gtm_signals``. Idempotent /
re-runnable.

Run (prod)::

    doppler run --project hq-all --config prd -- \
        uv run python -m scripts.backfill_gtm_signals_from_dex

(use ``--config dev`` to target dev.)

Standalone-script note (hq-x CLAUDE.md): wrap the async entry with
``app.db.init_pool()`` / ``close_pool()`` since the FastAPI lifespan doesn't run.
"""
from __future__ import annotations

import asyncio
import logging

from app.db import close_pool, init_pool
from app.services import dex_client, gtm_signals

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_gtm_signals")

# The FPDS spine every legacy USAspending signal targeted, in the canonical
# gtm-mcp dotted form.
_FPDS_SPINE = "usaspending.transaction_fpds_lance"
# SAM enrichment join the legacy cohort performed (UEI -> cage_code + name).
_SAM_JOIN = {
    "dataset": "spines.sam_entities_lance",
    "on": ["recipient_uei", "uei"],
    "select": ["cage_code", "legal_business_name"],
}
# The legacy FPDS projection (preserves cohort row shape).
_FPDS_SELECT = [
    "recipient_uei",
    "generated_unique_award_id",
    "piid",
    "fain",
    "type_description",
    "action_type",
    "modification_number",
    "action_date",
    "federal_action_obligation",
    "awarding_toptier_agency_name",
    "awarding_subtier_agency_name",
]


def _translate_legacy_criteria(legacy: dict) -> dict:
    """Legacy 4-key FPDS criteria -> generalized spec (parity-preserving).

    Legacy keys: time_window_hours, min_obligated_usd, award_types, action_types
    (action_types may contain JSON null to match brand-new awards).
    """
    predicates: list[dict] = []

    min_usd = legacy.get("min_obligated_usd")
    if min_usd is not None:
        predicates.append(
            {"column": "federal_action_obligation", "op": "gte", "value": min_usd}
        )

    award_types = legacy.get("award_types") or []
    if award_types:
        predicates.append(
            {"column": "type_description", "op": "in", "value": award_types}
        )

    action_types = legacy.get("action_types")
    if action_types is not None:
        # `null` element is preserved; the compiler emits the IS NULL OR-branch.
        predicates.append(
            {"column": "action_type", "op": "in", "value": action_types}
        )

    spec: dict = {
        "spine_target": _FPDS_SPINE,
        "predicates": predicates,
        "join": _SAM_JOIN,
        "select": _FPDS_SELECT,
        "order_by": {"column": "federal_action_obligation", "dir": "desc"},
    }
    hours = legacy.get("time_window_hours")
    if hours is not None:
        spec["time_window"] = {"column": "action_date", "hours": int(hours)}
    return spec


async def _run() -> int:
    listing = await dex_client.list_gtm_signals()
    signals = (
        listing.get("signals", []) if isinstance(listing, dict) else (listing or [])
    )
    count = 0
    for sig in signals:
        slug = sig.get("signal_slug") or sig.get("slug")
        if not slug:
            logger.warning("skipping signal with no slug: %r", sig)
            continue
        spec = {
            "signal_slug": slug,
            "display_name": sig.get("name") or sig.get("display_name") or slug,
            "spine_target": _FPDS_SPINE,
            "criteria": _translate_legacy_criteria(sig.get("criteria") or {}),
            "webhook_test_url": sig.get("webhook_test_url") or "",
            "webhook_prod_url": sig.get("webhook_prod_url")
            or sig.get("webhook_url")
            or "",
            "webhook_target": sig.get("webhook_target") or "test",
            "is_active": bool(sig.get("is_active", True)),
        }
        await gtm_signals.upsert_signal(spec)
        logger.info("backfilled signal slug=%s", slug)
        count += 1
    return count


async def _main() -> None:
    await init_pool()
    try:
        n = await _run()
        logger.info("backfill complete: %d signals upserted", n)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
