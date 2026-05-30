"""Precision audit for entities.mv_pdl_to_sam_name_state_matches (§6.5).

Samples 50 rows stratified across at least 5 distinct states (10 per state),
emits them to stdout and a CSV under tmp/pdl_sam_precision_audit_<ts>.csv
for operator review.

Gate (applied externally by the operator after marking SAME/DIFFERENT/AMBIGUOUS):
    - >=47/50 SAME  -> GREEN: commit proceeds
    - 42-46/50 SAME -> YELLOW: surface to user, user decides
    - <42/50 SAME   -> RED: STOP, do not commit

Read-only. Does not modify the MV.

Run under Doppler:

    sudo doppler run --project data-engine-x-api --config prd -- \\
      .venv/bin/python3 scripts/audit_pdl_sam_match_precision.py
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg


SAMPLE_SQL = """
WITH top_states AS (
    SELECT state_lower
    FROM entities.mv_pdl_to_sam_name_state_matches
    WHERE state_lower IS NOT NULL
    GROUP BY state_lower
    ORDER BY COUNT(*) DESC
    LIMIT 5
),
ranked AS (
    SELECT
        m.pdl_id,
        m.sam_uei,
        m.state_lower,
        m.pdl_name,
        m.sam_legal_business_name,
        m.pdl_website,
        m.sam_entity_url,
        m.pdl_locality_lower,
        m.sam_city_lower,
        m.match_score,
        m.match_reasons,
        ROW_NUMBER() OVER (
            PARTITION BY m.state_lower
            ORDER BY random()
        ) AS rn
    FROM entities.mv_pdl_to_sam_name_state_matches m
    JOIN top_states ts ON ts.state_lower = m.state_lower
)
SELECT
    pdl_id, sam_uei, state_lower, pdl_name, sam_legal_business_name,
    pdl_website, sam_entity_url, pdl_locality_lower, sam_city_lower,
    match_score, match_reasons
FROM ranked
WHERE rn <= 10
ORDER BY state_lower, rn
"""


def main() -> int:
    db_url = os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        print("ERROR: DEX_DB_URL_POOLED not set. Run under Doppler.", file=sys.stderr)
        return 2

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    tmp_dir = Path("tmp")
    tmp_dir.mkdir(exist_ok=True)
    csv_path = tmp_dir / f"pdl_sam_precision_audit_{ts}.csv"

    headers = [
        "pdl_id",
        "sam_uei",
        "state_lower",
        "pdl_name",
        "sam_legal_business_name",
        "pdl_website",
        "sam_entity_url",
        "pdl_locality_lower",
        "sam_city_lower",
        "match_score",
        "match_reasons",
        "operator_mark",  # operator fills: SAME / DIFFERENT / AMBIGUOUS
    ]

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SAMPLE_SQL)
            rows = cur.fetchall()

    if len(rows) < 50:
        print(
            f"WARNING: sampled only {len(rows)} rows (<50). "
            f"Top-5 states may have fewer than 10 rows each.",
            file=sys.stderr,
        )

    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for r in rows:
            (
                pdl_id, sam_uei, state_lower, pdl_name, sam_name,
                pdl_website, sam_entity_url, pdl_loc, sam_city,
                score, reasons,
            ) = r
            w.writerow([
                pdl_id,
                sam_uei,
                state_lower,
                pdl_name,
                sam_name,
                pdl_website,
                sam_entity_url,
                pdl_loc,
                sam_city,
                f"{float(score):.2f}",
                ",".join(reasons) if reasons else "",
                "",  # operator_mark left blank
            ])
            print(
                f"{state_lower:>12} | score={float(score):.2f} | "
                f"PDL={pdl_name!r:<60} SAM={sam_name!r:<60} "
                f"PDL_LOC={pdl_loc!r} SAM_CITY={sam_city!r} "
                f"reasons={reasons}"
            )

    print()
    print(f"Wrote {len(rows)} rows to {csv_path}")
    print()
    print(
        "Operator: inspect this CSV. Mark each row SAME / DIFFERENT / "
        "AMBIGUOUS in the operator_mark column. Paste the counts into "
        "the agent-summary."
    )
    print()
    print("Precision gate (apply after marking):")
    print("  >=47/50 SAME  -> GREEN (commit proceeds)")
    print("  42-46/50 SAME -> YELLOW (surface to user, user decides)")
    print("  <42/50 SAME   -> RED (STOP, do not commit)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
