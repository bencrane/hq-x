"""Phase 3 — cohort-drift scanner.

For each material-change event detected by DEX, scan active signings
in business.audience_spec_signings and emit an 'attribute_changed'
delivery into business.audience_spec_deliveries when the changed entity
is present in the signed cohort manifest.

This is the bridge between (a) DEX's ops.material_change_events ledger
and (b) hq-x's business.audience_spec_deliveries — the contracted
surface that operator-internal UIs and the alerter both consume.

Per outbound_is_emailbison_intros_are_on_platform.md: cohort drift
surfaces in-platform (operator review queue), never as a cold-email
side effect. Per matches_first_class_surfacing_multichannel.md: a match
involving any entity — platform or cold — is first-class; cohort drift
applies uniformly.

Per operator_data_anxieties_phase_0.md concern #3: this is the
"trust-contract" surface. The operator's example — insurance agent
matched on safety rating X, carrier had a material event making it Y —
is the load-bearing scenario this service exists to handle.

Public API:

    scan_material_change(material_change_event: dict) -> list[dict]
        For one event, return the list of (signing_id, delivery_id)
        affected. Synchronously inserts deliveries.

    run_scan_cycle() -> dict
        Pull all material_change_events from DEX since the last
        watermark, scan each, advance the watermark. Returns
        {events_scanned, deliveries_inserted, signings_affected, ts}.

The scanner persists its high-water-mark in business.cohort_drift_scan_state
(a 1-row table) so consecutive runs only process new events. Modal cron
calls run_scan_cycle every 6 hours alongside the detector.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from app.config import settings
from app.db import get_db_connection

LOG = logging.getLogger(__name__)


# ─── DuckDB singleton (for reading cohort manifest parquets) ─────────


_duckdb_lock = threading.Lock()
_duckdb_con: Any = None


def _get_duckdb() -> Any:
    """One DuckDB connection per process; cohort manifests are R2 parquet."""
    global _duckdb_con
    import duckdb
    with _duckdb_lock:
        if _duckdb_con is None:
            con = duckdb.connect(":memory:")
            con.execute("INSTALL httpfs; LOAD httpfs;")
            endpoint = os.environ.get("R2_ENDPOINT")
            if endpoint:
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
            _duckdb_con = con
        return _duckdb_con


# ─── DEX HTTP client ─────────────────────────────────────────────────


def _dex_base() -> str:
    base = settings.DEX_BASE_URL or os.environ.get("DEX_BASE_URL")
    if not base:
        raise RuntimeError("DEX_BASE_URL not set (Doppler hq-all/prd)")
    return base.rstrip("/")


def _dex_api_key() -> str:
    key = settings.DEX_SUPER_ADMIN_API_KEY
    if key is not None:
        return key.get_secret_value()
    raw = os.environ.get("DEX_SUPER_ADMIN_API_KEY")
    if not raw:
        raise RuntimeError("DEX_SUPER_ADMIN_API_KEY not set (Doppler hq-all/prd)")
    return raw


def fetch_material_events_since(
    after_detected_at: str | None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Pull events newer than the watermark from DEX.

    Prefers the DEX HTTP endpoint when available; falls back to a direct
    DEX DB read when the endpoint isn't reachable (e.g. pre-deploy
    smoke testing). Direct-DB falls back if DEX_DB_URL_DIRECT or
    DEX_DB_URL_POOLED is set in the hq-x environment.
    """
    params: dict[str, str | int] = {"limit": limit}
    if after_detected_at:
        params["after_detected_at"] = after_detected_at
    url = f"{_dex_base()}/api/v1/internal/observability/material-changes/events"
    headers = {
        "Authorization": f"Bearer {_dex_api_key()}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, params=params, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            LOG.warning(
                "DEX material-changes endpoint not deployed yet; "
                "falling back to direct DB read"
            )
            return _fetch_material_events_direct(after_detected_at, limit)
        raise RuntimeError(
            f"DEX events fetch failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    except (httpx.RequestError, httpx.HTTPError):
        LOG.warning("DEX HTTP fetch failed; falling back to direct DB read")
        return _fetch_material_events_direct(after_detected_at, limit)


def _fetch_material_events_direct(
    after_detected_at: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Direct read against DEX DB. Used as fallback when the HTTP
    endpoint isn't deployed yet.

    Requires DEX_DB_URL_DIRECT or DEX_DB_URL_POOLED in env. Used during
    pre-deploy smoke testing; the production Modal cron always goes
    through the HTTP path.
    """
    import json as _json
    import psycopg
    from datetime import datetime as _dt

    dex_db = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not dex_db:
        raise RuntimeError("neither DEX HTTP endpoint nor DEX_DB_URL is reachable")

    cutoff: _dt | None = None
    if after_detected_at:
        cutoff = _dt.fromisoformat(after_detected_at)

    with psycopg.connect(dex_db) as conn:
        if cutoff is None:
            rows = conn.execute(
                """
                SELECT mce.event_id::text,
                       mce.declaration_id::text,
                       mce.source_id::text,
                       ds.display_name,
                       mce.entity_ref,
                       mce.attribute_name,
                       mce.change_kind::text,
                       mce.old_value::text,
                       mce.new_value::text,
                       mce.detected_at,
                       mce.detection_run_id::text
                  FROM ops.material_change_events mce
                  JOIN ops.data_sources ds ON ds.source_id = mce.source_id
                 ORDER BY mce.detected_at ASC
                 LIMIT %s
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT mce.event_id::text,
                       mce.declaration_id::text,
                       mce.source_id::text,
                       ds.display_name,
                       mce.entity_ref,
                       mce.attribute_name,
                       mce.change_kind::text,
                       mce.old_value::text,
                       mce.new_value::text,
                       mce.detected_at,
                       mce.detection_run_id::text
                  FROM ops.material_change_events mce
                  JOIN ops.data_sources ds ON ds.source_id = mce.source_id
                 WHERE mce.detected_at > %s
                 ORDER BY mce.detected_at ASC
                 LIMIT %s
                """,
                (cutoff, limit),
            ).fetchall()
    out = []
    for r in rows:
        out.append({
            "event_id": r[0],
            "declaration_id": r[1],
            "source_id": r[2],
            "source_display_name": r[3],
            "entity_ref": r[4],
            "attribute_name": r[5],
            "change_kind": r[6],
            "old_value": _json.loads(r[7]) if r[7] is not None else None,
            "new_value": _json.loads(r[8]) if r[8] is not None else None,
            "detected_at": r[9].isoformat() if r[9] else None,
            "detection_run_id": r[10],
        })
    return out


# ─── Watermark state ─────────────────────────────────────────────────


async def _ensure_state_table() -> None:
    """Create business.cohort_drift_scan_state if missing (1-row state).

    Kept here (not in a migration) because it's a single operator-internal
    state row, not a contract-substrate table. If we want it as a true
    migration later we lift this into apps/hq-x/migrations/.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS business.cohort_drift_scan_state (
                    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    last_detected_at timestamptz,
                    last_run_at timestamptz NOT NULL DEFAULT NOW(),
                    last_events_scanned integer NOT NULL DEFAULT 0,
                    last_deliveries_inserted integer NOT NULL DEFAULT 0,
                    notes text
                )
                """
            )
            await cur.execute(
                """
                INSERT INTO business.cohort_drift_scan_state (id)
                VALUES (1) ON CONFLICT (id) DO NOTHING
                """
            )
        await conn.commit()


async def _get_watermark() -> str | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT last_detected_at FROM business.cohort_drift_scan_state WHERE id = 1"
            )
            row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    ts: datetime = row[0]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


async def _set_watermark(
    new_watermark_iso: str,
    events_scanned: int,
    deliveries_inserted: int,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cohort_drift_scan_state
                   SET last_detected_at         = %s::timestamptz,
                       last_run_at              = NOW(),
                       last_events_scanned      = %s,
                       last_deliveries_inserted = %s
                 WHERE id = 1
                """,
                (new_watermark_iso, events_scanned, deliveries_inserted),
            )
        await conn.commit()


# ─── Cohort membership check ─────────────────────────────────────────


def _signing_contains_entity(cohort_manifest_uri: str, entity_ref: str) -> bool:
    """Read cohort manifest parquet from R2; return whether entity_ref is in it.

    cohort_manifest_uri shape (from the evaluator's _cohort_manifest_uri):
        s3://<bucket>/audience-cohort-manifests/YYYY/MM/DD/<signing_id>.parquet

    DuckDB's R2 secret resolves `r2://` (NOT `s3://`) when the bucket
    lives in Cloudflare R2 — rewrite the scheme defensively so the
    evaluator's existing s3:// URIs work without a migration.

    The manifest has two columns: entity_ref (TEXT) + attribute_snapshot (JSON).
    """
    duckdb_uri = cohort_manifest_uri.replace("s3://", "r2://", 1) if cohort_manifest_uri.startswith("s3://") else cohort_manifest_uri
    con = _get_duckdb()
    # Parametrize via DuckDB's $1 placeholder, not f-string — entity_ref
    # is from the trusted DEX event payload but defense-in-depth.
    row = con.execute(
        f"""
        SELECT 1 FROM read_parquet('{duckdb_uri}')
         WHERE entity_ref = $1
         LIMIT 1
        """,
        [entity_ref],
    ).fetchone()
    return row is not None


# ─── Affected-signing lookup ─────────────────────────────────────────


async def _list_active_signings() -> list[dict[str, Any]]:
    """All non-expired signings in business.audience_spec_signings.

    JOIN to business.audience_specs to surface partner_id + spec status.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.signing_id, s.spec_id, s.cohort_manifest_uri,
                       s.signed_at, s.count_at_signing, s.expires_at,
                       sp.partner_id
                  FROM business.audience_spec_signings s
                  JOIN business.audience_specs sp ON sp.spec_id = s.spec_id
                 WHERE s.expires_at > NOW()
                """
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


async def scan_material_change(
    event: dict[str, Any],
    *,
    fire_telegram: bool = True,
) -> list[dict[str, Any]]:
    """For one material_change_event, find affected signings, insert
    'attribute_changed' deliveries, fire Telegram alerts.

    Returns list of {signing_id, delivery_id, entity_ref, event_id,
    telegram: {status, ...}} per affected signing.
    """
    affected: list[dict[str, Any]] = []
    entity_ref = event["entity_ref"]
    signings = await _list_active_signings()

    if not signings:
        LOG.info("scan_material_change: no active signings; skipping event_id=%s", event["event_id"])
        return []

    attribute_snapshot = {
        "attribute_name":   event["attribute_name"],
        "change_kind":      event["change_kind"],
        "old_value":        event.get("old_value"),
        "new_value":        event.get("new_value"),
        "detected_at":      event.get("detected_at"),
        "source_display_name": event.get("source_display_name"),
    }

    # Optional partner-org lookup for the alert text.
    org_names_by_id = await _fetch_org_names({str(s["partner_id"]) for s in signings})

    for signing in signings:
        cohort_uri = signing["cohort_manifest_uri"]
        try:
            in_cohort = _signing_contains_entity(cohort_uri, entity_ref)
        except Exception as exc:
            LOG.warning(
                "could not check manifest %s for entity %s: %s; treating as miss",
                cohort_uri, entity_ref, exc,
            )
            in_cohort = False
        if not in_cohort:
            continue

        metadata = {
            "material_change_event_id": event["event_id"],
            "signing_id": str(signing["signing_id"]),
            "detection_run_id": event.get("detection_run_id"),
            "declaration_id": event.get("declaration_id"),
        }

        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO business.audience_spec_deliveries
                        (signing_id, entity_ref, event_kind, channel,
                         attribute_snapshot, metadata)
                    VALUES (
                        %s, %s, 'attribute_changed', 'portal',
                        %s::jsonb, %s::jsonb
                    )
                    RETURNING delivery_id
                    """,
                    (
                        str(signing["signing_id"]),
                        entity_ref,
                        json.dumps(attribute_snapshot),
                        json.dumps(metadata),
                    ),
                )
                row = await cur.fetchone()
            await conn.commit()

        if row is None:
            continue

        rec = {
            "signing_id": str(signing["signing_id"]),
            "spec_id": str(signing["spec_id"]),
            "partner_id": str(signing["partner_id"]),
            "partner_name": org_names_by_id.get(str(signing["partner_id"])),
            "delivery_id": str(row[0]),
            "entity_ref": entity_ref,
            "event_id": event["event_id"],
            "attribute_snapshot": attribute_snapshot,
        }

        if fire_telegram:
            try:
                tg = _send_telegram_cohort_drift_alert(
                    signing=signing,
                    event=event,
                    partner_name=rec["partner_name"],
                    delivery_id=rec["delivery_id"],
                )
                rec["telegram"] = tg
            except Exception as exc:
                LOG.exception("telegram alert failed for signing_id=%s", signing["signing_id"])
                rec["telegram"] = {"status": "failed", "error": str(exc)}

        affected.append(rec)
    return affected


async def _fetch_org_names(partner_ids: set[str]) -> dict[str, str]:
    """Map partner_id (UUID str) → org name. Empty dict on miss."""
    if not partner_ids:
        return {}
    placeholders = ",".join(["%s"] * len(partner_ids))
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id::text, name FROM business.organizations WHERE id::text IN ({placeholders})",
                tuple(partner_ids),
            )
            rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


# ─── Telegram fire ────────────────────────────────────────────────────


def _telegram_bot_token() -> str | None:
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def _telegram_chat_id() -> str | None:
    return os.environ.get("TELEGRAM_ALERT_CHAT_ID")


def _send_telegram_cohort_drift_alert(
    signing: dict[str, Any],
    event: dict[str, Any],
    partner_name: str | None,
    delivery_id: str,
) -> dict[str, Any]:
    """POST a cohort_drift alert to Telegram and return delivery metadata."""
    token = _telegram_bot_token()
    chat_id = _telegram_chat_id()
    if not token or not chat_id:
        return {"status": "skipped", "reason": "TELEGRAM_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID not set"}

    partner = partner_name or f"partner_id={signing['partner_id']}"
    source = event.get("source_display_name", "?")
    attr = event.get("attribute_name", "?")
    kind = event.get("change_kind", "?")
    old_val = event.get("old_value")
    new_val = event.get("new_value")
    signing_id = signing["signing_id"]
    signed_at = signing.get("signed_at")
    if hasattr(signed_at, "isoformat"):
        signed_at = signed_at.isoformat()
    count_at_signing = signing.get("count_at_signing")
    entity_ref = event.get("entity_ref")

    text = (
        f"[COHORT DRIFT] {partner}\n"
        f"signing_id: {signing_id}\n"
        f"signed_at: {signed_at}\n"
        f"count_at_signing: {count_at_signing}\n"
        f"---\n"
        f"entity_ref: {entity_ref}\n"
        f"source: {source}\n"
        f"attribute: {attr}\n"
        f"change_kind: {kind}\n"
        f"old → new: {old_val} → {new_val}\n"
        f"---\n"
        f"delivery_id: {delivery_id}\n"
        f"action: review match in operator queue; possibly invalidate"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(url, json=body)
    try:
        tg_response = resp.json()
    except Exception:
        tg_response = {"raw": resp.text[:300]}
    return {
        "status": "sent" if resp.status_code == 200 and tg_response.get("ok") is True else "failed",
        "http_status": resp.status_code,
        "telegram_response": tg_response,
    }


# ─── Cycle orchestrator ──────────────────────────────────────────────


async def run_scan_cycle() -> dict[str, Any]:
    """Pull events since the watermark; scan each; advance the watermark.

    Returns:
        {events_scanned, deliveries_inserted, signings_affected, ts,
         high_water_mark}
    """
    await _ensure_state_table()
    watermark = await _get_watermark()
    LOG.info("cohort_drift_scanner: watermark=%s", watermark)

    events = fetch_material_events_since(watermark, limit=500)
    LOG.info("cohort_drift_scanner: %d new events", len(events))

    deliveries: list[dict[str, Any]] = []
    last_detected = watermark
    for event in events:
        try:
            affected = await scan_material_change(event)
            deliveries.extend(affected)
        except Exception as exc:
            LOG.exception("scan failed for event_id=%s", event.get("event_id"))
            continue
        if event.get("detected_at"):
            # Advance watermark monotonically (events come sorted ASC).
            last_detected = event["detected_at"]

    if events and last_detected:
        await _set_watermark(
            new_watermark_iso=last_detected,
            events_scanned=len(events),
            deliveries_inserted=len(deliveries),
        )

    signings_affected = len({d["signing_id"] for d in deliveries})

    return {
        "events_scanned": len(events),
        "deliveries_inserted": len(deliveries),
        "signings_affected": signings_affected,
        "high_water_mark": last_detected,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "fetch_material_events_since",
    "scan_material_change",
    "run_scan_cycle",
]
