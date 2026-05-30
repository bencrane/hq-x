"""ClinicalTrials.gov device-intervention studies -> R2 ZSTD Parquet ingest (AACT source).

Downloads the AACT daily flat-file export -- the official daily relational
mirror of the *entire* ClinicalTrials.gov registry, run by CTTI / Duke --
extracts the table files needed, transforms them with DuckDB into one row per
device-intervention study (typed surfaced columns + a raw_json catch-all),
writes an all-VARCHAR ZSTD Parquet snapshot to Cloudflare R2, and logs each run
to ops.clinicaltrials_device_studies_ingest_runs.

WHY AACT (not the CT.gov API).  PR #584 sourced this dataset by paginating the
public ClinicalTrials.gov API v2.  That works from a laptop but the weekly
Modal refresh cron is dead: ClinicalTrials.gov's WAF hard-blocks Modal's egress
IPs (403 across multiple Modal container IPs; a browser User-Agent did not
help).  AACT publishes a daily pipe-delimited flat-file export served from
DigitalOcean Spaces object storage, which Modal reaches without issue (object
storage does not WAF-block datacenters; the block is specific to
clinicaltrials.gov).  Switching the fetch to AACT permanently removes the
egress dependency and matches the standing preference for bulk-file downloads
over rate-limited APIs.

R2 layout (kebab-case per CLAUDE.md "R2 prefix convention" -- UNCHANGED from PR #584):
  s3://dex-raw-landing-zone/clinicaltrials-gov/device-studies/snapshot={YYYY-MM-DD}/data.parquet
The snapshot date is the AACT export date actually downloaded (today UTC, with a
day-by-day fallback up to ~7 days if today's export is not yet published).

AACT source (verified 2026-05-20):
  - Daily URL: https://aact.ctti-clinicaltrials.org/static/exported_files/daily/{YYYY-MM-DD}?source=web
    -> HTTP 302 -> ctti-aact.nyc3.digitaloceanspaces.com -> HTTP 200, application/zip, ~2.45 GB.
  - Public, unauthenticated; same-day availability.
  - The zip holds one pipe-delimited .txt per database table.  This ingest
    extracts ONLY the six tables it needs (studies, interventions, sponsors,
    conditions, countries, facilities) to keep disk modest.
  - File format: UTF-8, LF line endings, '|' delimiter, double-quote escaping
    only when a pipe is embedded in a value.  DuckDB read params therefore pin
    delim='|', quote='"', escape='"', all_varchar=TRUE, strict_mode=FALSE
    (an embedded-pipe value inside quotes exists in conditions.txt; without
    explicit quote/escape DuckDB auto-detects quote=empty and mis-splits it).
  - Schema reference: https://aact.ctti-clinicaltrials.org/schema -- column
    names below were confirmed against the downloaded files' header rows.

Device filter.  Distinct nct_id from the structured `interventions` table whose
intervention_type is in the device family -- DEVICE, DIAGNOSTIC TEST, or
COMBINATION PRODUCT (AACT stores intervention_type uppercase; the comparison is
upper-cased + underscore-normalized for robustness; the structured
interventions table, NOT browse_interventions).  All three are medical-device
regulatory categories -- DEVICE and DIAGNOSTIC TEST are unambiguous medical
devices, each with its own FDA 510(k)/PMA/De Novo pathway; COMBINATION PRODUCT
carries a device constituent.  The device family yields ~95K studies from AACT.
(PR #591 filtered on DEVICE alone -- 73,521 -- dropping ~18.9K Diagnostic Test
+ ~3.3K Combination Product studies, ~22% of the device universe; widened here
so the medtech tracker covers the full medical-device universe.)

Schema (per ARCHITECTURE-PATTERNS "Schema convention (Parquet layer)"):
typed surfaced columns for downstream filter/sort/aggregate + a raw_json VARCHAR
1:1-fidelity catch-all.  The 23 columns produced here are identical to PR #584;
the c4 Lance emit reads these and derives the 24th column
(lead_sponsor_name_normalized).  All columns are written as VARCHAR
(DuckDB all_varchar) per L9; multi-value fields (phases, conditions,
collaborator_*, device_intervention_*, location_*) are pipe-delimited VARCHAR
per L54 -- NOT JSON arrays and NOT LIST<VARCHAR>.

raw_json fidelity.  Because the source representation is now AACT-relational
(not a single CT.gov API study JSON), raw_json is a JSON object assembled per
nct_id from every AACT row used for that study: the `studies` row + its device
`interventions` + all `sponsors` + all `conditions` + all `countries` + all
`facilities`.  This satisfies CLAUDE.md "Source ingest invariant" rule 1
(1:1 column mirror) at the Parquet layer for the AACT-relational source.

Upload uses ExtraArgs={"ContentType": "application/x-parquet"} only -- no
Content-Encoding header (L42); plain .parquet extension.

Idempotent: re-running with the same --snapshot-date overwrites the same R2 key.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_clinicaltrials_device_studies_to_r2.py \\
        [--snapshot-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

import boto3
import duckdb
import httpx
import psycopg2

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# -- load-bearing constants (verify harness greps for these) -----------------

# AACT daily flat-file export base -- the official daily relational mirror of
# the entire ClinicalTrials.gov registry (CTTI / Duke).  Served from
# DigitalOcean Spaces object storage, which Modal reaches without WAF blocks.
AACT_BASE_URL = "https://aact.ctti-clinicaltrials.org/static/exported_files/daily"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "clinicaltrials-gov/device-studies"

# Day-by-day fallback if today's AACT export is not yet published.
MAX_DATE_FALLBACK_DAYS = 7
DOWNLOAD_TIMEOUT_SECONDS = 1800  # 30 min -- the daily zip is ~2.45 GB

# The six AACT table files this ingest needs (of the ~50 in the zip).
NEEDED_TABLE_FILES = (
    "studies.txt",
    "interventions.txt",
    "sponsors.txt",
    "conditions.txt",
    "countries.txt",
    "facilities.txt",
)

# DuckDB read_csv params for the AACT pipe-delimited flat-files.  delim='|',
# all_varchar per L9; quote/escape '"' because embedded pipes inside
# double-quoted values exist; strict_mode=FALSE tolerates the handful of rows
# whose column count drifts.
AACT_READ_OPTS = (
    "delim='|', header=true, all_varchar=true, "
    "quote='\"', escape='\"', strict_mode=false"
)

# Surfaced typed columns, in emit order, + raw_json catch-all (last).
# Identical to PR #584 -- the c4 Lance emit reads exactly these.
COLUMNS = [
    "nct_id",
    "study_title",
    "overall_status",
    "why_stopped",
    "study_type",
    "phases",
    "lead_sponsor_name",
    "lead_sponsor_class",
    "collaborator_names",
    "collaborator_classes",
    "device_intervention_names",
    "device_intervention_types",
    "enrollment_count",
    "start_date",
    "completion_date",
    "first_posted_date",
    "last_update_posted_date",
    "results_first_posted_date",
    "conditions",
    "location_states",
    "location_countries",
    "has_results",
    "raw_json",
]


# -- device-family filter ----------------------------------------------------
# The medical-device universe for the medtech regulatory-lifecycle tracker:
# a CT.gov / AACT intervention_type in the device family.  DEVICE and
# DIAGNOSTIC TEST are both unambiguous medical-device categories, each with its
# own FDA 510(k)/PMA/De Novo pathway; COMBINATION PRODUCT carries a device
# constituent.  PR #591 filtered on DEVICE alone and dropped ~18.9K Diagnostic
# Test + ~3.3K Combination Product studies (~22% of the device universe).
DEVICE_FAMILY_TYPES = ("DEVICE", "DIAGNOSTIC TEST", "COMBINATION PRODUCT")


def _device_family_filter(col: str) -> str:
    """SQL predicate -- TRUE when `col` holds a device-family intervention_type.

    Upper-cased + underscore-normalized so it is robust to AACT's flat-file
    casing and any space-vs-underscore separator in the multi-word values.
    """
    values = ", ".join(f"'{t}'" for t in DEVICE_FAMILY_TYPES)
    return f"upper(replace({col}, '_', ' ')) IN ({values})"


# -- DB ledger helpers -------------------------------------------------------

def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _record_run_start(conn, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.clinicaltrials_device_studies_ingest_runs
                (ingest_run_id, snapshot_date, started_at, status)
            VALUES (%s, %s, now(), 'running')
            """,
            (run_id, snapshot_date),
        )
    conn.commit()
    logger.info("started run %s snapshot=%s", run_id, snapshot_date)
    return run_id


def _record_run_complete(conn, run_id: str, rows_ingested: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.clinicaltrials_device_studies_ingest_runs
               SET status = 'completed', completed_at = now(), rows_ingested = %s
             WHERE ingest_run_id = %s
            """,
            (rows_ingested, run_id),
        )
    conn.commit()
    logger.info("completed run %s rows=%d", run_id, rows_ingested)


def _record_run_failed(conn, run_id: str, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.clinicaltrials_device_studies_ingest_runs
               SET status = 'failed', completed_at = now(), error_message = %s
             WHERE ingest_run_id = %s
            """,
            (error_message[:2000], run_id),
        )
    conn.commit()
    logger.error("failed run %s: %s", run_id, error_message[:200])


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


# -- AACT download + extract -------------------------------------------------

def _aact_url(export_date: datetime.date) -> str:
    return f"{AACT_BASE_URL}/{export_date.isoformat()}?source=web"


def _download_aact_zip(start_date: datetime.date, dest_dir: str) -> tuple[str, datetime.date]:
    """Download the AACT daily export zip; return (local_zip_path, export_date).

    Tries `start_date` first; on a non-200 / non-zip response, falls back one
    day at a time up to MAX_DATE_FALLBACK_DAYS.  The returned export_date is the
    AACT export date actually downloaded -- that becomes the snapshot date.
    """
    last_error: str | None = None
    for offset in range(MAX_DATE_FALLBACK_DAYS + 1):
        export_date = start_date - datetime.timedelta(days=offset)
        url = _aact_url(export_date)
        local_zip = os.path.join(dest_dir, f"aact-{export_date.isoformat()}.zip")
        logger.info("trying AACT export %s: %s", export_date, url)
        try:
            with httpx.Client(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        last_error = f"{export_date}: HTTP {resp.status_code}"
                        logger.warning("AACT %s -> HTTP %s, falling back", export_date, resp.status_code)
                        continue
                    content_type = resp.headers.get("content-type", "")
                    if "zip" not in content_type.lower():
                        last_error = f"{export_date}: content-type {content_type!r} (expected zip)"
                        logger.warning("AACT %s -> content-type %s, falling back", export_date, content_type)
                        continue
                    written = 0
                    with open(local_zip, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                            fh.write(chunk)
                            written += len(chunk)
            logger.info("downloaded AACT %s: %d bytes -> %s", export_date, written, local_zip)
            if written == 0:
                last_error = f"{export_date}: 0 bytes"
                Path(local_zip).unlink(missing_ok=True)
                continue
            return local_zip, export_date
        except httpx.HTTPError as exc:
            last_error = f"{export_date}: {exc}"
            logger.warning("AACT %s -> %s, falling back", export_date, exc)
            Path(local_zip).unlink(missing_ok=True)
            continue
    raise RuntimeError(
        f"AACT: no daily export available in the last {MAX_DATE_FALLBACK_DAYS + 1} "
        f"days (from {start_date}); last error: {last_error}"
    )


def _extract_needed_tables(zip_path: str, dest_dir: str) -> dict[str, str]:
    """Extract ONLY the six needed table files; return {table_file: local_path}."""
    extracted: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        members = {Path(name).name: name for name in zf.namelist()}
        for table_file in NEEDED_TABLE_FILES:
            if table_file not in members:
                raise RuntimeError(
                    f"AACT zip is missing expected table file {table_file!r}; "
                    f"present: {sorted(members)[:20]}..."
                )
            member = members[table_file]
            out_path = os.path.join(dest_dir, table_file)
            with zf.open(member) as src, open(out_path, "wb") as dst:
                while True:
                    block = src.read(8 * 1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
            size = Path(out_path).stat().st_size
            logger.info("extracted %s: %d bytes", table_file, size)
            extracted[table_file] = out_path
    return extracted


# -- DuckDB transform: AACT relational tables -> 23-column device-study rows --

def _build_transform_sql(table_paths: dict[str, str]) -> str:
    """Build the DuckDB SQL that produces the 23-column device-study result.

    One row per device-intervention nct_id.  Multi-value fields are pipe-joined
    VARCHAR (L54).  raw_json is a JSON object assembled from every AACT row used
    for the study.
    """
    studies = table_paths["studies.txt"]
    interventions = table_paths["interventions.txt"]
    sponsors = table_paths["sponsors.txt"]
    conditions = table_paths["conditions.txt"]
    countries = table_paths["countries.txt"]
    facilities = table_paths["facilities.txt"]

    def src(path: str) -> str:
        return f"read_csv('{path}', {AACT_READ_OPTS})"

    return f"""
WITH studies AS (SELECT * FROM {src(studies)}),
     interventions AS (SELECT * FROM {src(interventions)}),
     sponsors AS (SELECT * FROM {src(sponsors)}),
     conditions AS (SELECT * FROM {src(conditions)}),
     countries AS (SELECT * FROM {src(countries)}),
     facilities AS (SELECT * FROM {src(facilities)}),
device_nct AS (
    -- structured interventions table (NOT browse_interventions); a study is in
    -- scope when it has any device-family intervention.
    SELECT DISTINCT nct_id
    FROM interventions
    WHERE {_device_family_filter('intervention_type')}
),
dev_interventions AS (
    SELECT
        i.nct_id,
        array_to_string(list_distinct(list(i.name)
            FILTER (WHERE i.name IS NOT NULL AND i.name <> '')), '|')
            AS device_intervention_names,
        array_to_string(list_distinct(list(i.intervention_type)
            FILTER (WHERE i.intervention_type IS NOT NULL AND i.intervention_type <> '')), '|')
            AS device_intervention_types
    FROM interventions i
    WHERE {_device_family_filter('i.intervention_type')}
    GROUP BY i.nct_id
),
lead_sponsor AS (
    SELECT
        nct_id,
        max(name) AS lead_sponsor_name,
        max(agency_class) AS lead_sponsor_class
    FROM sponsors
    WHERE lower(lead_or_collaborator) = 'lead'
    GROUP BY nct_id
),
collab_sponsors AS (
    SELECT
        nct_id,
        array_to_string(list_distinct(list(name)
            FILTER (WHERE name IS NOT NULL AND name <> '')), '|')
            AS collaborator_names,
        array_to_string(list_distinct(list(agency_class)
            FILTER (WHERE agency_class IS NOT NULL AND agency_class <> '')), '|')
            AS collaborator_classes
    FROM sponsors
    WHERE lower(lead_or_collaborator) = 'collaborator'
    GROUP BY nct_id
),
study_conditions AS (
    SELECT
        nct_id,
        array_to_string(list_distinct(list(name)
            FILTER (WHERE name IS NOT NULL AND name <> '')), '|')
            AS conditions
    FROM conditions
    GROUP BY nct_id
),
study_facilities AS (
    SELECT
        nct_id,
        array_to_string(list_distinct(list(state)
            FILTER (WHERE state IS NOT NULL AND state <> '')), '|')
            AS location_states,
        array_to_string(list_distinct(list(country)
            FILTER (WHERE country IS NOT NULL AND country <> '')), '|')
            AS location_countries
    FROM facilities
    GROUP BY nct_id
)
SELECT
    s.nct_id,
    COALESCE(NULLIF(s.brief_title, ''), NULLIF(s.official_title, '')) AS study_title,
    NULLIF(s.overall_status, '')                    AS overall_status,
    NULLIF(s.why_stopped, '')                       AS why_stopped,
    NULLIF(s.study_type, '')                        AS study_type,
    NULLIF(s.phase, '')                             AS phases,
    ls.lead_sponsor_name                            AS lead_sponsor_name,
    ls.lead_sponsor_class                           AS lead_sponsor_class,
    cs.collaborator_names                           AS collaborator_names,
    cs.collaborator_classes                         AS collaborator_classes,
    di.device_intervention_names                    AS device_intervention_names,
    di.device_intervention_types                    AS device_intervention_types,
    NULLIF(s.enrollment, '')                        AS enrollment_count,
    NULLIF(s.start_date, '')                        AS start_date,
    NULLIF(s.completion_date, '')                   AS completion_date,
    NULLIF(s.study_first_posted_date, '')           AS first_posted_date,
    NULLIF(s.last_update_posted_date, '')           AS last_update_posted_date,
    NULLIF(s.results_first_posted_date, '')         AS results_first_posted_date,
    sc.conditions                                   AS conditions,
    sf.location_states                              AS location_states,
    sf.location_countries                           AS location_countries,
    CASE WHEN NULLIF(s.results_first_posted_date, '') IS NOT NULL
         THEN 'true' ELSE 'false' END               AS has_results,
    -- raw_json: 1:1-fidelity object assembled from every AACT row used for
    -- this study (source representation is AACT-relational, not API JSON).
    json_object(
        'source', 'aact',
        'studies', to_json(s),
        'device_interventions', (
            SELECT json_group_array(to_json(i)) FROM interventions i
            WHERE i.nct_id = s.nct_id AND {_device_family_filter('i.intervention_type')}),
        'sponsors', (
            SELECT json_group_array(to_json(sp)) FROM sponsors sp
            WHERE sp.nct_id = s.nct_id),
        'conditions', (
            SELECT json_group_array(to_json(c)) FROM conditions c
            WHERE c.nct_id = s.nct_id),
        'countries', (
            SELECT json_group_array(to_json(co)) FROM countries co
            WHERE co.nct_id = s.nct_id),
        'facilities', (
            SELECT json_group_array(to_json(f)) FROM facilities f
            WHERE f.nct_id = s.nct_id)
    )::VARCHAR                                       AS raw_json
FROM studies s
JOIN device_nct dn        ON dn.nct_id = s.nct_id
LEFT JOIN dev_interventions di ON di.nct_id = s.nct_id
LEFT JOIN lead_sponsor ls      ON ls.nct_id = s.nct_id
LEFT JOIN collab_sponsors cs   ON cs.nct_id = s.nct_id
LEFT JOIN study_conditions sc  ON sc.nct_id = s.nct_id
LEFT JOIN study_facilities sf  ON sf.nct_id = s.nct_id
"""


def _transform_to_parquet(table_paths: dict[str, str], local_parquet: str) -> int:
    """Run the DuckDB transform; COPY the result to an all-VARCHAR ZSTD Parquet.

    Returns the device-study row count.  every column is VARCHAR (all_varchar
    read per L9 + the explicit ::VARCHAR cast on raw_json); the COPY writes
    ZSTD-compressed Parquet -- downstream reads need no flag.
    """
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute(f"SET temp_directory='{os.path.dirname(local_parquet)}'")
    con.execute("SET preserve_insertion_order=false")

    transform_sql = _build_transform_sql(table_paths)
    con.execute(f"CREATE TABLE device_studies AS {transform_sql}")

    row_count = con.execute("SELECT count(*) FROM device_studies").fetchone()[0]
    dup_count = con.execute(
        "SELECT count(*) FROM ("
        "SELECT nct_id FROM device_studies GROUP BY nct_id HAVING count(*) > 1)"
    ).fetchone()[0]
    if dup_count:
        raise RuntimeError(
            f"AACT transform produced {dup_count} duplicate nct_id groups "
            "-- expected exactly one row per study"
        )
    logger.info("AACT transform: %d device-study rows (0 duplicate nct_id)", row_count)

    # Emit columns in the canonical order (matches c4's read).
    select_cols = ", ".join(COLUMNS)
    con.execute(
        f"""
        COPY (SELECT {select_cols} FROM device_studies)
        TO '{local_parquet}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    con.close()
    return row_count


def ingest(snapshot_date: datetime.date) -> int:
    """Pull the AACT device-study corpus -> R2 ZSTD Parquet snapshot.

    `snapshot_date` is the *requested* date -- the actual snapshot partition
    written is the AACT export date downloaded (today, or the most recent
    available within the fallback window).  Writes
    ops.clinicaltrials_device_studies_ingest_runs rows: running -> completed /
    failed.  Returns the number of device-study rows written.
    """
    conn = _pg_conn()
    run_id = _record_run_start(conn, snapshot_date)
    try:
        with tempfile.TemporaryDirectory(prefix="aact-ingest-") as work_dir:
            zip_path, export_date = _download_aact_zip(snapshot_date, work_dir)
            table_paths = _extract_needed_tables(zip_path, work_dir)
            # Drop the zip early -- the extracted .txt files are all we need.
            Path(zip_path).unlink(missing_ok=True)

            local_parquet = os.path.join(work_dir, "device_studies.parquet")
            row_count = _transform_to_parquet(table_paths, local_parquet)
            if row_count == 0:
                raise RuntimeError("AACT: zero device studies after transform -- aborting")

            size = Path(local_parquet).stat().st_size
            logger.info("wrote local parquet: %d bytes", size)

            # Snapshot partition = the AACT export date actually downloaded.
            r2_key = f"{R2_PREFIX}/snapshot={export_date.isoformat()}/data.parquet"
            s3 = _r2_client()
            s3.upload_file(
                local_parquet, R2_BUCKET, r2_key,
                ExtraArgs={"ContentType": "application/x-parquet"},
            )
            logger.info("uploaded -> s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, row_count)
        return row_count
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ClinicalTrials.gov device studies -> R2 ZSTD Parquet ingest (AACT source)"
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Requested snapshot date YYYY-MM-DD (default: today UTC). The "
             "actual snapshot partition is the AACT export date downloaded.",
    )
    args = parser.parse_args()

    if args.snapshot_date:
        snapshot_date = datetime.date.fromisoformat(args.snapshot_date)
    else:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()

    logger.info("requested snapshot_date=%s source=%s", snapshot_date, AACT_BASE_URL)
    n = ingest(snapshot_date)
    logger.info("AACT device-studies ingest OK: %d rows landed", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
