"""SAM.gov entity × CA UCC-1 debtor Pattern B Lance bridge.

**v2.0.0 (address-axis composite):** the bridge now joins on legal-name +
state exact-equality AND checks whether the SAM entity's normalized
physical or mailing address appears in the per-debtor-name aggregated set
of UCC-filing addresses. Address agreement promotes the name-axis fan-out
tier; absence holds or demotes:

    platinum  ← name 1:1 AND address_agrees
    gold      ← (name 1:1, no address) OR (name 1:N|N:1, address agrees)
    silver    ← residual (expected 0 post-dedup; name N:M is structurally unreachable)
    rejected  ← either side's fan-out > COLLISION_THRESHOLD

Bridge remains name-grain on output (one row per SAM UEI × UCC debtor-name
pair). The UCC side aggregates `address_base_normalized` across all
filings under each debtor name into a LIST, then asks: does SAM's
physical or mailing normalized address appear in that set? Schema additions
in v2.0.0:

    sam_physical_address_base_normalized           (left-side, pre-baked)
    sam_mailing_address_base_normalized            (left-side, pre-baked)
    address_agrees                                 BOOLEAN
    address_match_path                             VARCHAR — 'physical' | 'mailing' | 'mailing|physical' | NULL
    address_match_value                            VARCHAR — the matching normalized address, or NULL
    name_confidence_tier                           VARCHAR — legacy v1.0.0 tier rule
    composite_confidence_tier                      VARCHAR — same as confidence_tier

The aggregated `ucc_debtor_address_set` LIST column is NOT written to Lance
(dropped via SELECT * EXCLUDE) — Lance 1.5.x def-buffer cap risk; the
per-name agreement is captured in the boolean + path/value scalar columns.

Method: ``legal_name_state_exact_ca_with_address_corroboration`` v1.0.0 (NEW
method registered by this script — same method shared with the two UCC ×
CA SoS sibling bridges; idempotent UPSERT keeps the row consistent across
all three writers).

Pattern B exact-match bridge: SAM.gov registered entities (national —
sam_gov/entities_lance, legal_business_name + unique_entity_id, NO CA
pre-filter) × CA UCC-1 debtor filings (ucc_ca/debtors_lance, Organization
rows aggregated to debtor-name-grain with a per-name address set).

Near-clone of build_bridge_ppp_ucc_ca_debtor_lance.py (built 2026-05-20).
v2.0.0 adds the address axis baked into both spines per PR #803.

SAM-side shape (national — NO CA pre-filter, validator ambiguity-(b) decision):
    - Read sam_gov/entities_lance: unique_entity_id (UEI) + legal_business_name.
    - NO push-down state filter — an out-of-state SAM entity can still be a CA
      UCC debtor (feedback memory `dont_assume_restrictive_scope`).
    - Normalize legal_business_name Python-side via
      _lib.entity_name_normalize.normalize_entity_name (canonical _lib v1.0.0 —
      the SAME normalizer that produces the UCC join key; this is the validated
      join-safe path).
    - Drop None/empty normalizations.

UCC-debtor-side shape (IDENTICAL to the PPP×UCC precedent):
    - ucc_ca/debtors_lance URI: s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
    - Scanner filter: DEBTOR_TYPE='Organization' (excludes ~2.2M individual-debtor rows).
    - Read raw ORG_NAME, normalize Python-side via _lib.entity_name_normalize.normalize_entity_name.
    - Drop None/empty normalizations.
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
    sam_fan_out = COUNT(*) per normalized name (SAM entity rows per name).
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
MIN_ROWS_MATCHED = 45_000 (validator-calibrated 2026-05-20 — ~70% of measured 65,470)

Expected baseline (validator-measured, deterministic 3× runs, stddev 0.0%):
    rows_matched=65,470 (platinum=50,954; gold=14,516; silver=0; rejected=3,270)

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance
        (unique_entity_id, legal_business_name; national — NO CA pre-filter;
        normalize legal_business_name via _lib.entity_name_normalize)
    s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
        (raw ORG_NAME; filter: DEBTOR_TYPE='Organization';
        normalize via _lib.entity_name_normalize; SELECT DISTINCT debtor_name_normalized)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ucc_ca_debtor_lance
    (BTREE on sam_legal_name_normalized AND ucc_debtor_name_normalized)

Registry (REUSER pattern):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + dual BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (The method-definition and method-version-definition helpers are
    INTENTIONALLY NOT IMPORTED — constraint #1 anti-grep; REUSE not redefine.)

Polaris (constraint — SOFT):
    Deferred: init_polaris_lance_generic.py --namespace bridges --table sam_ucc_ca_debtor_lance
    Polaris (Railway) is 502-down as of 2026-05-20 (validator-confirmed). Not a blocker.

Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_sam_ucc_ca_debtor_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_sam_ucc_ca_debtor_lance.py
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
from scripts._lib.address_normalize import (
    __version__ as ADDR_NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# v2.0.0 (address-axis composite) — NEW method
# legal_name_state_exact_ca_with_address_corroboration v1.0.0; register
# method-definition + version-definition rows. Shared with the sibling
# ucc_ca_debtor/lender × sos_ca_owner bridges (idempotent UPSERT — three
# scripts registering the same method).
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps and constraint checks)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sam_ucc_ca_debtor"           # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "sam_ucc_ca_debtor_lance"    # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ca_with_address_corroboration"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "2.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-20 post full-corpus baseline probe (3× deterministic,
# stddev 0.0%). SAM national entities (norm-valid) = 884,174 rows; deduped UCC
# debtor names = 1,583,695. Observed non-rejected rows = 65,470 (platinum=50,954;
# gold=14,516; silver=0; rejected=3,270).
# Floor = 45,000 (~70% of measured 65,470; catches catastrophic failure from
# schema/normalizer regression without false-tripping on clean recipe runs).
MIN_ROWS_MATCHED = 45_000

SOURCE_LEFT = "sam_gov_entities_lance"
SOURCE_RIGHT = "ucc_ca_debtors_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance"
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ucc_ca_debtor_lance"
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
    """Load SAM national entities + UCC CA debtor names into Arrow tables.

    SAM side (national — NO CA pre-filter per validator ambiguity-(b) decision):
      - Read sam_gov/entities_lance: unique_entity_id + legal_business_name.
      - Push-down filter: legal_business_name is_valid() only (NO state filter —
        an out-of-state SAM entity can still be a CA UCC debtor).
      - Normalize legal_business_name Python-side via
        _lib.entity_name_normalize.normalize_entity_name (canonical _lib v1.0.0 —
        the SAME normalizer that produces the UCC join key).
      - Drop rows where the normalized name is None or empty.

    UCC debtor side (IDENTICAL to the PPP×UCC precedent):
      - Read ucc_ca/debtors_lance with push-down filter: DEBTOR_TYPE='Organization'
        (excludes ~2.2M individual rows).
      - Project ORG_NAME only (raw — the dataset has NO normalized-name column).
      - Normalize ORG_NAME Python-side via _lib.entity_name_normalize.normalize_entity_name.
      - Drop rows where normalized name is None or empty.
      - Return as Arrow table WITH debtor_name_normalized column.
      - The SELECT DISTINCT dedup happens later in DuckDB (constraint #8).
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # SAM left side — national, no CA pre-filter.
    logger.info("opening sam_gov/entities_lance (national — no CA filter) ...")
    sam_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sam_tbl = sam_ds.scanner(
        columns=[
            "unique_entity_id",
            "legal_business_name",
            "physical_address_base_normalized",
            "mailing_address_base_normalized",
        ],
        filter=pc.field("legal_business_name").is_valid(),
    ).to_table()
    rows_sam_raw = len(sam_tbl)
    logger.info(
        "  sam entities_lance (legal_business_name is_valid): %d rows",
        rows_sam_raw,
    )

    # Normalize legal_business_name Python-side via _lib (constraint #2).
    sam_names = sam_tbl.column("legal_business_name").to_pylist()
    sam_normalized = [normalize_entity_name(n) for n in sam_names]
    sam_tbl = sam_tbl.append_column(
        "sam_legal_name_normalized",
        pa.array(sam_normalized, type=pa.string()),
    )
    sam_valid_mask = pc.is_valid(sam_tbl.column("sam_legal_name_normalized"))
    sam_tbl = sam_tbl.filter(sam_valid_mask)
    rows_left = len(sam_tbl)
    logger.info(
        "  sam after _lib normalize (sam_legal_name_normalized is_valid): %d rows",
        rows_left,
    )
    # Validator-measured norm-valid figure: 884,174. Log for reproducibility.
    logger.info(
        "  expected sam norm-valid from validator probe: 884,174 (delta: %+d)",
        rows_left - 884_174,
    )

    # UCC right side — raw ORG_NAME, normalize Python-side, drop None/empty.
    # Constraint #2: ONLY _lib.entity_name_normalize — never the UCC-specific
    # normalizer (86.4% divergence; would break the join key).
    # v2.0.0: also pull pre-baked `address_base_normalized` per filing for
    # per-name address-set aggregation downstream.
    logger.info("opening ucc_ca/debtors_lance (DEBTOR_TYPE='Organization') ...")
    ucc_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    ucc_tbl = ucc_ds.scanner(
        columns=["ORG_NAME", "address_base_normalized"],
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
    valid_mask = pc.is_valid(ucc_tbl.column("debtor_name_normalized"))
    ucc_tbl = ucc_tbl.filter(valid_mask)
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

    return sam_tbl, ucc_tbl, rows_left, rows_ucc_raw


def _build_match_table(
    sam_tbl,
    ucc_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run dedup + exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Constraint #8: SELECT DISTINCT debtor_name_normalized dedup on the UCC side
    BEFORE the join. Assert: COUNT(*) == COUNT(DISTINCT debtor_name_normalized)
    on the deduped UCC input.

    Join key: sam.sam_legal_name_normalized = ucc.ucc_debtor_name_normalized
    BOTH are _lib v1.0.0 normalized — join directly, no re-normalize.

    Fan-out (CRITICAL asymmetry — constraint #6 / validator P3 tier-rule trap):
        sam_fan_out = COUNT(*) per sam_legal_name_normalized (# of SAM entity rows)
        ucc_fan_out = COUNT(DISTINCT debtor_name_normalized) per name (≡ 1 post-dedup)
    DO NOT use COUNT(*) for ucc_fan_out — that collapses gold=0.

    Expected post-dedup result: rows_matched=65,470; silver=0 (correct — ucc_fan_out≡1).
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sam", sam_tbl)
    con.register("ucc_filing", ucc_tbl)

    rows_sam_reg = con.execute("SELECT COUNT(*) FROM sam").fetchone()[0]
    rows_ucc_filing = con.execute("SELECT COUNT(*) FROM ucc_filing").fetchone()[0]
    logger.info(
        "  registered: sam=%d  ucc_filing=%d",
        rows_sam_reg, rows_ucc_filing,
    )

    # Constraint #8 dedup: collapse to debtor-name grain.
    # v2.0.0: also AGGREGATE all distinct UCC-filing addresses per name into a
    # LIST (`ucc_debtor_address_set`). A SAM entity's address matches if it
    # appears in this set — different UCC filings under the same legal name
    # may file from different addresses, so the per-name address agreement is
    # an OR across the set.
    con.execute(
        """
        CREATE TEMP TABLE ucc AS
        SELECT
            debtor_name_normalized AS ucc_debtor_name_normalized,
            LIST(DISTINCT address_base_normalized) FILTER (
                WHERE address_base_normalized IS NOT NULL
            ) AS ucc_debtor_address_set
        FROM ucc_filing
        WHERE debtor_name_normalized IS NOT NULL
          AND debtor_name_normalized <> ''
        GROUP BY 1
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

    # 1. Inner JOIN on normalized names; carry SAM addresses + UCC address set.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            s.unique_entity_id               AS sam_unique_entity_id,
            s.legal_business_name            AS sam_legal_business_name,
            s.sam_legal_name_normalized,
            s.physical_address_base_normalized AS sam_physical_address_base_normalized,
            s.mailing_address_base_normalized  AS sam_mailing_address_base_normalized,
            u.ucc_debtor_name_normalized,
            u.ucc_debtor_address_set,
            '{METHOD_NAME}'                  AS match_method,
            s.sam_legal_name_normalized      AS match_value,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'   AS generated_at
        FROM sam s
        JOIN ucc u
          ON s.sam_legal_name_normalized = u.ucc_debtor_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (CRITICAL asymmetry — constraint #6 / validator P3).
    #    sam_fan_out: # of SAM entity rows per normalized name.
    #    ucc_fan_out: # of distinct UCC debtor names per name (≡ 1 post-dedup always).
    #    DO NOT use COUNT(*) for ucc_fan_out — that would collapse gold=0.
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sam_legal_name_normalized, COUNT(*) AS sam_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ucc_fanout AS
        SELECT sam_legal_name_normalized,
               COUNT(DISTINCT ucc_debtor_name_normalized) AS ucc_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule + address agreement + composite tier (v2.0.0).
    #    silver is structurally unreachable post-dedup (ucc_fan_out ≡ 1).
    #    The bridge will be platinum + gold only — CORRECT, not a bug (constraint #6).
    #    Address agreement: SAM physical OR mailing must match any address in the
    #    aggregated UCC-side address set for this debtor name.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sam_fan_out,
            uf.ucc_fan_out,
            -- address agreement: ANY of SAM physical/mailing matches ANY entry in the UCC address set
            CASE
              WHEN b.ucc_debtor_address_set IS NULL OR LEN(b.ucc_debtor_address_set) = 0
                THEN FALSE
              WHEN list_contains(b.ucc_debtor_address_set, b.sam_physical_address_base_normalized)
                OR list_contains(b.ucc_debtor_address_set, b.sam_mailing_address_base_normalized)
                THEN TRUE
              ELSE FALSE
            END                                                  AS address_agrees,
            -- which SAM role matched (physical / mailing / both / NULL)
            NULLIF(
              CONCAT_WS('|',
                CASE WHEN b.sam_physical_address_base_normalized IS NOT NULL
                       AND list_contains(b.ucc_debtor_address_set, b.sam_physical_address_base_normalized)
                     THEN 'physical' END,
                CASE WHEN b.sam_mailing_address_base_normalized IS NOT NULL
                       AND list_contains(b.ucc_debtor_address_set, b.sam_mailing_address_base_normalized)
                     THEN 'mailing' END
              ),
              ''
            )                                                    AS address_match_path,
            -- the matching normalized address value (prefer physical; else mailing)
            CASE
              WHEN list_contains(b.ucc_debtor_address_set, b.sam_physical_address_base_normalized)
                THEN b.sam_physical_address_base_normalized
              WHEN list_contains(b.ucc_debtor_address_set, b.sam_mailing_address_base_normalized)
                THEN b.sam_mailing_address_base_normalized
              ELSE NULL
            END                                                  AS address_match_value,
            CASE
                WHEN sf.sam_fan_out > {COLLISION_THRESHOLD}
                  OR uf.ucc_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sam_fan_out = 1 AND uf.ucc_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sam_fan_out = 1 OR  uf.ucc_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                                  AS name_confidence_tier
        FROM bridge_raw b
        JOIN sam_fanout sf USING (sam_legal_name_normalized)
        JOIN ucc_fanout uf USING (sam_legal_name_normalized)
        """
    )

    # 4. Composite tier — address agreement promotes; absence holds.
    con.execute(
        """
        CREATE TEMP TABLE bridge_tiered AS
        SELECT
            b.*,
            CASE
                WHEN b.name_confidence_tier = 'rejected'                                 THEN 'rejected'
                WHEN b.name_confidence_tier = 'platinum' AND b.address_agrees             THEN 'platinum'
                WHEN b.name_confidence_tier = 'platinum' AND NOT b.address_agrees         THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND b.address_agrees             THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND NOT b.address_agrees         THEN 'silver'
                ELSE 'silver'
            END AS composite_confidence_tier
        FROM bridge_all b
        """
    )

    # 5. Filter rejected rows before write. Drop the LIST column (ucc_debtor_address_set)
    # before Lance write — Lance 1.5.x's def-buffer cap can choke on LIST<VARCHAR> with
    # many fan-out elements; the per-name agreement is already captured in the boolean
    # + match_path/value columns. Per L54 (CLAUDE.md §"Source ingest invariant").
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            * EXCLUDE (ucc_debtor_address_set),
            composite_confidence_tier AS confidence_tier
        FROM bridge_tiered
        WHERE composite_confidence_tier <> 'rejected'
        """
    )

    counts_row = con.execute(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver'),
            COUNT(*) FILTER (WHERE address_agrees),
            COUNT(*) FILTER (WHERE address_match_path = 'physical'),
            COUNT(*) FILTER (WHERE address_match_path = 'mailing'),
            COUNT(*) FILTER (WHERE address_match_path = 'mailing|physical' OR address_match_path = 'physical|mailing')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_tiered WHERE composite_confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_address_agrees": counts_row[4],
        "rows_addr_physical_only": counts_row[5],
        "rows_addr_mailing_only": counts_row[6],
        "rows_addr_both": counts_row[7],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE (constraint #4).

    BTREE on sam_legal_name_normalized AND ucc_debtor_name_normalized.
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
                "sam_legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on sam_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sam_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index(
                "ucc_debtor_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on ucc_debtor_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on ucc_debtor_name_normalized FAILED: %s", e)
            raise
        # v2.0.0 filter accelerators (non-fatal — not identity keys).
        for col in ("composite_confidence_tier", "address_agrees"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE on %s: OK", col)
            except Exception as e:
                logger.warning("BTREE on %s failed (non-fatal): %s", col, e)

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
    """Build the SAM.gov national entities × CA UCC-1 debtors Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description=(
            "SAM.gov national entities × CA UCC-1 debtors Pattern B bridge generator. "
            "Resolves SAM-registered entities against deduped CA UCC-1 debtor names "
            "via legal_name_state_exact_ca (REUSE). "
            "The collateral-lien signal: SAM entities that pledged collateral via "
            "a California UCC-1 filing."
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
        "bridge: %s  method=%s v%s  name_norm=v%s  addr_norm=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, ADDR_NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)
    logger.info("floor: %d rows_matched", MIN_ROWS_MATCHED)

    # v2.0.0 — NEW composite address-axis method (shared with the two
    # ucc × sos_ca_owner sibling bridges; idempotent UPSERT keeps the row
    # consistent across all three writers).
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Composite legal-name + state exact match with address corroboration. "
            "Inner-joins LEFT and RIGHT on exact-equality normalized legal name "
            "(canonical _lib/entity_name_normalize, CA-state constrained); then "
            "checks whether LEFT's normalized physical address (base form, "
            "unit-stripped via _lib/address_normalize) equals ANY of the RIGHT "
            "side's address roles (e.g. SoS principal / principal_in_ca / "
            "mailing). Address agreement promotes the name-axis fan-out tier: "
            "platinum requires BOTH 1:1 name AND address corroboration."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py + _lib/address_normalize.py",
        normalizer_version=f"name v{NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        blacklist_module="(same as component normalizers)",
        blacklist_version=f"name v{NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        tier_rule_description=(
            "Name fan-out tier (1:1=platinum, 1:N|N:1=gold, N:M<=50=silver, "
            ">50=rejected) preserved as `name_confidence_tier`. Composite tier "
            "(in canonical `confidence_tier`): platinum REQUIRES name 1:1 AND "
            "address_agrees; without address corroboration the name-axis "
            "platinum demotes to gold and gold demotes to silver."
        ),
        rejection_rule_description=(
            "Reject when EITHER side's fan-out under the shared normalized name "
            "exceeds COLLISION_THRESHOLD (50)."
        ),
        input_columns_left=[
            "sam_legal_name_normalized",
            "physical_address_base_normalized",
            "mailing_address_base_normalized",
        ],
        input_columns_right=[
            "ucc_debtor_name_normalized", "address_base_normalized",
        ],
        output_value_description=(
            "(sam_unique_entity_id, ucc_debtor_name_normalized) name-grain pair "
            "with boolean address_agrees + address_match_path (which SAM-side "
            "address role(s) matched against the UCC debtor's per-name address "
            "set) + composite_confidence_tier."
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SAM.gov registered entities (national — no CA pre-filter) × CA UCC-1 "
            "debtor filings (deduped to debtor-name-grain) — v2.0.0 composite "
            "name+address axis. Aggregates each debtor name's distinct UCC-side "
            "address_base_normalized values into a per-name set; checks whether "
            "SAM's physical or mailing normalized address appears in that set; "
            "promotes/demotes the fan-out tier accordingly. Platinum requires "
            "both 1:1 name AND address corroboration."
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
        sam_tbl, ucc_tbl, rows_left, rows_ucc_raw = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sam_tbl, ucc_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge composite tier distribution (v2.0.0 — address-axis):")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum:               %d  (name 1:1 + address agrees)", counts["rows_tier1"])
        logger.info("    gold:                   %d  (name 1:1 OR (name 1:N|N:1 + address agrees))", counts["rows_tier2"])
        logger.info("    silver:                 %d  (residual — v2.0.0: name 1:N|N:1 with no address agreement)", counts["rows_tier3"])
        logger.info("  address_agrees:           %d  (any-role agreement)", counts["rows_address_agrees"])
        logger.info("    physical only:          %d", counts["rows_addr_physical_only"])
        logger.info("    mailing only:           %d", counts["rows_addr_mailing_only"])
        logger.info("    both:                   %d", counts["rows_addr_both"])
        logger.info(
            "  rows_collision_rejected:  %d",
            counts["rows_collision_rejected"],
        )

        # HARD FAIL before Lance write if rows_matched < floor.
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,} — check normalizer (constraint #2) and "
                f"UCC dedup (constraint #8)"
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
                "rows_address_agrees": counts["rows_address_agrees"],
                "rows_addr_physical_only": counts["rows_addr_physical_only"],
                "rows_addr_mailing_only": counts["rows_addr_mailing_only"],
                "rows_addr_both": counts["rows_addr_both"],
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
            "--table sam_ucc_ca_debtor_lance"
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
