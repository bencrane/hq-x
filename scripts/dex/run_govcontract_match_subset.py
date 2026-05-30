#!/usr/bin/env python3
"""Subset run: contractor → open opportunity matcher (local-model, one-shot).

Validation harness for the matching-engine relationship A. Picks N
contractors from the $100k+ 2026-action_date cohort, builds per-UEI profile
text from full contract history (Pattern C lookup on
usaspending.contracts_lance), loads actionable open opps from
sam_gov.opps_active_lance, embeds both sides with a local Sentence
Transformer model, re-ranks the top-N via a local cross-encoder, prints
top-K matches per contractor and writes a JSONL file for later inspection.

Usage:
    cd apps/data-engine-x
    doppler run --project hq-all --config prd -- uv run python \\
        scripts/run_govcontract_match_subset.py \\
        --n-contractors 10 --top-k 10 --embedder stella

Model choices:
    --embedder stella  → dunzhang/stella_en_1.5B_v5     (default; ~3GB RAM)
    --embedder qwen    → Alibaba-NLP/gte-Qwen2-7B-instruct  (~15GB RAM; top-tier)

Output:
    Pretty-printed top-K per contractor to stdout + JSONL at --output.

First run downloads the chosen embedder + the reranker
(BAAI/bge-reranker-v2-m3) from Hugging Face Hub. Subsequent runs hit the
local HF cache and skip the download.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import lance
import pyarrow.compute as pc


CONTRACTS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
)
OPPS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/opps_active_lance"
)

EMBEDDERS = {
    # NOTE: --embedder stella was the intended default per directive
    # 2026-05-15-govcontract-match-subset-validation but Stella's vendored
    # modeling_qwen.py uses the legacy `past_key_values.get_usable_length()`
    # API removed in newer transformers DynamicCache → AttributeError on
    # first encode pass against sentence-transformers 4.x. Validator
    # prediction p1 fix 3 sanctions the BAAI/bge-large-en-v1.5 fallback
    # (Apache 2.0, ~335M params, plain SBERT API, no trust_remote_code).
    # Re-evaluate Stella when transformers / Stella's vendored code
    # converge; --embedder qwen remains the heavyweight flag-flip.
    "stella": "BAAI/bge-large-en-v1.5",
    "qwen": "Alibaba-NLP/gte-Qwen2-7B-instruct",
}
RERANKER = "BAAI/bge-reranker-v2-m3"

# Actionable notice types — drop Award Notices (already closed), Justifications,
# etc. These are the types where a contractor can still respond / bid.
ACTIONABLE_NOTICE_TYPES = (
    "Solicitation",
    "Combined Synopsis/Solicitation",
    "Presolicitation",
    "Sources Sought",
    "Special Notice",
)


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def pick_contractor_cohort(n: int, seed: int) -> list[str]:
    """Return N distinct UEIs of contractors who won $100k+ in 2026.

    Pulls action_date >= 2026-01-01 + federal_action_obligation >= 100_000
    from contracts_lance via PyLance scanner, dedupes by UEI in DuckDB,
    then random-samples N. Sorts by total obligated descending first so
    --seed reproducibility holds across runs.
    """
    storage = _r2_storage_options()
    ds = lance.dataset(CONTRACTS_URI, storage_options=storage)
    print(f"  scanning contracts_lance for 2026 $100k+ winners ...", file=sys.stderr)
    # Lance columns are string-typed; date comparison works lexicographically on
    # ISO-8601 strings, but the obligation amount-filter must happen in DuckDB
    # with TRY_CAST (Substrait can't compare string-vs-float scalar).
    cohort_rows = ds.scanner(
        columns=["recipient_uei", "federal_action_obligation", "action_date"],
        filter=pc.field("action_date") >= pc.scalar("2026-01-01"),
    ).to_table()
    con = duckdb.connect()
    con.register("rows", cohort_rows)
    # Use .fetch_arrow_table().to_pylist() instead of .df() to avoid pandas dependency.
    cohort_rows_out = con.execute(
        """
        SELECT
            recipient_uei,
            count(*) AS contract_count,
            sum(TRY_CAST(federal_action_obligation AS DOUBLE)) AS total_obligated_2026
        FROM rows
        WHERE recipient_uei IS NOT NULL
        GROUP BY recipient_uei
        HAVING sum(TRY_CAST(federal_action_obligation AS DOUBLE)) >= 100000
        ORDER BY total_obligated_2026 DESC
        """
    ).fetch_arrow_table().to_pylist()
    print(
        f"  cohort: {len(cohort_rows_out):,} distinct UEIs won $100k+ in 2026",
        file=sys.stderr,
    )
    ueis_all = [r["recipient_uei"] for r in cohort_rows_out]
    if n >= len(ueis_all):
        return ueis_all
    import random
    rng = random.Random(seed)
    return rng.sample(ueis_all, n)


def build_contractor_profile_text(uei: str) -> tuple[str, str, dict[str, Any]]:
    """For one UEI, query contracts_lance for full history → profile text.

    Returns (recipient_name, profile_text, scalar_attrs).
    """
    storage = _r2_storage_options()
    ds = lance.dataset(CONTRACTS_URI, storage_options=storage)
    tbl = ds.scanner(
        columns=[
            "recipient_uei",
            "recipient_name",
            "action_date",
            "federal_action_obligation",
            "naics_code",
            "naics_description",
            "product_or_service_code",
            "product_or_service_code_description",
            "awarding_agency_name",
            "awarding_sub_agency_name",
            "type_of_set_aside",
            "transaction_description",
        ],
        filter=pc.field("recipient_uei") == pc.scalar(uei),
    ).to_table()

    con = duckdb.connect()
    con.register("c", tbl)
    name = con.execute("SELECT any_value(recipient_name) FROM c").fetchone()[0]
    # All Lance columns are string-typed; cast obligation amounts in SQL.
    summary = con.execute(
        """
        SELECT
            count(*) AS contract_count,
            sum(TRY_CAST(federal_action_obligation AS DOUBLE)) AS total_obligated,
            min(action_date) AS earliest,
            max(action_date) AS latest
        FROM c
        """
    ).fetchone()
    top_naics = con.execute(
        """
        SELECT naics_code, any_value(naics_description) AS naics_desc,
               count(*) AS n, sum(TRY_CAST(federal_action_obligation AS DOUBLE)) AS total
        FROM c WHERE naics_code IS NOT NULL
        GROUP BY naics_code
        ORDER BY total DESC NULLS LAST
        LIMIT 5
        """
    ).fetchall()
    top_agencies = con.execute(
        """
        SELECT awarding_agency_name, count(*) AS n,
               sum(TRY_CAST(federal_action_obligation AS DOUBLE)) AS total
        FROM c WHERE awarding_agency_name IS NOT NULL
        GROUP BY awarding_agency_name
        ORDER BY total DESC NULLS LAST
        LIMIT 5
        """
    ).fetchall()
    top_pscs = con.execute(
        """
        SELECT product_or_service_code,
               any_value(product_or_service_code_description) AS desc,
               count(*) AS n
        FROM c WHERE product_or_service_code IS NOT NULL
        GROUP BY product_or_service_code
        ORDER BY n DESC
        LIMIT 5
        """
    ).fetchall()
    top_set_asides = con.execute(
        """
        SELECT type_of_set_aside, count(*) AS n
        FROM c
        WHERE type_of_set_aside IS NOT NULL AND type_of_set_aside <> ''
        GROUP BY type_of_set_aside ORDER BY n DESC LIMIT 5
        """
    ).fetchall()
    recent_descriptions = con.execute(
        """
        SELECT DISTINCT transaction_description
        FROM c
        WHERE transaction_description IS NOT NULL
          AND length(transaction_description) > 20
        ORDER BY transaction_description
        LIMIT 5
        """
    ).fetchall()

    contract_count = int(summary[0] or 0)
    total_obligated = float(summary[1] or 0.0)
    earliest = summary[2]
    latest = summary[3]

    lines = [
        f"Federal contractor: {name or '(unknown)'}",
        f"Recipient UEI: {uei}",
        f"Federal contract history: {contract_count} contracts, "
        f"${total_obligated:,.0f} total obligated, from {earliest} to {latest}.",
        "",
        "Top NAICS by dollars:",
    ]
    for code, desc, n_, total in top_naics:
        lines.append(f"  - NAICS {code} ({desc}): {n_} contracts, ${total or 0:,.0f}")
    lines.append("")
    lines.append("Top awarding agencies:")
    for ag, n_, total in top_agencies:
        lines.append(f"  - {ag}: {n_} contracts, ${total or 0:,.0f}")
    if top_pscs:
        lines.append("")
        lines.append("Top product/service categories (PSC):")
        for code, desc, n_ in top_pscs:
            lines.append(f"  - PSC {code} ({desc}): {n_} contracts")
    if top_set_asides:
        lines.append("")
        lines.append("Set-aside types previously won:")
        for sa, n_ in top_set_asides:
            lines.append(f"  - {sa}: {n_} contracts")
    if recent_descriptions:
        lines.append("")
        lines.append("Sample of contract descriptions:")
        for (desc,) in recent_descriptions:
            lines.append(f"  - {(desc or '')[:240]}")

    return (
        name or f"(unknown UEI {uei})",
        "\n".join(lines),
        {
            "uei": uei,
            "name": name,
            "contract_count": contract_count,
            "total_obligated_lifetime": total_obligated,
            "top_naics_codes": [n[0] for n in top_naics],
            "top_agencies": [a[0] for a in top_agencies],
            "set_asides_won": [s[0] for s in top_set_asides],
        },
    )


def load_actionable_opps() -> tuple[list[dict[str, Any]], list[str]]:
    """Load active opps with future deadlines + actionable notice types.

    Returns (opp_metadata_list, opp_text_list) aligned on index.
    """
    storage = _r2_storage_options()
    ds = lance.dataset(OPPS_URI, storage_options=storage)
    tbl = ds.scanner(
        columns=[
            "notice_id",
            "title",
            "description",
            "naics_code",
            "set_aside_code",
            "set_aside",
            "department_agency",
            "sub_tier",
            "office",
            "response_deadline",
            "notice_type",
            "pop_state",
            "primary_contact_email",
            "primary_contact_fullname",
            "link",
            "active_flag",
        ],
        filter=pc.field("active_flag") == pc.scalar("Yes"),
    ).to_table()

    con = duckdb.connect()
    con.register("o", tbl)
    today_iso = date.today().isoformat()
    placeholders = ", ".join(["?"] * len(ACTIONABLE_NOTICE_TYPES))
    opp_meta = con.execute(
        f"""
        SELECT * FROM o
        WHERE notice_type IN ({placeholders})
          AND (response_deadline IS NULL OR response_deadline >= ?)
          AND description IS NOT NULL
          AND length(description) > 100
        """,
        [*ACTIONABLE_NOTICE_TYPES, today_iso],
    ).fetch_arrow_table().to_pylist()
    opp_texts: list[str] = []
    for r in opp_meta:
        parts: list[str] = [f"Federal opportunity: {r.get('title') or ''}"]
        if r.get("naics_code"):
            parts.append(f"NAICS code: {r['naics_code']}")
        agency_chain = " / ".join(
            x for x in [r.get("department_agency"), r.get("sub_tier"), r.get("office")] if x
        )
        if agency_chain:
            parts.append(f"Awarding agency: {agency_chain}")
        if r.get("set_aside"):
            parts.append(f"Set-aside: {r['set_aside']}")
        if r.get("notice_type"):
            parts.append(f"Notice type: {r['notice_type']}")
        if r.get("response_deadline"):
            parts.append(f"Response deadline: {r['response_deadline']}")
        if r.get("pop_state"):
            parts.append(f"Place of performance state: {r['pop_state']}")
        if r.get("description"):
            parts.append("")
            parts.append("Description:")
            parts.append((r["description"] or "")[:4000])
        opp_texts.append("\n".join(parts))
    return opp_meta, opp_texts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-contractors", type=int, default=10)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument(
        "--rerank-pool",
        type=int,
        default=50,
        help="bi-encoder ANN top-N pool fed into the cross-encoder reranker",
    )
    ap.add_argument(
        "--embedder",
        choices=list(EMBEDDERS.keys()),
        default="stella",
        help="stella=1.5B (default, fast), qwen=7B (top-tier)",
    )
    ap.add_argument("--output", default="/tmp/govcontract_matches.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    print("=" * 70)
    print(f"Subset run: N={args.n_contractors} contractors × top-K={args.top_k} opps")
    print(f"  embedder: {EMBEDDERS[args.embedder]}")
    print(f"  reranker: {RERANKER}")
    print(f"  rerank pool: {args.rerank_pool}")
    print(f"  output:   {args.output}")
    print("=" * 70)

    print("\n[1/5] Picking contractor cohort ...")
    ueis = pick_contractor_cohort(args.n_contractors, seed=args.seed)
    print(f"      → {len(ueis)} UEIs sampled")

    print("\n[2/5] Building contractor profile text ...")
    contractor_records: list[dict[str, Any]] = []
    for uei in ueis:
        name, text, attrs = build_contractor_profile_text(uei)
        contractor_records.append(
            {"uei": uei, "name": name, "text": text, "attrs": attrs}
        )
        print(
            f"      ✓ {name} ({uei}) — "
            f"{attrs['contract_count']} contracts, ${attrs['total_obligated_lifetime']:,.0f}"
        )

    print("\n[3/5] Loading actionable opps from sam_gov.opps_active_lance ...")
    opp_meta, opp_texts = load_actionable_opps()
    print(f"      → {len(opp_meta):,} actionable opps")

    print(f"\n[4/5] Loading model {EMBEDDERS[args.embedder]} (first run downloads weights) ...")
    # Import locally so script can `--help` without sentence-transformers installed.
    from sentence_transformers import CrossEncoder, SentenceTransformer
    import numpy as np

    embedder = SentenceTransformer(
        EMBEDDERS[args.embedder], trust_remote_code=True
    )
    # Cap max sequence length to 512 tokens (~2k chars). 18K opps × 4000-char
    # raw text otherwise causes MPS encode rate to degrade from 1.3 it/s to
    # >2 s/it (validator prediction p2). The cross-encoder reranker carries
    # the long-context burden later; the bi-encoder needs only coarse
    # similarity.
    embedder.max_seq_length = 512
    print(f"      embedding opps (batch_size=8, max_seq_length=512) ...")
    opp_vecs = embedder.encode(
        opp_texts,
        batch_size=4 if args.embedder == "qwen" else 8,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    print("      embedding contractor profiles ...")
    contractor_vecs = embedder.encode(
        [c["text"] for c in contractor_records],
        batch_size=4 if args.embedder == "qwen" else 8,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )

    print(f"\n[5/5] Loading reranker {RERANKER} + ranking ...")
    reranker = CrossEncoder(RERANKER, trust_remote_code=True)

    out_rows: list[dict[str, Any]] = []
    for ci, contractor in enumerate(contractor_records):
        # Cosine via dot product (vectors are L2-normalized).
        sims = contractor_vecs[ci] @ opp_vecs.T
        ann_idx = np.argsort(-sims)[: args.rerank_pool]
        pairs = [(contractor["text"], opp_texts[i]) for i in ann_idx]
        rerank_scores = reranker.predict(
            pairs, batch_size=8, show_progress_bar=False
        )
        ranked = sorted(
            zip(ann_idx.tolist(), rerank_scores.tolist(), sims[ann_idx].tolist()),
            key=lambda x: -x[1],
        )[: args.top_k]

        print()
        print("─" * 70)
        print(f"### {contractor['name']} ({contractor['uei']})")
        attrs = contractor["attrs"]
        print(f"    NAICS focus: {', '.join(attrs['top_naics_codes'][:3]) or '(none)'}")
        print(f"    Top agencies: {', '.join(attrs['top_agencies'][:3]) or '(none)'}")
        print(
            f"    Lifetime: {attrs['contract_count']} contracts, "
            f"${attrs['total_obligated_lifetime']:,.0f}"
        )
        for rank, (oi, r_score, ann_score) in enumerate(ranked, 1):
            opp = opp_meta[oi]
            print(
                f"  {rank:>2}. [rerank={r_score:>+.3f} cos={ann_score:.3f}] "
                f"{opp.get('notice_id','?')}"
            )
            title = (opp.get("title") or "")[:140]
            print(f"       {title}")
            print(
                f"       NAICS {opp.get('naics_code','?')}  |  "
                f"{opp.get('department_agency','?')} / "
                f"{opp.get('sub_tier','?')}  |  "
                f"set-aside: {opp.get('set_aside') or 'full and open'}"
            )
            print(
                f"       deadline: {opp.get('response_deadline','?')}  |  "
                f"{opp.get('link','')}"
            )
            out_rows.append(
                {
                    "contractor_uei": contractor["uei"],
                    "contractor_name": contractor["name"],
                    "rank": rank,
                    "opp_notice_id": opp.get("notice_id"),
                    "opp_title": opp.get("title"),
                    "opp_naics": opp.get("naics_code"),
                    "opp_agency": opp.get("department_agency"),
                    "opp_sub_tier": opp.get("sub_tier"),
                    "opp_set_aside": opp.get("set_aside"),
                    "opp_deadline": opp.get("response_deadline"),
                    "opp_link": opp.get("link"),
                    "rerank_score": float(r_score),
                    "ann_cosine_score": float(ann_score),
                    "embedder": EMBEDDERS[args.embedder],
                    "reranker": RERANKER,
                }
            )

    print()
    print("=" * 70)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for row in out_rows:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote {len(out_rows)} match rows → {out_path}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
