#!/usr/bin/env python3
"""Emit ``bridges/sam_overture_lance`` — dark federal-contractor UEIs ↔
Overture business places, disambiguated by an Opus LLM step.

Rescue path
-----------
The "dark" set is federal-contractor UEIs that:
  * are active in FPDS or subawards in the last 90 days, AND
  * are NOT in ``bridges/sam_pdl_lance`` with a non-null pdl_linkedin_url, AND
  * have no ``entity_url`` in SAM ``entities_lance`` (slim spine surface), AND
  * have a full physical_address (line_1 + state + zip5).

For each dark UEI we join Overture US Places on (state, zip5, normalized
street). Where the candidate set is small enough (≤ COLLISION_THRESHOLD),
an Opus LLM disambiguation pass picks the Overture place that is the same
business as the SAM record. Output is a UEI-grain Lance bridge stamping
each rescued UEI with ``website_primary``.

Three stages — split across two ``--stage`` modes plus an
orchestrator-owned LLM step:

  Stage 1 (``--stage candidates``)
      Build dark UEI set + Overture candidate pool. Write
      ``/tmp/sam_overture_candidates.jsonl`` (one line per UEI).
      Auto-reject UEIs with > COLLISION_THRESHOLD candidates.

  Stage 2 (orchestrator)
      Fan out the JSONL across parallel Opus subagents; each subagent
      writes per-UEI decisions to ``/tmp/sam_overture_results.jsonl``.
      NOT in this script — the orchestrator owns it.

  Stage 3 (``--stage materialize``)
      Read the aggregated results JSONL; re-join against Overture + SAM;
      write Lance dataset; BTREE index on uei; Polaris registration.

Normalization
-------------
``_normalize_address_sql`` mirrors the chained-regexp_replace style of
``_normalize_domain_sql`` (build_bridge_sam_pdl_domain_lance.py:109).
Order: lower/trim → strip periods → strip suite/unit suffix → expand
multi-word directionals (NE/NW/SE/SW) → expand single-word directionals
→ expand street-type long-forms → strip non-alphanumeric → collapse
whitespace. Applied identically to both sides of the join.

Run
---
  Stage 1:
    cd apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
      uv run --with duckdb --with pylance --with pyarrow \\
      --with "psycopg[binary]" --with requests \\
      python -m scripts.build_bridge_sam_overture_lance --stage candidates

  Stage 3 (after Opus subagents have written results JSONL):
    doppler run --project hq-all --config prd -- \\
      uv run --with duckdb --with pylance --with pyarrow \\
      --with "psycopg[binary]" --with requests \\
      python -m scripts.build_bridge_sam_overture_lance --stage materialize --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger("build_bridge_sam_overture_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sam_overture_lance"
BRIDGE_VERSION = "1.0.0"

# R2 layout ------------------------------------------------------------------
SAM_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
FPDS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fpds_lance"
)
SUBAWARD_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/subaward_lance"
)
SAM_PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_lance"
)
OVERTURE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_overture_lance"
)
DATASET_SLUG = "sam_overture_lance"

# Tunables -------------------------------------------------------------------
ACTIVE_WINDOW_DAYS = 90
# Threshold chosen from observed fan-out distribution (2026-05-26 prod data):
# ≤25 captures 5,358 of 5,876 matched UEIs (94%) with 21,878 candidate rows.
# Above 25, additional rescue tails into office-tower / strip-mall cases that
# the LLM can't disambiguate from name+category alone.
COLLISION_THRESHOLD = 25
TMP_DIR = "/tmp/lance"
DEFAULT_CANDIDATES_JSONL = "/tmp/sam_overture_candidates.jsonl"
DEFAULT_RESULTS_JSONL = "/tmp/sam_overture_results.jsonl"


# --------------------------------------------------------------------------- #
# Storage + normalization helpers
# --------------------------------------------------------------------------- #


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _normalize_address_sql(raw_expr: str) -> str:
    """Address normalization SQL — chained regexp_replace, no UDFs.

    Mirrors the style of ``_normalize_domain_sql`` in
    build_bridge_sam_pdl_domain_lance.py:109. Applied identically to both
    sides of the SAM × Overture join.
    """
    e = f"lower(trim({raw_expr}))"
    # Strip all periods early — handles "n.e.", "p.o. box", "ste.".
    e = f"regexp_replace({e}, '\\.', '', 'g')"
    # Strip trailing unit suffix (suite|ste|apt|...|#) + the unit token.
    e = (
        f"regexp_replace({e}, "
        f"'\\s+(suite|ste|apartment|apt|unit|building|bldg|floor|fl|"
        f"room|rm|department|dept|office|ofc|number|no|box)\\s+[a-z0-9-]+\\s*$', '')"
    )
    e = f"regexp_replace({e}, '\\s+#\\s*[a-z0-9-]+\\s*$', '')"
    # Multi-word directionals first so single-word expansion doesn't shred them.
    e = f"regexp_replace({e}, '\\bnorth\\s*east\\b', 'ne', 'g')"
    e = f"regexp_replace({e}, '\\bnorth\\s*west\\b', 'nw', 'g')"
    e = f"regexp_replace({e}, '\\bsouth\\s*east\\b', 'se', 'g')"
    e = f"regexp_replace({e}, '\\bsouth\\s*west\\b', 'sw', 'g')"
    # Single-word directionals.
    e = f"regexp_replace({e}, '\\bnorth\\b', 'n', 'g')"
    e = f"regexp_replace({e}, '\\bsouth\\b', 's', 'g')"
    e = f"regexp_replace({e}, '\\beast\\b', 'e', 'g')"
    e = f"regexp_replace({e}, '\\bwest\\b', 'w', 'g')"
    # Street-type long-form → short-form.
    for long, short in (
        ("street", "st"), ("avenue", "ave"), ("boulevard", "blvd"),
        ("road", "rd"), ("drive", "dr"), ("lane", "ln"),
        ("court", "ct"), ("place", "pl"), ("highway", "hwy"),
        ("parkway", "pkwy"), ("circle", "cir"), ("square", "sq"),
        ("terrace", "ter"), ("turnpike", "tpke"), ("trail", "trl"),
        ("expressway", "expy"), ("plaza", "plz"),
    ):
        e = f"regexp_replace({e}, '\\b{long}\\b', '{short}', 'g')"
    # Strip remaining non-alphanumeric to spaces, collapse, trim.
    e = f"regexp_replace({e}, '[^a-z0-9 ]', ' ', 'g')"
    e = f"regexp_replace({e}, '\\s+', ' ', 'g')"
    e = f"trim({e})"
    return e


# --------------------------------------------------------------------------- #
# Stage 1 — candidate generation
# --------------------------------------------------------------------------- #


def _build_candidates(
    *,
    out_path: Path,
    collision_threshold: int,
    active_window_days: int,
) -> dict:
    """Stage 1: build dark UEI set, join Overture, write candidate JSONL.

    Returns a metrics dict.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb

    storage_options = _lance_storage_options()
    cutoff = (date.today() - timedelta(days=active_window_days)).isoformat()
    LOG.info(
        "stage 1 — dark UEI window: action_date >= %s (%dd)",
        cutoff, active_window_days,
    )
    LOG.info("collision_threshold = %d candidates/UEI", collision_threshold)

    # ---- read minimal columns from each Lance source --------------------- #
    LOG.info("opening usaspending/transaction_fpds_lance ...")
    fpds_ds = lance.dataset(FPDS_LANCE_URI, storage_options=storage_options)
    fpds_t = fpds_ds.scanner(
        columns=["recipient_uei", "action_date"],
        filter=(pc.field("action_date") >= cutoff)
            & pc.field("recipient_uei").is_valid(),
    ).to_table()
    LOG.info("  fpds 90d rows: %s", f"{fpds_t.num_rows:,}")

    LOG.info("opening usaspending/subaward_lance ...")
    sub_ds = lance.dataset(SUBAWARD_LANCE_URI, storage_options=storage_options)
    sub_t = sub_ds.scanner(
        columns=["sub_awardee_or_recipient_uei", "sub_action_date"],
        filter=(pc.field("sub_action_date") >= cutoff)
            & pc.field("sub_awardee_or_recipient_uei").is_valid(),
    ).to_table()
    LOG.info("  subaward 90d rows: %s", f"{sub_t.num_rows:,}")

    LOG.info("opening sam_gov/entities_lance ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_t = sam_ds.scanner(columns=[
        "unique_entity_id", "legal_business_name", "dba_name",
        "physical_address_line_1", "physical_address_line_2",
        "physical_address_city", "physical_address_state_normalized",
        "physical_address_zip5", "entity_url", "activation_date",
    ]).to_table()
    LOG.info("  sam rows: %s", f"{sam_t.num_rows:,}")

    LOG.info("opening bridges/sam_pdl_lance ...")
    spd_ds = lance.dataset(SAM_PDL_LANCE_URI, storage_options=storage_options)
    spd_t = spd_ds.scanner(columns=["uei", "pdl_linkedin_url"]).to_table()
    LOG.info("  sam_pdl_lance rows: %s", f"{spd_t.num_rows:,}")

    # ---- DuckDB pipeline -------------------------------------------------- #
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=6")
    con.execute("SET memory_limit='12GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.register("fpds", fpds_t)
    con.register("sub", sub_t)
    con.register("sam_raw", sam_t)
    con.register("spd", spd_t)

    con.execute(f"""
    CREATE TEMP TABLE active_ueis AS
    SELECT DISTINCT uei FROM (
      SELECT recipient_uei AS uei FROM fpds
       WHERE recipient_uei IS NOT NULL AND recipient_uei <> ''
         AND action_date >= '{cutoff}'
      UNION ALL
      SELECT sub_awardee_or_recipient_uei AS uei FROM sub
       WHERE sub_awardee_or_recipient_uei IS NOT NULL
         AND sub_awardee_or_recipient_uei <> ''
         AND sub_action_date >= '{cutoff}'
    )
    """)
    n_active = con.execute("SELECT COUNT(*) FROM active_ueis").fetchone()[0]
    LOG.info("active UEIs (90d): %s", f"{n_active:,}")

    con.execute("""
    CREATE TEMP TABLE has_pdl_linkedin AS
    SELECT DISTINCT uei FROM spd
    WHERE NULLIF(TRIM(pdl_linkedin_url), '') IS NOT NULL
    """)

    # SAM dedup: one row per UEI, preferring rows with entity_url + address.
    con.execute("""
    CREATE TEMP TABLE sam_one AS
    SELECT * FROM (
      SELECT *,
             ROW_NUMBER() OVER (
               PARTITION BY unique_entity_id
               ORDER BY (entity_url IS NOT NULL) DESC,
                        (physical_address_line_1 IS NOT NULL) DESC,
                        activation_date DESC NULLS LAST
             ) AS rn
      FROM sam_raw
      WHERE unique_entity_id IS NOT NULL
    ) WHERE rn = 1
    """)

    addr_sam_expr = _normalize_address_sql("physical_address_line_1")
    con.execute(f"""
    CREATE TEMP TABLE dark AS
    SELECT
      a.uei,
      s.legal_business_name                  AS sam_legal_business_name,
      s.dba_name                             AS sam_dba_name,
      s.physical_address_line_1              AS sam_address_line_1,
      s.physical_address_line_2              AS sam_address_line_2,
      s.physical_address_city                AS sam_city,
      s.physical_address_state_normalized    AS state,
      s.physical_address_zip5                AS zip5,
      {addr_sam_expr}                        AS norm_street
    FROM active_ueis a
    JOIN sam_one s ON s.unique_entity_id = a.uei
    LEFT JOIN has_pdl_linkedin p ON p.uei = a.uei
    WHERE p.uei IS NULL
      AND NULLIF(TRIM(s.entity_url), '') IS NULL
      AND s.physical_address_line_1 IS NOT NULL
      AND s.physical_address_state_normalized IS NOT NULL
      AND s.physical_address_zip5 IS NOT NULL
    """)
    n_dark = con.execute("SELECT COUNT(*) FROM dark").fetchone()[0]
    LOG.info("dark UEIs (addressable rescue pool): %s", f"{n_dark:,}")

    # ---- Overture: scope to dark states first, then to (state, zip5) ----- #
    states = sorted({r[0] for r in con.execute(
        "SELECT DISTINCT state FROM dark"
    ).fetchall()})
    LOG.info("opening overture/us_places_lance (state-scoped) ...")
    ov_ds = lance.dataset(OVERTURE_LANCE_URI, storage_options=storage_options)
    ov_t = ov_ds.scanner(
        columns=[
            "place_id", "name_primary", "address_freeform",
            "address_postcode_5", "address_region", "categories_primary",
            "phone_primary", "website_primary", "brand_name_primary",
        ],
        filter=pc.is_in(pc.field("address_region"),
                         value_set=pa.array(states))
            & pc.field("website_primary").is_valid()
            & pc.field("address_freeform").is_valid()
            & pc.field("address_postcode_5").is_valid(),
    ).to_table()
    LOG.info("  overture rows (state-scoped, with website + addr): %s",
             f"{ov_t.num_rows:,}")
    con.register("ov_raw", ov_t)

    addr_ov_expr = _normalize_address_sql("address_freeform")
    con.execute(f"""
    CREATE TEMP TABLE ov_scoped AS
    SELECT
      place_id, name_primary, address_freeform, address_postcode_5,
      address_region, categories_primary, phone_primary, website_primary,
      brand_name_primary,
      {addr_ov_expr} AS norm_street
    FROM ov_raw
    WHERE (address_region, address_postcode_5) IN (
              SELECT DISTINCT state, zip5 FROM dark)
      AND NULLIF(TRIM(website_primary), '') IS NOT NULL
    """)
    n_ov_scoped = con.execute("SELECT COUNT(*) FROM ov_scoped").fetchone()[0]
    LOG.info("overture in dark zips: %s", f"{n_ov_scoped:,}")

    # ---- Join + aggregate per UEI ---------------------------------------- #
    con.execute("""
    CREATE TEMP TABLE candidates AS
    SELECT
      d.uei,
      d.sam_legal_business_name,
      d.sam_dba_name,
      d.sam_address_line_1,
      d.sam_address_line_2,
      d.sam_city,
      d.state,
      d.zip5,
      d.norm_street       AS sam_address_normalized,
      o.place_id,
      o.name_primary      AS ov_name_primary,
      o.address_freeform  AS ov_address_freeform,
      o.norm_street       AS ov_address_normalized,
      o.categories_primary AS ov_categories_primary,
      o.brand_name_primary AS ov_brand_name_primary,
      o.website_primary    AS ov_website_primary,
      o.phone_primary      AS ov_phone_primary
    FROM dark d
    JOIN ov_scoped o
      ON o.address_region    = d.state
     AND o.address_postcode_5 = d.zip5
     AND o.norm_street        = d.norm_street
    """)
    n_cand_rows = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    n_ueis_with_cands = con.execute(
        "SELECT COUNT(DISTINCT uei) FROM candidates"
    ).fetchone()[0]
    LOG.info("candidate rows (pre-threshold): %s", f"{n_cand_rows:,}")
    LOG.info("UEIs with ≥1 candidate: %s / %s (%.1f%%)",
             f"{n_ueis_with_cands:,}", f"{n_dark:,}",
             100 * n_ueis_with_cands / max(n_dark, 1))

    # Per-UEI fan-out (used for auto-reject)
    con.execute("""
    CREATE TEMP TABLE per_uei_counts AS
    SELECT uei, COUNT(*) AS cands FROM candidates GROUP BY uei
    """)
    n_auto_reject = con.execute(
        f"SELECT COUNT(*) FROM per_uei_counts WHERE cands > {collision_threshold}"
    ).fetchone()[0]
    n_kept_ueis = con.execute(
        f"SELECT COUNT(*) FROM per_uei_counts WHERE cands <= {collision_threshold}"
    ).fetchone()[0]
    LOG.info(
        "auto-reject (UEIs with >%d candidates): %s",
        collision_threshold, f"{n_auto_reject:,}",
    )
    LOG.info(
        "kept UEIs (LLM-disambiguable): %s", f"{n_kept_ueis:,}",
    )

    # Fan-out distribution (audit)
    dist = con.execute("""
    SELECT bucket, ueis FROM (
      SELECT
        CASE WHEN cands = 1 THEN '01: 1'
             WHEN cands BETWEEN 2 AND 5 THEN '02: 2-5'
             WHEN cands BETWEEN 6 AND 10 THEN '03: 6-10'
             WHEN cands BETWEEN 11 AND 25 THEN '04: 11-25'
             WHEN cands BETWEEN 26 AND 50 THEN '05: 26-50'
             WHEN cands BETWEEN 51 AND 100 THEN '06: 51-100'
             ELSE '07: 100+' END AS bucket,
        COUNT(*) AS ueis
      FROM per_uei_counts
      GROUP BY 1
    ) ORDER BY bucket
    """).fetchall()
    LOG.info("--- fan-out distribution ---")
    for b, c in dist:
        LOG.info("  %s → %s", b, f"{c:,}")

    # ---- Aggregate kept candidates into one row per UEI ------------------ #
    con.execute(f"""
    CREATE TEMP TABLE kept AS
    SELECT c.* FROM candidates c
    JOIN per_uei_counts pc ON pc.uei = c.uei
    WHERE pc.cands <= {collision_threshold}
    """)

    # ---- Write JSONL: one line per UEI (with array of candidates) -------- #
    LOG.info("writing candidate JSONL → %s", out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for uei_row in con.execute("""
        SELECT DISTINCT uei, sam_legal_business_name, sam_dba_name,
               sam_address_line_1, sam_address_line_2, sam_city,
               state, zip5, sam_address_normalized
        FROM kept ORDER BY uei
        """).fetchall():
            uei = uei_row[0]
            cands = con.execute("""
            SELECT place_id, ov_name_primary, ov_address_freeform,
                   ov_address_normalized, ov_categories_primary,
                   ov_brand_name_primary, ov_website_primary, ov_phone_primary
            FROM kept WHERE uei = ? ORDER BY place_id
            """, [uei]).fetchall()
            rec = {
                "uei": uei,
                "sam_legal_business_name": uei_row[1],
                "sam_dba_name": uei_row[2],
                "sam_address_line_1": uei_row[3],
                "sam_address_line_2": uei_row[4],
                "sam_city": uei_row[5],
                "sam_state": uei_row[6],
                "sam_zip5": uei_row[7],
                "sam_address_normalized": uei_row[8],
                "candidates": [
                    {
                        "place_id": c[0],
                        "name_primary": c[1],
                        "address_freeform": c[2],
                        "address_normalized": c[3],
                        "categories_primary": c[4],
                        "brand_name_primary": c[5],
                        "website_primary": c[6],
                        "phone_primary": c[7],
                    }
                    for c in cands
                ],
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rows_written += 1
    LOG.info("JSONL: %s UEIs written", f"{rows_written:,}")

    return {
        "active_ueis_90d": n_active,
        "dark_ueis_addressable": n_dark,
        "ueis_with_candidates": n_ueis_with_cands,
        "candidate_rows_pre_threshold": n_cand_rows,
        "auto_rejected_ueis": n_auto_reject,
        "kept_ueis": n_kept_ueis,
    }


# --------------------------------------------------------------------------- #
# Stage 3 — materialize the bridge Lance from aggregated results
# --------------------------------------------------------------------------- #


def _materialize_bridge(
    *,
    results_jsonl: Path,
    apply: bool,
    bridge_run_id: str,
    generated_at_iso: str,
) -> dict:
    """Stage 3: read Opus results, re-join, write Lance, register Polaris."""
    import duckdb
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    storage_options = _lance_storage_options()

    LOG.info("reading results JSONL: %s", results_jsonl)
    if not results_jsonl.exists():
        raise FileNotFoundError(f"results JSONL not found: {results_jsonl}")

    # Aggregate rows: one per (uei, selected_place_id) where selected_place_id
    # is non-null OR confidence == "none". We materialize only the matched
    # rows; explicitly-rejected UEIs do not get a bridge row.
    matched_rows = []
    rejected_count = 0
    none_count = 0
    invalid_count = 0
    with results_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                invalid_count += 1
                continue
            uei = rec.get("uei")
            selected = rec.get("selected_place_id")
            confidence = (rec.get("confidence") or "").lower()
            if not uei:
                invalid_count += 1
                continue
            if confidence == "none" or selected is None or selected == "":
                if confidence == "none":
                    none_count += 1
                else:
                    rejected_count += 1
                continue
            if confidence not in ("high", "medium", "low"):
                invalid_count += 1
                continue
            matched_rows.append({
                "uei": uei,
                "selected_place_id": selected,
                "confidence": confidence,
                "reasoning_summary": rec.get("reasoning_summary") or "",
            })
    LOG.info(
        "results parsed: matched=%d  none=%d  rejected=%d  invalid=%d",
        len(matched_rows), none_count, rejected_count, invalid_count,
    )
    if not matched_rows:
        raise RuntimeError("no matched rows in results JSONL — nothing to write")

    # ---- Re-join against Overture + SAM for canonical field values ------- #
    LOG.info("opening overture (full) for re-join ...")
    ov_ds = lance.dataset(OVERTURE_LANCE_URI, storage_options=storage_options)
    place_ids = sorted({r["selected_place_id"] for r in matched_rows})
    LOG.info("re-joining %s distinct place_ids ...", f"{len(place_ids):,}")
    ov_t = ov_ds.scanner(
        columns=[
            "place_id", "name_primary", "address_freeform",
            "address_postcode_5", "address_region", "categories_primary",
            "phone_primary", "website_primary", "brand_name_primary",
        ],
        filter=pc.is_in(pc.field("place_id"),
                         value_set=pa.array(place_ids)),
    ).to_table()
    LOG.info("  overture matched rows: %s", f"{ov_t.num_rows:,}")

    LOG.info("opening sam_gov/entities_lance for re-join ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    uei_list = sorted({r["uei"] for r in matched_rows})
    sam_t = sam_ds.scanner(
        columns=[
            "unique_entity_id", "legal_business_name", "dba_name",
            "physical_address_line_1", "physical_address_line_2",
            "physical_address_city", "physical_address_state_normalized",
            "physical_address_zip5", "activation_date",
        ],
        filter=pc.is_in(pc.field("unique_entity_id"),
                         value_set=pa.array(uei_list)),
    ).to_table()
    LOG.info("  sam matched rows: %s", f"{sam_t.num_rows:,}")

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='6GB'")
    con.register("ov", ov_t)
    con.register("sam_raw", sam_t)
    con.execute(
        "CREATE TEMP TABLE matched(uei VARCHAR, selected_place_id VARCHAR, "
        "confidence VARCHAR, reasoning_summary VARCHAR)"
    )
    con.executemany(
        "INSERT INTO matched VALUES (?, ?, ?, ?)",
        [(r["uei"], r["selected_place_id"], r["confidence"],
          r["reasoning_summary"]) for r in matched_rows],
    )

    # SAM dedup (mirrors Stage 1 logic).
    con.execute("""
    CREATE TEMP TABLE sam_one AS
    SELECT * FROM (
      SELECT *,
             ROW_NUMBER() OVER (
               PARTITION BY unique_entity_id
               ORDER BY (physical_address_line_1 IS NOT NULL) DESC,
                        activation_date DESC NULLS LAST
             ) AS rn
      FROM sam_raw WHERE unique_entity_id IS NOT NULL
    ) WHERE rn = 1
    """)

    addr_sam_expr = _normalize_address_sql("physical_address_line_1")
    addr_ov_expr = _normalize_address_sql("address_freeform")
    con.execute(f"""
    CREATE TEMP TABLE bridge_match AS
    SELECT
      m.uei                                          AS uei,
      s.legal_business_name                          AS sam_legal_business_name,
      s.dba_name                                     AS sam_dba_name,
      s.physical_address_line_1                      AS sam_address_line_1,
      s.physical_address_line_2                      AS sam_address_line_2,
      s.physical_address_city                        AS sam_city,
      s.physical_address_state_normalized            AS sam_state,
      s.physical_address_zip5                        AS sam_zip5,
      {addr_sam_expr}                                AS sam_address_normalized,
      m.selected_place_id                            AS overture_place_id,
      o.name_primary                                 AS overture_name_primary,
      o.address_freeform                             AS overture_address_freeform,
      {addr_ov_expr}                                 AS overture_address_normalized,
      o.categories_primary                           AS overture_categories_primary,
      o.brand_name_primary                           AS overture_brand_name_primary,
      o.phone_primary                                AS overture_phone_primary,
      o.website_primary                              AS website_primary,
      CASE m.confidence
        WHEN 'high'   THEN 'platinum'
        WHEN 'medium' THEN 'gold'
        WHEN 'low'    THEN 'silver'
      END                                            AS confidence_tier,
      m.confidence                                   AS llm_confidence,
      m.reasoning_summary                            AS llm_reasoning_summary,
      TIMESTAMP '{generated_at_iso}'                 AS generated_at,
      '{BRIDGE_VERSION}'                             AS bridge_version,
      '{bridge_run_id}'                              AS bridge_run_id
    FROM matched m
    JOIN ov  o ON o.place_id = m.selected_place_id
    JOIN sam_one s ON s.unique_entity_id = m.uei
    """)
    counts_row = con.execute("""
    SELECT
      COUNT(*),
      COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
      COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
      COUNT(*) FILTER (WHERE confidence_tier = 'silver')
    FROM bridge_match
    """).fetchone()
    counts = {
        "rows_matched":     counts_row[0],
        "rows_platinum":    counts_row[1],
        "rows_gold":        counts_row[2],
        "rows_silver":      counts_row[3],
        "rows_none":        none_count,
        "rows_rejected":    rejected_count,
        "rows_invalid":     invalid_count,
    }
    LOG.info("--- tier distribution ---")
    for k, v in counts.items():
        LOG.info("  %-20s %s", k + ":", f"{v:,}")

    if not apply:
        LOG.info("DRY RUN — not writing Lance or registering Polaris")
        return counts

    # ---- Write Lance + BTREE + Polaris ----------------------------------- #
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        LOG.info("writing bridge Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=10_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_count = ds.count_rows()
        LOG.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, time.time() - t0, ds.version,
        )

        try:
            ds.create_scalar_index("uei", index_type="BTREE", replace=True)
            LOG.info("BTREE on uei: OK")
        except Exception as e:
            LOG.warning("BTREE create failed (non-fatal): %s", e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            LOG.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            LOG.warning("cleanup_old_versions failed (non-fatal): %s", e)

    register_or_update_polaris(
        namespace="bridges",
        table_name=DATASET_SLUG,
        s3_uri=BRIDGE_LANCE_URI.rstrip("/") + "/",
        docstring=(
            "SAM dark UEIs ↔ Overture US Places via physical address match + "
            "Opus LLM disambiguation. Stamps each rescued UEI with "
            "Overture's website_primary. UEI-grain."
        ),
    )
    LOG.info("polaris registration OK")
    return counts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--stage",
        choices=("candidates", "materialize"),
        required=True,
        help="candidates = Stage 1 (write JSONL); materialize = Stage 3 (write Lance)",
    )
    p.add_argument(
        "--candidates-out",
        default=DEFAULT_CANDIDATES_JSONL,
        help=f"Stage 1 output JSONL path (default: {DEFAULT_CANDIDATES_JSONL})",
    )
    p.add_argument(
        "--results-in",
        default=DEFAULT_RESULTS_JSONL,
        help=f"Stage 3 results JSONL path (default: {DEFAULT_RESULTS_JSONL})",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=COLLISION_THRESHOLD,
        help=f"Stage 1 candidates-per-UEI auto-reject threshold (default: {COLLISION_THRESHOLD})",
    )
    p.add_argument(
        "--window-days",
        type=int,
        default=ACTIVE_WINDOW_DAYS,
        help=f"Stage 1 active-UEI window in days (default: {ACTIVE_WINDOW_DAYS})",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--apply", action="store_true",
                     help="(materialize) write Lance + register Polaris")
    grp.add_argument("--dry-run", action="store_true",
                     help="(materialize) count only, no writes")
    args = p.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    t_total = time.time()
    if args.stage == "candidates":
        out = Path(args.candidates_out)
        metrics = _build_candidates(
            out_path=out,
            collision_threshold=args.threshold,
            active_window_days=args.window_days,
        )
        LOG.info("=" * 60)
        LOG.info("STAGE 1 OK — duration=%.1fs", time.time() - t_total)
        for k, v in metrics.items():
            LOG.info("  %-30s %s", k + ":", f"{v:,}")
        LOG.info("next: orchestrator fans %s out to Opus subagents", out)
        return 0

    # Stage 3
    if args.apply == args.dry_run:
        LOG.error("materialize: pass --apply or --dry-run")
        return 64
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        LOG.error("FAIL: DEX_DB_URL_DIRECT not set (required for Lance commit lock)")
        return 64

    bridge_run_id = str(uuid.uuid4())
    generated_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    LOG.info("bridge: %s  bridge_run_id=%s", BRIDGE_NAME, bridge_run_id)

    counts = _materialize_bridge(
        results_jsonl=Path(args.results_in),
        apply=args.apply,
        bridge_run_id=bridge_run_id,
        generated_at_iso=generated_at_iso,
    )
    LOG.info("=" * 60)
    LOG.info(
        "STAGE 3 OK — apply=%s rows_matched=%s duration=%.1fs",
        args.apply, f"{counts['rows_matched']:,}", time.time() - t_total,
    )
    LOG.info("output: %s", BRIDGE_LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
