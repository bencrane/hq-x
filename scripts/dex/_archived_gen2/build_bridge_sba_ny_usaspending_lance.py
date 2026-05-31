"""SBA NY borrowers × USAspending NY recipients — Pattern B REUSER bridge (Lance).

Pattern B exact-match bridge: SBA 7(a)/504 borrowers (legal_name_normalized,
filtered to borrstate='NY') × USAspending federal contract recipients
(contracts_lance filtered recipient_state_code='NY' + DISTINCT (recipient_uei,
recipient_name) → normalized on-the-fly Python-side).

Method: legal_name_state_exact_ny v1.0.0 (REUSED — the method + method-version
rows were registered by PR #513 (`build_bridge_sba_ny_contracts_lance.py`,
the PUBLISHER). This script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run per L21 — the method-definition and
method-version-definition helpers are INTENTIONALLY OMITTED; calling them
would UPSERT over the SBA-shape config registered by the publisher
(input_columns_left={legal_name_normalized, borrstate},
input_columns_right={vendor_name_normalized}) and corrupt the publisher's
bridge provenance trail). This is the FIRST REUSER of this method.

Pattern shape mirrors PR #487 (`build_bridge_usaspending_sos_ca_owner_lance.py`,
the canonical USAspending Pattern B REUSER on the CA side), with two
orientation differences:
  - PR #487 spine pivot was on USAspending side (since its LEFT was originally
    going to be the metrics-only recipient_grain_lance); here the LEFT is
    SBA borrowers which already carry pre-normalized legal_name_normalized,
    so the Python on-the-fly normalization is needed ONLY on the
    USAspending RIGHT side (mirrors PR #487's normalize-on-the-fly approach).
  - BTREE columns are sba_legal_name_normalized + recipient_uei (instead of
    recipient_uei + sos_entity_num).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (symmetric two-sided per PR #466/#482/#487).
MIN_ROWS_MATCHED = 300 (probe: 462 distinct SBA NY borrowers matched
USAspending NY recipients on normalized name; raw bridge rows expected
500+ with fan-out; 300 floor catches catastrophic regression with
~40% headroom vs 0.5 × estimated yield).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance
        (legal_name_normalized + borrstate; filter borrstate='NY';
        DISTINCT legal_name_normalized post-filter Python-side mirrors
        the publisher's pre-dedup approach — OOM-resistant.)
    s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance
        (recipient_uei + recipient_name + recipient_state_code; filter NY;
        DISTINCT (recipient_uei, recipient_name) → ~5,203 rows; normalize
        recipient_name Python-side via normalize_entity_name)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_usaspending_lance
    (BTREE on sba_legal_name_normalized AND recipient_uei; dual-BTREE per
    PR #466/#482/#487 precedent.)

Match method REUSE (L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + dual BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 — REUSER pattern mirrors PR #487.)

Tier rule (symmetric two-sided per PR #466/#482/#487):
    platinum = BOTH fan_out == 1 (1:1 exact)
    gold     = ONE side fan_out == 1 (1:N | N:1)
    silver   = BOTH fan_out <= 50 (N:M below collision threshold)
    rejected = EITHER fan_out >  50 (collision; excluded from output)

Plain Python (NOT Modal — corpus is small):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_sba_ny_usaspending_lance.py --apply

Dry-run (no Lance write; bridge run marked failed-dry-run):
    uv run python scripts/build_bridge_sba_ny_usaspending_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 pattern — allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (
    normalize_entity_name,
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle is the FIRST REUSER of
# legal_name_state_exact_ny v1.0.0 (publisher: PR #513 SBA × NY State Authority
# contracts). Calling those helpers would UPSERT over the publisher's
# input_columns_right={vendor_name_normalized} config and corrupt the
# publisher's bridge provenance trail.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sba_ny_usaspending"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "sba_ny_usaspending_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ny"   # REUSED — registered by PR #513
METHOD_SEMVER = "1.0.0"                     # REUSED — version row from PR #513
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Probe (2026-05-18): 462 distinct SBA NY borrowers matched USAspending NY
# recipients on normalized name. Raw bridge rows expected ~500-1000 with
# fan-out. 300 floor catches catastrophic regression (normalizer drift,
# state-filter regression, etc.) with conservative headroom.
MIN_ROWS_MATCHED = 300

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "usaspending_contracts_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_usaspending_lance"

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


def _ensure_db_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load SBA NY borrowers + USAspending NY recipients into Arrow tables.

    SBA side:
      - Read sba/borrowers_lance projecting [legal_name_normalized, borrstate]
        with pyarrow push-down filter borrstate='NY'.
      - Python-side dedup to distinct legal_name_normalized (OOM-resistant,
        mirrors PR #513 publisher pattern). The bridge LEFT spine is the
        distinct-name set; downstream fan-out tracks how many SBA loans share
        each name (effectively 1 per distinct name post-dedup, but kept for
        symmetric tier rule).

    USAspending side (spine pivot per PR #487 precedent):
      - Read usaspending/contracts_lance projecting [recipient_uei,
        recipient_name, recipient_state_code] with pyarrow push-down filter
        recipient_state_code='NY' (recipient_grain_lance has no
        recipient_name — cannot anchor a name-based bridge).
      - DuckDB DISTINCT (recipient_uei, recipient_name) → ~5,203 canonical pairs.
      - Python-side normalize via normalize_entity_name(name) list comprehension.
      - Filter rows where recipient_name_normalized is None (L33-blacklisted
        generic strings).
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb

    # ---- SBA side ----
    logger.info("opening sba/borrowers_lance (borrstate=NY filter) ...")
    sba_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sba_filter = pc.field("borrstate") == "NY"
    sba_raw = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter=sba_filter,
    ).to_table()
    rows_sba_raw = len(sba_raw)
    logger.info("  sba borrowers_lance (borrstate=NY): %d rows", rows_sba_raw)

    # Python-side distinct on legal_name_normalized (OOM-resistant).
    names = sba_raw.column("legal_name_normalized").to_pylist()
    distinct_sba: set[str] = set()
    for nm in names:
        if not nm:
            continue
        if isinstance(nm, str) and len(nm) >= 2:
            distinct_sba.add(nm)
    del sba_raw, names

    sba_branded_arrow = pa.table({
        "sba_legal_name_normalized": pa.array(
            sorted(distinct_sba), type=pa.string()
        ),
    })
    rows_sba_distinct = len(sba_branded_arrow)
    del distinct_sba
    logger.info(
        "  sba_branded (distinct legal_name_normalized, NY only): %d names",
        rows_sba_distinct,
    )

    # ---- USAspending side ----
    logger.info("opening usaspending/contracts_lance (NY filter + DISTINCT) ...")
    us_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    us_filter = pc.field("recipient_state_code") == "NY"
    us_raw = us_ds.scanner(
        columns=["recipient_uei", "recipient_name", "recipient_state_code"],
        filter=us_filter,
    ).to_table()
    rows_us_raw = len(us_raw)
    logger.info("  usaspending contracts_lance NY rows (pre-DISTINCT): %d", rows_us_raw)

    # DISTINCT (recipient_uei, recipient_name) — collapse transaction rows to
    # canonical recipient pairs (mirrors PR #487 step).
    con_distinct = duckdb.connect()
    con_distinct.register("u_raw", us_raw)
    us_distinct_arrow = con_distinct.execute(
        """
        SELECT DISTINCT recipient_uei, recipient_name
        FROM u_raw
        WHERE recipient_name IS NOT NULL AND recipient_uei IS NOT NULL
        """
    ).arrow().read_all()
    del us_raw
    rows_us_distinct = len(us_distinct_arrow)
    logger.info(
        "  usaspending NY distinct (recipient_uei, recipient_name): %d rows",
        rows_us_distinct,
    )

    # Python-side normalize: attach recipient_name_normalized column.
    # NOT a DuckDB UDF — corpus is small, cheap to normalize in-process.
    names_raw = us_distinct_arrow.column("recipient_name").to_pylist()
    normalized_names = [normalize_entity_name(n) for n in names_raw]
    us_arrow = us_distinct_arrow.append_column(
        "recipient_name_normalized",
        pa.array(normalized_names, type=pa.string()),
    )

    # Filter rows where normalization returned None (L33-blacklisted strings).
    valid_mask = pc.is_valid(us_arrow.column("recipient_name_normalized"))
    us_arrow = us_arrow.filter(valid_mask)
    rows_us = len(us_arrow)
    logger.info(
        "  usaspending after normalize + filter (non-None normalized): %d rows",
        rows_us,
    )

    return sba_branded_arrow, us_arrow, rows_sba_distinct, rows_us


def _build_match_table(
    sba_tbl,
    us_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: sba_legal_name_normalized (SBA) = recipient_name_normalized (USAspending).
    Both sides pre-normalized via scripts._lib.entity_name_normalize (publisher's
    canonical normalizer + L33 blacklist applied).
    Fan-out: sba_fan_out (per normalized name on SBA side; ≥1 by construction since
    SBA pre-dedup) + recipient_fan_out (per normalized name across distinct
    recipient_ueis on USAspending side).
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba", sba_tbl)
    con.register("us", us_tbl)

    rows_sba_reg = con.execute("SELECT COUNT(*) FROM sba").fetchone()[0]
    rows_us_reg = con.execute("SELECT COUNT(*) FROM us").fetchone()[0]
    logger.info("  registered: sba=%d  us=%d", rows_sba_reg, rows_us_reg)

    # 1. INNER JOIN on normalized name; SBA side is already distinct names.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            sba.sba_legal_name_normalized,
            us.recipient_uei,
            us.recipient_name                    AS recipient_name_raw,
            us.recipient_name_normalized,
            '{METHOD_NAME}'                      AS match_method,
            sba.sba_legal_name_normalized        AS match_value_normalized,
            '{BRIDGE_VERSION}'                   AS bridge_version,
            '{bridge_run_id}'                    AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'       AS generated_at
        FROM sba
        JOIN us
          ON sba.sba_legal_name_normalized = us.recipient_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided per PR #466/#482/#487).
    #    sba_fan_out: 1 by construction (SBA pre-dedup); included for symmetric
    #      tier-rule application across all bridges.
    #    recipient_fan_out: # of distinct recipient_ueis per normalized name on
    #      USAspending side (entities with the same name but different UEIs —
    #      e.g., parent/sub or naming-collision artifacts).
    con.execute(
        """
        CREATE TEMP TABLE sba_fanout AS
        SELECT sba_legal_name_normalized, COUNT(*) AS sba_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE recipient_fanout AS
        SELECT sba_legal_name_normalized,
               COUNT(DISTINCT recipient_uei) AS recipient_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided per PR #466/#482/#487).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sba_fan_out,
            rf.recipient_fan_out,
            CASE
                WHEN sf.sba_fan_out       > {COLLISION_THRESHOLD}
                  OR rf.recipient_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND rf.recipient_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR  rf.recipient_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN sba_fanout       sf USING (sba_legal_name_normalized)
        JOIN recipient_fanout rf USING (sba_legal_name_normalized)
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

        # Dual BTREE per PR #466/#482/#487 precedent.
        try:
            ds.create_scalar_index("sba_legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on sba_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sba_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("recipient_uei", index_type="BTREE", replace=True)
            logger.info("BTREE on recipient_uei: OK")
        except Exception as e:
            logger.error("BTREE on recipient_uei FAILED: %s", e)
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
    """Build the SBA NY × USAspending NY Pattern B REUSER bridge."""
    parser = argparse.ArgumentParser(
        description="SBA NY × USAspending NY Pattern B REUSER bridge generator."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to Lance + register run as completed. Without this flag runs in dry-run mode.",
    )
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #513)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA NY borrowers x USAspending NY recipients — legal-name exact match. "
            "Reuses legal_name_state_exact_ny v1.0.0 method registered by PR #513 "
            "(SBA x NY State Authority contracts publisher). FIRST REUSER of this "
            "method. Spine: sba/borrowers_lance filtered borrstate='NY' + distinct "
            "legal_name_normalized (LEFT); usaspending/contracts_lance filtered "
            "recipient_state_code='NY' + DISTINCT (recipient_uei, recipient_name) "
            "+ Python-side normalize_entity_name (RIGHT). Dual BTREE on "
            "sba_legal_name_normalized + recipient_uei. Mirrors PR #487 CA shape."
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

    if not args.apply:
        # Dry-run: mark as failed so the run is not left orphaned in 'running'.
        msg = "dry-run; no Lance write (pass --apply to execute)"
        logger.info("DRY-RUN: %s", msg)
        fail_bridge_run(run_uuid, msg)
        logger.info("bridge_run marked failed-dry-run (run_id=%s)", bridge_run_id)
        return 0

    try:
        sba_tbl, us_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sba_tbl, us_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M <= %d):   %d",
            COLLISION_THRESHOLD, counts["rows_tier3"],
        )
        logger.info(
            "  rows_collision_rejected:  %d",
            counts["rows_collision_rejected"],
        )

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

        lance_count = _write_bridge_lance(con, storage_options)
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
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
