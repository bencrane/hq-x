import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request

from app.auth.trigger_secret import verify_trigger_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["internal"])


@router.post("/tick", dependencies=[Depends(verify_trigger_secret)])
async def scheduler_tick(request: Request) -> dict[str, str]:
    raw = await request.body()
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = {"_malformed_json": True}
    received_at = datetime.now(UTC).isoformat()
    logger.info("scheduler tick received", extra={"payload": payload, "received_at": received_at})
    return {"status": "ok", "received_at": received_at}
