"""Deterministic idempotency-key derivation for Lob piece creates.

The Lob client's internal `build_idempotency_material` is a *formatter* — it
takes an already-decided key and selects header-vs-query placement. This
module is the *deriver* the router calls when the caller leaves
`idempotency_key` unset: it hashes a stable subset of the create payload
(piece type, recipient, content/template id, custom dedup-tag) into a
deterministic key.

Excluded from the hash by design (Lob recommends excluding mutable fields):
description, metadata, merge_variables, send_date, mail_type, billing_group_id,
use_type, color, double_sided, address_placement, return_envelope.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_DERIVE_VERSION = "v1"


def _normalize_recipient(value: Any) -> Any:
    """Lob's `to` field is either a string (saved-address ID) or a dict
    (inline address). For dicts, lowercase + strip whitespace on the
    deliverability-relevant fields so callers who supply slightly different
    capitalisation collapse to the same key.
    """
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return value
    relevant = (
        "address_line1",
        "address_line2",
        "address_city",
        "address_state",
        "address_zip",
        "address_country",
        "name",
        "company",
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
    # Content / template selection
    for key in (
        "front",
        "back",
        "inside",
        "outside",
        "file",
        "template_id",
        "html",
        "pdf",
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
    """Return a stable Lob-compatible idempotency key for this create.

    Same payload → same key, across processes and restarts. Two creates that
    differ only in mutable metadata (description, send_date, merge_variables)
    collide deliberately — Lob will deduplicate them.
    """
    subset = _stable_subset(piece_type, payload)
    canonical = json.dumps(subset, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"hqx-{_DERIVE_VERSION}-{piece_type}-{digest[:40]}"
