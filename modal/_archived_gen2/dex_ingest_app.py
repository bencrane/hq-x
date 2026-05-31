"""dex-ingest — Clay → Postgres ingest endpoints hosted on Modal.

Clay's outbound webhook has a strict client-side timeout. FastAPI-on-Railway
cold starts + serial Supabase writes have been blowing past it. This Modal
app mirrors modal/dex_modal_app.py's ASGI-FastAPI pattern so Clay gets per-request
horizontal scale-out with one always-warm container.

Logic is unchanged: we import the same ingest_* service functions the
FastAPI routes call today and write to the same Supabase database via the
same DATABASE_URL / service-role key.

Secrets (DEX_DB_URL_POOLED, DEX_SUPABASE_URL, DEX_SUPABASE_SERVICE_ROLE_KEY,
SUPER_ADMIN_JWT_SECRET, DEX_BEARER_TOKEN_VALUE) come from the
data-engine-x-api Doppler project at deploy time — do NOT create a
standing Modal Secret. Deploy with:

    doppler run -- modal deploy modal/dex_ingest_app.py

This matches the modal/fmcsa_ingest_app.py convention and keeps Doppler as
the single source of truth for secrets.

See docs/EXECUTOR_DIRECTIVE_CLAY_INGEST_MODAL_MIGRATION.md for full context.
"""

from __future__ import annotations

import os
from typing import Any

import modal
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

app = modal.App("dex-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("../data-engine-x/app", remote_path="/root/app")
)

auth_scheme = HTTPBearer(auto_error=False)


def require_bearer_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
) -> None:
    expected_key = os.environ.get("DEX_BEARER_TOKEN_VALUE")
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DEX_BEARER_TOKEN_VALUE is not configured",
        )
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or credentials.credentials != expected_key
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


# ---- Request models ------------------------------------------------------
# Copied verbatim from the FastAPI routers to preserve Clay's request contract.
# Sources:
#   app/routers/entities_v1.py         (ClayCompanyIngestRequest, ClayPersonIngestRequest)
#   app/routers/target_companies_v1.py (TargetCompanyIngestRequest)
#   app/routers/target_people_v1.py    (TargetPersonEmailInline, TargetPersonIngestRequest,
#                                       TargetPeopleEmailIngestRequest)


class ClayCompanyIngestRequest(BaseModel):
    source_table: str | None = Field(default=None, max_length=2048)
    payload: dict[str, Any]


class ClayPersonIngestRequest(BaseModel):
    source_table: str | None = Field(default=None, max_length=2048)
    payload: dict[str, Any]


class TargetCompanyIngestRequest(BaseModel):
    company_name: str | None = None
    domain: str | None = None
    linkedin_url: str | None = None
    vertical: str | None = None
    source: str | None = None


class TargetPersonEmailInline(BaseModel):
    email: str
    email_type: str | None = None
    source: str | None = None


class TargetPersonIngestRequest(BaseModel):
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    person_linkedin_url: str | None = None
    business_concept: str | None = None
    source: str | None = None
    emails: list[TargetPersonEmailInline] | None = None


class TargetPeopleEmailIngestRequest(BaseModel):
    target_person_id: str
    email: str
    email_type: str | None = None
    source: str | None = None


web_app = FastAPI(
    title="dex-ingest",
    dependencies=[Depends(require_bearer_auth)],
)


@web_app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Match the FastAPI app's error envelope: {"error": {"message": "..."}}.
    # Normalizes validation errors, auth failures, and ValueError into one
    # consistent shape for Clay.
    detail = exc.detail
    if isinstance(detail, dict) and "message" in detail:
        return JSONResponse(status_code=exc.status_code, content={"error": detail})
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": str(detail)}},
    )


@web_app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# ---- Clay-find source-table ingests --------------------------------------


@web_app.post("/clay-find-company")
async def ingest_clay_find_company(payload: ClayCompanyIngestRequest) -> dict[str, Any]:
    from app.services.clay_find_companies import ingest_clay_company

    try:
        result = ingest_clay_company(
            source_table=payload.source_table,
            payload=payload.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)})
    return {"data": result}


@web_app.post("/clay-find-person")
async def ingest_clay_find_person(payload: ClayPersonIngestRequest) -> dict[str, Any]:
    from app.services.clay_find_people import ingest_clay_person

    try:
        result = ingest_clay_person(
            source_table=payload.source_table,
            payload=payload.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)})
    return {"data": result}


@web_app.post("/clay-enrich-company")
async def ingest_clay_enrich_company_route(
    payload: ClayCompanyIngestRequest,
) -> dict[str, Any]:
    from app.services.clay_enrich_companies import ingest_clay_enrich_company

    try:
        result = ingest_clay_enrich_company(
            source_table=payload.source_table,
            payload=payload.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)})
    return {"data": result}


# ---- Target (canonical) ingests ------------------------------------------


@web_app.post("/target-company")
async def ingest_target_company(payload: TargetCompanyIngestRequest) -> dict[str, Any]:
    from app.services.target_companies import ingest_target_companies

    try:
        result = ingest_target_companies([payload.model_dump()])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)})
    return {"data": result}


@web_app.post("/target-person")
async def ingest_target_person(payload: TargetPersonIngestRequest) -> dict[str, Any]:
    from app.services.target_people import ingest_target_people

    try:
        result = ingest_target_people([payload.model_dump()])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)})
    return {"data": result}


@web_app.post("/target-person-email")
async def ingest_target_person_email(payload: TargetPeopleEmailIngestRequest) -> dict[str, Any]:
    from app.services.target_people import ingest_target_people_emails

    try:
        result = ingest_target_people_emails([payload.model_dump()])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)})
    return {"data": result}


# Secrets are injected from Doppler at `modal deploy` time. Always run
# `doppler run -- modal deploy modal/dex_ingest_app.py` so the
# data-engine-x-api Doppler project pushes the current values into a
# deploy-scoped Modal Secret. Matches the FMCSA pattern — one source of truth.
# Missing any env var fails the deploy with a KeyError, which is what we want.
#
# label="dex-ingest" pins the URL to
#   https://<workspace>--dex-ingest.modal.run
# independent of the Python function name.
# retry-policy: no-retry
@app.function(
    image=image,
    secrets=[
        modal.Secret.from_dict(
            {
                "DEX_DB_URL_POOLED": os.environ["DEX_DB_URL_POOLED"],
                "DEX_SUPABASE_URL": os.environ["DEX_SUPABASE_URL"],
                "DEX_SUPABASE_SERVICE_ROLE_KEY": os.environ["DEX_SUPABASE_SERVICE_ROLE_KEY"],
                # Required by app.config.Settings (imported transitively by the
                # ingest service functions). Not used for ingest auth — the
                # Clay bearer is DEX_BEARER_TOKEN_VALUE below.
                "SUPER_ADMIN_JWT_SECRET": os.environ["SUPER_ADMIN_JWT_SECRET"],
                "DEX_BEARER_TOKEN_VALUE": os.environ["DEX_BEARER_TOKEN_VALUE"],
            }
        )
    ],
    timeout=60 * 5,
    min_containers=1,
)
@modal.asgi_app(label="dex-ingest")
def fastapi_app() -> FastAPI:
    return web_app
