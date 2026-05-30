"""Validator baseline probe for the FL Sunbiz × SBA bridge floor.

Run via Doppler:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python \\
      scripts/migration-checks/hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-baseline-probe.py

What it does:
  1. Reads `sba.borrowers_lance` from R2 via Lance scanner; counts distinct
     `legal_name_normalized` where `borrstate='FL'` AND value IS NOT NULL.
  2. Streams ~100K cordata0.txt records via `unzip -p ... | head -c $((100000 * 1442))`
     (each line is 1440 chars + CRLF = 1442 bytes per the pinned layout).
     Parses each record using the canonical FL COR offset map
     (`hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-cor-layout.COR_FIELDS`).
     Projects entity_name → applies `_lib.entity_name_normalize.normalize_entity_name`
     (the SAME normalizer the executor will use — validator p1 enforcement).
  3. Scales the 100K sample by `total_cordata0_records / 100K` (each cordata
     shard is ~1.26M records; 10 shards → ~12.6M total entities, but the
     bridge only needs the entity-grain count, NOT officer-grain).
  4. INNER JOIN: distinct SBA FL borrower names × distinct FL SoS sample
     entity names. Count matched pairs.
  5. Scale the matched-pair count to the full 12.6M via 1.26M / sample_seen.
     Set bridge floor = 0.5 × scaled estimate.

This probe is read-only: no R2 writes, no DB writes, no migrations.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SAMPLE_RECORDS = 100_000
# Empirical: each cordata shard ≈ 1.26M records (per directive §Goal restated,
# 17.7 GB uncompressed total / 10 shards / 1442 bytes per line). Sanity-cross:
# 1817783758 / 1442 = 1,260,599 records in cordata0.txt — verified.
TOTAL_CORDATA0_RECORDS = 1_260_599
TOTAL_CORDATA_RECORDS_ALL_SHARDS = 12_606_000  # ~1.26M × 10 shards
SBA_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
CORDATA_ZIP = Path("/Users/benjamincrane/Downloads/cordata.zip")
CORDATA0_NAME = "cordata0.txt"

# Make sure we can import the layout module + the normalizer.
REPO_DEX_ROOT = Path("/Users/benjamincrane/hq-all/apps/data-engine-x")
sys.path.insert(0, str(REPO_DEX_ROOT))
sys.path.insert(0, str(REPO_DEX_ROOT / "scripts" / "migration-checks"))


def storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


# ---- step 1: FL SBA distinct normalized names ------------------------------ #

def sba_fl_distinct_norm_names() -> set[str]:
    import lance

    ds = lance.dataset(SBA_LANCE_URI, storage_options=storage_options())
    tbl = ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter="borrstate = 'FL'",
    ).to_table()
    names = set(tbl.column("legal_name_normalized").to_pylist())
    names.discard(None)
    names.discard("")
    return names


# ---- step 2: sample 100K cordata0.txt records ------------------------------ #

def sample_cordata_entity_names(sample_records: int) -> tuple[set[str], int]:
    """Return (distinct_normalized_entity_name_set, rows_seen).

    Streams `unzip -p cordata.zip cordata0.txt | head -c <bytes>`. Each record
    is 1442 bytes (1440 content + CRLF), so we need
    `sample_records * 1442` bytes. We parse each 1442-byte block using the
    pinned COR layout module.
    """
    from hq_all_fl_sunbiz_quarterly_ingest_and_sba_bridge_cor_layout import (  # type: ignore
        COR_FIELDS,
        COR_LINE_STRIDE_BYTES,
    )
    # NOTE: Python module name with hyphens is illegal — fall back to manual
    # constants. The module loads via importlib below if available; if not,
    # we hard-code the same offsets.
    raise NotImplementedError("inline-imported below; this path is dead")


def _load_layout_module():
    """Import the pinned COR layout despite its hyphenated filename."""
    import importlib.util
    layout_path = (
        REPO_DEX_ROOT / "scripts" / "migration-checks" /
        "hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-cor-layout.py"
    )
    spec = importlib.util.spec_from_file_location("fl_cor_layout", layout_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sample_cordata_entity_names_v2(sample_records: int) -> tuple[set[str], int]:
    """Return (distinct_normalized_entity_name_set, rows_seen). Loads layout via importlib."""
    from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402

    layout = _load_layout_module()
    stride = layout.COR_LINE_STRIDE_BYTES
    en_start, en_end = layout.COR_FIELDS["entity_name"][0], layout.COR_FIELDS["entity_name"][1]

    n_bytes = sample_records * stride
    cmd = f"unzip -p {CORDATA_ZIP} {CORDATA0_NAME} | head -c {n_bytes}"
    proc = subprocess.run(["bash", "-c", cmd], check=False, capture_output=True)
    if proc.returncode not in (0, 141):  # 141 = SIGPIPE from head
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        raise RuntimeError(f"unzip|head failed rc={proc.returncode}")
    raw = proc.stdout

    # Verify pure ASCII (validator-confirmed; Sunbiz docs say ASCII-only).
    # If non-ASCII bytes show up, document but don't fail — fall back to cp1252.
    try:
        text = raw.decode("ascii")
        encoding_used = "ascii"
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
        encoding_used = "cp1252-fallback"

    print(f"ENCODING_DETECTED: {encoding_used}")
    rows_seen = len(text) // stride
    print(f"DATA_BYTES_FETCHED: {len(text)}; RECORDS_PARSED: {rows_seen}")

    normalized: set[str] = set()
    for i in range(rows_seen):
        rec = text[i * stride : i * stride + stride]
        entity_name_raw = rec[en_start:en_end].strip()
        if not entity_name_raw:
            continue
        n = normalize_entity_name(entity_name_raw)
        if n:
            normalized.add(n)

    return normalized, rows_seen


# ---- step 3: intersect, scale, recommend floor ----------------------------- #

def main() -> int:
    print("=== FL SBA distinct normalized names probe ===")
    sba_set = sba_fl_distinct_norm_names()
    print(f"SBA_FL_DISTINCT_NORM: {len(sba_set):,}")

    print("=== cordata0.txt 100K-record sample probe ===")
    sos_sample_set, sos_rows_seen = sample_cordata_entity_names_v2(SAMPLE_RECORDS)
    print(f"SOS_SAMPLE_DISTINCT_NORM: {len(sos_sample_set):,}")

    intersection = sba_set & sos_sample_set
    sample_overlap = len(intersection)

    # Two-stage scale: first to all of cordata0, then to all 10 shards.
    scale_to_cordata0 = TOTAL_CORDATA0_RECORDS / max(sos_rows_seen, 1)
    scaled_overlap_cordata0 = sample_overlap * scale_to_cordata0
    scaled_overlap_full = scaled_overlap_cordata0 * 10  # ~10 shards

    print("=== Intersection ===")
    print(f"SAMPLE_OVERLAP_DISTINCT_ENTITY_NAMES: {sample_overlap:,}")
    print(f"SAMPLE_RECORDS_SEEN: {sos_rows_seen:,}")
    print(f"SCALE_TO_CORDATA0: {scale_to_cordata0:.2f}")
    print(f"SCALED_OVERLAP_CORDATA0: {scaled_overlap_cordata0:,.0f}")
    print(f"SCALED_OVERLAP_FULL_12_6M: {scaled_overlap_full:,.0f}")

    # The bridge produces N:M expansions up to COLLISION_THRESHOLD=50 per
    # matched normalized-name. Empirically (CA SoS bridge PR #464) the
    # distinct-name overlap UNDER-estimates the row floor by 1-3× — the actual
    # bridge has more rows than distinct-name pairs because of multi-officer
    # fan-out. We use the canonical 0.5× safety on the SCALED distinct-overlap
    # count to set a defensible lower bound.
    proposed_floor = int(0.5 * scaled_overlap_full)

    print("=== Bridge floor recommendation ===")
    print(f"PROPOSED_BRIDGE_FLOOR (0.5 × scaled distinct-name overlap): {proposed_floor:,}")
    print(
        "NOTE: distinct-name overlap is a LOWER bound on rows_matched because "
        "Pattern B bridges fan out N:M up to 50×50 per matched name. The "
        "executor should expect actual rows >= proposed_floor."
    )

    # Sanity: if proposed_floor is below the directive's hard floor of 50K,
    # surface that explicitly — the executor's gate uses max(50_000, proposed_floor).
    if proposed_floor < 50_000:
        print(
            f"WARN: proposed_floor={proposed_floor:,} is below the directive's "
            "hard 50K threshold. Use 50_000 as the executor's MIN_ROWS_MATCHED."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
