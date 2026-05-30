#!/usr/bin/env python3
"""Run PDL × SBA fuzzy match v1 — embedding-cosine sibling matcher.

Cycle: hq-all-pdl-sba-fuzzy-match-v1.

This is the top-level entry. The reusable core lives in
``scripts/_lib/pdl_sba_fuzzy_match.py``. The job writes scored matches to
``entities.pdl_to_sba_borrowers_fuzzy_v1`` and is idempotent on rerun (the
PK is (sba_loan_id, pdl_id) and the INSERT uses ON CONFLICT DO UPDATE).

Usage
-----
    # 2026 COMMIT cohort, dry-run (counts only, no embedding):
    cd ~/hq-all && doppler run --project hq-all --config prd -- \\
        uv run --with sentence-transformers --with psycopg[binary] \\
        --with numpy python3 \\
        apps/data-engine-x/scripts/run_pdl_sba_fuzzy_match_emit.py --dry-run

    # Smoke test on 200 borrowers:
    ... run_pdl_sba_fuzzy_match_emit.py --apply --max-borrowers 200

    # Full run (default cohort = 2026 COMMIT):
    ... run_pdl_sba_fuzzy_match_emit.py --apply

    # Custom cohort filter (raw SQL fragment ANDed into the borrower SELECT):
    ... run_pdl_sba_fuzzy_match_emit.py --apply \\
        --cohort-7a "loanstatus IN ('COMMIT','EXEMPT') AND approvaldate >= '2025-01-01'"

Verification
------------
    -- after the run, the canonical lift check is:
    SELECT
      COUNT(DISTINCT l.id) AS total_loans,
      COUNT(DISTINCT m.sba_loan_id) AS deterministic_matches,
      COUNT(DISTINCT f.sba_loan_id) AS fuzzy_matches,
      COUNT(DISTINCT COALESCE(m.sba_loan_id, f.sba_loan_id)) AS union_matches,
      ROUND(100.0 * COUNT(DISTINCT COALESCE(m.sba_loan_id, f.sba_loan_id))
        / NULLIF(COUNT(DISTINCT l.id), 0), 1) AS union_match_pct
    FROM entities.sba_7a_loans l
    LEFT JOIN entities.mv_pdl_to_sba_borrowers m ON m.sba_loan_id = l.id
    LEFT JOIN entities.mv_pdl_to_sba_borrowers_fuzzy_v1 f ON f.sba_loan_id = l.id
    WHERE l.loanstatus = 'COMMIT' AND l.approvaldate >= '2026-01-01';
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.pdl_sba_fuzzy_match import (  # noqa: E402
    DEFAULT_CAND_CAP, DEFAULT_THRESHOLD, DEFAULT_TOP_K,
    MatchJobConfig, run_match_job,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Actually embed + score + write. Default is dry-run "
                        "(prints sizes only).")
    p.add_argument("--dry-run", action="store_true",
                   help="Stop after candidate generation. Counts only, no "
                        "embedding, no write. Overrides --apply.")
    p.add_argument("--cohort-7a", default=None,
                   help="Override the 7a SQL filter fragment. Default is "
                        "'loanstatus=COMMIT AND approvaldate>=2026-01-01'.")
    p.add_argument("--cohort-504", default=None,
                   help="If set, ALSO process 504 with this filter. Default "
                        "skips 504. (Caller responsible for ALL 504-side "
                        "fields being valid.)")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help=f"Cosine threshold (default {DEFAULT_THRESHOLD})")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                   help=f"Top-K matches per borrower (default {DEFAULT_TOP_K})")
    p.add_argument("--cand-cap", type=int, default=DEFAULT_CAND_CAP,
                   help=f"Max candidates per borrower (default {DEFAULT_CAND_CAP})")
    p.add_argument("--max-borrowers", type=int, default=None,
                   help="Cap on number of borrowers (smoke / cost test).")
    args = p.parse_args()

    if not args.apply and not args.dry_run:
        print("ERROR: pass --apply or --dry-run", file=sys.stderr)
        return 64

    config = MatchJobConfig(
        cohort_sql_filter_7a=(
            args.cohort_7a or
            "loanstatus = 'COMMIT' AND approvaldate >= '2026-01-01'"
        ),
        cohort_sql_filter_504=args.cohort_504,
        threshold=args.threshold,
        top_k=args.top_k,
        cand_cap=args.cand_cap,
        dry_run=args.dry_run,
        max_borrowers=args.max_borrowers,
    )

    metrics = run_match_job(config)
    print(f"\nmetrics: {metrics}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
