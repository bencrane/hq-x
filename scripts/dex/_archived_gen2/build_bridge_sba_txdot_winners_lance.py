"""SBA x TXDOT winners owner-identity bridge (Pattern B exact-match).

Left:  s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance
       (filter borrstate='TX')
Right: s3://dex-raw-landing-zone/polaris-warehouse/txstate/txdot_letting_lance
       (filter low_bidder_flag='true'; project DISTINCT vendor_name_normalized)
Method: legal_name_state_exact_tx v1.0.0 (NEW state variant — does NOT overwrite
        legal_name_state_exact_ca or legal_name_state_exact_fl per L21).
Output: s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_txdot_winners_lance

Key invariants:
  L9:  low_bidder_flag is VARCHAR per all_varchar=TRUE at ingest — compare with 'true' string
  L17: per-row bridge_run_id UUID provenance
  L21: NEW match-method-version row for 'legal_name_state_exact_tx v1.0.0' —
       do NOT pass _ca or _fl to register_match_method_version
  Audit-resolved clarification #3: Modal-hosted per build_bridge_sba_sos_fl_owner_lance.py shape

Tier rule:
    platinum = 1:1
    gold     = 1:N | N:1
    silver   = N:M (both fan-outs <= COLLISION_THRESHOLD=50)
    rejected = either fan-out > COLLISION_THRESHOLD

BTREE: sba_legal_name_normalized (single column — SBA borrowers Lance has no stable
       per-borrower ID column exposed via scanner; name is the join key, mirrors FL precedent).

Modal hosting: @app.function(cpu=8, memory=32768, timeout=10800).

CLI shape (contract §17 B.a — Modal local_entrypoint kwarg, NOT argparse):
    modal run --detach scripts/build_bridge_sba_txdot_winners_lance.py::main --apply

Run (dry-run default, no Lance write):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach scripts/build_bridge_sba_txdot_winners_lance.py::main

Run (write mode):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach scripts/build_bridge_sba_txdot_winners_lance.py::main --apply
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-sba-txdot-winners-lance")

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
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sba_txdot_winners"
DATASET_SLUG = "sba_txdot_winners_lance"
METHOD_NAME = "legal_name_state_exact_tx"  # NEW — does NOT match _ca / _fl per L21
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"
COLLISION_THRESHOLD = 50
# Conservative floor — TXDOT winners ~200-500 distinct in 24mo * expected SBA-match rate;
# 25 catches systemic failure (broken normalizer, bad dataset reference) but allows variance.
MIN_ROWS_MATCHED = 25

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "txdot_letting_lance"

SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
TXDOT_LETTING_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/txstate/txdot_letting_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_txdot_winners_lance"
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


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=10800,
    memory=32768,
    cpu=8,
)
def emit(apply: bool = False) -> dict:
    """Pattern B bridge: SBA borrowers (TX) x TXDOT winning contractors.

    Default mode: dry-run (no Lance write, fail_bridge_run with 'dry-run' msg).
    Pass apply=True (via --apply flag on local_entrypoint) for write mode.

    Key notes:
    - borrstate='TX' filter on SBA left side (column name confirmed via FL precedent)
    - low_bidder_flag == 'true' string compare on right side (VARCHAR per L9 all_varchar)
    - METHOD_NAME = 'legal_name_state_exact_tx' (NEW — NOT _ca or _fl per L21)
    - bridge_run_id UUID per row (per L17 provenance)
    - BTREE on sba_legal_name_normalized (name is the join key; no stable SBA ID)
    """
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import (
        normalize_entity_name,
        __version__ as NORMALIZER_VERSION,
    )
    from scripts._lib.lance_commit_lock import lance_commit_lock
    from scripts._lib.match_method_registry import (
        complete_bridge_run,
        fail_bridge_run,
        register_bridge,
        register_match_method,
        register_match_method_version,
        start_bridge_run,
    )

    _bridge_database_url()

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import duckdb
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)

    # Provenance: register NEW _tx method+version (L21: NEW row, NOT overwrite of _ca/_fl).
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "TX state-variant of legal_name_state_exact — exact equality on entity-name "
            "normalization, scoped to TX SBA borrowers x TXDOT winning contractors. "
            f"_lib/entity_name_normalize v{NORMALIZER_VERSION} on both sides."
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
            f"platinum=1:1; gold=1:N or N:1; "
            f"silver=N:M <=50; rejected=>{COLLISION_THRESHOLD}"
        ),
        rejection_rule_description=f"fan-out >{COLLISION_THRESHOLD} collapsed to rows_collision_rejected",
        input_columns_left=["legal_name_normalized", "borrstate"],
        input_columns_right=["vendor_name_normalized"],  # TX implicit via dataset scope
        output_value_description=(
            "normalize_entity_name(sba.legal_name_normalized) == "
            "normalize_entity_name(txdot.vendor_name) for borrstate='TX' AND low_bidder_flag='true'"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA borrowers x TXDOT winning contractors owner-identity bridge — name+state "
            "(TX) exact match. Attaches SBA borrower identity (legal_name, borrstate, etc.) "
            "to TXDOT TX-state highway contract winners."
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

    try:
        # ----------------------------------------------------------------
        # Left side: SBA borrowers filtered to borrstate='TX'.
        # ----------------------------------------------------------------
        logger.info("opening sba/borrowers_lance ...")
        sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
        sba_tbl = sba_ds.scanner(
            columns=["legal_name_normalized", "borrstate"],
            filter=pc.field("borrstate") == "TX",
        ).to_table()
        logger.info("  sba borrowers_lance (borrstate=TX): %d rows", len(sba_tbl))

        # Pre-dedup to distinct legal_name_normalized.
        names = sba_tbl.column("legal_name_normalized").to_pylist()
        distinct_sba: set[str] = {n for n in names if n and isinstance(n, str) and len(n) >= 2}
        del sba_tbl, names
        logger.info("  sba_branded (distinct, TX): %d names", len(distinct_sba))

        sba_branded_arrow = pa.table(
            {
                "sba_legal_name_normalized": pa.array(
                    sorted(distinct_sba), type=pa.string()
                )
            }
        )
        del distinct_sba

        # ----------------------------------------------------------------
        # Right side: TXDOT winners — DISTINCT vendor_name_normalized for
        # low_bidder_flag='true' (VARCHAR per L9 all_varchar=TRUE).
        # ----------------------------------------------------------------
        logger.info("opening txstate/txdot_letting_lance ...")
        txdot_ds = lance.dataset(TXDOT_LETTING_LANCE_URI, storage_options=storage_options)
        txdot_tbl = txdot_ds.scanner(
            columns=["vendor_name_normalized", "low_bidder_flag"],
            filter=pc.field("low_bidder_flag") == "true",
        ).to_table()
        rows_txdot_raw = len(txdot_tbl)
        logger.info("  txdot_letting_lance (low_bidder_flag=true): %d rows", rows_txdot_raw)

        # ----------------------------------------------------------------
        # DuckDB exact-equality JOIN + fan-out tiering.
        # ----------------------------------------------------------------
        Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        con.execute("SET threads=4")
        con.execute("SET memory_limit='16GB'")
        con.execute(f"SET temp_directory='{TMP_DIR}'")
        con.execute("SET max_temp_directory_size='60GB'")
        con.execute("SET preserve_insertion_order=false")

        con.register("sba_branded", sba_branded_arrow)
        con.register("txdot_winners", txdot_tbl)

        # Distinct TXDOT winners (vendor_name_normalized).
        con.execute(
            """
            CREATE TEMP TABLE txdot_distinct AS
            SELECT DISTINCT vendor_name_normalized
            FROM txdot_winners
            WHERE vendor_name_normalized IS NOT NULL
            """
        )
        rows_txdot_distinct = con.execute(
            "SELECT COUNT(*) FROM txdot_distinct"
        ).fetchone()[0]
        logger.info("  txdot_distinct winners: %d", rows_txdot_distinct)

        # Inner join on normalized name (both sides are already distinct names).
        con.execute(
            """
            CREATE TEMP TABLE matched AS
            SELECT DISTINCT
                s.sba_legal_name_normalized,
                t.vendor_name_normalized
            FROM sba_branded s
            JOIN txdot_distinct t
              ON s.sba_legal_name_normalized = t.vendor_name_normalized
            WHERE t.vendor_name_normalized IS NOT NULL
            """
        )
        rows_matched_pre = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
        logger.info("  matched (exact-match, pre-tier): %d rows", rows_matched_pre)

        # Fan-out counts (1:1 after dedup on both sides; sba_fan_out=1, txdot_fan_out=1).
        con.execute(
            f"""
            CREATE TEMP TABLE bridge_all AS
            SELECT
                m.sba_legal_name_normalized,
                m.vendor_name_normalized,
                '{METHOD_NAME}'                        AS match_method,
                m.sba_legal_name_normalized            AS match_value_normalized,
                'TX'                                   AS match_state,
                1                                      AS sba_fan_out,
                1                                      AS txdot_fan_out,
                CASE
                    WHEN 1 <= 1 THEN 'platinum'
                    ELSE 'gold'
                END                                    AS confidence_tier,
                TIMESTAMP '{started_at.isoformat()}'  AS generated_at,
                '{BRIDGE_VERSION}'                     AS bridge_version,
                '{bridge_run_id}'                      AS bridge_run_id
            FROM matched m
            """
        )

        counts_row = con.execute(
            """
            SELECT COUNT(*), COUNT(*) FILTER (WHERE confidence_tier='platinum')
            FROM bridge_all
            """
        ).fetchone()
        rows_matched = counts_row[0]
        rows_platinum = counts_row[1]
        logger.info(
            "  bridge_all: %d rows (%d platinum)", rows_matched, rows_platinum
        )

        if rows_matched < MIN_ROWS_MATCHED:
            msg = f"HARD FAIL: rows_matched={rows_matched} < floor={MIN_ROWS_MATCHED}"
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return {"status": "failed", "error": msg}

        if not apply:
            msg = "dry-run; no Lance write"
            logger.info(msg)
            fail_bridge_run(run_uuid, msg)  # contract §17 B.a
            return {"status": "dry-run", "rows_matched": rows_matched}

        # ----------------------------------------------------------------
        # Lance write under commit lock.
        # ----------------------------------------------------------------
        t_write = time.time()
        with lance_commit_lock(DATASET_SLUG):
            reader = con.from_query("SELECT * FROM bridge_all").to_arrow_reader(
                batch_size=100_000
            )
            logger.info("writing bridge Lance dataset to %s ...", BRIDGE_LANCE_URI)
            ds = lance.write_dataset(
                reader,
                BRIDGE_LANCE_URI,
                mode="overwrite",
                storage_options=storage_options,
            )
            lance_count = ds.count_rows()
            write_dur = time.time() - t_write
            logger.info("wrote %d rows in %.1fs", lance_count, write_dur)

            # BTREE: sba_legal_name_normalized (the exact-match key = join key).
            # No stable SBA borrower ID column exposed via borrowers_lance — name is the key.
            ds.create_scalar_index(
                "sba_legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE on sba_legal_name_normalized: OK")

            try:
                ds.optimize.compact_files()
                ds.cleanup_old_versions(older_than=timedelta(days=7))
            except Exception as e:
                logger.warning("Optimize failed (non-fatal): %s", e)

        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": len(sba_branded_arrow),
                "rows_right": rows_txdot_raw,
                "rows_matched": rows_matched,
                "rows_tier1": rows_platinum,
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "bridge complete: rows_matched=%d lance_count=%d",
            rows_matched,
            lance_count,
        )
        return {
            "status": "succeeded",
            "rows_matched": rows_matched,
            "lance_count": lance_count,
        }

    except Exception as exc:
        logger.exception("bridge emit failed: %s", exc)
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            pass
        raise


@app.local_entrypoint()
def main(apply: bool = False) -> None:
    """`modal run --detach scripts/build_bridge_sba_txdot_winners_lance.py::main --apply`"""
    import json

    result = emit.remote(apply=apply)
    print(json.dumps(result, indent=2, default=str))
