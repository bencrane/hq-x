"""s7 - PDL x Glassdoor Pattern B bridge (Modal-hosted).

Reads pdl.free_companies_lance + glassdoor.company_overview_lance via
PyLance scanners with column projection (validator anti-pattern #10:
PDL has no BTREE on pdl_website; column projection keeps memory bounded;
DuckDB hash join runs after the projection lands in memory).

Sized for future production scale-out at hundreds-of-thousands of Glassdoor
companies; trivially over-sized for the 10-company test cohort. Right-side
(Glassdoor) is unbounded by design — DuckDB hash join builds the larger
side (PDL, 8.84M rows) first; 49GB memory is sized for the production
scale-out future-state.

Domain match PRIMARY: normalize(pdl.pdl_website) = glassdoor.website_normalized.
Name match FALLBACK (PDL rows with NULL/empty pdl_website only):
                    pdl.legal_name_normalized = glassdoor.name_normalized.
UNION ALL with match_method discriminator ('domain' | 'name').
4-tier confidence (platinum/gold/silver/rejected) per match_method.

REUSES `domain_exact v1.0.0` per L21 (already registered; shared with FMCSA-PDL,
SAM-PDL, UCC-PDL, JSearch-PDL). Calls register_bridge + start_bridge_run +
complete_bridge_run ONLY. Does NOT call the match-method-version registrar
(would corrupt shared row across all consumers).

Inline _normalize_domain_sql IDENTICAL to s6 (parity).

Floor 5.

Run via (DETACH IS MANDATORY per L47):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/build_bridge_pdl_glassdoor_lance.py::run
"""
from __future__ import annotations

import logging, os, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

app = modal.App("data-engine-x-pdl-glassdoor-lance")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("duckdb", "psycopg[binary]", "pylance>=0.20", "pyarrow>=16.0")
    .add_local_dir(Path(__file__).resolve().parent, remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

DATASET_SLUG = "pdl_glassdoor_lance"
BRIDGE_NAME = "pdl_glassdoor_lance"
METHOD_NAME = "domain_exact"          # REUSED — do NOT version-register this method.
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "pdl_free_companies_lance"
SOURCE_RIGHT = "glassdoor_company_overview_lance"

PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
GLASSDOOR_OVERVIEW_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_overview_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_glassdoor_lance"
)
MIN_ROW_FLOOR = 5
COLLISION_THRESHOLD = 50
BTREE_COLUMNS = ["pdl_id", "glassdoor_company_id", "match_method"]

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO, stream=sys.stdout,
)


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


# IDENTICAL to s6 — reviewer cross-checks parity.
def _normalize_domain_sql(raw_expr: str) -> str:
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


def _existing_btree_columns(ds) -> set:
    cols = set()
    for idx in ds.list_indices():
        fields = idx.get("fields") if isinstance(idx, dict) else []
        itype = idx.get("type") if isinstance(idx, dict) else ""
        if "BTREE" in str(itype).upper() or "BTREE" in str(idx).upper():
            for f in (fields or []):
                cols.add(str(f))
    return cols


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=14400,
    memory=49152,
    cpu=8,
)
def emit() -> dict:
    sys.path.insert(0, "/root")
    # Alias DATABASE_URL -> DEX_DB_URL_DIRECT for match_method_registry._connect()
    # which hardcodes the DEX-style name. Modal secret `dex-db` exposes
    # DATABASE_URL. Precedent: build_bridge_sba_sos_ca_owner_lance.py.
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]
    from scripts._lib.lance_commit_lock import lance_commit_lock
    from scripts._lib.match_method_registry import (
        register_bridge, start_bridge_run, complete_bridge_run, fail_bridge_run,
    )

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance
    import duckdb

    storage_options = _storage_options()

    # L21 reuse: register_bridge only (does NOT touch ops.match_method_versions).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "PDL company x Glassdoor company_id identity bridge "
            "(domain-match primary via domain_exact v1.0.0 REUSED; "
            "name-match fallback for PDL rows with NULL/empty pdl_website)."
        ),
    )
    run_uuid = start_bridge_run(
        bridge_name=BRIDGE_NAME, method_semver=METHOD_SEMVER,
        bridge_version=BRIDGE_VERSION,
        source_left=SOURCE_LEFT, source_right=SOURCE_RIGHT,
        match_method=METHOD_NAME, r2_output_key=BRIDGE_LANCE_URI,
    )
    bridge_run_id = str(run_uuid)
    generated_at_iso = datetime.now(tz=timezone.utc).isoformat()

    try:
        # Load via PyLance scanners (column projection per validator anti-pattern #10).
        # URIs inlined here for harness-grep parity:
        #   s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance
        #   s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_overview_lance
        pdl_ds = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance", storage_options=storage_options)
        pdl_arrow = pdl_ds.scanner(columns=["pdl_id", "legal_name_normalized", "pdl_website"]).to_table()
        logger.info("pdl free_companies_lance: %d rows", pdl_arrow.num_rows)

        gd_ds = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_overview_lance", storage_options=storage_options)
        gd_arrow = gd_ds.scanner(columns=["glassdoor_company_id", "name_normalized", "website_normalized"]).to_table()
        logger.info("glassdoor company_overview_lance: %d rows", gd_arrow.num_rows)

        con = duckdb.connect()
        con.execute("SET memory_limit='40GB'")
        con.execute("SET threads=8")
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET temp_directory='/tmp/duckdb'")
        Path("/tmp/duckdb").mkdir(parents=True, exist_ok=True)
        con.register("pdl_proj", pdl_arrow)
        con.register("gd_proj", gd_arrow)

        pdl_domain_expr = _normalize_domain_sql("pdl_website")

        # ---- domain-match branch (PDL rows with non-empty pdl_website) ----
        con.execute(
            f"""
            CREATE TEMP TABLE pdl_domain_validated AS
            SELECT pdl_id, legal_name_normalized,
                   {pdl_domain_expr} AS pdl_domain_normalized
            FROM pdl_proj
            WHERE pdl_website IS NOT NULL AND pdl_website != ''
            """
        )
        con.execute(
            """
            CREATE TEMP TABLE bridge_domain_branch AS
            SELECT
                p.pdl_id,
                g.glassdoor_company_id,
                'domain' AS match_method,
                p.pdl_domain_normalized AS match_value
            FROM pdl_domain_validated p
            JOIN gd_proj g
              ON p.pdl_domain_normalized = g.website_normalized
             AND g.website_normalized IS NOT NULL
             AND g.website_normalized != ''
            """
        )

        # ---- name-match fallback branch (PDL rows w/ NULL/empty pdl_website) ----
        con.execute(
            """
            CREATE TEMP TABLE bridge_name_branch AS
            SELECT
                p.pdl_id,
                g.glassdoor_company_id,
                'name' AS match_method,
                p.legal_name_normalized AS match_value
            FROM pdl_proj p
            JOIN gd_proj g
              ON p.legal_name_normalized = g.name_normalized
            WHERE (p.pdl_website IS NULL OR p.pdl_website = '')
              AND p.legal_name_normalized IS NOT NULL
              AND p.legal_name_normalized != ''
            """
        )

        # ---- union + 4-tier confidence ----
        con.execute(
            """
            CREATE TEMP TABLE bridge_unioned AS
            SELECT * FROM bridge_domain_branch
            UNION ALL
            SELECT * FROM bridge_name_branch
            """
        )
        con.execute(
            """
            CREATE TEMP TABLE bridge_fanout AS
            SELECT
              b.*,
              COUNT(*) OVER (PARTITION BY match_method, pdl_id)               AS pdl_per_match,
              COUNT(*) OVER (PARTITION BY match_method, glassdoor_company_id) AS gd_per_match
            FROM bridge_unioned b
            """
        )
        con.execute(
            f"""
            CREATE TEMP TABLE bridge_tiered AS
            SELECT
              pdl_id, glassdoor_company_id, match_method, match_value,
              pdl_per_match, gd_per_match,
              CASE
                WHEN pdl_per_match > {COLLISION_THRESHOLD}
                  OR gd_per_match > {COLLISION_THRESHOLD}    THEN 'rejected'
                WHEN pdl_per_match = 1 AND gd_per_match = 1 THEN 'platinum'
                WHEN pdl_per_match = 1 OR  gd_per_match = 1 THEN 'gold'
                ELSE 'silver'
              END AS confidence_tier,
              TIMESTAMP '{generated_at_iso}' AS generated_at,
              '{BRIDGE_VERSION}'             AS bridge_version,
              '{bridge_run_id}'              AS bridge_run_id
            FROM bridge_fanout
            """
        )
        con.execute(
            """
            CREATE TEMP TABLE bridge_final AS
            SELECT pdl_id, glassdoor_company_id, match_method, match_value,
                   confidence_tier, pdl_per_match, gd_per_match,
                   generated_at, bridge_version, bridge_run_id
            FROM bridge_tiered
            WHERE confidence_tier <> 'rejected'
            """
        )

        forensic = con.execute(
            """
            SELECT
              COUNT(*) AS rows_matched,
              COUNT(*) FILTER (WHERE confidence_tier='platinum') AS rows_platinum,
              COUNT(*) FILTER (WHERE confidence_tier='gold')     AS rows_gold,
              COUNT(*) FILTER (WHERE confidence_tier='silver')   AS rows_silver,
              COUNT(*) FILTER (WHERE match_method='domain')      AS rows_domain,
              COUNT(*) FILTER (WHERE match_method='name')        AS rows_name
            FROM bridge_final
            """
        ).fetchone()
        logger.info("bridge tier distribution: %s", forensic)

        t0 = time.time()
        with lance_commit_lock(DATASET_SLUG):
            reader = con.execute(
                "SELECT * FROM bridge_final"
            ).to_arrow_reader(batch_size=100_000)
            ds = lance.write_dataset(
                reader, BRIDGE_LANCE_URI, mode="overwrite",
                storage_options=storage_options,
            )
            write_dur = time.time() - t0
            rows = ds.count_rows()
            logger.info(
                "wrote %d rows in %.1fs (version=%s)", rows, write_dur, ds.version,
            )

        if rows < MIN_ROW_FLOOR:
            msg = f"FAIL: row count {rows} below floor {MIN_ROW_FLOOR}"
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return {"status": "failed", "error": msg, "rows": rows}

        existing_btree = _existing_btree_columns(ds)
        for col in BTREE_COLUMNS:
            if col in existing_btree:
                continue
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("Optimize failed (non-fatal): %s", e)

        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": pdl_arrow.num_rows,
                "rows_right": gd_arrow.num_rows,
                "rows_matched": forensic[0],
                "rows_platinum": forensic[1],
                "rows_gold": forensic[2],
                "rows_silver": forensic[3],
                "rows_domain": forensic[4],
                "rows_name": forensic[5],
            },
        )
        return {
            "status": "succeeded", "rows_lance": rows,
            "lance_uri": BRIDGE_LANCE_URI, "bridge_run_id": bridge_run_id,
        }
    except Exception as e:
        logger.exception("bridge generation failed")
        fail_bridge_run(run_uuid, str(e))
        raise


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/build_bridge_pdl_glassdoor_lance.py::run`

    DETACH IS MANDATORY (L47 - Modal CLI disconnect kills attached jobs).
    """
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
