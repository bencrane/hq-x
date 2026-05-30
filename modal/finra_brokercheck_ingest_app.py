"""FINRA BrokerCheck firms ingest — Modal wrapper around the local script.

Why Modal: phase 1 + phase 2 together take ~3-4 hours at 10 RPS over the
public BrokerCheck API. Running locally (laptop sleep / network blips /
process kills) is fragile. Modal gives us a detached cloud sandbox; the
durable stage table (ops.finra_brokercheck_stage_crds) lets phase 2
resume from any partial phase-1 enumeration if a container is reaped.

The actual ingest logic lives in scripts/run_finra_brokercheck_firms_ingest.py.
This file deliberately doesn't reimplement it — it imports run_phase1 /
run_phase2 from the script module and provides Modal scaffolding around them.

Secrets: a named Modal secret 'finra-brokercheck-db' holding DATABASE_URL.
Create once via:

    doppler run -- bash -c 'modal secret create finra-brokercheck-db DATABASE_URL="$DEX_DB_URL_POOLED"'

Why named secret over Secret.from_dict: matches the FMCSA pattern. The
from_dict path was found unreliable for cross-deploy env propagation in
this repo's history (see modal/fmcsa_ingest_app.py L96-99).

Usage:

    # One-shot ingest (phase1 + phase2 in same container; SLOW — single
    # worker runs into FINRA's per-hour rate ceiling after ~1h):
    modal run modal/finra_brokercheck_ingest_app.py --phase all

    # Just phase 2 (resume after phase 1 completed):
    modal run modal/finra_brokercheck_ingest_app.py --phase phase2

    # PARALLEL phase 2 — fan out across N Modal containers, each at a low
    # per-worker RPS. Different containers get different egress IPs from
    # the AWS NAT pool, so per-IP rate limits multiply. With 6 workers at
    # 3 RPS each (18 RPS aggregate), 84k pending CRDs finish in ~80 min.
    # This is the production path:
    modal run --detach modal/finra_brokercheck_ingest_app.py --phase parallel

    # Detached (returns immediately, runs in cloud independent of caller):
    modal run --detach modal/finra_brokercheck_ingest_app.py --phase parallel

Apps still needing to monitor progress can poll:
    SELECT stage_status, count(*)
      FROM ops.finra_brokercheck_stage_crds GROUP BY stage_status;
    SELECT * FROM ops.finra_brokercheck_ingest_runs ORDER BY started_at DESC;
"""

from __future__ import annotations

import os
import sys
from typing import Any

import modal


app = modal.App("data-engine-x-finra-brokercheck-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

# dex-db required for HeartbeatLoop write to ops.cron_heartbeats.
FUNCTION_SECRETS = [
    modal.Secret.from_name("finra-brokercheck-db"),
    modal.Secret.from_name("hqx-db"),
]

# Phase 1 alone is ~2-2.5 h at 10 RPS (recursive prefix drilldown — much
# more search work than the directive estimated). Phase 2 at 10 RPS hits
# FINRA's per-hour rate limit after ~1 h of sustained traffic; effective
# RPS collapses to <0.5 once 429s land. Sustained 3 RPS is the empirical
# safe ceiling for hours-long runs.
#
# Combined cap of 12 h covers an 80-100k CRD phase 2 run at 3 RPS plus a
# fresh phase 1 run with margin.
INGEST_TIMEOUT_SECONDS = 60 * 60 * 12


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the script reads DEX_DB_URL_POOLED.
    Map across so the script can `os.environ.get("DEX_DB_URL_POOLED")` and
    Just Work."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _import_script() -> Any:
    """Import the script module from /root/scripts. Has to happen *inside*
    the Modal function (not at module top level) so the local script files
    are present in the container."""
    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    import importlib

    return importlib.import_module("run_finra_brokercheck_firms_ingest")


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def run_full_ingest(
    rate_limit_rps: float = 10.0,
    prefix: str | None = None,
    max_crds: int | None = None,
) -> dict[str, Any]:
    """Run phase 1 + phase 2 end-to-end in a single Modal container.

    Args:
        rate_limit_rps: Cap on requests per second to api.brokercheck.finra.org.
            Tested safe at 10 RPS sustained; 15 triggers 429s. Default 10.
        prefix: Comma-separated seed prefixes for phase 1 (default a-z + 0-9).
            Use a single letter for smoke testing (e.g. "z" — yields 577 CRDs).
        max_crds: If set, phase 2 stops after fetching N firms (smoke testing).
    """
    _bridge_database_url()
    import httpx
    import psycopg
    import uuid as _uuid

    script = _import_script()

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    seed_prefixes = (
        [p.strip() for p in prefix.split(",") if p.strip()]
        if prefix
        else list(script.DEFAULT_SEED_PREFIX_ALPHABET)
    )
    rate = script.RateLimiter(rate_limit_rps)
    headers = {"User-Agent": script.USER_AGENT, "Accept": "application/json"}

    summary: dict[str, Any] = {
        "rate_limit_rps": rate_limit_rps,
        "seed_prefixes": seed_prefixes,
    }
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_full_ingest",
        run_id=run_id,
    ) as hb:
        with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
            with psycopg.connect(script._database_url()) as conn:
                hb.set_stage("phase1_search", {"prefixes": len(seed_prefixes)})
                print("[finra-modal] phase1 starting")
                unique_crds, drilled, p1_stats = script.run_phase1(
                    conn, client, rate=rate, seed_prefixes=seed_prefixes,
                    dry_run=False,
                )
                summary["phase1"] = {
                    "unique_crds": unique_crds,
                    "prefixes_drilled": drilled,
                    "search_calls": p1_stats.total,
                    "search_4xx": p1_stats.by_4xx,
                    "search_5xx": p1_stats.by_5xx,
                }
                print(f"[finra-modal] phase1 done: unique_crds={unique_crds} "
                      f"prefixes_drilled={drilled} search_calls={p1_stats.total}")

                hb.set_stage("phase2_detail", {"unique_crds": unique_crds})
                print("[finra-modal] phase2 starting")
                inserted, updated, unchanged, failed, p2_stats = script.run_phase2(
                    conn, client, rate=rate, max_crds=max_crds, dry_run=False,
                )
                summary["phase2"] = {
                    "inserted": inserted,
                    "updated": updated,
                    "unchanged": unchanged,
                    "failed": failed,
                    "detail_calls": p2_stats.total,
                    "detail_4xx": p2_stats.by_4xx,
                    "detail_5xx": p2_stats.by_5xx,
                }
                print(f"[finra-modal] phase2 done: ins={inserted} upd={updated} "
                      f"unch={unchanged} fail={failed} calls={p2_stats.total}")

    summary["run_id"] = run_id
    return summary


# Per-worker timeout for parallel phase 2: 3h is plenty for ~14k CRDs at
# 3 RPS (worst-case 78 min) with retry headroom.
WORKER_TIMEOUT_SECONDS = 60 * 60 * 3

# Concurrency cap on the worker function. Modal allows max_containers up to
# the workspace ceiling; 8 is well within fmcsa's pattern.
PARALLEL_PHASE2_MAX_CONTAINERS = 8


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=WORKER_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
    max_containers=PARALLEL_PHASE2_MAX_CONTAINERS,
)
def fetch_crds_batch(
    batch_id: int,
    crds_with_branches: list[tuple[int, int | None]],
    rate_limit_rps: float,
) -> dict[str, Any]:
    """Worker — fetches detail for a slice of CRDs.

    Each Modal container gets a separate egress IP from the AWS NAT pool,
    so per-IP rate limits multiply across workers.
    """
    _bridge_database_url()
    import httpx
    import psycopg
    from datetime import datetime, timezone
    from psycopg.types.json import Jsonb

    script = _import_script()

    rate = script.RateLimiter(rate_limit_rps)
    headers = {"User-Agent": script.USER_AGENT, "Accept": "application/json"}
    stats = script.HttpStats()

    inserted = updated = unchanged = failed = 0
    print(f"[worker batch_id={batch_id}] starting size={len(crds_with_branches)} "
          f"rps={rate_limit_rps}")

    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        with psycopg.connect(script._database_url()) as conn:
            for idx, (crd, branches_from_search) in enumerate(crds_with_branches, start=1):
                content = script.fetch_detail(client, crd, rate=rate, stats=stats)
                if content is None:
                    failed += 1
                    script.mark_stage_crd(conn, crd, status="failed")
                    conn.commit()
                    continue
                row = script._extract_columns(content)
                row["raw_detail_json"] = Jsonb(content)
                row["dataset_fetched_at"] = datetime.now(timezone.utc)
                if row["crd_number"] is None:
                    row["crd_number"] = crd
                if row.get("branches_count") is None and branches_from_search is not None:
                    row["branches_count"] = branches_from_search
                try:
                    outcome = script.upsert_firm(conn, row)
                    if outcome == "inserted":
                        inserted += 1
                    elif outcome == "updated":
                        updated += 1
                    else:
                        unchanged += 1
                    script.mark_stage_crd(conn, crd, status="fetched")
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    failed += 1
                    script.mark_stage_crd(conn, crd, status="failed")
                    conn.commit()
                if idx % 500 == 0:
                    print(f"[worker batch_id={batch_id}] progress idx={idx}/"
                          f"{len(crds_with_branches)} ins={inserted} upd={updated} "
                          f"fail={failed} 4xx={stats.by_4xx} 5xx={stats.by_5xx}")

    print(f"[worker batch_id={batch_id}] done ins={inserted} upd={updated} "
          f"unch={unchanged} fail={failed} calls={stats.total} "
          f"4xx={stats.by_4xx} 5xx={stats.by_5xx}")
    return {
        "batch_id": batch_id,
        "size": len(crds_with_branches),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "failed": failed,
        "calls": stats.total,
        "calls_4xx": stats.by_4xx,
        "calls_5xx": stats.by_5xx,
    }


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def run_phase2_parallel(
    num_workers: int = 6,
    rate_limit_rps_per_worker: float = 3.0,
) -> dict[str, Any]:
    """Orchestrator — fan out phase 2 across num_workers parallel containers.

    Reads pending CRDs from ops.finra_brokercheck_stage_crds, splits them
    into roughly-equal slices, and dispatches a worker per slice via
    starmap. Aggregates results.

    Default 6 workers × 3 RPS = 18 RPS aggregate, well under FINRA's
    sustained ceiling on any single IP. If many workers happen to share
    egress IPs we still come in under per-IP limits.
    """
    _bridge_database_url()
    import psycopg
    import uuid as _uuid

    script = _import_script()

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    print(f"[orchestrator] reading pending CRDs from {script.STAGE_TABLE}")
    with psycopg.connect(script._database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT crd_number, branches_count_from_search "
                f"FROM {script.STAGE_TABLE} "
                f"WHERE stage_status = 'pending' "
                f"ORDER BY crd_number"
            )
            rows = cur.fetchall()
    pending = [
        (int(r[0]), int(r[1]) if r[1] is not None else None) for r in rows
    ]
    print(f"[orchestrator] pending={len(pending)} num_workers={num_workers} "
          f"rps_per_worker={rate_limit_rps_per_worker}")

    if not pending:
        return {"total_pending": 0, "workers_dispatched": 0, "results": []}

    chunk_size = (len(pending) + num_workers - 1) // num_workers
    chunks = [pending[i:i + chunk_size] for i in range(0, len(pending), chunk_size)]
    print(f"[orchestrator] split into {len(chunks)} chunks of ~{chunk_size} each")

    args = [
        (i, chunk, rate_limit_rps_per_worker) for i, chunk in enumerate(chunks)
    ]

    results: list[Any] = []
    totals = {
        "inserted": 0, "updated": 0, "unchanged": 0, "failed": 0,
        "calls": 0, "calls_4xx": 0, "calls_5xx": 0,
    }
    error_count = 0
    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_phase2_parallel",
        run_id=run_id,
    ) as hb:
        hb.set_stage("phase2_fanout", {"workers": len(chunks), "total_pending": len(pending)})
        for r in fetch_crds_batch.starmap(args, return_exceptions=True):
            if isinstance(r, BaseException):
                error_count += 1
                results.append({"error": str(r)[:300]})
                continue
            for k in totals:
                totals[k] += int(r.get(k, 0) or 0)
            results.append(r)

    summary = {
        "run_id": run_id,
        "total_pending": len(pending),
        "workers_dispatched": len(chunks),
        "worker_errors": error_count,
        **totals,
        "per_worker": results,
    }
    print(f"[orchestrator] done: {totals} worker_errors={error_count}")
    return summary


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def run_phase2_only(
    rate_limit_rps: float = 10.0,
    max_crds: int | None = None,
) -> dict[str, Any]:
    """Resume — fetch detail for any pending CRDs in ops.finra_brokercheck_stage_crds
    without re-running phase 1. Useful if phase 1 already populated the
    stage table and a prior phase 2 was killed mid-flight."""
    _bridge_database_url()
    import httpx
    import psycopg

    script = _import_script()

    rate = script.RateLimiter(rate_limit_rps)
    headers = {"User-Agent": script.USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        with psycopg.connect(script._database_url()) as conn:
            inserted, updated, unchanged, failed, stats = script.run_phase2(
                conn, client, rate=rate, max_crds=max_crds, dry_run=False,
            )
    return {
        "rate_limit_rps": rate_limit_rps,
        "phase2": {
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
            "detail_calls": stats.total,
            "detail_4xx": stats.by_4xx,
            "detail_5xx": stats.by_5xx,
        },
    }


@app.local_entrypoint()
def main(
    phase: str = "parallel",
    rate_limit_rps: float = 3.0,
    prefix: str = "",
    max_crds: int = 0,
    num_workers: int = 6,
) -> None:
    """Local entry point — `modal run modal/finra_brokercheck_ingest_app.py`.

    phase:
        - 'parallel' (default): fan-out phase 2 across num_workers containers.
          Use this for production — finishes 80k CRDs in ~80 min.
        - 'all': single-container phase1 + phase2. Phase 1 is OK; phase 2
          hits FINRA's per-hour rate ceiling and slows to a crawl. Avoid.
        - 'phase2': single-container phase 2 only. Same problem as 'all'
          for phase 2.
    """
    max_crds_arg = max_crds if max_crds > 0 else None
    prefix_arg = prefix or None
    if phase == "parallel":
        result = run_phase2_parallel.remote(
            num_workers=num_workers,
            rate_limit_rps_per_worker=rate_limit_rps,
        )
    elif phase == "all":
        result = run_full_ingest.remote(
            rate_limit_rps=rate_limit_rps,
            prefix=prefix_arg,
            max_crds=max_crds_arg,
        )
    elif phase == "phase2":
        result = run_phase2_only.remote(
            rate_limit_rps=rate_limit_rps,
            max_crds=max_crds_arg,
        )
    else:
        raise ValueError(
            f"Unknown phase: {phase!r} (use 'parallel', 'all', or 'phase2')"
        )
    print(result)
