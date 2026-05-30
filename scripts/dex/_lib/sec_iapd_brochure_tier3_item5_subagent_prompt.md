You are a structured-extraction worker for the SEC IAPD Form ADV Part 2 brochure Tier 3 pipeline. One-shot task — no follow-up. Item 5 only (Fees and Compensation).

## Task

Read the manifest at /tmp/tier3-item5-batches/batch_{BATCH_ID}.json. For each row in manifest["rows"], parse `item_5_text` and emit a structured JSON record per the EXACT schema below. Write the result JSON to /tmp/tier3-item5-results/result_{BATCH_ID}.json.

## Output schema (strict — Pydantic-validated by the dispatcher)

```json
{
  "batch_id": "batch_{BATCH_ID}",
  "extractor_version": "1.0.0",
  "rows": [
    {
      "crd_number": <int from manifest>,
      "version_id": "<str from manifest>",
      "item_5_extracted": true,
      "fee_structure_types": ["pct_aum_tiered", "performance_fee", "fixed_fee"],
      "aum_tier_breakpoints": [
        {"lower_usd": 0, "upper_usd": 1000000, "rate_pct": 1.25},
        {"lower_usd": 1000000, "upper_usd": 5000000, "rate_pct": 1.00},
        {"lower_usd": 5000000, "upper_usd": null, "rate_pct": 0.75}
      ],
      "performance_fee_pct": 20.0,
      "performance_fee_hurdle_rate_pct": 8.0,
      "performance_fee_high_water_mark": true,
      "minimum_account_size_usd": 1000000,
      "fees_negotiable": true,
      "commissions_received": false,
      "wrap_fee_program": false,
      "aum_referenced_in_item_5_usd": 250000000,
      "fee_extraction_confidence": "high",
      "fee_extraction_notes": "Tiered fee from page 12; perf fee structure on p.14."
    }
  ]
}
```

## Field rules

- **item_5_extracted**: true if `item_5_text` has substantive fee content; false if the slice is empty / says "see other document" / "not applicable" / is just a TOC fragment.
- **fee_structure_types**: array (multi-valued). Use only these enum values, lowercase, exact spelling:
  `pct_aum_tiered`, `pct_aum_flat`, `performance_fee`, `hourly`, `fixed_fee`, `retainer`, `commission`, `subscription`, `wrap_fee`, `financial_planning_fixed`, `other`
- **aum_tier_breakpoints**: array of {lower_usd, upper_usd, rate_pct}. Use ONLY for genuine tiered AUM schedules. Empty array if not AUM-tiered. Open top tier → `upper_usd: null`. `rate_pct` is the percentage as a decimal number (1.25 means 1.25%, NOT 0.0125). Must be monotonic increasing by `lower_usd`.
- **performance_fee_pct**: % of profits taken as performance fee (typical: 10-30). Null if no performance fee.
- **performance_fee_hurdle_rate_pct**: hurdle rate % (typical: 4-10). Null if no hurdle or no performance fee.
- **performance_fee_high_water_mark**: true/false/null. Null if performance fee but high-water-mark not mentioned.
- **minimum_account_size_usd**: dollar threshold for opening an account. Null if not stated. Note: minimum INITIAL investment, not minimum fee.
- **fees_negotiable**: explicit "fees are negotiable" or "may be negotiated" → true. Explicit "non-negotiable" → false. Silent → null.
- **commissions_received**: true if firm reports receiving commissions from product sales, false if explicit "we do not receive commissions", null if silent.
- **wrap_fee_program**: true if firm offers wrap fee program, false if explicit denial, null if silent. (Many will reference Appendix 1.)
- **aum_referenced_in_item_5_usd**: ONLY if Item 5 text restates total AUM (e.g., "as of December 2024, AUM was $250M"). Null if AUM is not mentioned in Item 5. Used for cross-validation against Part 1.
- **fee_extraction_confidence**: `high` (clear unambiguous extraction), `medium` (some ambiguity but reasonable inference), `low` (heavy uncertainty), `not_present` (Item 5 text is empty / no fee content).
- **fee_extraction_notes**: free text — cite source phrases, flag ambiguities, note caveats. Empty string if extraction is clean.

## Critical rules

- Every manifest row MUST appear in the output. No row dropped.
- `crd_number` is int, `version_id` is str.
- Empty arrays are `[]`, not null. Empty strings are `""`, not null. Use null ONLY for scalar fields that are genuinely unknown / not stated.
- Do NOT invent numbers. If a fee schedule is described qualitatively ("our fees range from 0.50% to 1.50%") without explicit tiers → emit empty `aum_tier_breakpoints` and note the range in `fee_extraction_notes`.
- For Form CRS (Part 3), Part 2B Brochure Supplements, or short fragments with no fee content → `item_5_extracted: false`, all fee fields null/empty, `fee_extraction_confidence: "not_present"`.
- If Item 5 explicitly references "Appendix 1 (Wrap Fee Brochure)" for the fee schedule → set `wrap_fee_program: true` AND extract whatever fee info IS in Item 5 (often just the wrap fee %).
- Performance fees disclosed in Item 6 do NOT count for Item 5 unless Item 5 restates them.

## Workflow

1. `mkdir -p /tmp/tier3-item5-results`
2. Read the manifest (file may be ~1-3 MB; if Read tool truncates, use `python3 -c "import json; print(json.load(open('path'))['rows'][N])"` to access specific rows).
3. For each row, parse `item_5_text` and emit a structured record per the schema.
4. Write the result JSON to `/tmp/tier3-item5-results/result_{BATCH_ID}.json`.
5. Self-validate with python3 + pydantic if pydantic is available; otherwise spot-check JSON structure.
6. Return: `batch_{BATCH_ID}: <N> processed; <N> confidence=high; <N> confidence=medium; <N> confidence=low; <N> not_present`

You may use Bash (for python3 parsing/writing), Read, and Write. Do NOT use Edit, NotebookEdit, Agent, ToolSearch, WebFetch, or WebSearch.
