#!/usr/bin/env python3
"""Bridge generator: SBA 7(a)+504 borrowers × USPTO trademark owners.

Sibling of `sba_overture_address_lance` for the trademark-owner axis. Joins:

  - SBA:      `polaris-warehouse/sba/borrowers_lance`              (~12.0M rows)
  - USPTO L1: `polaris-warehouse/uspto/case_file_owner_lance`      (~27.8M owners)
  - USPTO L2: `polaris-warehouse/uspto/case_file_lance`            (~11.5M marks)

Identity-resolution path: SBA borrower's `legal_name_normalized` + 2-letter US
state matches an `(owner) legal_name_normalized + owner_state_normalized` on
case_file_owner; the matched owner's `serial_no` then joins case_file for the
mark text + registration metadata.

Both sides come with `legal_name_normalized` pre-baked at emit time using the
canonical `_lib.entity_name_normalize.normalize_entity_name` SQL v1.0.0 rule.
No UDF calls at JOIN time.

Why this bridge:
  The existing `uspto_sba_capital_matching_lance` (987 rows) is a narrow,
  experimental aggregate. This builds the canonical loan-grain bridge that
  attaches the registered operating brand for every SBA borrower with at
  least one trademark filing. Especially valuable on the non-franchise
  residual cohort where Overture name-guess and the address-axis bridge
  both fail to surface the operating identity.

Output shape (per matched row):
  - SBA identity:    legal_name_normalized, borrname_sample, borrstate, borrzip
  - SBA loan rollup: total_loans, total_gross_approval, max/min_approval_date,
                     latest_loanstatus, has_pending_commit, franchise_brands_set,
                     naics_codes_set, lender_set
  - USPTO owner:     serial_no, own_name, owner_kind_normalized, own_entity_cd,
                     own_addr_1, own_addr_city, owner_zip5, owner_country_normalized
  - USPTO mark:      mark_text_normalized, mark_id_char, mark_draw_cd,
                     trade_mark_in, serv_mark_in, std_char_claim_in,
                     filing_dt, publication_dt, registration_dt, registration_no,
                     cfh_status_cd, cfh_status_dt, case_file_year

Fan-out tiering:
  platinum = 1:1 on (sba_identity, serial_no)
  gold     = 1:N or N:1
  silver   = N:M (fan-out ≤ 50 on each side)
  rejected = any fan-out > 50

Where `sba_identity` is the composite (legal_name_normalized, borrstate,
borrzip) — the natural key of `sba/borrowers_lance`.

Output: `polaris-warehouse/bridges/sba_uspto_owner_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='sba_uspto_owner')
Floor:  ≥ 100,000 rows.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_uspto_owner_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_uspto_owner_lance.py --dry-run
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
    __version__ as NAME_NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_sba_uspto_owner_lance")

BRIDGE_NAME = "sba_uspto_owner"
METHOD_NAME = "legal_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "uspto_case_file_owner_lance"

SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
USPTO_OWNER_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_owner_lance"
USPTO_CASE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_uspto_owner_lance"
DATASET_SLUG = "sba_uspto_owner_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 100_000
# Process-unique tmp to avoid DuckDB spill collisions when multiple bridge
# builders run concurrently against the same `/tmp/lance/` shared root.
TMP_DIR = f"/tmp/lance/sba_uspto_owner_{os.getpid()}"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load SBA borrowers + USPTO owner + USPTO case_file Arrow tables.

    All three sides are pre-normalized at emit time — `legal_name_normalized`
    on both SBA and USPTO owner uses the same canonical SQL v1.0.0 rule
    (mirrors `_lib/entity_name_normalize.py`).
    """
    import lance
    import pyarrow.compute as pc

    # ---- SBA borrowers ----
    logger.info("opening sba/borrowers_lance ...")
    sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
    sba_filter = (
        pc.field("legal_name_normalized").is_valid()
        & pc.field("borrstate").is_valid()
    )
    sba_cols = [
        "legal_name_normalized",
        "borrname_sample",
        "borrstate",
        "borrzip",
        "total_loans",
        "total_gross_approval",
        "max_approval_date",
        "min_approval_date",
        "latest_loanstatus",
        "has_pending_commit",
        "franchise_brands_set",
        "naics_codes_set",
        "lender_set",
    ]
    sba_arrow = sba_ds.scanner(columns=sba_cols, filter=sba_filter).to_table()
    rows_sba = len(sba_arrow)
    logger.info("  sba borrowers_lance (post-filter): %d rows", rows_sba)

    # ---- USPTO case_file_owner ----
    logger.info("opening uspto/case_file_owner_lance ...")
    owner_ds = lance.dataset(USPTO_OWNER_LANCE_URI, storage_options=storage_options)
    owner_filter = (
        pc.field("legal_name_normalized").is_valid()
        & pc.field("owner_state_normalized").is_valid()
        & pc.field("serial_no").is_valid()
    )
    owner_cols = [
        "serial_no",
        "own_name",
        "owner_name_normalized",
        "legal_name_normalized",
        "owner_zip5",
        "owner_state_normalized",
        "owner_country_normalized",
        "owner_kind_normalized",
        "own_addr_1",
        "own_addr_city",
        "own_entity_cd",
    ]
    owner_arrow = owner_ds.scanner(
        columns=owner_cols, filter=owner_filter
    ).to_table()
    rows_owner = len(owner_arrow)
    logger.info("  uspto case_file_owner_lance (post-filter): %d rows", rows_owner)

    # ---- USPTO case_file (mark text + registration metadata) ----
    logger.info("opening uspto/case_file_lance ...")
    case_ds = lance.dataset(USPTO_CASE_LANCE_URI, storage_options=storage_options)
    case_filter = pc.field("serial_number_bigint").is_valid()
    case_cols = [
        "serial_number_bigint",
        "mark_id_char",
        "mark_text_normalized",
        "mark_draw_cd",
        "trade_mark_in",
        "serv_mark_in",
        "std_char_claim_in",
        "filing_dt",
        "publication_dt",
        "registration_dt",
        "registration_no",
        "cfh_status_cd",
        "cfh_status_dt",
        "case_file_year",
    ]
    case_arrow = case_ds.scanner(columns=case_cols, filter=case_filter).to_table()
    rows_case = len(case_arrow)
    logger.info("  uspto case_file_lance (post-filter): %d rows", rows_case)

    return sba_arrow, owner_arrow, case_arrow, rows_sba, rows_owner, rows_case


def _build_match_table(
    sba_arrow,
    owner_arrow,
    case_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
):
    """JOIN SBA × case_file_owner × case_file + fan-out tiering.

    Step 1: SBA × case_file_owner on (legal_name_normalized, state)
    Step 2: result × case_file on serial_no (LEFT JOIN — keep matches even if
            case_file row is missing, since case_file_owner can legitimately
            outlive a single case_file row via assignments)
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba", sba_arrow)
    con.register("owner", owner_arrow)
    con.register("case_file", case_arrow)

    logger.info(
        "  registered: sba=%d  owner=%d  case=%d",
        con.execute("SELECT COUNT(*) FROM sba").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM owner").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM case_file").fetchone()[0],
    )

    # Step 1: SBA × owner on (legal_name, state). Both `legal_name_normalized`
    # columns are pre-baked. SBA borrstate is uppercase 2-letter; owner_state
    # _normalized comes pre-uppercased.
    con.execute(
        """
        CREATE TEMP TABLE matched_owner AS
        SELECT
            s.legal_name_normalized                           AS sba_legal_name_normalized,
            s.borrname_sample                                 AS sba_borrname_sample,
            UPPER(TRIM(s.borrstate))                          AS sba_borrstate,
            s.borrzip                                         AS sba_borrzip,
            s.total_loans                                     AS sba_total_loans,
            s.total_gross_approval                            AS sba_total_gross_approval,
            s.max_approval_date                               AS sba_max_approval_date,
            s.min_approval_date                               AS sba_min_approval_date,
            s.latest_loanstatus                               AS sba_latest_loanstatus,
            s.has_pending_commit                              AS sba_has_pending_commit,
            s.franchise_brands_set                            AS sba_franchise_brands_set,
            s.naics_codes_set                                 AS sba_naics_codes_set,
            s.lender_set                                      AS sba_lender_set,
            o.serial_no                                       AS uspto_serial_no,
            o.own_name                                        AS uspto_own_name,
            o.owner_name_normalized                           AS uspto_owner_name_normalized,
            o.legal_name_normalized                           AS uspto_legal_name_normalized,
            o.owner_kind_normalized                           AS uspto_owner_kind_normalized,
            o.own_entity_cd                                   AS uspto_own_entity_cd,
            o.own_addr_1                                      AS uspto_own_addr_1,
            o.own_addr_city                                   AS uspto_own_addr_city,
            o.owner_zip5                                      AS uspto_owner_zip5,
            o.owner_country_normalized                        AS uspto_owner_country_normalized
        FROM sba s
        JOIN owner o
          ON s.legal_name_normalized = o.legal_name_normalized
         AND UPPER(TRIM(s.borrstate)) = o.owner_state_normalized
        """
    )
    rows_owner_match = con.execute("SELECT COUNT(*) FROM matched_owner").fetchone()[0]
    logger.info("  step1 owner-matched: %d rows", rows_owner_match)

    # Step 2: LEFT JOIN to case_file on serial_no. case_file.serial_number_bigint
    # is int64, case_file_owner.serial_no is string — cast to align.
    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            mo.*,
            cf.mark_id_char                                   AS uspto_mark_id_char,
            cf.mark_text_normalized                           AS uspto_mark_text_normalized,
            cf.mark_draw_cd                                   AS uspto_mark_draw_cd,
            cf.trade_mark_in                                  AS uspto_trade_mark_in,
            cf.serv_mark_in                                   AS uspto_serv_mark_in,
            cf.std_char_claim_in                              AS uspto_std_char_claim_in,
            cf.filing_dt                                      AS uspto_filing_dt,
            cf.publication_dt                                 AS uspto_publication_dt,
            cf.registration_dt                                AS uspto_registration_dt,
            cf.registration_no                                AS uspto_registration_no,
            cf.cfh_status_cd                                  AS uspto_cfh_status_cd,
            cf.cfh_status_dt                                  AS uspto_cfh_status_dt,
            cf.case_file_year                                 AS uspto_case_file_year,
            'legal_name_state'                                AS match_path
        FROM matched_owner mo
        LEFT JOIN case_file cf
          ON TRY_CAST(mo.uspto_serial_no AS BIGINT) = cf.serial_number_bigint
        """
    )
    rows_matched = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  step2 (with case_file metadata) matched: %d rows", rows_matched)

    # Fan-out tiering: SBA identity = (legal_name, state, zip); USPTO identity = serial_no
    con.execute(
        """
        CREATE TEMP TABLE sba_fanout AS
        SELECT sba_legal_name_normalized AS k1,
               sba_borrstate              AS k2,
               sba_borrzip                AS k3,
               COUNT(*)                   AS sba_fan_out
        FROM matched
        GROUP BY 1, 2, 3
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE uspto_fanout AS
        SELECT uspto_serial_no, COUNT(*) AS uspto_fan_out
        FROM matched
        GROUP BY uspto_serial_no
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.*,
            sf.sba_fan_out,
            uf.uspto_fan_out,
            CASE
                WHEN sf.sba_fan_out > {COLLISION_THRESHOLD}
                  OR uf.uspto_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND uf.uspto_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR  uf.uspto_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched m
        JOIN sba_fanout sf
          ON sf.k1 = m.sba_legal_name_normalized
         AND sf.k2 = m.sba_borrstate
         AND sf.k3 = m.sba_borrzip
        JOIN uspto_fanout uf ON uf.uspto_serial_no = m.uspto_serial_no
        """
    )
    con.execute(
        "CREATE TEMP TABLE bridge_match AS "
        "SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'"
    )

    row_counts = con.execute(
        """
        SELECT
          COUNT(*),
          COUNT(*) FILTER (WHERE confidence_tier='platinum'),
          COUNT(*) FILTER (WHERE confidence_tier='gold'),
          COUNT(*) FILTER (WHERE confidence_tier='silver')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'"
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
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

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

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in (
            "sba_legal_name_normalized",
            "uspto_serial_no",
            "uspto_mark_text_normalized",
            "uspto_cfh_status_cd",
        ):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE index created on %s", col)
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
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (legal_name_normalized, 2-letter US state). "
            "Applies _lib/entity_name_normalize.py "
            f"v{NAME_NORMALIZER_VERSION} (canonical SQL v1.0.0 rule). Both sides "
            "are pre-normalized at emit time — no UDF calls during this JOIN."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NAME_NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NAME_NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50"
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["legal_name_normalized", "borrstate"],
        input_columns_right=["legal_name_normalized", "owner_state_normalized"],
        output_value_description=(
            "normalized entity name + 2-letter US state join key"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA 7(a)+504 borrowers × USPTO trademark owners — legal-name + "
            "state exact match, stitched to case_file mark text + registration "
            "metadata via serial_no. Surfaces the registered operating brand "
            "behind SBA borrowers, especially valuable on the non-franchise "
            "residual where Overture name-guess and address-axis both miss. "
            "Replaces the narrow uspto_sba_capital_matching aggregate (987 rows) "
            "with the canonical loan-grain bridge."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + ledger row")
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

    logger.info(
        "bridge: %s  method=%s v%s  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NAME_NORMALIZER_VERSION,
    )
    logger.info(
        "inputs: sba/borrowers_lance + uspto/case_file_owner_lance + uspto/case_file_lance"
    )
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
        sba_arrow, owner_arrow, case_arrow, rows_sba, rows_owner, rows_case = (
            _materialize_inputs(storage_options)
        )
        con, counts = _build_match_table(
            sba_arrow,
            owner_arrow,
            case_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M ≤%d):     %d", COLLISION_THRESHOLD, counts["rows_tier3"]
        )
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

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
                "DRY RUN OK — no Lance / Postgres writes.  duration=%.1fs",
                time.time() - t0,
            )
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_sba,
                "rows_right": rows_owner,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0
        )
        logger.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        logger.exception("bridge build FAILED: %s", exc)
        if run_uuid is not None:
            fail_bridge_run(run_uuid, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
