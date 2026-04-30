"""Tests for app.services.qr — pure render, no I/O."""

from __future__ import annotations

import pytest

from app.services.qr import render_qr


def test_renders_png_bytes():
    out = render_qr("https://example.com/abc", fmt="png")
    assert isinstance(out, bytes)
    assert len(out) > 0
    # PNG magic number
    assert out.startswith(b"\x89PNG\r\n\x1a\n")


def test_renders_svg_bytes():
    out = render_qr("https://example.com/abc", fmt="svg")
    assert isinstance(out, bytes)
    assert b"<svg" in out


def test_empty_url_raises():
    with pytest.raises(ValueError):
        render_qr("")
    with pytest.raises(ValueError):
        render_qr("   ")


def test_unsupported_format_raises():
    with pytest.raises(ValueError):
        render_qr("https://example.com", fmt="jpg")  # type: ignore[arg-type]


def test_size_px_scales_png():
    small = render_qr("https://example.com/x", fmt="png", size_px=200)
    large = render_qr("https://example.com/x", fmt="png", size_px=800)
    assert len(large) > len(small)
