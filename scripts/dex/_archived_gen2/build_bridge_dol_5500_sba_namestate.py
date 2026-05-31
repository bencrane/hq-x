#!/usr/bin/env python3
"""DuckDB-on-R2 bridge generator: DOL Form 5500 plan sponsor ↔ SBA borrower
via name+state. Unlocks EIN-discovery for SBA borrowers.

Strategic value:
  Form 5500 has 100% mandatory EIN (every employee benefit plan filer).
  SBA bulk has none. Bridge unlocks EIN linkage for ~10-25% of established
  SBA borrowers (those who file Form 5500 = ~retirement plan, health plan,
  or other ERISA-covered benefit). EIN unlocks cross-source bridges to
  USAspending / IRS BMF / IRS 990 / GLEIF / etc.

Match logic:
  1. Register match-method `name_state_exact` v1.0.0 + bridge
     `dol_5500_sba_namestate` v1.0.0 in the registry (idempotent UPSERT).
  2. Read Form 5500 main (f_5500) Parquets via DuckDB-on-R2.
  3. Parse `raw_json` to extract sponsor mail US state (Form 5500 main
     table doesn't project state directly — only EIN + sponsor name +
     plan-meta).
  4. Normalize sponsor_dfe_name via _lib/entity_name_normalize.py
     (verified-equivalent SQL — same approach as FEC × SBA bridge).
  5. Aggregate per (sponsor_name_normalized, sponsor_state) across plans
     and filing years: max(EIN), max(participants), max(year),
     count(plans), latest sponsor address.
  6. Read SBA Parquets (UNION historical 7a/504 + PPP); apply same
     name-normalization + state.
  7. INNER JOIN on (normalized_name, state).
  8. Compute fan-out tier (platinum 1:1, gold 1:N|N:1, silver N:M ≤50,
     reject >50).
  9. Write Parquet → bridges/dol_5500_sba_namestate/snapshot=<YYYY-MM-DD>/
     data.parquet with bridge_run_id column embedded per row.
 10. HARD FAIL if rows_matched < 50K (validation floor).

Inputs:
  Form 5500: r2://dex-raw-landing-zone/dol-5500/year=*/table=f_5500/*.parquet
  SBA:        r2://dex-raw-landing-zone/sba/program=*/...

Usage:
  doppler run -p hq-all -c prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_dol_5500_sba_namestate.py --apply
  doppler run -p hq-all -c prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_dol_5500_sba_namestate.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
)
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
logger = logging.getLogger("build_bridge_dol_5500_sba_namestate")


# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "dol_5500_sba_namestate"
METHOD_NAME = "name_state_exact"
METHOD_SEMVER = "1.0.0"
LEGACY_BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "source_dol_5500_f_5500"
SOURCE_RIGHT = "mv_sba_borrower_essentials"

# R2 layout ------------------------------------------------------------------
R2_BUCKET = "dex-raw-landing-zone"
FORM_5500_INPUT_GLOB = "dol-5500/year=*/table=f_5500/part-*.parquet"
SBA_HISTORICAL_GLOB_7A = "sba/program=7a/decade=*/*.parquet"
SBA_HISTORICAL_GLOB_504 = "sba/program=504/decade=*/*.parquet"
SBA_PPP_GLOB = "sba/program=ppp/segment=*/part-*.parquet"
BRIDGE_OUTPUT_PREFIX = "bridges/dol_5500_sba_namestate"

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50  # >50 fan-out on either side → rejected

# Validation floor -----------------------------------------------------------
MIN_ROWS_MATCHED = 50_000


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    return endpoint.split("//")[-1].split(".")[0]


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
            ACCOUNT_ID '{_r2_account_id_from_endpoint(os.environ["R2_ENDPOINT"])}'
        );
        """
    )
    return con


# DuckDB SQL equivalent of normalize_entity_name() v1.0.0. Mirrors the FEC
# bridge generator (single source of truth: _lib/entity_name_normalize.py).
def _normalize_entity_sql(raw_expr: str) -> str:
    suffixes = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          WHEN trim({raw_expr}) IN ('Unknown', 'Unknown/NotStated', 'Unanswered', 'N/A', 'NA') THEN NULL
          ELSE NULLIF(
            CASE
              WHEN length(
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
                )
              ) < 2 THEN NULL
              WHEN trim(
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
              ) IN (
                'self employed', 'selfemployed', 'self', 'owner',
                'owner operator', 'sole proprietor', 'proprietor',
                'retired', 'unemployed', 'homemaker',
                'stay at home', 'stay at home mom', 'stay at home parent',
                'student', 'disabled',
                'n a', 'na', 'none', 'not applicable', 'not employed',
                'various', 'private', 'information requested', 'requested',
                'info requested', 'best efforts', 'unknown'
              ) THEN NULL
              ELSE trim(
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
              )
            END,
            ''
          )
        END
    """.strip()


def _materialize_inputs(con) -> tuple[int, int]:
    """Build form_5500_branded + sba_branded TEMP tables; return (rows_left, rows_right)."""
    form_5500_uri = f"r2://{R2_BUCKET}/{FORM_5500_INPUT_GLOB}"
    sba_hist_7a_uri = f"r2://{R2_BUCKET}/{SBA_HISTORICAL_GLOB_7A}"
    sba_hist_504_uri = f"r2://{R2_BUCKET}/{SBA_HISTORICAL_GLOB_504}"
    sba_ppp_uri = f"r2://{R2_BUCKET}/{SBA_PPP_GLOB}"

    sponsor_norm = _normalize_entity_sql("sponsor_dfe_name")

    logger.info("materializing form_5500_raw …")
    # Form 5500 main: parse raw_json to extract sponsor address state +
    # admin block for downstream payload. raw_json is the full source
    # JSON; the projected columns are a thin subset.
    con.execute(
        f"""
        CREATE TEMP TABLE form_5500_raw AS
        SELECT
            spons_dfe_ein,
            sponsor_dfe_name,
            spons_dfe_dba_name,
            business_code,
            tot_active_partcp_cnt,
            tot_partcp_boy_cnt,
            year AS filing_year,
            ack_id,
            -- raw_json holds the address; CAST→JSON to navigate.
            CAST(raw_json AS JSON) AS j,
            ({sponsor_norm}) AS sponsor_name_normalized
          FROM read_parquet('{form_5500_uri}', union_by_name=true, hive_partitioning=true)
         WHERE sponsor_dfe_name IS NOT NULL
           AND spons_dfe_ein IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE form_5500_extracted AS
        SELECT
          spons_dfe_ein,
          sponsor_dfe_name,
          spons_dfe_dba_name,
          business_code,
          tot_active_partcp_cnt,
          tot_partcp_boy_cnt,
          filing_year,
          ack_id,
          sponsor_name_normalized,
          upper(trim(json_extract_string(j, '$.SPONS_DFE_MAIL_US_STATE'))) AS sponsor_state,
          json_extract_string(j, '$.SPONS_DFE_MAIL_US_ADDRESS1')           AS sponsor_street,
          json_extract_string(j, '$.SPONS_DFE_MAIL_US_CITY')               AS sponsor_city,
          json_extract_string(j, '$.SPONS_DFE_MAIL_US_ZIP')                AS sponsor_zip,
          json_extract_string(j, '$.SPONS_DFE_PHONE_NUM')                  AS sponsor_phone,
          json_extract_string(j, '$.ADMIN_NAME')                           AS admin_name,
          json_extract_string(j, '$.ADMIN_EIN')                            AS admin_ein,
          json_extract_string(j, '$.ADMIN_PHONE_NUM')                      AS admin_phone,
          upper(trim(json_extract_string(j, '$.ADMIN_US_STATE')))          AS admin_state,
          json_extract_string(j, '$.ADMIN_US_CITY')                        AS admin_city,
          json_extract_string(j, '$.ADMIN_US_ZIP')                         AS admin_zip
        FROM form_5500_raw
        WHERE sponsor_name_normalized IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE form_5500_branded AS
        SELECT
          sponsor_name_normalized,
          sponsor_state,
          -- payload: dedupe per (name_normalized, state). Within one
          -- company-state, EIN should be a single value (we pick the most
          -- recent filing year's EIN to handle EIN reissues).
          arg_max(spons_dfe_ein, filing_year)            AS sponsor_ein,
          arg_max(sponsor_dfe_name, filing_year)         AS sponsor_name_raw,
          arg_max(spons_dfe_dba_name, filing_year)       AS sponsor_dba_name,
          arg_max(business_code, filing_year)            AS business_code,
          arg_max(sponsor_street, filing_year)           AS sponsor_street,
          arg_max(sponsor_city, filing_year)             AS sponsor_city,
          arg_max(sponsor_zip, filing_year)              AS sponsor_zip,
          arg_max(sponsor_phone, filing_year)            AS sponsor_phone,
          arg_max(admin_name, filing_year)               AS admin_name,
          arg_max(admin_ein, filing_year)                AS admin_ein,
          arg_max(admin_phone, filing_year)              AS admin_phone,
          arg_max(admin_state, filing_year)              AS admin_state,
          arg_max(admin_city, filing_year)               AS admin_city,
          arg_max(admin_zip, filing_year)                AS admin_zip,
          max(try_cast(tot_active_partcp_cnt AS BIGINT)) AS max_active_participants,
          max(try_cast(tot_partcp_boy_cnt AS BIGINT))    AS max_total_participants,
          count(DISTINCT ack_id)                         AS distinct_plan_count,
          max(filing_year)                               AS latest_filing_year,
          min(filing_year)                               AS earliest_filing_year
        FROM form_5500_extracted
        WHERE sponsor_state IS NOT NULL
          AND length(sponsor_state) = 2
        GROUP BY sponsor_name_normalized, sponsor_state
        """
    )

    rows_left = con.execute("SELECT count(*) FROM form_5500_branded").fetchone()[0]
    logger.info(f"  form_5500_branded (unique sponsor on name+state): {rows_left:,}")

    logger.info("materializing sba_branded …")
    sba_hist_norm = _normalize_entity_sql("borrname")
    sba_ppp_norm = _normalize_entity_sql("borrower_name")
    con.execute(
        f"""
        CREATE TEMP TABLE sba_raw AS
        SELECT
          'historical' AS dataset,
          program,
          md5(
            coalesce(program, '') || '|' ||
            coalesce(cast(decade AS VARCHAR), '') || '|' ||
            coalesce(borrname, '') || '|' ||
            coalesce(cast(approvaldate AS VARCHAR), '') || '|' ||
            coalesce(cast(grossapproval AS VARCHAR), '') || '|' ||
            coalesce(locationid, '')
          ) AS loan_id,
          borrname AS borrower_name,
          upper(trim(borrstate)) AS borrower_state,
          ({sba_hist_norm}) AS borrower_name_normalized
        FROM read_parquet(['{sba_hist_7a_uri}', '{sba_hist_504_uri}'], union_by_name=true, hive_partitioning=true)
        WHERE borrname IS NOT NULL
          AND borrstate IS NOT NULL
          AND length(trim(borrstate)) = 2
        UNION ALL
        SELECT
          'ppp' AS dataset,
          'ppp' AS program,
          md5('ppp|' || coalesce(cast(loan_number AS VARCHAR), '')) AS loan_id,
          borrower_name AS borrower_name,
          upper(trim(borrower_state)) AS borrower_state,
          ({sba_ppp_norm}) AS borrower_name_normalized
        FROM read_parquet('{sba_ppp_uri}', union_by_name=true, hive_partitioning=true)
        WHERE borrower_name IS NOT NULL
          AND borrower_state IS NOT NULL
          AND length(trim(borrower_state)) = 2
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sba_branded AS
        SELECT *
        FROM sba_raw
        WHERE borrower_name_normalized IS NOT NULL
        """
    )
    rows_right = con.execute("SELECT count(*) FROM sba_branded").fetchone()[0]
    logger.info(f"  sba_branded: {rows_right:,}")
    return rows_left, rows_right


def _build_match_table(
    con,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> dict[str, int]:
    """Compute fan-out, JOIN, tier; write to TEMP TABLE bridge_match."""
    logger.info("computing per-(name,state) fan-out + tiered join …")
    con.execute(
        """
        CREATE TEMP TABLE form_5500_fanout AS
        SELECT
          sponsor_name_normalized AS norm_name,
          sponsor_state AS state,
          count(*) AS form_5500_sponsors_at_name_state
        FROM form_5500_branded
        GROUP BY sponsor_name_normalized, sponsor_state
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sba_fanout AS
        SELECT
          borrower_name_normalized AS norm_name,
          borrower_state AS state,
          count(*) AS sba_borrowers_at_name_state
        FROM sba_branded
        GROUP BY borrower_name_normalized, borrower_state
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            f.sponsor_name_normalized AS match_value_normalized,
            f.sponsor_state AS match_state,
            s.dataset,
            s.program,
            s.loan_id,
            '{METHOD_NAME}' AS match_method,
            ff.form_5500_sponsors_at_name_state,
            sf.sba_borrowers_at_name_state,
            CASE
                WHEN ff.form_5500_sponsors_at_name_state > {COLLISION_THRESHOLD}
                  OR sf.sba_borrowers_at_name_state > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.form_5500_sponsors_at_name_state = 1
                  AND sf.sba_borrowers_at_name_state = 1
                    THEN 'platinum'
                WHEN ff.form_5500_sponsors_at_name_state = 1
                  OR sf.sba_borrowers_at_name_state = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            -- Form 5500 payload
            f.sponsor_ein,
            f.sponsor_name_raw,
            f.sponsor_dba_name,
            f.business_code,
            f.sponsor_street,
            f.sponsor_city,
            f.sponsor_zip,
            f.sponsor_phone,
            f.admin_name,
            f.admin_ein,
            f.admin_phone,
            f.admin_state,
            f.admin_city,
            f.admin_zip,
            f.max_active_participants,
            f.max_total_participants,
            f.distinct_plan_count,
            f.latest_filing_year,
            f.earliest_filing_year,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM form_5500_branded f
        JOIN sba_branded s
          ON s.borrower_name_normalized = f.sponsor_name_normalized
         AND s.borrower_state = f.sponsor_state
        JOIN form_5500_fanout ff
          ON ff.norm_name = f.sponsor_name_normalized AND ff.state = f.sponsor_state
        JOIN sba_fanout sf
          ON sf.norm_name = s.borrower_name_normalized AND sf.state = s.borrower_state
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
          bridge_run_id,
          loan_id,
          program,
          dataset,
          match_method,
          match_value_normalized,
          match_state,
          confidence_tier,
          sponsor_ein,
          sponsor_name_raw AS sponsor_name,
          sponsor_dba_name,
          business_code,
          sponsor_street,
          sponsor_city,
          sponsor_zip,
          sponsor_phone,
          admin_name,
          admin_ein,
          admin_phone,
          admin_state,
          admin_city,
          admin_zip,
          max_active_participants,
          max_total_participants,
          distinct_plan_count,
          latest_filing_year,
          earliest_filing_year,
          form_5500_sponsors_at_name_state,
          sba_borrowers_at_name_state,
          generated_at,
          bridge_version
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
        """
    )

    counts = con.execute(
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

    return {
        "rows_matched": counts[0],
        "rows_tier1": counts[1],
        "rows_tier2": counts[2],
        "rows_tier3": counts[3],
        "rows_collision_rejected": rejected,
    }


def _write_bridge_parquet(con, snapshot: str) -> str:
    output_key = f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"writing bridge → {output_url}")
    con.execute(
        f"""
        COPY bridge_match TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    return output_key


def _ensure_registry() -> None:
    """Idempotent UPSERTs registering name_state_exact + dol_5500_sba_namestate."""
    logger.info("registering match_method + bridge in ops registry …")
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact match on (entity_name_normalized, state) — applies "
            "_lib/entity_name_normalize.py to both sides, joins where "
            "(normalized_name, upper-state) matches."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1 at (name,state); gold=1:N or N:1; "
            "silver=N:M ≤50; rejected=>50"
        ),
        rejection_rule_description=(
            "fan-out >50 on either side collapses to rows_collision_rejected"
        ),
        input_columns_left=["sponsor_dfe_name", "SPONS_DFE_MAIL_US_STATE"],
        input_columns_right=["borrname", "borrstate"],
        output_value_description=(
            "lower(trim(name after corp-suffix-strip + punct-strip + ws-collapse)), "
            "joined with upper(trim(state))"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=f"{BRIDGE_OUTPUT_PREFIX}/",
        description=(
            "DOL Form 5500 plan-sponsor legal name ↔ SBA borrower legal "
            "name, joined on (entity_name_normalized, state). Sponsor "
            "state extracted from raw_json.SPONS_DFE_MAIL_US_STATE. "
            "Payload includes sponsor EIN (the GTM unlock), DBA, NAICS-like "
            "business_code, participants count, and plan administrator."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write Parquet + ledger row")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="row counts + tier distribution only, no R2 / Postgres writes",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="YYYY-MM-DD output snapshot dir (default: today UTC)",
    )
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    snapshot = args.snapshot or started_at.strftime("%Y-%m-%d")
    t0 = time.time()

    logger.info(f"bridge: {BRIDGE_NAME} (method={METHOD_NAME} v{METHOD_SEMVER})")
    logger.info(f"normalizer: _lib/entity_name_normalize.py v{NORMALIZER_VERSION}")
    logger.info(f"snapshot: {snapshot}")

    output_key_planned = (
        f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    )

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=LEGACY_BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=output_key_planned,
        )
        bridge_run_id = str(run_uuid)
        logger.info(f"bridge_run_id={bridge_run_id}")

    con = _connect_duckdb_to_r2()

    try:
        rows_left, rows_right = _materialize_inputs(con)
        counts = _build_match_table(
            con,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("─" * 60)
        logger.info("bridge tier distribution:")
        logger.info(f"  rows_matched:            {counts['rows_matched']:,}")
        logger.info(f"    platinum (1:1):        {counts['rows_tier1']:,}")
        logger.info(f"    gold     (1:N | N:1):  {counts['rows_tier2']:,}")
        logger.info(f"    silver   (N:M ≤{COLLISION_THRESHOLD}):    {counts['rows_tier3']:,}")
        logger.info(f"  rows_collision_rejected: {counts['rows_collision_rejected']:,}")

        if counts["rows_matched"] > 0:
            tier1_share = counts["rows_tier1"] / counts["rows_matched"]
            logger.info(f"  tier-1 share:            {tier1_share:.1%}")

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,} — bridge too thin to ship"
            )
            logger.error(msg)
            if args.apply and run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info(f"DRY RUN — no R2 / Postgres writes. duration={time.time()-t0:.1f}s")
            return 0

        r2_output_key = _write_bridge_parquet(con, snapshot)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": 0,
            },
        )
        logger.info(f"OK — run_id={bridge_run_id}  duration={time.time()-t0:.1f}s")
        logger.info(f"     output: r2://{R2_BUCKET}/{r2_output_key}")
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if args.apply and run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
