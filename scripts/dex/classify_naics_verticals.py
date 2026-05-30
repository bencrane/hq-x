#!/usr/bin/env python3
"""
Classify all 1,012 NAICS 6-digit codes into staffing verticals using Claude Opus 4.7.

Inputs:
  /tmp/naics_6digit_with_hierarchy.csv
  /tmp/proposed_verticals.json

Output:
  /tmp/naics_vertical_classifications.json
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter

import anthropic

CSV_PATH = "/tmp/naics_6digit_with_hierarchy.csv"
VERTICALS_PATH = "/tmp/proposed_verticals.json"
OUT_PATH = "/tmp/naics_vertical_classifications.json"
MODEL = "claude-opus-4-7"
BATCH_SIZE = 50


def load_codes(csv_path: str) -> list[dict]:
    with open(csv_path) as f:
        return list(csv.DictReader(f))


def load_verticals(json_path: str) -> list[dict]:
    with open(json_path) as f:
        return json.load(f)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def classify_batch(client: anthropic.Anthropic, verticals: list, codes: list) -> list[dict]:
    verticals_text = json.dumps(
        [
            {
                "vertical_key": v["vertical_key"],
                "label": v["label"],
                "description": v["description"],
            }
            for v in verticals
        ],
        indent=2,
    )
    codes_text = json.dumps(
        [
            {
                "naics_code": c["naics_code"],
                "title": c["title"],
                "sector": c["sector_title"],
                "subsector": c["subsector_title"],
                "industry_group": c["industry_group_title"],
            }
            for c in codes
        ],
        indent=2,
    )
    prompt = f"""You are classifying NAICS codes for a staffing agency serving federal government contractors.

STAFFING VERTICALS (choose exactly one vertical_key per code):
{verticals_text}

NAICS CODES TO CLASSIFY:
{codes_text}

For each code return JSON: [{{"naics_code": "...", "vertical_key": "...", "confidence": "high|medium|review", "rationale": "..."}}]

Rules:
- Map to the workforce being placed, not the end-product industry. Aircraft Manufacturing places aerospace technicians, so it maps to aerospace_defense.
- Defense / weapons / aircraft / ships / ordnance / missiles / military vehicles -> aerospace_defense even when labeled manufacturing.
- All construction codes 23xxxx -> skilled_trades.
- Government-operated programs (NAICS 92xxxx) -> "other" unless there is a clear staffing angle.
- IT engineering / software / cybersecurity / data / cloud -> it_cybersecurity (NOT engineering).
- Non-IT engineering services (civil, mechanical, electrical, environmental) -> engineering.
- Warehousing 4931xx, courier 4922xx, freight/trucking 484xxx, 488xxx -> logistics_supply_chain.
- Janitorial, security guards, building maintenance, groundskeeping -> facilities_services.
- Legal, accounting, HR, management consulting, financial advisory -> professional_services.
- Biological / pharma / biomedical R&D -> life_sciences_research.
- Physical / engineering / materials sciences R&D -> scientific_research.
- Clinical health (nurses, MDs, allied health, mental health) -> healthcare_clinical.
- General manufacturing not tied to defense -> light_industrial_manufacturing.
- Environmental remediation / hazardous waste / waste management -> environmental_services.
- PMO / acquisition / contract specialists / SETA -> program_management.
- If a code has clearly low staffing relevance (crop farming, private households, finance trading, etc.) -> "other".
- Use confidence "review" only for genuinely ambiguous cases; otherwise use "high" or "medium".
- Return a JSON array only, no other text. No markdown fences.
"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    text = _strip_fences(resp.content[0].text)
    return json.loads(text)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in env (run under doppler).", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()
    codes = load_codes(CSV_PATH)
    verticals = load_verticals(VERTICALS_PATH)
    print(f"Loaded {len(codes)} codes, {len(verticals)} verticals.")

    results: list[dict] = []
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i : i + BATCH_SIZE]
        attempt = 0
        while True:
            attempt += 1
            try:
                print(
                    f"Classifying codes {i + 1}-{min(i + BATCH_SIZE, len(codes))} / {len(codes)} (attempt {attempt})..."
                )
                batch_results = classify_batch(client, verticals, batch)
                if len(batch_results) != len(batch):
                    raise ValueError(
                        f"batch returned {len(batch_results)} rows, expected {len(batch)}"
                    )
                results.extend(batch_results)
                break
            except Exception as e:
                print(f"  attempt {attempt} failed: {e!r}", file=sys.stderr)
                if attempt >= 3:
                    raise
                time.sleep(2 * attempt)
        time.sleep(0.4)

    with open(OUT_PATH, "w") as f:
        json.dump(results, f, indent=2)

    counts = Counter(r["vertical_key"] for r in results)
    conf_counts = Counter(r["confidence"] for r in results)
    print(f"\nDone. {len(results)} codes classified -> {OUT_PATH}")
    print("\nDistribution by vertical:")
    for v, n in counts.most_common():
        print(f"  {v}: {n}")
    print("\nConfidence distribution:")
    for c, n in conf_counts.most_common():
        print(f"  {c}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
