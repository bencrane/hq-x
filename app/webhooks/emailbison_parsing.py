import hashlib
from typing import Any


def extract_event_type(payload: dict[str, Any]) -> str | None:
    return payload.get("event") or payload.get("event_type") or payload.get("type")


def compute_event_key(payload: dict[str, Any], raw_body: bytes) -> str:
    explicit = payload.get("event_id") or payload.get("id")
    if explicit is not None:
        return str(explicit)
    return hashlib.sha256(raw_body).hexdigest()
