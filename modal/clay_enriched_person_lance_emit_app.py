"""Clay Enriched Person → Lance emit (Pattern A, one-shot, two datasets).

Reads entities.source_clay_enriched_person WHERE _snapshot_date = ? via
Doppler-injected DEX_DB_URL_POOLED (psycopg). Emits two Lance datasets:

  1. clay.enriched_person_lance
     One row per LinkedIn URL. Flat scalar columns + Arrow nested struct
     columns for the 6 small per-person arrays (education, certifications,
     languages, awards, volunteering, current_experience). raw_source_row
     column carries the full body as serialized JSON (pa.string()).

  2. clay.enriched_person_work_history_lance
     Long-form explode of experience[]. One row per (linkedin_url_normalized,
     experience_idx). Mirrors the shape of sam_gov.entity_pocs_lance (PR #468
     long-form POC explode). BTREE on linkedin_url_normalized + per-entry hot cols.

Snapshot date selection (precedence):
  1. --snapshot YYYY-MM-DD CLI argument.
  2. CLAY_ENRICHED_PERSON_SNAPSHOT env var.
  3. Default: SELECT MAX(_snapshot_date) FROM entities.source_clay_enriched_person.

Nested struct schema asymmetry (L48 / validator §Pre-flight 4(e)):
  The 6 per-person arrays have ASYMMETRIC key sets across the 4 sample records.
  All schemas are declared as the UNION of all keys seen across all records.
  Missing keys for any given record are null-padded. Specifically:
    - awards: some entries lack 'summary'
    - volunteering: company_id optional
    - certifications: company_id optional
    - current_experience: yearly_revenue_range only on Hillary Powell's record

IMPORTANT — DETACH IS MANDATORY:
  Run via `modal run --detach` per L47. The local_entrypoint enforces
  this requirement via comment; the operator MUST NOT omit --detach.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for lance_commit_lock.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID,
                         R2_SECRET_ACCESS_KEY).

Run via (DETACH IS MANDATORY):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach modal/clay_enriched_person_lance_emit_app.py::run

Or with explicit snapshot:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach modal/clay_enriched_person_lance_emit_app.py::run \\
      --snapshot 2026-05-17
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import modal

app = modal.App("data-engine-x-clay-enriched-person-lance-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "pyarrow>=16.0",
        "boto3",
    )
    .add_local_dir(
        Path(__file__).resolve().parent.parent / "scripts" / "dex",
        remote_path="/root/scripts",
    )
    .add_local_dir(
        Path(__file__).resolve().parent / "landing",
        remote_path="/root/landing",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# Dataset 1 — flat per-LinkedIn-URL
DATASET_SLUG_FLAT = "clay_enriched_person_lance"
LANCE_URI_FLAT = "s3://dex-raw-landing-zone/polaris-warehouse/clay/enriched_person_lance"

# Dataset 2 — long-form explode of experience[]
DATASET_SLUG_WH = "clay_enriched_person_work_history_lance"
LANCE_URI_WH = "s3://dex-raw-landing-zone/polaris-warehouse/clay/enriched_person_work_history_lance"

# BTREE columns per directive §3.
BTREE_COLUMNS_FLAT = [
    "linkedin_url_normalized",
    "slug",
    "profile_id",
    "_snapshot_date",
    "latest_experience_company_domain",
]

BTREE_COLUMNS_WH = [
    "linkedin_url_normalized",
    "company_domain",
    "org_id",
    "start_date",
    "_snapshot_date",
]

# Smoke-mode floor = 4 (4-record sample: Tim, George, John, Hillary).
# Validator §Pre-flight 4(f): 4 records, 48 work_history rows.
DEFAULT_MIN_ROW_FLOOR_FLAT = 4
DEFAULT_MIN_ROW_FLOOR_WH = 48

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


def _db_url() -> str:
    return (
        os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DATABASE_URL")
        or os.environ["DEX_DB_URL_DIRECT"]
    )


def _resolve_snapshot_date(explicit_snapshot: str | None) -> str:
    """Resolve snapshot date per precedence: arg > env > MAX from DB."""
    if explicit_snapshot:
        return explicit_snapshot

    env_snap = os.environ.get("CLAY_ENRICHED_PERSON_SNAPSHOT")
    if env_snap:
        return env_snap

    import psycopg

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(_snapshot_date)::text FROM entities.source_clay_enriched_person"
            )
            row = cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(
            "entities.source_clay_enriched_person is empty — no snapshot to emit"
        )
    return row[0]


def _existing_btree_columns(ds) -> set:
    cols = set()
    for idx in ds.list_indices():
        fields = idx.get("fields") if isinstance(idx, dict) else []
        itype = idx.get("type") if isinstance(idx, dict) else ""
        if "BTREE" in str(itype).upper() or "BTREE" in str(idx).upper():
            for f in (fields or []):
                cols.add(str(f))
    return cols


def _null_pad_struct(record: dict, all_keys: list[str]) -> dict:
    """Return a copy of record with all_keys present, defaulting missing to None."""
    return {k: record.get(k) for k in all_keys}


# ── Nested struct schemas (union of all keys across all sample records) ───────
# Per validator §Pre-flight 4(e) and L48 asymmetry mitigation.
# All fields are text (Arrow pa.string()) unless noted.

_EDUCATION_KEYS = [
    "school_name", "degree", "field_of_study", "start_date", "end_date",
    "grade", "activities",
]

_CERTIFICATIONS_KEYS = [
    "title", "company_name", "date", "summary", "credential_id",
    "verify_url", "company_id",
]

_LANGUAGES_KEYS = ["language", "proficiency"]

_AWARDS_KEYS = ["title", "company_name", "date", "summary", "company_id"]

_VOLUNTEERING_KEYS = [
    "role", "cause", "company_name", "company_domain", "summary",
    "start_date", "end_date", "company_id",
]

# current_experience mirrors experience[] entry shape + yearly_revenue_range
# (present only on some records — Hillary Powell's record adds it).
_CURRENT_EXPERIENCE_KEYS = [
    "company", "company_domain", "company_id", "end_date", "is_current",
    "locality", "org_id", "start_date", "summary", "title", "url",
    "yearly_revenue_range",
]


def _build_nested_struct_list(
    entries: list | None,
    all_keys: list[str],
) -> list[dict | None]:
    """Build a list of null-padded structs from Clay array entries.

    Missing keys within each entry are set to None (null-pad for L48 asymmetry).
    Non-dict entries become None. None input returns empty list.
    """
    if not entries or not isinstance(entries, list):
        return []
    result = []
    for e in entries:
        if not isinstance(e, dict):
            result.append({k: None for k in all_keys})
        else:
            result.append(_null_pad_struct(e, all_keys))
    return result


def _coerce_struct_list_to_arrow(entries_list: list[list], all_keys: list[str]):
    """Build pa.array of list<struct<...>> with unioned schema.

    Handles per-row asymmetry: each row may have a different subset of keys.
    Builds column arrays then combines.
    """
    import pyarrow as pa

    struct_fields = [pa.field(k, pa.string()) for k in all_keys]
    struct_type = pa.struct(struct_fields)

    # Build per-key columns across all rows' entries (flat, then reconstruct).
    # entries_list[i] = list of dicts for row i.
    # We convert to pa.array(pa.list_(struct_type)).
    arrays = []
    for entries in entries_list:
        # Each entry in this row is a dict with all_keys present (null-padded).
        structs = []
        for e in entries:
            if e is None:
                structs.append(None)
            else:
                # Convert every value to str or None.
                coerced = {}
                for k in all_keys:
                    v = e.get(k)
                    if v is None:
                        coerced[k] = None
                    elif isinstance(v, bool):
                        coerced[k] = str(v).lower()
                    else:
                        coerced[k] = str(v)
                structs.append(coerced)
        arrays.append(pa.array(structs, type=struct_type))

    list_type = pa.list_(struct_type)
    return pa.chunked_array([pa.array(arrays, type=list_type)])


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=10800,
    memory=32768,
    cpu=8,
)
def emit(
    snapshot_date: str,
    min_row_floor_flat: int = DEFAULT_MIN_ROW_FLOOR_FLAT,
    min_row_floor_wh: int = DEFAULT_MIN_ROW_FLOOR_WH,
) -> dict:
    """Pattern A Lance emit: staging → enriched_person_lance + work_history_lance.

    Emit 1: clay.enriched_person_lance (flat scalar + nested struct arrays).
    Emit 2: clay.enriched_person_work_history_lance (long-form explode of experience[]).

    Both use lance_commit_lock, BTREE, compact_files, cleanup_old_versions(7d).
    LANCE_BYPASS_SPILLING=true, TMPDIR=/tmp/lance.

    MUST be launched via `modal run --detach` per L47.
    """
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance
    import pyarrow as pa
    import psycopg

    storage_options = _storage_options()
    run_id = str(uuid.uuid4())
    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit",
        run_id=run_id,
    )
    hb_cm.__enter__()
    hb_cm.set_stage("step_1_read_staging", {"snapshot_date": snapshot_date})

    # ── Step 1: read staging table ────────────────────────────────────────────
    logger.info("reading staging table for _snapshot_date = %s ...", snapshot_date)
    t0 = time.time()

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    url,
                    slug,
                    name,
                    first_name,
                    last_name,
                    org,
                    title,
                    headline,
                    summary,
                    country,
                    location_name,
                    last_refresh::text AS last_refresh,
                    profile_id,
                    connections,
                    num_followers,
                    jobs_count,
                    latest_experience_company,
                    latest_experience_company_domain,
                    latest_experience_title,
                    latest_experience_org_id,
                    latest_experience_locality,
                    latest_experience_start_date::text AS latest_experience_start_date,
                    latest_experience_is_current,
                    linkedin_url_normalized,
                    _snapshot_date::text AS _snapshot_date,
                    raw_source_row::text AS raw_source_row,
                    source_provider,
                    ingested_at::text AS ingested_at
                FROM entities.source_clay_enriched_person
                WHERE _snapshot_date = %s::date
                """,
                (snapshot_date,),
            )
            rows = cur.fetchall()
            col_names = [desc[0] for desc in cur.description]

    staging_rows = len(rows)
    logger.info(
        "read %d rows from staging (elapsed=%.1fs)", staging_rows, time.time() - t0
    )

    if staging_rows < min_row_floor_flat:
        msg = f"FAIL: staging row count {staging_rows} below floor {min_row_floor_flat}"
        logger.error(msg)
        hb_cm.__exit__(None, None, None)
        return {"status": "failed", "error": msg, "rows_staging": staging_rows}

    # Convert to list of dicts keyed by col_names for easier access.
    row_dicts = [dict(zip(col_names, row)) for row in rows]

    # ── Step 2: parse raw_source_row JSON to build nested struct arrays ───────
    hb_cm.set_stage("step_2_parse_raw_json", {"rows_staging": staging_rows})
    logger.info("parsing raw_source_row JSON for nested struct arrays ...")
    education_lists: list[list] = []
    certifications_lists: list[list] = []
    languages_lists: list[list] = []
    awards_lists: list[list] = []
    volunteering_lists: list[list] = []
    current_experience_lists: list[list] = []

    for rd in row_dicts:
        raw_json_str = rd.get("raw_source_row") or "{}"
        try:
            raw = json.loads(raw_json_str)
        except Exception:
            raw = {}

        education_lists.append(
            _build_nested_struct_list(raw.get("education"), _EDUCATION_KEYS)
        )
        certifications_lists.append(
            _build_nested_struct_list(raw.get("certifications"), _CERTIFICATIONS_KEYS)
        )
        languages_lists.append(
            _build_nested_struct_list(raw.get("languages"), _LANGUAGES_KEYS)
        )
        awards_lists.append(
            _build_nested_struct_list(raw.get("awards"), _AWARDS_KEYS)
        )
        volunteering_lists.append(
            _build_nested_struct_list(raw.get("volunteering"), _VOLUNTEERING_KEYS)
        )
        # current_experience = latest_experience as a single-element list,
        # or experience[0] if available, preserving full struct shape.
        lx = raw.get("latest_experience")
        if isinstance(lx, dict):
            current_experience_lists.append(
                _build_nested_struct_list([lx], _CURRENT_EXPERIENCE_KEYS)
            )
        else:
            current_experience_lists.append([])

    # ── Step 3: build PyArrow table for enriched_person_lance ─────────────────
    hb_cm.set_stage("step_3_build_arrow_flat")
    logger.info("building PyArrow table for enriched_person_lance ...")

    # Flat scalar columns (text/int/bool/date) — coerced from staged values.
    flat_cols: dict[str, list] = {
        col: [rd.get(col) for rd in row_dicts]
        for col in col_names
    }

    # Convert bigint/int columns to the right types (staging returns Python ints).
    profile_id_arr = pa.array(flat_cols["profile_id"], type=pa.int64())
    latest_experience_org_id_arr = pa.array(flat_cols["latest_experience_org_id"], type=pa.int64())
    connections_arr = pa.array(flat_cols["connections"], type=pa.int64())
    num_followers_arr = pa.array(flat_cols["num_followers"], type=pa.int64())
    jobs_count_arr = pa.array(flat_cols["jobs_count"], type=pa.int64())
    latest_experience_is_current_arr = pa.array(flat_cols["latest_experience_is_current"], type=pa.bool_())

    # Build nested struct arrays (L48 — union schemas, null-pad missing keys).
    education_arr = _coerce_struct_list_to_arrow(education_lists, _EDUCATION_KEYS)
    certifications_arr = _coerce_struct_list_to_arrow(certifications_lists, _CERTIFICATIONS_KEYS)
    languages_arr = _coerce_struct_list_to_arrow(languages_lists, _LANGUAGES_KEYS)
    awards_arr = _coerce_struct_list_to_arrow(awards_lists, _AWARDS_KEYS)
    volunteering_arr = _coerce_struct_list_to_arrow(volunteering_lists, _VOLUNTEERING_KEYS)
    current_experience_arr = _coerce_struct_list_to_arrow(current_experience_lists, _CURRENT_EXPERIENCE_KEYS)

    arrow_table_flat = pa.table({
        "url": pa.array(flat_cols["url"], type=pa.string()),
        "slug": pa.array(flat_cols["slug"], type=pa.string()),
        "name": pa.array(flat_cols["name"], type=pa.string()),
        "first_name": pa.array(flat_cols["first_name"], type=pa.string()),
        "last_name": pa.array(flat_cols["last_name"], type=pa.string()),
        "org": pa.array(flat_cols["org"], type=pa.string()),
        "title": pa.array(flat_cols["title"], type=pa.string()),
        "headline": pa.array(flat_cols["headline"], type=pa.string()),
        "summary": pa.array(flat_cols["summary"], type=pa.string()),
        "country": pa.array(flat_cols["country"], type=pa.string()),
        "location_name": pa.array(flat_cols["location_name"], type=pa.string()),
        "last_refresh": pa.array(flat_cols["last_refresh"], type=pa.string()),
        "profile_id": profile_id_arr,
        "connections": connections_arr,
        "num_followers": num_followers_arr,
        "jobs_count": jobs_count_arr,
        "latest_experience_company": pa.array(flat_cols["latest_experience_company"], type=pa.string()),
        "latest_experience_company_domain": pa.array(flat_cols["latest_experience_company_domain"], type=pa.string()),
        "latest_experience_title": pa.array(flat_cols["latest_experience_title"], type=pa.string()),
        "latest_experience_org_id": latest_experience_org_id_arr,
        "latest_experience_locality": pa.array(flat_cols["latest_experience_locality"], type=pa.string()),
        "latest_experience_start_date": pa.array(flat_cols["latest_experience_start_date"], type=pa.string()),
        "latest_experience_is_current": latest_experience_is_current_arr,
        "linkedin_url_normalized": pa.array(flat_cols["linkedin_url_normalized"], type=pa.string()),
        "_snapshot_date": pa.array(flat_cols["_snapshot_date"], type=pa.string()),
        "raw_source_row": pa.array(flat_cols["raw_source_row"], type=pa.string()),
        "source_provider": pa.array(flat_cols["source_provider"], type=pa.string()),
        "ingested_at": pa.array(flat_cols["ingested_at"], type=pa.string()),
        # Nested struct columns (L48 — unioned schemas, null-padded)
        "education": education_arr,
        "certifications": certifications_arr,
        "languages": languages_arr,
        "awards": awards_arr,
        "volunteering": volunteering_arr,
        "current_experience": current_experience_arr,
    })

    logger.info(
        "arrow_table_flat: %d rows x %d cols",
        arrow_table_flat.num_rows, arrow_table_flat.num_columns,
    )

    # ── Step 4: Lance write — enriched_person_lance (inside lance_commit_lock) ─
    hb_cm.set_stage("step_4_lance_write_flat")
    write_t0 = time.time()
    with lance_commit_lock(DATASET_SLUG_FLAT):
        logger.info("writing Lance dataset to %s ...", LANCE_URI_FLAT)
        ds_flat = lance.write_dataset(
            arrow_table_flat,
            LANCE_URI_FLAT,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur_flat = time.time() - write_t0
        lance_rows_flat = ds_flat.count_rows()
        logger.info(
            "wrote enriched_person_lance: %d rows in %.1fs (version=%s)",
            lance_rows_flat, write_dur_flat, ds_flat.version,
        )

    if lance_rows_flat < min_row_floor_flat:
        msg = f"FAIL: enriched_person_lance row count {lance_rows_flat} below floor {min_row_floor_flat}"
        logger.error(msg)
        hb_cm.__exit__(None, None, None)
        return {"status": "failed", "error": msg, "rows_lance_flat": lance_rows_flat}

    # BTREE on flat dataset.
    t_btree_flat = time.time()
    existing_btree_flat = _existing_btree_columns(ds_flat)
    for col in BTREE_COLUMNS_FLAT:
        if col in existing_btree_flat:
            logger.info("BTREE on %s (flat) already present — skipping", col)
            continue
        try:
            ds_flat.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s (flat): OK", col)
        except Exception as e:
            logger.error("BTREE on %s (flat) FAILED: %s", col, e)
            raise

    try:
        ds_flat.optimize.compact_files()
        ds_flat.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:
        logger.warning("flat optimize failed (non-fatal): %s", e)

    btree_dur_flat = time.time() - t_btree_flat
    logger.info("enriched_person_lance done: %d rows, btree=%.1fs", lance_rows_flat, btree_dur_flat)

    # ── Step 5: build work_history long-form explode ──────────────────────────
    hb_cm.set_stage("step_5_work_history_explode")
    logger.info("building work_history long-form explode rows ...")

    wh_rows: list[dict] = []
    for rd in row_dicts:
        linkedin_url_norm = rd.get("linkedin_url_normalized") or ""
        snap_date = rd.get("_snapshot_date") or snapshot_date
        raw_json_str = rd.get("raw_source_row") or "{}"
        try:
            raw = json.loads(raw_json_str)
        except Exception:
            raw = {}

        experience_list = raw.get("experience") or []
        if not isinstance(experience_list, list):
            experience_list = []

        for idx, exp in enumerate(experience_list):
            if not isinstance(exp, dict):
                exp = {}
            # Flat columns per experience entry (C13 — linkedin_url_normalized
            # must match a row in enriched_person_lance).
            company = exp.get("company")
            if company and isinstance(company, str):
                company = company.strip()
            company_domain = exp.get("company_domain")
            if company_domain and isinstance(company_domain, str):
                company_domain = company_domain.strip()
            # company_url is the LinkedIn company URL (exp["url"] per Clay shape)
            company_url = exp.get("url")
            if company_url and isinstance(company_url, str):
                company_url = company_url.strip()
            org_id_raw = exp.get("org_id")
            org_id = int(org_id_raw) if org_id_raw is not None and not isinstance(org_id_raw, bool) else None
            exp_title = exp.get("title")
            if exp_title and isinstance(exp_title, str):
                exp_title = exp_title.strip()
            start_date = exp.get("start_date")
            if start_date and isinstance(start_date, str):
                start_date = start_date.strip()
            end_date = exp.get("end_date")
            if end_date and isinstance(end_date, str):
                end_date = end_date.strip()
            is_current = exp.get("is_current")
            if not isinstance(is_current, bool):
                is_current = None
            locality = exp.get("locality")
            if locality and isinstance(locality, str):
                locality = locality.strip()
            exp_summary = exp.get("summary")
            if exp_summary and isinstance(exp_summary, str):
                exp_summary = exp_summary.strip()

            wh_rows.append({
                "linkedin_url_normalized": linkedin_url_norm,
                "experience_idx": idx,
                "company": company,
                "company_domain": company_domain,
                "company_url": company_url,
                "org_id": org_id,
                "title": exp_title,
                "start_date": start_date,
                "end_date": end_date,
                "is_current": is_current,
                "locality": locality,
                "summary": exp_summary,
                "_snapshot_date": snap_date,
            })

    total_wh_rows = len(wh_rows)
    logger.info("work_history explode: %d rows (from %d people)", total_wh_rows, staging_rows)

    if total_wh_rows < min_row_floor_wh:
        msg = f"FAIL: work_history row count {total_wh_rows} below floor {min_row_floor_wh}"
        logger.error(msg)
        hb_cm.__exit__(None, None, None)
        return {
            "status": "failed",
            "error": msg,
            "rows_work_history": total_wh_rows,
            "rows_lance_flat": lance_rows_flat,
        }

    # Build Arrow table for work_history.
    arrow_table_wh = pa.table({
        "linkedin_url_normalized": pa.array([r["linkedin_url_normalized"] for r in wh_rows], type=pa.string()),
        "experience_idx": pa.array([r["experience_idx"] for r in wh_rows], type=pa.int16()),
        "company": pa.array([r["company"] for r in wh_rows], type=pa.string()),
        "company_domain": pa.array([r["company_domain"] for r in wh_rows], type=pa.string()),
        "company_url": pa.array([r["company_url"] for r in wh_rows], type=pa.string()),
        "org_id": pa.array([r["org_id"] for r in wh_rows], type=pa.int64()),
        "title": pa.array([r["title"] for r in wh_rows], type=pa.string()),
        "start_date": pa.array([r["start_date"] for r in wh_rows], type=pa.string()),
        "end_date": pa.array([r["end_date"] for r in wh_rows], type=pa.string()),
        "is_current": pa.array([r["is_current"] for r in wh_rows], type=pa.bool_()),
        "locality": pa.array([r["locality"] for r in wh_rows], type=pa.string()),
        "summary": pa.array([r["summary"] for r in wh_rows], type=pa.string()),
        "_snapshot_date": pa.array([r["_snapshot_date"] for r in wh_rows], type=pa.string()),
    })

    logger.info(
        "arrow_table_wh: %d rows x %d cols",
        arrow_table_wh.num_rows, arrow_table_wh.num_columns,
    )

    # ── Step 6: Lance write — work_history_lance (inside lance_commit_lock) ───
    hb_cm.set_stage("step_6_lance_write_wh")
    write_t0_wh = time.time()
    with lance_commit_lock(DATASET_SLUG_WH):
        logger.info("writing Lance dataset to %s ...", LANCE_URI_WH)
        ds_wh = lance.write_dataset(
            arrow_table_wh,
            LANCE_URI_WH,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur_wh = time.time() - write_t0_wh
        lance_rows_wh = ds_wh.count_rows()
        logger.info(
            "wrote work_history_lance: %d rows in %.1fs (version=%s)",
            lance_rows_wh, write_dur_wh, ds_wh.version,
        )

    # BTREE on work_history dataset.
    t_btree_wh = time.time()
    existing_btree_wh = _existing_btree_columns(ds_wh)
    for col in BTREE_COLUMNS_WH:
        if col in existing_btree_wh:
            logger.info("BTREE on %s (wh) already present — skipping", col)
            continue
        try:
            ds_wh.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s (wh): OK", col)
        except Exception as e:
            logger.error("BTREE on %s (wh) FAILED: %s", col, e)
            raise

    try:
        ds_wh.optimize.compact_files()
        ds_wh.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:
        logger.warning("wh optimize failed (non-fatal): %s", e)

    btree_dur_wh = time.time() - t_btree_wh
    final_rows_wh = ds_wh.count_rows()
    final_rows_flat = ds_flat.count_rows()
    logger.info(
        "emit complete: enriched_person=%d, work_history=%d, btree_flat=%.1fs, btree_wh=%.1fs",
        final_rows_flat, final_rows_wh, btree_dur_flat, btree_dur_wh,
    )

    hb_cm.__exit__(None, None, None)
    return {
        "status": "succeeded",
        "run_id": run_id,
        "snapshot_date": snapshot_date,
        "rows_staging": staging_rows,
        "rows_lance_enriched_person": final_rows_flat,
        "rows_lance_work_history": final_rows_wh,
        "lance_uri_flat": LANCE_URI_FLAT,
        "lance_uri_wh": LANCE_URI_WH,
        "write_duration_flat_s": round(write_dur_flat, 1),
        "write_duration_wh_s": round(write_dur_wh, 1),
        "btree_duration_flat_s": round(btree_dur_flat, 1),
        "btree_duration_wh_s": round(btree_dur_wh, 1),
    }


@app.local_entrypoint()
def run(
    snapshot: str = "",
    min_rows: int = DEFAULT_MIN_ROW_FLOOR_FLAT,
    min_rows_wh: int = DEFAULT_MIN_ROW_FLOOR_WH,
) -> None:
    """Launch the Clay Enriched Person Lance emit (both datasets).

    DETACH IS MANDATORY — run with `modal run --detach`:
        cd ~/hq-all/apps/data-engine-x && \\
          doppler run --project hq-all --config prd -- \\
          modal run --detach modal/clay_enriched_person_lance_emit_app.py::run

    Args:
        snapshot: YYYY-MM-DD snapshot date override (optional).
                  Precedence: arg > CLAY_ENRICHED_PERSON_SNAPSHOT env > MAX(_snapshot_date).
        min_rows: minimum row floor for enriched_person_lance (default=4 smoke mode).
        min_rows_wh: minimum row floor for work_history_lance (default=48 smoke mode).
    """
    import json

    resolved_snapshot = _resolve_snapshot_date(snapshot or None)
    logger.info(
        "resolved snapshot_date = %s, min_rows=%d, min_rows_wh=%d",
        resolved_snapshot, min_rows, min_rows_wh,
    )

    out = emit.remote(resolved_snapshot, min_rows, min_rows_wh)
    print(json.dumps(out, indent=2, default=str))
