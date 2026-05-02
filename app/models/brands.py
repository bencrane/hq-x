"""Pydantic models for brand-level configuration.

The router-side `BrandResponse` lives in `app/routers/brands.py`; this
module carries the structured *content* models that are persisted into
JSONB columns on `business.brands`:

  * `BrandTheme` — visual theme (logo, palette, font, optional custom CSS)
    rendered into landing pages and any other brand-aware surface.

Validation lives at the API boundary so a malformed PATCH body fails
with 422 before touching the DB. The DB layer keeps the column
permissive (raw JSONB) so future fields don't require a migration.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# 6-digit hex color (uppercase or lowercase). 3-digit shorthand and named
# colors aren't allowed — we want exactly one canonical representation.
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# 10 KB cap on custom CSS. Larger payloads almost certainly indicate the
# customer is pasting a full stylesheet rather than overriding theme
# tokens; nudge them toward variables.
_CUSTOM_CSS_MAX_BYTES = 10 * 1024

# Recognized font families. Open-ended (operator can extend at any time
# by editing this list) but constrained enough to prevent typos and
# arbitrary user-agent fonts. The render path falls back to a sensible
# system stack if the value isn't a known web-safe font.
_KNOWN_FONT_FAMILIES = {
    "Inter",
    "Roboto",
    "Open Sans",
    "Lato",
    "Montserrat",
    "Poppins",
    "Source Sans Pro",
    "Helvetica",
    "Arial",
    "Georgia",
    "Times New Roman",
    "Courier New",
    "system-ui",
}


class BrandTheme(BaseModel):
    """Visual theme rendered into hosted landing pages and other surfaces."""

    logo_url: str | None = Field(default=None, max_length=2048)
    primary_color: str | None = None
    secondary_color: str | None = None
    background_color: str | None = None
    text_color: str | None = None
    font_family: str | None = Field(default=None, max_length=100)
    custom_css: str | None = None

    model_config = {"extra": "forbid"}

    @field_validator("logo_url")
    @classmethod
    def _logo_must_be_https(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("https://"):
            raise ValueError("logo_url must use https:// scheme")
        return v

    @field_validator(
        "primary_color", "secondary_color", "background_color", "text_color"
    )
    @classmethod
    def _color_must_be_hex(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _HEX_COLOR_RE.match(v):
            raise ValueError("color must be a 6-digit hex like #1A2B3C")
        return v

    @field_validator("custom_css")
    @classmethod
    def _custom_css_size(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v.encode("utf-8")) > _CUSTOM_CSS_MAX_BYTES:
            raise ValueError(
                f"custom_css exceeds {_CUSTOM_CSS_MAX_BYTES} bytes; "
                "use theme tokens for large styling"
            )
        return v

    @field_validator("font_family")
    @classmethod
    def _font_family_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in _KNOWN_FONT_FAMILIES:
            # Allow but warn-via-error-message-style: the render path will
            # fall through to system-ui anyway, but we reject here so the
            # operator notices a typo rather than silently losing the font.
            raise ValueError(
                f"font_family '{v}' is not in the known list "
                f"({sorted(_KNOWN_FONT_FAMILIES)})"
            )
        return v


__all__ = ["BrandTheme"]
