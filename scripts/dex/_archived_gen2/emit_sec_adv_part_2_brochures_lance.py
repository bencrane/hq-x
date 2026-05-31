"""Stage C — emit sec_adv.part_2_brochures_lance + register in Polaris.

Reads stage_a + stage_b Parquets (boto3 -> /tmp/ -> read_parquet per L58),
LEFT JOINs on (crd_number, version_id) — stage_a is the spine; stage_b adds
Tier 2 fallback Items for low-confidence rows (regex_partial -> opus_fallback
or opus_failed). DuckDB -> Arrow reader -> lance.write_dataset(mode="overwrite")
inside lance_commit_lock("sec_adv_part_2_brochures_lance").

Lance write discipline (precedent: run_sam_entity_pocs_lance_emit.py):
  - LANCE_BYPASS_SPILLING=true; TMPDIR=/tmp/lance
  - BTREE on crd_number, version_id, filing_date (3 scalar indices)
  - ds.optimize.compact_files()
  - ds.cleanup_old_versions(older_than=timedelta(days=7))

Polaris registration: subprocess to init_polaris_lance_generic.py
  --namespace sec_adv --table part_2_brochures_lance --doc "<...>"

Run (locally, NOT on Modal — Lance emit is fast enough for laptop):
    cd /Users/benjamincrane/hq-all/.claude/worktrees/interesting-hawking-a8b507 && \\
        doppler run --project hq-all --config prd -- python3 \\
        apps/data-engine-x/scripts/emit_sec_adv_part_2_brochures_lance.py \\
        --release YYYY-MM-DD --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger(__name__)

# Resolve scripts/_lib relative to this file when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sec_adv/part_2_brochures_lance/"
DATASET_SLUG = "sec_adv_part_2_brochures_lance"
POLARIS_NAMESPACE = "sec_adv"
POLARIS_TABLE = "part_2_brochures_lance"
POLARIS_DOC = (
    "SEC IAPD Form ADV Part 2A firm-brochure full-text + per-Item parsed text. "
    "One row per (crd_number, version_id). "
    "Tier 1 (pdfplumber full text + page count) + "
    "Tier 2 (regex Items 1-18 with Opus fallback on partial splits). "
    "Source: PR #318 raw scrape (17,525 PDFs) + this cycle's parse pipeline."
)


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _download_parquet(s3, key: str, local_path: str) -> None:
    """Download Parquet from R2 to /tmp/ — L58 pattern."""
    if Path(local_path).exists():
        LOG.info("using cached %s", local_path)
        return
    LOG.info("downloading s3://%s/%s -> %s", R2_BUCKET, key, local_path)
    s3.download_file(R2_BUCKET, key, local_path)
    LOG.info("  downloaded %.1f MB", Path(local_path).stat().st_size / 1_048_576)


def emit(release: str) -> dict:
    """Run Stage C Lance emit from stage_a + stage_b Parquets.

    Returns metrics dict with row counts.
    """
    import duckdb
    import lance

    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ["TMPDIR"] = "/tmp/lance"

    s3 = _r2_client()

    # L58: boto3 -> /tmp/ -> read_parquet (no direct DuckDB R2 httpfs reads for intermediates)
    stage_a_key = f"sec-iapd/form-adv-part-2-parsed/release={release}/stage_a.parquet"
    stage_b_key = f"sec-iapd/form-adv-part-2-parsed/release={release}/stage_b.parquet"
    local_a = f"/tmp/sec_iapd_stage_a_{release}.parquet"
    local_b = f"/tmp/sec_iapd_stage_b_{release}.parquet"

    _download_parquet(s3, stage_a_key, local_a)

    # stage_b may not exist if Tier 2 fallback had nothing to do
    try:
        _download_parquet(s3, stage_b_key, local_b)
        stage_b_exists = True
    except Exception as e:
        LOG.info("stage_b.parquet not found on R2 (%s) — will use stage_a only", e)
        stage_b_exists = False

    # DuckDB LEFT JOIN: stage_a is the spine; stage_b overlays Tier 2 fallback columns
    from scripts._lib.sec_iapd_brochure_parse import ITEM_TITLES

    item_cols = {n: f"item_{n}_{slug}" for n, slug in ITEM_TITLES.items()}
    item_coalesces = "\n    ".join(
        f"COALESCE(b.{col}, a.{col}) AS {col},"
        for n, col in item_cols.items()
    )

    con = duckdb.connect()

    stage_a_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{local_a}')"
    ).fetchone()[0]
    LOG.info("stage_a rows: %d", stage_a_count)

    if stage_b_exists:
        stage_b_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{local_b}')"
        ).fetchone()[0]
        LOG.info("stage_b rows (low-confidence fallback): %d", stage_b_count)
        join_sql = f"""
            SELECT
                a.crd_number,
                a.version_id,
                a.brochure_type,
                TRY_CAST(a.filing_date AS DATE) AS filing_date,
                a.r2_key,
                a.content_sha256,
                TRY_CAST(a.content_bytes AS BIGINT) AS content_bytes,
                TRY_CAST(a.page_count AS INTEGER) AS page_count,
                a.full_text_md,
                {item_coalesces}
                COALESCE(b.section_split_confidence, a.section_split_confidence) AS section_split_confidence,
                COALESCE(b.section_split_method, a.section_split_method) AS section_split_method,
                '1.1.0' AS parser_version,
                CURRENT_TIMESTAMP AS parsed_at,
                a.parse_run_id
            FROM read_parquet('{local_a}') a
            LEFT JOIN read_parquet('{local_b}') b
              ON  a.crd_number = b.crd_number
              AND a.version_id = b.version_id
        """
    else:
        # No stage_b — use stage_a columns directly
        item_selects = "\n    ".join(
            f"a.{col},"
            for n, col in item_cols.items()
        )
        join_sql = f"""
            SELECT
                a.crd_number,
                a.version_id,
                a.brochure_type,
                TRY_CAST(a.filing_date AS DATE) AS filing_date,
                a.r2_key,
                a.content_sha256,
                TRY_CAST(a.content_bytes AS BIGINT) AS content_bytes,
                TRY_CAST(a.page_count AS INTEGER) AS page_count,
                a.full_text_md,
                {item_selects}
                a.section_split_confidence,
                a.section_split_method,
                '1.1.0' AS parser_version,
                CURRENT_TIMESTAMP AS parsed_at,
                a.parse_run_id
            FROM read_parquet('{local_a}') a
        """

    LOG.info("building Arrow reader from DuckDB JOIN ...")
    t0 = time.time()

    storage_options = _lance_storage_options()
    metrics: dict = {"stage_a_rows": stage_a_count}

    with lance_commit_lock(DATASET_SLUG):
        reader = con.from_query(join_sql).to_arrow_reader(batch_size=50_000)
        LOG.info("writing Lance dataset (mode=overwrite) to %s ...", LANCE_URI)
        import lance
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_rows = ds.count_rows()
        LOG.info("wrote %d rows in %.1fs (version=%s)", lance_rows, write_dur, ds.version)
        metrics.update(lance_rows=lance_rows, lance_version=ds.version, write_seconds=round(write_dur, 1))

        # BTREE indexes on canonical PK columns per directive Smoke gates
        for col in ("crd_number", "version_id", "filing_date"):
            t_idx = time.time()
            LOG.info("  creating BTREE on %s ...", col)
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                LOG.info("    done in %.1fs", time.time() - t_idx)
            except Exception as exc:
                LOG.error("  BTREE on %s FAILED: %s", col, exc)
                raise

        # Compact + cleanup old versions
        LOG.info("  compact_files() ...")
        try:
            stats = ds.optimize.compact_files()
            LOG.info("    compact: %s", stats)
        except Exception as exc:
            LOG.warning("  compact_files non-fatal: %s", exc)

        LOG.info("  cleanup_old_versions(7d) ...")
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("    cleanup: %s", cleanup)
        except Exception as exc:
            LOG.warning("  cleanup_old_versions non-fatal: %s", exc)

    con.close()
    metrics["total_seconds"] = round(time.time() - t0, 1)
    return metrics


def register_polaris() -> None:
    """Register the Lance dataset in Polaris via the canonical helper."""
    here = Path(__file__).resolve().parent.parent  # apps/data-engine-x/
    helper = here / "scripts" / "init_polaris_lance_generic.py"
    cmd = [
        sys.executable, str(helper),
        "--namespace", POLARIS_NAMESPACE,
        "--table", POLARIS_TABLE,
        "--doc", POLARIS_DOC,
    ]
    LOG.info("registering Polaris: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, env=os.environ)
    LOG.info("Polaris registration complete")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release", required=True,
                    help="Release date YYYY-MM-DD (must match stage_a/stage_b Parquet keys)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset + register Polaris")
    grp.add_argument("--dry-run", action="store_true", help="count rows only, no write")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    if args.dry_run:
        import duckdb
        stage_a_key = f"sec-iapd/form-adv-part-2-parsed/release={args.release}/stage_a.parquet"
        local_a = f"/tmp/sec_iapd_stage_a_{args.release}.parquet"
        s3 = _r2_client()
        _download_parquet(s3, stage_a_key, local_a)
        con = duckdb.connect()
        count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{local_a}')").fetchone()[0]
        LOG.info("DRY RUN: stage_a has %d rows — would emit to %s", count, LANCE_URI)
        con.close()
        return 0

    metrics = emit(args.release)
    LOG.info("emit metrics: %s", metrics)

    lance_rows = metrics.get("lance_rows", 0)
    if lance_rows < 16000:
        LOG.error("FAIL: lance_rows=%d < 16000 (smoke gate floor)", lance_rows)
        return 1
    if lance_rows > 17700:
        LOG.error("FAIL: lance_rows=%d > 17700 (smoke gate ceiling)", lance_rows)
        return 1

    register_polaris()
    LOG.info("Stage C complete: lance_rows=%d", lance_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
