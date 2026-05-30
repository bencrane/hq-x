"""PDL × SBA fuzzy match v1 — embedding-cosine sibling matcher.

Cycle: hq-all-pdl-sba-fuzzy-match-v1
Predecessor: Phase 4 vector primitives (PR #369)

This module implements the borrower-side augmentation for the 2026 COMMIT
cohort (and any cohort the caller passes). It is intentionally **not** built
on the FMCSA-style EmbeddingEmitConfig flow because:

  - The source rows live in DEX Postgres (not Lance).
  - The result table is small (~7K borrowers × top-3 matches ≈ 20K rows)
    so we INSERT to Postgres, not write a new Lance dataset.
  - Vectors are intermediate; we discard them after scoring (no pgvector
    extension, no need to persist).

We DO reuse the low-level embedder from `scripts._lib.embedding_emit`
so the sentence-transformers model and its batching live in one place.

Pipeline shape:
  1. SELECT borrowers — `_select_unmatched_borrowers`
  2. For each (state, first_word) bucket, SELECT PDL candidates capped at
     200/borrower (already deduped) — `_select_candidates_for_bucket`
  3. Compose profile text both sides — `_compose_borrower_profile`,
     `_compose_pdl_profile`
  4. Batch-embed via sentence-transformers (single global batch)
  5. For each borrower, top-K cosine score against its candidates
  6. Bulk INSERT to entities.pdl_to_sba_borrowers_fuzzy_v1 with
     ON CONFLICT DO NOTHING (idempotent)

Cohort filter is an opaque SQL fragment ANDed into the SELECT. Default:
``loanstatus = 'COMMIT' AND approvaldate >= '2026-01-01'``.

Required env (from Doppler hq-all/prd):
    DEX_DB_URL_DIRECT — for the candidate-gen SELECTs and INSERT.

Optional env:
    EMBEDDING_PROVIDER — passthrough to scripts._lib.embedding_emit.
                         Default 'sentence-transformers' (free).
    PDL_SBA_FUZZY_THRESHOLD — minimum cosine to keep a match (default 0.5).
    PDL_SBA_FUZZY_TOP_K — top-K matches kept per borrower (default 3).
    PDL_SBA_FUZZY_CAND_CAP — max candidates per borrower (default 200).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger(__name__)

# Add the data-engine-x app dir to path so `scripts._lib.embedding_emit`
# imports work whether this module is invoked from a runner script or from
# the Modal container.
_APP_DIR = Path(__file__).resolve().parents[2]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


MATCH_PATH = "vector_state_first_word_block_v1"

# Tiers — see directive. Thresholds are conservative defaults; operator can
# tune via env if smoke tests reveal a better empirical point.
DEFAULT_THRESHOLD = float(os.environ.get("PDL_SBA_FUZZY_THRESHOLD", "0.5"))
DEFAULT_TOP_K = int(os.environ.get("PDL_SBA_FUZZY_TOP_K", "3"))
DEFAULT_CAND_CAP = int(os.environ.get("PDL_SBA_FUZZY_CAND_CAP", "200"))
TIER_HIGH = 0.75
TIER_MEDIUM = 0.65


@dataclass
class MatchJobConfig:
    """Per-run config."""

    cohort_sql_filter_7a: str = (
        "loanstatus = 'COMMIT' AND approvaldate >= '2026-01-01'"
    )
    cohort_sql_filter_504: str | None = None  # Default: skip 504 for v1.
    threshold: float = DEFAULT_THRESHOLD
    top_k: int = DEFAULT_TOP_K
    cand_cap: int = DEFAULT_CAND_CAP
    dry_run: bool = False
    max_borrowers: int | None = None  # for smoke / cost test


@dataclass
class _Borrower:
    sba_loan_id: str
    program: str
    borrname: str
    borrcity: str | None
    borrstate: str
    state_lower: str  # full state name lower (matches mv_pdl_companies_normalized.region_lower)
    name_norm: str
    first_word: str
    profile_text: str
    naicsdescription: str | None = None
    businesstype: str | None = None
    franchisename: str | None = None


@dataclass
class _Candidate:
    sba_loan_id: str
    program: str
    pdl_id: str
    pdl_name: str
    profile_text: str


def _select_unmatched_borrowers(
    cur: Any, config: MatchJobConfig,
) -> list[_Borrower]:
    """Pull the unmatched cohort from sba_7a_loans (+ sba_504 if configured)."""
    parts: list[str] = []
    binds: list[Any] = []

    # 7a leg
    parts.append(f"""
        SELECT s.id::text AS sba_loan_id,
               '7a'::text AS program,
               s.borrname,
               s.borrcity,
               s.borrstate,
               us.name_lowercase AS state_lower,
               pdl.normalize_company_name(s.borrname) AS name_norm,
               s.naicsdescription,
               s.businesstype,
               s.franchisename
          FROM entities.sba_7a_loans s
          LEFT JOIN entities.mv_pdl_to_sba_borrowers m ON m.sba_loan_id = s.id
          JOIN lookup.us_states us ON us.code = upper(s.borrstate)
         WHERE m.sba_loan_id IS NULL
           AND s.borrname IS NOT NULL AND s.borrname <> ''
           AND s.borrstate IS NOT NULL AND s.borrstate <> ''
           AND ({config.cohort_sql_filter_7a})
    """)

    if config.cohort_sql_filter_504:
        parts.append(f"""
        UNION ALL
        SELECT s.id::text AS sba_loan_id,
               '504'::text AS program,
               s.borrname,
               s.borrcity,
               s.borrstate,
               us.name_lowercase AS state_lower,
               pdl.normalize_company_name(s.borrname) AS name_norm,
               s.naicsdescription,
               s.businesstype,
               NULL::text AS franchisename
          FROM entities.sba_504_loans s
          LEFT JOIN entities.mv_pdl_to_sba_borrowers m ON m.sba_loan_id = s.id
          JOIN lookup.us_states us ON us.code = upper(s.borrstate)
         WHERE m.sba_loan_id IS NULL
           AND s.borrname IS NOT NULL AND s.borrname <> ''
           AND s.borrstate IS NOT NULL AND s.borrstate <> ''
           AND ({config.cohort_sql_filter_504})
        """)

    sql = "WITH rows AS (\n" + "\n".join(parts) + "\n)\n"
    sql += "SELECT * FROM rows WHERE name_norm IS NOT NULL AND name_norm <> '' AND length(name_norm) >= 2"
    if config.max_borrowers is not None:
        sql += f"\nLIMIT {int(config.max_borrowers)}"

    cur.execute(sql, binds)
    out: list[_Borrower] = []
    for r in cur.fetchall():
        sba_loan_id, program, borrname, borrcity, borrstate, \
            state_lower, name_norm, naics, btype, franch = r
        first_word = name_norm.split(" ", 1)[0]
        if not first_word:
            continue
        b = _Borrower(
            sba_loan_id=sba_loan_id,
            program=program,
            borrname=borrname,
            borrcity=borrcity,
            borrstate=borrstate,
            state_lower=state_lower,
            name_norm=name_norm,
            first_word=first_word,
            naicsdescription=naics,
            businesstype=btype,
            franchisename=franch,
            profile_text=_compose_borrower_profile(
                borrname=borrname, borrcity=borrcity, borrstate=borrstate,
                naicsdescription=naics, businesstype=btype, franchisename=franch,
            ),
        )
        out.append(b)
    LOG.info("borrowers selected: %d", len(out))
    return out


def _compose_borrower_profile(
    *, borrname: str, borrcity: str | None, borrstate: str,
    naicsdescription: str | None, businesstype: str | None,
    franchisename: str | None,
) -> str:
    """Human-readable composite for the SBA borrower side."""
    parts: list[str] = [f"Business: {borrname.strip()}"]
    loc_bits: list[str] = []
    if borrcity:
        loc_bits.append(borrcity.strip())
    if borrstate:
        loc_bits.append(borrstate.strip())
    if loc_bits:
        parts.append("located in " + ", ".join(loc_bits))
    if naicsdescription:
        parts.append(f"industry: {naicsdescription.strip().lower()}")
    if businesstype:
        parts.append(f"organization type: {businesstype.strip().lower()}")
    if franchisename:
        parts.append(f"franchise: {franchisename.strip()}")
    return " | ".join(parts)


def _compose_pdl_profile(
    *, name: str, locality_lower: str | None, region_lower: str | None,
    industry: str | None, size_bucket: str | None, founded_year: int | None,
    website: str | None,
) -> str:
    """Human-readable composite for the PDL side."""
    parts: list[str] = [f"Business: {name.strip()}"]
    loc_bits: list[str] = []
    if locality_lower:
        loc_bits.append(locality_lower.strip())
    if region_lower:
        loc_bits.append(region_lower.strip())
    if loc_bits:
        parts.append("located in " + ", ".join(loc_bits))
    if industry:
        parts.append(f"industry: {industry.strip().lower()}")
    if size_bucket:
        parts.append(f"size: {size_bucket.strip()}")
    if founded_year:
        parts.append(f"founded {founded_year}")
    if website:
        parts.append(f"website: {website.strip()}")
    return " | ".join(parts)


_CAND_CHUNK = 500


def _select_candidates(
    cur: Any, borrowers: list[_Borrower], cand_cap: int,
) -> dict[str, list[_Candidate]]:
    """For all borrowers, fetch candidates. Chunked into batches of
    ``_CAND_CHUNK`` borrowers to keep each query under any session-level
    timeout and to keep the planner happy with the LATERAL join.

    Strategy: group borrowers by (state_lower, first_word) and unnest into a
    VALUES table, then LATERAL-join to mv_pdl_companies_normalized with the
    per-bucket cap.
    """
    if not borrowers:
        return {}

    out: dict[str, list[_Candidate]] = {}
    n = len(borrowers)
    t_total = time.time()
    n_rows_total = 0
    for chunk_start in range(0, n, _CAND_CHUNK):
        chunk = borrowers[chunk_start: chunk_start + _CAND_CHUNK]
        loan_ids = [b.sba_loan_id for b in chunk]
        programs = [b.program for b in chunk]
        state_lowers = [b.state_lower for b in chunk]
        first_words = [b.first_word for b in chunk]

        sql = f"""
            WITH inp AS (
                SELECT unnest(%s::text[]) AS sba_loan_id,
                       unnest(%s::text[]) AS program,
                       unnest(%s::text[]) AS state_lower,
                       unnest(%s::text[]) AS first_word
            )
            SELECT i.sba_loan_id, i.program, c.pdl_id, c.name,
                   c.locality_lower, c.region_lower, c.industry,
                   c.size_bucket, c.founded_year, c.website
              FROM inp i
              JOIN LATERAL (
                SELECT p.pdl_id, p.name, p.locality_lower, p.region_lower,
                       p.industry, p.size_bucket, p.founded_year, p.website
                  FROM entities.mv_pdl_companies_normalized p
                 WHERE p.region_lower = i.state_lower
                   AND split_part(p.name_normalized, ' ', 1) = i.first_word
                   AND p.name IS NOT NULL AND p.name <> ''
                 ORDER BY length(p.name_normalized) ASC
                 LIMIT {int(cand_cap)}
              ) c ON true
        """
        t0 = time.time()
        cur.execute(sql, (loan_ids, programs, state_lowers, first_words))
        rows = cur.fetchall()
        n_rows_total += len(rows)
        LOG.info(
            "  cand-chunk %d-%d (%d borrowers): %d rows in %.1fs",
            chunk_start, chunk_start + len(chunk), len(chunk),
            len(rows), time.time() - t0,
        )

        for r in rows:
            sba_loan_id, program, pdl_id, pdl_name, locality_lower, region_lower, \
                industry, size_bucket, founded_year, website = r
            text = _compose_pdl_profile(
                name=pdl_name, locality_lower=locality_lower, region_lower=region_lower,
                industry=industry, size_bucket=size_bucket, founded_year=founded_year,
                website=website,
            )
            out.setdefault(sba_loan_id, []).append(_Candidate(
                sba_loan_id=sba_loan_id, program=program, pdl_id=pdl_id,
                pdl_name=pdl_name, profile_text=text,
            ))

    LOG.info(
        "candidate fetch: %d rows total in %.1fs (%d borrowers, %d chunks)",
        n_rows_total, time.time() - t_total, n,
        (n + _CAND_CHUNK - 1) // _CAND_CHUNK,
    )
    return out


def _embed_all_texts(texts: list[str]) -> "list[list[float]]":
    """Embed a flat list of texts via sentence-transformers in one pass.

    Reuses scripts._lib.embedding_emit for the model handle so we don't load
    the model twice in the same process.
    """
    from scripts._lib.embedding_emit import (
        _get_sentence_transformers_model,
        _embed_batch_sentence_transformers,
    )

    model = _get_sentence_transformers_model()
    # Internal batching at 256 is fine for the L6 model; just call once.
    return _embed_batch_sentence_transformers(model, texts)


def _score_and_topk(
    borrower_vecs: "list[list[float]]",
    borrowers: list[_Borrower],
    candidate_lookup: dict[str, list[_Candidate]],
    cand_vec_lookup: dict[tuple[str, str], "list[float]"],
    threshold: float,
    top_k: int,
) -> list[tuple[_Borrower, _Candidate, float]]:
    """Compute cosine sim (dot product since vectors are L2-normalized) for
    each borrower×candidate pair. Returns top-K per borrower with cosine ≥
    threshold."""
    import numpy as np

    out: list[tuple[_Borrower, _Candidate, float]] = []
    for b, bvec in zip(borrowers, borrower_vecs, strict=True):
        cands = candidate_lookup.get(b.sba_loan_id, [])
        if not cands:
            continue
        bnp = np.array(bvec, dtype=np.float32)
        cmat = np.array(
            [cand_vec_lookup[(c.sba_loan_id, c.pdl_id)] for c in cands],
            dtype=np.float32,
        )
        sims = cmat @ bnp  # cosine since both are L2-normalized
        # top-K by similarity, descending.
        idxs = np.argsort(-sims)[: top_k]
        for idx in idxs:
            sim = float(sims[idx])
            if sim < threshold:
                continue
            out.append((b, cands[idx], sim))
    LOG.info("scored matches above threshold %.2f: %d", threshold, len(out))
    return out


def _tier_for(score: float) -> str:
    if score >= TIER_HIGH:
        return "vector_high"
    if score >= TIER_MEDIUM:
        return "vector_medium"
    return "vector_low"


def _bulk_insert_matches(
    cur: Any,
    matches: list[tuple[_Borrower, _Candidate, float]],
    model_version: str,
) -> int:
    """Bulk INSERT scored matches. Idempotent via ON CONFLICT DO UPDATE on
    the (sba_loan_id, pdl_id) primary key — re-running the job refreshes
    scores in place."""
    if not matches:
        return 0

    rows = []
    for b, c, score in matches:
        reasons = ["name_first_word_match", "state_match"]
        if score >= TIER_HIGH:
            reasons.append("high_cosine_sim")
        elif score >= TIER_MEDIUM:
            reasons.append("medium_cosine_sim")
        else:
            reasons.append("low_cosine_sim")
        rows.append((
            b.sba_loan_id, c.pdl_id, b.program, MATCH_PATH,
            round(score, 4), _tier_for(score), reasons, model_version,
        ))

    from psycopg.types.json import Jsonb  # noqa: F401  (kept for parity)
    from psycopg import sql  # noqa: F401

    cur.executemany(
        """
        INSERT INTO entities.pdl_to_sba_borrowers_fuzzy_v1
            (sba_loan_id, pdl_id, program, match_path, match_score,
             confidence_tier, match_reasons, model_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (sba_loan_id, pdl_id) DO UPDATE SET
            program         = EXCLUDED.program,
            match_path      = EXCLUDED.match_path,
            match_score     = EXCLUDED.match_score,
            confidence_tier = EXCLUDED.confidence_tier,
            match_reasons   = EXCLUDED.match_reasons,
            model_version   = EXCLUDED.model_version,
            scored_at       = now()
        """,
        rows,
    )
    return len(rows)


def run_match_job(config: MatchJobConfig) -> dict[str, Any]:
    """Drive the full pipeline. Returns metrics."""
    import psycopg
    from scripts._lib.embedding_emit import EMBEDDING_MODEL  # noqa: PLC0415

    db_url = os.environ["DEX_DB_URL_DIRECT"]

    LOG.info("=" * 60)
    LOG.info("pdl_sba_fuzzy_match v1")
    LOG.info("  match_path:  %s", MATCH_PATH)
    LOG.info("  threshold:   %.2f", config.threshold)
    LOG.info("  top_k:       %d", config.top_k)
    LOG.info("  cand_cap:    %d", config.cand_cap)
    LOG.info("  dry_run:     %s", config.dry_run)
    LOG.info("  model:       %s", EMBEDDING_MODEL)

    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    metrics: dict[str, Any] = {
        "match_path": MATCH_PATH,
        "model_version": EMBEDDING_MODEL,
        "threshold": config.threshold,
        "started_at": started_at.isoformat(),
    }

    with psycopg.connect(db_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Disable Postgres-side statement timeout for this connection —
            # the candidate-gen UNNEST+LATERAL join can scan millions of PDL
            # rows and trips the default role timeout (~5-15min). Direct URL
            # (not pooled) so this stays scoped to this session.
            cur.execute("SET statement_timeout = 0")
            borrowers = _select_unmatched_borrowers(cur, config)
            metrics["borrowers"] = len(borrowers)
            if not borrowers:
                LOG.info("no borrowers to process — done")
                metrics["matches_written"] = 0
                metrics["duration_s"] = round(time.time() - t0, 1)
                return metrics

            cand_lookup = _select_candidates(cur, borrowers, config.cand_cap)
            n_cand = sum(len(v) for v in cand_lookup.values())
            n_with_cands = sum(1 for v in cand_lookup.values() if v)
            metrics["borrowers_with_candidates"] = n_with_cands
            metrics["total_candidates"] = n_cand
            metrics["avg_candidates_per_borrower"] = round(
                n_cand / max(1, n_with_cands), 1,
            )

            if config.dry_run:
                LOG.info("DRY RUN — stopping before embedding")
                metrics["duration_s"] = round(time.time() - t0, 1)
                return metrics

            # Flat list of all texts we need vectors for. Borrower texts first
            # (so we can later split by length), then all unique candidate
            # texts. Unique-by (sba_loan_id, pdl_id) is overkill — pdl_id alone
            # is unique across the candidate set, but the same pdl might
            # appear in multiple borrower buckets and the text is identical,
            # so we dedupe by pdl_id.
            borrower_texts = [b.profile_text for b in borrowers]
            pdl_seen: dict[str, str] = {}  # pdl_id -> profile_text
            for cands in cand_lookup.values():
                for c in cands:
                    pdl_seen.setdefault(c.pdl_id, c.profile_text)

            uniq_pdl_ids = list(pdl_seen.keys())
            uniq_pdl_texts = [pdl_seen[i] for i in uniq_pdl_ids]
            LOG.info("embedding plan: %d borrowers + %d unique PDL profiles "
                     "(out of %d candidate rows)",
                     len(borrower_texts), len(uniq_pdl_texts), n_cand)

            te = time.time()
            all_texts = borrower_texts + uniq_pdl_texts
            all_vecs = _embed_all_texts(all_texts)
            metrics["embed_seconds"] = round(time.time() - te, 1)
            metrics["embedded_total"] = len(all_vecs)

            borrower_vecs = all_vecs[: len(borrower_texts)]
            pdl_vecs = all_vecs[len(borrower_texts):]
            pdl_vec_by_id: dict[str, list[float]] = dict(
                zip(uniq_pdl_ids, pdl_vecs, strict=True),
            )

            # Build the per-(sba_loan_id, pdl_id) vector lookup that
            # _score_and_topk wants.
            cand_vec_lookup: dict[tuple[str, str], list[float]] = {}
            for sba_loan_id, cands in cand_lookup.items():
                for c in cands:
                    cand_vec_lookup[(sba_loan_id, c.pdl_id)] = pdl_vec_by_id[c.pdl_id]

            matches = _score_and_topk(
                borrower_vecs, borrowers, cand_lookup, cand_vec_lookup,
                threshold=config.threshold, top_k=config.top_k,
            )
            metrics["matches_above_threshold"] = len(matches)

            n_written = _bulk_insert_matches(cur, matches, EMBEDDING_MODEL)
            conn.commit()
            metrics["matches_written"] = n_written

            cur.execute(
                "SELECT confidence_tier, COUNT(*) "
                "FROM entities.pdl_to_sba_borrowers_fuzzy_v1 "
                "GROUP BY confidence_tier ORDER BY confidence_tier",
            )
            tier_counts = {tier: cnt for tier, cnt in cur.fetchall()}
            metrics["tier_counts"] = tier_counts

    metrics["duration_s"] = round(time.time() - t0, 1)
    LOG.info("DONE: %s", metrics)
    return metrics


__all__ = [
    "MATCH_PATH",
    "MatchJobConfig",
    "run_match_job",
]
