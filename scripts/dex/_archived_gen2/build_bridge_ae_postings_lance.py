"""s6 - AE-canonical postings Lance emit (Pattern A enriched-cohort, Modal).

Reads jsearch.jobs_lance + bridges.jsearch_pdl_employer_lance via PyLance.
Filter (AE-role detect): LOWER(job_title) LIKE AE variants.
Joins PDL employer via the bridge.
Per-publisher dedup: cluster by (pdl_id, LOWER(TRIM(job_title)),
LOWER(TRIM(job_city)), TRY_CAST(job_posted_at_datetime_utc AS DATE)).
Picks canonical row per cluster (longest job_description, tiebreak by platinum
bridge tier, tiebreak by earliest last_observed_at).

Pattern A enriched-cohort discipline (PR #469/#472):
  - NOT a new cross-source identity bridge.
  - NO register_match_method*, NO register_bridge, NO ops.bridges row.
  - Match logic already settled by s5.
  - Provenance: this emit's bridge_run_id UUID propagated as column.

Output (1 row per canonical posting): job_id_canonical, pdl_id,
employer_canonical_name, employer_canonical_domain, role_canonical
(Account Executive | Enterprise AE | Mid-Market AE | SMB AE | Strategic AE
| Senior AE), seniority_band (Senior | Standard), job_title_raw,
job_description, job_city, job_state, job_country, job_is_remote,
job_min_salary, job_max_salary, job_salary_period, job_apply_link_canonical,
job_publishers_array (DISTINCT publishers in cluster - syndication breadth
signal), cluster_size, posted_date, last_observed_at, is_active
(last_observed_at >= now() - interval 7 days), bridge_run_id, generated_at,
bridge_version.

Floor 232.

Run via (DETACH IS MANDATORY per L47):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/build_bridge_ae_postings_lance.py::run
"""
from __future__ import annotations

import logging, os, sys, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

app = modal.App("data-engine-x-ae-postings-lance")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("duckdb", "psycopg[binary]", "pylance>=0.20", "pyarrow>=16.0")
    .add_local_dir(Path(__file__).resolve().parent, remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

DATASET_SLUG = "ae_postings_lance"
JSEARCH_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/jsearch/jobs_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/jsearch_pdl_employer_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ae_postings_lance"
MIN_ROW_FLOOR = 232
BRIDGE_VERSION = "1.0.0"
BTREE_COLUMNS = ["pdl_id", "role_canonical", "seniority_band", "job_state", "is_active"]

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
    timeout=3600,
    memory=16384,
    cpu=8,
)
def emit() -> dict:
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance
    import duckdb

    storage_options = _storage_options()
    emit_bridge_run_id = str(uuid.uuid4())
    generated_at_iso = datetime.now(tz=timezone.utc).isoformat()

    # Load via PyLance scanners.
    jobs_ds = lance.dataset(JSEARCH_LANCE_URI, storage_options=storage_options)
    jobs_arrow = jobs_ds.scanner(
        columns=[
            "job_id", "job_title", "job_description", "job_city",
            "job_state", "job_country", "job_is_remote",
            "job_min_salary", "job_max_salary", "job_salary_period",
            "job_apply_link", "job_publisher",
            "job_posted_at_datetime_utc", "last_observed_at",
            "employer_name_normalized", "employer_domain_normalized",
        ],
    ).to_table()
    logger.info("jobs_lance: %d rows", jobs_arrow.num_rows)

    bridge_ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=storage_options)
    bridge_arrow = bridge_ds.scanner(
        columns=["job_id", "pdl_id", "match_method", "confidence_tier"],
    ).to_table()
    logger.info("jsearch_pdl_employer_lance: %d rows", bridge_arrow.num_rows)

    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='12GB'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET temp_directory='/tmp/duckdb'")
    Path("/tmp/duckdb").mkdir(parents=True, exist_ok=True)
    con.register("jobs_proj", jobs_arrow)
    con.register("bridge_proj", bridge_arrow)

    # AE-role detect filter + role_canonical mapping + seniority_band.
    con.execute(
        """
        CREATE TEMP TABLE jobs_ae AS
        SELECT *,
          CASE
            WHEN LOWER(job_title) LIKE '%enterprise%'   THEN 'Enterprise AE'
            WHEN LOWER(job_title) LIKE '%mid-market%'
              OR LOWER(job_title) LIKE '%mid market%'   THEN 'Mid-Market AE'
            WHEN LOWER(job_title) LIKE '%smb%'
              OR LOWER(job_title) LIKE '%small business%' THEN 'SMB AE'
            WHEN LOWER(job_title) LIKE '%strategic%'    THEN 'Strategic AE'
            WHEN LOWER(job_title) LIKE '%senior%'
              OR LOWER(job_title) LIKE '%sr.%'
              OR LOWER(job_title) LIKE '%sr %'          THEN 'Senior AE'
            ELSE 'Account Executive'
          END AS role_canonical,
          CASE
            WHEN LOWER(job_title) LIKE '%senior%'
              OR LOWER(job_title) LIKE '%sr.%'
              OR LOWER(job_title) LIKE '%sr %'          THEN 'Senior'
            ELSE 'Standard'
          END AS seniority_band
        FROM jobs_proj
        WHERE
          LOWER(job_title) LIKE '%account executive%'
          OR LOWER(job_title) LIKE '%enterprise ae%'
          OR LOWER(job_title) LIKE '%mid-market ae%'
          OR LOWER(job_title) LIKE '%smb ae%'
          OR LOWER(job_title) LIKE '%strategic ae%'
        """
    )

    # Inner-join PDL employer via s5 bridge.
    con.execute(
        """
        CREATE TEMP TABLE jobs_with_employer AS
        SELECT j.*,
               b.pdl_id,
               b.match_method,
               b.confidence_tier,
               j.employer_name_normalized   AS employer_canonical_name,
               j.employer_domain_normalized AS employer_canonical_domain
        FROM jobs_ae j
        INNER JOIN bridge_proj b ON b.job_id = j.job_id
        """
    )

    # Per-publisher dedup: cluster by (pdl_id, title, city, posted_date).
    con.execute(
        """
        CREATE TEMP TABLE clustered AS
        SELECT *,
          ROW_NUMBER() OVER (
            PARTITION BY pdl_id, LOWER(TRIM(job_title)), LOWER(TRIM(job_city)),
                         TRY_CAST(job_posted_at_datetime_utc AS DATE)
            ORDER BY LENGTH(COALESCE(job_description, '')) DESC NULLS LAST,
                     CASE WHEN confidence_tier = 'platinum' THEN 0 ELSE 1 END,
                     last_observed_at ASC
          ) AS dedup_rn,
          COUNT(*) OVER (
            PARTITION BY pdl_id, LOWER(TRIM(job_title)), LOWER(TRIM(job_city)),
                         TRY_CAST(job_posted_at_datetime_utc AS DATE)
          ) AS cluster_size,
          ARRAY_AGG(DISTINCT job_publisher) OVER (
            PARTITION BY pdl_id, LOWER(TRIM(job_title)), LOWER(TRIM(job_city)),
                         TRY_CAST(job_posted_at_datetime_utc AS DATE)
          ) AS job_publishers_array
        FROM jobs_with_employer
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE ae_postings_out AS
        SELECT
          job_id                                          AS job_id_canonical,
          pdl_id,
          employer_canonical_name,
          employer_canonical_domain,
          role_canonical,
          seniority_band,
          job_title                                        AS job_title_raw,
          job_description,
          job_city, job_state, job_country, job_is_remote,
          job_min_salary, job_max_salary, job_salary_period,
          job_apply_link                                  AS job_apply_link_canonical,
          job_publishers_array,
          cluster_size,
          TRY_CAST(job_posted_at_datetime_utc AS DATE)     AS posted_date,
          last_observed_at,
          (last_observed_at >= (now() - INTERVAL '7 days')) AS is_active,
          '{emit_bridge_run_id}'                            AS bridge_run_id,
          TIMESTAMP '{generated_at_iso}'                    AS generated_at,
          '{BRIDGE_VERSION}'                                AS bridge_version
        FROM clustered
        WHERE dedup_rn = 1
        """
    )

    forensic = con.execute(
        """
        SELECT COUNT(*) AS rows_out,
               COUNT(*) FILTER (WHERE cluster_size > 1) AS rows_dedup_collapsed,
               COUNT(*) FILTER (WHERE is_active)        AS rows_active
        FROM ae_postings_out
        """
    ).fetchone()
    logger.info(
        "ae_postings forensic: out=%d dedup_collapsed=%d active=%d",
        forensic[0], forensic[1], forensic[2],
    )

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(
            "SELECT * FROM ae_postings_out"
        ).to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader, LANCE_URI, mode="overwrite",
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

    return {
        "status": "succeeded", "rows_lance": rows, "lance_uri": LANCE_URI,
        "bridge_run_id": emit_bridge_run_id,
        "rows_active": forensic[2], "rows_dedup_collapsed": forensic[1],
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/build_bridge_ae_postings_lance.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
