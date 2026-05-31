#!/usr/bin/env python3
"""Lance-on-R2 bridge generator: SEC ADV firms × PDL companies via legal_name + state.

Pattern B Lance bridge — national composite-key (normalized_legal_name, 2-letter US state).

Cycle 3 of 4 in the RIA-spine bridge sequence. Long-tail catcher for RIAs not matched
by website in sec_adv_pdl_domain_lance (cycle 2).

References:
  L17  — every output row carries bridge_run_id UUID (per-row provenance).
  L21  — Path B: register NEW national method legal_name_state_exact v1.0.0.
         Do NOT reuse company_name_state_exact or name_state_exact — both are
         SBA-shape-hardcoded; register_match_method_version ON CONFLICT DO UPDATE
         would clobber input_columns_left/right for 7-11 other bridges. A new
         method is the only clean path. State-scoped variants (_ca/_fl/_ny/_tx)
         are untouched (regression-protected by G10 sub-check 2).
  L10  — normalizer imported from _lib/entity_name_normalize.py (canonical).
         Never inline the regex. PDL's legal_name_normalized is ALREADY normalized
         via the same module — do NOT re-normalize the PDL side.
  L9   — composite-key (name, state) JOIN with Python pre-dedup on ADV side
         (distinct firm-grain pairs). Plain single-key join on name-only risks
         111+ GiB DuckDB temp spill (UCC-PDL precedent lesson).
  L47  — modal run --detach for >5min jobs.
  L50  — ops.data_sources 5-col shape; no 'kind' column.

ADV source-table findings (validator):
  - legal_name: base_a_lance.legal_name — TOP-LEVEL column, NOT raw_json.
  - state: base_a_lance.raw_json["1F1-State"] filtered by raw_json["1F1-Country"]
    == "United States". State is in raw_json sub-key (2-letter US code).
  - Filing grain: base_a_lance has 471,129 rows across 27,679 distinct CRDs.
    Take LATEST filing per CRD by date_submitted for firm-grain dedup.
  - schedule_d_1i_lance has ONLY website + crd_number — irrelevant for this cycle.

PDL source:
  - pdl/free_companies_lance — columns: pdl_id, legal_name_normalized (already normalized),
    state (2-letter US). Do NOT re-normalize PDL side.

Cohort (validator):
  - 10,680 distinct (norm_name, state) pairs intersect PDL
  - 11,225 post-join rows
  - MIN_ROWS_MATCHED floor = 7,000 (0.7 × 10,680 rounded down)

G9 smoke: CRD 25 → "ALLEN & COMPANY OF FLORIDA, LLC" → normalized "allen of florida" + FL
          → pdl_id=9wfJNmjCtrE1HoL1GCe2ZAsmM6WV (platinum 1:1)

Usage:
    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/build_bridge_sec_adv_pdl_name_state_lance.py --apply

    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/build_bridge_sec_adv_pdl_name_state_lance.py --dry-run
"""
from __future__ import annotations

import argparse
import json
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
    normalize_entity_name as py_normalize,
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
logger = logging.getLogger("build_bridge_sec_adv_pdl_name_state_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sec_adv_pdl_name_state_lance"
METHOD_NAME = "legal_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sec_adv_base_a_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

# R2 layout ------------------------------------------------------------------
ADV_BASE_A_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/base_a_lance"
PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_adv_pdl_name_state_lance"
DATASET_SLUG = BRIDGE_NAME

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50
# Floor: 0.7 × 10,680 validator-measured intersecting pairs ≈ 7,476.
# Rounded down to 7,000 for safety margin.
# HARD FAIL if rows_matched < MIN_ROWS_MATCHED.
MIN_ROWS_MATCHED = 7_000
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


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read ADV base_a_lance + PDL free_companies_lance via PyLance scanner.

    ADV side:
      - Read crd_number, legal_name, raw_json from base_a_lance.
      - Parse raw_json to extract 1F1-State (filter 1F1-Country == "United States").
      - Take LATEST filing per CRD by date_submitted (firm-grain dedup).
      - Normalize legal_name via py_normalize.
      - Pre-dedup to distinct (legal_name_normalized, adv_state) pairs (L9 footnote:
        composite-key JOIN with Python pre-dedup avoids 111+ GiB DuckDB temp spill).
      - Keep back-mapping: crd_to_pair dict for join output.

    PDL side:
      - Read pdl_id, legal_name_normalized, state from free_companies_lance.
      - Filter: state IS NOT NULL AND legal_name_normalized IS NOT NULL.
      - Do NOT re-normalize PDL side (already canonical per _lib/entity_name_normalize.py).
    """
    import lance
    import pyarrow as pa

    logger.info("opening sec_adv/base_a_lance ...")
    adv_ds = lance.dataset(ADV_BASE_A_LANCE_URI, storage_options=storage_options)
    adv_raw = adv_ds.scanner(
        columns=["crd_number", "legal_name", "raw_json", "date_submitted"],
    ).to_table()
    rows_adv_raw = len(adv_raw)
    logger.info("  base_a_lance: %d rows (filing-grain)", rows_adv_raw)

    # --- Firm-grain dedup: take latest filing per CRD by date_submitted -----
    # Then parse raw_json to extract 1F1-State with 1F1-Country filter.
    logger.info("  parsing raw_json for 1F1-State + firm-grain dedup ...")
    crd_numbers = adv_raw.column("crd_number").to_pylist()
    legal_names = adv_raw.column("legal_name").to_pylist()
    raw_jsons = adv_raw.column("raw_json").to_pylist()
    date_submitteds = adv_raw.column("date_submitted").to_pylist()

    # Step 1: find latest date_submitted per CRD
    crd_latest: dict[int, tuple] = {}  # crd -> (date_submitted, legal_name, state)
    for crd, lname, rj, ds_val in zip(crd_numbers, legal_names, raw_jsons, date_submitteds):
        if crd is None:
            continue
        # Parse raw_json to get state
        state = None
        try:
            if isinstance(rj, str):
                rj_dict = json.loads(rj)
            elif isinstance(rj, bytes):
                rj_dict = json.loads(rj.decode("utf-8"))
            elif isinstance(rj, dict):
                rj_dict = rj
            else:
                rj_dict = {}
            country = rj_dict.get("1F1-Country", "")
            if country == "United States":
                raw_state = rj_dict.get("1F1-State", "")
                if raw_state and len(str(raw_state).strip()) == 2:
                    state = str(raw_state).strip().upper()
        except Exception:
            pass

        # Keep only entries with valid state
        if state is None:
            continue

        # Firm-grain: keep latest by date_submitted
        prev = crd_latest.get(crd)
        if prev is None or (ds_val is not None and ds_val > prev[0]):
            crd_latest[crd] = (ds_val, lname, state, crd)

    del adv_raw, crd_numbers, legal_names, raw_jsons, date_submitteds
    logger.info("  firm-grain (US state filter): %d distinct CRDs", len(crd_latest))

    # Step 2: normalize legal_name and build crd-level table + distinct pairs
    # Python pre-dedup pattern per L9 footnote (UCC-PDL precedent lines 148-171):
    # build distinct (normalized_name, state) set BEFORE handing to DuckDB.
    branded_set: set = set()
    # Also keep per-crd rows for the join output (crd_number, norm_name, adv_state)
    adv_crd_rows: list[tuple] = []  # (crd_number, legal_name_normalized, adv_state)

    for crd, (ds_val, lname, state, _) in crd_latest.items():
        norm_name = py_normalize(lname) if lname else None
        if norm_name is None or state is None:
            continue
        branded_set.add((norm_name, state))
        adv_crd_rows.append((crd, norm_name, state))

    branded_rows = list(branded_set)
    logger.info(
        "  adv_branded distinct (name, state) pairs: %d (from %d CRD rows)",
        len(branded_rows), len(adv_crd_rows),
    )

    # Build Arrow tables:
    # 1. Distinct pairs for the JOIN (OOM-resistant)
    adv_branded_arrow = pa.table({
        "legal_name_normalized": pa.array([r[0] for r in branded_rows], type=pa.string()),
        "adv_state":             pa.array([r[1] for r in branded_rows], type=pa.string()),
    })
    # 2. Per-CRD table for output enrichment (crd_number per match)
    adv_crd_arrow = pa.table({
        "crd_number":            pa.array([r[0] for r in adv_crd_rows], type=pa.int64()),
        "legal_name_normalized": pa.array([r[1] for r in adv_crd_rows], type=pa.string()),
        "adv_state":             pa.array([r[2] for r in adv_crd_rows], type=pa.string()),
    })
    del branded_set, branded_rows, adv_crd_rows, crd_latest
    rows_adv = len(adv_branded_arrow)

    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id",
            "pdl_name",
            "legal_name_normalized",
            "state",
            "pdl_website",
            "pdl_linkedin_url",
            "pdl_industry",
            "pdl_size",
            "pdl_founded",
            "pdl_locality",
        ]
    ).to_table()
    rows_pdl = len(pdl_arrow)
    logger.info("  pdl free_companies_lance: %d rows", rows_pdl)

    return adv_branded_arrow, adv_crd_arrow, pdl_arrow, rows_adv_raw, rows_pdl


def _build_match_table(
    adv_branded_arrow,
    adv_crd_arrow,
    pdl_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Composite-key JOIN + fan-out tiering in DuckDB (Arrow-bridge pattern).

    Pattern per L9 footnote (UCC-PDL precedent):
      - adv_branded_arrow is the Python-pre-deduped distinct (name, state) pairs.
      - Composite-key JOIN: JOIN ... ON legal_name_normalized AND adv_state = state.
      - Then re-join adv_crd to expand to per-crd output rows.
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    # Register Arrow tables (Arrow-bridge pattern — NOT the lance-duckdb extension)
    con.register("adv_branded", adv_branded_arrow)
    con.register("adv_crd", adv_crd_arrow)
    con.register("pdl_raw", pdl_arrow)

    rows_adv_branded = con.execute("SELECT COUNT(*) FROM adv_branded").fetchone()[0]
    rows_adv_crd = con.execute("SELECT COUNT(*) FROM adv_crd").fetchone()[0]
    rows_pdl = con.execute("SELECT COUNT(*) FROM pdl_raw").fetchone()[0]
    logger.info(
        "  registered: adv_branded=%d  adv_crd=%d  pdl_raw=%d",
        rows_adv_branded, rows_adv_crd, rows_pdl,
    )

    # Composite-key JOIN on distinct pairs (selective, streams without spilling)
    # JOIN on (legal_name_normalized, state) — the two-column key is critical
    # to avoid cartesian explosion on common names across states (L9 footnote).
    con.execute("""
        CREATE TEMP TABLE matched_pairs AS
        SELECT
            a.legal_name_normalized,
            a.adv_state,
            p.pdl_id,
            p.pdl_name,
            p.pdl_website,
            p.pdl_linkedin_url,
            p.pdl_industry,
            p.pdl_size,
            p.pdl_founded,
            p.pdl_locality
        FROM adv_branded a
        JOIN pdl_raw p
          ON p.legal_name_normalized = a.legal_name_normalized
         AND p.state                 = a.adv_state
        WHERE p.legal_name_normalized IS NOT NULL
          AND p.state IS NOT NULL
    """)
    rows_pairs = con.execute("SELECT COUNT(*) FROM matched_pairs").fetchone()[0]
    logger.info("  matched_pairs (distinct-key join): %d rows", rows_pairs)

    # Re-join adv_crd to expand pairs back to per-CRD output rows
    con.execute("""
        CREATE TEMP TABLE matched_expanded AS
        SELECT
            c.crd_number,
            m.legal_name_normalized,
            m.adv_state,
            m.pdl_id,
            m.pdl_name,
            m.pdl_website,
            m.pdl_linkedin_url,
            m.pdl_industry,
            m.pdl_size,
            m.pdl_founded,
            m.pdl_locality
        FROM matched_pairs m
        JOIN adv_crd c
          ON c.legal_name_normalized = m.legal_name_normalized
         AND c.adv_state             = m.adv_state
    """)
    rows_expanded = con.execute("SELECT COUNT(*) FROM matched_expanded").fetchone()[0]
    logger.info("  matched_expanded (with crd_number): %d rows", rows_expanded)

    # Fan-out counts
    con.execute("""
        CREATE TEMP TABLE adv_fanout AS
        SELECT legal_name_normalized, adv_state, COUNT(DISTINCT crd_number) AS adv_fan_out
        FROM matched_expanded
        GROUP BY legal_name_normalized, adv_state
    """)
    con.execute("""
        CREATE TEMP TABLE pdl_fanout AS
        SELECT pdl_id, COUNT(*) AS pdl_fan_out
        FROM matched_expanded
        GROUP BY pdl_id
    """)

    # Tier + provenance
    # match_value = f"{normalized_name}|{adv_state}" per validator notes
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            e.crd_number,
            e.pdl_id,
            '{METHOD_NAME}'                              AS match_method,
            e.legal_name_normalized || '|' || e.adv_state AS match_value,
            af.adv_fan_out,
            pf.pdl_fan_out,
            e.pdl_name,
            e.pdl_website,
            e.pdl_linkedin_url,
            e.pdl_industry,
            e.pdl_size,
            e.pdl_founded,
            e.pdl_locality,
            CASE
                WHEN af.adv_fan_out > {COLLISION_THRESHOLD}
                  OR pf.pdl_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN af.adv_fan_out = 1 AND pf.pdl_fan_out = 1
                    THEN 'platinum'
                WHEN af.adv_fan_out = 1 OR pf.pdl_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                          AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'               AS generated_at,
            '{BRIDGE_VERSION}'                           AS bridge_version,
            '{bridge_run_id}'                            AS bridge_run_id
        FROM matched_expanded e
        JOIN adv_fanout af
          ON af.legal_name_normalized = e.legal_name_normalized
         AND af.adv_state             = e.adv_state
        JOIN pdl_fanout pf
          ON pf.pdl_id = e.pdl_id
        """
    )
    con.execute("""
        CREATE TEMP TABLE bridge_match AS
        SELECT
            crd_number, pdl_id, match_method, match_value,
            confidence_tier, adv_fan_out, pdl_fan_out,
            pdl_name, pdl_website, pdl_linkedin_url, pdl_industry,
            pdl_size, pdl_founded, pdl_locality,
            generated_at, bridge_version, bridge_run_id
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
    """)

    row_counts = con.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE confidence_tier='platinum'),
               COUNT(*) FILTER (WHERE confidence_tier='gold'),
               COUNT(*) FILTER (WHERE confidence_tier='silver')
        FROM bridge_match
    """).fetchone()
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
    """Lance write inside commit lock; create BTREE on crd_number."""
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

        try:
            ds.create_scalar_index("crd_number", index_type="BTREE", replace=True)
            logger.info("  BTREE on crd_number created")
        except Exception as e:
            logger.warning("BTREE index failed (non-fatal): %s", e)
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
    """Register new national method + method version + bridge (L21 Path B).

    Path B — register a BRAND NEW national method legal_name_state_exact v1.0.0.
    This method does NOT exist pre-execution; creating it carries zero clobber risk.
    Do NOT modify company_name_state_exact or name_state_exact (SBA-shaped, 7-11 other
    bridges depend on their input_columns_left/right via ON CONFLICT DO UPDATE).
    Do NOT modify legal_name_state_exact_{ca,fl,ny,tx} state-specific variants
    (regression-protected by G10 sub-check 2).
    """
    # Step 1: register the new method in ops.match_methods (UPSERT, idempotent)
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (entity_name_normalized, 2-letter US state). "
            "National scope; applies _lib/entity_name_normalize.py v1.0.0 on both sides. "
            "Distinct from company_name_state_exact (which is SBA-shaped) and from "
            "state-scoped legal_name_state_exact_{ca,fl,ny,tx} variants."
        ),
    )
    # Step 2: register the version row in ops.match_method_versions (UPSERT, idempotent)
    # This IS a new row — first time this method+semver appears. input_columns_left
    # uses ADV-specific column names to avoid clobbering SBA-shaped methods.
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description="platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50",
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["legal_name_normalized", "adv_state"],
        input_columns_right=["legal_name_normalized", "state"],
        output_value_description="normalized legal-entity name + 2-letter US state composite join key",
    )
    # Step 3: register the bridge in ops.bridges (UPSERT, idempotent)
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SEC ADV firms × PDL companies via composite-key (normalized legal_name, "
            "2-letter US state). Long-tail catcher for RIAs not matched by website "
            "in sec_adv_pdl_domain_lance (cycle 2)."
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

    logger.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("normalizer: _lib/entity_name_normalize.py v%s", NORMALIZER_VERSION)
    logger.info("inputs: %s + %s (Arrow-bridge)", ADV_BASE_A_LANCE_URI, PDL_LANCE_URI)
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
        adv_branded_arrow, adv_crd_arrow, pdl_arrow, rows_adv_raw, rows_pdl = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            adv_branded_arrow,
            adv_crd_arrow,
            pdl_arrow,
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
                "rows_left": rows_adv_raw,
                "rows_right": rows_pdl,
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
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
