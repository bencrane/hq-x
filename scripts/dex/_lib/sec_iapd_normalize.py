"""Minimal IAPD normalization helpers — Phase 1 only.

Stage 0 reconnaissance (2026-05-10) confirmed adviserinfo.sec.gov uses raw
integer CRDs in URLs (no zero-padding). The hook stays in case Phase 2's
deeper parsing surfaces normalization needs.
"""
from __future__ import annotations


def normalize_crd(crd_int: int | str) -> str:
    """Render a CRD as the integer string IAPD expects in URL paths."""
    return str(int(crd_int))
