"""s4 — SEC ADV × PDL × GLEIF Pattern A enriched-cohort rollup at CRD grain (Modal-hosted).

Cycle 4 of 4 in the RIA-spine bridge sequence. Downstream-product surface: one
row per RIA firm in the SEC ADV base_a spine, enriched with PDL firmographics
and GLEIF LEI resolution. Match logic was already settled by cycles 2
(sec_adv_pdl_domain_lance) and 3 (sec_adv_pdl_name_state_lance).

References (load-bearing):
  L17  — every output row carries cohort_run_id UUID (fresh per emit, propagated
          to all rows); pdl_domain_bridge_run_id and pdl_name_state_bridge_run_id
          inherited from cycle 2 / cycle 3 bridge rows where applicable.
  L28  — ENRICHED COHORT IS NOT A BRIDGE: ZERO register_bridge(), ZERO
          register_match_method(), ZERO register_match_method_version(),
          ZERO start_bridge_run(), ZERO complete_bridge_run().
          No ops.bridges INSERT. No ops.match_method_versions INSERT.
          (Verified by validator grep on precedent build_bridge_sam_pdl_usaspending_lance.py)
  L47  — modal run --detach MANDATORY (joins 6 Lance datasets; >5min).
          Config: timeout=7200, memory=24576, cpu=4.
  L50  — ops.data_sources INSERT uses 5-col shape (display_name, storage_uri,
          format, status, owner_app). No 'kind' column.
  L54  — pipe-delimited VARCHAR for pdl_id_all (NOT LIST<VARCHAR>).
  L4   — spine is LEFT JOIN from base_a (all 27,679 distinct CRDs); output
          includes unmatched CRDs with NULL in PDL/GLEIF columns.

Validator schema corrections (load-bearing — supersede directive initial wording):
  P3  — PDL canonical column names: pdl_size (NOT employee_count), pdl_founded
         (NOT founded_year), pdl_linkedin_url. NO revenue/funding columns.
  P5  — dom bridge uses pdl_company_id (NOT pdl_id); ns bridge uses pdl_id.
         Both aliased to unified pdl_id when joining/coalescing.
  P6  — GLEIF schema has NO ultimate_parent_lei or ultimate_parent_name.
         Drop entirely. Use: lei, gleif_legal_name (= legal_name), entity_status,
         headquarters_country.
  P7  — schedule_d_1i fan-out: avg 5.4 websites/CRD, max 4363. Filter social-media
         domains (SOCIAL_MEDIA_DOMAINS), then pick MIN(website) for determinism.
  P2  — Match-coalesce: 46.9% pdl_id disagreement on dom∩ns (16,341 CRDs).
         Option C MANDATORY: pdl_id (dom priority) + pdl_id_all (pipe-delimited
         all distinct ids from both bridges).

Run via (DETACH IS MANDATORY — Modal CLI disconnect kills >5min attached jobs):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/build_bridge_sec_adv_pdl_gleif_lance.py::run

Or direct (if wall-clock permits):
    doppler run --project hq-all --config prd -- \\
      python3 apps/data-engine-x/scripts/build_bridge_sec_adv_pdl_gleif_lance.py --apply
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

app = modal.App("data-engine-x-sec-adv-pdl-gleif-cohort-lance-emit")

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

# ── Module-level constants ─────────────────────────────────────────────────── #
DATASET_SLUG = "sec_adv_pdl_gleif_lance"
COHORT_VERSION = "1.0.0"

BASE_A_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/base_a_lance"
)
SD1I_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/schedule_d_1i_lance"
)
DOM_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_adv_pdl_domain_lance"
)
NS_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_adv_pdl_name_state_lance"
)
PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
)
GLEIF_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_records_lance"
)
OUTPUT_LANCE_URI = (
    f"s3://dex-raw-landing-zone/polaris-warehouse/bridges/{DATASET_SLUG}"
)

# Validator-measured floor: spine is LEFT JOIN from base_a (27,679 distinct CRDs).
# G3 gate: ≥25,000 (allows headroom for any US-filter drops).
MIN_ROW_FLOOR = 25_000

# Social-media domain filter for schedule_d_1i website selection (P7).
# Pick MIN(website) after filtering these out for determinism.
SOCIAL_MEDIA_DOMAINS = frozenset({
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
})

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
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
    # Scale DuckDB for 24 GiB Modal container; ADV cohort is lighter than
    # USAspending precedent (no 15.5M-row GROUP BY), but PDL is 8.8M rows.
    con.execute("SET memory_limit='18GB'")
    con.execute("SET threads=4")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET temp_directory='/tmp/duckdb'")
    con.execute("SET max_temp_directory_size='40GB'")
    Path("/tmp/duckdb").mkdir(parents=True, exist_ok=True)
    return con


def _is_social_media(url: str) -> bool:
    """Return True if the URL belongs to a known social-media domain."""
    if not url:
        return False
    url_lower = url.lower().strip()
    for domain in SOCIAL_MEDIA_DOMAINS:
        if domain in url_lower:
            return True
    return False


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=7200,     # 2h — 6-dataset join; half precedent SAM-PDL-USAspending
    memory=24576,     # 24 GiB — PDL 8.8M rows joined via pdl_id; smaller than SAM
    cpu=4,
)
def emit() -> dict:
    """Pattern A enriched-cohort rollup at CRD grain.

    Match logic inherited from sec_adv_pdl_domain_lance (cycle 2, domain-based)
    and sec_adv_pdl_name_state_lance (cycle 3, composite-key). This function
    performs NO bridge registration and NO match-method registration (L28).
    """
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    # DataFusion external-sort spill OOMs on multi-million-row string BTREEs.
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance  # noqa
    import pyarrow.compute as pc  # noqa

    storage_options = _storage_options()

    # Per-emit provenance (L17)
    cohort_run_id = str(uuid.uuid4())
    generated_at_dt = datetime.now(tz=timezone.utc)
    generated_at_iso = generated_at_dt.isoformat()
    logger.info(
        "emit cohort_run_id=%s starting at %s (output=%s)",
        cohort_run_id, generated_at_iso, OUTPUT_LANCE_URI,
    )

    # ── Step 1: load all 6 input Lance datasets via PyLance scanners ── #

    # 1a. base_a_lance — CRD spine (471K rows, 27,679 distinct CRDs)
    logger.info("opening sec_adv/base_a_lance ...")
    base_a_ds = lance.dataset(BASE_A_LANCE_URI, storage_options=storage_options)
    base_a_arrow = base_a_ds.scanner(
        columns=["crd_number", "legal_name", "raw_json", "date_submitted"],
        filter=pc.field("crd_number").is_valid(),
    ).to_table()
    logger.info("  base_a_lance: %d rows (filing-grain)", base_a_arrow.num_rows)

    # 1b. schedule_d_1i_lance — website per CRD; social-media filter applied in Python
    # Fan-out: avg 5.4 distinct websites/CRD, max 4,363 (P7).
    # Strategy: collect per-CRD, filter socials, pick MIN(website) for determinism.
    logger.info("opening sec_adv/schedule_d_1i_lance (website rollup) ...")
    sd1i_ds = lance.dataset(SD1I_LANCE_URI, storage_options=storage_options)
    sd1i_arrow = sd1i_ds.scanner(
        columns=["crd_number", "website"],
        filter=pc.field("crd_number").is_valid(),
    ).to_table()
    logger.info("  schedule_d_1i_lance: %d rows", sd1i_arrow.num_rows)

    # Build per-CRD firm_website: filter socials then MIN(website).
    import pyarrow as pa  # noqa

    crd_websites: dict[int, list[str]] = {}
    sd1i_crds = sd1i_arrow.column("crd_number").to_pylist()
    sd1i_urls = sd1i_arrow.column("website").to_pylist()
    for crd, url in zip(sd1i_crds, sd1i_urls):
        if crd is None or url is None or url == "":
            continue
        if _is_social_media(url):
            continue
        crd_websites.setdefault(crd, []).append(url)
    # Deterministic MIN
    website_crd_list: list[int] = []
    website_url_list: list[str] = []
    for crd, urls in crd_websites.items():
        website_crd_list.append(crd)
        website_url_list.append(min(urls))
    firm_website_arrow = pa.table({
        "crd_number": pa.array(website_crd_list, type=pa.int64()),
        "firm_website": pa.array(website_url_list, type=pa.string()),
    })
    logger.info(
        "  sd1i firm_website rollup: %d distinct CRDs with non-social website",
        len(firm_website_arrow),
    )
    del sd1i_arrow, crd_websites, sd1i_crds, sd1i_urls, website_crd_list, website_url_list

    # 1c. domain bridge — cycle 2; KEY IS pdl_company_id (NOT pdl_id) — P5 CRITICAL
    logger.info("opening bridges/sec_adv_pdl_domain_lance ...")
    dom_ds = lance.dataset(DOM_BRIDGE_URI, storage_options=storage_options)
    dom_arrow = dom_ds.scanner(
        columns=[
            "crd_number",
            "pdl_company_id",   # P5: dom bridge uses pdl_company_id, NOT pdl_id
            "confidence_tier",
            "bridge_run_id",
        ],
    ).to_table()
    logger.info("  dom bridge: %d rows", dom_arrow.num_rows)

    # 1d. name+state bridge — cycle 3; KEY IS pdl_id (NOT pdl_company_id) — P5 CRITICAL
    logger.info("opening bridges/sec_adv_pdl_name_state_lance ...")
    ns_ds = lance.dataset(NS_BRIDGE_URI, storage_options=storage_options)
    ns_arrow = ns_ds.scanner(
        columns=[
            "crd_number",
            "pdl_id",           # P5: ns bridge uses pdl_id, NOT pdl_company_id
            "confidence_tier",
            "bridge_run_id",
        ],
    ).to_table()
    logger.info("  ns bridge: %d rows", ns_arrow.num_rows)

    # 1e. PDL firmographics — P3: canonical column names (pdl_size, pdl_founded, etc.)
    # NO revenue, NO funding columns (P3: those do not exist in this PDL dataset).
    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id",
            "pdl_name",
            "pdl_website",
            "pdl_linkedin_url",    # P3: pdl_linkedin_url (NOT linkedin_url)
            "pdl_industry",
            "pdl_size",            # P3: pdl_size (NOT employee_count)
            "pdl_founded",         # P3: pdl_founded (NOT founded_year)
            "pdl_locality",
            "pdl_country",
        ],
    ).to_table()
    logger.info("  pdl/free_companies_lance: %d rows", pdl_arrow.num_rows)

    # 1f. GLEIF — P6: NO ultimate_parent_lei, NO ultimate_parent_name.
    # Use: lei, legal_name (→ gleif_legal_name), entity_status, headquarters_country.
    logger.info("opening gleif/lei_records_lance ...")
    gleif_ds = lance.dataset(GLEIF_LANCE_URI, storage_options=storage_options)
    gleif_arrow = gleif_ds.scanner(
        columns=[
            "lei",
            "legal_name",        # aliased → gleif_legal_name in SQL
            "entity_status",     # → gleif_entity_status
            "headquarters_country",  # → gleif_headquarters_country
        ],
    ).to_table()
    logger.info("  gleif/lei_records_lance: %d rows", gleif_arrow.num_rows)

    # ── Step 2: DuckDB plan ── #
    con = _connect_duckdb()
    con.register("base_a_proj", base_a_arrow)
    con.register("firm_website_proj", firm_website_arrow)
    con.register("dom_proj", dom_arrow)
    con.register("ns_proj", ns_arrow)
    con.register("pdl_proj", pdl_arrow)
    con.register("gleif_proj", gleif_arrow)

    # 2a. CRD-grain spine: deduplicate base_a to one row per CRD.
    # ALL 27,679 distinct CRDs are kept in the spine (L4 directive — LEFT JOIN
    # includes all CRDs; unmatched ones have NULL PDL/GLEIF columns).
    # State is only populated when 1F1-Country = 'United States' (conditional
    # extraction — does NOT filter out non-US-country CRDs from the spine).
    # LEI from 1P (20-char alphanumeric validation; non-US CRDs may still bear LEI).
    logger.info("step 2a: building CRD-grain spine from base_a ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE crd_spine AS
        SELECT
            crd_number,
            legal_name,
            -- State: only populated when 1F1-Country = 'United States' (conditional)
            CASE
                WHEN json_extract_string(raw_json, '$."1F1-Country"') = 'United States'
                     AND length(TRIM(COALESCE(json_extract_string(raw_json, '$."1F1-State"'), ''))) = 2
                THEN UPPER(TRIM(json_extract_string(raw_json, '$."1F1-State"')))
                ELSE NULL
            END AS state,
            -- LEI from raw_json: 1P key, 20-char alphanumeric validation
            CASE
                WHEN length(TRIM(COALESCE(json_extract_string(raw_json, '$."1P"'), ''))) = 20
                THEN UPPER(TRIM(json_extract_string(raw_json, '$."1P"')))
                ELSE NULL
            END AS lei
        FROM base_a_proj
        WHERE crd_number IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY crd_number ORDER BY date_submitted DESC NULLS LAST) = 1
        """
    )
    spine_count = con.execute("SELECT COUNT(*) FROM crd_spine").fetchone()[0]
    logger.info("  crd_spine (all-CRD firm-grain, US state when available): %d distinct CRDs", spine_count)

    # 2b. Dom bridge: one-row-per-CRD priority resolution.
    # Validator note: dom CRD fan-out up to 1,235 pdl_company_ids.
    # Priority: pick the platinum row's pdl_company_id; when multiple platinum rows,
    # use MIN(pdl_company_id) for determinism. Inherit bridge_run_id from that row.
    logger.info("step 2b: dom bridge one-row-per-CRD priority resolution ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE dom_priority AS
        WITH ranked AS (
            SELECT
                crd_number,
                pdl_company_id AS pdl_id_dom,
                confidence_tier,
                bridge_run_id AS pdl_domain_bridge_run_id,
                ROW_NUMBER() OVER (
                    PARTITION BY crd_number
                    ORDER BY
                        CASE confidence_tier
                            WHEN 'platinum' THEN 1
                            WHEN 'gold'     THEN 2
                            WHEN 'silver'   THEN 3
                            ELSE 4
                        END,
                        pdl_company_id  -- deterministic tiebreak
                ) AS rn
            FROM dom_proj
            WHERE pdl_company_id IS NOT NULL
        )
        SELECT crd_number, pdl_id_dom, confidence_tier AS dom_tier, pdl_domain_bridge_run_id
        FROM ranked
        WHERE rn = 1
        """
    )
    dom_count = con.execute("SELECT COUNT(*) FROM dom_priority").fetchone()[0]
    logger.info("  dom_priority: %d distinct CRDs", dom_count)

    # 2c. NS bridge: one-row-per-CRD priority resolution.
    # Validator: ns is substantially 1:1 (11,110 distinct CRDs; 11,681 rows).
    logger.info("step 2c: ns bridge one-row-per-CRD priority resolution ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE ns_priority AS
        WITH ranked AS (
            SELECT
                crd_number,
                pdl_id AS pdl_id_ns,
                confidence_tier,
                bridge_run_id AS pdl_name_state_bridge_run_id,
                ROW_NUMBER() OVER (
                    PARTITION BY crd_number
                    ORDER BY
                        CASE confidence_tier
                            WHEN 'platinum' THEN 1
                            WHEN 'gold'     THEN 2
                            WHEN 'silver'   THEN 3
                            ELSE 4
                        END,
                        pdl_id  -- deterministic tiebreak
                ) AS rn
            FROM ns_proj
            WHERE pdl_id IS NOT NULL
        )
        SELECT crd_number, pdl_id_ns, confidence_tier AS ns_tier, pdl_name_state_bridge_run_id
        FROM ranked
        WHERE rn = 1
        """
    )
    ns_count = con.execute("SELECT COUNT(*) FROM ns_priority").fetchone()[0]
    logger.info("  ns_priority: %d distinct CRDs", ns_count)

    # 2d. pdl_id_all aggregator: pipe-delimited VARCHAR of ALL distinct pdl_ids
    # from dom + ns per CRD. Sorted ascending for determinism (L54).
    # Surfaces 46.9% disagreement to downstream callers (P2 Option C).
    logger.info("step 2d: pdl_id_all pipe-delimited aggregator ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE pdl_ids_all AS
        WITH all_ids AS (
            SELECT crd_number, pdl_company_id AS pdl_id FROM dom_proj
            WHERE pdl_company_id IS NOT NULL
            UNION
            SELECT crd_number, pdl_id FROM ns_proj
            WHERE pdl_id IS NOT NULL
        ),
        sorted AS (
            SELECT crd_number, pdl_id
            FROM all_ids
            ORDER BY crd_number, pdl_id
        )
        SELECT
            crd_number,
            STRING_AGG(pdl_id, '|' ORDER BY pdl_id) AS pdl_id_all
        FROM sorted
        GROUP BY crd_number
        """
    )
    agg_count = con.execute("SELECT COUNT(*) FROM pdl_ids_all").fetchone()[0]
    logger.info("  pdl_ids_all: %d CRDs with any PDL match", agg_count)

    # 2e. Main cohort assembly: LEFT JOIN spine × all sources.
    # pdl_id coalesce: dom wins (priority URL-based), fall back to ns.
    # pdl_match_path: 'both' = same id in dom and ns; 'domain' = dom present
    # (even if ns disagrees); 'name_state' = ns-only; NULL = no match.
    # pdl_confidence_tier: max tier across matched paths (platinum > gold > silver).
    logger.info("step 2e: main cohort LEFT JOIN assembly ...")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE cohort_out AS
        SELECT
            -- ── identity ──────────────────────────────────────────────── --
            spine.crd_number,
            spine.legal_name,
            spine.state,
            spine.lei,
            fw.firm_website,

            -- ── PDL match coalesce (Option C: P2 MANDATORY) ──────────── --
            COALESCE(dom.pdl_id_dom, ns.pdl_id_ns) AS pdl_id,
            pia.pdl_id_all,
            CASE
                WHEN dom.pdl_id_dom IS NOT NULL AND ns.pdl_id_ns IS NOT NULL
                     AND dom.pdl_id_dom = ns.pdl_id_ns   THEN 'both'
                WHEN dom.pdl_id_dom IS NOT NULL           THEN 'domain'
                WHEN ns.pdl_id_ns  IS NOT NULL            THEN 'name_state'
                ELSE NULL
            END AS pdl_match_path,
            -- pdl_confidence_tier: max tier across matched bridges
            CASE
                WHEN dom.dom_tier = 'platinum' OR ns.ns_tier = 'platinum' THEN 'platinum'
                WHEN dom.dom_tier = 'gold'     OR ns.ns_tier = 'gold'     THEN 'gold'
                WHEN dom.dom_tier = 'silver'   OR ns.ns_tier = 'silver'   THEN 'silver'
                ELSE NULL
            END AS pdl_confidence_tier,

            -- ── PDL firmographics (P3: canonical column names) ─────────── --
            pdl.pdl_name,
            pdl.pdl_website AS pdl_website,
            pdl.pdl_linkedin_url,
            pdl.pdl_industry,
            pdl.pdl_size,          -- NOT employee_count (P3)
            pdl.pdl_founded,       -- NOT founded_year (P3)
            pdl.pdl_locality,
            pdl.pdl_country,

            -- ── GLEIF (P6: NO ultimate_parent_* — those cols do not exist) ── --
            (gleif.lei IS NOT NULL) AS gleif_resolved,
            gleif.legal_name        AS gleif_legal_name,
            gleif.entity_status     AS gleif_entity_status,
            gleif.headquarters_country AS gleif_headquarters_country,

            -- ── provenance (L17 + L28) ─────────────────────────────────── --
            CAST('{cohort_run_id}' AS VARCHAR)   AS cohort_run_id,
            '{COHORT_VERSION}'                   AS cohort_version,
            TIMESTAMP '{generated_at_iso}'       AS generated_at,
            dom.pdl_domain_bridge_run_id,
            ns.pdl_name_state_bridge_run_id

        FROM crd_spine spine
        LEFT JOIN firm_website_proj fw  ON fw.crd_number   = spine.crd_number
        LEFT JOIN dom_priority dom      ON dom.crd_number  = spine.crd_number
        LEFT JOIN ns_priority  ns       ON ns.crd_number   = spine.crd_number
        LEFT JOIN pdl_ids_all  pia      ON pia.crd_number  = spine.crd_number
        LEFT JOIN pdl_proj     pdl      ON pdl.pdl_id      = COALESCE(dom.pdl_id_dom, ns.pdl_id_ns)
        LEFT JOIN gleif_proj   gleif    ON gleif.lei        = spine.lei
        """
    )

    forensic = con.execute(
        """
        SELECT
            COUNT(*)                                                              AS rows_out,
            COUNT(*) FILTER (WHERE pdl_id IS NOT NULL)                            AS rows_pdl_matched,
            COUNT(*) FILTER (WHERE pdl_match_path = 'domain')                     AS rows_domain_only,
            COUNT(*) FILTER (WHERE pdl_match_path = 'name_state')                 AS rows_ns_only,
            COUNT(*) FILTER (WHERE pdl_match_path = 'both')                       AS rows_both,
            COUNT(*) FILTER (WHERE pdl_match_path = 'domain'
                             AND pdl_id_all LIKE '%|%')                           AS rows_domain_multi,
            COUNT(*) FILTER (WHERE lei IS NOT NULL)                               AS rows_lei_bearing,
            COUNT(*) FILTER (WHERE gleif_resolved IS TRUE)                        AS rows_gleif_resolved
        FROM cohort_out
        """
    ).fetchone()
    logger.info(
        "cohort forensic: rows_out=%d pdl_matched=%d "
        "domain_only=%d ns_only=%d both=%d domain_multi=%d "
        "lei_bearing=%d gleif_resolved=%d",
        forensic[0], forensic[1], forensic[2], forensic[3],
        forensic[4], forensic[5], forensic[6], forensic[7],
    )

    rows_emitted = forensic[0]

    # ── HARD-FAIL floor gate ── #
    if rows_emitted < MIN_ROW_FLOOR:
        msg = (
            f"HARD FAIL: rows_emitted={rows_emitted} < floor={MIN_ROW_FLOOR}. "
            "Check LEFT JOIN spine — must be base_a-anchored (all 27,679 CRDs)."
        )
        logger.error(msg)
        return {"status": "failed", "error": msg, "rows": rows_emitted}

    # ── Step 3: Lance write inside commit_lock ── #
    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute("SELECT * FROM cohort_out").to_arrow_reader(
            batch_size=100_000,
        )
        logger.info("writing Lance cohort to %s ...", OUTPUT_LANCE_URI)
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        rows = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", rows, write_dur, ds.version,
        )

    # ── Step 4: BTREE scalar index on crd_number ── #
    t_btree = time.time()
    try:
        ds.create_scalar_index("crd_number", index_type="BTREE", replace=True)
        logger.info("BTREE on crd_number: OK (%.1fs)", time.time() - t_btree)
    except Exception as e:  # noqa: BLE001
        logger.error("BTREE on crd_number FAILED: %s", e)
        raise

    # ── Step 5: compact + cleanup_old_versions ── #
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("compact_files + cleanup_old_versions: OK")
    except Exception as e:  # noqa: BLE001
        logger.warning("optimize/cleanup failed (non-fatal): %s", e)

    final_rows = ds.count_rows()
    btree_dur = time.time() - t_btree
    logger.info(
        "s4 complete: %d rows, write=%.1fs btree=%.1fs",
        final_rows, write_dur, btree_dur,
    )
    return {
        "status": "succeeded",
        "rows_lance": final_rows,
        "lance_uri": OUTPUT_LANCE_URI,
        "cohort_run_id": cohort_run_id,
        "cohort_version": COHORT_VERSION,
        "rows_pdl_matched": forensic[1],
        "rows_domain_only": forensic[2],
        "rows_ns_only": forensic[3],
        "rows_both": forensic[4],
        "rows_lei_bearing": forensic[6],
        "rows_gleif_resolved": forensic[7],
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/build_bridge_sec_adv_pdl_gleif_lance.py::run`

    DETACH IS MANDATORY (L47 — Modal CLI disconnect kills attached jobs;
    6-dataset join with 8.8M-row PDL is reliably >5 min).
    """
    import argparse
    import json
    import os
    import sys

    # Allow --apply / --dry-run from local_entrypoint for local direct invocation
    ap = argparse.ArgumentParser(description="Emit sec_adv_pdl_gleif_lance cohort")
    ap.add_argument("--apply", action="store_true", help="write Lance dataset")
    ap.add_argument("--dry-run", action="store_true", default=False)
    args, _ = ap.parse_known_args()

    if args.dry_run:
        logger.info("DRY RUN — skipping remote emit")
        return

    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
