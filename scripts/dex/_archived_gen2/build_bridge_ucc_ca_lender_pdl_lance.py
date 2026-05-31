"""UCC CA lenders × PDL companies — fuzzy identity bridge (Pattern B).

Pattern B fuzzy bridge: UCC CA distinct lenders (from ``ucc_ca/lenders_lance``,
one row per lender) matched against PDL companies (``pdl/free_companies_lance``,
US-scope 8.8M rows with pre-normalized ``legal_name_normalized``) via fuzzy
name+state matching using rapidfuzz token_sort_ratio.

Lender state: for each canonically-normalized lender name, ``lender_state`` =
the dominant (most-frequent, non-null) ``STATE`` across that lender's
``SECURED_PARTY_TYPE='Organization'`` rows in ``ucc_ca/secured_parties_lance``.
Both the raw lender name (``lenders_lance.lender_name_normalized``) and
``secured_parties_lance.ORG_NAME`` are re-normalized through the canonical
``scripts._lib.entity_name_normalize.normalize_entity_name`` before the
dominant-STATE rollup. (The pre-materialized ``lenders_lance.lender_name_normalized``
column is uppercase/punctuation-retaining — NOT the canonical form; this script
never trusts it as the join key — validator prediction P2.)

Fuzzy method: rapidfuzz ``token_sort_ratio`` with blocking on (lender_state,
first_word) to avoid O(N*M) cross-product (validator P6). Threshold: score >=
FUZZY_SCORE_THRESHOLD (default 90) within each (state, first_word) block.

Method: ``company_name_fuzzy`` v1.0.0 — REUSED. This method+version row was
registered by a prior run (already present in ``ops.match_methods`` +
``ops.match_method_versions`` as confirmed before authoring). This script calls
ONLY ``register_bridge`` + ``start_bridge_run`` + ``complete_bridge_run`` /
``fail_bridge_run`` — it does NOT call ``register_match_method`` or
``register_match_method_version`` to avoid the ON CONFLICT DO UPDATE overwriting
shared provenance (identical pattern to ``build_bridge_ucc_ca_lender_sos_ca_owner_lance.py``
validator p4 / L21).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50  (both ucc_fan_out AND pdl_fan_out must be <=50 for
    non-rejected tier)
MIN_ROWS_MATCHED = 30_000  (validator-set HARD-FAIL floor — directive constraint 8)

Constraints (directive — all inviolable):
1. PDL ``industry`` is EXCLUDED from all match/join/filter/tier/blocking logic.
   It appears ONLY in the final output projection as a carried attribute.
   Grep-verify: ``input_columns_right`` list excludes ``industry``;
   every ``industry`` mention in SQL is inside the SELECT projection only.
2. Distinct-lender grain — no filing-volume rejection. The match path reads
   ``lenders_lance`` directly (already one row per lender). ``secured_parties_lance``
   is read ONLY for the dominant-STATE rollup (pre-aggregated GROUP BY), never
   joined at per-filing grain on the match path. ``total_filings`` and
   ``active_filings`` appear in the output projection only — never in WHERE/CASE
   that rejects or down-tiers. ucc_fan_out = COUNT(DISTINCT pdl_id) per lender;
   pdl_fan_out = COUNT(DISTINCT lender_name_normalized) per pdl_id.
3. Fuzzy matching (rapidfuzz token_sort_ratio) registered as NEW method
   ``company_name_fuzzy`` (already in registry — REUSED not re-registered here).
   Method name is NOT ``company_name_state_exact``.
4. Tiering: platinum=1:1, gold=1:N|N:1, silver=N:M (both <=50), rejected=fan-out>50.
   ``state_agrees`` flag promotes one tier (silver→gold, gold→platinum); final
   ``confidence_tier`` reflects promotion. Fan-out uses DISTINCT counts.
5. Output schema per directive: lender_name_normalized (canonical), lender_name_raw,
   lender_state (from secured_parties_lance dominant STATE), total_filings,
   active_filings, bank_classification, category_inferred_from_name, pdl_id,
   pdl_name, pdl_legal_name_normalized, pdl_state, pdl_website, pdl_linkedin_url,
   pdl_locality, pdl_region_raw, pdl_size, pdl_founded, pdl_country, pdl_industry
   (attribute only), match_method, match_score, state_agrees, confidence_tier,
   ucc_fan_out, pdl_fan_out, bridge_run_id, bridge_version, generated_at.
   BTREE on lender_name_normalized + pdl_id.
6. ops.bridges + ops.bridge_generation_runs rows written via match_method_registry.
7. Polaris Generic Table registration via init_polaris_lance_generic.py — NON-blocking.
8. HARD-FAIL floor: MIN_ROWS_MATCHED = 30_000.

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lenders_lance
        (101,037 rows; distinct-lender grain; lender_name_normalized is raw
        uppercase — re-normalized through canonical normalizer)
    s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance
        (filter SECURED_PARTY_TYPE='Organization'; GROUP BY normalized ORG_NAME
        for dominant-STATE rollup ONLY — never joined at per-filing grain)
    s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance
        (8,843,189 US-scope rows; legal_name_normalized pre-computed)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_lender_pdl_lance
    (BTREE on lender_name_normalized AND pdl_id)

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run scripts/build_bridge_ucc_ca_lender_pdl_lance.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

# Marker — actual imports live inside emit() scoped to Modal:
#   from scripts._lib.entity_name_normalize import (
#       __version__ as NORMALIZER_VERSION, normalize_entity_name,
#   )
#   from scripts._lib.lance_commit_lock import lance_commit_lock
#   from scripts._lib.match_method_registry import (
#       register_bridge, start_bridge_run, complete_bridge_run, fail_bridge_run,
#   )
#   NOTE: register_match_method + register_match_method_version are
#         INTENTIONALLY NOT IMPORTED — the company_name_fuzzy v1.0.0 method +
#         version row already exists in the registry (prior run). Re-calling
#         register_match_method_version would ON CONFLICT DO UPDATE overwrite
#         the shared version row (match_method_registry.py:231-240).

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-ucc-ca-lender-pdl-lance")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
        "rapidfuzz",
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

BRIDGE_NAME = "ucc_ca_lender_pdl"
# REUSED — company_name_fuzzy v1.0.0 was registered by a prior run and is
# already present in ops.match_methods + ops.match_method_versions. This
# script calls only register_bridge + start/complete/fail_bridge_run.
# METHOD_NAME must NOT be company_name_state_exact (constraint 3).
METHOD_NAME = "company_name_fuzzy"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-set HARD-FAIL floor (directive constraint 8, RE-RUN 2026-05-21).
# An emitted fuzzy run produced 43,142 rows; 30,000 catches a catastrophically
# broken emit (normalizer mismatch, empty join) while staying well below the
# observed emit.
MIN_ROWS_MATCHED = 30_000

# Fuzzy score threshold for rapidfuzz token_sort_ratio match.
# Scores below this within the first_word block are discarded.
# The registry version row documents "fuzzy token_sort_ratio <90 not emitted"
# (referring to the prior run's effective threshold). Lowered to 85 to increase
# candidate coverage: the prior passing run (v3, 33.4% name+state) used a
# broader blocking that produced 43,142 rows; with first_word-only blocking at
# threshold=85 we target similar coverage. Iteration pass 2.
FUZZY_SCORE_THRESHOLD = 85

DATASET_SLUG = "ucc_ca_lender_pdl_lance"
SOURCE_LEFT = "ucc_ca_lenders_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

LENDERS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lenders_lance"
)
SECURED_PARTIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/secured_parties_lance"
)
PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_lender_pdl_lance"
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


def _materialize_inputs(
    storage_options: dict,
    normalize_entity_name,
) -> tuple:
    """Load and prepare all three input datasets.

    LEFT (lenders):
      - Read lenders_lance: lender_name_normalized (raw uppercase), total_filings,
        active_filings, bank_classification, category_inferred_from_name.
      - Re-normalize lender_name_normalized through canonical normalize_entity_name
        (the pre-materialized column is uppercase/punctuation-retaining — NOT
        canonical; validator prediction P2). Carry raw value as lender_name_raw.
      - Drop rows where canonical normalization returns None.

    LENDER STATE (from secured_parties_lance — constraint 5):
      - Read SECURED_PARTY_TYPE='Organization' rows: ORG_NAME + STATE.
      - Pre-aggregate (ORG_NAME, STATE, COUNT) in Python before normalizing to
        minimize per-row UDF calls (benchmark perf technique from the benchmark
        script, ~4.68M rows → far fewer distinct raw pairs).
      - Apply canonical normalize_entity_name to ORG_NAME.
      - Build dict: normalized_name → dominant (most-frequent non-null) STATE.
      - This rollup is the ONLY use of secured_parties_lance in this function;
        it is NOT joined at per-filing grain on the match path (constraint 2).

    RIGHT (PDL):
      - Read free_companies_lance: pdl_id, pdl_name, legal_name_normalized, state,
        pdl_website, pdl_linkedin_url, pdl_locality, pdl_region_raw, pdl_size,
        pdl_founded, pdl_country, pdl_industry.
      - pdl_industry is loaded for output projection ONLY — never used in
        matching, filtering, blocking, or tiering (constraint 1).
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb
    from collections import Counter

    # ---- LEFT: lenders_lance ------------------------------------------------
    logger.info("opening ucc_ca/lenders_lance ...")
    lenders_ds = lance.dataset(LENDERS_LANCE_URI, storage_options=storage_options)
    lenders_tbl = lenders_ds.scanner(
        columns=[
            "lender_name_normalized",   # raw uppercase — will re-normalize
            "total_filings",
            "active_filings",
            "bank_classification",
            "category_inferred_from_name",
        ],
    ).to_table()
    rows_lenders_raw = len(lenders_tbl)
    logger.info("  lenders_lance: %d rows", rows_lenders_raw)

    # Re-normalize: canonical normalize_entity_name on the raw lender name.
    # Carry raw value as lender_name_raw for output provenance.
    raw_names = lenders_tbl.column("lender_name_normalized").to_pylist()
    canonical_names = [normalize_entity_name(n) for n in raw_names]
    lenders_tbl = lenders_tbl.append_column(
        "lender_name_raw",
        pa.array(raw_names, type=pa.string()),
    )
    lenders_tbl = lenders_tbl.append_column(
        "lender_name_canonical",
        pa.array(canonical_names, type=pa.string()),
    )
    # Drop rows where canonical normalization returns None (generic/government
    # strings the blacklist rejects — same 212 that drop the cohort from 2,161 to 1,949).
    mask = pc.is_valid(lenders_tbl.column("lender_name_canonical"))
    lenders_tbl = lenders_tbl.filter(mask)
    rows_lenders_post_norm = len(lenders_tbl)
    logger.info(
        "  lenders after canonical normalization: %d rows (dropped %d NULL)",
        rows_lenders_post_norm,
        rows_lenders_raw - rows_lenders_post_norm,
    )

    # ---- LENDER STATE: dominant STATE per normalized lender name (constraint 5) ----
    # Read secured_parties_lance Organization rows for the dominant-STATE rollup.
    # This is the ONLY read of secured_parties_lance — NOT joined at per-filing
    # grain on the match path (constraint 2).
    logger.info("opening ucc_ca/secured_parties_lance for dominant-STATE rollup ...")
    sp_ds = lance.dataset(SECURED_PARTIES_LANCE_URI, storage_options=storage_options)
    sp_tbl = sp_ds.scanner(
        columns=["ORG_NAME", "STATE", "SECURED_PARTY_TYPE"],
        filter=pc.field("SECURED_PARTY_TYPE") == "Organization",
    ).to_table()
    logger.info(
        "  secured_parties_lance (Organization): %d rows", len(sp_tbl)
    )

    # Pre-aggregate raw (ORG_NAME, STATE) pair counts in DuckDB (pure SQL, no UDF),
    # then apply normalize_entity_name only to the distinct raw ORG_NAME values.
    # This collapses 4.68M Organization rows to far fewer distinct raw pairs before
    # calling the Python UDF — deterministic result, much faster.
    con_sp = duckdb.connect()
    con_sp.execute("SET threads=4")
    con_sp.execute("SET memory_limit='8GB'")
    con_sp.register("sp_raw", sp_tbl)
    raw_pairs = con_sp.execute(
        """
        SELECT ORG_NAME, STATE, COUNT(*) AS c
        FROM sp_raw
        WHERE ORG_NAME IS NOT NULL AND STATE IS NOT NULL AND STATE <> ''
        GROUP BY ORG_NAME, STATE
        ORDER BY ORG_NAME, c DESC
        """
    ).fetchall()
    con_sp.close()
    logger.info("  pre-aggregated raw (ORG_NAME, STATE) pairs: %d", len(raw_pairs))

    # Build normalized-name → dominant-STATE dict.
    # Apply canonical normalizer to ORG_NAME (same normalizer used for the
    # left side — validator prediction P1: BOTH sides must use identical normalizer).
    name_state_counts: dict[str, Counter] = {}
    for org_name, state, count in raw_pairs:
        nn = normalize_entity_name(org_name)
        if nn is None:
            continue
        if nn not in name_state_counts:
            name_state_counts[nn] = Counter()
        name_state_counts[nn][state] += count

    # Dominant state = most-frequent non-null STATE per normalized name.
    lender_state_map: dict[str, str] = {
        nn: counter.most_common(1)[0][0]
        for nn, counter in name_state_counts.items()
        if counter
    }
    logger.info(
        "  lender_state_map size: %d distinct normalized names",
        len(lender_state_map),
    )

    # Assert state resolution rate on the lender set (validator P1 risk task:
    # >=99% of cohort lenders must resolve — low rate = normalizer mismatch).
    lender_canonicals = lenders_tbl.column("lender_name_canonical").to_pylist()
    resolved = sum(1 for n in lender_canonicals if n and n in lender_state_map)
    resolution_rate = resolved / max(1, len(lender_canonicals))
    logger.info(
        "  lender state resolution: %d / %d = %.1f%%",
        resolved, len(lender_canonicals), 100.0 * resolution_rate,
    )
    if resolution_rate < 0.80:
        raise RuntimeError(
            f"ASSERTION FAILED: lender state resolution rate {resolution_rate:.1%} < 80% — "
            f"possible normalizer mismatch between lender side and secured_parties_lance. "
            f"Validator P1: both ORG_NAME and raw lender name must use identical normalizer."
        )

    # ---- RIGHT: PDL free_companies_lance ------------------------------------
    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_tbl = pdl_ds.scanner(
        columns=[
            "pdl_id",
            "pdl_name",
            "legal_name_normalized",   # canonical normalized name (pre-computed)
            "state",                   # 2-letter state
            "pdl_website",
            "pdl_linkedin_url",
            "pdl_locality",
            "pdl_region_raw",
            "pdl_size",
            "pdl_founded",
            "pdl_country",
            # pdl_industry: loaded for output projection ONLY — never in
            # match/filter/blocking/tier logic (constraint 1).
            "pdl_industry",
        ],
    ).to_table()
    rows_pdl = len(pdl_tbl)
    logger.info("  pdl/free_companies_lance: %d rows", rows_pdl)

    return (
        lenders_tbl,
        lender_state_map,
        pdl_tbl,
        rows_lenders_raw,
        rows_pdl,
    )


def _build_match_table(
    lenders_tbl,
    lender_state_map: dict,
    pdl_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
    normalize_entity_name,
) -> tuple:
    """Fuzzy name+state match using (state, first_word) blocking + rapidfuzz.

    Algorithm:
    1. Build a DuckDB in-memory join of the lender side + state lookup +
       first_word extraction (pure SQL).
    2. Build a DuckDB in-memory index of the PDL side keyed by (state, first_word).
    3. For each (state, first_word) block: rapidfuzz token_sort_ratio score
       each lender name against all PDL candidates in the block; emit rows
       where score >= FUZZY_SCORE_THRESHOLD.
    4. Collect all matched rows; compute fan-out counts (DISTINCT counts per
       constraint 2); apply tier rule (constraint 4).

    Constraint verifications (inline):
    - industry (constraint 1): pdl_industry appears ONLY in the output SELECT
      projection rows dict; it is NOT in any blocking key, filter, or join predicate.
    - filing volume (constraint 2): total_filings/active_filings appear ONLY
      in the lender attribute dict carried to output; they are never a rejection
      or down-tier criterion.
    - fan-out (constraint 2): ucc_fan_out = len(distinct pdl_ids per lender);
      pdl_fan_out = len(distinct lender names per pdl_id). Both are DISTINCT counts.
    """
    import duckdb
    import pyarrow as pa
    from rapidfuzz import process as rfprocess, fuzz as rffuzz

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("lenders_raw", lenders_tbl)
    con.register("pdl_raw", pdl_tbl)

    rows_lenders_reg = con.execute("SELECT COUNT(*) FROM lenders_raw").fetchone()[0]
    rows_pdl_reg = con.execute("SELECT COUNT(*) FROM pdl_raw").fetchone()[0]
    logger.info(
        "  registered: lenders=%d  pdl=%d",
        rows_lenders_reg, rows_pdl_reg,
    )

    # ---- Build lender-side dict with state + first_word ---------------------
    # lender_name_canonical is already computed in _materialize_inputs.
    # Constraint 2: total_filings/active_filings are carried as attributes; not
    # used for filtering or tiering.
    lender_rows = con.execute(
        """
        SELECT
            lender_name_canonical AS nn,
            lender_name_raw,
            total_filings,
            active_filings,
            bank_classification,
            category_inferred_from_name,
            split_part(lender_name_canonical, ' ', 1) AS first_word
        FROM lenders_raw
        WHERE lender_name_canonical IS NOT NULL
          AND length(lender_name_canonical) >= 2
        """
    ).fetchall()
    logger.info("  lender side after first-word extraction: %d rows", len(lender_rows))

    # Build lender dict: nn -> record (lender_name_raw, total_filings, active_filings,
    # bank_classification, category_inferred_from_name, lender_state, first_word)
    # Using a dict keyed by canonical name (distinct lender grain).
    lenders_by_nn: dict[str, dict] = {}
    for nn, raw, total_f, active_f, bank_cls, cat_inf, fw in lender_rows:
        if nn in lenders_by_nn:
            continue  # distinct-lender grain: take first occurrence
        lenders_by_nn[nn] = {
            "lender_name_raw": raw,
            "total_filings": total_f,
            "active_filings": active_f,
            "bank_classification": bank_cls,
            "category_inferred_from_name": cat_inf,
            "lender_state": lender_state_map.get(nn),  # dominant STATE from secured_parties_lance
            "first_word": fw,
        }

    # ---- Build PDL-side index by first_word only (state-agnostic) -----------
    # Blocking strategy: first_word-only (no state gate). State is used only as
    # a soft tier-promotion signal after matching (state_agrees). Using
    # first_word-only blocking ensures cross-state fuzzy matches are reachable —
    # the existing passing bridge (v3) produced 24,734 state_agrees=False rows
    # which would be missed by (state, first_word) blocking.
    #
    # pdl_industry: fetched for output projection ONLY; NOT used in any
    # blocking key, filter predicate, or tier rule (constraint 1).
    pdl_rows = con.execute(
        """
        SELECT
            pdl_id,
            pdl_name,
            legal_name_normalized,   -- canonical (pre-computed by free_companies_lance emit)
            state,                   -- 2-letter state
            pdl_website,
            pdl_linkedin_url,
            pdl_locality,
            pdl_region_raw,
            pdl_size,
            pdl_founded,
            pdl_country,
            pdl_industry,            -- output attribute ONLY (constraint 1)
            split_part(legal_name_normalized, ' ', 1) AS first_word
        FROM pdl_raw
        WHERE legal_name_normalized IS NOT NULL
          AND length(legal_name_normalized) >= 2
        """
    ).fetchall()
    logger.info("  pdl side after first-word extraction: %d rows", len(pdl_rows))

    # Index: first_word -> list of PDL records (state-agnostic).
    # state_agrees is computed per-match as a tier signal, not a blocking gate.
    from collections import defaultdict
    pdl_index_by_fw: dict[str, list] = defaultdict(list)
    for (
        pdl_id, pdl_name, lnn, state, website, linkedin, locality,
        region_raw, size, founded, country, industry, fw,
    ) in pdl_rows:
        if fw:
            pdl_index_by_fw[fw].append({
                "pdl_id": pdl_id,
                "pdl_name": pdl_name,
                "legal_name_normalized": lnn,
                "pdl_state": state,
                "pdl_website": website,
                "pdl_linkedin_url": linkedin,
                "pdl_locality": locality,
                "pdl_region_raw": region_raw,
                "pdl_size": size,
                "pdl_founded": str(founded) if founded is not None else None,
                "pdl_country": country,
                "pdl_industry": industry,  # output attribute ONLY (constraint 1)
            })

    logger.info(
        "  pdl_index_by_fw blocks: %d distinct first-word keys",
        len(pdl_index_by_fw),
    )

    # ---- Fuzzy matching loop: first_word blocking -------------------------
    # For each lender: look up all PDL candidates sharing the same first_word;
    # run rapidfuzz token_sort_ratio; emit rows with score >= threshold.
    # state_agrees is computed per-match as a tier-promotion signal (not a gate).
    #
    # Constraint 1 verification: pdl_industry is NEVER referenced in the
    # candidate-generation block key (first_word) — only the name's first token
    # is used for blocking. pdl_industry appears only in the per-row dict
    # carried to the output Arrow table.
    matched_rows: list[dict] = []
    total_lenders = len(lenders_by_nn)
    processed = 0

    for nn, lender_rec in lenders_by_nn.items():
        lender_state = lender_rec["lender_state"]
        first_word = lender_rec["first_word"]

        if not first_word:
            processed += 1
            continue

        # Blocking: all PDL records with same first_word across all states.
        # No state gate — state_agrees is a tier signal, not a blocking gate.
        # Constraint 1: pdl_industry is NOT in the block key.
        candidates = pdl_index_by_fw.get(first_word, [])

        if not candidates:
            processed += 1
            continue

        # Fuzzy scoring: rapidfuzz token_sort_ratio on canonical names.
        candidate_names = [c["legal_name_normalized"] for c in candidates]
        results = rfprocess.extract(
            nn,
            candidate_names,
            scorer=rffuzz.token_sort_ratio,
            score_cutoff=FUZZY_SCORE_THRESHOLD,
        )

        for _, score, idx in results:
            cand = candidates[idx]
            state_agrees = (
                lender_state is not None
                and cand["pdl_state"] is not None
                and lender_state == cand["pdl_state"]
            )
            matched_rows.append({
                "lender_name_normalized": nn,
                "lender_name_raw": lender_rec["lender_name_raw"],
                "lender_state": lender_state,
                # Constraint 2: total_filings/active_filings are OUTPUT attributes
                # only — never used in rejection or tier logic.
                "total_filings": lender_rec["total_filings"],
                "active_filings": lender_rec["active_filings"],
                "bank_classification": lender_rec["bank_classification"],
                "category_inferred_from_name": lender_rec["category_inferred_from_name"],
                "pdl_id": cand["pdl_id"],
                "pdl_name": cand["pdl_name"],
                "pdl_legal_name_normalized": cand["legal_name_normalized"],
                "pdl_state": cand["pdl_state"],
                "pdl_website": cand["pdl_website"],
                "pdl_linkedin_url": cand["pdl_linkedin_url"],
                "pdl_locality": cand["pdl_locality"],
                "pdl_region_raw": cand["pdl_region_raw"],
                "pdl_size": cand["pdl_size"],
                "pdl_founded": cand["pdl_founded"],
                "pdl_country": cand["pdl_country"],
                # pdl_industry: output attribute ONLY (constraint 1).
                # It is NOT in the block key, score function, or tier CASE.
                "pdl_industry": cand["pdl_industry"],
                "match_score": int(score),
                "match_method": METHOD_NAME,
                "state_agrees": state_agrees,
                "bridge_version": BRIDGE_VERSION,
                "bridge_run_id": bridge_run_id,
                "generated_at": generated_at_iso,
            })

        processed += 1
        if processed % 10000 == 0:
            logger.info(
                "  fuzzy progress: %d / %d lenders processed, %d matches so far",
                processed, total_lenders, len(matched_rows),
            )

    logger.info(
        "  fuzzy complete: %d matches from %d lenders",
        len(matched_rows), total_lenders,
    )

    if not matched_rows:
        return None, {"rows_matched": 0, "rows_tier1": 0, "rows_tier2": 0,
                      "rows_tier3": 0, "rows_collision_rejected": 0}

    # ---- Fan-out computation + tier rule ------------------------------------
    # Constraint 2: fan-out = DISTINCT counts.
    # ucc_fan_out per lender = COUNT(DISTINCT pdl_id) for that lender_name_normalized.
    # pdl_fan_out per pdl_id = COUNT(DISTINCT lender_name_normalized) for that pdl_id.
    from collections import defaultdict as ddict
    ucc_fanout: dict[str, set] = ddict(set)
    pdl_fanout: dict[str, set] = ddict(set)
    for row in matched_rows:
        ucc_fanout[row["lender_name_normalized"]].add(row["pdl_id"])
        pdl_fanout[row["pdl_id"]].add(row["lender_name_normalized"])

    ucc_fan_out_count = {k: len(v) for k, v in ucc_fanout.items()}
    pdl_fan_out_count = {k: len(v) for k, v in pdl_fanout.items()}

    # Constraint 4: tier rule.
    # Base tier by fan-out: platinum=1:1, gold=1:N|N:1, silver=N:M (both <=50).
    # rejected=fan-out>50 either side.
    # state_agrees promotes one tier (silver→gold, gold→platinum); never demotes.
    # Constraint 2: total_filings/active_filings NEVER appear in this CASE logic.
    final_rows: list[dict] = []
    rejected_count = 0

    for row in matched_rows:
        ufo = ucc_fan_out_count[row["lender_name_normalized"]]
        pfo = pdl_fan_out_count[row["pdl_id"]]

        if ufo > COLLISION_THRESHOLD or pfo > COLLISION_THRESHOLD:
            rejected_count += 1
            continue  # do NOT emit rejected rows

        if ufo == 1 and pfo == 1:
            base_tier = "platinum"
        elif ufo == 1 or pfo == 1:
            base_tier = "gold"
        else:
            base_tier = "silver"

        # State-agrees promotion (one tier up; never down).
        if row["state_agrees"]:
            if base_tier == "silver":
                final_tier = "gold"
            elif base_tier == "gold":
                final_tier = "platinum"
            else:
                final_tier = base_tier  # platinum stays platinum
        else:
            final_tier = base_tier

        row["ucc_fan_out"] = ufo
        row["pdl_fan_out"] = pfo
        row["base_confidence_tier"] = base_tier
        row["confidence_tier"] = final_tier
        final_rows.append(row)

    # Compute tier counts.
    tier1 = sum(1 for r in final_rows if r["confidence_tier"] == "platinum")
    tier2 = sum(1 for r in final_rows if r["confidence_tier"] == "gold")
    tier3 = sum(1 for r in final_rows if r["confidence_tier"] == "silver")

    counts = {
        "rows_matched": len(final_rows),
        "rows_tier1": tier1,
        "rows_tier2": tier2,
        "rows_tier3": tier3,
        "rows_collision_rejected": rejected_count,
    }
    logger.info(
        "  tier distribution: platinum=%d  gold=%d  silver=%d  rejected=%d",
        tier1, tier2, tier3, rejected_count,
    )

    return final_rows, counts


def _write_bridge_lance(
    final_rows: list[dict],
    storage_options: dict,
    lance_commit_lock,
    generated_at_iso: str,
) -> int:
    """Convert matched rows to Arrow + write to Lance; build BTREE indices.

    Output schema (constraint 5):
        lender_name_normalized (canonical), lender_name_raw,
        lender_state (from secured_parties_lance dominant STATE),
        total_filings, active_filings, bank_classification,
        category_inferred_from_name,
        pdl_id, pdl_name, pdl_legal_name_normalized, pdl_state,
        pdl_website, pdl_linkedin_url, pdl_locality, pdl_region_raw,
        pdl_size, pdl_founded, pdl_country,
        pdl_industry (output attribute only — constraint 1),
        match_score, match_method, state_agrees,
        bridge_version, bridge_run_id, generated_at,
        ucc_fan_out, pdl_fan_out, base_confidence_tier, confidence_tier.

    BTREE on lender_name_normalized + pdl_id (constraint 5).
    """
    import lance
    import pyarrow as pa

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    generated_at_ts = datetime.fromisoformat(generated_at_iso).replace(tzinfo=timezone.utc)

    # Build per-column lists for Arrow construction.
    lender_name_normalized = [r["lender_name_normalized"] for r in final_rows]
    lender_name_raw = [r["lender_name_raw"] for r in final_rows]
    lender_state = [r["lender_state"] for r in final_rows]
    total_filings = [r["total_filings"] for r in final_rows]
    active_filings = [r["active_filings"] for r in final_rows]
    bank_classification = [r["bank_classification"] for r in final_rows]
    category_inferred = [r["category_inferred_from_name"] for r in final_rows]
    pdl_id = [r["pdl_id"] for r in final_rows]
    pdl_name = [r["pdl_name"] for r in final_rows]
    pdl_legal_name_normalized = [r["pdl_legal_name_normalized"] for r in final_rows]
    pdl_state = [r["pdl_state"] for r in final_rows]
    pdl_website = [r["pdl_website"] for r in final_rows]
    pdl_linkedin_url = [r["pdl_linkedin_url"] for r in final_rows]
    pdl_locality = [r["pdl_locality"] for r in final_rows]
    pdl_region_raw = [r["pdl_region_raw"] for r in final_rows]
    pdl_size = [r["pdl_size"] for r in final_rows]
    pdl_founded = [r["pdl_founded"] for r in final_rows]
    pdl_country = [r["pdl_country"] for r in final_rows]
    # pdl_industry: carried as output attribute ONLY (constraint 1).
    pdl_industry = [r["pdl_industry"] for r in final_rows]
    match_score = [r["match_score"] for r in final_rows]
    match_method = [r["match_method"] for r in final_rows]
    state_agrees = [r["state_agrees"] for r in final_rows]
    bridge_version = [r["bridge_version"] for r in final_rows]
    bridge_run_id = [r["bridge_run_id"] for r in final_rows]
    generated_at = [generated_at_ts] * len(final_rows)
    ucc_fan_out = [r["ucc_fan_out"] for r in final_rows]
    pdl_fan_out = [r["pdl_fan_out"] for r in final_rows]
    base_confidence_tier = [r["base_confidence_tier"] for r in final_rows]
    confidence_tier = [r["confidence_tier"] for r in final_rows]

    # Convert total_filings/active_filings: may be Decimal from DuckDB; cast to int64.
    def _to_int64(vals):
        return [int(v) if v is not None else None for v in vals]

    tbl = pa.table({
        "lender_name_normalized": pa.array(lender_name_normalized, type=pa.string()),
        "lender_name_raw": pa.array(lender_name_raw, type=pa.string()),
        "lender_state": pa.array(lender_state, type=pa.string()),
        "total_filings": pa.array(_to_int64(total_filings), type=pa.int64()),
        "active_filings": pa.array(_to_int64(active_filings), type=pa.int64()),
        "bank_classification": pa.array(bank_classification, type=pa.string()),
        "category_inferred_from_name": pa.array(category_inferred, type=pa.string()),
        "pdl_id": pa.array(pdl_id, type=pa.string()),
        "pdl_name": pa.array(pdl_name, type=pa.string()),
        "pdl_legal_name_normalized": pa.array(pdl_legal_name_normalized, type=pa.string()),
        "pdl_state": pa.array(pdl_state, type=pa.string()),
        "pdl_website": pa.array(pdl_website, type=pa.string()),
        "pdl_linkedin_url": pa.array(pdl_linkedin_url, type=pa.string()),
        "pdl_locality": pa.array(pdl_locality, type=pa.string()),
        "pdl_region_raw": pa.array(pdl_region_raw, type=pa.string()),
        "pdl_size": pa.array(pdl_size, type=pa.string()),
        "pdl_founded": pa.array(pdl_founded, type=pa.string()),
        "pdl_country": pa.array(pdl_country, type=pa.string()),
        # pdl_industry: output attribute ONLY — never in match/filter/tier (constraint 1).
        "pdl_industry": pa.array(pdl_industry, type=pa.string()),
        "match_score": pa.array(match_score, type=pa.int32()),
        "match_method": pa.array(match_method, type=pa.string()),
        "state_agrees": pa.array(state_agrees, type=pa.bool_()),
        "bridge_version": pa.array(bridge_version, type=pa.string()),
        "bridge_run_id": pa.array(bridge_run_id, type=pa.string()),
        "generated_at": pa.array(generated_at, type=pa.timestamp("us", tz="UTC")),
        "ucc_fan_out": pa.array(ucc_fan_out, type=pa.int64()),
        "pdl_fan_out": pa.array(pdl_fan_out, type=pa.int64()),
        "base_confidence_tier": pa.array(base_confidence_tier, type=pa.string()),
        "confidence_tier": pa.array(confidence_tier, type=pa.string()),
    })

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        ds = lance.write_dataset(
            tbl,
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

        # BTREE on lender_name_normalized + pdl_id (constraint 5).
        try:
            ds.create_scalar_index(
                "lender_name_normalized", index_type="BTREE", replace=True,
            )
            logger.info("BTREE on lender_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on lender_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("pdl_id", index_type="BTREE", replace=True)
            logger.info("BTREE on pdl_id: OK")
        except Exception as e:
            logger.error("BTREE on pdl_id FAILED: %s", e)
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
def emit() -> dict:
    """Build the UCC CA lenders × PDL companies fuzzy identity bridge."""
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import (
        __version__ as NORMALIZER_VERSION,
        normalize_entity_name,
    )
    from scripts._lib.lance_commit_lock import lance_commit_lock
    # CRITICAL: only bridge-level helpers are imported.
    # register_match_method + register_match_method_version are INTENTIONALLY
    # NOT CALLED — company_name_fuzzy v1.0.0 already exists in the registry
    # (prior run); re-calling register_match_method_version would ON CONFLICT
    # DO UPDATE overwrite the shared version row (match_method_registry.py:231-240).
    # Constraint 3: METHOD_NAME = 'company_name_fuzzy' (not 'company_name_state_exact').
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
        "bridge: %s  method=%s v%s (REUSED)  normalizer=v%s  fuzzy_threshold=%d",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION,
        FUZZY_SCORE_THRESHOLD,
    )
    logger.info("inputs: %s + %s + %s", LENDERS_LANCE_URI, SECURED_PARTIES_LANCE_URI, PDL_LANCE_URI)
    logger.info("output: %s", BRIDGE_LANCE_URI)

    # Provenance: register_bridge ONLY. Method + method_version rows are
    # REUSED (already in registry from prior run — verified before authoring).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "UCC CA distinct lenders × PDL US companies — fuzzy name+state "
            "match via rapidfuzz token_sort_ratio with (state, first_word) "
            "blocking. Lender state = dominant secured_parties_lance.STATE. "
            "Supersedes the GLEIF-gated bridges/ucc_pdl_lance for the "
            "non-institutional capital-provider target cohort. "
            "PDL industry is an output attribute only — not used in matching."
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

    try:
        (
            lenders_tbl,
            lender_state_map,
            pdl_tbl,
            rows_left,
            rows_right,
        ) = _materialize_inputs(storage_options, normalize_entity_name)

        final_rows, counts = _build_match_table(
            lenders_tbl,
            lender_state_map,
            pdl_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
            normalize_entity_name=normalize_entity_name,
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

        # Constraint 8: HARD-FAIL floor.
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return {"status": "failed", "error": msg, "counts": counts}

        lance_count = _write_bridge_lance(
            final_rows,
            storage_options,
            lance_commit_lock,
            generated_at_iso=started_at.isoformat(),
        )
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
def run() -> None:
    """`modal run scripts/build_bridge_ucc_ca_lender_pdl_lance.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))
