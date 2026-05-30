"""Tier 3 extraction — schemas + per-item config.

One Pydantic schema per Item (Item5Extraction for fees/compensation,
Item4Extraction for advisory business / AUM). Used by the dispatcher to
validate subagent JSON output and project to Lance-compatible row dicts.

Schema-first by design. The Opus subagent prompts at
`scripts/_lib/sec_iapd_brochure_tier3_item{N}_subagent_prompt.md` are the
human-readable specifications; this module is the machine-readable spec the
dispatcher enforces.

Complex collection fields (arrays/objects) serialize to JSON-string VARCHAR
per Lance 1.5.x L54 (avoid LIST<VARCHAR>). Downstream DuckDB/Polaris consumers
parse via json_extract.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

EXTRACTOR_VERSION = "1.0.0"

FeeStructureType = Literal[
    "pct_aum_tiered",
    "pct_aum_flat",
    "performance_fee",
    "hourly",
    "fixed_fee",
    "retainer",
    "commission",
    "subscription",
    "wrap_fee",
    "financial_planning_fixed",
    "other",
]

ExtractionConfidence = Literal["high", "medium", "low", "not_present"]


class AumTierBreakpoint(BaseModel):
    """One row in the AUM-tiered fee schedule.

    Half-open interval [lower_usd, upper_usd) — upper_usd=null means open-ended
    top tier ("above $X"). rate_pct is the % charged on assets in this tier,
    expressed as the actual percentage (e.g. 1.25 means 1.25%, NOT 0.0125).
    rate_pct=null means the tier exists but the rate is negotiable / undisclosed
    (common pattern: "$10M+ Negotiable").
    """
    lower_usd: float = Field(ge=0)
    upper_usd: float | None = None
    rate_pct: float | None = Field(default=None, ge=0, le=10)


class Item5Extraction(BaseModel):
    """Tier 3 Item 5 extracted structured signals (one per row)."""

    crd_number: int
    version_id: str

    item_5_extracted: bool = Field(
        description="True if Item 5 text was present + non-empty in the source brochure."
    )
    fee_structure_types: list[FeeStructureType] = Field(
        default_factory=list,
        description="Enum tags for every fee model the firm uses (multi-valued)."
    )
    aum_tier_breakpoints: list[AumTierBreakpoint] = Field(
        default_factory=list,
        description="Tiered % of AUM schedule. Empty list = not AUM-tiered or no schedule disclosed."
    )
    performance_fee_pct: float | None = Field(default=None, ge=0, le=100)
    performance_fee_hurdle_rate_pct: float | None = Field(default=None, ge=0, le=100)
    performance_fee_high_water_mark: bool | None = None
    minimum_account_size_usd: float | None = Field(default=None, ge=0)
    fees_negotiable: bool | None = None
    commissions_received: bool | None = None
    wrap_fee_program: bool | None = None
    aum_referenced_in_item_5_usd: float | None = Field(
        default=None, ge=0,
        description=(
            "If Item 5 text mentions total AUM, capture for cross-validation against "
            "Part 1 5F2c. Many brochures restate AUM in Item 5; many do not (null)."
        )
    )
    fee_extraction_confidence: ExtractionConfidence
    fee_extraction_notes: str = Field(
        default="",
        description=(
            "Free-text caveats, source phrase citations, or notes about ambiguity. "
            "Empty if extraction was clean."
        )
    )

    @field_validator("aum_tier_breakpoints")
    @classmethod
    def _tiers_monotonic(cls, v: list[AumTierBreakpoint]) -> list[AumTierBreakpoint]:
        """Tier breakpoints should be in increasing order of lower_usd."""
        if len(v) < 2:
            return v
        for i in range(1, len(v)):
            if v[i].lower_usd < v[i-1].lower_usd:
                raise ValueError(
                    f"aum_tier_breakpoints must be monotonically increasing by lower_usd; "
                    f"got [{v[i-1].lower_usd}, {v[i].lower_usd}] at index {i-1}->{i}"
                )
        return v


def to_lance_row(ext: Item5Extraction) -> dict:
    """Project a validated Item5Extraction to the flat-typed dict shape we'll
    write into Parquet (and then add_columns into Lance).

    Complex collection fields serialize to JSON-string VARCHAR per L54.
    """
    return {
        "crd_number": ext.crd_number,
        "version_id": ext.version_id,
        "item_5_extracted": ext.item_5_extracted,
        "fee_structure_types": json.dumps(ext.fee_structure_types),
        "aum_tier_breakpoints": json.dumps(
            [t.model_dump() for t in ext.aum_tier_breakpoints]
        ),
        "performance_fee_pct": ext.performance_fee_pct,
        "performance_fee_hurdle_rate_pct": ext.performance_fee_hurdle_rate_pct,
        "performance_fee_high_water_mark": ext.performance_fee_high_water_mark,
        "minimum_account_size_usd": ext.minimum_account_size_usd,
        "fees_negotiable": ext.fees_negotiable,
        "commissions_received": ext.commissions_received,
        "wrap_fee_program": ext.wrap_fee_program,
        "aum_referenced_in_item_5_usd": ext.aum_referenced_in_item_5_usd,
        "fee_extraction_confidence": ext.fee_extraction_confidence,
        "fee_extraction_notes": ext.fee_extraction_notes,
        "extractor_version": EXTRACTOR_VERSION,
    }


def lance_column_names() -> list[str]:
    """Names of the new typed columns Tier 3 Item 5 will land on the Lance dataset."""
    return [
        "item_5_extracted",
        "fee_structure_types",
        "aum_tier_breakpoints",
        "performance_fee_pct",
        "performance_fee_hurdle_rate_pct",
        "performance_fee_high_water_mark",
        "minimum_account_size_usd",
        "fees_negotiable",
        "commissions_received",
        "wrap_fee_program",
        "aum_referenced_in_item_5_usd",
        "fee_extraction_confidence",
        "fee_extraction_notes",
        "tier3_item5_extractor_version",
    ]


# ─────────────────────────────────────────────────────────────────────────── #
# Item 4 — Advisory Business (AUM, services, founding, wrap sponsorship)
# ─────────────────────────────────────────────────────────────────────────── #

ServiceType = Literal[
    "portfolio_management_individual",
    "portfolio_management_institutional",
    "financial_planning",
    "selection_of_other_advisers",
    "educational_seminars",
    "newsletters_publications",
    "retirement_plan_advisory",
    "wrap_fee_program_sponsor",
    "private_fund_management",
    "model_portfolio_provider",
    "other",
]


class Item4Extraction(BaseModel):
    """Tier 3 Item 4 (Advisory Business) extracted structured signals."""

    crd_number: int
    version_id: str

    item_4_extracted: bool = Field(
        description="True if Item 4 text was present and non-empty in the source brochure."
    )
    total_aum_usd: float | None = Field(
        default=None, ge=0,
        description="Total regulatory AUM in USD (Item 4E). Null if not disclosed.",
    )
    discretionary_aum_usd: float | None = Field(default=None, ge=0)
    non_discretionary_aum_usd: float | None = Field(default=None, ge=0)
    aum_as_of_date: str | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD) as-of which AUM was measured. Null if not disclosed.",
    )
    firm_founded_year: int | None = Field(default=None, ge=1800, le=2100)
    services_offered: list[ServiceType] = Field(
        default_factory=list,
        description="Advisory service categories the firm offers (multi-valued, from Item 4B)."
    )
    tailored_to_individual_clients: bool | None = Field(
        default=None,
        description="Item 4C: 'Yes' if the firm tailors advisory services to individual clients."
    )
    wrap_fee_sponsor: bool | None = Field(
        default=None,
        description="Item 4D: True if the firm sponsors a wrap fee program."
    )
    aum_extraction_confidence: Literal["high", "medium", "low", "not_present"]
    aum_extraction_notes: str = Field(default="")


def to_lance_row_item4(ext: Item4Extraction) -> dict:
    return {
        "crd_number": ext.crd_number,
        "version_id": ext.version_id,
        "item_4_extracted": ext.item_4_extracted,
        "total_aum_usd": ext.total_aum_usd,
        "discretionary_aum_usd": ext.discretionary_aum_usd,
        "non_discretionary_aum_usd": ext.non_discretionary_aum_usd,
        "aum_as_of_date": ext.aum_as_of_date,
        "firm_founded_year": ext.firm_founded_year,
        "services_offered": json.dumps(ext.services_offered),
        "tailored_to_individual_clients": ext.tailored_to_individual_clients,
        "wrap_fee_sponsor": ext.wrap_fee_sponsor,
        "aum_extraction_confidence": ext.aum_extraction_confidence,
        "aum_extraction_notes": ext.aum_extraction_notes,
        "extractor_version": EXTRACTOR_VERSION,
    }


# ─────────────────────────────────────────────────────────────────────────── #
# Per-item dispatcher config (single source of truth for the generalized
# dispatch_sec_iapd_tier3_*_extract.py).
# ─────────────────────────────────────────────────────────────────────────── #

ITEM_CONFIG: dict[int, dict] = {
    5: {
        "title": "Fees and Compensation",
        "lance_source_col": "item_5_fees_and_compensation",
        "schema_cls": Item5Extraction,
        "to_lance_row": to_lance_row,
        "manifest_text_key": "item_5_text",
        "batches_dir": "/tmp/tier3-item5-batches",
        "results_dir": "/tmp/tier3-item5-results",
        "prompt_alias_path": "/tmp/sec_iapd_tier3_item5_subagent_prompt.txt",
        "prompt_repo_filename": "sec_iapd_brochure_tier3_item5_subagent_prompt.md",
    },
    4: {
        "title": "Advisory Business",
        "lance_source_col": "item_4_advisory_business",
        "schema_cls": Item4Extraction,
        "to_lance_row": to_lance_row_item4,
        "manifest_text_key": "item_4_text",
        "batches_dir": "/tmp/tier3-item4-batches",
        "results_dir": "/tmp/tier3-item4-results",
        "prompt_alias_path": "/tmp/sec_iapd_tier3_item4_subagent_prompt.txt",
        "prompt_repo_filename": "sec_iapd_brochure_tier3_item4_subagent_prompt.md",
    },
}
