#!/usr/bin/env python3
"""
Verify data/lob_mailer_specs.json against Lob's published template PDFs and
sanity-check the v1 zone catalog the DMaaS DSL consumes.

Run with:

    uv run python -m scripts.sync_lob_specs

What this script checks
-----------------------

1. PDF MediaBox vs spec bleed dims (the original drift check). For every spec
   row that points to a `template_pdf_url`, downloads the PDF, reads MediaBox
   via `pdfinfo -box`, compares against the spec's bleed (or trim, if no
   bleed) dimensions in inches. Tolerance is ±0.01" to absorb Adobe rounding.

2. v1 zone catalog (NEW). For every v1 spec (4 postcards + 3 self_mailer
   bifolds) — bypassing the DB by building MailerSpec from the JSON — runs
   the resolver's `bind_spec_zones` and asserts:

   * Every `*_safe` rectangle is fully inside its parent surface (postcard
     face or self_mailer panel).
   * For postcards: `address_block`, `postage_indicia`, `return_address`,
     `usps_barcode_clear` are mutually non-overlapping on the back face.
   * For self_mailers: `address_window`, `postage_indicia`,
     `usps_barcode_clear` are mutually non-overlapping on the outside cover
     panel.
   * `fold_gutter_<n>` zones straddle the documented fold positions.
   * `glue_zone_*` rectangles lie along the expected edges (opening edges
     for bifolds; the documented panel for trifold).

Exit code is non-zero if any check fails.

Requires: poppler-utils (`brew install poppler` on macOS) for `pdfinfo`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Make tests/conftest's dummy env available so app.config doesn't blow up at
# import time when this script is invoked outside Doppler.
import os
_CONF = Path(__file__).resolve().parent.parent / "tests" / "conftest.py"
if _CONF.exists():
    for line in _CONF.read_text().splitlines():
        if '": "' in line and '"' in line.split(":")[0]:
            try:
                k, v = [p.strip().strip(',').strip('"') for p in line.split('": "', 1)]
                os.environ.setdefault(k.lstrip('"'), v)
            except Exception:
                pass

from app.direct_mail.specs import MailerSpec  # noqa: E402
from app.dmaas.service import bind_spec_zones  # noqa: E402

SPEC_PATH = Path(__file__).resolve().parent.parent / "data" / "lob_mailer_specs.json"
PTS_PER_INCH = 72.0
TOLERANCE_IN = 0.01  # 0.01" ≈ 3 pts

V1_POSTCARD_VARIANTS = {"4x6", "5x7", "6x9", "6x11"}
V1_SELF_MAILER_VARIANTS = {"11x9_bifold", "12x9_bifold", "6x18_bifold"}


def _spec_from_json_row(s: dict) -> MailerSpec:
    """Build a MailerSpec from a JSON row — no DB. Mirrors specs.MailerSpec."""
    return MailerSpec(
        id=s.get("id", "json"),
        mailer_category=s["mailer_category"],
        variant=s["variant"],
        label=s["label"],
        bleed_w_in=s.get("bleed_w_in"),
        bleed_h_in=s.get("bleed_h_in"),
        trim_w_in=s["trim_w_in"],
        trim_h_in=s["trim_h_in"],
        safe_inset_in=s.get("safe_inset_in"),
        zones=s.get("zones") or {},
        folding=s.get("folding"),
        pagination=s.get("pagination"),
        address_placement=s.get("address_placement"),
        envelope=s.get("envelope"),
        production=s.get("production") or {},
        ordering=s.get("ordering") or {},
        template_pdf_url=s.get("template_pdf_url"),
        additional_template_urls=s.get("additional_template_urls") or [],
        source_urls=s.get("source_urls") or [],
        notes=s.get("notes"),
        faces=s.get("faces") or [],
    )


# ---------------------------------------------------------------------------
# 1. PDF MediaBox check
# ---------------------------------------------------------------------------


def media_box_inches(pdf_path: Path) -> tuple[float, float]:
    out = subprocess.check_output(["pdfinfo", "-box", str(pdf_path)], text=True)
    for line in out.splitlines():
        if line.startswith("MediaBox:"):
            _, x0, y0, x1, y1 = line.split()
            w_pts = float(x1) - float(x0)
            h_pts = float(y1) - float(y0)
            return round(w_pts / PTS_PER_INCH, 4), round(h_pts / PTS_PER_INCH, 4)
    raise RuntimeError(f"no MediaBox in {pdf_path}")


def expected_inches(spec: dict) -> tuple[float, float]:
    if spec.get("bleed_w_in") and spec.get("bleed_h_in"):
        return tuple(sorted([spec["bleed_w_in"], spec["bleed_h_in"]], reverse=True))
    return tuple(sorted([spec["trim_w_in"], spec["trim_h_in"]], reverse=True))


def check_pdf_mediaboxes(rows: list[dict]) -> list[tuple]:
    failures = []
    checked = 0
    for spec in rows:
        url = spec.get("template_pdf_url")
        if not url:
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                urllib.request.urlretrieve(url, tmp.name)
                actual = media_box_inches(Path(tmp.name))
        except Exception as e:
            print(f"SKIP  {spec['mailer_category']:18s} {spec['variant']:25s} (fetch failed: {e})")
            continue
        actual_sorted = tuple(sorted(actual, reverse=True))
        expected = expected_inches(spec)
        ok = (
            abs(actual_sorted[0] - expected[0]) < TOLERANCE_IN
            and abs(actual_sorted[1] - expected[1]) < TOLERANCE_IN
        )
        marker = "OK " if ok else "FAIL"
        print(
            f"{marker}  {spec['mailer_category']:18s} {spec['variant']:25s} "
            f"PDF={actual[0]:.3f}x{actual[1]:.3f}\"  expected={expected[0]:.3f}x{expected[1]:.3f}\""
        )
        checked += 1
        if not ok:
            failures.append(("mediabox", spec["mailer_category"], spec["variant"], actual, expected))
    print(f"\n[mediabox] Checked {checked} templates, {len(failures)} mismatch(es).")
    return failures


# ---------------------------------------------------------------------------
# 2. Zone catalog sanity checks
# ---------------------------------------------------------------------------


def _rect_inside(inner, outer, eps: float = 0.5) -> bool:
    return (
        inner.x >= outer.x - eps
        and inner.y >= outer.y - eps
        and inner.x + inner.w <= outer.x + outer.w + eps
        and inner.y + inner.h <= outer.y + outer.h + eps
    )


def _rects_overlap(a, b, eps: float = 0.5) -> bool:
    return not (
        a.x + a.w <= b.x + eps
        or b.x + b.w <= a.x + eps
        or a.y + a.h <= b.y + eps
        or b.y + b.h <= a.y + eps
    )


def check_zones_postcard(spec: MailerSpec) -> list[str]:
    """Return list of failure messages. Empty list = OK."""
    binding = bind_spec_zones(spec)
    fails: list[str] = []
    z = binding.zones

    # *_safe inside its parent face
    for face in ("front", "back"):
        face_rect = z.get(f"{face}_face")
        safe_rect = z.get(f"{face}_safe")
        if face_rect and safe_rect and not _rect_inside(safe_rect, face_rect):
            fails.append(f"{face}_safe not inside {face}_face")

    # Mutual non-overlap on back face
    names = ["back_address_block", "back_postage_indicia", "back_return_address", "back_usps_barcode_clear"]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = z.get(names[i]), z.get(names[j])
            if a is None or b is None:
                fails.append(f"missing zone: {names[i] if a is None else names[j]}")
                continue
            if _rects_overlap(a, b):
                fails.append(f"{names[i]} overlaps {names[j]}")
    return fails


def check_zones_self_mailer(spec: MailerSpec) -> list[str]:
    binding = bind_spec_zones(spec)
    fails: list[str] = []
    z = binding.zones
    folding = spec.folding or {}
    cover = folding.get("cover_panel")

    # Cover panel + cover-panel safe present
    for name in (cover, f"{cover}_safe"):
        if name not in z:
            fails.append(f"missing zone: {name}")

    # Mutual non-overlap on cover panel
    names = ["outside_address_window", "outside_postage_indicia", "outside_usps_barcode_clear"]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = z.get(names[i]), z.get(names[j])
            if a is None or b is None:
                fails.append(f"missing zone: {names[i] if a is None else names[j]}")
                continue
            if _rects_overlap(a, b):
                fails.append(f"{names[i]} overlaps {names[j]}")

    # Cover-panel safe inside cover panel
    cover_rect = z.get(cover) if cover else None
    safe_rect = z.get(f"{cover}_safe") if cover else None
    if cover_rect and safe_rect and not _rect_inside(safe_rect, cover_rect):
        fails.append(f"{cover}_safe not inside {cover}")

    # Fold gutter straddles fold line(s)
    fold_xs = folding.get("fold_lines_in_from_left") or []
    fold_ys = folding.get("fold_lines_in_from_top") or []
    bleed_w = spec.bleed_w_in or spec.trim_w_in
    bleed_h = spec.bleed_h_in or spec.trim_h_in
    bleed_margin_x = (bleed_w - spec.trim_w_in) / 2.0
    bleed_margin_y = (bleed_h - spec.trim_h_in) / 2.0
    dpi = int(spec.production.get("required_dpi", 300))
    for i, fx in enumerate(sorted(float(x) for x in fold_xs), start=1):
        gutter = z.get(f"fold_gutter_{i}")
        if gutter is None:
            fails.append(f"missing fold_gutter_{i}")
            continue
        # Gutter center should be at fx (in canvas inches) + bleed_margin_x.
        center_in = (gutter.x + gutter.w / 2) / dpi
        expected = bleed_margin_x + fx
        if abs(center_in - expected) > 0.01:
            fails.append(f"fold_gutter_{i} center {center_in:.3f}\" != fold {expected:.3f}\"")
    for i, fy in enumerate(sorted(float(y) for y in fold_ys), start=1):
        gutter = z.get(f"fold_gutter_{i}")
        if gutter is None:
            fails.append(f"missing fold_gutter_{i}")
            continue
        center_in = (gutter.y + gutter.h / 2) / dpi
        expected = bleed_margin_y + fy
        if abs(center_in - expected) > 0.01:
            fails.append(f"fold_gutter_{i} center {center_in:.3f}\" != fold {expected:.3f}\"")

    # Glue zones on opening edges
    opening_edges = folding.get("opening_edges") or []
    edge_to_zone = {
        "top": "glue_zone_top",
        "bottom": "glue_zone_bottom",
        "left_outer": "glue_zone_left",
        "right_outer": "glue_zone_right",
    }
    for edge in opening_edges:
        zone_name = edge_to_zone.get(edge)
        if zone_name and zone_name not in z:
            fails.append(f"missing {zone_name} (declared opening edge {edge})")

    return fails


def check_v1_zones(rows: list[dict]) -> list[tuple]:
    failures = []
    for row in rows:
        cat, var = row["mailer_category"], row["variant"]
        if cat == "postcard" and var in V1_POSTCARD_VARIANTS:
            spec = _spec_from_json_row(row)
            fs = check_zones_postcard(spec)
        elif cat == "self_mailer" and var in V1_SELF_MAILER_VARIANTS:
            spec = _spec_from_json_row(row)
            fs = check_zones_self_mailer(spec)
        else:
            continue
        marker = "OK " if not fs else "FAIL"
        print(f"{marker}  zones {cat:18s} {var:25s} {'; '.join(fs) if fs else ''}")
        if fs:
            failures.append(("zones", cat, var, fs))
    print(f"\n[zones] Checked {len(V1_POSTCARD_VARIANTS) + len(V1_SELF_MAILER_VARIANTS)} v1 specs, {len(failures)} failure(s).")
    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    data = json.loads(SPEC_PATH.read_text())
    rows = data["specs"]
    pdf_failures = check_pdf_mediaboxes(rows)
    print()
    zone_failures = check_v1_zones(rows)

    total = len(pdf_failures) + len(zone_failures)
    if total:
        print(f"\nSync FAILED with {total} issue(s).")
        return 1
    print("\nSync OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
