#!/usr/bin/env python3
"""Smoke test for Phase 3 material-change detection + cohort drift.

Drives the full Phase 3 lifecycle against prod data:

  1. Schema gate — verifies ops.material_attribute_declarations,
     ops.material_change_events, ops.material_detection_runs exist;
     verifies the 4 FMCSA declarations seeded.
  2. Picks an existing active audience_spec_signing (Phase 2's TX
     safety-rating-Satisfactory smoke-test signing, 12,338 carriers).
     Reads one DOT_NUMBER out of the cohort manifest as the "victim".
  3. Inserts a SYNTHETIC ops.material_change_events row (no actual
     snapshot diff — we want a deterministic event to scan against).
  4. Runs the cohort_drift_scanner.run_scan_cycle().
  5. Verifies a 'attribute_changed' delivery row appeared with the
     correct metadata.material_change_event_id back-reference.
  6. Hits the 3 REST endpoints:
        GET /api/v1/signings/{signing_id}/drift
        GET /api/v1/cohort-drift/recent
        (verification only — endpoints called via direct service module
        rather than HTTP to avoid round-tripping through Railway.)
  7. Cleans up: deletes the synthetic event + delivery + watermark
     bump (so re-running this script is a no-op against fresh state).

Designed to be idempotent: re-running creates fresh synthetic state +
cleans up. Exit 0 only when every gate passes.

Usage:
    cd ~/hq-all/apps/hq-x && \\
        doppler run --project hq-all --config prd -- \\
        python3 scripts/smoke_phase_3_material_change.py

Required env (from Doppler hq-all/prd):
    HQX_DB_URL_DIRECT
    DEX_DB_URL_DIRECT
    R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    DEX_BASE_URL
    DEX_SUPER_ADMIN_API_KEY
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("smoke-phase-3")

SOURCE_DISPLAY_NAME = "fmcsa_carrier_essentials"
EXPECTED_DECLS = {"safety_rating", "status_code", "power_units", "email_address"}


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("required env var %s not set", name)
        sys.exit(64)
    return val


def _dex_connect():
    import psycopg
    return psycopg.connect(_req("DEX_DB_URL_DIRECT"), autocommit=False)


def _hqx_db_url() -> str:
    return _req("HQX_DB_URL_DIRECT")


# ─── Gate 1: schema present ──────────────────────────────────────────


def gate_schema_present() -> tuple[str, bool]:
    with _dex_connect() as conn:
        n = conn.execute(
            """
            SELECT count(*) FROM information_schema.tables
             WHERE table_schema='ops'
               AND table_name IN ('material_attribute_declarations',
                                  'material_change_events',
                                  'material_detection_runs')
            """
        ).fetchone()[0]
    return ("3 ops.material_* tables present", n == 3)


def gate_declarations_seeded() -> tuple[str, bool]:
    with _dex_connect() as conn:
        rows = conn.execute(
            """
            SELECT attribute_name
              FROM ops.material_attribute_declarations mad
              JOIN ops.data_sources ds ON ds.source_id = mad.source_id
             WHERE ds.display_name = %s
            """,
            (SOURCE_DISPLAY_NAME,),
        ).fetchall()
    found = {r[0] for r in rows}
    ok = EXPECTED_DECLS == found
    return (f"4 FMCSA declarations seeded (have={sorted(found)})", ok)


# ─── Gate 2: pick signing + read victim from cohort manifest ─────────


def pick_active_signing() -> dict:
    import psycopg
    with psycopg.connect(_hqx_db_url(), autocommit=True) as conn:
        row = conn.execute(
            """
            SELECT signing_id::text, spec_id::text, cohort_manifest_uri,
                   count_at_signing
              FROM business.audience_spec_signings
             WHERE expires_at > NOW()
             ORDER BY signed_at DESC
             LIMIT 1
            """
        ).fetchone()
    if row is None:
        log.error("no active signings in business.audience_spec_signings")
        sys.exit(66)
    return {
        "signing_id": row[0],
        "spec_id": row[1],
        "cohort_manifest_uri": row[2],
        "count_at_signing": row[3],
    }


def read_victim_from_manifest(cohort_uri: str) -> str:
    """Read one entity_ref out of the cohort manifest parquet via DuckDB.

    DuckDB's R2 secret resolves r2:// (not s3://) for Cloudflare R2.
    """
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint = os.environ["R2_ENDPOINT"]
    account_id = endpoint.split("//")[-1].split(".")[0]
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{account_id}'
        );
        """
    )
    duckdb_uri = cohort_uri.replace("s3://", "r2://", 1) if cohort_uri.startswith("s3://") else cohort_uri
    row = con.execute(
        f"SELECT entity_ref FROM read_parquet('{duckdb_uri}') LIMIT 1"
    ).fetchone()
    if row is None:
        log.error("cohort manifest %s is empty", cohort_uri)
        sys.exit(66)
    return row[0]


# ─── Gate 3: inject synthetic material_change_event ─────────────────


def inject_synthetic_event(victim_entity_ref: str) -> dict:
    """Insert a synthetic material_change_events row that mimics a real
    safety-rating tier change on `victim_entity_ref`.

    Returns {event_id, declaration_id, detection_run_id, source_id}.
    """
    with _dex_connect() as conn:
        with conn.transaction():
            # Resolve source_id + a declaration_id for safety_rating.
            row = conn.execute(
                """
                SELECT mad.declaration_id::text, mad.source_id::text
                  FROM ops.material_attribute_declarations mad
                  JOIN ops.data_sources ds ON ds.source_id = mad.source_id
                 WHERE ds.display_name = %s
                   AND mad.attribute_name = 'safety_rating'
                """,
                (SOURCE_DISPLAY_NAME,),
            ).fetchone()
            if row is None:
                log.error("safety_rating declaration not found; seed first")
                sys.exit(66)
            declaration_id, source_id = row

            detection_run_id = str(uuid4())
            # Audit row (so the high-water mark advances).
            conn.execute(
                """
                INSERT INTO ops.material_detection_runs
                    (detection_run_id, status, started_at, completed_at,
                     sources_scanned, events_emitted, run_metadata)
                VALUES (%s, 'succeeded', NOW(), NOW(), 1, 1,
                        %s::jsonb)
                """,
                (detection_run_id, json.dumps({"smoke_test": "phase-3"})),
            )
            event_row = conn.execute(
                """
                INSERT INTO ops.material_change_events
                    (declaration_id, source_id, entity_ref,
                     attribute_name, change_kind,
                     old_value, new_value, detection_run_id, notes)
                VALUES (%s, %s, %s, 'safety_rating', 'tier_change',
                        '"Satisfactory"'::jsonb, '"Conditional"'::jsonb,
                        %s, %s)
                RETURNING event_id::text, detected_at
                """,
                (
                    declaration_id, source_id, victim_entity_ref,
                    detection_run_id,
                    "phase-3 smoke test — synthetic event",
                ),
            ).fetchone()
        event_id, detected_at = event_row
    return {
        "event_id": event_id,
        "declaration_id": declaration_id,
        "source_id": source_id,
        "detection_run_id": detection_run_id,
        "detected_at": detected_at,
    }


# ─── Gate 4: reset watermark + run scanner ─────────────────────────


async def reset_watermark_and_scan() -> dict:
    """Reset business.cohort_drift_scan_state.last_detected_at = NULL so
    the scanner re-picks our synthetic event, then run one cycle.
    """
    # Late import; the scanner pulls hq-x DB pool which init-pools lazily.
    sys.path.insert(0, os.path.abspath("."))
    from app.db import close_pool, init_pool
    from app.services.cohort_drift_scanner import run_scan_cycle, _ensure_state_table

    await init_pool()
    try:
        await _ensure_state_table()
        # Reset watermark to NULL so the scanner sees the synthetic event.
        import psycopg
        from app.config import settings
        with psycopg.connect(str(settings.HQX_DB_URL_DIRECT), autocommit=True) as conn:
            conn.execute(
                "UPDATE business.cohort_drift_scan_state SET last_detected_at = NULL WHERE id = 1"
            )

        summary = await run_scan_cycle()
        return summary
    finally:
        await close_pool()


# ─── Gate 5: verify delivery row ────────────────────────────────────


def verify_delivery_row(signing_id: str, event_id: str, victim_entity_ref: str) -> tuple[str, bool, dict | None]:
    import psycopg
    with psycopg.connect(_hqx_db_url(), autocommit=True) as conn:
        row = conn.execute(
            """
            SELECT delivery_id::text, entity_ref, event_kind, channel,
                   attribute_snapshot, metadata
              FROM business.audience_spec_deliveries
             WHERE signing_id = %s
               AND entity_ref = %s
               AND event_kind = 'attribute_changed'
               AND metadata->>'material_change_event_id' = %s
             ORDER BY occurred_at DESC
             LIMIT 1
            """,
            (signing_id, victim_entity_ref, event_id),
        ).fetchone()
    if row is None:
        return ("delivery row for synthetic event", False, None)
    delivery_id, entity_ref, event_kind, channel, attr_snap, metadata = row
    payload = {
        "delivery_id": delivery_id,
        "entity_ref": entity_ref,
        "event_kind": event_kind,
        "channel": channel,
        "attribute_snapshot": attr_snap,
        "metadata": metadata,
    }
    ok = (
        event_kind == "attribute_changed"
        and entity_ref == victim_entity_ref
        and metadata
        and metadata.get("material_change_event_id") == event_id
    )
    return (f"delivery row found: {delivery_id}", ok, payload)


# ─── Gate 6: REST endpoints ─────────────────────────────────────────


async def call_rest_endpoints(signing_id: str) -> tuple[str, bool, dict]:
    """Call the 3 REST endpoints via the service modules + router functions.

    We invoke the handler functions directly (bypassing FastAPI middleware)
    to keep the smoke test independent of a running uvicorn. Sanity-check
    only — full HTTP integration is verified post-deploy.
    """
    sys.path.insert(0, os.path.abspath("."))
    from app.db import close_pool, init_pool
    from app.routers.cohort_drift_v1 import (
        list_drift_for_signing,
        list_recent_drift,
    )
    from app.auth.flexible import SystemContext

    auth = SystemContext()

    await init_pool()
    try:
        drift = await list_drift_for_signing(  # type: ignore[arg-type]
            signing_id=signing_id,
            limit=10,
            _auth=auth,
        )
        recent = await list_recent_drift(  # type: ignore[arg-type]
            limit=10,
            _auth=auth,
        )
        out = {
            "drift_count_for_signing": len(drift),
            "recent_count_total": len(recent),
            "first_drift": drift[0] if drift else None,
        }
        ok = len(drift) >= 1
        return (f"REST endpoints: drift_for_signing={len(drift)} recent={len(recent)}", ok, out)
    finally:
        await close_pool()


# ─── Gate 7: cleanup ─────────────────────────────────────────────────


def cleanup_synthetic(event_id: str, detection_run_id: str, victim_entity_ref: str, signing_id: str) -> None:
    """Delete the synthetic event + delivery + detection run.

    Drops by event_id only (the synthetic event may have produced
    deliveries against multiple active signings sharing the same cohort
    member).
    """
    import psycopg
    with psycopg.connect(_hqx_db_url(), autocommit=True) as conn:
        conn.execute(
            """
            DELETE FROM business.audience_spec_deliveries
             WHERE event_kind = 'attribute_changed'
               AND metadata->>'material_change_event_id' = %s
            """,
            (event_id,),
        )
        # Reset watermark again so the next real run doesn't skip events.
        conn.execute(
            "UPDATE business.cohort_drift_scan_state SET last_detected_at = NULL WHERE id = 1"
        )
    with _dex_connect() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM ops.material_change_events WHERE event_id = %s",
                (event_id,),
            )
            conn.execute(
                "DELETE FROM ops.material_detection_runs WHERE detection_run_id = %s",
                (detection_run_id,),
            )


# ─── Orchestrator ────────────────────────────────────────────────────


def report(label: str, ok: bool, extra: dict | None = None) -> None:
    sym = "PASS" if ok else "FAIL"
    log.info("[%s] %s", sym, label)
    if extra and (not ok or os.environ.get("SMOKE_VERBOSE")):
        log.info("       %s", json.dumps(extra, default=str))


async def main() -> int:
    log.info("==> Phase 3 smoke test starting")
    results: list[tuple[str, bool]] = []

    # Gate 1+1.5 — schema
    label, ok = gate_schema_present()
    report(label, ok)
    results.append((label, ok))
    label, ok = gate_declarations_seeded()
    report(label, ok)
    results.append((label, ok))

    # Gate 2 — pick signing
    signing = pick_active_signing()
    log.info("[INFO] picked signing %s with %d entities, manifest=%s",
             signing["signing_id"], signing["count_at_signing"], signing["cohort_manifest_uri"])

    victim = read_victim_from_manifest(signing["cohort_manifest_uri"])
    log.info("[INFO] victim entity_ref=%r", victim)

    # Gate 3 — inject synthetic event
    event = inject_synthetic_event(victim)
    log.info("[INFO] injected synthetic event %s (detection_run_id=%s)",
             event["event_id"], event["detection_run_id"])

    try:
        # Gate 4 — run scanner
        summary = await reset_watermark_and_scan()
        log.info("[INFO] scanner summary: %s", json.dumps(summary, default=str))
        ok4 = summary["deliveries_inserted"] >= 1
        report("cohort scanner inserted ≥1 delivery", ok4, summary)
        results.append(("cohort scanner inserted ≥1 delivery", ok4))

        # Gate 5 — verify delivery row
        label, ok5, payload = verify_delivery_row(signing["signing_id"], event["event_id"], victim)
        report(label, ok5, payload)
        results.append((label, ok5))

        # Gate 6 — REST endpoints
        label, ok6, rest_out = await call_rest_endpoints(signing["signing_id"])
        report(label, ok6, rest_out)
        results.append((label, ok6))

    finally:
        # Gate 7 — cleanup
        cleanup_synthetic(
            event_id=event["event_id"],
            detection_run_id=event["detection_run_id"],
            victim_entity_ref=victim,
            signing_id=signing["signing_id"],
        )
        log.info("[INFO] cleaned up synthetic event + delivery")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    log.info("==> SUMMARY: %d/%d PASS", passed, total)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
