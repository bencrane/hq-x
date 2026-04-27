import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.webhooks import storage
from app.webhooks.cal_parsing import extract_cal_fields
from app.webhooks.cal_signature import verify_cal_signature

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/cal")
async def receive_cal_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()

    signature = request.headers.get("X-Cal-Signature-256")
    if not verify_cal_signature(raw_body, signature):
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    if not isinstance(payload, dict):
        payload = {"raw_payload": payload}

    fields = extract_cal_fields(payload)
    event_id = await storage.insert_cal_raw_event(fields=fields, payload=payload)

    logger.info(
        "cal_webhook stored trigger_event=%s uid=%s id=%s",
        fields["trigger_event"],
        fields["cal_event_uid"],
        event_id,
    )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "trigger_event": fields["trigger_event"],
            "event_id": str(event_id),
        },
    )
