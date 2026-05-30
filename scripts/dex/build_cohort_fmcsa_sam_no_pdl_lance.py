"""Materialize the FMCSA carriers — SAM-matched but no PDL — cohort.

Source funnel
-------------
* ``bridges/fmcsa_sam_legal_name_state_lance`` — FMCSA carriers joined to
  SAM entities via normalized legal_name + state (PR #774).
* ``bridges/sam_pdl_lance`` — SAM ∩ PDL match table; presence of a UEI
  here means the SAM corporate website (or PDL website) landed a PDL
  company and therefore a ``pdl_linkedin_url`` (100% non-null on this
  bridge).

The "domain but no LinkedIn through this chain" segment is the set of
FMCSA × SAM UEIs that satisfy:

    sam_entity_url IS NOT NULL              -- have a domain to enrich
    AND uei NOT IN bridges/sam_pdl_lance    -- but PDL didn't catch it

Per the funnel pulse-check (FMCSA × SAM × USAspending):
    93,261 chain DOTs (stage 1)
    55,420 with sam_entity_url (stage 2)
    41,254 with SAM→PDL match landing a pdl_linkedin_url (stage 3 & 4)
        ↳ stage 2 − stage 3 ≈ 14K DOTs ≈ 13.8K distinct UEIs go to
          Parallel.ai for fallback domain → linkedin resolution.

Cohort emit
-----------
Output: ``s3://dex-raw-landing-zone/polaris-warehouse/cohorts/fmcsa_sam_no_pdl_lance``

Schema (strict superset of ``cohorts/primes_90d_slow`` — first two columns
are ``uei`` + ``domain``, matching the orchestrator's existing
``run_parallel_domain_to_linkedin.py`` scanner contract):

    uei                       string   (cohort key; BTREE indexed)
    domain                    string   (normalized; ready for Parallel.ai)
    sam_entity_url            string   (raw URL; audit)
    dot_number                string   (one DOT per UEI — MIN if fan-out)
    dot_count                 int64    (number of DOTs sharing this UEI)
    sam_legal_business_name   string   (audit / QA)
    cohort_version            string   ('1.0.0')
    generated_at              timestamp[us, UTC]
    source_bridges            string   (provenance)

Domain normalization matches the canonical pattern in
``build_bridge_sam_pdl_domain_lance._normalize_domain_sql``: lower → strip
``^https?://`` → strip ``^www\\.`` → strip ``[/?#].*$``. Rows whose
normalized domain is null/empty after the strip are dropped at emit time.

A small junk filter drops government/state portal domains (``.gov``,
``.mil``, ``*.state.*.us``) that occasionally appear as a SAM
entity_url for an entity that's actually a private contractor referring
to a state agency in their listing.

Re-runnable (mode="overwrite"). BTREE on ``uei`` + Polaris registration.

Usage
-----
    cd apps/data-engine-x
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/build_cohort_fmcsa_sam_no_pdl_lance.py --dry-run
    doppler run -p hq-all -c prd -- \\
        uv run python scripts/build_cohort_fmcsa_sam_no_pdl_lance.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
_DEX_ROOT = _THIS.parent.parent
if str(_DEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEX_ROOT))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build_cohort_fmcsa_sam_no_pdl")

# ---------------------------------------------------------------------------
# Sources / output
# ---------------------------------------------------------------------------

SOURCE_FMCSA_SAM_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "bridges/fmcsa_sam_legal_name_state_lance"
)
SOURCE_SAM_PDL_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_lance"
)

COHORT_SLUG = "fmcsa_sam_no_pdl_lance"
COHORT_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/cohorts/fmcsa_sam_no_pdl_lance"
)

COHORT_VERSION = "1.0.0"
SOURCE_BRIDGES = "fmcsa_sam_legal_name_state_lance×sam_pdl_lance"

TMP_DIR = "/tmp/lance"


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


# ---------------------------------------------------------------------------
# Normalization (DuckDB SQL — identical to
# build_bridge_sam_pdl_domain_lance._normalize_domain_sql)
# ---------------------------------------------------------------------------

_NORMALIZE_DOMAIN_SQL = """
    NULLIF(
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    lower(trim({col})),
                    '^https?://', ''
                ),
                '^www\\.', ''
            ),
            '[/?#].*$', ''
        ),
        ''
    )
"""

_GOV_JUNK_REGEX = r"(\.gov|\.mil|\.state\.[a-z]{2}\.us)$"


def _normalize_domain_expr(col: str) -> str:
    return _NORMALIZE_DOMAIN_SQL.format(col=col).strip()


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------

def _scan_fmcsa_sam(storage_options: dict[str, str]):
    """Scan the FMCSA × SAM bridge for the columns we need."""
    import lance

    ds = lance.dataset(SOURCE_FMCSA_SAM_URI, storage_options=storage_options)
    t0 = time.perf_counter()
    tbl = ds.scanner(
        columns=[
            "dot_number",
            "uei",
            "sam_entity_url",
            "sam_legal_business_name",
        ],
    ).to_table()
    logger.info(
        "fmcsa_sam_legal_name_state scan: %d rows in %dms (version=%s)",
        tbl.num_rows, int((time.perf_counter() - t0) * 1000), ds.version,
    )
    return tbl


def _scan_sam_pdl_ueis(storage_options: dict[str, str]):
    """Scan sam_pdl_lance for just the UEI column (anti-join key)."""
    import lance

    ds = lance.dataset(SOURCE_SAM_PDL_URI, storage_options=storage_options)
    t0 = time.perf_counter()
    tbl = ds.scanner(columns=["uei"]).to_table()
    logger.info(
        "sam_pdl_lance scan (uei only): %d rows in %dms (version=%s)",
        tbl.num_rows, int((time.perf_counter() - t0) * 1000), ds.version,
    )
    return tbl


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _build_cohort_rel(
    fmcsa_sam_tbl,
    sam_pdl_tbl,
    *,
    generated_at_iso: str,
):
    """Filter, normalize, anti-join, dedupe — return DuckDB relation."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='4GB'")
    con.register("fs", fmcsa_sam_tbl)
    con.register("sp_ueis", sam_pdl_tbl)

    domain_expr = _normalize_domain_expr("sam_entity_url")

    con.execute(
        f"""
        CREATE TEMP TABLE candidates AS
        SELECT
            uei,
            dot_number,
            sam_entity_url,
            sam_legal_business_name,
            {domain_expr} AS normalized_domain
        FROM fs
        WHERE uei IS NOT NULL
          AND sam_entity_url IS NOT NULL
          AND sam_entity_url <> ''
        """
    )
    cand_count = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    cand_ueis = con.execute("SELECT COUNT(DISTINCT uei) FROM candidates").fetchone()[0]
    cand_dots = con.execute("SELECT COUNT(DISTINCT dot_number) FROM candidates").fetchone()[0]
    logger.info(
        "candidates (uei + sam_entity_url IS NOT NULL): rows=%d distinct_uei=%d distinct_dot=%d",
        cand_count, cand_ueis, cand_dots,
    )

    con.execute(
        """
        CREATE TEMP TABLE no_pdl AS
        SELECT c.*
        FROM candidates c
        LEFT JOIN (SELECT DISTINCT uei FROM sp_ueis) sp ON sp.uei = c.uei
        WHERE sp.uei IS NULL
        """
    )
    no_pdl_count = con.execute("SELECT COUNT(*) FROM no_pdl").fetchone()[0]
    no_pdl_ueis = con.execute("SELECT COUNT(DISTINCT uei) FROM no_pdl").fetchone()[0]
    no_pdl_dots = con.execute("SELECT COUNT(DISTINCT dot_number) FROM no_pdl").fetchone()[0]
    logger.info(
        "after anti-join vs sam_pdl_lance: rows=%d distinct_uei=%d distinct_dot=%d",
        no_pdl_count, no_pdl_ueis, no_pdl_dots,
    )

    con.execute(
        f"""
        CREATE TEMP TABLE filtered AS
        SELECT *
        FROM no_pdl
        WHERE normalized_domain IS NOT NULL
          AND NOT regexp_matches(normalized_domain, '{_GOV_JUNK_REGEX}')
        """
    )
    filt_count = con.execute("SELECT COUNT(*) FROM filtered").fetchone()[0]
    filt_ueis = con.execute("SELECT COUNT(DISTINCT uei) FROM filtered").fetchone()[0]
    logger.info(
        "after normalization + gov-junk filter: rows=%d distinct_uei=%d (dropped %d rows for null/junk domain)",
        filt_count, filt_ueis, no_pdl_count - filt_count,
    )

    # Collapse to one row per UEI: take MIN(dot_number) and capture
    # dot_count for provenance. sam_entity_url / legal_name come from
    # the same MIN-dot row.
    cohort_rel = con.from_query(
        f"""
        WITH ranked AS (
            SELECT
                uei,
                normalized_domain AS domain,
                sam_entity_url,
                dot_number,
                sam_legal_business_name,
                ROW_NUMBER() OVER (
                    PARTITION BY uei
                    ORDER BY dot_number
                ) AS rn,
                COUNT(*) OVER (PARTITION BY uei) AS dot_count
            FROM filtered
        )
        SELECT
            uei,
            domain,
            sam_entity_url,
            dot_number,
            CAST(dot_count AS BIGINT) AS dot_count,
            sam_legal_business_name,
            '{COHORT_VERSION}' AS cohort_version,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{SOURCE_BRIDGES}' AS source_bridges
        FROM ranked
        WHERE rn = 1
        """
    )
    final_count = con.execute(
        "SELECT COUNT(DISTINCT uei) FROM (SELECT * FROM filtered)"
    ).fetchone()[0]
    logger.info("cohort grain = 1 row per UEI; final distinct UEIs = %d", final_count)
    return con, cohort_rel, final_count


def _write_lance(
    rel,
    *,
    storage_options: dict[str, str],
) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(COHORT_SLUG):
        logger.info("writing cohort Lance at %s ...", COHORT_URI)
        reader = rel.to_arrow_reader(batch_size=50_000)
        ds = lance.write_dataset(
            reader,
            COHORT_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        row_count = ds.count_rows()
        logger.info(
            "wrote slug=%s rows=%d in %.1fs (version=%s)",
            COHORT_SLUG, row_count, write_dur, ds.version,
        )

        ds.create_scalar_index("uei", index_type="BTREE", replace=True)
        logger.info("BTREE on uei: OK")

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)
    return row_count


def _register_polaris() -> None:
    register_or_update_polaris(
        namespace="cohorts",
        table_name=COHORT_SLUG,
        s3_uri=COHORT_URI.rstrip("/") + "/",
        docstring=(
            "FMCSA carriers with SAM entity_url but no PDL match — "
            "fallback domain→linkedin enrichment target for the "
            "parallel_domain_to_linkedin orchestrator."
        ),
    )
    logger.info("Polaris registration: OK")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="Write the Lance cohort + register in Polaris.")
    grp.add_argument("--dry-run", action="store_true",
                     help="Log counts only; no R2 write.")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    storage_options = _r2_storage_options()
    started_at = datetime.now(tz=timezone.utc)

    fmcsa_sam_tbl = _scan_fmcsa_sam(storage_options)
    sam_pdl_tbl = _scan_sam_pdl_ueis(storage_options)
    con, cohort_rel, distinct_ueis = _build_cohort_rel(
        fmcsa_sam_tbl, sam_pdl_tbl,
        generated_at_iso=started_at.isoformat(),
    )

    if args.dry_run:
        sample = con.execute(
            "SELECT uei, normalized_domain, sam_legal_business_name "
            "FROM filtered LIMIT 5"
        ).fetchall()
        for row in sample:
            logger.info("  sample uei=%s domain=%s legal=%s", *row)
        print(f"COHORT_DISTINCT_UEI: {distinct_ueis}")
        return 0

    row_count = _write_lance(cohort_rel, storage_options=storage_options)
    _register_polaris()
    print(f"COHORT_ROW_COUNT: {row_count}")
    print(f"COHORT_URI: {COHORT_URI}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
