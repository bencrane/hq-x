"""s6 - Glassdoor /company-salaries-v2 Lance emit (Pattern A, Modal-hosted).

Reads r2://dex-raw-landing-zone/glassdoor/company_salaries_v2/
snapshot=YYYY-MM-DD/data.parquet via DuckDB-on-R2. Derives
job_title_normalized (inline _normalize_entity_sql; lowercase+trim+suffix-strip)
from job_title. Writes Lance to
s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_v2_lance.

3 BTREE: glassdoor_company_id, job_title_normalized, job_title_id.

Per-(company, job_title_id, page) rows preserved.

Run via (DETACH IS MANDATORY per L47):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/run_glassdoor_company_salaries_v2_lance_emit.py::run
"""
from __future__ import annotations

import logging, os, sys, time
from datetime import date, timedelta
from pathlib import Path

import modal

app = modal.App("data-engine-x-glassdoor-company-salaries-v2-lance-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("duckdb", "psycopg[binary]", "pylance>=0.20", "pyarrow>=16.0")
    .add_local_dir(Path(__file__).resolve().parent, remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

DATASET_SLUG = "glassdoor_company_salaries_v2_lance"
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/company_salaries_v2_lance"
)
MIN_ROW_FLOOR = 72  # ceil(0.9 × 80 validator-stamped Postgres floor)
BTREE_COLUMNS = [
    "glassdoor_company_id", "job_title_normalized", "job_title_id",
]

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


def _connect_duckdb():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


# Retained for s6 parity (unused inline — v2 has no website/domain column).
def _normalize_domain_sql(raw_expr: str) -> str:
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


# Inline entity-name normalization — IDENTICAL to run_jsearch_jobs_lance_emit.py.
def _normalize_entity_sql(raw_expr: str) -> str:
    suffixes = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          ELSE NULLIF(
            trim(
              regexp_replace(
                regexp_replace(
                  regexp_replace(
                    lower(trim({raw_expr})),
                    '\\b({suffixes})\\b\\.?',
                    ' ',
                    'g'
                  ),
                  '[^\\w\\s]+',
                  ' ',
                  'g'
                ),
                '\\s+',
                ' ',
                'g'
              )
            ),
            ''
          )
        END
    """.strip()


def _existing_btree_columns(ds) -> set:
    cols = set()
    for idx in ds.list_indices():
        fields = idx.get("fields") if isinstance(idx, dict) else []
        itype = idx.get("type") if isinstance(idx, dict) else ""
        if "BTREE" in str(itype).upper() or "BTREE" in str(idx).upper():
            for f in (fields or []):
                cols.add(str(f))
    return cols


def _build_select_sql(parquet_uri: str) -> str:
    title_norm_expr = _normalize_entity_sql("job_title")
    return f"""
        SELECT
            *,
            {title_norm_expr} AS job_title_normalized
        FROM read_parquet('{parquet_uri}')
    """


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=3600,
    memory=8192,
    cpu=4,
)
def emit() -> dict:
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance

    storage_options = _storage_options()

    snapshot_date = date.today().isoformat()
    src_parquet_uri = (
        f"r2://dex-raw-landing-zone/glassdoor/company_salaries_v2/snapshot={snapshot_date}/data.parquet"
    )
    logger.info("source: %s", src_parquet_uri)

    con = _connect_duckdb()
    src_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{src_parquet_uri}')"
    ).fetchone()[0]
    logger.info("source parquet rows: %d", src_rows)

    select_sql = _build_select_sql(src_parquet_uri)

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(select_sql).to_arrow_reader(batch_size=100_000)
        logger.info("writing Lance dataset to %s ...", LANCE_URI)
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

    t_btree = time.time()
    existing_btree = _existing_btree_columns(ds)
    logger.info("existing BTREE columns: %s", sorted(existing_btree))
    for col in BTREE_COLUMNS:
        if col in existing_btree:
            logger.info("BTREE on %s already present - skipping", col)
            continue
        try:
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK", col)
        except Exception as e:
            logger.error("BTREE on %s FAILED: %s", col, e)
            raise

    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    btree_dur = time.time() - t_btree
    final_rows = ds.count_rows()
    return {
        "status": "succeeded", "rows_lance": final_rows, "lance_uri": LANCE_URI,
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/run_glassdoor_company_salaries_v2_lance_emit.py::run`

    DETACH IS MANDATORY (L47 - Modal CLI disconnect kills attached jobs).
    """
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
