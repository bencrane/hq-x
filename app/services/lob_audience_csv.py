"""Build the Lob audience CSV for a direct-mail step.

Lob's ``POST /v1/uploads`` accepts a CSV that maps to required
address columns (name + line1/city/state/zip), optional columns
(line2/company/country), and merge variables (used to populate
per-recipient values in the creative — most importantly the QR code's
``qr_code_redirect_url``).

This module is the pure builder: take a list of ``AudienceRow`` dicts
(one per step membership, already joined to recipient and Dub link),
emit deterministic CSV bytes. The Lob adapter calls
``LOB_REQUIRED_COLUMN_MAPPING`` / ``LOB_OPTIONAL_COLUMN_MAPPING`` /
``LOB_MERGE_VARIABLE_COLUMN_MAPPING`` when creating the upload row so
Lob knows which CSV column maps to which Lob field.

Out of scope: querying memberships / recipients / Dub links. That sits
in the adapter where it has DB access. This module is import-light and
DB-free so it stays trivially testable.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

# CSV column headers — order matters because the rows are written in
# this order. The Lob upload row's column-mapping objects reference
# these names directly.
_HEADER = (
    "recipient_name",
    "primary_line",
    "secondary_line",
    "city",
    "state",
    "zip_code",
    "country",
    "qr_code_redirect_url",
)

# Mapping objects passed to Lob's ``POST /v1/uploads``. The CSV column
# name (right-hand side) maps to the Lob field (left-hand side).
LOB_REQUIRED_COLUMN_MAPPING: dict[str, str] = {
    "name": "recipient_name",
    "address_line1": "primary_line",
    "address_city": "city",
    "address_state": "state",
    "address_zip": "zip_code",
}
LOB_OPTIONAL_COLUMN_MAPPING: dict[str, str] = {
    "address_line2": "secondary_line",
    "address_country": "country",
}
LOB_MERGE_VARIABLE_COLUMN_MAPPING: dict[str, str] = {
    "qr_code_redirect_url": "qr_code_redirect_url",
}


@dataclass(frozen=True)
class AudienceRow:
    """One CSV row. All fields are pre-stringified — None becomes "".

    Address values are taken from ``business.recipients.mailing_address``
    (a JSONB object); the adapter normalizes it before constructing this
    row.
    """

    recipient_name: str
    primary_line: str
    secondary_line: str | None
    city: str
    state: str
    zip_code: str
    country: str | None
    qr_code_redirect_url: str | None


class AudienceRowInvalid(ValueError):
    """Raised when an AudienceRow is missing a Lob-required field. The
    adapter pre-validates on ingest so the upload doesn't fail server-side
    with a per-row reject.
    """


def validate_row(row: AudienceRow) -> None:
    """Reject rows that would fail Lob's address validation outright.

    Lob's per-row validation runs server-side after the file upload; we
    pre-check the mandatory address fields so a missing line1/city/state/
    zip surfaces as a local error before we hit the network.
    """
    missing = [
        name
        for name in ("recipient_name", "primary_line", "city", "state", "zip_code")
        if not (getattr(row, name) or "").strip()
    ]
    if missing:
        raise AudienceRowInvalid(
            f"audience row missing required field(s): {', '.join(missing)}"
        )


def build_audience_csv(rows: list[AudienceRow]) -> bytes:
    """Serialize the audience rows to CSV bytes.

    The output is deterministic given identical input ordering — useful
    for unit tests and for hashing into idempotency keys if we ever need
    to.
    """
    if not rows:
        raise ValueError("audience rows must be non-empty")
    for row in rows:
        validate_row(row)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_HEADER)
    for row in rows:
        writer.writerow(
            [
                row.recipient_name,
                row.primary_line,
                row.secondary_line or "",
                row.city,
                row.state,
                row.zip_code,
                row.country or "",
                row.qr_code_redirect_url or "",
            ]
        )
    return buf.getvalue().encode("utf-8")


__all__ = [
    "LOB_MERGE_VARIABLE_COLUMN_MAPPING",
    "LOB_OPTIONAL_COLUMN_MAPPING",
    "LOB_REQUIRED_COLUMN_MAPPING",
    "AudienceRow",
    "AudienceRowInvalid",
    "build_audience_csv",
    "validate_row",
]
