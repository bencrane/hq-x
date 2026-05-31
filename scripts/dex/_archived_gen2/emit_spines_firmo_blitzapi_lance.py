"""Spines — Blitz-API Firmographics Lance emit (domain-grain).

Promotes the blitz-api firmographic enrichment payloads that currently live
ONLY in Postgres (`ops.task_runs` in the hq-x DB) into an R2 Lance spine,
cataloged in Polaris. This is the "push out" step: the operational ledger is
a cache, not the system of record — the analytical spine is Lance.

Grain: **one row per normalized domain**. A firmographic fact is a property of
the company (its website), NOT of the SAM UEI that happened to be the input
binding. Many UEIs fan out to one domain (wm.com → 356 UEIs in the ledger);
keying on UEI would smear the parent's firmo across every child. So the spine
is domain-keyed, and the claimed SAM UEI(s) ride as **lineage only**
(`claimed_ueis`, pipe-delimited) — to be re-associated downstream by joining
this spine's `domain_norm` against `sam_gov.entities_lance.entity_url`
(measured 2026-05-29: 89.7% of blitz domains rejoin; 100% round-trip on the
`binding='entity_url'` subset). UEI is NOT trusted as a join key here.

Source rows: `ops.task_runs` WHERE
    task_type IN ('blitz_firmo_direct', 'modal_hydrate_firmo_cascade')
    AND status = 'completed'
    AND <company object present>
    AND domain IS NOT NULL
Two payload shapes are unified: direct sweep nests the company under
`result_payload.blitz_payload.company`; the Modal cascade nests it under
`result_payload.blitz_data.company`. The company object is identical in both
(name, domain, website, linkedin_url, linkedin_id, industry, size,
employees_on_linkedin, followers, founded_year, type, about, specialties,
hq{city,state,country_code,country_name,region,continent}).

Faithful passthrough — NO normalization of firmo attributes. `size` stays a
string bucket ("11-50"), `hq_state` stays a full name ("Georgia"),
`employees_on_linkedin` stays as-stored. Normalization belongs downstream at
union-with-SAM time, not in the source spine. The ONE exception is the join
KEY: `domain_norm` is canonicalized (lowercase, strip scheme/www/path) because
a resolution key must be canonical to be load-bearing — `domain_raw` preserves
the input verbatim alongside it.

Dedup: a domain can have many ledger runs. The winning firmo record is the
most recent completed run for that domain (`row_number() … ORDER BY
created_at DESC, run_id DESC`); `claimed_ueis` / `source_task_types` aggregate
across ALL of the domain's runs.

Lance write discipline (Pattern A canonical, mirrors
emit_spines_pdl_b2b_firmographics_lance.py):
  - LANCE_BYPASS_SPILLING=true; TMPDIR=/tmp/lance
  - lance_commit_lock("spines_firmo_blitzapi_lance")  (advisory lock on dex DB)
  - mode="overwrite" (re-runnable snapshot)
  - BTREE scalar index on domain_norm (the resolution key)
  - On index failure: HARD ABORT with rollback to prior version (or full R2
    prefix delete on first-time emit)
  - ds.optimize.compact_files(); ds.cleanup_old_versions(older_than=7d)
  - Polaris generic-table registration via catalog_hooks.

Run:
    # source = hq-x DB (HQX_DB_URL_DIRECT); R2 + DEX lock creds from hq-all/prd
    doppler run --project hq-all --config prd -- bash -c \\
        'cd apps/data-engine-x && uv run python scripts/emit_spines_firmo_blitzapi_lance.py --dry-run'
    doppler run --project hq-all --config prd -- bash -c \\
        'cd apps/data-engine-x && uv run python scripts/emit_spines_firmo_blitzapi_lance.py --apply'
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger(__name__)

R2_BUCKET = "dex-raw-landing-zone"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/firmo_blitzapi_lance"
LANCE_PREFIX = "polaris-warehouse/spines/firmo_blitzapi_lance/"

DATASET_SLUG = "spines_firmo_blitzapi_lance"
POLARIS_NAMESPACE = "spines"
POLARIS_TABLE = "firmo_blitzapi_lance"
POLARIS_DOC = (
    "Blitz-API firmographics — domain-grain spine. Promotes blitz company "
    "enrichment payloads from hq-x Postgres ops.task_runs (task_type "
    "blitz_firmo_direct + modal_hydrate_firmo_cascade, status=completed) into "
    "Lance, one row per normalized domain (latest completed run wins). Faithful "
    "passthrough: size is a string bucket, hq_state a full name, "
    "employees_on_linkedin as-stored. claimed_ueis (pipe-delimited) is the SAM "
    "UEI lineage that was the enrichment INPUT binding — NOT a trusted join "
    "key; re-associate UEI downstream via domain_norm ↔ "
    "sam_gov.entities_lance.entity_url. BTREE scalar index on domain_norm."
)

# The resolution key.
INDEX_COLUMN = "domain_norm"

# Volume floor: refuse to overwrite the spine if the projection collapses well
# below the known population (~118.5k distinct domains as of 2026-05-29). Guards
# against a bad query / upstream wipe silently publishing an empty spine.
MIN_DOMAIN_FLOOR = 100_000

TMP_DIR = "/tmp/lance"
TMP_DIR_FREE_GB_FLOOR = 5

# Domain-key canonicalization (matches run_parallel_domain_to_linkedin.py /
# build_cohort_won_365d_lance.py): lowercase, strip scheme, strip leading www.,
# cut at first path/query/fragment/port delimiter. Applied to the KEY only.
# NOTE: deliberately NO .gov/.mil junk filter — if blitz returned a company for
# a domain, that is faithful data; downstream filters as needed.
_DOMAIN_NORM_SQL = (
    r"regexp_replace("
    r"  regexp_replace("
    r"    regexp_replace(lower(trim(domain)), '^https?://', ''),"
    r"  '^www\.', ''),"
    r"'[/?#:].*$', '')"
)

PROJECT_SQL = f"""
SET statement_timeout = 0;
WITH base AS (
    SELECT
        {_DOMAIN_NORM_SQL} AS domain_norm,
        domain                              AS domain_raw,
        uei,
        linkedin_url                        AS input_linkedin_url,
        task_type,
        run_id,
        created_at,
        COALESCE(
            result_payload->'blitz_payload'->'company',
            result_payload->'blitz_data'->'company'
        ) AS company
    FROM ops.task_runs
    WHERE task_type IN ('blitz_firmo_direct', 'modal_hydrate_firmo_cascade')
      AND status = 'completed'
      AND COALESCE(
              result_payload->'blitz_payload'->'company',
              result_payload->'blitz_data'->'company'
          ) IS NOT NULL
      AND domain IS NOT NULL
),
norm AS (
    SELECT * FROM base WHERE domain_norm <> ''
),
agg AS (
    SELECT
        domain_norm,
        string_agg(DISTINCT uei, '|' ORDER BY uei)
            FILTER (WHERE uei IS NOT NULL)               AS claimed_ueis,
        count(DISTINCT uei) FILTER (WHERE uei IS NOT NULL) AS claimed_uei_count,
        string_agg(DISTINCT task_type, '|' ORDER BY task_type) AS source_task_types
    FROM norm
    GROUP BY domain_norm
),
ranked AS (
    SELECT *,
        row_number() OVER (
            PARTITION BY domain_norm
            ORDER BY created_at DESC, run_id DESC
        ) AS rn
    FROM norm
)
SELECT
    r.domain_norm,
    r.domain_raw,
    r.company->>'name'                  AS company_name,
    r.company->>'domain'                AS company_domain,
    r.company->>'website'               AS company_website,
    r.company->>'linkedin_url'          AS company_linkedin_url,
    r.company->>'linkedin_id'           AS company_linkedin_id,
    r.company->>'industry'              AS industry,
    r.company->>'size'                  AS size,
    r.company->>'employees_on_linkedin' AS employees_on_linkedin,
    r.company->>'followers'             AS followers,
    r.company->>'founded_year'          AS founded_year,
    r.company->>'type'                  AS company_type,
    r.company->>'about'                 AS about,
    (r.company->'specialties')::text    AS specialties,
    r.company->'hq'->>'city'            AS hq_city,
    r.company->'hq'->>'state'           AS hq_state,
    r.company->'hq'->>'country_code'    AS hq_country_code,
    r.company->'hq'->>'country_name'    AS hq_country_name,
    r.company->'hq'->>'region'          AS hq_region,
    r.company->'hq'->>'continent'       AS hq_continent,
    a.claimed_ueis,
    a.claimed_uei_count,
    r.input_linkedin_url,
    a.source_task_types,
    r.task_type                         AS source_task_type,
    r.run_id                            AS source_run_id,
    r.created_at                        AS source_created_at,
    now()                               AS promoted_at
FROM ranked r
JOIN agg a USING (domain_norm)
WHERE r.rn = 1
"""

# Column → pyarrow type. Everything is faithful TEXT except the count (int),
# and the two timestamps. Order here is the Lance schema order.
_STRING_COLS = [
    "domain_norm", "domain_raw", "company_name", "company_domain",
    "company_website", "company_linkedin_url", "company_linkedin_id",
    "industry", "size", "employees_on_linkedin", "followers", "founded_year",
    "company_type", "about", "specialties", "hq_city", "hq_state",
    "hq_country_code", "hq_country_name", "hq_region", "hq_continent",
    "claimed_ueis", "input_linkedin_url", "source_task_types",
    "source_task_type", "source_run_id",
]
_INT_COLS = ["claimed_uei_count"]
_TS_COLS = ["source_created_at", "promoted_at"]


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _hqx_source_url() -> str:
    url = os.environ.get("HQX_DB_URL_DIRECT") or os.environ.get("HQX_DB_URL_POOLED")
    if not url:
        raise EnvironmentError(
            "HQX_DB_URL_DIRECT (or HQX_DB_URL_POOLED) is required — the firmo "
            "payloads live in the hq-x database."
        )
    return url


def _check_tmp_capacity() -> None:
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    st = os.statvfs(TMP_DIR)
    free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
    LOG.info("TMPDIR=%s free=%.1f GB", TMP_DIR, free_gb)
    if free_gb < TMP_DIR_FREE_GB_FLOOR:
        raise RuntimeError(
            f"FAIL: {TMP_DIR} free {free_gb:.1f} GB < floor "
            f"{TMP_DIR_FREE_GB_FLOOR} GB — refusing to write."
        )


def _fetch_rows() -> list[dict]:
    import psycopg

    url = _hqx_source_url()
    t0 = time.time()
    LOG.info("querying hq-x ops.task_runs (project+dedup to domain grain) ...")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for stmt in (s.strip() for s in PROJECT_SQL.split(";") if s.strip()):
                cur.execute(stmt)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, rec)) for rec in cur.fetchall()]
    LOG.info("  fetched %d domain-grain rows in %.1fs", len(rows), time.time() - t0)
    return rows


def _build_arrow(rows: list[dict]):
    import pyarrow as pa

    fields = (
        [pa.field(c, pa.string()) for c in _STRING_COLS]
        + [pa.field(c, pa.int64()) for c in _INT_COLS]
        + [pa.field(c, pa.timestamp("us", tz="UTC")) for c in _TS_COLS]
    )
    schema = pa.schema(fields)
    arrays = {}
    for c in _STRING_COLS:
        arrays[c] = pa.array([r[c] for r in rows], type=pa.string())
    for c in _INT_COLS:
        arrays[c] = pa.array(
            [int(r[c]) if r[c] is not None else None for r in rows], type=pa.int64()
        )
    for c in _TS_COLS:
        arrays[c] = pa.array([r[c] for r in rows], type=pa.timestamp("us", tz="UTC"))
    return pa.table(arrays, schema=schema)


def _delete_r2_prefix(prefix: str) -> int:
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    paginator = s3.get_paginator("list_objects_v2")
    total = 0
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        objs = page.get("Contents", []) or []
        if not objs:
            continue
        s3.delete_objects(
            Bucket=R2_BUCKET,
            Delete={"Objects": [{"Key": o["Key"]} for o in objs]},
        )
        total += len(objs)
    return total


def _rollback(storage_options: dict, prior_version: int | None) -> None:
    import lance

    if prior_version is not None:
        LOG.error("ROLLBACK: restore prior version %s of %s", prior_version, LANCE_URI)
        ds = lance.dataset(LANCE_URI, storage_options=storage_options)
        ds.checkout_version(prior_version)
        ds.restore()
        LOG.error("ROLLBACK: restored to version %s", prior_version)
        return
    LOG.error("ROLLBACK: first-time emit — deleting R2 prefix %s", LANCE_PREFIX)
    deleted = _delete_r2_prefix(LANCE_PREFIX)
    LOG.error("ROLLBACK: deleted %d objects from %s", deleted, LANCE_PREFIX)


def emit() -> dict:
    import lance

    _check_tmp_capacity()
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ["TMPDIR"] = TMP_DIR

    storage_options = _storage_options()

    rows = _fetch_rows()
    if len(rows) < MIN_DOMAIN_FLOOR:
        raise RuntimeError(
            f"Volume floor breach: projection returned {len(rows)} domain rows, "
            f"below floor {MIN_DOMAIN_FLOOR}. Aborting WITHOUT writing."
        )
    LOG.info("volume floor passed: %d >= %d", len(rows), MIN_DOMAIN_FLOOR)

    table = _build_arrow(rows)
    LOG.info("arrow table: %d rows × %d cols", table.num_rows, len(table.schema))

    prior_version: int | None = None
    try:
        prior_ds = lance.dataset(LANCE_URI, storage_options=storage_options)
        prior_version = prior_ds.version
        LOG.info("prior dataset present: version=%s rows=%d",
                 prior_version, prior_ds.count_rows())
    except Exception:
        LOG.info("no prior dataset at target — first-time spine emit")

    metrics: dict = {"spine_rows": table.num_rows}

    with lance_commit_lock(DATASET_SLUG):
        LOG.info("writing Lance dataset (mode=overwrite) to %s ...", LANCE_URI)
        t0 = time.time()
        ds = lance.write_dataset(
            table,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=1_000_000,
        )
        lance_count = ds.count_rows()
        LOG.info("  wrote %d rows in %.1fs (version=%s)",
                 lance_count, time.time() - t0, ds.version)
        metrics["lance_rows"] = lance_count
        metrics["lance_version"] = ds.version

        if lance_count != table.num_rows:
            LOG.error("row count mismatch arrow=%d lance=%d — rollback",
                      table.num_rows, lance_count)
            _rollback(storage_options, prior_version)
            raise RuntimeError("row count mismatch on write")

        try:
            t_idx = time.time()
            LOG.info("creating BTREE on %s (replace=True) ...", INDEX_COLUMN)
            ds.create_scalar_index(INDEX_COLUMN, index_type="BTREE", replace=True)
            LOG.info("  BTREE built in %.1fs", time.time() - t_idx)
        except Exception as exc:  # noqa: BLE001
            LOG.error("INDEX FAILED — hard abort + rollback: %s", exc)
            _rollback(storage_options, prior_version)
            raise

        try:
            stats = ds.optimize.compact_files()
            LOG.info("  compact_files: %s", stats)
        except Exception as e:  # noqa: BLE001
            LOG.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:  # noqa: BLE001
            LOG.warning("  cleanup_old_versions failed (non-fatal): %s", e)

        indices = ds.list_indices()
        metrics["indices"] = [i["name"] for i in indices]
        LOG.info("INDICES (post-write): %s", metrics["indices"])

    return metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Spines — Blitz-API firmographics Lance emit (domain-grain)"
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + register Polaris")
    grp.add_argument("--dry-run", action="store_true", help="counts only; no write")
    ap.add_argument("--skip-polaris", action="store_true",
                    help="write Lance only; skip Polaris registration")
    args = ap.parse_args(argv)

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64
    # lance_commit_lock needs the dex DB; the source read needs the hq-x DB.
    if not (os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")):
        LOG.error("FAIL: DEX_DB_URL_DIRECT not set (required for commit lock)")
        return 64
    _hqx_source_url()  # raises with a clear message if hq-x URL missing

    if args.dry_run:
        rows = _fetch_rows()
        LOG.info("DRY RUN — would write %d domain-grain rows to %s", len(rows), LANCE_URI)
        floor_ok = len(rows) >= MIN_DOMAIN_FLOOR
        LOG.info("volume floor %d: %s", MIN_DOMAIN_FLOOR, "OK" if floor_ok else "BREACH")
        for r in rows[:3]:
            LOG.info("  sample: domain_norm=%s name=%r size=%r hq_state=%r "
                     "claimed_uei_count=%s claimed_ueis=%.60s",
                     r["domain_norm"], r["company_name"], r["size"], r["hq_state"],
                     r["claimed_uei_count"], str(r["claimed_ueis"]))
        return 0 if floor_ok else 1

    metrics = emit()
    LOG.info("EMIT METRICS: %s", metrics)
    if args.skip_polaris:
        LOG.info("--skip-polaris set; skipping Polaris registration")
    else:
        register_or_update_polaris(
            namespace=POLARIS_NAMESPACE,
            table_name=POLARIS_TABLE,
            s3_uri=LANCE_URI,
            docstring=POLARIS_DOC,
        )
        LOG.info("polaris registered: %s.%s", POLARIS_NAMESPACE, POLARIS_TABLE)
    LOG.info("DONE: spine rows=%d uri=%s", metrics["lance_rows"], LANCE_URI)
    return 0


if __name__ == "__main__":
    sys.exit(main())
