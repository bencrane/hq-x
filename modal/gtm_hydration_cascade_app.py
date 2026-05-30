"""GTM hydration cascade Modal app — Phase 1 Bulk Firmographic Hydration.

Web Function target for the hq-x `/internal/tasks/enrich` proxy's
`provider == "modal"` branch (see apps/hq-x/app/routers/internal/
gtm_pipeline.py). Receives one POST per cohort row from the Trigger.dev
`gtm_hydration_cascade_test` orchestrator (via the proxy) and runs the
Blitz firmographic waterfall for that single `{uei, domain}`.

Stable URL (workspace `bencrane`):
    https://bencrane--data-engine-x-gtm-hydration-modal-run.modal.run

Concurrency contract:
    Blitz API caps inbound traffic at 5 req/sec across the workspace.
    `max_containers=1` serializes Blitz calls inside Modal — sustained
    throughput is ~2 req/sec at ~500ms per call. Excess Trigger.dev fan-out
    queues at Modal's input buffer instead of breaching the upstream rate
    limit. Hard cap, not a latency-dependent estimate.

Failure semantics:
    The function returns `{"status": "failed", "error": ...}` with HTTP 200
    on Blitz-side errors. The hq-x enrich proxy's modal branch
    (apps/hq-x/app/routers/internal/gtm_pipeline.py) now inspects
    `result_payload["status"]` and demotes the ledger row to `'failed'`
    when this body is returned — so the Trigger orchestrator's
    `ack.status === "completed"` check sees the truth.

Routing — two-hop Blitz waterfall:
    Hop 1 (conditional): POST /v2/enrichment/domain-to-linkedin with
        {"domain": ...} and harvest `company_linkedin_url` from the
        response. Skipped when PDL already supplied the LinkedIn URL.
    Hop 2 (always): POST /v2/enrichment/company with strict key
        {"company_linkedin_url": target_url}. The target URL is either
        the PDL-pre-resolved value or the Hop 1 output.
    The success response carries the target LinkedIn URL back to the
    proxy so it can be persisted on the ledger row.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/gtm_hydration_cascade_app.py

Local smoke test:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal serve modal/gtm_hydration_cascade_app.py
"""
from __future__ import annotations

import logging
import os
from typing import Any

import modal
from pydantic import BaseModel

app = modal.App("data-engine-x-gtm-hydration-modal")

# Minimal image: httpx for the Blitz call, fastapi/pydantic for the
# `@modal.web_endpoint` request-parsing surface.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "httpx>=0.27,<1.0",
        "fastapi>=0.110,<1.0",
        "pydantic>=2,<3",
    )
)

# The `blitz-api` Modal secret must expose BLITZAPI_API_KEY (mirror of the
# Doppler `hq-all/prd` value). Create via:
#   modal secret create blitz-api BLITZAPI_API_KEY=$(doppler secrets get \\
#       BLITZAPI_API_KEY --plain --project hq-all --config prd)
FUNCTION_SECRETS = [
    modal.Secret.from_name("blitz-api"),
]

# Blitz API workspace cap is 5 req/sec. With max_containers=1 we serialize
# Blitz calls inside Modal — at ~500ms per call, sustained throughput is
# ~2 req/sec, well under the cap. Hard guard rather than a latency-dependent
# estimate (the previous max_containers=4 implicitly assumed ~1s per call
# and produced bursts of 8-15 req/sec when cached responses returned faster,
# triggering Blitz 429s during the 2026-05-26 fast-lane run).
_CONCURRENCY_LIMIT = 1

# Blitz two-hop waterfall — both hops live on the same workspace API key,
# but the endpoints and required body keys differ. Hop 1 resolves a domain
# to a LinkedIn URL when PDL didn't already supply one; Hop 2 enriches the
# firmograph keyed STRICTLY on `company_linkedin_url` (not `linkedin_url`).
_BLITZ_DOMAIN_TO_LINKEDIN = "https://api.blitz-api.ai/v2/enrichment/domain-to-linkedin"
_BLITZ_COMPANY_ENRICHMENT = "https://api.blitz-api.ai/v2/enrichment/company"
_BLITZ_TIMEOUT_SECONDS = 30.0

# Per-call function budget. Bounded under the proxy's 80s httpx timeout
# (gtm_pipeline.py modal branch) so the upstream sees a clean error rather
# than a Modal-side abort mid-flight.
_FUNCTION_TIMEOUT_SECONDS = 60

logger = logging.getLogger(__name__)


class _EntityData(BaseModel):
    uei: str
    domain: str
    # PDL-bridge LinkedIn URL when the upstream sam_pdl_domain bridge matched
    # this UEI to a PDL company. When present, used as the deterministic Blitz
    # key; when null, falls back to the SAM-derived `domain`.
    linkedin_url: str | None = None


class _HydrationRequest(BaseModel):
    action: str
    entity_data: _EntityData


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    max_containers=_CONCURRENCY_LIMIT,
    timeout=_FUNCTION_TIMEOUT_SECONDS,
)
@modal.fastapi_endpoint(method="POST")
def run(payload: _HydrationRequest) -> dict[str, Any]:
    """Two-hop Blitz waterfall for one {uei, domain, linkedin_url?}.

    Hop 1 (skipped when PDL pre-resolved): domain → company_linkedin_url.
    Hop 2 (always): company_linkedin_url → firmographic payload.

    Returns:
      success — {"status": "completed", "uei", "domain", "linkedin_url",
                 "blitz_data"}
      Hop 1 fail — {"status": "failed", "uei", "domain",
                    "error": "domain_to_linkedin_resolution_failed"}
      Hop 2 fail — {"status": "failed", "uei", "domain", "linkedin_url",
                    "error": <Blitz HTTP / transport error>}

    Always HTTP 200 — the proxy demotes ledger status via the JSON `status`
    field (see apps/hq-x/app/routers/internal/gtm_pipeline.py modal branch).
    """
    import httpx

    uei = payload.entity_data.uei
    domain = payload.entity_data.domain
    pdl_linkedin_url = payload.entity_data.linkedin_url
    action = payload.action

    logger.info(
        "hydration_cascade_start",
        extra={
            "uei": uei,
            "domain": domain,
            "pdl_linkedin_url": pdl_linkedin_url,
            "needs_hop1_resolution": not bool(pdl_linkedin_url),
            "action": action,
        },
    )

    api_key = os.environ.get("BLITZAPI_API_KEY")
    if not api_key:
        return {
            "status": "failed",
            "uei": uei,
            "domain": domain,
            "error": "BLITZAPI_API_KEY not present in the blitz-api Modal secret",
        }

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # ── Hop 1: domain → company_linkedin_url ─────────────────────────────
    # Skipped entirely when PDL already supplied a LinkedIn URL upstream.
    target_url = pdl_linkedin_url or None
    if not target_url:
        try:
            with httpx.Client(timeout=_BLITZ_TIMEOUT_SECONDS) as client:
                resp = client.post(
                    _BLITZ_DOMAIN_TO_LINKEDIN,
                    json={"domain": domain},
                    headers=headers,
                )
            if resp.status_code // 100 != 2:
                logger.warning(
                    "hydration_cascade_hop1_http_error",
                    extra={
                        "uei": uei,
                        "domain": domain,
                        "status_code": resp.status_code,
                        "body": resp.text[:500],
                    },
                )
                return {
                    "status": "failed",
                    "uei": uei,
                    "domain": domain,
                    "error": "domain_to_linkedin_resolution_failed",
                }
            try:
                resolved = resp.json()
            except ValueError:
                resolved = {}
            # Blitz keys company-grain LinkedIn fields as `company_linkedin_url`
            # consistently — `linkedin_url` retained as a defensive fallback in
            # case a future Blitz response shape regresses.
            target_url = (
                (resolved.get("company_linkedin_url") if isinstance(resolved, dict) else None)
                or (resolved.get("linkedin_url") if isinstance(resolved, dict) else None)
                or None
            )
            if not target_url:
                logger.warning(
                    "hydration_cascade_hop1_empty_resolution",
                    extra={"uei": uei, "domain": domain, "resolved_body": resolved},
                )
                return {
                    "status": "failed",
                    "uei": uei,
                    "domain": domain,
                    "error": "domain_to_linkedin_resolution_failed",
                }
        except Exception:  # noqa: BLE001 — stub-grade; tighten once Blitz client errors are known
            logger.exception(
                "hydration_cascade_hop1_exception",
                extra={"uei": uei, "domain": domain},
            )
            return {
                "status": "failed",
                "uei": uei,
                "domain": domain,
                "error": "domain_to_linkedin_resolution_failed",
            }

    # ── Hop 2: company_linkedin_url → firmograph ─────────────────────────
    # Strict Blitz key: `company_linkedin_url` (NOT `linkedin_url`).
    try:
        with httpx.Client(timeout=_BLITZ_TIMEOUT_SECONDS) as client:
            resp = client.post(
                _BLITZ_COMPANY_ENRICHMENT,
                json={"company_linkedin_url": target_url},
                headers=headers,
            )
        if resp.status_code // 100 != 2:
            return {
                "status": "failed",
                "uei": uei,
                "domain": domain,
                "linkedin_url": target_url,
                "error": (
                    f"Blitz HTTP {resp.status_code} at {_BLITZ_COMPANY_ENRICHMENT}: "
                    f"{resp.text[:500]}"
                ),
            }
        try:
            blitz_data = resp.json()
        except ValueError:
            blitz_data = {"raw_response": resp.text}
    except Exception as exc:  # noqa: BLE001 — stub-grade; tighten once Blitz client errors are known
        logger.exception(
            "hydration_cascade_hop2_exception",
            extra={"uei": uei, "domain": domain, "linkedin_url": target_url},
        )
        return {
            "status": "failed",
            "uei": uei,
            "domain": domain,
            "linkedin_url": target_url,
            "error": str(exc),
        }

    return {
        "status": "completed",
        "uei": uei,
        "domain": domain,
        "linkedin_url": target_url,
        "blitz_data": blitz_data,
    }
