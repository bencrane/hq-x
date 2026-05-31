#!/usr/bin/env python3
"""Lance-on-R2 bridge generator: FMCSA carrier ↔ SAM.gov entity via email/web domain.

Pattern B Lance bridge: reads carrier_essentials_lance (pre-materialized
email_domain_normalized column) and sam_gov/entities_lance (entity_url through
identical domain normalization), joins on normalized domain, fans out into
tier (platinum/gold/silver/rejected), writes a Lance dataset at
polaris-warehouse/bridges/fmcsa_sam_domain_lance with BTREE indexes on
dot_number AND uei.

Pattern: Arrow-bridge (lance.dataset.scanner → arrow → DuckDB tables → join
→ Lance write). Single-path Pattern B; no GLEIF parent layer.

Domain normalization SQL is IDENTICAL to build_bridge_sam_pdl_domain_lance.py:
    lower → trim → strip '^https?://' → strip '^www\\.' → strip '/.*$'
    validate via regex ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{2,}$
        AND not numeric-only

FMCSA join key: the pre-materialized email_domain_normalized column on
carrier_essentials_lance (built by build_fmcsa_carrier_essentials.py with
the canonical rule). Use it directly — do NOT re-derive from email_address.
Filter only on email_domain_normalized IS NOT NULL (and shape-valid). Per C8,
no personal-email pre-filter is applied: personal-email domains self-drop on the
join (no personal-email domain is a SAM entity_url), and any pathological
collision fans out >50 and is absorbed by the rejected tier before the write.

SAM join key: entity_url normalized with the same domain normalization SQL.

METHOD: domain_exact v1.0.0 — already registered in ops.match_methods and
ops.match_method_versions (shared with FMCSA-PDL and SAM-PDL bridges). REUSE
it: call register_bridge(...) ONLY. Do NOT call register_match_method_version
(it does ON CONFLICT DO UPDATE and would overwrite the shared config — same
precedent as build_bridge_sam_pdl_domain_lance.py).

Output: polaris-warehouse/bridges/fmcsa_sam_domain_lance

Floor: >= 44,000 rows (validator-calibrated 2026-05-20; ~59% of 74,804
estimated dry-run). HARD FAIL if rows_matched < MIN_ROWS_MATCHED.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow \\
        --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/build_bridge_fmcsa_sam_domain_lance.py --dry-run

    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow \\
        --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/build_bridge_fmcsa_sam_domain_lance.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_fmcsa_sam_domain_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "fmcsa_sam_domain_lance"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"  # existing shared version row — do NOT re-register
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "fmcsa_carrier_essentials_lance"
SOURCE_RIGHT = "sam_gov_entities_lance"

# R2 layout ------------------------------------------------------------------
FMCSA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
SAM_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sam_domain_lance"
)
DATASET_SLUG = "fmcsa_sam_domain_lance"

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50  # >50 fan-out on either side → rejected (dropped)
# HARD FAIL if rows_matched < floor — validator-calibrated 2026-05-20 (~59% of 74,804)
MIN_ROWS_MATCHED = 44_000
TMP_DIR = "/tmp/lance"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _normalize_domain_sql(raw_expr: str) -> str:
    """Domain normalization SQL — IDENTICAL to build_bridge_sam_pdl_domain_lance.py."""
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


def _domain_validation_sql(col: str) -> str:
    """Validation predicate: well-formed DNS shape AND not numeric-only."""
    return (
        f"{col} ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{{2,}}$' "
        f"AND NOT ({col} ~ '^[0-9.]+$')"
    )


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read FMCSA + SAM Lance datasets via PyLance scanner with column projection."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening fmcsa/carrier_essentials_lance ...")
    fmcsa_ds = lance.dataset(FMCSA_LANCE_URI, storage_options=storage_options)
    # Use the pre-materialized email_domain_normalized column directly — do NOT
    # re-derive from email_address. No personal-email pre-filter is applied (C8):
    # personal-email domains self-drop on the join or fan out >50 → rejected tier.
    fmcsa_arrow = fmcsa_ds.scanner(
        columns=[
            "dot_number",
            "email_domain_normalized",
            "legal_name",
            "phy_state",
        ],
        filter=pc.field("email_domain_normalized").is_valid(),
    ).to_table()
    rows_fmcsa = len(fmcsa_arrow)
    logger.info("  fmcsa (email_domain_normalized IS NOT NULL): %d rows", rows_fmcsa)

    logger.info("opening sam_gov/entities_lance ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_arrow = sam_ds.scanner(
        columns=[
            "unique_entity_id",
            "legal_business_name",
            "physical_address_state_normalized",
            "entity_url",
        ],
        filter=pc.field("entity_url").is_valid(),
    ).to_table()
    rows_sam = len(sam_arrow)
    logger.info("  sam entities (entity_url IS NOT NULL): %d rows", rows_sam)

    return fmcsa_arrow, sam_arrow, rows_fmcsa, rows_sam


def _build_match_table(
    fmcsa_arrow,
    sam_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Normalize → fan-out → tier; populate TEMP TABLE bridge_match."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("fmcsa_raw", fmcsa_arrow)
    con.register("sam_raw", sam_arrow)

    # FMCSA side: email_domain_normalized is already normalized by
    # build_fmcsa_carrier_essentials.py using the canonical rule. Still apply
    # the shape-validation predicate for parity with the SAM side.
    validate_fmcsa = _domain_validation_sql("email_domain_normalized")
    sam_domain_expr = _normalize_domain_sql("entity_url")
    validate_sam = _domain_validation_sql("normalized_domain")

    logger.info("materializing fmcsa_proj + sam_branded ...")
    con.execute(
        f"""
        CREATE TEMP TABLE fmcsa_proj AS
        SELECT
            dot_number,
            email_domain_normalized AS normalized_domain,
            legal_name AS fmcsa_legal_name,
            phy_state AS fmcsa_state
        FROM fmcsa_raw
        WHERE email_domain_normalized IS NOT NULL
          AND email_domain_normalized <> ''
          AND {validate_fmcsa}
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE sam_branded AS
        WITH sam AS (
            SELECT
                unique_entity_id AS uei,
                legal_business_name AS sam_legal_name,
                physical_address_state_normalized AS sam_state,
                entity_url AS sam_url_raw,
                {sam_domain_expr} AS normalized_domain
            FROM sam_raw
            WHERE entity_url IS NOT NULL AND entity_url != ''
        )
        SELECT *
          FROM sam
         WHERE normalized_domain IS NOT NULL
           AND normalized_domain != ''
           AND {validate_sam}
        """
    )

    rows_fmcsa_valid = con.execute("SELECT count(*) FROM fmcsa_proj").fetchone()[0]
    rows_sam_valid = con.execute("SELECT count(*) FROM sam_branded").fetchone()[0]
    logger.info(
        "  fmcsa_proj: %s | sam_branded: %s",
        f"{rows_fmcsa_valid:,}", f"{rows_sam_valid:,}",
    )

    logger.info("computing per-domain fan-out + tiered join ...")
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_fanout AS
        SELECT normalized_domain, count(*) AS fmcsa_carriers_at_domain
        FROM fmcsa_proj
        GROUP BY normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT normalized_domain, count(*) AS sam_entities_at_domain
        FROM sam_branded
        GROUP BY normalized_domain
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            f.dot_number,
            s.uei,
            '{METHOD_NAME}' AS match_method,
            f.normalized_domain AS match_value,
            ff.fmcsa_carriers_at_domain,
            sf.sam_entities_at_domain,
            CASE
                WHEN ff.fmcsa_carriers_at_domain > {COLLISION_THRESHOLD}
                  OR sf.sam_entities_at_domain > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.fmcsa_carriers_at_domain = 1
                  AND sf.sam_entities_at_domain = 1
                    THEN 'platinum'
                WHEN ff.fmcsa_carriers_at_domain = 1
                  OR sf.sam_entities_at_domain = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM fmcsa_proj f
        JOIN sam_branded s USING (normalized_domain)
        JOIN fmcsa_fanout ff ON ff.normalized_domain = f.normalized_domain
        JOIN sam_fanout   sf ON sf.normalized_domain = s.normalized_domain
        """
    )
    # Rejected rows are dropped before the write (C3) — bridge_match holds only
    # platinum + gold + silver rows.
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            dot_number, uei, match_method, match_value,
            confidence_tier, fmcsa_carriers_at_domain, sam_entities_at_domain,
            generated_at, bridge_version, bridge_run_id
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
        """
    )

    row_counts = con.execute(
        """
        SELECT
            count(*) AS rows_matched,
            count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
            count(*) FILTER (WHERE confidence_tier = 'gold')     AS rows_tier2,
            count(*) FILTER (WHERE confidence_tier = 'silver')   AS rows_tier3
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT count(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Lance write inside the commit lock; create BTREE on dot_number AND uei."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        # C4: BTREE scalar indexes on dot_number AND uei (both — this bridge
        # differs from SAM-PDL which only indexes uei).
        for col in ("dot_number", "uei"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("  BTREE on %s created", col)
            except Exception as e:
                logger.warning("BTREE index on %s failed (non-fatal): %s", col, e)

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def _ensure_registry() -> None:
    """Register ONLY the new bridge_name row in ops.bridges.

    IMPORTANT: do NOT call register_match_method or register_match_method_version.
    The domain_exact rule and its v1.0.0 version row are SHARED with the FMCSA-PDL
    and SAM-PDL bridges. The helpers do idempotent UPSERTs ON CONFLICT DO UPDATE,
    so calling register_match_method_version with this bridge's config would
    overwrite the shared fields used by the existing bridges and break their
    provenance trail. Precedent: build_bridge_sam_pdl_domain_lance.py.

    register_bridge is safe because bridge_name 'fmcsa_sam_domain_lance' is NEW.
    """
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "FMCSA motor carriers × SAM.gov entities via normalized email/web domain "
            "(Lance). FMCSA join key: pre-materialized email_domain_normalized. "
            "SAM join key: entity_url normalized with the same rule. "
            "Strategic payoff: identify FMCSA carriers that won federal contracts "
            "(FMCSA dot_number → SAM uei → USAspending recipient spine)."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + registry rows")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("inputs: %s + %s (Arrow-bridge)", FMCSA_LANCE_URI, SAM_LANCE_URI)
    logger.info("output: %s", BRIDGE_LANCE_URI)

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        fmcsa_arrow, sam_arrow, rows_fmcsa, rows_sam = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            fmcsa_arrow,
            sam_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %s", f"{counts['rows_matched']:,}")
        logger.info("    platinum (1:1):         %s", f"{counts['rows_tier1']:,}")
        logger.info("    gold     (1:N | N:1):   %s", f"{counts['rows_tier2']:,}")
        logger.info(
            "    silver   (N:M ≤%d):    %s",
            COLLISION_THRESHOLD, f"{counts['rows_tier3']:,}",
        )
        logger.info(
            "  rows_collision_rejected:  %s", f"{counts['rows_collision_rejected']:,}"
        )

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info(
                "DRY RUN — no Lance / Postgres writes. duration=%.1fs",
                time.time() - t0,
            )
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_fmcsa,
                "rows_right": rows_sam,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": 0,  # C8: no personal-email pre-filter
            },
        )
        logger.info(
            "OK — run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        logger.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
