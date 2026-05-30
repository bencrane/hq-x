#!/usr/bin/env python3
"""Parity gate for the lifted entity-name normalizer (per L32).

Compares the new shared `_lib/entity_name_normalize.py` against the existing
PPP `_normalize_borrower_name` rule baked into `build_sba_ppp_parquet.py`.

Logic:
  1. Read 10K random borrower-name rows from the SBA PPP Parquet on R2.
  2. Re-normalize each name via the new shared module.
  3. Compare against the existing `borrower_name_normalized` column.
  4. Require ≥99% match (per L32). Surface mismatches.

Why these two:
  - The PPP `_normalize_borrower_name` rule is the **production** rule that
    populated `borrower_name_normalized` in `entities.raw_entity_records` /
    the R2 PPP Parquet, and it's the rule the EIDL ingest copied to achieve
    44.44% PPP↔EIDL overlap.
  - The new shared module lifts that rule and adds:
      a) holdings/group/associates suffix tokens (matches EIDL's superset).
      b) L33 generic-string blacklist (rejects things PPP would've kept as a
         non-empty string — but those rows can't appear in PPP since PPP
         borrower names are real businesses, not "self employed").

Surface: any name where new returns None but existing column has a value
(and vice versa). The blacklist additions should NOT trigger many cases
on PPP (PPP borrowers = real businesses by program definition).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb python apps/data-engine-x/scripts/parity_gate_entity_name_normalize.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import duckdb

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("parity_gate")


PPP_PARQUET_GLOB = (
    "s3://dex-raw-landing-zone/sba/program=ppp/segment=*/part-*.parquet"
)
SAMPLE_SIZE = 10_000
MIN_MATCH_PCT = 0.99


def _connect_duckdb_to_r2() -> duckdb.DuckDBPyConnection:
    endpoint_full = os.environ["R2_ENDPOINT"]
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}';")
    con.execute(
        f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}';"
    )
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_region='auto';")
    return con


def main() -> int:
    con = _connect_duckdb_to_r2()
    log.info("sampling %d rows from %s …", SAMPLE_SIZE, PPP_PARQUET_GLOB)

    rows = con.execute(
        f"""
        SELECT borrower_name, borrower_name_normalized
          FROM read_parquet('{PPP_PARQUET_GLOB}')
         WHERE borrower_name IS NOT NULL
           AND borrower_name_normalized IS NOT NULL
         USING SAMPLE {SAMPLE_SIZE} ROWS
        """
    ).fetchall()
    log.info("got %d rows", len(rows))

    matches = 0
    mismatches: list[tuple[str, str | None, str | None]] = []
    for raw, existing_norm in rows:
        new_norm = normalize_entity_name(raw)
        if new_norm == existing_norm:
            matches += 1
        else:
            mismatches.append((raw, existing_norm, new_norm))

    pct = matches / max(1, len(rows))
    log.info("matches: %d / %d = %.2f%%", matches, len(rows), pct * 100)
    log.info("mismatches: %d", len(mismatches))

    # Sample mismatches
    for raw, existing, new in mismatches[:20]:
        log.info("  raw=%r existing=%r new=%r", raw, existing, new)

    if pct < MIN_MATCH_PCT:
        log.error(
            "PARITY GATE FAIL: %.2f%% < %.0f%%",
            pct * 100, MIN_MATCH_PCT * 100,
        )
        return 1

    log.info(
        "PARITY GATE PASS: %.2f%% >= %.0f%%",
        pct * 100, MIN_MATCH_PCT * 100,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
