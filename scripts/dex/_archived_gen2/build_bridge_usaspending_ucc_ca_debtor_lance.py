"""USAspending recipient × CA UCC-1 debtor Pattern B Lance bridge.

Pattern B exact-match bridge: USAspending federal-contract award recipients
(national — usaspending/contracts_lance, ~15.5M transaction-grain rows
collapsed via SELECT DISTINCT (recipient_uei, recipient_name) → recipient
grain, NO CA pre-filter) × CA UCC-1 debtor filings (ucc_ca/debtors_lance,
Organization rows deduped to debtor-name-grain via SELECT DISTINCT
debtor_name_normalized).

Near-clone of build_bridge_ppp_ucc_ca_debtor_lance.py (built 2026-05-20 —
same right-side dataset, same method, same tiering); the left-side dataset
is swapped from SBA PPP CA borrowers to USAspending national recipients.

VALIDATOR DEFECT CORRECTION (Stage 2): the directive originally named
usaspending/recipient_grain_lance as the left input. That dataset is
UEI-keyed obligation/set-aside aggregates with NO entity-name column — it
cannot anchor a name-exact join. The correct dataset is
usaspending/contracts_lance (recipient_name + recipient_uei columns), the
canonical name-bearing USAspending Lance dataset per
build_bridge_usaspending_sos_ca_owner_lance.py.

Method: legal_name_state_exact_ca v1.0.0 (REUSED — the method + version rows
were registered by PR #464's build_bridge_sba_sos_ca_owner_lance.py;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run — the method-definition and
method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the shared method-version row and corrupt the
provenance trail of all 10+ existing reusers — see the PPP precedent
docstring + constraint #1).

USAspending-side shape (national — NO CA pre-filter, validator ambiguity-(b)):
    - Read usaspending/contracts_lance: recipient_uei + recipient_name.
    - contracts_lance is transaction-grain (~15.5M rows). MANDATORY collapse:
      SELECT DISTINCT (recipient_uei, recipient_name) in DuckDB FIRST — without
      it, left_fan_out inflates massively (a recipient with hundreds of
      transactions), pushing most matched names over COLLISION_THRESHOLD into
      the rejected tier (validator P6). Probe-measured post-DISTINCT grain =
      134,837 (recipient_uei, recipient_name) pairs.
    - NO push-down recipient_state_code filter — an out-of-state recipient can
      still be a CA UCC debtor (feedback memory `dont_assume_restrictive_scope`).
      DELIBERATE DIVERGENCE from build_bridge_usaspending_sos_ca_owner_lance.py
      which DOES filter recipient_state_code='CA'; this cycle's left side stays
      national per the validator decision.
    - Normalize recipient_name Python-side via
      _lib.entity_name_normalize.normalize_entity_name AFTER the DISTINCT
      collapse (corpus is ~135K rows — cheap to normalize in-process).
    - Drop None/empty normalizations.

UCC-debtor-side shape (IDENTICAL to the PPP×UCC precedent):
    - ucc_ca/debtors_lance URI: s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
    - Scanner filter: DEBTOR_TYPE='Organization' (excludes ~2.2M individual-debtor rows).
    - Read raw ORG_NAME, normalize Python-side via _lib.entity_name_normalize.normalize_entity_name.
    - Drop None/empty normalizations.
    - Return as Arrow table WITH debtor_name_normalized column.
    - ADD `SELECT DISTINCT debtor_name_normalized` dedup BEFORE the join.
    - After dedup: ucc_fan_out ≡ 1 for every row → silver is structurally
      unreachable (bridge is platinum + gold only — CORRECT, not a bug).

Normalizer discipline (CRITICAL — constraint #2 / validator P4):
    ONLY _lib.entity_name_normalize.normalize_entity_name on BOTH sides.
    DO NOT import or call anything from the _lib UCC-specific normalizer
    module (86.4% divergence from _lib — caused the PR #459/#460 reverts).
    Constraint #2 anti-grep: the UCC-specific normalizer's token does not
    appear anywhere in this script.

Fan-out (CRITICAL — constraint #6 / validator P3 tier-rule trap):
    usaspending_fan_out = COUNT(*) per normalized name (recipient rows per name).
    ucc_fan_out = COUNT(DISTINCT debtor_name_normalized) per name (always 1 post-dedup).
    DO NOT use COUNT(*) for both denominators — that collapses gold=0.
    The two denominators MUST be different.

Tier rule (symmetric two-sided, verbatim from the PPP precedent):
    platinum = BOTH fan_out == 1
    gold     = EXACTLY ONE side == 1
    silver   = BOTH <= COLLISION_THRESHOLD (structurally unreachable post-dedup)
    rejected = EITHER > COLLISION_THRESHOLD

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 10_000 (validator-calibrated 2026-05-20 — ~70% of measured 15,306)

Expected baseline (validator-measured, deterministic 3× runs, stddev 0.0%):
    rows_matched=15,306 (platinum=12,806; gold=2,500; silver=0; rejected=458)

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance
        (recipient_uei + recipient_name; ~15.5M transaction rows;
        SELECT DISTINCT (recipient_uei, recipient_name) → ~134,837 pairs;
        national — NO CA pre-filter; normalize recipient_name via _lib)
    s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
        (raw ORG_NAME; filter: DEBTOR_TYPE='Organization';
        normalize via _lib.entity_name_normalize; SELECT DISTINCT debtor_name_normalized)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_ucc_ca_debtor_lance
    (BTREE on usaspending_recipient_name_normalized AND ucc_debtor_name_normalized)

Registry (REUSER pattern):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + dual BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (The method-definition and method-version-definition helpers are
    INTENTIONALLY NOT IMPORTED — constraint #1 anti-grep; REUSE not redefine.)

Polaris (constraint — SOFT):
    Deferred: init_polaris_lance_generic.py --namespace bridges --table usaspending_ucc_ca_debtor_lance
    Polaris (Railway) is 502-down as of 2026-05-20 (validator-confirmed). Not a blocker.

Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_usaspending_ucc_ca_debtor_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_usaspending_ucc_ca_debtor_lance.py
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

from scripts._lib.entity_name_normalize import (  # noqa: F401 — __version__ for log provenance
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL constraint #1 / validator P2: the method-definition and
# method-version-definition helpers are INTENTIONALLY OMITTED — this script
# is a REUSER of legal_name_state_exact_ca v1.0.0.
# Calling those two helpers would UPSERT over the shared method-version row
# and corrupt the provenance trail of all existing reusers (sba_sos_ca_owner,
# ucc_ca_debtor_sos_ca_owner, ppp_ucc_ca_debtor, usaspending_sos_ca_owner,
# cslb/sam CA bridges). Only the four bridge-lifecycle helpers are imported.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps and constraint checks)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "usaspending_ucc_ca_debtor"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "usaspending_ucc_ca_debtor_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ca"          # REUSED — registered by PR #464
METHOD_SEMVER = "1.0.0"                            # REUSED — version row from PR #464
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-20 post full-corpus baseline probe (3× deterministic,
# stddev 0.0%). USAspending contracts_lance ≈ 15.5M transaction rows → DISTINCT
# (recipient_uei, recipient_name) = 134,837 pairs → 134,832 norm-valid; deduped
# UCC debtor names = 1,583,695. Observed non-rejected rows = 15,306 (platinum=12,806;
# gold=2,500; silver=0; rejected=458).
# Floor = 10,000 (~70% of measured 15,306; catches catastrophic failure from
# schema/normalizer regression without false-tripping on clean recipe runs).
MIN_ROWS_MATCHED = 10_000

SOURCE_LEFT = "usaspending_contracts_lance"
SOURCE_RIGHT = "ucc_ca_debtors_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance"
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_ucc_ca_debtor_lance"
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


def _ensure_db_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load USAspending national recipients + UCC CA debtor names into Arrow tables.

    USAspending side (national — NO CA pre-filter; mandatory DISTINCT collapse):
      - Read usaspending/contracts_lance: recipient_uei + recipient_name.
      - contracts_lance is transaction-grain (~15.5M rows). NO push-down state
        filter — the left side stays national per the validator decision.
      - DuckDB SELECT DISTINCT (recipient_uei, recipient_name) → ~134,837 pairs.
        MANDATORY (constraint #8 / validator P6) — without it left_fan_out
        inflates massively and most matched names land in the rejected tier.
      - Python-side normalize: normalize_entity_name(name) list comprehension
        → usaspending_recipient_name_normalized column attached to the Arrow
        table BEFORE DuckDB registration (corpus ~135K rows — cheap in-process).
      - Drop rows where the normalized name is None.

    UCC debtor side (IDENTICAL to the PPP×UCC precedent):
      - Read ucc_ca/debtors_lance with push-down filter: DEBTOR_TYPE='Organization'.
      - Project ORG_NAME only (raw — the dataset has NO normalized-name column).
      - Normalize ORG_NAME Python-side via _lib.entity_name_normalize.normalize_entity_name.
      - Drop rows where normalized name is None or empty.
      - Return as Arrow table WITH debtor_name_normalized column.
      - The SELECT DISTINCT dedup happens later in DuckDB (constraint #8).
    """
    import duckdb
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # USAspending left side — national, transaction-grain, mandatory DISTINCT collapse.
    logger.info(
        "opening usaspending/contracts_lance (national — no CA filter, "
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

    # SELECT DISTINCT (recipient_uei, recipient_name) — collapse ~15.5M transaction
    # rows → recipient grain. MANDATORY per constraint #8 / validator P6.
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
    # Validator-measured post-DISTINCT figure: 134,837. Log for reproducibility.
    logger.info(
        "  expected post-DISTINCT from validator probe: 134,837 (delta: %+d)",
        rows_after_distinct - 134_837,
    )
    con_distinct.close()

    # Python-side normalize: attach usaspending_recipient_name_normalized column.
    # Corpus is ~135K rows — cheap to normalize in-process (constraint #2).
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
    # Validator-measured norm-valid figure: 134,832. Log for reproducibility.
    logger.info(
        "  expected norm-valid from validator probe: 134,832 (delta: %+d)",
        rows_left - 134_832,
    )

    # UCC right side — raw ORG_NAME, normalize Python-side, drop None/empty.
    # Constraint #2: ONLY _lib.entity_name_normalize — never the UCC-specific
    # normalizer (86.4% divergence; would break the join key).
    logger.info("opening ucc_ca/debtors_lance (DEBTOR_TYPE='Organization') ...")
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

    # Normalize ORG_NAME in Python via _lib (canonical normalizer — constraint #2).
    org_names = ucc_tbl.column("ORG_NAME").to_pylist()
    normalized = [normalize_entity_name(n) for n in org_names]
    ucc_tbl = ucc_tbl.append_column(
        "debtor_name_normalized",
        pa.array(normalized, type=pa.string()),
    )
    ucc_valid_mask = pc.is_valid(ucc_tbl.column("debtor_name_normalized"))
    ucc_tbl = ucc_tbl.filter(ucc_valid_mask)
    rows_ucc_post_norm = len(ucc_tbl)
    logger.info(
        "  ucc after _lib normalize (debtor_name_normalized is_valid): %d rows",
        rows_ucc_post_norm,
    )
    # Validator-measured post-normalize figure: 3,681,435. Log for reproducibility.
    logger.info(
        "  expected ucc post-norm from validator probe: 3,681,435 (delta: %+d)",
        rows_ucc_post_norm - 3_681_435,
    )

    return left_arrow, ucc_tbl, rows_left, rows_ucc_raw


def _build_match_table(
    left_tbl,
    ucc_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run dedup + exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Constraint #8: SELECT DISTINCT debtor_name_normalized dedup on the UCC side
    BEFORE the join. Assert: COUNT(*) == COUNT(DISTINCT debtor_name_normalized)
    on the deduped UCC input.

    Join key: usaspending.usaspending_recipient_name_normalized
              = ucc.ucc_debtor_name_normalized
    BOTH are _lib v1.0.0 normalized — join directly, no re-normalize.

    Fan-out (CRITICAL asymmetry — constraint #6 / validator P3 tier-rule trap):
        usaspending_fan_out = COUNT(*) per name (# of recipient rows post-DISTINCT)
        ucc_fan_out         = COUNT(DISTINCT debtor_name_normalized) per name (≡ 1)
    DO NOT use COUNT(*) for ucc_fan_out — that collapses gold=0.

    Expected post-dedup result: rows_matched=15,306; silver=0 (correct — ucc_fan_out≡1).
    """
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

    # Constraint #8 dedup: SELECT DISTINCT debtor_name_normalized to debtor-name grain.
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
    # Constraint #8 assertion: COUNT(*) == COUNT(DISTINCT debtor_name_normalized).
    if rows_ucc_deduped != rows_ucc_distinct_check:
        raise RuntimeError(
            f"Constraint #8 VIOLATED: UCC dedup failed — "
            f"COUNT(*) {rows_ucc_deduped} != COUNT(DISTINCT) {rows_ucc_distinct_check}"
        )
    logger.info(
        "  constraint #8 PASS: COUNT(*) == COUNT(DISTINCT) == %d (debtor-name-grain confirmed)",
        rows_ucc_deduped,
    )
    logger.info(
        "  expected ucc distinct from validator probe: 1,583,695 (delta: %+d)",
        rows_ucc_deduped - 1_583_695,
    )

    # 1. Inner JOIN on normalized names.
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

    # 2. Fan-out counts (CRITICAL asymmetry — constraint #6 / validator P3).
    #    usaspending_fan_out: # of recipient rows per normalized name (post-DISTINCT).
    #    ucc_fan_out: # of distinct UCC debtor names per name (≡ 1 post-dedup always).
    #    DO NOT use COUNT(*) for ucc_fan_out — that would collapse gold=0.
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

    # 3. Tier rule (symmetric two-sided per the PPP precedent).
    #    silver is structurally unreachable post-dedup (ucc_fan_out ≡ 1).
    #    The bridge will be platinum + gold only — CORRECT, not a bug (constraint #6).
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

    # 4. Filter rejected rows before write.
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
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE (constraint #4).

    BTREE on usaspending_recipient_name_normalized AND ucc_debtor_name_normalized.
    Both must succeed or the run fails — `raise` on index failure (PPP precedent;
    NOT the sam_pdl_domain warn-and-continue pattern).
    """
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

        # Dual BTREE per constraint #4 — HARD failure on either, `raise`.
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


def main() -> int:
    """Build the USAspending national recipients × CA UCC-1 debtors Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description=(
            "USAspending national recipients × CA UCC-1 debtors Pattern B bridge "
            "generator. Resolves USAspending federal-contract award recipients "
            "against deduped CA UCC-1 debtor names via legal_name_state_exact_ca "
            "(REUSE). The collateral-lien signal: federal contractors that pledged "
            "collateral via a California UCC-1 filing."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to Lance + register ops. Without this flag runs in dry-run mode.",
    )
    args = parser.parse_args()

    _ensure_db_url()
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)
    logger.info("floor: %d rows_matched", MIN_ROWS_MATCHED)
    logger.info(
        "expected: 15,306 rows_matched (platinum=12,806; gold=2,500; silver=0; rejected=458)"
    )

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per constraint #1).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "USAspending federal-contract award recipients (national — no CA "
            "pre-filter) × CA UCC-1 debtor filings (deduped to debtor-name-grain) "
            "— the collateral-lien signal. Resolves federal contractors that also "
            "pledged collateral via a California UCC-1 filing. Method: "
            "legal_name_state_exact_ca v1.0.0 (REUSE — calls only register_bridge "
            "+ start/complete/fail_bridge_run). USAspending side: "
            "usaspending/contracts_lance (~15.5M transaction rows) collapsed via "
            "SELECT DISTINCT (recipient_uei, recipient_name) → ~134,837 recipient "
            "pairs; recipient_name normalized via _lib.entity_name_normalize "
            "Python-side; national, no state filter (an out-of-state recipient can "
            "be a CA UCC debtor). UCC side: Organization debtors from "
            "ucc_ca/debtors_lance; normalized via _lib.entity_name_normalize; "
            "deduped to debtor-name-grain via SELECT DISTINCT (1.58M distinct names). "
            "silver=0 by construction (ucc_fan_out≡1). BTREE on "
            "usaspending_recipient_name_normalized + ucc_debtor_name_normalized."
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
        # Dry-run: mark as failed so the run is not left orphaned.
        msg = "dry-run; no Lance write (pass --apply to execute)"
        logger.info("DRY-RUN: %s", msg)
        fail_bridge_run(run_uuid, msg)
        logger.info("bridge_run marked failed-dry-run (run_id=%s)", bridge_run_id)
        return 0

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
        logger.info(
            "  rows_collision_rejected:  %d",
            counts["rows_collision_rejected"],
        )

        # HARD FAIL before Lance write if rows_matched < floor.
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,} — check normalizer (constraint #2), "
                f"USAspending DISTINCT collapse (constraint #8) and UCC dedup"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

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
            "OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        logger.info(
            "Polaris registration DEFERRED (SOFT — Polaris 502-down 2026-05-20):"
        )
        logger.info(
            "  init_polaris_lance_generic.py --namespace bridges "
            "--table usaspending_ucc_ca_debtor_lance"
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
    sys.exit(main())
