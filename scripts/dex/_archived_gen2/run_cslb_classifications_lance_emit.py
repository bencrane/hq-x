"""CSLB license classification lookup → Lance emit (Pattern A, one-off).

Writes a static ~49-row Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/cslb/classifications_lance

Source: CA Business & Professions Code §7055 (general/specialty) +
        §7058.5 (ASB — asbestos certification) + §7058.7 (HAZ — hazardous
        substance certification). Hardcoded in CLASSIFICATIONS constant below.

Schema: code, kind, description, statute_ref, notes (5 columns).
BTREE on code.

No R2 Parquet intermediate — builds Arrow table in-memory directly from the
CLASSIFICATIONS constant. Direct lance.write_dataset per validator p1 (pattern
for static lookup, not CSV-sourced data).

One-off: no Modal, no cron, no idempotency loop.

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_cslb_classifications_lance_emit.py

Directive: hq/directives/2026-05-17-cslb-master-license-data-lance-emit-one-off.md
Joins to: cslb.licensees_lance.Classifications(s) (dash-inconsistency "C57" vs
"C-7" preserved verbatim in licensees; downstream consumer's normalization concern).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# Resolve scripts/ as a package when run directly (not via installed module).
# apps/data-engine-x/scripts/ is this file's parent; insert its parent
# (apps/data-engine-x/) so that `from scripts._lib.*` resolves correctly.
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

import lance
import pyarrow as pa

from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

DATASET_SLUG = "cslb_classifications_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/cslb/classifications_lance"

# Verify floor: must have at least 45 rows to catch truncation.
MIN_ROW_FLOOR = 45

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# Classification lookup — CA B&P §7055 + §7058.5 + §7058.7
# ---------------------------------------------------------------------------
# 4 general + 2 general-umbrella + 42 specialty C-codes + 2 certifications = 49 rows.
# Source: https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=BPC&sectionNum=7055
# Notes column: None where not applicable.

CLASSIFICATIONS: list[dict] = [
    # General licenses (CA B&P §7055 — general engineering, general building)
    {
        "code": "A",
        "kind": "general",
        "description": "General Engineering Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": "Licensed to construct fixed works requiring specialized engineering knowledge and skill",
    },
    {
        "code": "B",
        "kind": "general",
        "description": "General Building Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": "Licensed for framing, structural, and general building work; may sub 2+ unrelated specialty trades",
    },
    {
        "code": "B-2",
        "kind": "general",
        "description": "Residential Remodeling Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": "Residential remodeling; may contract for 2+ unrelated specialty trades on residential structures",
    },
    {
        "code": "C",
        "kind": "general",
        "description": "Specialty Contractor (umbrella)",
        "statute_ref": "CA B&P §7055",
        "notes": "Parent classification umbrella for all C-prefix specialty trades",
    },
    # Specialty licenses (CA B&P §7055 — C-prefix codes)
    {
        "code": "C-2",
        "kind": "specialty",
        "description": "Insulation and Acoustical Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-4",
        "kind": "specialty",
        "description": "Boiler, Hot Water Heating and Steam Fitting Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-5",
        "kind": "specialty",
        "description": "Framing and Rough Carpentry Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-6",
        "kind": "specialty",
        "description": "Cabinet, Millwork and Finish Carpentry Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-7",
        "kind": "specialty",
        "description": "Low Voltage Systems Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": "Includes alarm, communications, and data systems; joins to 'C-7' in licensees (dash-consistent)",
    },
    {
        "code": "C-8",
        "kind": "specialty",
        "description": "Concrete Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-9",
        "kind": "specialty",
        "description": "Drywall Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-10",
        "kind": "specialty",
        "description": "Electrical Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-11",
        "kind": "specialty",
        "description": "Elevator Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-12",
        "kind": "specialty",
        "description": "Earthwork and Paving Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-13",
        "kind": "specialty",
        "description": "Fencing Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-15",
        "kind": "specialty",
        "description": "Flooring and Floor Covering Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-16",
        "kind": "specialty",
        "description": "Fire Protection Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-17",
        "kind": "specialty",
        "description": "Glazing Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-20",
        "kind": "specialty",
        "description": "Warm-Air Heating, Ventilating and Air-Conditioning Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-21",
        "kind": "specialty",
        "description": "Building Moving and Demolition Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-22",
        "kind": "specialty",
        "description": "Asbestos Abatement Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-23",
        "kind": "specialty",
        "description": "Ornamental Metal Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-27",
        "kind": "specialty",
        "description": "Landscaping Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-28",
        "kind": "specialty",
        "description": "Lock and Security Equipment Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-29",
        "kind": "specialty",
        "description": "Masonry Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-31",
        "kind": "specialty",
        "description": "Construction Zone Traffic Control Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-32",
        "kind": "specialty",
        "description": "Parking and Highway Improvement Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-33",
        "kind": "specialty",
        "description": "Painting and Decorating Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-34",
        "kind": "specialty",
        "description": "Pipeline Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-35",
        "kind": "specialty",
        "description": "Lathing and Plastering Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-36",
        "kind": "specialty",
        "description": "Plumbing Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-38",
        "kind": "specialty",
        "description": "Refrigeration Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-39",
        "kind": "specialty",
        "description": "Roofing Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-42",
        "kind": "specialty",
        "description": "Sanitation System Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-43",
        "kind": "specialty",
        "description": "Sheet Metal Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-45",
        "kind": "specialty",
        "description": "Sign Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-46",
        "kind": "specialty",
        "description": "Solar Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-47",
        "kind": "specialty",
        "description": "General Manufactured Housing Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-49",
        "kind": "specialty",
        "description": "Tree Service Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-50",
        "kind": "specialty",
        "description": "Reinforcing Steel Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-51",
        "kind": "specialty",
        "description": "Structural Steel Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-53",
        "kind": "specialty",
        "description": "Swimming Pool Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-54",
        "kind": "specialty",
        "description": "Ceramic and Mosaic Tile Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-55",
        "kind": "specialty",
        "description": "Water Conditioning Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-57",
        "kind": "specialty",
        "description": "Well Drilling Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": "Note: licensees data may show 'C57' (no dash) due to upstream formatting inconsistency",
    },
    {
        "code": "C-60",
        "kind": "specialty",
        "description": "Welding Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": None,
    },
    {
        "code": "C-61",
        "kind": "specialty",
        "description": "Limited Specialty Contractor",
        "statute_ref": "CA B&P §7055",
        "notes": "D-prefix sub-classifications (D-01 through D-65) exist under C-61",
    },
    # Certifications (CA B&P §7058.5 + §7058.7)
    {
        "code": "ASB",
        "kind": "certification",
        "description": "Asbestos Certification",
        "statute_ref": "CA B&P §7058.5",
        "notes": "Certification for asbestos-related work; not a standalone license classification",
    },
    {
        "code": "HAZ",
        "kind": "certification",
        "description": "Hazardous Substance Removal Certification",
        "statute_ref": "CA B&P §7058.7",
        "notes": "Certification for hazardous substance removal; not a standalone license classification",
    },
]


# ---------------------------------------------------------------------------
# R2 storage options (needed for lance_commit_lock + write_dataset)
# ---------------------------------------------------------------------------

def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


# ---------------------------------------------------------------------------
# Main emit logic
# ---------------------------------------------------------------------------

def emit() -> dict:
    """Pattern A Lance emit: CLASSIFICATIONS constant → Arrow table → Lance + BTREE.

    No R2 Parquet intermediate — builds Arrow table in-memory directly.
    """
    # Lance env setup (same as licensees emit)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("TMPDIR", "/tmp/lance")
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

    # Build Arrow table from hardcoded constant
    table = pa.Table.from_pylist(CLASSIFICATIONS)
    num_rows = table.num_rows
    logger.info(
        "Classifications table: %d rows, schema=%s",
        num_rows, table.schema,
    )

    if num_rows < MIN_ROW_FLOOR:
        msg = f"FAIL: CLASSIFICATIONS has {num_rows} rows (floor {MIN_ROW_FLOOR})"
        logger.error(msg)
        sys.exit(1)

    storage_options = _storage_options()

    # Write Lance dataset inside commit lock
    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("Writing Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            table,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_count = ds.count_rows()
        write_dur = time.time() - t0
        logger.info(
            "Wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

    # BTREE on code (single index for static lookup)
    logger.info("Building BTREE index on code ...")
    ds.create_scalar_index("code", index_type="BTREE", replace=True)
    logger.info("BTREE on code: OK")

    # Compact + cleanup
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("compact_files + cleanup_old_versions: OK")
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    result = {
        "status": "succeeded",
        "rows_classifications": num_rows,
        "rows_lance": lance_count,
        "lance_uri": LANCE_URI,
        "write_duration_s": round(write_dur, 1),
    }
    logger.info("emit complete: %s", result)
    return result


def main() -> None:
    import json
    out = emit()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
