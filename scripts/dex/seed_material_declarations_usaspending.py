#!/usr/bin/env python3
"""Seed USAspending material-attribute declarations into ops.material_attribute_declarations.

Cycle: usaspending-pipeline-remediation (2026-05-13).

Parallel to scripts/seed_material_declarations_fmcsa.py — the FMCSA cycle's
canonical seed pattern. Wires 4 USAspending contract attributes so the
existing material_change_detector emits change events on USAspending
snapshots (s7 adds the snapshot resolver; this seeds the declarations).

Selected attributes:
  - recipient_uei              — change_kind=value_disappeared.
                                  When a contract's recipient UEI flips to NULL/blank
                                  the recipient EFT routing breaks for any audience
                                  cohort filtering by UEI.
  - total_obligated_amount     — change_kind=threshold_crossed, op=gt, threshold=0.
                                  First-money-on-a-contract signal (definitive
                                  awarded-not-just-modified marker for new
                                  audience cohorts).
  - period_of_performance_end_date — change_kind=value_changed.
                                  End-date shifts indicate scope-of-work change
                                  (option exercise, extension, early termination)
                                  that audience cohorts should re-evaluate.
  - naics_code                 — change_kind=value_changed.
                                  Reclassification by awarding agency is a
                                  vertical-targeting signal for audience cohorts.

Source resolved at runtime by display_name='usaspending_contracts_lance'
(the Lance-polaris layer s7's resolver reads from). ON CONFLICT
(source_id, attribute_name) DO UPDATE — re-runnable.

Usage:
    cd apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        python scripts/seed_material_declarations_usaspending.py

Companion migration: supabase/migrations/20260513154300_usaspending_material_attribute_declarations.sql
runs the same INSERTs in SQL so the declarations survive a fresh-DB rebuild.
The seed script remains useful for manual one-shot runs (operator demand).
"""
from __future__ import annotations

import logging
import os
import sys
from uuid import UUID

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SOURCE_DISPLAY_NAME = "usaspending_contracts_lance"

DECLARATIONS = [
    {
        "attribute_name": "recipient_uei",
        "change_kind": "value_disappeared",
        "threshold_value": None,
        "threshold_op": None,
        "notes": (
            "Recipient UEI disappearing breaks EFT routing + audience filtering. "
            "Column-level proxy for 'awarded-entity-routing-broken' signal."
        ),
    },
    {
        "attribute_name": "total_obligated_amount",
        "change_kind": "threshold_crossed",
        "threshold_value": 0,
        "threshold_op": "gt",
        "notes": (
            "First-money signal: total_obligated_amount crossing 0 upward marks "
            "the transition from administratively-modified to actually-funded. "
            "Audience cohorts gating on awarded-not-just-modified subscribe to this."
        ),
    },
    {
        "attribute_name": "period_of_performance_end_date",
        "change_kind": "value_changed",
        "threshold_value": None,
        "threshold_op": None,
        "notes": (
            "End-date shifts indicate scope/duration change (option exercise, "
            "extension, early termination). Audience cohorts revaluate eligibility."
        ),
    },
    {
        "attribute_name": "naics_code",
        "change_kind": "value_changed",
        "threshold_value": None,
        "threshold_op": None,
        "notes": (
            "Awarding-agency reclassification of NAICS is a vertical-targeting "
            "signal. Audience cohorts gating on NAICS prefixes need to refresh."
        ),
    },
]


def _get_db_url() -> str:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        log.error("DEX_DB_URL_DIRECT must be set (Doppler hq-all/prd)")
        sys.exit(64)
    return url


def _resolve_source_id(conn: psycopg.Connection, display_name: str) -> UUID:
    row = conn.execute(
        "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
        (display_name,),
    ).fetchone()
    if row is None:
        raise SystemExit(
            f"FAIL: source display_name={display_name!r} not registered in "
            "ops.data_sources. Seed Phase 0a's observability ledger first."
        )
    return row[0]


def main() -> int:
    db_url = _get_db_url()
    with psycopg.connect(db_url, autocommit=True) as conn:
        source_id = _resolve_source_id(conn, SOURCE_DISPLAY_NAME)
        log.info("source: %s = %s", SOURCE_DISPLAY_NAME, source_id)

        for decl in DECLARATIONS:
            row = conn.execute(
                """
                INSERT INTO ops.material_attribute_declarations
                    (source_id, attribute_name, change_kind,
                     threshold_value, threshold_op, notes, declared_by)
                VALUES
                    (%s, %s, %s::material_change_kind,
                     %s, %s::material_threshold_op, %s, %s)
                ON CONFLICT (source_id, attribute_name) DO UPDATE
                    SET change_kind     = EXCLUDED.change_kind,
                        threshold_value = EXCLUDED.threshold_value,
                        threshold_op    = EXCLUDED.threshold_op,
                        notes           = EXCLUDED.notes,
                        declared_by     = COALESCE(EXCLUDED.declared_by, ops.material_attribute_declarations.declared_by),
                        declared_at     = NOW()
                RETURNING declaration_id, (xmax = 0) AS was_inserted
                """,
                (
                    source_id,
                    decl["attribute_name"],
                    decl["change_kind"],
                    decl["threshold_value"],
                    decl["threshold_op"],
                    decl["notes"],
                    "usaspending-pipeline-remediation/2026-05-13",
                ),
            ).fetchone()
            log.info(
                "%s declaration_id=%s attribute=%s",
                "INSERTED" if row[1] else "UPDATED",
                row[0],
                decl["attribute_name"],
            )

    log.info("seeded %d USAspending declarations", len(DECLARATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
