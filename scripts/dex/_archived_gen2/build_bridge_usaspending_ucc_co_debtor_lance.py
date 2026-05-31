"""USAspending recipient × CO UCC-1 debtor Pattern B Lance bridge.

CO parity port of build_bridge_usaspending_ucc_ca_debtor_lance.py (CA edition).

Pattern B exact-match bridge: USAspending federal-contract award recipients
(national — usaspending/contracts_lance, ~15.5M transaction-grain rows
collapsed via SELECT DISTINCT (recipient_uei, recipient_name) → recipient
grain, NO state pre-filter) × CO UCC-1 debtor filings (ucc_co/debtors_lance,
Organization rows deduped to debtor-name-grain via SELECT DISTINCT).

Method: legal_name_state_exact_co v1.0.0 (REUSED — defined by the
ppp_ucc_co_debtor bridge). This script ONLY calls register_bridge — the
method-definition helpers are intentionally not imported (reuse, not redefine).

USAspending-side shape (national — NO state pre-filter; mandatory DISTINCT collapse):
    - Read usaspending/contracts_lance: recipient_uei + recipient_name.
    - contracts_lance is transaction-grain (~15.5M rows). MANDATORY collapse:
      SELECT DISTINCT (recipient_uei, recipient_name) FIRST — without it
      left_fan_out inflates massively, pushing matched names over
      COLLISION_THRESHOLD into the rejected tier.
    - Normalize recipient_name Python-side via _lib.entity_name_normalize
      AFTER the DISTINCT collapse. Drop None/empty normalizations.

UCC-debtor-side shape:
    - ucc_co/debtors_lance; scanner filter DEBTOR_TYPE='Organization'.
    - Read raw ORG_NAME, normalize Python-side via _lib.entity_name_normalize.
    - Drop None/empty; SELECT DISTINCT debtor_name_normalized dedup before join.
    - After dedup ucc_fan_out ≡ 1 → silver structurally unreachable.

Fan-out asymmetry:
    usaspending_fan_out = COUNT(*) per normalized name (recipient rows per name).
    ucc_fan_out = COUNT(DISTINCT debtor_name_normalized) per name (≡ 1 post-dedup).

Tier rule: platinum=both 1; gold=exactly one 1; silver=both ≤50; rejected=either >50.

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_ucc_co_debtor_lance
    (BTREE on usaspending_recipient_name_normalized AND ucc_debtor_name_normalized)

Floor: MIN_ROWS_MATCHED — calibrated from --dry-run.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_usaspending_ucc_co_debtor_lance.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

BRIDGE_NAME = "usaspending_ucc_co_debtor"
DATASET_SLUG = "usaspending_ucc_co_debtor_lance"
METHOD_NAME = "legal_name_state_exact_co"   # REUSED — defined by ppp_ucc_co_debtor
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Calibrated from --dry-run: natural matched-row count 4,808 (3,778 platinum
# + 1,030 gold; silver 0 by construction). Floor 3,000 (~62% of measured).
MIN_ROWS_MATCHED = 3_000

SOURCE_LEFT = "usaspending_contracts_lance"
SOURCE_RIGHT = "ucc_co_debtors_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/debtors_lance"
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_ucc_co_debtor_lance"
)

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load USAspending national recipients + CO UCC debtor names into Arrow tables."""
    import duckdb
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # USAspending left side — national, transaction-grain, mandatory DISTINCT collapse.
    logger.info(
        "opening usaspending/contracts_lance (national — no state filter, "
        "transaction-grain) ..."
    )
    contracts_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    contracts_raw = contracts_ds.scanner(
        columns=["recipient_uei", "recipient_name"],
    ).to_table()
    logger.info(
        "  contracts_lance rows (pre-DISTINCT, transaction-grain): %d",
        len(contracts_raw),
    )

    # SELECT DISTINCT (recipient_uei, recipient_name) — collapse transaction rows
    # → recipient grain. MANDATORY: without it left_fan_out inflates massively.
    con_distinct = duckdb.connect()
    con_distinct.execute("SET threads=2")
    con_distinct.execute("SET memory_limit='8GB'")
    con_distinct.execute(f"SET temp_directory='{TMP_DIR}'")
    con_distinct.execute("SET max_temp_directory_size='120GB'")
    con_distinct.execute("SET preserve_insertion_order=false")
    con_distinct.register("contracts_raw", contracts_raw)
    left_distinct_arrow = con_distinct.execute(
        """
        SELECT DISTINCT recipient_uei, recipient_name
        FROM contracts_raw
        WHERE recipient_name IS NOT NULL AND recipient_uei IS NOT NULL
        """
    ).arrow().read_all()
    rows_after_distinct = len(left_distinct_arrow)
    logger.info(
        "  contracts_lance DISTINCT (recipient_uei, recipient_name): %d rows",
        rows_after_distinct,
    )
    con_distinct.close()

    # Python-side normalize: attach usaspending_recipient_name_normalized column.
    names_raw = left_distinct_arrow.column("recipient_name").to_pylist()
    normalized_names = [normalize_entity_name(n) for n in names_raw]
    left_arrow = left_distinct_arrow.append_column(
        "usaspending_recipient_name_normalized",
        pa.array(normalized_names, type=pa.string()),
    )
    valid_mask = pc.is_valid(left_arrow.column("usaspending_recipient_name_normalized"))
    left_arrow = left_arrow.filter(valid_mask)
    rows_left = len(left_arrow)
    logger.info(
        "  after _lib normalize + filter (non-None normalized): %d rows",
        rows_left,
    )

    # UCC right side — raw ORG_NAME, normalize Python-side, drop None/empty.
    logger.info("opening ucc_co/debtors_lance (DEBTOR_TYPE='Organization') ...")
    ucc_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    ucc_tbl = ucc_ds.scanner(
        columns=["ORG_NAME"],
        filter=pc.field("DEBTOR_TYPE") == "Organization",
    ).to_table()
    rows_ucc_raw = len(ucc_tbl)
    logger.info(
        "  ucc debtors_lance (DEBTOR_TYPE=Organization): %d rows",
        rows_ucc_raw,
    )

    org_names = ucc_tbl.column("ORG_NAME").to_pylist()
    normalized = [normalize_entity_name(n) for n in org_names]
    ucc_tbl = ucc_tbl.append_column(
        "debtor_name_normalized",
        pa.array(normalized, type=pa.string()),
    )
    ucc_valid_mask = pc.is_valid(ucc_tbl.column("debtor_name_normalized"))
    ucc_tbl = ucc_tbl.filter(ucc_valid_mask)
    logger.info(
        "  ucc after _lib normalize (debtor_name_normalized is_valid): %d rows",
        len(ucc_tbl),
    )

    return left_arrow, ucc_tbl, rows_left, rows_ucc_raw


def _build_match_table(
    left_tbl,
    ucc_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run dedup + exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("usaspending", left_tbl)
    con.register("ucc_filing", ucc_tbl)

    rows_us_reg = con.execute("SELECT COUNT(*) FROM usaspending").fetchone()[0]
    rows_ucc_filing = con.execute("SELECT COUNT(*) FROM ucc_filing").fetchone()[0]
    logger.info(
        "  registered: usaspending=%d  ucc_filing=%d",
        rows_us_reg, rows_ucc_filing,
    )

    # SELECT DISTINCT debtor_name_normalized — debtor-name grain.
    con.execute(
        """
        CREATE TEMP TABLE ucc AS
        SELECT DISTINCT debtor_name_normalized AS ucc_debtor_name_normalized
        FROM ucc_filing
        WHERE debtor_name_normalized IS NOT NULL
          AND debtor_name_normalized <> ''
        """
    )
    rows_ucc_deduped = con.execute("SELECT COUNT(*) FROM ucc").fetchone()[0]
    rows_ucc_distinct_check = con.execute(
        "SELECT COUNT(DISTINCT ucc_debtor_name_normalized) FROM ucc"
    ).fetchone()[0]
    logger.info(
        "  ucc after SELECT DISTINCT: %d rows (distinct check: %d)",
        rows_ucc_deduped, rows_ucc_distinct_check,
    )
    if rows_ucc_deduped != rows_ucc_distinct_check:
        raise RuntimeError(
            f"UCC dedup failed — COUNT(*) {rows_ucc_deduped} != "
            f"COUNT(DISTINCT) {rows_ucc_distinct_check}"
        )

    # Inner JOIN on normalized names. Both sides _lib v1.0.0 normalized.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            u.recipient_uei                          AS usaspending_recipient_uei,
            u.recipient_name                         AS usaspending_recipient_name,
            u.usaspending_recipient_name_normalized,
            d.ucc_debtor_name_normalized,
            '{METHOD_NAME}'                          AS match_method,
            u.usaspending_recipient_name_normalized  AS match_value,
            '{BRIDGE_VERSION}'                       AS bridge_version,
            '{bridge_run_id}'                        AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'           AS generated_at
        FROM usaspending u
        JOIN ucc d
          ON u.usaspending_recipient_name_normalized = d.ucc_debtor_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # Fan-out counts (CRITICAL asymmetry).
    #   usaspending_fan_out: # of recipient rows per normalized name (post-DISTINCT).
    #   ucc_fan_out: # of distinct UCC debtor names per name (≡ 1 post-dedup).
    con.execute(
        """
        CREATE TEMP TABLE usaspending_fanout AS
        SELECT usaspending_recipient_name_normalized, COUNT(*) AS usaspending_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ucc_fanout AS
        SELECT usaspending_recipient_name_normalized,
               COUNT(DISTINCT ucc_debtor_name_normalized) AS ucc_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # Tier rule (symmetric two-sided). silver structurally unreachable post-dedup.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            uf.usaspending_fan_out,
            cf.ucc_fan_out,
            CASE
                WHEN uf.usaspending_fan_out > {COLLISION_THRESHOLD}
                  OR cf.ucc_fan_out         > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN uf.usaspending_fan_out = 1 AND cf.ucc_fan_out = 1
                    THEN 'platinum'
                WHEN uf.usaspending_fan_out = 1 OR  cf.ucc_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN usaspending_fanout uf USING (usaspending_recipient_name_normalized)
        JOIN ucc_fanout         cf USING (usaspending_recipient_name_normalized)
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
        """
    )

    counts_row = con.execute(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
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
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        try:
            ds.create_scalar_index(
                "usaspending_recipient_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on usaspending_recipient_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on usaspending_recipient_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index(
                "ucc_debtor_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on ucc_debtor_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on ucc_debtor_name_normalized FAILED: %s", e)
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


def _ensure_registry() -> None:
    # Method legal_name_state_exact_co v1.0.0 is REUSED (defined by the
    # ppp_ucc_co_debtor bridge). A reusing bridge calls only register_bridge.
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "USAspending federal-contract award recipients (national — no state "
            "pre-filter) × CO UCC-1 debtor filings (deduped to debtor-name-grain) "
            "— the collateral-lien signal. Resolves federal contractors that also "
            "pledged collateral via a Colorado UCC-1 filing. Method: "
            "legal_name_state_exact_co v1.0.0 (REUSE). USAspending side: "
            "usaspending/contracts_lance (~15.5M transaction rows) collapsed via "
            "SELECT DISTINCT (recipient_uei, recipient_name); recipient_name "
            "normalized via _lib.entity_name_normalize; national, no state filter. "
            "UCC side: Organization debtors from ucc_co/debtors_lance, normalized "
            "and deduped to debtor-name-grain via SELECT DISTINCT. silver=0 by "
            "construction (ucc_fan_out≡1). BTREE on "
            "usaspending_recipient_name_normalized + ucc_debtor_name_normalized. "
            "CO parity port of the usaspending_ucc_ca_debtor bridge."
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

    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED)  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

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
        left_tbl, ucc_tbl, rows_left, rows_ucc_raw = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            left_tbl, ucc_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M <= %d):   %d  (expected 0 — ucc_fan_out≡1 post-dedup)",
            COLLISION_THRESHOLD, counts["rows_tier3"],
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
            logger.info("DRY RUN — no Lance / Postgres writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_ucc_raw,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
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
                fail_bridge_run(run_uuid, repr(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
