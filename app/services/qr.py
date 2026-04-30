"""QR rendering via segno.

Used at step-launch time to render one QR per recipient encoding the
recipient's Dub short link. Pure render — DB lookup of the link happens at
the call site.

We intentionally wrap segno rather than letting callers import it directly
so swapping libraries later is a one-file change.
"""

from __future__ import annotations

import io
from typing import Literal

import segno


QrFormat = Literal["png", "svg"]
QrErrorCorrection = Literal["L", "M", "Q", "H"]


def render_qr(
    url: str,
    *,
    fmt: QrFormat = "png",
    size_px: int = 600,
    border_modules: int = 2,
    ec_level: QrErrorCorrection = "M",
    dark: str = "#000000",
    light: str = "#ffffff",
) -> bytes:
    """Render `url` as a QR image and return raw bytes.

    `size_px` is target output size. For PNG we compute `scale` to land
    near that size; for SVG `size_px` is honoured indirectly via scale=1
    (consumer is expected to re-size via viewBox). `border_modules` is
    the quiet-zone in module units (2 is below the spec minimum of 4 —
    only use for tightly-bounded direct-mail zones).

    Raises `ValueError` for empty/whitespace `url`. Does not validate the
    URL beyond that — short-link generation is the caller's concern.
    """
    if not url or not url.strip():
        raise ValueError("render_qr requires a non-empty url")

    qr = segno.make(url, error=ec_level.lower())
    buf = io.BytesIO()
    if fmt == "png":
        modules = qr.symbol_size(border=border_modules)[0]
        scale = max(1, round(size_px / modules))
        qr.save(
            buf,
            kind="png",
            scale=scale,
            border=border_modules,
            dark=dark,
            light=light,
        )
    elif fmt == "svg":
        qr.save(
            buf,
            kind="svg",
            scale=1,
            border=border_modules,
            dark=dark,
            light=light,
            xmldecl=False,
        )
    else:
        raise ValueError(f"unsupported fmt: {fmt}")

    return buf.getvalue()


__all__ = ["render_qr", "QrFormat", "QrErrorCorrection"]
