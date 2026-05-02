"""Deterministic idempotency-key derivation for PostGrid piece creates.

Mirrors app/providers/lob/idempotency.py in structure. PostGrid honors an
Idempotency-Key request header on POST endpoints with the same semantics:
same-key + same-body = previously-created resource returned.

PostGrid uses verbose ID prefixes (letter_*, postcard_*, selfmailer_*,
contact_*, template_*, webhook_*). The idempotency key derivation is
provider-neutral (just hashes a stable payload subset), so the prefix
differs but the logic is identical to Lob's derivation.

Excluded from the hash by design (mutable fields that should not
change dedup behavior):
description, metadata, mergeVariables, sendDate, mailingClass,
color, doubleSided, addressPlacement, extraService, returnEnvelope.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_DERIVE_VERSION = "v1"


def _normalize_recipient(value: Any) -> Any:
    """PostGrid's `to` / `from` field is either a contact id or an inline
    address dict. Lowercase + strip for inline dicts so minor capitalisation
    differences collapse to the same key.
    """
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return value
    relevant = (
        "firstName",
        "lastName",
        "companyName",
        "addressLine1",
        "addressLine2",
        "city",
        "provinceOrState",
        "postalOrZip",
        "country",
    )
    out: dict[str, Any] = {}
    for key in relevant:
        raw = value.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            out[key] = raw.strip().lower()
        else:
            out[key] = raw
    return out


def _stable_subset(piece_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    subset: dict[str, Any] = {"piece_type": piece_type}
    if "to" in payload:
        subset["to"] = _normalize_recipient(payload["to"])
    if "from" in payload:
        subset["from"] = _normalize_recipient(payload["from"])
    # Content / template selection — PostGrid field names differ from Lob
    for key in (
        "frontHTML",
        "backHTML",
        "frontTemplate",
        "backTemplate",
        "frontPDFURL",
        "backPDFURL",
        "html",
        "pdf",
        "pdfURL",
        "template",
        "insideHTML",
        "outsideHTML",
        "insideTemplate",
        "outsideTemplate",
        "insidePDFURL",
        "outsidePDFURL",
    ):
        if key in payload:
            value = payload[key]
            if isinstance(value, str):
                subset[key] = value.strip()
            else:
                subset[key] = value
    # Caller-supplied dedup tag wins if present
    if "dedup_tag" in payload:
        subset["dedup_tag"] = payload["dedup_tag"]
    return subset


def derive_idempotency_key(*, piece_type: str, payload: dict[str, Any]) -> str:
    """Return a stable PostGrid-compatible idempotency key for this create.

    Same payload → same key, across processes and restarts. Two creates
    that differ only in mutable fields (description, sendDate, mergeVariables)
    collide deliberately — PostGrid will deduplicate them.
    """
    subset = _stable_subset(piece_type, payload)
    canonical = json.dumps(subset, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"hqx-pg-{_DERIVE_VERSION}-{piece_type}-{digest[:40]}"
