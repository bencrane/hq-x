"""Seed / re-sync ops.scheduled_tasks from the canonical manifest below.

This manifest is THE source of truth for the hq-x Trigger.dev scheduled-task
registry: cadence, the real work each task does (Modal app::function it
dispatches, or the hq-x endpoint it drives), category, and the proposed
priority / SLA tier. It is authored from the task definitions in
src/trigger/*.ts as of the 2026-05-30 modal.Cron -> Trigger.dev migration.

UPSERT SEMANTICS (idempotent, re-run-safe):
  - On INSERT: every column is seeded, including the operator-owned defaults
    (priority, is_sla_critical, is_enabled=true).
  - On CONFLICT (task_id): only the CODE-OWNED columns are refreshed
    (label, description, category, cron, cron_human, timezone, execution_kind,
    modal_app, modal_function, hqx_endpoint, produces, grace_minutes). The
    OPERATOR-OWNED columns (is_enabled, priority, is_sla_critical, notes,
    disabled_*) are left untouched so a re-sync after a cadence change never
    clobbers an operator's toggle or retag.

Run:
  doppler --project hq-x --config dev run -- uv run python -m scripts.seed_scheduled_tasks
  doppler --project hq-x --config prd run -- uv run python -m scripts.seed_scheduled_tasks
"""

# ruff: noqa: E501 — data manifest below reads best as one row per task.

from __future__ import annotations

import asyncio

from app.db import close_pool, get_db_connection, init_pool

# Shared Modal dispatcher target — for the 62 tasks that POST the generic
# dispatch endpoint with {app, function}. The 10 per-feed tasks carry their own
# stable Modal Web Function URL but are modeled the same way (app::function).
DISPATCHER = "trigger-dispatch"

# ── P1 (client-SLA-critical) and P3 (low / infra) task ids ──────────────────
# Everything not listed is P2 (normal). The signal -> direct-mail chain is the
# P1 spine: ingest deltas -> derived/bridges -> signal emit -> matching ->
# surfacing -> SLA-mandated mail. Operator retags any of this in the hq-zone UI.
P1: set[str] = {
    "usaspending-bulk-drip.daily",
    "usaspending-contracts-delta.daily",
    "usaspending-assistance-delta.daily",
    "usaspending-contracts-lance-rebuild.daily",
    "usaspending-recipient-grain-lance-emit.daily",
    "usaspending-derived-views.daily",
    "usaspending-daily-verify.daily",
    "sam-opps-active.daily",
    "sam-opps-active-lance-emit.daily",
    "sam-opps-uei.daily",
    "sam-construction-opps-sized.daily",
    "gtm-usaspending-signals.daily",
    "matching-engine-daily",
    "sba-bridges-daily",
    "dmaas.reconcile_customer_webhook_deliveries",
    "dmaas.reconcile_lob_pieces",
    "hqx.voice_callback_runner",
    "hqx.voice_callback_reminders",
    "intro.dispatch_pending_positives",
}
P3: set[str] = {
    "cluster1.heartbeat",
    "cluster2.heartbeat",
    "cluster3.heartbeat",
    "cluster3.reconciliation_sweep",
    "cluster3.recovery_sweep",
    "clusters.outbound_recovery_sweep",
    "hqx.health_check",
    "polaris-health-check.daily",
    "reap-orphans.15m",
    "material-change-detection.6h",
    "coverage-stats-emit.daily",
}

# ── The manifest. (task_id, category, kind, target, fn_or_endpoint, cron, produces) ──
# kind: "md" = modal_dispatch (target=modal_app, fn=modal_function)
#       "hx" = hqx_compute    (target=hqx_endpoint, fn=None)
_M = "md"
_H = "hx"
TASKS: list[tuple[str, str, str, str, str | None, str, str]] = [
    # ── SEC EDGAR / DERA + BDC SOI (10) ─────────────────────────────────────
    ("sec-edgar-form-8k.scan", "sec", _M, "data-engine-x-sec-edgar-form-8k", "run_form_8k_backfill", "30 3,9,15,21 * * *", "SEC 8-K material-event filings"),
    ("sec-edgar-form-13f.scan", "sec", _M, "data-engine-x-sec-edgar-form-13f", "run_form_13f_backfill", "0 2,8,14,20 * * *", "SEC 13F institutional holdings"),
    ("sec-edgar-form-abs-15g.scan", "sec", _M, "data-engine-x-sec-edgar-form-abs-15g", "run_form_abs_15g_backfill", "0 3,9,15,21 * * *", "SEC ABS-15G filings"),
    ("sec-edgar-schedule-13d-13g.scan", "sec", _M, "data-engine-x-sec-edgar-schedule-13d-13g", "run_schedule_13d_13g_backfill", "0 1,7,13,19 * * *", "SEC 13D/13G beneficial ownership"),
    ("sec-edgar-def-14a.scan", "sec", _M, "data-engine-x-sec-edgar-def-14a", "run_def_14a_backfill", "0 */6 * * *", "SEC DEF 14A proxy statements"),
    ("sec-edgar-form-10k.quarterly", "sec", _M, "data-engine-x-sec-edgar-form-10k", "run_form_10k_backfill", "30 3 1 */3 *", "SEC 10-K annual reports"),
    ("sec-dera-form-d.daily", "sec", _M, "data-engine-x-sec-dera-form-d", "daily_incremental", "0 4 * * *", "SEC DERA Form D private offerings"),
    ("sec-dera-fsds.daily", "sec", _M, "data-engine-x-sec-dera-fsds", "daily_incremental", "0 4 * * *", "SEC DERA financial-statement datasets"),
    ("sec-bdc-soi.monthly", "sec", _M, "data-engine-x-sec-bdc-soi", "monthly_refresh", "0 14 8 * *", "SEC BDC schedule-of-investments"),
    ("bdc-soi-parse-v2.monthly", "sec", _M, "data-engine-x-bdc-soi-parse-v2", "monthly_refresh", "0 14 9 * *", "Re-parsed BDC SOI (chained after sec-bdc-soi)"),

    # ── epiq bankruptcy — 5 ingest legs + 2 bridges (7) ─────────────────────
    ("epiq-cases.daily", "epiq", _M, "data-engine-x-epiq-ingest", "daily_cases_refresh", "0 1 * * *", "epiq bankruptcy cases"),
    ("epiq-claims.daily", "epiq", _M, "data-engine-x-epiq-ingest", "daily_claims_refresh", "0 2 * * *", "epiq bankruptcy claims"),
    ("epiq-dockets.daily", "epiq", _M, "data-engine-x-epiq-ingest", "daily_dockets_refresh", "30 2 * * *", "epiq bankruptcy dockets"),
    ("epiq-claims-resolved.daily", "epiq", _M, "data-engine-x-epiq-ingest", "daily_claims_resolved_refresh", "0 3 * * *", "epiq resolved claims"),
    ("epiq-creditors.daily", "epiq", _M, "data-engine-x-epiq-ingest", "daily_creditors_refresh", "15 3 * * *", "epiq creditors"),
    ("epiq-bridge-ppp-borrower.daily", "epiq", _M, "data-engine-x-epiq-ingest", "daily_bridge_ppp_borrower_refresh", "30 3 * * *", "epiq ↔ PPP borrower bridge"),
    ("epiq-bridge-uspto-owner.daily", "epiq", _M, "data-engine-x-epiq-ingest", "daily_bridge_uspto_owner_refresh", "0 4 * * *", "epiq ↔ USPTO owner bridge"),

    # ── Gov / public-data feed singletons (16) ──────────────────────────────
    ("grants-gov.daily", "gov", _M, "data-engine-x-grants-gov-daily", "run_grants_gov_daily", "0 8 * * *", "Grants.gov opportunities"),
    ("fl-cilb.daily", "gov", _M, "data-engine-x-fl-cilb-daily", "run_daily", "0 11 * * *", "FL CILB contractor licenses"),
    ("caltrans-ccop.daily", "gov", _M, "data-engine-x-caltrans-ccop", "daily_ccop_refresh", "0 14 * * *", "Caltrans CCOP projects"),
    ("az-app-rfp-public.daily", "gov", _M, "data-engine-x-az-app-rfp-public", "daily_az_app_rfp_refresh", "0 17 * * *", "AZ APP public RFPs"),
    ("clinicaltrials-device-studies.weekly", "gov", _M, "data-engine-x-clinicaltrials-device-studies", "weekly_refresh", "0 14 * * 1", "ClinicalTrials.gov device studies"),
    ("ny-data-construction.weekly", "gov", _M, "data-engine-x-ny-data-construction-ingest", "run_backfill", "0 12 * * 1", "NY construction data"),
    ("ny-nyc-local-awards.weekly", "gov", _M, "data-engine-x-ny-nyc-local-awards-ingest", "run_backfill", "0 13 * * 1", "NYC local awards"),
    ("openfda-device.weekly", "gov", _M, "data-engine-x-openfda-device", "weekly_ingest_and_emit", "0 14 * * 1", "openFDA device records"),
    ("opsc-school-facility-funding.weekly", "gov", _M, "data-engine-x-opsc-school-facility-funding", "weekly_opsc_refresh", "0 16 * * 1", "OPSC school facility funding"),
    ("faa-aircraft-registry.weekly", "gov", _M, "data-engine-x-faa-aircraft-registry-ingest", "run_ingest", "0 6 * * 4", "FAA aircraft registry"),
    ("bts-t100-segment.monthly", "gov", _M, "data-engine-x-bts-t100-segment-ingest", "run_ingest", "0 3 5 * *", "BTS T-100 air-segment data"),
    ("faa-airmen.monthly", "gov", _M, "data-engine-x-faa-airmen-ingest", "run_ingest", "0 0 5 * *", "FAA airmen registry"),
    ("overture-places.monthly", "gov", _M, "data-engine-x-overture-places-ingest", "run_ingest", "0 0 5 * *", "Overture Places POIs"),
    ("sbir-awards.monthly", "gov", _M, "data-engine-x-sbir-awards-monthly-ingest", "run_sbir_awards_monthly_ingest", "0 6 5 * *", "SBIR/STTR awards"),
    ("uspto-patents.monthly", "gov", _M, "data-engine-x-uspto-patents", "ingest", "0 8 1 * *", "USPTO patents"),
    ("fdic-call-report.quarterly", "gov", _M, "data-engine-x-fdic-call-report-ingest", "run_quarterly_ingest", "0 6 15 2,5,8,11 *", "FDIC bank call reports"),

    # ── USAspending pipeline (11) ───────────────────────────────────────────
    ("usaspending-bulk-drip.daily", "usaspending", _M, "data-engine-x-usaspending-daily-ingest", "trigger_daily_ingest_via_http", "0 5 * * *", "USAspending bulk daily drip"),
    ("usaspending-contracts-delta.daily", "usaspending", _M, "data-engine-x-usaspending-api-daily-delta", "trigger_daily_delta_via_http", "0 6 * * *", "USAspending contracts delta"),
    ("usaspending-assistance-delta.daily", "usaspending", _M, "data-engine-x-usaspending-api-daily-assistance", "trigger_daily_assistance_via_http", "0 7 * * *", "USAspending assistance delta"),
    ("usaspending-recipient-grain-lance-emit.daily", "usaspending", _M, "data-engine-x-usaspending-recipient-grain-lance-emit", "emit", "0 4 * * *", "USAspending recipient-grain Lance"),
    ("usaspending-contracts-lance-rebuild.daily", "usaspending", _M, "data-engine-x-usaspending-api-daily-lance-rebuild", "run_contracts_lance_daily", "0 8 * * *", "USAspending contracts Lance rebuild"),
    ("usaspending-derived-views.daily", "usaspending", _M, "data-engine-x-usaspending-derived-views-daily", "run_derived_views_daily", "30 8 * * *", "USAspending derived views"),
    ("usaspending-daily-verify.daily", "usaspending", _M, "data-engine-x-usaspending-daily-verify", "run_daily_verify", "0 8 * * *", "USAspending daily freshness verify"),
    ("usaspending-weekly-coverage.weekly", "usaspending", _M, "data-engine-x-usaspending-weekly-coverage", "run_weekly_coverage", "30 12 * * 1", "USAspending weekly coverage stats"),
    ("usaspending-monthly-refresh.monthly", "usaspending", _M, "data-engine-x-usaspending-monthly-ingest", "trigger_monthly_refresh_via_http", "0 6 16 * *", "USAspending monthly full refresh"),
    ("usaspending-contracts-lance-emit.monthly", "usaspending", _M, "data-engine-x-usaspending-contracts-lance-emit", "emit", "0 7 16 * *", "USAspending contracts Lance (monthly)"),
    ("usaspending-recipient-features.monthly", "usaspending", _M, "data-engine-x-usaspending-recipient-features", "emit", "0 9 16 * *", "USAspending recipient features"),

    # ── SAM opportunities (5) ───────────────────────────────────────────────
    ("sam-opps-active.daily", "sam", _M, "data-engine-x-sam-opps-active-daily", "trigger_active_via_http", "0 12 * * *", "SAM active opportunities"),
    ("sam-opps-active-lance-emit.daily", "sam", _M, "data-engine-x-sam-opps-active-lance-emit", "emit", "30 12 * * *", "SAM active opps Lance"),
    ("sam-opps-uei.daily", "sam", _M, "data-engine-x-sam-opps-api-uei-enrichment", "trigger_smart_enrichment_via_http", "0 13 * * *", "SAM opps UEI enrichment"),
    ("sam-construction-opps-sized.daily", "sam", _M, "data-engine-x-sam-construction-opps-sized", "trigger_sized_via_http", "0 16 * * *", "SAM construction opps (sized)"),
    ("sam-opps-archived.weekly", "sam", _M, "data-engine-x-sam-opps-archived-weekly", "trigger_archived_via_http", "0 14 * * 1", "SAM archived opportunities"),

    # ── Pattern-B entity bridges (13) ───────────────────────────────────────
    ("sam-sos-ca-entities-bridge.weekly", "bridges", _M, "data-engine-x-sam-sos-ca-entities-bridge", "weekly_refresh", "0 13 * * 1", "SAM ↔ CA SOS entities bridge"),
    ("sam-sos-ca-principals-cohort.weekly", "bridges", _M, "data-engine-x-sam-sos-ca-principals-cohort", "weekly_refresh", "0 14 * * 1", "SAM ↔ CA SOS principals cohort"),
    ("sam-sos-ny-entities-bridge.weekly", "bridges", _M, "data-engine-x-sam-sos-ny-entities-bridge", "weekly_refresh", "0 14 * * 1", "SAM ↔ NY SOS entities bridge"),
    ("usaspending-sos-ny-owner-bridge.weekly", "bridges", _M, "data-engine-x-usaspending-sos-ny-owner-bridge", "weekly_refresh", "0 15 * * 1", "USAspending ↔ NY SOS owner bridge"),
    ("ppp-sos-ca-bridge.weekly", "bridges", _M, "data-engine-x-ppp-sos-ca-bridge", "weekly_refresh", "0 15 * * 1", "PPP ↔ CA SOS bridge"),
    ("openfda-device-pdl-bridge.weekly", "bridges", _M, "data-engine-x-openfda-device-pdl-bridge", "weekly_refresh", "0 16 * * 1", "openFDA device ↔ PDL bridge"),
    ("sam-sos-fl-entities-bridge.weekly", "bridges", _M, "data-engine-x-sam-sos-fl-entities-bridge", "weekly_refresh", "0 13 * * 2", "SAM ↔ FL SOS entities bridge"),
    ("ppp-sos-fl-bridge.weekly", "bridges", _M, "data-engine-x-ppp-sos-fl-bridge", "weekly_refresh", "0 15 * * 2", "PPP ↔ FL SOS bridge"),
    ("sam-sos-fl-officers-cohort.weekly", "bridges", _M, "data-engine-x-sam-sos-fl-officers-cohort", "weekly_refresh", "0 14 * * 3", "SAM ↔ FL SOS officers cohort"),
    ("ppp-sos-ny-bridge.weekly", "bridges", _M, "data-engine-x-ppp-sos-ny-bridge", "weekly_refresh", "0 15 * * 3", "PPP ↔ NY SOS bridge"),
    ("sba-sos-ny-owner-bridge.weekly", "bridges", _M, "data-engine-x-sba-sos-ny-owner-bridge", "weekly_refresh", "0 16 * * 3", "SBA ↔ NY SOS owner bridge"),
    ("usaspending-sos-fl-owner-bridge.weekly", "bridges", _M, "data-engine-x-usaspending-sos-fl-owner-bridge", "weekly_refresh", "0 16 * * 4", "USAspending ↔ FL SOS owner bridge"),
    ("ppp-ucc-ca-debtor-bridge.weekly", "bridges", _M, "data-engine-x-ppp-ucc-ca-debtor-bridge", "weekly_refresh", "0 16 * * 4", "PPP ↔ CA UCC debtor bridge"),

    # ── Derived Lance emitters + cross-source (8) ───────────────────────────
    ("cms-open-payments-general-emit.daily", "emitters", _M, "data-engine-x-cms-open-payments-general-lance-emit", "emit", "30 7 * * *", "CMS Open Payments (general) Lance"),
    ("cms-open-payments-research-emit.daily", "emitters", _M, "data-engine-x-cms-open-payments-research-lance-emit", "emit", "45 7 * * *", "CMS Open Payments (research) Lance"),
    ("coverage-stats-emit.daily", "emitters", _M, "data-engine-x-coverage-stats-emit", "run_emit", "0 8 * * *", "Cross-source coverage stats"),
    ("gtm-usaspending-signals.daily", "gtm", _M, "data-engine-x-gtm-usaspending-trigger", "run_signals", "0 9 * * *", "GTM USAspending signal emit"),
    ("gleif-lei-records-emit.weekly", "emitters", _M, "data-engine-x-gleif-lei-records-lance-emit", "emit", "0 8 * * 0", "GLEIF LEI records Lance"),
    ("material-change-detection.6h", "infra", _M, "data-engine-x-material-change-cron", "run_cycle", "0 */6 * * *", "Material-change detection cycle"),
    ("polaris-health-check.daily", "infra", _M, "data-engine-x-polaris-health-check", "health_check", "0 6 * * *", "Polaris catalog health check"),
    ("reap-orphans.15m", "infra", _M, "data-engine-x-reap-orphans", "reap_orphans", "*/15 * * * *", "Reap orphaned ingest artifacts"),

    # ── Other feeds + matching (4) ──────────────────────────────────────────
    ("warn-notices.daily", "gov", _M, "data-engine-x-warn-notices", "trigger_refresh_via_http", "30 13 * * *", "WARN Act layoff notices"),
    ("txdot-letting.monthly", "gov", _M, "data-engine-x-txdot-letting-ingest", "trigger_ingest_via_http", "0 8 1 * *", "TxDOT letting schedule"),
    ("matching-engine-daily", "matching", _H, "/api/v1/internal/matching-engine/evaluate-all", None, "0 8 * * *", "Ranked matches + per-channel surfacings"),
    ("sba-bridges-daily", "bridges", _H, "/internal/sba-bridges/run-daily", None, "0 9 * * *", "SBA SOS bridges (feeds matching engine)"),

    # ── Infra / ops runtime — compute in hq-x (15) ──────────────────────────
    ("hqx.voice_callback_runner", "voice", _H, "/internal/voice/callback/run-due-callbacks", None, "* * * * *", "Fires due voice callbacks via Vapi"),
    ("hqx.voice_callback_reminders", "voice", _H, "/internal/voice/callback/reminders", None, "*/5 * * * *", "Voice callback reminders"),
    ("intro.dispatch_pending_positives", "gtm", _H, "/internal/customer-activation/pending-positive-replies", None, "*/5 * * * *", "Dispatches pending positive-reply intros"),
    ("cluster3.recovery_sweep", "infra", _H, "/internal/cluster3/recovery-sweep", None, "*/5 * * * *", "Cluster-3 stuck-queue recovery"),
    ("clusters.outbound_recovery_sweep", "infra", _H, "/internal/clusters/outbound-recovery-sweep", None, "*/15 * * * *", "Outbound stuck-step recovery"),
    ("dmaas.reconcile_customer_webhook_deliveries", "dmaas", _H, "/internal/dmaas/reconcile/customer-webhook-deliveries", None, "*/15 * * * *", "Replays failed customer webhook deliveries"),
    ("cluster3.heartbeat", "infra", _H, "/internal/cluster3/heartbeat", None, "0 * * * *", "Cluster-3 end-to-end synthetic heartbeat"),
    ("cluster1.heartbeat", "infra", _H, "/internal/cluster1/heartbeat", None, "5 * * * *", "Cluster-1 outbound heartbeat"),
    ("cluster2.heartbeat", "infra", _H, "/internal/cluster2/heartbeat", None, "10 * * * *", "Cluster-2 outbound heartbeat"),
    ("cluster3.reconciliation_sweep", "infra", _H, "/internal/cluster3/reconciliation-sweep", None, "0 4 * * *", "Cluster-3 webhook reconciliation"),
    ("dmaas.reconcile_stale_jobs", "dmaas", _H, "/internal/dmaas/reconcile/stale-jobs", None, "0 5 * * *", "Wakes stale DMaaS activation jobs"),
    ("dmaas.reconcile_lob_pieces", "dmaas", _H, "/internal/dmaas/reconcile/lob-pieces", None, "0 6 * * *", "Reconciles Lob piece states"),
    ("dmaas.reconcile_dub_clicks", "dmaas", _H, "/internal/dmaas/reconcile/dub-clicks", None, "0 7 * * *", "Reconciles Dub click attribution"),
    ("dmaas.reconcile_webhook_replays", "dmaas", _H, "/internal/dmaas/reconcile/webhook-replays", None, "0 8 * * *", "Replays dead-lettered webhooks"),
    ("hqx.health_check", "infra", _H, "/internal/health-check/run", None, "0 14 * * *", "Daily hq-x platform health check"),
]

# ── Acronym fixups for auto-generated labels ────────────────────────────────
_ACRONYMS = {
    "sec": "SEC", "edgar": "EDGAR", "dera": "DERA", "fsds": "FSDS", "bdc": "BDC",
    "soi": "SOI", "13f": "13F", "8k": "8-K", "10k": "10-K", "13d": "13D", "13g": "13G",
    "abs": "ABS", "15g": "15G", "def": "DEF", "14a": "14A", "epiq": "epiq", "ppp": "PPP",
    "uspto": "USPTO", "fl": "FL", "ca": "CA", "ny": "NY", "az": "AZ", "nyc": "NYC",
    "cilb": "CILB", "ccop": "CCOP", "rfp": "RFP", "faa": "FAA", "bts": "BTS",
    "opsc": "OPSC", "sbir": "SBIR", "fdic": "FDIC", "usaspending": "USAspending",
    "sam": "SAM", "sos": "SOS", "uei": "UEI", "sba": "SBA", "ucc": "UCC",
    "cms": "CMS", "gleif": "GLEIF", "lei": "LEI", "gtm": "GTM", "dmaas": "DMaaS",
    "hqx": "hq-x", "txdot": "TxDOT", "warn": "WARN", "openfda": "openFDA", "pdl": "PDL",
}
_SUFFIXES = (".scan", ".daily", ".weekly", ".monthly", ".quarterly", ".6h", ".15m")


def _label(task_id: str) -> str:
    base = task_id
    for suf in _SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    words = base.replace(".", " ").replace("_", " ").replace("-", " ").split()
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in words)


def _humanize_cron(cron: str) -> str:
    f = cron.split()
    if len(f) != 5:
        return cron
    mn, hr, dom, mon, dow = f
    days = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}
    if cron == "* * * * *":
        return "every minute"
    if mn.startswith("*/") and hr == "*":
        return f"every {mn[2:]} min"
    if hr == "*":  # hourly at minute mn
        return f"hourly (:{int(mn):02d})"
    if hr.startswith("*/"):
        return f"every {hr[2:]}h"
    if "," in hr:  # multi-times per day
        times = ",".join(f"{int(h):02d}:{int(mn):02d}" for h in hr.split(","))
        return f"{len(hr.split(','))}×/day ({times})"
    hm = f"{int(hr):02d}:{int(mn):02d}"
    if dow != "*":  # weekly
        return f"weekly {days.get(dow, dow)} {hm}"
    if dom != "*":  # monthly / quarterly
        if mon != "*":
            return f"quarterly day {dom} {hm}"
        return f"monthly day {dom} {hm}"
    return f"daily {hm}"


def _grace_minutes(cron: str) -> int:
    f = cron.split()
    mn, hr, dom, mon, dow = f
    if cron == "* * * * *":
        return 10
    if mn.startswith("*/") and hr == "*":
        step = int(mn[2:])
        return 25 if step <= 5 else 50
    if hr == "*":
        return 90
    if hr.startswith("*/") or "," in hr:
        return 180
    if dow != "*":
        return 1440
    if dom != "*":
        return 4320 if mon != "*" else 2880
    return 240


def _description(kind: str, target: str, fn: str | None, produces: str) -> str:
    if kind == _M:
        return (
            f"Trigger.dev fires the cron and dispatches Modal {target}::{fn}. "
            f"Produces: {produces}. Compute runs in Modal — a green run proves the "
            f"dispatch handoff succeeded, not Modal-job completion (layer 2)."
        )
    return (
        f"Trigger.dev fires the cron and calls hq-x {target}. Produces: {produces}. "
        f"Work runs in hq-x — a green run means the work actually executed."
    )


_UPSERT = """
INSERT INTO ops.scheduled_tasks (
    task_id, label, description, category, priority, is_sla_critical,
    cron, cron_human, timezone, execution_kind, modal_app, modal_function,
    hqx_endpoint, produces, grace_minutes, is_enabled
) VALUES (
    %(task_id)s, %(label)s, %(description)s, %(category)s, %(priority)s, %(is_sla_critical)s,
    %(cron)s, %(cron_human)s, 'UTC', %(execution_kind)s, %(modal_app)s, %(modal_function)s,
    %(hqx_endpoint)s, %(produces)s, %(grace_minutes)s, true
)
ON CONFLICT (task_id) DO UPDATE SET
    -- code-owned columns only; operator-owned (is_enabled, priority,
    -- is_sla_critical, notes, disabled_*) are deliberately preserved.
    label          = EXCLUDED.label,
    description    = EXCLUDED.description,
    category       = EXCLUDED.category,
    cron           = EXCLUDED.cron,
    cron_human     = EXCLUDED.cron_human,
    timezone       = EXCLUDED.timezone,
    execution_kind = EXCLUDED.execution_kind,
    modal_app      = EXCLUDED.modal_app,
    modal_function = EXCLUDED.modal_function,
    hqx_endpoint   = EXCLUDED.hqx_endpoint,
    produces       = EXCLUDED.produces,
    grace_minutes  = EXCLUDED.grace_minutes,
    updated_at     = now()
"""


def _rows() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for task_id, cat, kind, target, fn, cron, produces in TASKS:
        if task_id in seen:
            raise ValueError(f"duplicate task_id in manifest: {task_id}")
        seen.add(task_id)
        out.append(
            {
                "task_id": task_id,
                "label": _label(task_id),
                "description": _description(kind, target, fn, produces),
                "category": cat,
                "priority": 1 if task_id in P1 else 3 if task_id in P3 else 2,
                "is_sla_critical": task_id in P1,
                "cron": cron,
                "cron_human": _humanize_cron(cron),
                "execution_kind": "modal_dispatch" if kind == _M else "hqx_compute",
                "modal_app": target if kind == _M else None,
                "modal_function": fn if kind == _M else None,
                "hqx_endpoint": target if kind == _H else None,
                "produces": produces,
                "grace_minutes": _grace_minutes(cron),
            }
        )
    return out


async def main() -> None:
    rows = _rows()
    await init_pool()
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                for r in rows:
                    await cur.execute(_UPSERT, r)
            await conn.commit()
    finally:
        await close_pool()
    p1 = sum(1 for r in rows if r["priority"] == 1)
    p3 = sum(1 for r in rows if r["priority"] == 3)
    md = sum(1 for r in rows if r["execution_kind"] == "modal_dispatch")
    print(
        f"seeded {len(rows)} scheduled tasks "
        f"({md} modal_dispatch, {len(rows) - md} hqx_compute; "
        f"{p1} P1, {len(rows) - p1 - p3} P2, {p3} P3)"
    )


if __name__ == "__main__":
    asyncio.run(main())
