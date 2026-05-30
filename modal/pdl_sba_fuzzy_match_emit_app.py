"""PDL × SBA fuzzy match v1 — Modal wrapper for the fuzzy match emit job.

Cycle: hq-all-pdl-sba-fuzzy-match-v1.

This is the Modal-resident sibling of the local-runnable entry
``apps/data-engine-x/scripts/run_pdl_sba_fuzzy_match_emit.py``. The job
shape is identical: pull unmatched borrowers from the configured cohort
filter, generate candidate PDL companies via state + first-word block,
embed both sides via sentence-transformers, score cosine, and write
top-K matches to ``entities.pdl_to_sba_borrowers_fuzzy_v1``.

v1 ships with the lib + local runner pre-validated; the Modal wrapper is
provided for the eventual daily refresh (post-v1 cron wiring is in the
follow-up cycle). Until then, this app can be invoked manually:

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/pdl_sba_fuzzy_match_emit_app.py::emit

Secrets required (Modal):
    dex-db                — DEX_DB_URL_DIRECT for the candidate-gen
                                     SELECT + INSERT to entities.pdl_to_sba_borrowers_fuzzy_v1.

Schedule: not wired in v1. Follow-up cycle will attach a cron.

Cost: ~$0.50 per full run on the 2026 COMMIT cohort (sentence-transformers
on Modal CPU; no OpenAI calls).
"""
from __future__ import annotations

import logging
import os
import sys

import modal

app = modal.App("data-engine-x-pdl-sba-fuzzy-match-emit")

# sentence-transformers/all-MiniLM-L6-v2 on CPU is dominated by the
# embedding pass; 4 GiB memory is enough for ~100K vectors at 384-dim
# (~150 MB) plus the borrower/candidate buffers. 2 h timeout covers the
# 2026 COMMIT cohort (7,500 borrowers × 80 candidates avg = ~600K rows,
# but only ~100K unique PDL profiles get embedded due to dedup).
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "psycopg[binary]",
        "sentence-transformers>=2.7,<4.0",
        "numpy",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
]

EMIT_MEMORY_MB = 4096
EMIT_TIMEOUT_SECONDS = 60 * 60 * 2

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the lib reads DEX_DB_URL_DIRECT."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=EMIT_MEMORY_MB,
    timeout=EMIT_TIMEOUT_SECONDS,
)
def emit(
    cohort_7a: str | None = None,
    cohort_504: str | None = None,
    threshold: float | None = None,
    top_k: int | None = None,
    cand_cap: int | None = None,
    max_borrowers: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Run the fuzzy match emit job inside Modal.

    Args are forwarded to ``MatchJobConfig``. ``None`` means "use the
    lib's default".
    """
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )
    _bridge_database_url()
    os.environ.setdefault("EMBEDDING_PROVIDER", "sentence-transformers")

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts._lib.pdl_sba_fuzzy_match import (
        DEFAULT_CAND_CAP, DEFAULT_THRESHOLD, DEFAULT_TOP_K,
        MatchJobConfig, run_match_job,
    )

    config = MatchJobConfig(
        cohort_sql_filter_7a=(
            cohort_7a or "loanstatus = 'COMMIT' AND approvaldate >= '2026-01-01'"
        ),
        cohort_sql_filter_504=cohort_504,
        threshold=DEFAULT_THRESHOLD if threshold is None else float(threshold),
        top_k=DEFAULT_TOP_K if top_k is None else int(top_k),
        cand_cap=DEFAULT_CAND_CAP if cand_cap is None else int(cand_cap),
        max_borrowers=max_borrowers,
        dry_run=dry_run,
    )
    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit",
        run_id=run_id,
    ) as hb:
        hb.set_stage("fuzzy_match_emit", {"dry_run": dry_run})
        metrics = run_match_job(config)
    logger.info("modal emit done: %s", metrics)
    metrics["run_id"] = run_id
    return metrics


@app.local_entrypoint()
def main(
    cohort_7a: str | None = None,
    cohort_504: str | None = None,
    threshold: float | None = None,
    top_k: int | None = None,
    cand_cap: int | None = None,
    max_borrowers: int | None = None,
    dry_run: bool = False,
) -> None:
    metrics = emit.remote(
        cohort_7a=cohort_7a, cohort_504=cohort_504,
        threshold=threshold, top_k=top_k, cand_cap=cand_cap,
        max_borrowers=max_borrowers, dry_run=dry_run,
    )
    print(f"metrics: {metrics}")
