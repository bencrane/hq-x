"""Shared Trigger.dev -> Modal dispatch endpoint (Modal-cron -> Trigger.dev migration).

Trigger.dev `schedules.task`s POST `{app, function, kwargs}` here; we look up the
target Modal function by name, `.spawn()` it async, and return the call_id.

FIRE-AND-FORGET: all compute runs in the target's own Modal container — never in
Trigger.dev (this is the whole point: Trigger schedules, Modal computes). This one
endpoint replaces N per-app `trigger_*_via_http` wrappers for the derived/chained/
observability feeds migrated 2026-05-29.

Only allowlisted `(app, function)` pairs may be spawned — this endpoint is NOT a
general arbitrary-Modal-invocation surface. Unauthenticated for now, matching the
warn_notices/txdot/usaspending pilot posture; add `requires_proxy_auth=True` +
Modal-Key/Modal-Secret headers before broadening exposure.

Deploy:
    cd ~/hq-all/apps/data-engine-x && doppler run --project hq-all --config prd -- \
        modal deploy modal/trigger_dispatch_app.py
"""
from __future__ import annotations

import modal
from fastapi import HTTPException
from pydantic import BaseModel

app = modal.App("data-engine-x-trigger-dispatch")

image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "fastapi[standard]", "pydantic>=2"
)

# (modal_app_name, function_name) pairs this dispatcher is permitted to spawn.
ALLOWLIST: set[tuple[str, str]] = {
    ("data-engine-x-usaspending-contracts-lance-emit", "emit"),
    ("data-engine-x-usaspending-recipient-grain-lance-emit", "emit"),
    ("data-engine-x-sam-opps-active-lance-emit", "emit"),
    ("data-engine-x-sam-sos-ca-entities-bridge", "weekly_refresh"),
    ("data-engine-x-sam-sos-ca-principals-cohort", "weekly_refresh"),
    ("data-engine-x-sam-sos-fl-entities-bridge", "weekly_refresh"),
    ("data-engine-x-sam-sos-fl-officers-cohort", "weekly_refresh"),
    ("data-engine-x-sam-sos-ny-entities-bridge", "weekly_refresh"),
    ("data-engine-x-usaspending-sos-fl-owner-bridge", "weekly_refresh"),
    ("data-engine-x-usaspending-sos-ny-owner-bridge", "weekly_refresh"),
    ("data-engine-x-usaspending-api-daily-lance-rebuild", "run_contracts_lance_daily"),
    ("data-engine-x-usaspending-derived-views-daily", "run_derived_views_daily"),
    ("data-engine-x-usaspending-recipient-features", "emit"),
    ("data-engine-x-usaspending-daily-verify", "run_daily_verify"),
    ("data-engine-x-usaspending-weekly-coverage", "run_weekly_coverage"),
    # ── Batch A (2026-05-30): SEC (9) ──
    ("data-engine-x-sec-edgar-form-8k", "run_form_8k_backfill"),
    ("data-engine-x-sec-edgar-form-10k", "run_form_10k_backfill"),
    ("data-engine-x-sec-edgar-form-13f", "run_form_13f_backfill"),
    ("data-engine-x-sec-edgar-form-abs-15g", "run_form_abs_15g_backfill"),
    ("data-engine-x-sec-edgar-schedule-13d-13g", "run_schedule_13d_13g_backfill"),
    ("data-engine-x-sec-edgar-def-14a", "run_def_14a_backfill"),
    ("data-engine-x-sec-bdc-soi", "monthly_refresh"),
    ("data-engine-x-sec-dera-form-d", "daily_incremental"),
    ("data-engine-x-sec-dera-fsds", "daily_incremental"),
    # ── Batch A: epiq — 5 ingest legs ONLY (bridges deliberately omitted) ──
    ("data-engine-x-epiq-ingest", "daily_cases_refresh"),
    ("data-engine-x-epiq-ingest", "daily_claims_refresh"),
    ("data-engine-x-epiq-ingest", "daily_dockets_refresh"),
    ("data-engine-x-epiq-ingest", "daily_claims_resolved_refresh"),
    ("data-engine-x-epiq-ingest", "daily_creditors_refresh"),
    # ── Batch A: gov singletons (16) ──
    ("data-engine-x-az-app-rfp-public", "daily_az_app_rfp_refresh"),
    ("data-engine-x-bts-t100-segment-ingest", "run_ingest"),
    ("data-engine-x-caltrans-ccop", "daily_ccop_refresh"),
    ("data-engine-x-clinicaltrials-device-studies", "weekly_refresh"),
    ("data-engine-x-faa-aircraft-registry-ingest", "run_ingest"),
    ("data-engine-x-faa-airmen-ingest", "run_ingest"),
    ("data-engine-x-fdic-call-report-ingest", "run_quarterly_ingest"),
    ("data-engine-x-fl-cilb-daily", "run_daily"),
    ("data-engine-x-grants-gov-daily", "run_grants_gov_daily"),
    ("data-engine-x-ny-data-construction-ingest", "run_backfill"),
    ("data-engine-x-ny-nyc-local-awards-ingest", "run_backfill"),
    ("data-engine-x-openfda-device", "weekly_ingest_and_emit"),
    ("data-engine-x-opsc-school-facility-funding", "weekly_opsc_refresh"),
    ("data-engine-x-overture-places-ingest", "run_ingest"),
    ("data-engine-x-sbir-awards-monthly-ingest", "run_sbir_awards_monthly_ingest"),
    ("data-engine-x-uspto-patents", "ingest"),
    # ── Derived emitters / bridges / infra (2026-05-30) — non-FMCSA remainder ──
    ("data-engine-x-bdc-soi-parse-v2", "monthly_refresh"),
    ("data-engine-x-cms-open-payments-general-lance-emit", "emit"),
    ("data-engine-x-cms-open-payments-research-lance-emit", "emit"),
    ("data-engine-x-coverage-stats-emit", "run_emit"),
    ("data-engine-x-gleif-lei-records-lance-emit", "emit"),
    ("data-engine-x-gtm-usaspending-trigger", "run_signals"),
    ("data-engine-x-material-change-cron", "run_cycle"),
    ("data-engine-x-openfda-device-pdl-bridge", "weekly_refresh"),
    ("data-engine-x-polaris-health-check", "health_check"),
    ("data-engine-x-ppp-sos-ca-bridge", "weekly_refresh"),
    ("data-engine-x-ppp-sos-fl-bridge", "weekly_refresh"),
    ("data-engine-x-ppp-sos-ny-bridge", "weekly_refresh"),
    ("data-engine-x-ppp-ucc-ca-debtor-bridge", "weekly_refresh"),
    ("data-engine-x-reap-orphans", "reap_orphans"),
    ("data-engine-x-sba-sos-ny-owner-bridge", "weekly_refresh"),
    ("data-engine-x-epiq-ingest", "daily_bridge_ppp_borrower_refresh"),
    ("data-engine-x-epiq-ingest", "daily_bridge_uspto_owner_refresh"),
}


class DispatchReq(BaseModel):
    app: str
    function: str
    kwargs: dict = {}


@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def dispatch(req: DispatchReq) -> dict:
    if (req.app, req.function) not in ALLOWLIST:
        raise HTTPException(status_code=403, detail=f"not allowlisted: {req.app}::{req.function}")
    fn = modal.Function.from_name(req.app, req.function)
    call = fn.spawn(**(req.kwargs or {}))
    return {"call_id": call.object_id, "app": req.app, "function": req.function}
