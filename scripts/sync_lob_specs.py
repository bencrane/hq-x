#!/usr/bin/env python3
"""
Verify data/lob_mailer_specs.json against Lob's published template PDFs.

This script does NOT scrape Lob's Help Center HTML — those pages are the
human-readable source of truth and we treat data/lob_mailer_specs.json as
the curated copy of what we read off them. What this script DOES do:

1. For every spec row that points to a template_pdf_url, download the PDF.
2. Read its MediaBox via `pdfinfo -box` (poppler-utils).
3. Compare MediaBox dimensions (in inches @ 72 pts/in) against the spec's
   bleed dimensions (or trim, if no bleed). Flag mismatches.

Re-run when Lob updates a template — the MediaBox is the easy machine-
readable check that the dimensions you transcribed still match. If a
mismatch appears, open the corresponding Help Center page (linked in
source_urls) and update data/lob_mailer_specs.json by hand, then
re-generate the seed migration.

Requires: poppler-utils (`brew install poppler` on macOS).
"""
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent.parent / "data" / "lob_mailer_specs.json"
PTS_PER_INCH = 72.0
TOLERANCE_IN = 0.01  # 0.01" ≈ 3 pts; plenty of slack for Adobe rounding


def media_box_inches(pdf_path: Path) -> tuple[float, float]:
    out = subprocess.check_output(["pdfinfo", "-box", str(pdf_path)], text=True)
    for line in out.splitlines():
        if line.startswith("MediaBox:"):
            _, x0, y0, x1, y1 = line.split()
            w_pts = float(x1) - float(x0)
            h_pts = float(y1) - float(y0)
            return round(w_pts / PTS_PER_INCH, 4), round(h_pts / PTS_PER_INCH, 4)
    raise RuntimeError(f"no MediaBox in {pdf_path}")


def expected_inches(spec: dict) -> tuple[float, float] | None:
    """The canvas size we expect the PDF MediaBox to match: bleed if the
    format has bleed, otherwise trim. Returned in (longer, shorter) order
    because PDFs are sometimes saved landscape vs portrait — we compare
    sorted pairs to ignore orientation."""
    if spec.get("bleed_w_in") and spec.get("bleed_h_in"):
        return tuple(sorted([spec["bleed_w_in"], spec["bleed_h_in"]], reverse=True))
    return tuple(sorted([spec["trim_w_in"], spec["trim_h_in"]], reverse=True))


def main():
    data = json.loads(SPEC_PATH.read_text())
    rows = data["specs"]
    failures = []
    checked = 0
    for spec in rows:
        url = spec.get("template_pdf_url")
        if not url:
            continue
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            urllib.request.urlretrieve(url, tmp.name)
            actual = media_box_inches(Path(tmp.name))
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
            failures.append((spec["mailer_category"], spec["variant"], actual, expected))

    print(f"\nChecked {checked} templates, {len(failures)} mismatch(es).")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
