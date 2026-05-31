"""USAspending API daily contracts Lance rebuild — Modal-hosted append cron.

Topology: modal.Function.map() per-batch fan-out (rewrite 2026-05-25).

The problem this rewrite solves
--------------------------------
USAspending sits behind F5 BIG-IP with active BotDefense (cookies
``BIGipServer~api.usaspending.gov...`` + ``TS01d83a7d=...``; DNS resolves
to 166.123.8.118, owned by Treasury Bureau of the Fiscal Service —
self-hosted F5, not Cloudflare). F5's per-source-IP rate ceiling on
``/awards/{id}/`` is reached at roughly request #303 / 67s of sustained
single-IP traffic. The prior 1-container-async-fan-out topology drove
~7,000 requests from one Modal egress IP and was killed by the F5 stick
table — failure rate >70% at full scale despite five rounds of httpx-
config patches (PR #689 → #694 → #696 → #701).

The solution
------------
Stage 2 fan-out runs via :func:`fetch_award_batch.map` across short-lived
per-batch containers. Each container gets its own egress IP from Modal's
NAT pool (empirically 20 unique IPs across 24 containers at 600-award
scale per the diagnostic probe). F5 sees a distributed swarm of normal-
looking clients instead of one hot IP. Modal's native ``modal.Retries``
re-spawns failed batches on a fresh container (= fresh egress IP), which
naturally bypasses any per-IP throttling that fired in-flight.

Architecture (3-stage pipeline)
--------------------------------
- Stage 1 (orchestrator): ``POST /api/v2/search/spending_by_transaction/``
  paginated for the 24h ``last_modified_date`` window. 42 validator-pinned
  SEARCH_FIELDS. Sister-cron-shaped — low rate, no F5 trigger.
- Stage 2 (workers via ``.map()``): ``GET /api/v2/awards/{generated_unique_award_id}/``,
  BATCH_SIZE awards per worker container, sequential ``httpx.Client`` inside.
  ``modal.Retries(max_retries=3, backoff_coefficient=2.0)`` on the worker
  cycles the container (and its egress IP) on RemoteProtocolError (the
  F5 BotDefense signature).
- Stage 5 (orchestrator): ``lance.write_dataset(mode="append")`` inside
  ``lance_commit_lock("usaspending_contracts")``. Single writer per the
  Lance commit protocol; cannot be parallelized. BTREE indices on
  ``"Recipient UEI"``, ``"generated_internal_id"``, ``"internal_id"``.

Periodic maintenance (Mondays, orchestrator):
``ds.optimize.compact_files()`` + ``ds.cleanup_old_versions(timedelta(days=30))``.

The library functions for Stages 1, 4, and 5 live in
``scripts/run_usaspending_api_daily_contracts_lance_ingest.py``; this app
imports them at orchestrator invocation time.

Why Variant E (``award_json`` as ``pa.string()`` instead of typed struct):
Lance 6.0.0's substrait-converter panics on filter-scan against schemas
with many nested-struct leaves. See the script docstring for the full
substrait-panic rationale.

Schedule: ``0 8 * * *`` (08:00 UTC). Staggered after the existing contracts
delta cron (06:00 UTC) and assistance cron (07:00 UTC). Override via env
``MODAL_USA_LANCE_CRON``.

Secrets required:
- ``dex-db`` — injects ``DEX_DB_URL_POOLED`` (for ledger UPSERTs in
  ``_record_run``) and ``DEX_DB_URL_DIRECT`` (for ``lance_commit_lock``'s
  advisory-lock connection)
- ``bulk-ingest-r2`` — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY

Worker function ``fetch_award_batch`` is pure outbound httpx — no secrets.

Deploy::

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_api_daily_contracts_lance_app.py

Manual backfill::

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_api_daily_contracts_lance_app.py::run_contracts_lance_daily \\
        --target-date=2026-05-24
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-usaspending-api-daily-lance-rebuild")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

# Orchestrator: needs DB (ledger writes + Lance commit lock) + R2 (Lance write).
ORCHESTRATOR_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# Per-batch worker: pure outbound httpx to USAspending. No secrets.
WORKER_SECRETS: list[modal.Secret] = []

ORCHESTRATOR_MEMORY_MB = 2048
ORCHESTRATOR_TIMEOUT_SECONDS = 60 * 60  # 1h
WORKER_MEMORY_MB = 512
WORKER_TIMEOUT_SECONDS = 300  # 5min; one batch should finish in ~15s

# Batch size for the .map() fan-out. 25 awards/batch * ~0.5s/award = ~13s/batch.
# Empirically Variant B with batch_size=25 hit 100% / 17s / 20 unique egress IPs
# at 600-award scale.
BATCH_SIZE = 25

SOURCE_ID = "usaspending_contracts_lance_rebuild"
FEED_NAME = "contracts_lance_api_append"
# 08:00 UTC — after existing contracts delta (06:00) and assistance (07:00).
DEFAULT_CRON = "0 8 * * *"


# --------------------------------------------------------------------------- #
# Per-batch worker — runs in its own short-lived container via .map()
# --------------------------------------------------------------------------- #

# retry-policy: modal-retries-transient
@app.function(
    image=image,
    secrets=WORKER_SECRETS,
    cpu=1.0,
    memory=WORKER_MEMORY_MB,
    timeout=WORKER_TIMEOUT_SECONDS,
    retries=modal.Retries(max_retries=3, backoff_coefficient=2.0),
)
def fetch_award_batch(award_ids: list[str]) -> dict[str, dict]:
    """Fetch ``/api/v2/awards/{id}/`` for up to ``BATCH_SIZE`` awards.

    Runs in a fresh short-lived Modal container per call. Sequential
    ``httpx.Client`` — keeps per-IP request rate well under F5's ceiling
    (~4.5 req/sec measured cliff vs. our natural ~2 req/sec sequential).

    Returns ``{award_id: response_dict}``. Award IDs that returned 404 or
    other non-200 status, or hit a transient httpx error, are silently
    omitted from the returned dict — the orchestrator merges what it gets
    and the missing keys yield ``award_json=null`` in the Lance row.

    F5 BotDefense throttling manifests as ``httpx.RemoteProtocolError``
    (server-closed-mid-stream). We re-raise that one exception type to
    trigger ``modal.Retries`` → fresh container → fresh egress IP, which
    is the F5-bypass mechanism. Any other exception type is treated as a
    transient per-request blip and skipped.
    """
    import httpx

    out: dict[str, dict] = {}
    with httpx.Client(
        headers={"User-Agent": "data-engine-x/1.0"},
        timeout=60.0,
    ) as client:
        for aid in award_ids:
            url = f"https://api.usaspending.gov/api/v2/awards/{aid}/"
            try:
                r = client.get(url)
                if r.status_code == 200:
                    out[aid] = r.json()
                # 404 or other non-200: drop silently
            except httpx.RemoteProtocolError:
                # F5 BotDefense signature — egress IP is being throttled.
                # Raise so modal.Retries cycles the container to a fresh IP.
                raise
            except httpx.HTTPError:
                # Transient per-request error (timeout, connect). Skip and
                # continue; orchestrator will see the award_id missing.
                pass
    return out


# --------------------------------------------------------------------------- #
# Ledger UPSERT + error classification: see `modal/landing/ledger.py`.
# Local helpers were deleted in PR-B of the 2026-05-25 P0 cycle (audit §"P0-2");
# every cron now writes through the canonical `record_run` helper. The
# `landing/` dir is mounted into the orchestrator container at /root/landing
# via the image's `add_local_dir(...)` call above; the import below resolves
# against that mount path at function-runtime.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Orchestrator — runs Stage 1, dispatches Stage 2 via .map(), runs Stages 4–5
# --------------------------------------------------------------------------- #

# retry-policy: no-retry
@app.function(
    image=image,
    secrets=ORCHESTRATOR_SECRETS,
    timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
    memory=ORCHESTRATOR_MEMORY_MB,
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=modal.Cron(os.environ.get("MODAL_USA_LANCE_CRON", DEFAULT_CRON)),
)
def run_contracts_lance_daily(
    target_date: str | None = None,
    max_api_calls: int = 1000,
    dry_run: bool = False,
    force_compact: bool = False,
) -> dict[str, Any]:
    """Daily entry point. Defaults to (yesterday UTC) when called by cron.

    Stage 1 search pagination and Stages 4–5 (assemble + Lance commit-locked
    append) run inside this orchestrator container. Stage 2 award fan-out
    is dispatched via :func:`fetch_award_batch.map`, which spreads the
    workload across Modal's NAT pool and bypasses F5 BotDefense's per-source-
    IP rate ceiling on ``/awards/{id}/``.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    log = logging.getLogger("usaspending_lance_cron")

    feed_date: date
    if target_date:
        feed_date = date.fromisoformat(target_date)
    else:
        feed_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    # Mount the landing/ + scripts/ libs (added to the image above).
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from landing.ledger import (  # noqa: E402
        HeartbeatLoop,
        RunResult,
        classify_exception,
        record_run,
    )
    from run_usaspending_api_daily_contracts_lance_ingest import (  # noqa: E402
        COMPACT_WEEKDAY,
        _s,
        assemble_rows,
        fetch_search_transactions,
        write_lance_append,
    )

    record_run(
        source_id=SOURCE_ID,
        feed_name=FEED_NAME,
        run_id=run_id,
        feed_date=feed_date,
        started_at=started_at,
        completed_at=None,
        result=None,                # pre-record → status=running, outcome=never_ran
        landing_zone="r2",
        payload_format="lance",
        evidence={
            "feed_date": feed_date.isoformat(),
            "trigger": "schedule" if target_date is None else "manual",
            "max_api_calls": max_api_calls,
            "batch_size": BATCH_SIZE,
            "topology": "modal.Function.map_per_batch",
            "dry_run": dry_run,
        },
    )

    try:
        import httpx

        ingested_at = datetime.now(timezone.utc)
        run_compact = force_compact or (
            datetime.now(timezone.utc).weekday() == COMPACT_WEEKDAY
        )

        with HeartbeatLoop(
            cron_app=app.name,
            cron_function="run_contracts_lance_daily",
            run_id=run_id,
        ) as hb:
            hb.set_stage("stage_1_search")
            log.info(
                "stage 1: search target_date=%s dry_run=%s compact=%s",
                feed_date, dry_run, run_compact,
            )

            with httpx.Client(headers={"User-Agent": "data-engine-x/1.0"}) as client:
                transactions = fetch_search_transactions(
                    client=client,
                    target_date=feed_date,
                    max_api_calls=max_api_calls,
                )

            if not transactions:
                log.info("no transactions for %s; nothing to append", feed_date)
                result: dict[str, Any] = {
                    "target_date": feed_date.isoformat(),
                    "rows_written": 0,
                    "transactions_found": 0,
                    "award_fetch_ok": 0,
                    "award_fetch_fail": 0,
                    "batch_errors": 0,
                    "batches_total": 0,
                    "dry_run": dry_run,
                    "compact_ran": False,
                }
            else:
                # Stage 2 — fan-out via modal.Function.map per-batch.
                unique_award_ids = sorted({
                    _s(tx.get("generated_internal_id"))
                    for tx in transactions
                    if tx.get("generated_internal_id")
                })
                batches = [
                    unique_award_ids[i : i + BATCH_SIZE]
                    for i in range(0, len(unique_award_ids), BATCH_SIZE)
                ]
                hb.set_stage("stage_2_fanout", {
                    "batches_total": len(batches),
                    "unique_award_ids": len(unique_award_ids),
                })
                log.info(
                    "stage 2: %d unique award_ids -> %d batches of <= %d via .map()",
                    len(unique_award_ids), len(batches), BATCH_SIZE,
                )

                batch_results = list(
                    fetch_award_batch.map(batches, return_exceptions=True)
                )

                award_details: dict[str, dict] = {}
                batch_errors = 0
                for res in batch_results:
                    if isinstance(res, BaseException):
                        batch_errors += 1
                        continue
                    award_details.update(res)

                award_ok = len(award_details)
                award_fail = len(unique_award_ids) - award_ok
                log.info(
                    "stage 2 done: ok=%d fail=%d batch_errors=%d/%d",
                    award_ok, award_fail, batch_errors, len(batches),
                )

                # Stage 4 — assemble
                hb.set_stage("stage_4_assemble", {
                    "transactions_found": len(transactions),
                    "award_ok": award_ok,
                    "batch_errors": batch_errors,
                })
                merged_rows = assemble_rows(transactions, award_details, ingested_at)
                log.info("stage 4: assembled %d merged rows", len(merged_rows))

                # Stage 5 — Lance append
                hb.set_stage("stage_5_lance_write", {
                    "rows_to_write": len(merged_rows),
                    "run_compact": run_compact,
                })
                rows_written = write_lance_append(
                    merged_rows,
                    run_compact=run_compact,
                    dry_run=dry_run,
                )
                log.info("stage 5: wrote %d rows", rows_written)

                result = {
                    "target_date": feed_date.isoformat(),
                    "rows_written": rows_written,
                    "transactions_found": len(transactions),
                    "award_fetch_ok": award_ok,
                    "award_fetch_fail": award_fail,
                    "batch_errors": batch_errors,
                    "batches_total": len(batches),
                    "dry_run": dry_run,
                    "compact_ran": run_compact and not dry_run,
                }

    except Exception as exc:  # noqa: BLE001
        completed_at = datetime.now(timezone.utc).isoformat()
        duration_seconds = (
            datetime.fromisoformat(completed_at)
            - datetime.fromisoformat(started_at)
        ).total_seconds()
        record_run(
            source_id=SOURCE_ID,
            feed_name=FEED_NAME,
            run_id=run_id,
            feed_date=feed_date,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            result=None,
            error_class=classify_exception(exc),
            error_message=str(exc)[:4000],
            landing_zone="r2",
            payload_format="lance",
            evidence={"feed_date": feed_date.isoformat()},
        )
        raise

    completed_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = (
        datetime.fromisoformat(completed_at)
        - datetime.fromisoformat(started_at)
    ).total_seconds()

    rows_written = int(result.get("rows_written", 0))
    transactions_found = int(result.get("transactions_found", 0))
    batch_errors = int(result.get("batch_errors", 0) or 0)
    batches_total = int(result.get("batches_total", 0) or 0)

    run_result = RunResult(
        rows_loaded=rows_written,
        upstream_probe_returned_data=(transactions_found > 0) if transactions_found is not None else None,
        fanout_total=batches_total,
        fanout_failed=batch_errors,
        is_dry_run=bool(dry_run),
    )
    record_run(
        source_id=SOURCE_ID,
        feed_name=FEED_NAME,
        run_id=run_id,
        feed_date=feed_date,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        result=run_result,
        landing_zone="r2",
        payload_format="lance",
        evidence={
            "feed_date": feed_date.isoformat(),
            "trigger": "schedule" if target_date is None else "manual",
            **{k: v for k, v in result.items() if k not in ("rows_written",)},
        },
    )

    # outcome label (legacy bucket per compute_outcome); kept in the return
    # for backward compat with any caller / log consumer parsing it.
    outcome = "succeeded_with_changes" if rows_written > 0 else "succeeded_with_zero_new_rows"

    return {
        "run_id": run_id,
        "feed_date": feed_date.isoformat(),
        "rows_written": rows_written,
        "transactions_found": result.get("transactions_found"),
        "award_fetch_ok": result.get("award_fetch_ok"),
        "award_fetch_fail": result.get("award_fetch_fail"),
        "batch_errors": result.get("batch_errors"),
        "batches_total": result.get("batches_total"),
        "compact_ran": result.get("compact_ran"),
        "duration_seconds": duration_seconds,
        "outcome": outcome,
    }
