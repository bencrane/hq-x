"""Validator baseline probe for the UCC CA lender × CA SoS owner bridge floor.

Run via Doppler:
    cd /Users/benjamincrane/hq-all/apps/data-engine-x && \
      doppler run --project hq-all --config prd -- \
      bash -c 'uv run python scripts/migration-checks/hq-all-ucc-ca-lender-sos-ca-owner-bridge-baseline-probe.py'

What it does:
  1. Opens `s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance`
     via PyLance scanner. Loads columns ['ORG_NAME', 'SECURED_PARTY_TYPE'] with
     filter SECURED_PARTY_TYPE='Organization'. Samples SAMPLE_ROWS rows.
  2. Applies `scripts._lib.entity_name_normalize.normalize_entity_name` to ORG_NAME
     (the SAME canonical normalizer used in the just-shipped SBA × SoS bridge).
     Drops None / empty results.
  3. Computes distinct `secured_party_name_normalized` from the sample. Scales to
     the full 4.74M UCC corpus.
  4. Opens `s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance` via
     PyLance scanner. Loads column ['entity_name_normalized']. Counts distinct
     values (filter is_valid).
  5. Intersects the UCC-sample-distinct-name set with the SoS-distinct-name set,
     then scales to the full UCC corpus.
  6. Reports the scaled overlap estimate. Recommends `bridge floor = 0.5 ×
     scaled_estimate` per directive §"Volume floors".

This probe is read-only: no R2 writes, no DB writes, no migrations. Idempotent.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import lance
import pyarrow.compute as pc

# ---- inputs ------------------------------------------------------------------

UCC_SP_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance"
)
SOS_ENTITIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance"
)

# UCC corpus stats from directive:
#   - secured_parties_lance: 4,743,627 rows total
#   - SECURED_PARTY_TYPE='Organization' is the relevant subset
UCC_TOTAL_ROWS_EST = 4_743_627

# Sample size for UCC normalize/distinct. Pulled with PyLance scanner — Arrow
# slicing keeps memory bounded. UCC secured parties have heavy repetition (one
# big lender appears 70K-117K times in 4.7M filings), so distinct-name growth
# saturates slowly. Use FULL corpus (4.68M rows) — only ~12s to load + normalize
# in Python at 200K rows/s, and it eliminates the sub-linear-scaling uncertainty.
SAMPLE_ROWS = 5_000_000  # set higher than 4.68M Organization rows → reads all

# Make sure we can import `scripts._lib.entity_name_normalize` from this repo.
REPO_DEX_ROOT = Path("/Users/benjamincrane/hq-all/apps/data-engine-x")
sys.path.insert(0, str(REPO_DEX_ROOT))

from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402


def storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


# ---- step 1: UCC CA secured parties — sample + normalize ---------------------

def ucc_sample_distinct_norm_names(sample_rows: int) -> tuple[set[str], int, int]:
    """Return (distinct_norm_name_set, sample_rows_actually_used, total_org_rows).

    Reads the Organization subset of UCC CA secured_parties_lance, samples
    the first `sample_rows` rows from the scanner, normalizes ORG_NAME with
    `_lib/entity_name_normalize.normalize_entity_name`, returns distinct
    non-None normalized values.
    """
    t0 = time.time()
    ds = lance.dataset(UCC_SP_LANCE_URI, storage_options=storage_options())
    print(f"UCC: lance dataset opened in {time.time() - t0:.1f}s; version={ds.version}")

    # First get total row count of Organization subset (cheap — Arrow row count)
    t1 = time.time()
    org_count_tbl = ds.scanner(
        columns=["SECURED_PARTY_TYPE"],
        filter=pc.field("SECURED_PARTY_TYPE") == "Organization",
    ).to_table()
    total_org_rows = len(org_count_tbl)
    print(f"UCC_ORGANIZATION_TOTAL_ROWS: {total_org_rows} (probe took {time.time() - t1:.1f}s)")
    del org_count_tbl

    # Now sample the first `sample_rows` rows of the Organization subset.
    t2 = time.time()
    scanner = ds.scanner(
        columns=["ORG_NAME"],
        filter=pc.field("SECURED_PARTY_TYPE") == "Organization",
        limit=sample_rows,
    )
    sample_tbl = scanner.to_table()
    rows_used = len(sample_tbl)
    print(f"UCC_SAMPLE_ROWS_LOADED: {rows_used} (took {time.time() - t2:.1f}s)")

    org_names = sample_tbl.column("ORG_NAME").to_pylist()
    del sample_tbl

    t3 = time.time()
    norm_set: set[str] = set()
    for raw in org_names:
        n = normalize_entity_name(raw)
        if n:
            norm_set.add(n)
    print(f"UCC_SAMPLE_DISTINCT_NORM_NAMES: {len(norm_set)} (normalize took {time.time() - t3:.1f}s)")

    return norm_set, rows_used, total_org_rows


# ---- step 2: SoS distinct entity_name_normalized ------------------------------

def sos_distinct_norm_names() -> set[str]:
    """Return distinct entity_name_normalized from CA SoS entities_lance."""
    t0 = time.time()
    ds = lance.dataset(SOS_ENTITIES_LANCE_URI, storage_options=storage_options())
    print(f"SOS: lance dataset opened in {time.time() - t0:.1f}s; version={ds.version}")

    t1 = time.time()
    tbl = ds.scanner(
        columns=["entity_name_normalized"],
        filter=pc.field("entity_name_normalized").is_valid(),
    ).to_table()
    print(f"SOS_TOTAL_NON_NULL_ROWS: {len(tbl)} (took {time.time() - t1:.1f}s)")

    t2 = time.time()
    names = set(tbl.column("entity_name_normalized").to_pylist())
    names.discard(None)
    names.discard("")
    print(f"SOS_DISTINCT_NORM_NAMES: {len(names)} (set build took {time.time() - t2:.1f}s)")
    return names


# ---- step 3: intersect and scale ---------------------------------------------

def main() -> int:
    print("=" * 70)
    print("UCC CA lender × CA SoS owner bridge — baseline probe")
    print(f"sample_rows={SAMPLE_ROWS}  ucc_total_est={UCC_TOTAL_ROWS_EST:,}")
    print("=" * 70)

    print("\n--- Step 1: UCC CA secured parties sample (SECURED_PARTY_TYPE=Organization)")
    ucc_sample_norm, sample_used, total_org_rows = ucc_sample_distinct_norm_names(SAMPLE_ROWS)

    print("\n--- Step 2: CA SoS entities distinct normalized names")
    sos_norm = sos_distinct_norm_names()

    print("\n--- Step 3: intersection + scale to full UCC corpus")
    intersection = ucc_sample_norm & sos_norm
    sample_overlap = len(intersection)
    print(f"SAMPLE_OVERLAP_DISTINCT_NAMES: {sample_overlap}")

    # If sample_used >= total_org_rows, no extrapolation is needed — the
    # observed overlap IS the full-corpus distinct overlap. Set scale_factor=1.
    # This is the default mode for this probe (SAMPLE_ROWS > total_org_rows).
    if sample_used >= total_org_rows:
        scale_factor = 1.0
        scaled_estimate = sample_overlap
        scaling_mode = "FULL_CORPUS (no extrapolation needed)"
    elif sample_used > 0:
        # Sub-linear extrapolation: distinct-name growth saturates because UCC
        # has heavy repetition (top secured parties appear 70K-117K times). A
        # log-scaling factor sqrt(total/sample) better approximates actual
        # growth than linear extrapolation. NOTE: when sample is full corpus
        # (default), this branch is not taken.
        import math
        linear_factor = total_org_rows / sample_used
        sqrt_factor = math.sqrt(linear_factor)
        scale_factor = sqrt_factor
        scaled_estimate = int(sample_overlap * scale_factor)
        scaling_mode = f"SQRT_EXTRAPOLATION (linear={linear_factor:.1f}, sqrt={sqrt_factor:.2f})"
    else:
        scale_factor = 1.0
        scaled_estimate = sample_overlap
        scaling_mode = "EMPTY_SAMPLE"

    print(f"SAMPLE_ROWS_USED: {sample_used}")
    print(f"UCC_ORG_TOTAL_ROWS: {total_org_rows}")
    print(f"SCALING_MODE: {scaling_mode}")
    print(f"SCALE_FACTOR: {scale_factor:.4f}")
    print(f"SCALED_ESTIMATE_DISTINCT_OVERLAP: {scaled_estimate}")

    # The floor recommendation is 0.5 × scaled estimate per directive §"Volume
    # floors". For the bridge ROW count: Pattern B fan-out can lift rows above
    # distinct-name overlap, but distinct-name overlap is the lower-bound
    # "matched-names" floor — the executor's floor should be at least this many
    # distinct-name matches (one row per matched name minimum).
    proposed_floor = int(0.5 * scaled_estimate)

    print("\n=== Bridge floor recommendation ===")
    print(f"PROPOSED_BRIDGE_FLOOR (0.5 × scaled distinct-name overlap): {proposed_floor}")
    print(
        "\nNOTE: distinct-name overlap is a LOWER bound on rows_matched because "
        "Pattern B bridges fan out N:M up to COLLISION_THRESHOLD=50 per matched "
        "name. The executor should expect actual rows_matched to be at least "
        "distinct-name overlap (fan-out=1) and often 2-5× that for legal-name "
        "joins with multiple SoS rows per matched name."
    )
    print(
        "\nIf proposed_floor < 10000: blocked-baseline-too-low (insufficient coverage to ship)."
    )
    print(
        "If proposed_floor < 50000: surface as risk-flag but do not block — "
        "operator intent is 'see if you can get to some identity'."
    )

    print("\n--- Summary table ---")
    print(f"ucc_sample_distinct_norm_names       = {len(ucc_sample_norm)}")
    print(f"sos_distinct_norm_names              = {len(sos_norm)}")
    print(f"sample_overlap_distinct_names        = {sample_overlap}")
    print(f"ucc_org_total_rows                   = {total_org_rows}")
    print(f"sample_rows_used                     = {sample_used}")
    print(f"scaling_mode                         = {scaling_mode}")
    print(f"scaled_distinct_overlap              = {scaled_estimate}")
    print(f"proposed_bridge_floor                = {proposed_floor}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
