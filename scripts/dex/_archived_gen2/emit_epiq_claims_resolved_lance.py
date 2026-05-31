#!/usr/bin/env python3
"""Emit epiq.claims_resolved_lance — the canonical claim-grain join axis.

Reads epiq.claims_lance and augments every claim row with four neutral
identity-resolution columns derived from the existing raw data:

    creditor_legal_name_normalized      ← _lib.entity_name_normalize v1.0.0
    creditor_state                      ← _lib.epiq_normalize state parse
    creditor_zip5                       ← _lib.epiq_normalize zip parse
    creditor_address_base_normalized    ← _lib.address_normalize v1.0.0 (base)

Plus one boolean flag:

    is_generic_creditor_marker          ← _lib.epiq_normalize markers + numbered-claimant
                                          regex + NULL-normalize-result

No rollups, no GTM lens. This is the identity-resolution layer on top of
the raw per-claim source-of-truth. Every claim has its own row; granularity
is preserved end-to-end.

Bridges JOIN through this dataset. The deduped creditor-identity rolodex
(`epiq.creditors_lance`) is a separate derived spine that reads from THIS
dataset and GROUPs to identity grain — single source of normalization truth.

Lance URI:
    s3://dex-raw-landing-zone/polaris-warehouse/epiq/claims_resolved_lance

BTREE on:
    creditor_legal_name_normalized
    creditor_state
    creditor_zip5
    creditor_address_base_normalized
    project_code
    case_number

Doppler env:
    R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    DEX_DB_URL_DIRECT (for lance_commit_lock)

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/emit_epiq_claims_resolved_lance.py [--apply]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
    normalize_address_street,
)
from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NAME_NORMALIZER_VERSION,
    normalize_entity_name,
)
from scripts._lib.epiq_normalize import (  # noqa: E402
    __version__ as EPIQ_NORMALIZER_VERSION,
    is_epiq_generic_creditor_marker,
    parse_state_zip_from_address_list_json,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("emit-epiq-claims-resolved")

CLAIMS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/epiq/claims_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/epiq/claims_resolved_lance"
DATASET_SLUG = "epiq_claims_resolved_lance"
TMP_DIR = "/tmp/lance"


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write Lance (else dry-run)")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT required when --apply")

    log.info("Epiq claims-resolved emit  apply=%s", args.apply)
    log.info("source: %s", CLAIMS_LANCE_URI)
    log.info("target: %s", LANCE_URI)
    log.info(
        "normalizers: entity_name=v%s  address=v%s  epiq=v%s",
        NAME_NORMALIZER_VERSION, ADDR_NORMALIZER_VERSION, EPIQ_NORMALIZER_VERSION,
    )

    import lance
    import pyarrow as pa

    storage_options = _storage_options()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    log.info("opening %s …", CLAIMS_LANCE_URI)
    ds_src = lance.dataset(CLAIMS_LANCE_URI, storage_options=storage_options)
    src_tbl = ds_src.to_table()
    n_rows = len(src_tbl)
    log.info("  claims_lance rows: %d  cols: %d", n_rows, len(src_tbl.schema.names))

    log.info("computing identity columns row-by-row (python-side) …")
    creditor_names = src_tbl["creditor_name"].to_pylist()
    addr_jsons = src_tbl["creditor_address_list_json"].to_pylist()
    redact_names = src_tbl["redact_creditor_name"].to_pylist()
    redact_addrs = src_tbl["redact_creditor_address"].to_pylist()

    name_norm: list[str | None] = [None] * n_rows
    state_col: list[str | None] = [None] * n_rows
    zip_col: list[str | None] = [None] * n_rows
    addr_base: list[str | None] = [None] * n_rows
    is_generic: list[bool] = [False] * n_rows

    for i in range(n_rows):
        nn = normalize_entity_name(creditor_names[i]) if creditor_names[i] else None
        name_norm[i] = nn
        is_generic[i] = bool(redact_names[i]) or is_epiq_generic_creditor_marker(nn)

        st, zp, street = parse_state_zip_from_address_list_json(addr_jsons[i])
        state_col[i] = st
        zip_col[i] = zp
        if street and not redact_addrs[i]:
            addr_base[i] = normalize_address_street(street)

    log.info("  rows with normalized name: %d  generic markers: %d",
             sum(1 for x in name_norm if x), sum(1 for x in is_generic if x))
    log.info("  rows with state parsed:    %d  with zip5:        %d",
             sum(1 for x in state_col if x), sum(1 for x in zip_col if x))
    log.info("  rows with address_base:    %d", sum(1 for x in addr_base if x))

    # Append the 5 derived columns to the source table.
    out_tbl = src_tbl.append_column(
        "creditor_legal_name_normalized", pa.array(name_norm, type=pa.string())
    ).append_column(
        "creditor_state", pa.array(state_col, type=pa.string())
    ).append_column(
        "creditor_zip5", pa.array(zip_col, type=pa.string())
    ).append_column(
        "creditor_address_base_normalized", pa.array(addr_base, type=pa.string())
    ).append_column(
        "is_generic_creditor_marker", pa.array(is_generic, type=pa.bool_())
    )
    log.info("output schema cols: %d (= input %d + 5 derived)",
             len(out_tbl.schema.names), len(src_tbl.schema.names))

    if not args.apply:
        log.info("DRY-RUN — pass --apply to write Lance.")
        return 0

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    with lance_commit_lock(DATASET_SLUG):
        log.info("writing Lance (mode=overwrite) …")
        ds = lance.write_dataset(
            out_tbl, LANCE_URI, mode="overwrite", storage_options=storage_options,
        )
        log.info("  rows=%d  version=%s", ds.count_rows(), ds.version)

        for col in (
            "creditor_legal_name_normalized",
            "creditor_state",
            "creditor_zip5",
            "creditor_address_base_normalized",
            "project_code",
            "case_number",
        ):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                log.info("  BTREE on %s", col)
            except Exception as exc:  # noqa: BLE001
                log.warning("  BTREE on %s failed (non-fatal): %s", col, exc)

        try:
            ds.optimize.compact_files()
        except Exception as exc:  # noqa: BLE001
            log.warning("compact_files failed (non-fatal): %s", exc)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:  # noqa: BLE001
            log.warning("cleanup_old_versions failed (non-fatal): %s", exc)

    log.info("OK uri=%s", LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
