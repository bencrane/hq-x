"""Read + validation helpers for direct_mail_specs.

Specs are immutable lookup data, seeded by migrations 0016/0017. Frontends
and managed agents (via MCP) read from this module to:

  * pick the right (mailer_category, variant) for a campaign,
  * fetch the bleed/trim/safe geometry to draw artboard guides,
  * pre-flight artwork against a spec before paying Lob to print it.

Schema is defined in migrations/0016_lob_mailer_specs.sql; data origin is
documented in scripts/sync_lob_specs.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import get_db_connection

CATEGORY_VALUES: tuple[str, ...] = (
    "postcard",
    "letter",
    "self_mailer",
    "snap_pack",
    "booklet",
    "check",
    "card_affix",
    "buckslip",
    "letter_envelope",
)


@dataclass
class MailerSpec:
    id: str
    mailer_category: str
    variant: str
    label: str
    bleed_w_in: float | None
    bleed_h_in: float | None
    trim_w_in: float
    trim_h_in: float
    safe_inset_in: float | None
    zones: dict[str, Any]
    folding: dict[str, Any] | None
    pagination: dict[str, Any] | None
    address_placement: dict[str, Any] | None
    envelope: dict[str, Any] | None
    production: dict[str, Any]
    ordering: dict[str, Any]
    template_pdf_url: str | None
    additional_template_urls: list[str]
    source_urls: list[str]
    notes: str | None


_SPEC_COLUMNS = (
    "id, mailer_category, variant, label, "
    "bleed_w_in, bleed_h_in, trim_w_in, trim_h_in, safe_inset_in, "
    "zones, folding, pagination, address_placement, envelope, "
    "production, ordering, "
    "template_pdf_url, additional_template_urls, source_urls, notes"
)


def _row_to_spec(row: tuple) -> MailerSpec:
    return MailerSpec(
        id=str(row[0]),
        mailer_category=row[1],
        variant=row[2],
        label=row[3],
        bleed_w_in=float(row[4]) if row[4] is not None else None,
        bleed_h_in=float(row[5]) if row[5] is not None else None,
        trim_w_in=float(row[6]),
        trim_h_in=float(row[7]),
        safe_inset_in=float(row[8]) if row[8] is not None else None,
        zones=row[9] or {},
        folding=row[10],
        pagination=row[11],
        address_placement=row[12],
        envelope=row[13],
        production=row[14] or {},
        ordering=row[15] or {},
        template_pdf_url=row[16],
        additional_template_urls=row[17] or [],
        source_urls=row[18] or [],
        notes=row[19],
    )


async def list_specs(category: str | None = None) -> list[MailerSpec]:
    """Return all specs, optionally filtered by category. Sorted by
    (mailer_category, variant) so frontends can render stable lists."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if category is not None:
                await cur.execute(
                    f"SELECT {_SPEC_COLUMNS} FROM direct_mail_specs "
                    "WHERE mailer_category = %s "
                    "ORDER BY mailer_category, variant",
                    (category,),
                )
            else:
                await cur.execute(
                    f"SELECT {_SPEC_COLUMNS} FROM direct_mail_specs "
                    "ORDER BY mailer_category, variant"
                )
            rows = await cur.fetchall()
    return [_row_to_spec(r) for r in rows]


async def get_spec(category: str, variant: str) -> MailerSpec | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_SPEC_COLUMNS} FROM direct_mail_specs "
                "WHERE mailer_category = %s AND variant = %s",
                (category, variant),
            )
            row = await cur.fetchone()
    return _row_to_spec(row) if row else None


async def list_categories() -> list[dict[str, Any]]:
    """[{category, variant_count, variants[]}] — drives navigation."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT mailer_category, "
                "       COUNT(*) AS n, "
                "       ARRAY_AGG(variant ORDER BY variant) AS variants "
                "FROM direct_mail_specs "
                "GROUP BY mailer_category "
                "ORDER BY mailer_category"
            )
            rows = await cur.fetchall()
    return [{"category": r[0], "variant_count": r[1], "variants": list(r[2])} for r in rows]


async def list_design_rules() -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT key, value, description, source_url, updated_at "
                "FROM direct_mail_design_rules ORDER BY key"
            )
            rows = await cur.fetchall()
    return [
        {
            "key": r[0],
            "value": r[1],
            "description": r[2],
            "source_url": r[3],
            "updated_at": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Pre-flight validator
#
# Given an artwork's reported dimensions + DPI, return a structured pass/fail
# report against a spec. This is *dimension-level* validation only — it does
# NOT inspect actual pixel content (e.g. ink coverage in the ink-free zone).
# That is a future PDF-parsing job; this endpoint is the cheap pre-flight
# every renderer / agent should call before paying Lob.
#
# Severities:
#   "error"    — hard failure; submission to Lob will likely be rejected
#   "warning"  — usable but suboptimal (e.g. orientation matches but DPI low)
#   "info"     — passing check, surfaced for transparency
# ---------------------------------------------------------------------------


@dataclass
class ValidationCheck:
    code: str
    severity: str  # "error" | "warning" | "info"
    message: str
    expected: Any = None
    actual: Any = None


@dataclass
class ValidationReport:
    spec_id: str
    mailer_category: str
    variant: str
    is_valid: bool
    error_count: int
    warning_count: int
    checks: list[ValidationCheck]


# ±0.02" tolerance covers PDF rounding (Adobe / poppler often round to 0.001")
# without flagging legitimate 1/16" / 1/8" measurements as off-spec.
_DIM_TOLERANCE_IN = 0.02


def _dims_match(a: float, b: float, tol: float = _DIM_TOLERANCE_IN) -> bool:
    return abs(a - b) <= tol


def validate_artwork_dimensions(
    spec: MailerSpec,
    *,
    width_in: float,
    height_in: float,
    dpi: int | None = None,
    panel: str | None = None,
) -> ValidationReport:
    """Validate a single rendered panel against `spec`.

    The renderer / agent passes the panel size in inches (the rectangle the
    artwork actually occupies on disk) plus optional DPI. We don't try to
    enforce panel-of-origin (FRONT vs BACK) here — for postcards both panels
    share the same bleed/trim/safe geometry, so a single check suffices.

    For self-mailers, callers should pass the *flat unfolded* panel size
    (matches `bleed_*_in`); validator does not currently distinguish
    OUTSIDE vs INSIDE artwork.
    """
    checks: list[ValidationCheck] = []

    # 1. Dimension match: prefer bleed if the format has one, else trim.
    expected_w, expected_h, expected_label = (
        (spec.bleed_w_in, spec.bleed_h_in, "bleed")
        if spec.bleed_w_in and spec.bleed_h_in
        else (spec.trim_w_in, spec.trim_h_in, "trim")
    )
    # Allow either orientation — Lob templates ship both portrait and landscape.
    a, b = sorted([width_in, height_in], reverse=True)
    e_a, e_b = sorted([expected_w, expected_h], reverse=True)
    if _dims_match(a, e_a) and _dims_match(b, e_b):
        checks.append(
            ValidationCheck(
                code="dimensions_match",
                severity="info",
                message=f"Artwork matches {expected_label} dimensions",
                expected={"w_in": expected_w, "h_in": expected_h, "anchor": expected_label},
                actual={"w_in": width_in, "h_in": height_in},
            )
        )
    else:
        checks.append(
            ValidationCheck(
                code="dimensions_mismatch",
                severity="error",
                message=(
                    f"Artwork is {width_in}x{height_in}\" but spec requires "
                    f"{expected_w}x{expected_h}\" ({expected_label})"
                ),
                expected={"w_in": expected_w, "h_in": expected_h, "anchor": expected_label},
                actual={"w_in": width_in, "h_in": height_in},
            )
        )

    # 2. DPI guard. Default required is 300 (Lob's universal rule, mirrored
    # into production.required_dpi where the help-center page calls it out).
    required_dpi = spec.production.get("required_dpi", 300)
    if dpi is not None:
        if dpi >= required_dpi:
            checks.append(
                ValidationCheck(
                    code="dpi_ok",
                    severity="info",
                    message=f"DPI {dpi} meets spec ({required_dpi})",
                    expected=required_dpi,
                    actual=dpi,
                )
            )
        elif dpi >= 240:
            checks.append(
                ValidationCheck(
                    code="dpi_low",
                    severity="warning",
                    message=f"DPI {dpi} below recommended {required_dpi}",
                    expected=required_dpi,
                    actual=dpi,
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    code="dpi_too_low",
                    severity="error",
                    message=f"DPI {dpi} too low; print quality will be unacceptable (need {required_dpi})",
                    expected=required_dpi,
                    actual=dpi,
                )
            )

    # 3. Panel awareness. We surface relevant zones for the requested panel
    # so the caller can render an overlay.
    if panel:
        relevant_zones = _zones_for_panel(spec.zones, panel)
        if relevant_zones:
            checks.append(
                ValidationCheck(
                    code="zones_for_panel",
                    severity="info",
                    message=f"{len(relevant_zones)} zone(s) apply to panel '{panel}'",
                    actual={"panel": panel, "zones": relevant_zones},
                )
            )

    error_count = sum(1 for c in checks if c.severity == "error")
    warning_count = sum(1 for c in checks if c.severity == "warning")
    return ValidationReport(
        spec_id=spec.id,
        mailer_category=spec.mailer_category,
        variant=spec.variant,
        is_valid=error_count == 0,
        error_count=error_count,
        warning_count=warning_count,
        checks=checks,
    )


def _zones_for_panel(zones: dict[str, Any], panel: str) -> list[dict[str, Any]]:
    """Return zones whose 'panel' field matches the requested panel name.
    Zones with no panel field apply universally and are also returned."""
    panel_norm = panel.lower().strip()
    out = []
    for name, z in zones.items():
        if not isinstance(z, dict):
            continue
        z_panel = z.get("panel")
        if z_panel is None or panel_norm in str(z_panel).lower():
            out.append({"zone": name, **z})
    return out
