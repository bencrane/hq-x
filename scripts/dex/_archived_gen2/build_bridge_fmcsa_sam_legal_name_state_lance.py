#!/usr/bin/env python3
"""DuckDB bridge generator: FMCSA carrier × SAM entity by legal name + state (Lance).

Pattern B exact-match bridge between FMCSA motor carriers
(carrier_essentials_lance, 4.4M rows nationwide) and SAM.gov registered
entities (sam_gov/entities_lance, ~884K rows). Joins on the normalized
legal name + 2-letter US state across both sides — the standard
`name_state_exact` v1.0.0 method already in use by SAM × SBA, FEC × SBA,
PDL × SBA, FMCSA × UCC, etc.

The existing `fmcsa_sam_domain_lance` bridge (domain_exact) only catches the
~63K carriers whose `email_address` shares a domain with the SAM entity's
`entity_url`. Name+state recovers the carriers that simply don't expose a
contact email matching the SAM entity URL — a much larger population. Sample
sizing probe (2026-05-26, normalized-column read): name+state matches ~84K
distinct DOTs / ~80K distinct UEIs at 91.7% unambiguous 1:1, vs domain's
63K / 51K. Net expansion: ~32% more carrier coverage.

Reads
-----
  FMCSA: s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance
         (re-normalize raw `legal_name` — pre-materialized `legal_name_normalized`
         is 99.9786% parity only; old normalizer revision lacks the
         GENERIC_NON_ENTITY_STRINGS blacklist. Same discipline as
         build_bridge_fmcsa_sos_ca_owner_lance.py.)
  SAM:   s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance
         (re-normalize raw `legal_business_name` — pre-materialized
         `legal_business_name_normalized` measured 10.9% divergence vs canonical
         normalizer on a 200-row sample, per build_bridge_sam_sba_borrower.py.)

Method
------
  name_state_exact v1.0.0 — REUSED. Row in ops.match_methods +
  ops.match_method_versions is SHARED across SAM × SBA, FEC × SBA, etc.
  This script ONLY calls register_bridge + start_bridge_run +
  complete_bridge_run + fail_bridge_run. Method/version helpers are
  INTENTIONALLY NOT CALLED — the idempotent UPSERT would otherwise overwrite
  the shared config with this bridge's `input_columns_*` shape and corrupt
  upstream consumers' provenance trail. Precedent: SOS-CA bridge.

Tier rule
---------
  platinum = (name+state) is 1:1                       (highest confidence)
  gold     = (name+state) is 1:N or N:1                (one side unique)
  silver   = (name+state) is N:M with both fan-outs ≤ COLLISION_THRESHOLD
  rejected = either fan-out > COLLISION_THRESHOLD      (dropped before write)

Output
------
  s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sam_legal_name_state_lance/
  Dual BTREE: dot_number AND uei.

Row floor
---------
  MIN_ROWS_MATCHED = 75_000.
  Sizing probe at name+state (pre-materialized columns; re-normalization will
  shift slightly): 77,418 distinct keys × tiered join product ≈ 85-100K rows
  pre-rejection. Floor at 75K catches a catastrophically broken join while
  allowing ~10% snapshot/normalizer drift downward.

Usage
-----
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_fmcsa_sam_legal_name_state_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_fmcsa_sam_legal_name_state_lance.py --dry-run
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

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)
# NOTE: register_match_method + register_match_method_version are
# INTENTIONALLY NOT imported. The name_state_exact v1.0.0 row in
# ops.match_methods / ops.match_method_versions is SHARED across many
# bridges. Re-registering here would UPSERT and corrupt the shared config.
# Same discipline as build_bridge_fmcsa_sos_ca_owner_lance.py.

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_fmcsa_sam_legal_name_state_lance")


BRIDGE_NAME = "fmcsa_sam_legal_name_state"
METHOD_NAME = "name_state_exact"  # REUSED
METHOD_SEMVER = "1.0.0"           # REUSED
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "fmcsa_carrier_essentials_lance"
SOURCE_RIGHT = "sam_entities_lance"

FMCSA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
SAM_ENTITIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sam_legal_name_state_lance"
)
DATASET_SLUG = "fmcsa_sam_legal_name_state_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 75_000

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


def _normalize_entity_sql(raw_expr: str) -> str:
    """SQL equivalent of _lib/entity_name_normalize.py v1.0.0.

    Mirrors the macro used by build_bridge_sam_sba_borrower.py — same suffix
    set, same punctuation strip, same whitespace collapse. Returns NULL for
    empty/whitespace-only inputs. The Python module's GENERIC_NON_ENTITY_STRINGS
    blacklist isn't applied here (1.0.0 SQL parity matches SAM-SBA); rare
    generic strings show up as small isolated clusters rather than mass-collision
    rejections, and the fan-out >50 rejected tier catches the worst offenders.
    """
    suffixes = (
        "incorporated|corporation|company|limited|"
        "pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    )
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          ELSE NULLIF(
            trim(
              regexp_replace(
                regexp_replace(
                  regexp_replace(
                    lower(trim({raw_expr})),
                    '\\b({suffixes})\\b\\.?',
                    ' ',
                    'g'
                  ),
                  '[^\\w\\s]+',
                  ' ',
                  'g'
                ),
                '\\s+',
                ' ',
                'g'
              )
            ),
            ''
          )
        END
    """.strip()


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read both Lance datasets via Arrow-bridge."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening %s ...", FMCSA_LANCE_URI)
    fmcsa_ds = lance.dataset(FMCSA_LANCE_URI, storage_options=storage_options)
    fmcsa_cols = [
        "dot_number",
        "legal_name",
        "dba_name",
        "phy_state",
        "phy_city",
        "phy_zip",
        "phy_country",
        "carrier_mailing_state",
        "carrier_mailing_city",
        "carrier_mailing_zip",
        "email_domain_normalized",
        "status_code",
        "carrier_operation",
        "power_units",
        "total_drivers",
    ]
    available = {f.name for f in fmcsa_ds.schema}
    fmcsa_scan_cols = [c for c in fmcsa_cols if c in available]
    fmcsa_arrow = fmcsa_ds.scanner(
        columns=fmcsa_scan_cols,
        filter=pc.is_valid(pc.field("legal_name")),
    ).to_table()
    rows_left = len(fmcsa_arrow)
    logger.info("  fmcsa: %d rows (legal_name non-null)", rows_left)

    logger.info("opening %s ...", SAM_ENTITIES_LANCE_URI)
    sam_ds = lance.dataset(SAM_ENTITIES_LANCE_URI, storage_options=storage_options)
    sam_cols = [
        "unique_entity_id",
        "legal_business_name",
        "dba_name",
        "physical_address_state_normalized",
        "physical_address_city",
        "physical_address_zip5",
        "physical_address_country_code",
        "mailing_address_state_or_province",
        "mailing_address_city",
        "mailing_address_zip5",
        "entity_url",
        "cage_code",
        "primary_naics",
        "bus_type_string",
        "sba_business_types_string",
        "entity_structure",
        "state_of_incorporation",
        "registration_expiration_date",
        "last_update_date",
    ]
    sam_available = {f.name for f in sam_ds.schema}
    sam_scan_cols = [c for c in sam_cols if c in sam_available]
    sam_arrow = sam_ds.scanner(
        columns=sam_scan_cols,
        filter=pc.is_valid(pc.field("legal_business_name")),
    ).to_table()
    rows_right = len(sam_arrow)
    logger.info("  sam:   %d rows (legal_business_name non-null)", rows_right)

    return fmcsa_arrow, sam_arrow, rows_left, rows_right


def _build_match_table(
    fmcsa_arrow,
    sam_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run the (name+state) JOIN + symmetric fan-out tiering in DuckDB."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")

    con.register("fmcsa_raw", fmcsa_arrow)
    con.register("sam_raw", sam_arrow)

    fmcsa_norm = _normalize_entity_sql("legal_name")
    sam_norm = _normalize_entity_sql("legal_business_name")

    # FMCSA side — re-normalize legal_name fresh; derive zip5 from raw zip.
    con.execute(
        f"""
        CREATE TEMP TABLE fmcsa_branded AS
        SELECT
            dot_number                                                 AS dot_number,
            legal_name                                                 AS fmcsa_legal_name,
            ({fmcsa_norm})                                             AS fmcsa_name_normalized,
            upper(trim(phy_state))                                     AS fmcsa_state,
            substr(regexp_replace(phy_zip, '[^0-9]', '', 'g'), 1, 5)   AS fmcsa_zip5,
            phy_city                                                   AS fmcsa_city,
            phy_country                                                AS fmcsa_country,
            dba_name                                                   AS fmcsa_dba_name,
            substr(regexp_replace(carrier_mailing_zip, '[^0-9]', '', 'g'), 1, 5)
                                                                       AS fmcsa_mailing_zip5,
            upper(trim(carrier_mailing_state))                         AS fmcsa_mailing_state,
            carrier_mailing_city                                       AS fmcsa_mailing_city,
            email_domain_normalized                                    AS fmcsa_email_domain_normalized,
            status_code                                                AS fmcsa_status_code,
            carrier_operation                                          AS fmcsa_carrier_operation,
            power_units                                                AS fmcsa_power_units,
            total_drivers                                              AS fmcsa_total_drivers
        FROM fmcsa_raw
        WHERE ({fmcsa_norm}) IS NOT NULL
          AND phy_state IS NOT NULL
          AND length(trim(phy_state)) = 2
        """
    )
    rows_fmcsa = con.execute("SELECT COUNT(*) FROM fmcsa_branded").fetchone()[0]
    logger.info("  fmcsa_branded (post-normalize, state 2-char): %d", rows_fmcsa)

    # SAM side — re-normalize legal_business_name fresh; US-only.
    con.execute(
        f"""
        CREATE TEMP TABLE sam_branded AS
        SELECT
            unique_entity_id                                           AS uei,
            legal_business_name                                        AS sam_legal_business_name,
            ({sam_norm})                                               AS sam_name_normalized,
            upper(trim(physical_address_state_normalized))             AS sam_state,
            physical_address_zip5                                      AS sam_zip5,
            physical_address_city                                      AS sam_city,
            physical_address_country_code                              AS sam_country,
            dba_name                                                   AS sam_dba_name,
            mailing_address_zip5                                       AS sam_mailing_zip5,
            upper(trim(mailing_address_state_or_province))             AS sam_mailing_state,
            mailing_address_city                                       AS sam_mailing_city,
            entity_url                                                 AS sam_entity_url,
            cage_code                                                  AS sam_cage_code,
            primary_naics                                              AS sam_primary_naics,
            bus_type_string                                            AS sam_bus_type_string,
            sba_business_types_string                                  AS sam_sba_business_types_string,
            entity_structure                                           AS sam_entity_structure,
            state_of_incorporation                                     AS sam_state_of_incorporation,
            registration_expiration_date                               AS sam_registration_expiration_date,
            last_update_date                                           AS sam_last_update_date
        FROM sam_raw
        WHERE ({sam_norm}) IS NOT NULL
          AND physical_address_state_normalized IS NOT NULL
          AND length(trim(physical_address_state_normalized)) = 2
          AND (physical_address_country_code IS NULL
               OR physical_address_country_code = 'USA')
        """
    )
    rows_sam = con.execute("SELECT COUNT(*) FROM sam_branded").fetchone()[0]
    logger.info("  sam_branded   (post-normalize, US, state 2-char): %d", rows_sam)

    # Symmetric fan-out tables (each side's cardinality at (name, state)).
    logger.info("computing fan-out tables ...")
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_fanout AS
        SELECT fmcsa_name_normalized AS norm_name, fmcsa_state AS state,
               COUNT(DISTINCT dot_number) AS fmcsa_fan_out
        FROM fmcsa_branded
        GROUP BY 1, 2
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sam_name_normalized AS norm_name, sam_state AS state,
               COUNT(DISTINCT uei) AS sam_fan_out
        FROM sam_branded
        GROUP BY 1, 2
        """
    )

    logger.info("computing tiered JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            f.dot_number,
            s.uei,
            f.fmcsa_name_normalized                                    AS match_value_normalized,
            f.fmcsa_state                                              AS match_state,
            CASE
                WHEN ff.fmcsa_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sam_fan_out   > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.fmcsa_fan_out = 1 AND sf.sam_fan_out = 1
                    THEN 'platinum'
                WHEN ff.fmcsa_fan_out = 1 OR  sf.sam_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                                        AS confidence_tier,
            ff.fmcsa_fan_out,
            sf.sam_fan_out,
            -- zip5 evidence (operator-discussed; not part of the join key)
            CASE
                WHEN f.fmcsa_zip5 IS NOT NULL AND s.sam_zip5 IS NOT NULL
                 AND length(f.fmcsa_zip5) = 5 AND length(s.sam_zip5) = 5
                 AND f.fmcsa_zip5 = s.sam_zip5
                  THEN TRUE
                WHEN f.fmcsa_zip5 IS NOT NULL AND s.sam_zip5 IS NOT NULL
                 AND length(f.fmcsa_zip5) = 5 AND length(s.sam_zip5) = 5
                  THEN FALSE
                ELSE NULL
            END                                                        AS zip5_physical_match,
            f.fmcsa_legal_name,
            f.fmcsa_dba_name,
            f.fmcsa_zip5,
            f.fmcsa_city,
            f.fmcsa_country,
            f.fmcsa_mailing_zip5,
            f.fmcsa_mailing_state,
            f.fmcsa_mailing_city,
            f.fmcsa_email_domain_normalized,
            f.fmcsa_status_code,
            f.fmcsa_carrier_operation,
            f.fmcsa_power_units,
            f.fmcsa_total_drivers,
            s.sam_legal_business_name,
            s.sam_dba_name,
            s.sam_zip5,
            s.sam_city,
            s.sam_country,
            s.sam_mailing_zip5,
            s.sam_mailing_state,
            s.sam_mailing_city,
            s.sam_entity_url,
            s.sam_cage_code,
            s.sam_primary_naics,
            s.sam_bus_type_string,
            s.sam_sba_business_types_string,
            s.sam_entity_structure,
            s.sam_state_of_incorporation,
            s.sam_registration_expiration_date,
            s.sam_last_update_date,
            '{METHOD_NAME}'                                            AS match_method,
            '{BRIDGE_VERSION}'                                         AS bridge_version,
            '{bridge_run_id}'                                          AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'                             AS generated_at
        FROM fmcsa_branded f
        JOIN sam_branded s
          ON f.fmcsa_name_normalized = s.sam_name_normalized
         AND f.fmcsa_state           = s.sam_state
        JOIN fmcsa_fanout ff
          ON ff.norm_name = f.fmcsa_name_normalized AND ff.state = f.fmcsa_state
        JOIN sam_fanout sf
          ON sf.norm_name = s.sam_name_normalized   AND sf.state = s.sam_state
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
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE confidence_tier='platinum'),
               COUNT(*) FILTER (WHERE confidence_tier='gold'),
               COUNT(*) FILTER (WHERE confidence_tier='silver'),
               COUNT(DISTINCT dot_number),
               COUNT(DISTINCT uei),
               COUNT(*) FILTER (WHERE zip5_physical_match = TRUE),
               COUNT(*) FILTER (WHERE zip5_physical_match = FALSE)
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1_platinum": row_counts[1],
        "rows_tier2_gold": row_counts[2],
        "rows_tier3_silver": row_counts[3],
        "distinct_dots_matched": row_counts[4],
        "distinct_ueis_matched": row_counts[5],
        "rows_zip5_match_true": row_counts[6],
        "rows_zip5_match_false": row_counts[7],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance; build dual BTREE on (dot_number, uei)."""
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
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version,
        )

        try:
            ds.create_scalar_index("dot_number", index_type="BTREE", replace=True)
            logger.info("BTREE on dot_number: OK")
        except Exception as e:
            logger.error("BTREE on dot_number FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("uei", index_type="BTREE", replace=True)
            logger.info("BTREE on uei: OK")
        except Exception as e:
            logger.error("BTREE on uei FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s  method=%s v%s (REUSED)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("normalizer: _lib/entity_name_normalize.py v%s (SQL macro)", NORMALIZER_VERSION)
    logger.info("inputs : %s + %s", FMCSA_LANCE_URI, SAM_ENTITIES_LANCE_URI)
    logger.info("output : %s", BRIDGE_LANCE_URI)

    run_uuid = None
    bridge_run_id = "00000000-0000-0000-0000-000000000000"
    if args.apply:
        register_bridge(
            bridge_name=BRIDGE_NAME,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            method_name=METHOD_NAME,
            r2_output_prefix=BRIDGE_LANCE_URI,
            description=(
                "FMCSA carrier × SAM entity by normalized legal name + 2-letter US "
                "state. Reuses the shared name_state_exact v1.0.0 method. Both "
                "sides re-normalize the raw name (FMCSA pre-materialized "
                "legal_name_normalized is 99.9786% parity; SAM pre-materialized "
                "legal_business_name_normalized measured 10.9% divergent). "
                "Complements the domain-only fmcsa_sam_domain_lance bridge — name "
                "+state extends carrier coverage by ~32% over domain alone."
            ),
        )
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
        fmcsa_arrow, sam_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            fmcsa_arrow, sam_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:             %d", counts["rows_matched"])
        logger.info("    platinum (1:1):          %d", counts["rows_tier1_platinum"])
        logger.info("    gold     (1:N | N:1):    %d", counts["rows_tier2_gold"])
        logger.info("    silver   (N:M ≤%d):      %d", COLLISION_THRESHOLD, counts["rows_tier3_silver"])
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])
        logger.info("  distinct_dots_matched:    %d", counts["distinct_dots_matched"])
        logger.info("  distinct_ueis_matched:    %d", counts["distinct_ueis_matched"])
        logger.info("  zip5_match=TRUE:          %d", counts["rows_zip5_match_true"])
        logger.info("  zip5_match=FALSE:         %d", counts["rows_zip5_match_false"])

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
            logger.info("DRY RUN — no Lance / Postgres writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1_platinum"],
                "rows_tier2": counts["rows_tier2_gold"],
                "rows_tier3": counts["rows_tier3_silver"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "distinct_dots_matched": counts["distinct_dots_matched"],
                "distinct_ueis_matched": counts["distinct_ueis_matched"],
                "rows_zip5_match_true": counts["rows_zip5_match_true"],
                "rows_zip5_match_false": counts["rows_zip5_match_false"],
                "lance_rows": lance_count,
            },
        )
        logger.info("OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0)
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
