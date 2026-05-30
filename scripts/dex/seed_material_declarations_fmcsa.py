#!/usr/bin/env python3
"""Seed 4 FMCSA material-attribute declarations for the Phase 3 canary.

Per directive 2026-05-12-hq-all-phase-3-material-change-detection.md §B:
declare the FMCSA carrier_essentials source's material attributes so the
detector emits change events when a carrier's safety_rating, status_code,
power_units, or email_address materially shifts.

Notes on the four attributes (vs. the directive's named four):

  - safety_rating       — change_kind=tier_change.
                          (Satisfactory → Conditional → Unsatisfactory.)
                          MATCHES directive item 1.

  - status_code         — change_kind=value_revoked.
                          The directive specifies "operating_authority_status
                          → value_revoked"; FMCSA's status_code is the
                          column-level proxy on carrier_essentials (A→I
                          transitions correspond to authority becoming
                          inactive). MATCHES directive item 2.

  - power_units         — change_kind=threshold_crossed, op=eq, threshold=0.
                          The directive specifies "oos_orders_24mo → threshold
                          crossed". carrier_essentials does not carry OOS
                          orders directly; power_units dropping to 0 is the
                          column-level proxy for "fleet effectively shut
                          down" and is a comparable wrong-match risk for an
                          insurance partner. SUBSTITUTED — operator note in
                          the declaration.

  - email_address       — change_kind=value_disappeared.
                          The directive specifies "insurance_on_file →
                          value_disappeared"; carrier_essentials doesn't
                          carry an insurance column, but email_address
                          disappearing is the column-level proxy that
                          breaks contact integrity for outbound + intro
                          flows. SUBSTITUTED — operator note in the
                          declaration.

Re-running this script is a no-op (UPSERT on (source_id, attribute_name)).

Usage:
    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/seed_material_declarations_fmcsa.py
"""
from __future__ import annotations

import logging
import os
import sys
from uuid import UUID

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Source we declare against. Looked up by display_name; resolved at runtime
# rather than hardcoded UUID so this stays decoupled from the prod source_id.
SOURCE_DISPLAY_NAME = "fmcsa_carrier_essentials"

DECLARATIONS = [
    {
        "attribute_name": "safety_rating",
        "change_kind": "tier_change",
        "threshold_value": None,
        "threshold_op": None,
        "notes": (
            "FMCSA Safety Rating tier change (Satisfactory → Conditional → "
            "Unsatisfactory). Per operator-data-anxieties #3 (wrong matches "
            "from stale data): the canonical example — insurance partner "
            "matched on rating X discovers it's now Y."
        ),
    },
    {
        "attribute_name": "status_code",
        "change_kind": "value_revoked",
        "threshold_value": None,
        "threshold_op": None,
        "notes": (
            "FMCSA status_code transition away from active (A) to inactive (I) "
            "or NULL. Column-level proxy for the directive's "
            "'operating_authority_status: value_revoked' — carrier_essentials "
            "carries this directly."
        ),
    },
    {
        "attribute_name": "power_units",
        "change_kind": "threshold_crossed",
        "threshold_value": 0,
        "threshold_op": "eq",
        "notes": (
            "power_units = 0 indicates fleet has effectively shut down. "
            "Column-level proxy for the directive's 'oos_orders_24mo: "
            "threshold_crossed' since carrier_essentials does not carry "
            "OOS-order counts; substituted to keep the Phase 3 canary "
            "operative against the existing schema."
        ),
    },
    {
        "attribute_name": "email_address",
        "change_kind": "value_disappeared",
        "threshold_value": None,
        "threshold_op": None,
        "notes": (
            "email_address disappearing breaks contact integrity for any "
            "audience cohort that filtered on the presence of an outbound "
            "channel. Column-level proxy for the directive's "
            "'insurance_on_file: value_disappeared' since carrier_essentials "
            "does not carry an insurance column."
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
                    str(source_id),
                    decl["attribute_name"],
                    decl["change_kind"],
                    decl["threshold_value"],
                    decl["threshold_op"],
                    decl["notes"],
                    "phase-3-seed-script",
                ),
            ).fetchone()
            if row is None:
                log.warning("upsert produced no row for %s", decl["attribute_name"])
                continue
            declaration_id, was_inserted = row
            log.info(
                "  %-15s kind=%-20s %s declaration_id=%s",
                decl["attribute_name"],
                decl["change_kind"],
                "INSERTED" if was_inserted else "UPDATED",
                declaration_id,
            )

        cnt = conn.execute(
            "SELECT COUNT(*) FROM ops.material_attribute_declarations WHERE source_id = %s",
            (str(source_id),),
        ).fetchone()[0]
        log.info("total declarations for %s: %d", SOURCE_DISPLAY_NAME, cnt)

    return 0


if __name__ == "__main__":
    sys.exit(main())
