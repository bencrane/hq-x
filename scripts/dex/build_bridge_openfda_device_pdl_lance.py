#!/usr/bin/env python3
"""Lance-on-R2 bridge generator: openFDA Medical Device applicants x PDL companies.

Pattern B identity bridge. Resolves cleared/approved medical-device companies —
510(k) clearance applicants + PMA approval applicants — to PDL companies via
normalized name + 2-letter US state exact match. The deliverable is the company
website: every output row carries PDL's pdl_website through, so a cleared device
company resolves to its domain.

Single-path (no GLEIF parent layer). Modeled on build_bridge_sam_pdl_domain_lance.py
(single-path skeleton + bridge-instance-only registry) and build_bridge_ucc_pdl_lance.py
(name+state matching, Python-side normalization to avoid a DuckDB regex spill).

Inputs (PyLance scanner -> Arrow -> DuckDB tables):
  - openfda/device_510k_lance  — applicant + state (one row per k_number).
  - openfda/device_pma_lance   — applicant + state (one row per pma_number+supplement).
  - pdl/free_companies_lance   — pdl_id, legal_name_normalized, state, pdl_website.

openFDA is at clearance/approval grain (one company across many k_number / pma_number
rows). The generator UNIONs the two openFDA datasets and dedups to distinct
(normalized applicant name, 2-letter state) pairs — company grain — before joining.

Output: polaris-warehouse/bridges/openfda_device_pdl_lance, BTREE on
applicant_name_normalized (the join key).

Match-method REUSE (L21): the name+state semantics are identical to the existing
company_name_state_exact rule (exact equality on entity_name_normalize v1.0.0 +
2-letter US state), shared by the ucc_pdl / pdl_sba_borrower / ucc_gleif bridges.
This generator registers ONLY the bridge-instance row in ops.bridges (via
register_bridge) and starts/completes/fails the run via the bridge-run helpers.
It deliberately does NOT touch the shared method or its per-version row — those
helpers do idempotent UPSERTs ON CONFLICT DO UPDATE, so writing the shared
per-version row with openFDA-shape source columns would clobber the config the
other three bridges depend on. Precedent: build_bridge_sam_pdl_domain_lance.py
_ensure_registry(), which imports only the bridge-instance + run helpers for the
same reason.

Floor: >= 2,500 matched rows (validator-measured natural count ~5,431). HARD FAIL
if rows_matched < MIN_ROWS_MATCHED.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow \\
        --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/build_bridge_openfda_device_pdl_lance.py --apply

    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow \\
        --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/build_bridge_openfda_device_pdl_lance.py --dry-run
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_openfda_device_pdl_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "openfda_device_pdl"        # ops.bridges natural key (slug, no _lance suffix)
METHOD_NAME = "company_name_state_exact"  # REUSE — shared with ucc_pdl; not re-registered
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "openfda_device_lance"      # 510k + pma UNION — descriptive source-left label
SOURCE_RIGHT = "pdl_free_companies_lance"

# R2 layout ------------------------------------------------------------------
DEVICE_510K_URI = "s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_510k_lance"
DEVICE_PMA_URI = "s3://dex-raw-landing-zone/polaris-warehouse/openfda/device_pma_lance"
PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/openfda_device_pdl_lance"
DATASET_SLUG = "openfda_device_pdl_lance"  # passed to lance_commit_lock

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50  # >50 fan-out on either side -> rejected
# Floor: ~46% of the validator-measured natural count (~5,431). A conservative
# catastrophic-collapse gate — fires only on a >50% drop (broken normalizer,
# wrong join key, empty PDL read, wrong dataset URI). HARD FAIL if
# rows_matched < MIN_ROWS_MATCHED.
MIN_ROWS_MATCHED = 2500
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
    """Read openFDA 510k + pma + PDL Lance datasets; dedup openFDA to company grain.

    openFDA is at clearance/approval grain — one company appears across many
    k_number / pma_number rows. Normalize the applicant name in Python (mirrors
    the UCC x PDL precedent, which normalizes Python-side to avoid a DuckDB regex
    spill) and dedup to a distinct (normalized name, 2-letter state) set — the
    company-grain join key.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    from scripts._lib.entity_name_normalize import normalize_entity_name as py_normalize

    # --- openFDA 510(k) clearances --------------------------------------- #
    logger.info("opening openfda/device_510k_lance ...")
    k_ds = lance.dataset(DEVICE_510K_URI, storage_options=storage_options)
    k_raw = k_ds.scanner(
        columns=["applicant", "state"],
        filter=pc.field("applicant").is_valid(),
    ).to_table()
    rows_510k = len(k_raw)
    logger.info("  device_510k_lance (applicant IS NOT NULL): %d rows", rows_510k)

    # --- openFDA PMA approvals ------------------------------------------- #
    logger.info("opening openfda/device_pma_lance ...")
    pma_ds = lance.dataset(DEVICE_PMA_URI, storage_options=storage_options)
    pma_raw = pma_ds.scanner(
        columns=["applicant", "state"],
        filter=pc.field("applicant").is_valid(),
    ).to_table()
    rows_pma = len(pma_raw)
    logger.info("  device_pma_lance (applicant IS NOT NULL): %d rows", rows_pma)

    rows_openfda_raw = rows_510k + rows_pma

    # Normalize the applicant name in Python and dedup to distinct
    # (normalized name, 2-letter state). normalize_entity_name returns None for
    # generic/junk strings (per L33) — the `if norm and st` filter drops those,
    # so there is NO DuckDB Python-UDF in this generator and the DuckDB
    # UDF null-handling trap does not apply.
    logger.info("  normalizing applicant names in Python + dedup to company grain ...")
    branded_set: set = set()
    for tbl in (k_raw, pma_raw):
        applicants = tbl.column("applicant").to_pylist()
        states = tbl.column("state").to_pylist()
        for raw_name, raw_state in zip(applicants, states):
            norm = py_normalize(raw_name) if raw_name else None
            st = (
                raw_state.strip().upper()
                if raw_state and len(raw_state.strip()) == 2
                else None
            )
            if norm and st:
                branded_set.add((norm, st))
    branded_rows = list(branded_set)
    logger.info(
        "  openfda_branded (distinct normalized name + 2-letter state, "
        "deduped from %d raw 510k+pma rows): %d rows",
        rows_openfda_raw, len(branded_rows),
    )
    openfda_branded_arrow = pa.table({
        "applicant_name_normalized": pa.array(
            [r[0] for r in branded_rows], type=pa.string()
        ),
        "applicant_state": pa.array(
            [r[1] for r in branded_rows], type=pa.string()
        ),
    })
    del k_raw, pma_raw, branded_rows, branded_set
    rows_openfda = len(openfda_branded_arrow)

    # --- PDL companies --------------------------------------------------- #
    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=["pdl_id", "legal_name_normalized", "state", "pdl_website"]
    ).to_table()
    rows_pdl = len(pdl_arrow)
    logger.info("  pdl free_companies_lance: %d rows", rows_pdl)

    return openfda_branded_arrow, pdl_arrow, rows_openfda, rows_pdl


def _build_match_table(
    openfda_branded_arrow,
    pdl_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Normalize -> fan-out -> tier; populate TEMP TABLE bridge_match.

    Single composite-key (name, state) INNER JOIN. The openFDA side is already
    Python-normalized + deduped to ~31K distinct pairs (a tiny hash side); the
    8.84M-row PDL side streams. PDL legal_name_normalized is already normalized
    upstream — no re-normalization on either side.
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    # openfda_branded_arrow is already Python-normalized + deduped — register directly.
    con.register("openfda_branded", openfda_branded_arrow)
    con.register("pdl_raw", pdl_arrow)

    rows_openfda = con.execute("SELECT COUNT(*) FROM openfda_branded").fetchone()[0]
    logger.info("  openfda_branded (pre-normalized, from Python): %d", rows_openfda)

    # pdl_branded: PDL legal_name_normalized is normalized upstream; just filter NULLs.
    con.execute(
        """
        CREATE TEMP TABLE pdl_branded AS
        SELECT pdl_id, legal_name_normalized, state, pdl_website
        FROM pdl_raw
        WHERE legal_name_normalized IS NOT NULL AND state IS NOT NULL
        """
    )
    rows_pdl_valid = con.execute("SELECT COUNT(*) FROM pdl_branded").fetchone()[0]
    logger.info("  pdl_branded (legal_name_normalized + state NOT NULL): %d", rows_pdl_valid)

    # Composite-key (name, state) INNER JOIN — selective, streams without spilling.
    logger.info("computing composite-key (name, state) join ...")
    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            o.applicant_name_normalized,
            o.applicant_state,
            p.pdl_id,
            p.pdl_website
        FROM openfda_branded o
        JOIN pdl_branded p
          ON p.legal_name_normalized = o.applicant_name_normalized
         AND p.state                 = o.applicant_state
        """
    )
    rows_joined = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  raw join rows (pre-tier): %d", rows_joined)

    # Fan-out computed on the post-join set so the tier reflects actual match
    # cardinality (mirrors the precedent).
    con.execute(
        """
        CREATE TEMP TABLE openfda_fanout AS
        SELECT applicant_name_normalized, applicant_state,
               COUNT(*) AS openfda_fan_out
        FROM matched
        GROUP BY applicant_name_normalized, applicant_state
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE pdl_fanout AS
        SELECT pdl_id, COUNT(*) AS pdl_fan_out
        FROM matched
        GROUP BY pdl_id
        """
    )

    # bridge_all: per-row provenance + tier CASE. The bridge is single-valued per
    # row (one openFDA applicant <-> one PDL company); no evidence-column
    # aggregation, so no array/list column — emits flat scalar columns only.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.applicant_name_normalized,
            m.applicant_state,
            m.pdl_id,
            m.pdl_website,
            '{METHOD_NAME}'                 AS match_method,
            m.applicant_name_normalized     AS match_value,
            of.openfda_fan_out,
            pf.pdl_fan_out,
            CASE
                WHEN of.openfda_fan_out > {COLLISION_THRESHOLD}
                  OR pf.pdl_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN of.openfda_fan_out = 1 AND pf.pdl_fan_out = 1
                    THEN 'platinum'
                WHEN of.openfda_fan_out = 1 OR pf.pdl_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                             AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'  AS generated_at,
            '{BRIDGE_VERSION}'              AS bridge_version,
            '{bridge_run_id}'               AS bridge_run_id
        FROM matched m
        JOIN openfda_fanout of
          ON of.applicant_name_normalized = m.applicant_name_normalized
         AND of.applicant_state          = m.applicant_state
        JOIN pdl_fanout pf
          ON pf.pdl_id = m.pdl_id
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
            COUNT(*) AS rows_matched,
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
            COUNT(*) FILTER (WHERE confidence_tier = 'gold')     AS rows_tier2,
            COUNT(*) FILTER (WHERE confidence_tier = 'silver')   AS rows_tier3
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
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
    """Lance write inside the commit lock; create BTREE on the join key."""
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

        # BTREE on the join key (analogue of the UCC x PDL precedent's
        # secured_party_name_normalized index).
        try:
            ds.create_scalar_index(
                "applicant_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("  BTREE on applicant_name_normalized created")
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
    """Register ONLY the new bridge-instance row in ops.bridges.

    The company_name_state_exact rule + its 1.0.0 per-version row already exist
    (registered by the UCC x PDL bridge) and are SHARED with the ucc_pdl /
    pdl_sba_borrower / ucc_gleif bridges. The registry helpers do idempotent
    UPSERTs ON CONFLICT DO UPDATE, so writing the shared per-version row with
    openFDA-shape source columns (applicant) would OVERWRITE the config the
    other three bridges depend on and break their provenance trail.

    Precedent: build_bridge_sam_pdl_domain_lance.py _ensure_registry(), which
    imports only the bridge-instance + run helpers for exactly this reason.
    register_bridge is safe because bridge_name 'openfda_device_pdl' is NEW.
    start_bridge_run only reads the shared per-version row (resolves its
    version_id by (method_name, semver)) — it never writes it.
    """
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "openFDA Medical Device applicants (510k + PMA) x PDL companies "
            "via normalized name + 2-letter US state; carries pdl_website per row."
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
    logger.info(
        "inputs: %s + %s + %s (Arrow-bridge)",
        DEVICE_510K_URI, DEVICE_PMA_URI, PDL_LANCE_URI,
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
        openfda_branded_arrow, pdl_arrow, rows_left, rows_right = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            openfda_branded_arrow,
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
            "    silver   (N:M <=%d):    %s",
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
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
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
