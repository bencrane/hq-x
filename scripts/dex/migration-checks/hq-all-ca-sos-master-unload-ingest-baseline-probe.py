"""Validator baseline probe for the CA SoS master-unload bridge floor.

Run via Doppler:
    doppler run --project hq-all --config prd -- uv run python \
        apps/data-engine-x/scripts/migration-checks/hq-all-ca-sos-master-unload-ingest-baseline-probe.py

What it does:
  1. Reads `sba.borrowers_lance` from R2 via Lance scanner; counts distinct
     `legal_name_normalized` where `borrstate='CA'` AND value IS NOT NULL.
  2. Streams the first 100,000 rows of `Principals.csv` from the operator-staged
     zip via `unzip -p`, parses with DuckDB `read_csv` (3-char `*|*` delimiter
     is not single-char so we hand-parse line by line into a typed list, then
     pass to DuckDB for batched normalization through the same `entity_name_normalize`
     normalizer the executor will use).
  3. Computes the intersection of the sampled SoS normalized entity-name set with
     the CA SBA normalized legal-name set.
  4. Scales the intersection size by 67 (Principals.csv is ~6.7M rows; sample is
     100K = 1/67 of the file). Reports the scaled overlap estimate.
  5. Recommends `bridge floor = 0.5 * scaled_estimate`.

This probe is read-only: no R2 writes, no DB writes, no migrations.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import lance
import pyarrow as pa
import pyarrow.compute as pc

# ---- inputs ------------------------------------------------------------------

ZIP_PATH = Path(
    "/Users/benjamincrane/Downloads/DataRequest0x229B7838EEF50299FDF89F08D317243BD03FFFF3.zip"
)
SAMPLE_ROWS = 100_000
TOTAL_PRINCIPALS_EST = 6_700_000  # per directive: Principals.csv 2.68 GB ≈ 6.7M rows
SBA_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"

# Make sure we can import `_lib.entity_name_normalize` from the data-engine-x repo.
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


# ---- step 1: CA SBA distinct normalized names --------------------------------

def sba_ca_distinct_norm_names() -> set[str]:
    ds = lance.dataset(SBA_LANCE_URI, storage_options=storage_options())
    tbl = ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter="borrstate = 'CA'",
    ).to_table()
    names = set(tbl.column("legal_name_normalized").to_pylist())
    names.discard(None)
    names.discard("")
    return names


# ---- step 2: sample Principals.csv -------------------------------------------

def sample_principals_entity_names(sample_rows: int) -> tuple[set[str], int]:
    """Return (distinct_normalized_entity_name_set, rows_seen).

    Streams via `unzip -p <zip> Principals.csv | head -n <sample_rows+1>` so we
    only spool the first 100K data rows. Delimiter is the directive's `*|*`
    (3 chars). Column order per directive: ENTITY_NAME | ENTITY_NUM | ORG_NAME
    | FIRST_NAME | MIDDLE_NAME | LAST_NAME | POSITION_TYPE | ADDRESS1 |
    ADDRESS2 | ADDRESS3 | CITY | STATE | COUNTRY | POSTAL_CODE.
    """
    # +1 for the header line
    cmd = f"unzip -p {ZIP_PATH} Principals.csv | head -n {sample_rows + 1}"
    proc = subprocess.run(
        ["bash", "-c", cmd], check=False, capture_output=True
    )
    if proc.returncode not in (0, 141):  # 141 = SIGPIPE from head closing pipe
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        raise RuntimeError(f"unzip|head failed rc={proc.returncode}")
    raw = proc.stdout

    # Probe encoding: try UTF-8 first, fall back to cp1252 if a byte is invalid.
    try:
        text = raw.decode("utf-8")
        encoding_used = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
        encoding_used = "cp1252"

    print(f"ENCODING_DETECTED: {encoding_used}")
    lines = text.splitlines()
    if not lines:
        return set(), 0

    header = lines[0]
    print(f"PRINCIPALS_HEADER: {header[:240]}")
    data_lines = lines[1:]
    print(f"DATA_LINES_SAMPLED: {len(data_lines)}")

    # Split on the 3-char delimiter '*|*'.
    DELIM = "*|*"
    entity_names_raw: list[str] = []
    for line in data_lines:
        if not line:
            continue
        parts = line.split(DELIM)
        if parts:
            entity_names_raw.append(parts[0])

    # Normalize via the canonical normalizer in batch.
    normalized = set()
    for raw_name in entity_names_raw:
        n = normalize_entity_name(raw_name)
        if n:
            normalized.add(n)

    return normalized, len(entity_names_raw)


# ---- step 3: intersect and scale ---------------------------------------------

def main() -> int:
    print("=== CA SBA distinct normalized names probe ===")
    sba_set = sba_ca_distinct_norm_names()
    print(f"SBA_CA_DISTINCT_NORM: {len(sba_set)}")

    print("=== Principals.csv 100K-row sample probe ===")
    sos_sample_set, sos_rows_seen = sample_principals_entity_names(SAMPLE_ROWS)
    print(f"SOS_SAMPLE_DISTINCT_NORM: {len(sos_sample_set)}")

    intersection = sba_set & sos_sample_set
    sample_overlap = len(intersection)
    scale_factor = TOTAL_PRINCIPALS_EST / max(sos_rows_seen, 1)
    scaled_estimate = int(sample_overlap * scale_factor)

    print("=== Intersection ===")
    print(f"SAMPLE_OVERLAP_DISTINCT_ENTITY_NAMES: {sample_overlap}")
    print(f"SAMPLE_ROWS_SEEN: {sos_rows_seen}")
    print(f"SCALE_FACTOR: {scale_factor:.2f}")
    print(f"SCALED_ESTIMATE_DISTINCT_OVERLAP: {scaled_estimate}")

    # Important caveat: this is distinct-name overlap, NOT row-level matched
    # pairs. The bridge produces N:M expansions up to COLLISION_THRESHOLD=50
    # per matched normalized-name, so the row floor must be at least
    # `scaled_estimate * tier-weighted-avg-fanout`, which empirically is ~1-3×
    # for legal-name joins per the existing SBA × Overture bridge.
    proposed_floor = int(0.5 * scaled_estimate)

    print("=== Bridge floor recommendation ===")
    print(f"PROPOSED_BRIDGE_FLOOR (0.5 × scaled distinct-name overlap): {proposed_floor}")
    print(
        "NOTE: distinct-name overlap is a LOWER bound on rows_matched because "
        "Pattern B bridges fan out N:M per matched name. The executor should "
        "expect actual rows >> proposed_floor."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
