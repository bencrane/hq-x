"""Precision audit for entities.mv_pdl_to_usaspending_contract_recipients (§6.5).

Samples 50 rows stratified across at least 5 distinct recipient_state_code values
(10 per state), restricted to the `name_state_direct` fallback path (which is
the new surface Sprint 3 introduces — the `uei_sam` path already has Sprint 2's
47/50 audit). Emits to stdout and a CSV under
tmp/pdl_federal_winners_precision_audit_<ts>.csv for operator review.

Joins `entities.mv_usaspending_entity_grain_slim` inline to surface
recipient_name + recipient_state_code alongside the PDL side.

Gate (applied externally after the operator marks SAME / DIFFERENT / AMBIGUOUS):
    - >=42/50 SAME  -> GREEN:  commit proceeds
    - 35-41/50 SAME -> YELLOW: surface to user, recommend endpoint default
                               `match_paths=['uei_sam']`, do NOT block commit
    - <35/50 SAME   -> RED:    STOP, do not commit

Read-only. Does not modify the MV.

Run under Doppler:

    doppler run --project data-engine-x-api --config prd -- \\
      .venv/bin/python3 scripts/audit_pdl_federal_winners_precision.py
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg


SAMPLE_SQL = """
WITH joined AS (
    SELECT
        m.recipient_uei,
        s.recipient_name,
        s.recipient_state_code,
        m.pdl_id,
        m.pdl_name,
        m.pdl_website,
        m.pdl_linkedin_url,
        m.match_score,
        m.match_path,
        m.match_reasons
    FROM entities.mv_pdl_to_usaspending_contract_recipients m
    JOIN entities.mv_usaspending_entity_grain_slim s
      ON s.recipient_uei = m.recipient_uei
    WHERE m.match_path = 'name_state_direct'
      AND s.recipient_state_code IS NOT NULL
      AND s.recipient_state_code <> ''
),
top_states AS (
    SELECT recipient_state_code
    FROM joined
    GROUP BY recipient_state_code
    ORDER BY COUNT(*) DESC
    LIMIT 5
),
ranked AS (
    SELECT
        j.*,
        ROW_NUMBER() OVER (
            PARTITION BY j.recipient_state_code
            ORDER BY random()
        ) AS rn
    FROM joined j
    JOIN top_states ts ON ts.recipient_state_code = j.recipient_state_code
)
SELECT
    recipient_uei,
    recipient_name,
    recipient_state_code,
    pdl_id,
    pdl_name,
    pdl_website,
    pdl_linkedin_url,
    match_score,
    match_path,
    match_reasons
FROM ranked
WHERE rn <= 10
ORDER BY recipient_state_code, rn
"""


def main() -> int:
    db_url = os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        print("ERROR: DEX_DB_URL_POOLED not set. Run under Doppler.", file=sys.stderr)
        return 2

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    tmp_dir = Path("tmp")
    tmp_dir.mkdir(exist_ok=True)
    csv_path = tmp_dir / f"pdl_federal_winners_precision_audit_{ts}.csv"

    headers = [
        "recipient_uei",
        "recipient_name",
        "recipient_state_code",
        "pdl_id",
        "pdl_name",
        "pdl_website",
        "pdl_linkedin_url",
        "match_score",
        "match_path",
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
            f"Top-5 states may have fewer than 10 name_state_direct rows each. "
            f"Adjust gate ratios proportionally and flag in agent summary.",
            file=sys.stderr,
        )

    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for r in rows:
            (
                recipient_uei, recipient_name, state, pdl_id, pdl_name,
                pdl_website, pdl_linkedin_url, score, path, reasons,
            ) = r
            reasons_str = ",".join(reasons) if reasons else ""
            w.writerow([
                recipient_uei,
                recipient_name,
                state,
                pdl_id,
                pdl_name,
                pdl_website or "",
                pdl_linkedin_url or "",
                f"{float(score):.2f}",
                path,
                reasons_str,
                "",  # operator_mark left blank
            ])
            print(
                f"{state:>4} | score={float(score):.2f} | "
                f"USA={recipient_name!r:<55} "
                f"PDL={pdl_name!r:<55} "
                f"web={pdl_website!r} li={pdl_linkedin_url!r} "
                f"reasons={reasons_str}"
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
    print("  >=42/50 SAME  -> GREEN  (commit proceeds)")
    print("  35-41/50 SAME -> YELLOW (surface to user, recommend "
          "match_paths=['uei_sam'] default)")
    print("  <35/50 SAME   -> RED    (STOP, do not commit)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
