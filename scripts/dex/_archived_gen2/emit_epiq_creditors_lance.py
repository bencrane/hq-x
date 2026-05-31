#!/usr/bin/env python3
"""Emit epiq.creditors_lance — the deduped creditor rolodex.

Reads epiq.claims_resolved_lance (the canonical claim-grain spine with the
four identity-resolution columns already baked) and aggregates to creditor
identity grain — one row per `(creditor_legal_name_normalized,
creditor_state, creditor_zip5)`.

This is a CONVENIENCE spine for identity-level rollups (count + dollar
exposure across the standard Epiq amount buckets, distinct cases, time
window). Consumers that need different rollups should aggregate directly
from `claims_resolved_lance` — that's the raw join axis, this is one of
many possible identity-grain views derived from it.

This spine is NOT the bridge JOIN axis — that role belongs to
`claims_resolved_lance`. Bridges JOIN at claim grain to preserve per-claim
fan-out detail. Use this spine for:

  - Identity-level lookups (top-N creditors by total exposure, etc.)
  - Cross-case rolodex queries ("which creditors appear in N cases")
  - Pre-aggregated reporting surfaces

Lance URI:
    s3://dex-raw-landing-zone/polaris-warehouse/epiq/creditors_lance

BTREE on:
    creditor_legal_name_normalized
    creditor_state
    creditor_zip5
    creditor_address_base_normalized
    total_unsec_claim_amount

Doppler env:
    R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    DEX_DB_URL_DIRECT

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/emit_epiq_creditors_lance.py [--apply]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
)
from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NAME_NORMALIZER_VERSION,
)
from scripts._lib.epiq_normalize import (  # noqa: E402
    __version__ as EPIQ_NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("emit-epiq-creditors")

CLAIMS_RESOLVED_URI = "s3://dex-raw-landing-zone/polaris-warehouse/epiq/claims_resolved_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/epiq/creditors_lance"
DATASET_SLUG = "epiq_creditors_lance"
TMP_DIR = "/tmp/lance"

# The 11 Epiq amount-bucket keys, in canonical order. Each is a dollar-formatted
# string like "$186,978.34" inside the amount_list_json struct.
_AMOUNT_KEYS = (
    "scheduledSecuredAmount", "scheduledPriorityAmount", "scheduledUnsecuredAmount",
    "secClaimAmount", "priClaimAmount", "unsecClaimAmount", "adminClaimAmount",
    "secAllowedAmount", "priAllowedAmount", "unsecAllowedAmount", "adminAllowedAmount",
)


def _amount_sum_sql(field: str) -> str:
    """SQL expression that SUMs a $-formatted amount across rows."""
    return (
        f"SUM(TRY_CAST(REPLACE(REPLACE(REPLACE("
        f"json_extract_string(amount_list_json, '$.{field}'), '$', ''), ',', ''), ' ', '') AS DOUBLE))"
    )


def _camel_to_snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    return con


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write Lance (else dry-run)")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT required when --apply")

    log.info("Epiq creditors rolodex emit  apply=%s", args.apply)
    log.info("source: %s", CLAIMS_RESOLVED_URI)
    log.info("target: %s", LANCE_URI)
    log.info(
        "normalizers (provenance): entity_name=v%s  address=v%s  epiq=v%s",
        NAME_NORMALIZER_VERSION, ADDR_NORMALIZER_VERSION, EPIQ_NORMALIZER_VERSION,
    )

    import lance

    storage_options = _storage_options()
    log.info("opening claims_resolved_lance …")
    src_ds = lance.dataset(CLAIMS_RESOLVED_URI, storage_options=storage_options)
    src_tbl = src_ds.to_table(columns=[
        "project_code",
        "source_notice_id",
        "case_name",
        "case_number",
        "debtor_name",
        "creditor_name",
        "creditor_address_list_json",
        "search_type",
        "value_display",
        "amount_list_json",
        "filed_date_display",
        "document_urls",
        "creditor_legal_name_normalized",
        "creditor_state",
        "creditor_zip5",
        "creditor_address_base_normalized",
        "is_generic_creditor_marker",
        "ingested_at",
    ])
    log.info("  source rows: %d", len(src_tbl))

    con = _duckdb_conn()
    con.register("c", src_tbl)

    amount_cols_sql = ",\n            ".join(
        f"{_amount_sum_sql(k)} AS total_{_camel_to_snake(k)}"
        for k in _AMOUNT_KEYS
    )

    log.info("aggregating to creditor-identity grain …")
    con.execute(
        f"""
        CREATE TEMP TABLE spine_raw AS
        SELECT
            creditor_legal_name_normalized,
            creditor_state,
            COALESCE(creditor_zip5, '')                          AS creditor_zip5,
            mode(creditor_name)                                   AS creditor_name_sample,
            mode(creditor_address_base_normalized)
                FILTER (WHERE creditor_address_base_normalized IS NOT NULL)
                                                                  AS creditor_address_base_normalized,
            mode(creditor_address_list_json)
                FILTER (WHERE creditor_address_list_json IS NOT NULL)
                                                                  AS creditor_address_full_sample_json,
            COUNT(*)                                              AS n_claims_total,
            COUNT(*) FILTER (WHERE search_type = 'c')             AS n_claims_filed,
            COUNT(*) FILTER (WHERE search_type = 's')             AS n_claims_scheduled,
            COUNT(DISTINCT project_code)                          AS n_cases_distinct,
            LIST(DISTINCT project_code)                           AS case_codes_set,
            LIST(DISTINCT debtor_name)                            AS debtor_names_set,
            LIST(DISTINCT case_number)                            AS case_numbers_set,
            SUM(TRY_CAST(REPLACE(REPLACE(REPLACE(value_display, '$', ''), ',', ''), ' ', '') AS DOUBLE))
                                                                  AS total_value_displayed_dollars,
            {amount_cols_sql},
            MIN(TRY_STRPTIME(filed_date_display, '%b %d %Y')::DATE) AS first_filed_date,
            MAX(TRY_STRPTIME(filed_date_display, '%b %d %Y')::DATE) AS latest_filed_date,
            LIST(DISTINCT document_urls)
                FILTER (WHERE document_urls IS NOT NULL)          AS document_urls_set,
            MAX(ingested_at)                                      AS ingested_at
        FROM c
        WHERE creditor_legal_name_normalized IS NOT NULL
          AND NOT is_generic_creditor_marker
          AND creditor_state IS NOT NULL
        GROUP BY 1, 2, 3
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE spine AS
        SELECT
            *,
            (latest_filed_date IS NOT NULL
              AND latest_filed_date >= (CURRENT_DATE - INTERVAL 24 MONTH))
                                                                  AS recently_active
        FROM spine_raw
        """
    )

    n_spine, n_with_zip = con.execute(
        "SELECT COUNT(*), COUNT(NULLIF(creditor_zip5, '')) FROM spine"
    ).fetchone()
    log.info("  spine rows: %d  (with zip5: %d)", n_spine, n_with_zip)

    sample = con.execute(
        """
        SELECT creditor_name_sample, creditor_state, n_cases_distinct,
               n_claims_filed, total_unsec_claim_amount, latest_filed_date
        FROM spine
        WHERE total_unsec_claim_amount IS NOT NULL
        ORDER BY total_unsec_claim_amount DESC NULLS LAST LIMIT 5
        """
    ).fetchall()
    log.info("top 5 by total_unsec_claim_amount:")
    for r in sample:
        log.info("  %-40s %s  cases=%-2s filed=%-3s unsec=$%-15s latest=%s",
                 (r[0] or "")[:40], r[1], r[2], r[3], f"{r[4]:,.0f}" if r[4] else "n/a", r[5])

    if not args.apply:
        log.info("DRY-RUN — pass --apply to write Lance.")
        return 0

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    reader = con.execute("SELECT * FROM spine").fetch_record_batch(rows_per_batch=50_000)

    with lance_commit_lock(DATASET_SLUG):
        log.info("writing Lance (mode=overwrite) …")
        ds = lance.write_dataset(
            reader, LANCE_URI, mode="overwrite", storage_options=storage_options,
        )
        log.info("  rows=%d  version=%s", ds.count_rows(), ds.version)

        for col in (
            "creditor_legal_name_normalized",
            "creditor_state",
            "creditor_zip5",
            "creditor_address_base_normalized",
            "total_unsec_claim_amount",
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
