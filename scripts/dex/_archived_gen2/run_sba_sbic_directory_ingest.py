"""SBA SBIC Directory — one-shot ingest (CSV → R2 ZSTD Parquet → Lance).

The SBA SBIC Directory is a small reference dataset (~400 federally
licensed SBIC funds — lower-middle-market private credit supply side).
At this scale the Modal-hosted two-script split used by CA SoS / FL Sunbiz
is overkill: one local script does CSV transform → R2 upload → Lance emit
end-to-end.

Source: https://www.sba.gov/funding-programs/investment-capital/sbic-directory
Operator downloads the CSV manually (no public bulk endpoint discovered);
this script consumes the local file.

Pattern A discipline (per inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md):
  - lance_commit_lock("sba_sbic_directory_lance")
  - mode="overwrite"
  - BTREE on fund_name_normalized, manager_name_normalized, state
  - compact_files() + cleanup_old_versions(timedelta(days=7))

Lessons applied:
  - L41: source CSV is UTF-8 (verified at sniff time); no transcode needed.
  - L42: R2 upload uses ContentType only, NO Content-Encoding.
  - L50: ops.data_sources 5-col shape (see sibling migration).

Run:
  cd apps/data-engine-x && doppler run --project hq-all --config prd -- \\
      python3 scripts/run_sba_sbic_directory_ingest.py --apply \\
      --csv-path /Users/benjamincrane/Downloads/sbic_contacts.csv
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Ensure repo-root imports work when invoked as `python3 scripts/...`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

DATASET_SLUG = "sba_sbic_directory_lance"
SOURCE_PROVIDER = "sba_sbic_directory"
SOURCE_DOWNLOAD_URL = (
    "https://www.sba.gov/funding-programs/investment-capital/sbic-directory"
)

R2_BUCKET = "dex-raw-landing-zone"
R2_PARQUET_KEY_TEMPLATE = "sba-sbic-directory/snapshot={snapshot}/data.parquet"
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sba/sbic_directory_lance"
)

# Per-row floor — SBA publishes ~300-400 active SBICs; well above 100 baseline.
MIN_ROW_FLOOR = 100

# Tmp dirs Lance + DuckDB will use
TMP_LANCE = "/tmp/lance"
TMP_DUCKDB = "/tmp/lance/duckdb"

LOG = logging.getLogger("sba_sbic_directory")
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


_PHONE_DIGIT_RE = re.compile(r"\D+")


def _normalize_phone(raw: str | None) -> str | None:
    """US-style digits-only phone. Returns None for empty / <7-digit results."""
    if not raw:
        return None
    digits = _PHONE_DIGIT_RE.sub("", str(raw))
    if not digits or len(digits) < 7:
        return None
    # Strip leading US country code if 11-digit starting with 1
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _email_domain(raw: str | None) -> str | None:
    if not raw or "@" not in raw:
        return None
    dom = raw.split("@", 1)[1].strip().lower()
    return dom or None


def _connect_duckdb():
    import duckdb
    Path(TMP_DUCKDB).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{TMP_DUCKDB}'")
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
    # Register Python normalizers as DuckDB UDFs (per L34).
    # null_handling="special" lets the UDFs receive + emit NULL freely;
    # DuckDB's DEFAULT mode rejects None returns even on non-NULL input.
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    con.create_function(
        "py_normalize_phone",
        _normalize_phone,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    con.create_function(
        "py_email_domain",
        _email_domain,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    return con


# ---------------------------------------------------------------------------
# Transform: CSV → typed Parquet
# ---------------------------------------------------------------------------

def transform_csv_to_parquet(
    con,
    csv_path: Path,
    out_parquet: Path,
    snapshot: str,
) -> int:
    """Read SBA SBIC directory CSV, emit typed ZSTD Parquet locally.

    Returns the row count.
    """
    LOG.info("CSV → Parquet: %s → %s", csv_path, out_parquet)

    # SBA CSV columns (verified 2026-05-18):
    #   Name, City, State, Manager, Vintage Year, Fund Size,
    #   Average Investment, Investment Strategy, Fund Style,
    #   Making New Investments?, Investor Relations Name,
    #   Investor Relations Email, Investor Relations Phone
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE raw AS
        SELECT *
        FROM read_csv_auto(
            '{csv_path.as_posix()}',
            header = TRUE,
            all_varchar = TRUE,
            sample_size = -1
        )
        """
    )

    raw_count = con.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
    LOG.info("CSV rows: %d", raw_count)

    # Typed projection + normalized columns.
    # NOTE: column names from the CSV header are quoted-identifier-preserved
    # by DuckDB; we project explicitly to the snake_case storage schema.
    out_path = out_parquet.as_posix()
    con.execute(
        f"""
        COPY (
            SELECT
                "Name" AS fund_name,
                "City" AS city,
                "State" AS state,
                "Manager" AS manager,
                TRY_CAST("Vintage Year" AS INTEGER) AS vintage_year,
                TRY_CAST("Fund Size" AS BIGINT) AS fund_size_usd,
                TRY_CAST("Average Investment" AS BIGINT)
                    AS average_investment_usd,
                "Investment Strategy" AS investment_strategy,
                "Fund Style" AS fund_style,
                CASE
                    WHEN lower(trim("Making New Investments?")) = 'yes'
                        THEN TRUE
                    WHEN lower(trim("Making New Investments?")) = 'no'
                        THEN FALSE
                    ELSE NULL
                END AS making_new_investments,
                "Investor Relations Name" AS ir_name,
                "Investor Relations Email" AS ir_email,
                py_email_domain("Investor Relations Email")
                    AS ir_email_domain,
                "Investor Relations Phone" AS ir_phone,
                py_normalize_phone("Investor Relations Phone")
                    AS ir_phone_normalized,
                py_normalize_entity("Name") AS fund_name_normalized,
                py_normalize_entity("Manager") AS manager_name_normalized,
                DATE '{snapshot}' AS snapshot_date,
                '{SOURCE_PROVIDER}' AS source_provider,
                '{SOURCE_DOWNLOAD_URL}' AS source_download_url,
                now() AS ingested_at
            FROM raw
        )
        TO '{out_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )

    parquet_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{out_path}')"
    ).fetchone()[0]
    LOG.info("Parquet rows: %d", parquet_count)

    if parquet_count != raw_count:
        raise SystemExit(
            f"FAIL: CSV→Parquet row delta: csv={raw_count} parquet={parquet_count}"
        )
    if parquet_count < MIN_ROW_FLOOR:
        raise SystemExit(
            f"FAIL: parquet rows {parquet_count} below floor {MIN_ROW_FLOOR}"
        )

    return parquet_count


# ---------------------------------------------------------------------------
# R2 upload
# ---------------------------------------------------------------------------

def upload_to_r2(local_path: Path, key: str) -> None:
    """Upload Parquet to R2. ContentType only, NO Content-Encoding (L42)."""
    import boto3

    LOG.info("R2 upload: %s → s3://%s/%s", local_path, R2_BUCKET, key)
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    s3.upload_file(
        local_path.as_posix(),
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    size = local_path.stat().st_size
    LOG.info("R2 upload OK: %d bytes", size)


# ---------------------------------------------------------------------------
# Lance emit
# ---------------------------------------------------------------------------

def emit_lance(con, r2_key: str) -> dict:
    """Read uploaded Parquet from R2, write Lance with BTREEs."""
    import lance
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = TMP_LANCE
    Path(TMP_LANCE).mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    parquet_uri = f"r2://{R2_BUCKET}/{r2_key}"
    LOG.info("Lance input: %s", parquet_uri)
    LOG.info("Lance output: %s", LANCE_URI)

    parquet_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{parquet_uri}')"
    ).fetchone()[0]
    LOG.info("R2 parquet rows: %d", parquet_rows)

    storage_options = _lance_storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.from_query(
            f"SELECT * FROM read_parquet('{parquet_uri}')"
        ).to_arrow_reader(batch_size=10_000)

        LOG.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_rows = ds.count_rows()
        LOG.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_rows, write_dur, ds.version,
        )

        if lance_rows != parquet_rows:
            raise SystemExit(
                f"FAIL: lance/parquet row delta: "
                f"parquet={parquet_rows} lance={lance_rows}"
            )

        # BTREEs for downstream Pattern B / Pattern C joins.
        for col in (
            "fund_name_normalized",
            "manager_name_normalized",
            "state",
        ):
            t_idx = time.time()
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                LOG.info("BTREE on %s: OK (%.1fs)", col, time.time() - t_idx)
            except Exception as e:
                LOG.error("BTREE on %s FAILED: %s", col, e)
                raise

        # Optimize + cleanup
        try:
            stats = ds.optimize.compact_files()
            LOG.info("compact_files: %s", stats)
        except Exception as e:
            LOG.warning("compact_files non-fatal: %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("cleanup_old_versions: %s", cleanup)
        except Exception as e:
            LOG.warning("cleanup_old_versions non-fatal: %s", e)

    return {
        "parquet_rows": parquet_rows,
        "lance_rows": lance_rows,
        "lance_version": ds.version,
        "duration_s": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

def smoke(con) -> None:
    """Per-key Lance scanner lookup + aggregate row count via DuckDB-on-Lance."""
    import lance
    import pyarrow.compute as pc

    storage_options = _lance_storage_options()
    ds = lance.dataset(LANCE_URI, storage_options=storage_options)

    total = ds.count_rows()
    LOG.info("smoke: total rows = %d", total)

    # Per-key lookup smoke: scanner with filter on first row's normalized name.
    head = ds.scanner(columns=["fund_name_normalized"], limit=1).to_table()
    if head.num_rows == 0:
        raise SystemExit("FAIL: smoke head() returned 0 rows")
    sample_key = head.column("fund_name_normalized")[0].as_py()
    if not sample_key:
        # Skip-filter smoke if the first row's normalized key happens to be NULL
        LOG.warning("smoke: first-row fund_name_normalized is NULL; "
                    "skipping filter smoke")
    else:
        hit = ds.scanner(
            filter=pc.field("fund_name_normalized") == sample_key,
        ).to_table()
        LOG.info(
            "smoke filter on fund_name_normalized='%s' -> %d rows",
            sample_key, hit.num_rows,
        )
        if hit.num_rows == 0:
            raise SystemExit(
                f"FAIL: smoke filter on '{sample_key}' returned 0 rows"
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="SBA SBIC Directory ingest")
    ap.add_argument(
        "--csv-path",
        required=True,
        help="Local SBA SBIC CSV path "
             "(downloaded from sba.gov SBIC Directory)",
    )
    ap.add_argument(
        "--snapshot",
        default=date.today().isoformat(),
        help="YYYY-MM-DD snapshot label (default: today UTC)",
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="run end-to-end (CSV→R2→Lance)")
    grp.add_argument("--dry-run", action="store_true",
                     help="transform only; no R2 upload, no Lance write")
    args = ap.parse_args()

    csv_path = Path(args.csv_path).expanduser()
    if not csv_path.exists():
        LOG.error("FAIL: csv-path not found: %s", csv_path)
        return 64

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    Path(TMP_LANCE).mkdir(parents=True, exist_ok=True)
    Path(TMP_DUCKDB).mkdir(parents=True, exist_ok=True)

    snapshot = args.snapshot
    r2_key = R2_PARQUET_KEY_TEMPLATE.format(snapshot=snapshot)
    local_parquet = Path(TMP_LANCE) / f"sbic_directory_{snapshot}.parquet"

    con = _connect_duckdb()

    # 1. CSV → typed Parquet (local)
    rows = transform_csv_to_parquet(con, csv_path, local_parquet, snapshot)

    if args.dry_run:
        LOG.info("DRY RUN — produced local parquet %s (%d rows); stopping",
                 local_parquet, rows)
        return 0

    # 2. Upload to R2
    upload_to_r2(local_parquet, r2_key)

    # 3. Lance emit (DuckDB-on-R2 → Arrow reader → lance.write_dataset)
    metrics = emit_lance(con, r2_key)
    LOG.info("Lance emit: %s", metrics)

    # 4. Smoke
    smoke(con)

    LOG.info("=" * 60)
    LOG.info("OK — sba_sbic_directory_lance ingest complete")
    LOG.info("  snapshot=%s", snapshot)
    LOG.info("  parquet=s3://%s/%s", R2_BUCKET, r2_key)
    LOG.info("  lance=%s", LANCE_URI)
    LOG.info("  rows=%d", metrics["lance_rows"])
    LOG.info("")
    LOG.info("Next: register in Polaris:")
    LOG.info(
        "  doppler run --project hq-all --config prd -- python3 \\\n"
        "      apps/data-engine-x/scripts/init_polaris_lance_generic.py \\\n"
        "      --namespace sba --table sbic_directory_lance \\\n"
        '      --doc "SBA SBIC Directory '
        '— federally licensed lower-middle-market private credit funds"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
