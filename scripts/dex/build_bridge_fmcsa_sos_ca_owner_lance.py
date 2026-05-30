"""s1 - FMCSA CA carrier × CA SoS entities owner-identity bridge (Pattern B).

Pattern B exact-match bridge: FMCSA motor carriers with ``phy_state='CA'``
(legal name from carrier_essentials_lance) × CA SoS business entities
(``entity_name_normalized`` produced by run_ca_sos_master_unload_to_r2.py).
Normalizer is canonical ``scripts._lib.entity_name_normalize`` on BOTH sides
(asymmetric: SoS column is pre-produced by it, joined directly; FMCSA raw
``legal_name`` is re-normalized fresh — see C3 below).

Method: legal_name_state_exact_ca v1.0.0 (REUSED — the method + version rows
were registered by PR #464's ``build_bridge_sba_sos_ca_owner_lance.py``;
this script ONLY calls ``register_bridge`` + ``start_bridge_run`` +
``complete_bridge_run`` + ``fail_bridge_run`` — the idempotent UPSERT in
``_lib/match_method_registry`` would otherwise overwrite the SBA-shape
``input_columns_left=['legal_name_normalized', 'borrstate']`` config and
corrupt the SBA × SoS bridge's provenance trail). This is a REUSER of the
method — see build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py for the first
REUSER precedent.

C3 — normalizer determination (validator-resolved 2026-05-21, load-bearing):
  * CA SoS ca_entities_lance.entity_name_normalized — 100.0000% parity with
    canonical normalize_entity_name v1.0.0 (produced BY it in
    run_ca_sos_master_unload_to_r2.py::_project_entities). Joined DIRECTLY;
    no re-normalization.
  * FMCSA carrier_essentials_lance.legal_name_normalized — only 99.9786%
    parity (106/495,488 CA-row mismatches: single-char strings + generic
    blacklist words like "none", "na", "self" that the canonical v1.0.0
    normalizer correctly rejects). FMCSA column built by an older/blacklist-
    less normalizer revision. MUST re-normalize raw FMCSA ``legal_name`` fresh
    via normalize_entity_name — NEVER use the pre-materialized column.
    This is the identical discipline of build_bridge_sba_sos_ca_owner_lance.py
    and benchmarks/fmcsa-ucc-ca-debtor-bridge.sh (the PR #459/#460 revert
    class).

C8 — entity-status filter (validator-resolved 2026-05-21): NO filter on
  entity_status or standing_sos. All five CA-SoS-entity bridge precedents
  apply only entity_name_normalized IS NOT NULL (is_valid() scanner filter).
  entity_status + standing_sos carried as downstream-filterable evidence
  columns, never as exclusions.

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected — N:M up to 50×50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 200_000 (validator-calibrated 2026-05-21 from measured
input-side estimate of 369,010 non-rejected rows; 54.2% of estimate —
conservative end of the 55-60% calibration band; platinum=181,961,
gold=104,303, silver=82,746, rejected=195).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance
        (filter pc.field('phy_state') == 'CA'; re-normalize raw legal_name
        via _lib/entity_name_normalize.normalize_entity_name — do NOT use
        the pre-materialized legal_name_normalized column)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance
        (entity_name_normalized is_valid; 100% canonical — join directly)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sos_ca_owner_lance
    (dual BTREE on dot_number AND entity_num — C7)

Match method REUSE (C2 / CRITICAL p4):
    register_bridge                          → ops.bridges                (idempotent)
    start_bridge_run                         → ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      → status=completed + metrics
    fail_bridge_run (on shortfall or dry-run) → status=failed + error
    (Method-definition and method-version-definition helpers are NOT called
    — those rows are SHARED with sba_sos_ca_owner from PR #464; an UPSERT
    here would corrupt the shared method-version config for all consumers.)

Tier rule:
    platinum = 1:1
    gold     = 1:N | N:1
    silver   = N:M (both ≤ 50)
    rejected = >50 on either side

Modal hosting: @app.function(cpu=8, memory=32768, timeout=10800)

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/build_bridge_fmcsa_sos_ca_owner_lance.py::main --apply
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

# Marker — the actual imports live inside emit() scoped to Modal:
#   from scripts._lib.entity_name_normalize import (
#       __version__ as NORMALIZER_VERSION, normalize_entity_name,
#   )
#   from scripts._lib.lance_commit_lock import lance_commit_lock
#   from scripts._lib.match_method_registry import (
#       register_bridge, start_bridge_run, complete_bridge_run, fail_bridge_run,
#   )
#   NOTE: method-definition + method-version-definition helpers are
#         INTENTIONALLY NOT IMPORTED (C2 — REUSE not redefine).
#         Only bridge-level helpers: register_bridge, start/complete/fail_bridge_run.

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-fmcsa-sos-ca-owner-lance")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
    )
    .add_local_dir(
        Path(__file__).resolve().parent,
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps; C9 floor)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "fmcsa_sos_ca_owner"
METHOD_NAME = "legal_name_state_exact_ca"  # REUSED — registered by PR #464
METHOD_SEMVER = "1.0.0"                    # REUSED — version row from PR #464
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-21 from measured input-side estimate of
# 369,010 non-rejected rows. 200,000 = 54.2% of estimate (conservative end
# of 55-60% band, matching CO UCC 73,000/134,196=54.4% + CA UCC
# 300,000/545,143=55.0% precedents). Hard-fail below this floor (C9).
MIN_ROWS_MATCHED = 200_000

DATASET_SLUG = "fmcsa_sos_ca_owner_lance"
SOURCE_LEFT = "fmcsa_carrier_essentials_lance"
SOURCE_RIGHT = "sos_ca_entities_lance"

# v1 scope (C8): FMCSA carriers pinned to phy_state='CA'.
FMCSA_STATE = "CA"

FMCSA_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
SOS_ENTITIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sos_ca_owner_lance"
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


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict, normalize_entity_name) -> tuple:
    """Load FMCSA CA carriers + CA SoS entities into Arrow.

    FMCSA side (C3 — divergent, re-normalize):
      - Scan raw dot_number + legal_name + phy_state from carrier_essentials_lance,
        filter to phy_state='CA' at the Lance scanner.
      - Re-normalize raw legal_name in Python via _lib/entity_name_normalize
        (canonical normalizer). Do NOT use the pre-materialized legal_name_normalized
        column — it is 99.9786% parity only (106/495,488 mismatches on stale
        normalizer revision that lacks the GENERIC_NON_ENTITY_STRINGS blacklist).
      - Drop rows where the re-normalized name is None or empty.

    SoS side (C3 — 100% canonical, join directly; C8 — no status filter):
      - Scan entity_name_normalized + entity metadata columns from ca_entities_lance
        with is_valid() filter at the scanner.
      - entity_status + standing_sos carried as evidence columns.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening fmcsa/carrier_essentials_lance ...")
    fmcsa_ds = lance.dataset(FMCSA_LANCE_URI, storage_options=storage_options)
    fmcsa_tbl = fmcsa_ds.scanner(
        columns=[
            "dot_number",
            "legal_name",
            "phy_state",
        ],
        filter=pc.field("phy_state") == FMCSA_STATE,
    ).to_table()
    rows_fmcsa_raw = len(fmcsa_tbl)
    logger.info(
        "  fmcsa carrier_essentials_lance (phy_state='CA'): %d rows",
        rows_fmcsa_raw,
    )

    # Re-normalize raw legal_name in Python (canonical normalizer ONLY — C3).
    # Do NOT use the pre-materialized legal_name_normalized column — divergent.
    legal_names = fmcsa_tbl.column("legal_name").to_pylist()
    normalized = [normalize_entity_name(n) for n in legal_names]
    fmcsa_tbl = fmcsa_tbl.append_column(
        "legal_name_normalized_canon",
        pa.array(normalized, type=pa.string()),
    )
    # Filter out None/empty normalizations (generic strings / too-short names).
    mask = pc.is_valid(fmcsa_tbl.column("legal_name_normalized_canon"))
    fmcsa_tbl = fmcsa_tbl.filter(mask)
    rows_fmcsa_post_norm = len(fmcsa_tbl)
    logger.info(
        "  fmcsa after re-normalization (legal_name_normalized_canon is_valid): %d rows",
        rows_fmcsa_post_norm,
    )

    logger.info("opening sos/ca_entities_lance ...")
    sos_ds = lance.dataset(SOS_ENTITIES_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    sos_tbl = sos_ds.scanner(
        columns=[
            "entity_num",
            "entity_name",
            "entity_name_normalized",
            "entity_status",
            "standing_sos",
            "entity_type",
            "llc_management_structure",
            "initial_filing_date",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info("  sos ca_entities_lance (post-filter): %d rows", rows_sos)

    return fmcsa_tbl, sos_tbl, rows_fmcsa_raw, rows_sos


def _build_match_table(
    fmcsa_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    DuckDB tuning for Modal scale (cpu=8 memory=32768):
      SET threads=8
      SET memory_limit='24GB'
      SET temp_directory='/tmp/lance'
      SET max_temp_directory_size='200GB'
      SET preserve_insertion_order=false

    Fan-out tiering (C5 — symmetric two-sided):
      fmcsa_fan_out: distinct dot_number values per normalized name.
      sos_fan_out:   distinct entity_num values per normalized name.
      Both computed via COUNT(DISTINCT identity_key) over the join product —
      NOT via a pre-dedup of either side (which would collapse the gold tier).
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='24GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='200GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("fmcsa", fmcsa_tbl)
    con.register("sos", sos_tbl)

    rows_fmcsa_reg = con.execute("SELECT COUNT(*) FROM fmcsa").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos").fetchone()[0]
    logger.info(
        "  registered: fmcsa=%d  sos=%d",
        rows_fmcsa_reg, rows_sos_reg,
    )

    # 1. Inner JOIN on normalized name (both sides CA-pinned — C8).
    #    FMCSA join key: legal_name_normalized_canon (re-derived from raw legal_name).
    #    SoS join key:   entity_name_normalized (pre-produced by canonical normalizer).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            f.dot_number,
            f.legal_name                         AS fmcsa_legal_name_raw,
            f.legal_name_normalized_canon        AS legal_name_normalized,
            s.entity_num,
            s.entity_name,
            s.entity_name_normalized,
            s.entity_status,
            s.standing_sos,
            s.entity_type,
            s.llc_management_structure,
            s.initial_filing_date,
            '{METHOD_NAME}'                      AS match_method,
            f.legal_name_normalized_canon        AS match_value,
            '{BRIDGE_VERSION}'                   AS bridge_version,
            '{bridge_run_id}'                    AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'       AS generated_at
        FROM fmcsa f
        JOIN sos s
          ON f.legal_name_normalized_canon = s.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (C5 — symmetric two-sided).
    #    fmcsa_fan_out: # of distinct FMCSA carriers sharing this normalized name.
    #    sos_fan_out:   # of distinct SoS entity_num values for this name.
    #    Both use COUNT(DISTINCT identity_key) over the join product — not
    #    COUNT(*), which would make both sides identical and collapse gold tier.
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_fanout AS
        SELECT legal_name_normalized,
               COUNT(DISTINCT dot_number) AS fmcsa_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT legal_name_normalized,
               COUNT(DISTINCT entity_num) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule + materialize bridge_all then bridge_match (C5).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            ff.fmcsa_fan_out,
            sf.sos_fan_out,
            CASE
                WHEN ff.fmcsa_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.fmcsa_fan_out = 1 AND sf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN ff.fmcsa_fan_out = 1 OR  sf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN fmcsa_fanout ff USING (legal_name_normalized)
        JOIN sos_fanout sf USING (legal_name_normalized)
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


def _write_bridge_lance(con, storage_options: dict, lance_commit_lock) -> int:
    """Write bridge_match to Lance via Arrow-bridge pattern + dual BTREE (C7)."""
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
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        # Dual BTREE — C7: BOTH indexes must succeed or run fails.
        # dot_number: primary join key (FMCSA side lookup).
        # entity_num: downstream hop key (entity_num → ca_principals_lance).
        try:
            ds.create_scalar_index("dot_number", index_type="BTREE", replace=True)
            logger.info("BTREE on dot_number: OK")
        except Exception as e:
            logger.error("BTREE on dot_number FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on entity_num: OK")
        except Exception as e:
            logger.error("BTREE on entity_num FAILED: %s", e)
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


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=10800,
    memory=32768,
    cpu=8,
)
def emit(apply: bool = False) -> dict:
    """Build the FMCSA CA carrier × CA SoS owner-identity bridge.

    apply=True  — full pipeline: Lance write + BTREE + complete_bridge_run.
    apply=False — dry-run: register + start + fail_bridge_run (no Lance write).
    """
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import (
        __version__ as NORMALIZER_VERSION,
        normalize_entity_name,
    )
    from scripts._lib.lance_commit_lock import lance_commit_lock
    # CRITICAL C2: only bridge-level helpers are imported.
    # The method-definition and method-version-definition helpers are
    # intentionally omitted — the row in ops.match_methods + ops.match_
    # method_versions for legal_name_state_exact_ca v1.0.0 is SHARED with
    # sba_sos_ca_owner (PR #464). Re-registering would UPSERT over the SBA-
    # shape config and corrupt the shared method-version provenance for all
    # consumers. Precedent: build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py.
    from scripts._lib.match_method_registry import (
        complete_bridge_run,
        fail_bridge_run,
        register_bridge,
        start_bridge_run,
    )

    _bridge_database_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #464)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, apply,
    )
    logger.info("inputs: %s + %s", FMCSA_LANCE_URI, SOS_ENTITIES_LANCE_URI)
    logger.info("output: %s", BRIDGE_LANCE_URI)

    # Provenance: register_bridge ONLY. Method + method_version rows
    # are REUSED from PR #464 (C2 — REUSE not redefine).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "FMCSA CA motor carrier x CA SoS entities — "
            "legal-name exact match. Reuses legal_name_state_exact_ca "
            "v1.0.0 method registered by PR #464. "
            "FMCSA side: raw legal_name re-normalized fresh via canonical "
            "normalize_entity_name (pre-materialized legal_name_normalized "
            "column is divergent — 99.9786% parity only). "
            "SoS side: entity_name_normalized joined directly (100% canonical). "
            "No entity-status filter (all five CA-SoS-entity bridge precedents "
            "apply only is_valid() filter; entity_status + standing_sos are "
            "evidence columns). "
            "Joins FMCSA carriers to CA-registered entities; downstream: "
            "entity_num → sos.ca_principals_lance gives owner identity."
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

    # Dry-run gate — gate BEFORE any Lance write (C10).
    if not apply:
        logger.info("DRY-RUN: no Lance write (pass --apply to execute)")
        fail_bridge_run(run_uuid, "dry-run; no Lance write (pass --apply to execute)")
        logger.info("bridge_run marked failed-dry-run (run_id=%s)", bridge_run_id)
        return {
            "status": "dry-run",
            "bridge_run_id": bridge_run_id,
            "message": "dry-run; no Lance write (pass --apply to execute)",
        }

    try:
        fmcsa_tbl, sos_tbl, rows_left, rows_right = _materialize_inputs(
            storage_options, normalize_entity_name,
        )
        con, counts = _build_match_table(
            fmcsa_tbl, sos_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M ≤%d):     %d",
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
            return {"status": "failed", "error": msg, "counts": counts}

        lance_count = _write_bridge_lance(con, storage_options, lance_commit_lock)
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
            "OK - run_id=%s  duration=%.1fs",
            bridge_run_id, time.time() - t0,
        )
        logger.info(
            "OK: bridges.fmcsa_sos_ca_owner_lance written (%d rows; "
            "platinum=%d gold=%d silver=%d rejected=%d)",
            lance_count,
            counts["rows_tier1"],
            counts["rows_tier2"],
            counts["rows_tier3"],
            counts["rows_collision_rejected"],
        )
        return {
            "status": "succeeded",
            "bridge_run_id": bridge_run_id,
            "rows_left": rows_left,
            "rows_right": rows_right,
            "rows_matched": counts["rows_matched"],
            "rows_tier1": counts["rows_tier1"],
            "rows_tier2": counts["rows_tier2"],
            "rows_tier3": counts["rows_tier3"],
            "rows_collision_rejected": counts["rows_collision_rejected"],
            "lance_count": lance_count,
            "duration_s": round(time.time() - t0, 1),
        }
    except Exception as exc:
        logger.exception("bridge generation failed")
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            logger.exception("also failed to mark run as failed")
        raise


@app.local_entrypoint()
def main(apply: bool = False) -> None:
    """`modal run scripts/build_bridge_fmcsa_sos_ca_owner_lance.py::main --apply`

    Without --apply: dry-run (register + start + fail_bridge_run; no Lance write).
    With --apply:    full emit (JOIN + tier + write Lance + BTREE + complete_bridge_run).
    """
    import json
    out = emit.remote(apply=apply)
    print(json.dumps(out, indent=2, default=str))
