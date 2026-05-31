#!/usr/bin/env python3
"""Lance-on-R2 bridge: SAM entity ↔ blitz firmo via normalized website domain.

Re-attaches a SAM UEI to the blitz-API firmographic spine by joining on the
website host. `spines/firmo_blitzapi_lance` is domain-grain (one row per
normalized domain); `sam_gov/entities_lance` carries `entity_url` (SAM's
registered website) and `unique_entity_id` (the canonical 12-char UEI). The
bridge normalizes both hosts identically and joins exact-equality, producing a
**UEI-keyed, firmo-denormalized** dataset: one row per (UEI, domain) match,
carrying the full firmo payload inline.

This is the load-bearing re-association the operator scoped: blitz's own `uei`
column was untrusted input passthrough, so it rode the spine as lineage only.
Here UEI is DERIVED from SAM's authoritative `entity_url`↔UEI binding — as good
as current SAM, not a stale Modal guess. The output keys on `uei` (BTREE), so
the downstream join `usaspending.winners_recent_lance ⨝ this ON recipient_uei`
is single-hop, both sides indexed.

Grain / fan-out (faithful, NOT silently collapsed):
  - firmo is unique per normalized domain → at most one firmo record per match.
  - SAM fans out: many UEIs share one entity_url (wm.com → 446 SAM UEIs). Each
    such UEI is a DISTINCT corporate registration and legitimately inherits the
    domain's firmo — so all are emitted. `sam_uei_at_domain` carries the fan-out
    degree on every row so a consumer can threshold it (a query for "the firm"
    vs "all registrations under this parent domain") WITHOUT this script
    guessing. Generic super-hosts (facebook.com → 487 UEIs, sites.google.com,
    webmail, etc.) are auto-rejected at fan-out > COLLISION_THRESHOLD — the
    house tier rule — so they can't inject hundreds of unrelated UEIs onto one
    firmo key.

Inner join: a firmo domain with no SAM `entity_url` match (SAM's entity_url
empty, or the host simply isn't in SAM) does not appear. Per operator: fine for
now; the Overture/name-fuzzy fallback is a later derivation.

Domain normalization + validation: IDENTICAL to
build_bridge_sam_pdl_domain_lance.py (lower→trim→strip scheme→strip www→strip
path; DNS-shape regex, non-numeric). Reuses the shared `domain_exact` method
row — does NOT re-register the method/version (shared with sam_pdl_domain /
fmcsa_pdl); only registers the new bridge_name.

Output: s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_firmo_blitzapi_lance
        Polaris: bridges.sam_firmo_blitzapi_lance

Usage:
    doppler run --project hq-all --config prd -- bash -c \\
      'cd apps/data-engine-x && uv run python scripts/build_bridge_sam_firmo_blitzapi_lance.py --dry-run'
    doppler run --project hq-all --config prd -- bash -c \\
      'cd apps/data-engine-x && uv run python scripts/build_bridge_sam_firmo_blitzapi_lance.py --apply'
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

from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402
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
logger = logging.getLogger("build_bridge_sam_firmo_blitzapi_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sam_firmo_blitzapi_lance"
METHOD_NAME = "domain_exact"   # SHARED rule — do NOT re-register method/version
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sam_gov_entities_lance"
SOURCE_RIGHT = "spines_firmo_blitzapi_lance"

# R2 layout ------------------------------------------------------------------
SAM_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
FIRMO_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/spines/firmo_blitzapi_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_firmo_blitzapi_lance"
DATASET_SLUG = "sam_firmo_blitzapi_lance"

POLARIS_NAMESPACE = "bridges"
POLARIS_TABLE = "sam_firmo_blitzapi_lance"
POLARIS_DOC = (
    "SAM entity × blitz firmo via normalized website domain. Re-attaches the "
    "authoritative SAM UEI (unique_entity_id ↔ entity_url) to the domain-grain "
    "spines.firmo_blitzapi_lance, producing UEI-keyed denormalized firmo (one "
    "row per UEI×domain match). BTREE on uei → single-hop join to "
    "usaspending.winners_recent_lance on recipient_uei. sam_uei_at_domain "
    "carries SAM fan-out degree per row; generic super-hosts auto-rejected at "
    "fan-out > 50. Inner join — firmo domains absent from SAM entity_url drop."
)

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50  # SAM fan-out > 50 at a domain → rejected (super-host)
# Floor: firmo is ~118.6k domains; SAM entity_url is 55% filled with the live
# rejoin measured at 89.7% of blitz domains. Conservative floor well under the
# expected matched-UEI volume; HARD FAIL below it (guards a silent wipe).
MIN_ROWS_MATCHED = 60_000
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
    """Domain normalization — IDENTICAL to build_bridge_sam_pdl_domain_lance.py."""
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
    """Read SAM + firmo Lance datasets via PyLance scanner with projection."""
    import lance
    import pyarrow.compute as pc

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

    logger.info("opening spines/firmo_blitzapi_lance ...")
    firmo_ds = lance.dataset(FIRMO_LANCE_URI, storage_options=storage_options)
    firmo_arrow = firmo_ds.scanner(
        columns=[
            "domain_norm", "domain_raw", "company_name", "company_website",
            "company_linkedin_url", "industry", "size", "employees_on_linkedin",
            "founded_year", "company_type", "hq_city", "hq_state",
            "hq_country_code", "claimed_ueis", "claimed_uei_count",
            "source_task_type",
        ]
    ).to_table()
    rows_firmo = len(firmo_arrow)
    logger.info("  firmo_blitzapi_lance: %d rows", rows_firmo)

    return sam_arrow, firmo_arrow, rows_sam, rows_firmo


def _build_match_table(
    sam_arrow,
    firmo_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Normalize → SAM fan-out → join → reject super-hosts. TEMP TABLE bridge_match."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("sam_raw", sam_arrow)
    con.register("firmo_raw", firmo_arrow)

    sam_domain_expr = _normalize_domain_sql("entity_url")
    validate_sam = _domain_validation_sql("normalized_domain")
    # firmo.domain_norm is already canonicalized at spine-emit time, but
    # re-validate DNS shape so a malformed key can't pollute the join.
    validate_firmo = _domain_validation_sql("normalized_domain")

    logger.info("materializing sam_branded + firmo_validated ...")
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
           AND {validate_sam}
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE firmo_validated AS
        SELECT
            domain_norm AS normalized_domain,
            domain_raw, company_name, company_website, company_linkedin_url,
            industry, size, employees_on_linkedin, founded_year, company_type,
            hq_city, hq_state, hq_country_code, claimed_ueis, claimed_uei_count,
            source_task_type
        FROM firmo_raw
        WHERE domain_norm IS NOT NULL
          AND {validate_firmo}
        """
    )

    rows_sam_valid = con.execute("SELECT count(*) FROM sam_branded").fetchone()[0]
    rows_firmo_valid = con.execute("SELECT count(*) FROM firmo_validated").fetchone()[0]
    logger.info(
        "  sam_branded: %s | firmo_validated: %s",
        f"{rows_sam_valid:,}", f"{rows_firmo_valid:,}",
    )

    # SAM-side fan-out per domain (firmo is unique per domain → no firmo fanout).
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT normalized_domain, count(*) AS sam_uei_at_domain
        FROM sam_branded
        GROUP BY normalized_domain
        """
    )

    logger.info("joining SAM × firmo on normalized_domain ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            s.uei,
            s.normalized_domain                         AS domain_norm,
            f.domain_raw,
            s.sam_legal_name,
            s.sam_state,
            s.sam_url_raw,
            sf.sam_uei_at_domain,
            -- firmo payload (denormalized through) ----------------------------
            f.company_name,
            f.company_website,
            f.company_linkedin_url,
            f.industry,
            f.size,
            f.employees_on_linkedin,
            f.founded_year,
            f.company_type,
            f.hq_city,
            f.hq_state,
            f.hq_country_code,
            -- lineage from the spine -----------------------------------------
            f.claimed_ueis,
            f.claimed_uei_count,
            f.source_task_type                          AS firmo_source_task_type,
            -- match provenance -----------------------------------------------
            '{METHOD_NAME}'                             AS match_method,
            CASE
                WHEN sf.sam_uei_at_domain > {COLLISION_THRESHOLD} THEN 'rejected'
                WHEN sf.sam_uei_at_domain = 1                     THEN 'platinum'
                ELSE 'gold'
            END                                         AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'              AS generated_at,
            '{BRIDGE_VERSION}'                          AS bridge_version,
            '{bridge_run_id}'                           AS bridge_run_id
        FROM sam_branded s
        JOIN firmo_validated f USING (normalized_domain)
        JOIN sam_fanout     sf ON sf.normalized_domain = s.normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
        """
    )

    row_counts = con.execute(
        """
        SELECT
            count(*) AS rows_matched,
            count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
            count(*) FILTER (WHERE confidence_tier = 'gold')     AS rows_tier2,
            count(DISTINCT uei)         AS distinct_uei,
            count(DISTINCT domain_norm) AS distinct_domain
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
        "rows_tier3": 0,
        "distinct_uei": row_counts[3],
        "distinct_domain": row_counts[4],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Lance write inside the commit lock; BTREE on uei + domain_norm."""
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

        # uei is the load-bearing join key (→ winners_recent_lance.recipient_uei).
        # domain_norm indexed too (→ back to the firmo spine / future SAM-less joins).
        for col in ("uei", "domain_norm"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("  BTREE on %s created", col)
            except Exception as e:  # noqa: BLE001
                logger.warning("BTREE index on %s failed (non-fatal): %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:  # noqa: BLE001
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:  # noqa: BLE001
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

        logger.info("INDICES: %s", [i["name"] for i in ds.list_indices()])

    return lance_count


def _ensure_registry() -> None:
    """Register ONLY the new bridge_name row in ops.bridges.

    Reuses the shared `domain_exact` method (already registered by
    sam_pdl_domain / fmcsa_pdl). Do NOT call register_match_method /
    register_match_method_version — those rows are shared; re-registering with
    our column config would clobber the siblings' provenance. Precedent:
    build_bridge_sam_pdl_domain_lance.py._ensure_registry.
    """
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SAM entities × blitz firmo via website domain (Lance). Re-attaches "
            "authoritative SAM UEI to spines/firmo_blitzapi_lance; UEI-keyed "
            "denormalized firmo for single-hop join to usaspending winners."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + ledger + Polaris")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    ap.add_argument("--skip-polaris", action="store_true",
                    help="write Lance only; skip Polaris registration")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry + lock)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("inputs: %s + %s", SAM_LANCE_URI, FIRMO_LANCE_URI)
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
        sam_arrow, firmo_arrow, rows_sam, rows_firmo = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sam_arrow,
            firmo_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:           %s", f"{counts['rows_matched']:,}")
        logger.info("    platinum (1 UEI@dom):  %s", f"{counts['rows_tier1']:,}")
        logger.info("    gold     (2..%d UEIs): %s", COLLISION_THRESHOLD, f"{counts['rows_tier2']:,}")
        logger.info("  distinct_uei:           %s", f"{counts['distinct_uei']:,}")
        logger.info("  distinct_domain:        %s", f"{counts['distinct_domain']:,}")
        logger.info("  rejected (super-host):  %s", f"{counts['rows_collision_rejected']:,}")

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = f"HARD FAIL: rows_matched={counts['rows_matched']:,} < floor={MIN_ROWS_MATCHED:,}"
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info("DRY RUN — no writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_sam,
                "rows_right": rows_firmo,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": 0,
            },
        )
        if args.skip_polaris:
            logger.info("--skip-polaris set; skipping Polaris registration")
        else:
            register_or_update_polaris(
                namespace=POLARIS_NAMESPACE,
                table_name=POLARIS_TABLE,
                s3_uri=BRIDGE_LANCE_URI,
                docstring=POLARIS_DOC,
            )
            logger.info("polaris registered: %s.%s", POLARIS_NAMESPACE, POLARIS_TABLE)

        logger.info(
            "OK — run_id=%s  lance_rows=%d  distinct_uei=%d  duration=%.1fs",
            bridge_run_id, lance_count, counts["distinct_uei"], time.time() - t0,
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
