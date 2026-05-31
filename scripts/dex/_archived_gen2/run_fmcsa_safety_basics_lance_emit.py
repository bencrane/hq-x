#!/usr/bin/env python3
"""Re-emit FMCSA safety_basics as a Lance dataset on R2 (sms_ab_pass_property LEFT JOIN sms_ab_pass).

Architecture: LEFT JOIN of sms_ab_pass_property (driver, 706K rows, raw violation measures
for all property-authority carriers) and sms_ab_pass (9K rows, FMCSA-published percentiles
+ BASIC alert flags) on DOT_NUMBER. Result: 706K-row Lance dataset where:
- 9K carriers (the AB-pass cohort) have full FMCSA percentile scores + BASIC/RD alert flags
  populated (copied verbatim from sms_ab_pass — no recomputation).
- 697K carriers (property-only cohort) have NULL for percentile/alert columns (FMCSA suppresses
  these for carriers below their data-sufficiency threshold per SMS Methodology v4.0 — recomputing
  would diverge from published methodology; NULL is the correct value, not a gap to fill).
- All 706K carriers have raw violation measures (_INSP_W_VIOL, _MEASURE, _AC × 5 BASICs) and
  Acute/Critical violation counts from the sms_ab_pass_property feed.

Source choice: sms_ab_pass_property (705,854 rows, snapshot 2026-05-10) is the driver because
it covers every property-authority carrier. sms_ab_pass (8,923 rows) is a perfect subset (all
AB-pass DOTs exist in property; verified 2026-05-13). The LEFT JOIN guarantees 706K output rows.

The BTREE scalar index on dot_number and Postgres advisory commit lock are preserved from the
Wave 1 design (see _scan_parquet_to_arrow_with_left_join and emit_lance).

Sources:
  ``r2://dex-raw-landing-zone/fmcsa-derived/sms_ab_pass_property/snapshot=YYYY-MM-DD/data.parquet``
  ``r2://dex-raw-landing-zone/fmcsa-derived/sms_ab_pass/snapshot=YYYY-MM-DD/data.parquet``

Output:
  ``s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/safety_basics_lance/``

Usage
-----
    # Dry run:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance python3 \\
        apps/data-engine-x/scripts/run_fmcsa_safety_basics_lance_emit.py --dry-run

    # Apply:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance python3 \\
        apps/data-engine-x/scripts/run_fmcsa_safety_basics_lance_emit.py --apply

Required env (from Doppler):
    R2_ENDPOINT
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    DEX_DB_URL_DIRECT       (for the commit lock)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger("fmcsa-safety-basics-lance-emit")

R2_BUCKET = "dex-raw-landing-zone"
INPUT_PREFIX = "fmcsa-derived/sms_ab_pass_property"
AB_PREFIX = "fmcsa-derived/sms_ab_pass"
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/safety_basics_lance"
)
DATASET_SLUG = "fmcsa_safety_basics_lance"  # commit-lock key
TMP_DIR = "/tmp/lance"


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _ensure_tmpdir() -> None:
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR


def _connect_duckdb_to_r2():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _detect_latest_snapshot(con, prefix: str = INPUT_PREFIX) -> str:
    """Detect the latest snapshot= partition under the given R2 prefix.

    Uses INPUT_PREFIX by default (sms_ab_pass_property — the authoritative driver).
    Pass AB_PREFIX to detect the sms_ab_pass snapshot separately for alignment checks.
    """
    rows = con.execute(
        f"SELECT file FROM glob('r2://{R2_BUCKET}/{prefix}/snapshot=*/data.parquet')"
    ).fetchall()
    if not rows:
        raise SystemExit(
            f"FAIL: no Parquet files found under r2://{R2_BUCKET}/{prefix}/"
        )
    snapshots: set[str] = set()
    for (path,) in rows:
        for p in path.split("/"):
            if p.startswith("snapshot=") and len(p) == len("snapshot=YYYY-MM-DD"):
                snapshots.add(p[len("snapshot="):])
    if not snapshots:
        raise SystemExit("FAIL: no snapshot=YYYY-MM-DD dirs detected")
    return max(snapshots)


def _detect_aligned_snapshots(con) -> tuple[str, str]:
    """Return (prop_snapshot, ab_snapshot) ensuring alignment.

    Both feeds are co-published by the daily fmcsa-factory cron at 06:00 UTC,
    so they should always be aligned. If they diverge (anomalous), we use
    min(prop_snap, ab_snap) and log a warning. If no ab_snap exists that is
    <= prop_snap, raises SystemExit with a clear error.
    """
    prop_snap = _detect_latest_snapshot(con, prefix=INPUT_PREFIX)
    ab_snap = _detect_latest_snapshot(con, prefix=AB_PREFIX)

    if prop_snap == ab_snap:
        LOG.info("snapshot alignment: prop=%s ab=%s (aligned)", prop_snap, ab_snap)
        return prop_snap, ab_snap

    LOG.warning(
        "WARN: snapshot misalignment detected — prop=%s ab=%s. "
        "Using min(prop, ab) = %s to avoid forward-looking bias.",
        prop_snap, ab_snap, min(prop_snap, ab_snap),
    )
    aligned = min(prop_snap, ab_snap)

    # Verify the aligned snapshot actually exists for both prefixes.
    prop_uri_check = f"r2://{R2_BUCKET}/{INPUT_PREFIX}/snapshot={aligned}/data.parquet"
    ab_uri_check = f"r2://{R2_BUCKET}/{AB_PREFIX}/snapshot={aligned}/data.parquet"
    try:
        con.execute(f"SELECT 1 FROM read_parquet('{prop_uri_check}') LIMIT 1")
    except Exception:
        raise SystemExit(
            f"FAIL: aligned snapshot {aligned} missing for prop prefix {INPUT_PREFIX}"
        )
    try:
        con.execute(f"SELECT 1 FROM read_parquet('{ab_uri_check}') LIMIT 1")
    except Exception:
        raise SystemExit(
            f"FAIL: aligned snapshot {aligned} missing for ab prefix {AB_PREFIX}"
        )

    return aligned, aligned


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _scan_parquet_to_arrow_with_left_join(con, prop_uri: str, ab_uri: str):
    """Read both Parquets and LEFT JOIN sms_ab_pass_property ← sms_ab_pass on DOT_NUMBER.

    The property feed is the driver (706K rows). The ab feed (9K rows) supplies
    FMCSA-published percentiles and alert flags for the publishable-percentile cohort.
    Carriers not in the ab cohort get NULL for all 15 percentile/alert columns.

    EXCLUDE ("DOT_NUMBER", snapshot) on the property side prevents duplicate columns
    (DOT_NUMBER is aliased to dot_number; snapshot is pulled from prop explicitly as
    the trailing column so the Lance schema has exactly one snapshot column).

    The 15 explicitly enumerated ab columns are the 5 BASICs × 3 suffix groups:
      _PCT          — FMCSA-published percentile score (0-100)
      _BASIC_ALERT  — FMCSA BASIC alert flag (Y/N/NULL)
      _RD_ALERT     — Roadside Detection alert flag (Y/N/NULL)
    """
    rel = con.from_query(f'''
        SELECT
            prop."DOT_NUMBER" AS dot_number,
            prop.* EXCLUDE ("DOT_NUMBER", snapshot),
            ab."UNSAFE_DRIV_PCT", ab."HOS_DRIV_PCT", ab."DRIV_FIT_PCT",
            ab."CONTR_SUBST_PCT", ab."VEH_MAINT_PCT",
            ab."UNSAFE_DRIV_BASIC_ALERT", ab."HOS_DRIV_BASIC_ALERT",
            ab."DRIV_FIT_BASIC_ALERT", ab."CONTR_SUBST_BASIC_ALERT",
            ab."VEH_MAINT_BASIC_ALERT",
            ab."UNSAFE_DRIV_RD_ALERT", ab."HOS_DRIV_RD_ALERT",
            ab."DRIV_FIT_RD_ALERT", ab."CONTR_SUBST_RD_ALERT",
            ab."VEH_MAINT_RD_ALERT",
            prop.snapshot
        FROM read_parquet('{prop_uri}') prop
        LEFT JOIN read_parquet('{ab_uri}') ab
            ON prop."DOT_NUMBER" = ab."DOT_NUMBER"
    ''')
    return rel.to_arrow_reader(batch_size=100_000)


def emit_lance(prop_snapshot: str, ab_snapshot: str) -> dict:
    """Read both Parquet snapshots, LEFT JOIN, write to Lance, optimize. Returns metrics."""
    import lance

    prop_uri = (
        f"r2://{R2_BUCKET}/{INPUT_PREFIX}/snapshot={prop_snapshot}/data.parquet"
    )
    ab_uri = (
        f"r2://{R2_BUCKET}/{AB_PREFIX}/snapshot={ab_snapshot}/data.parquet"
    )
    LOG.info("prop input:  %s", prop_uri)
    LOG.info("ab input:    %s", ab_uri)
    LOG.info("output: %s", LANCE_URI)
    LOG.info(
        "note: LEFT JOIN on DOT_NUMBER; property rows are authoritative; "
        "ab supplies percentile/alert cols for ~9K AB-pass carriers"
    )

    con = _connect_duckdb_to_r2()

    # parquet_count is from the property feed (authoritative driver row count).
    parquet_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{prop_uri}')"
    ).fetchone()[0]
    LOG.info("property parquet rows (authoritative): %d", parquet_count)

    ab_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{ab_uri}')"
    ).fetchone()[0]
    LOG.info("ab-pass parquet rows (percentile cohort): %d", ab_count)

    storage_options = _lance_storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = _scan_parquet_to_arrow_with_left_join(con, prop_uri, ab_uri)
        LOG.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        LOG.info(
            "wrote %d rows to Lance in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        t_idx = time.time()
        LOG.info("creating BTREE scalar index on dot_number ...")
        try:
            ds.create_scalar_index("dot_number", index_type="BTREE", replace=True)
            idx_dur = time.time() - t_idx
            LOG.info("  index built in %.1fs", idx_dur)
        except Exception as e:
            LOG.warning("  BTREE index build failed (non-fatal): %s", e)
            idx_dur = None

        t1 = time.time()
        LOG.info("optimize: cleanup_older_than=7d ...")
        try:
            stats = ds.optimize.compact_files()
            LOG.info("  compact_files: %s", stats)
        except Exception as e:
            LOG.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:
            LOG.warning("  cleanup_old_versions failed (non-fatal): %s", e)
        opt_dur = time.time() - t1
        LOG.info("optimize done in %.1fs", opt_dur)

    return {
        "prop_snapshot": prop_snapshot,
        "ab_snapshot": ab_snapshot,
        "parquet_rows": parquet_count,
        "ab_pass_rows": ab_count,
        "lance_rows": lance_count,
        "lance_version": ds.version,
        "write_seconds": round(write_dur, 1),
        "index_seconds": round(idx_dur, 1) if idx_dur is not None else None,
        "optimize_seconds": round(opt_dur, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="counts only")
    ap.add_argument(
        "--snapshot",
        default=None,
        help="YYYY-MM-DD snapshot for both prefixes (default: latest, aligned)",
    )
    args = ap.parse_args()

    for var in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    ):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    _ensure_tmpdir()

    if args.dry_run:
        con = _connect_duckdb_to_r2()
        if args.snapshot:
            prop_snapshot = ab_snapshot = args.snapshot
        else:
            prop_snapshot, ab_snapshot = _detect_aligned_snapshots(con)
        prop_uri = (
            f"r2://{R2_BUCKET}/{INPUT_PREFIX}/snapshot={prop_snapshot}/data.parquet"
        )
        ab_uri = (
            f"r2://{R2_BUCKET}/{AB_PREFIX}/snapshot={ab_snapshot}/data.parquet"
        )
        total = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{prop_uri}')"
        ).fetchone()[0]
        ab_total = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{ab_uri}')"
        ).fetchone()[0]
        LOG.info(
            "DRY RUN — prop_snapshot=%s rows=%d  ab_snapshot=%s ab_rows=%d",
            prop_snapshot, total, ab_snapshot, ab_total,
        )
        return 0

    con = _connect_duckdb_to_r2()
    if args.snapshot:
        prop_snapshot = ab_snapshot = args.snapshot
    else:
        prop_snapshot, ab_snapshot = _detect_aligned_snapshots(con)
    con.close()

    metrics = emit_lance(prop_snapshot, ab_snapshot)
    LOG.info("OK — metrics: %s", metrics)
    if metrics["parquet_rows"] != metrics["lance_rows"]:
        LOG.error(
            "FAIL: row count mismatch parquet=%d lance=%d",
            metrics["parquet_rows"], metrics["lance_rows"],
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
