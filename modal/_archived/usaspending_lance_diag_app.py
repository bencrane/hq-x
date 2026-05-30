"""Throwaway diagnostic Modal app — DOES NOT replace canonical app.

Compares two fan-out topologies for the same USAspending /awards/{id}/
workload, capturing per-request exception classification, DNS/edge-IP info,
and timing distributions to settle the architecture question.

Variant A: one Modal container doing async httpx fan-out internally
           (the current canonical shape).
Variant B: modal.Function.map() — one container per batch of 25 awards,
           sequential httpx within each container.

Both variants use the same target_date and the same SEARCH_FIELDS request to
build a comparable award_id list, so they are running against the same
upstream URLs at roughly the same time.

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_lance_diag_app.py::probe \\
        --target-date=2026-05-20 --max-awards=600
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-usaspending-lance-diag")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates", "dnsutils", "traceroute")
    .run_commands("update-ca-certificates")
    .pip_install("httpx>=0.27", "certifi>=2024.7.4")
)

FUNCTION_SECRETS: list[modal.Secret] = []  # Pure outbound — no DB or R2 needed


# ============================================================================
# Helper: classify exception
# ============================================================================

def _classify(exc: BaseException) -> str:
    """Return a stable bucket label for httpx exceptions."""
    import httpx as _httpx
    if isinstance(exc, _httpx.ConnectError):
        return "ConnectError"
    if isinstance(exc, _httpx.ReadTimeout):
        return "ReadTimeout"
    if isinstance(exc, _httpx.ConnectTimeout):
        return "ConnectTimeout"
    if isinstance(exc, _httpx.RemoteProtocolError):
        return "RemoteProtocolError"
    if isinstance(exc, _httpx.ReadError):
        return "ReadError"
    if isinstance(exc, _httpx.WriteError):
        return "WriteError"
    if isinstance(exc, _httpx.HTTPStatusError):
        return f"HTTPStatusError_{exc.response.status_code}"
    if isinstance(exc, _httpx.HTTPError):
        return f"HTTPError_{type(exc).__name__}"
    return type(exc).__name__


def _capture_dns_and_edge() -> dict[str, Any]:
    """Capture DNS resolution + best-effort edge IP info for api.usaspending.gov."""
    info: dict[str, Any] = {}
    try:
        addrs = socket.getaddrinfo("api.usaspending.gov", 443, socket.AF_INET)
        info["dns_v4"] = sorted({a[4][0] for a in addrs})
    except Exception as e:  # noqa: BLE001
        info["dns_v4_error"] = str(e)
    try:
        addrs6 = socket.getaddrinfo("api.usaspending.gov", 443, socket.AF_INET6)
        info["dns_v6"] = sorted({a[4][0] for a in addrs6})
    except Exception as e:  # noqa: BLE001
        info["dns_v6_error"] = str(e)
    # Capture our outbound IP via httpbin-equivalent
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=10.0) as c:
            r = c.get("https://api.ipify.org?format=json")
            info["egress_ip"] = r.json().get("ip")
    except Exception as e:  # noqa: BLE001
        info["egress_ip_error"] = str(e)
    return info


# ============================================================================
# Stage 1 helper: collect a list of unique generated_internal_id for target_date
# ============================================================================

def _collect_award_ids(target_date_iso: str, max_awards: int) -> list[str]:
    """POST /search/spending_by_transaction/ and gather unique award IDs.

    Uses the canonical 9-field set from run_usaspending_api_daily_ingest.py
    (the working sister cron) — USAspending rejects too-minimal field lists.
    """
    import httpx as _httpx
    url = "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
    fields = [
        "Award ID", "Recipient Name", "Recipient UEI", "Action Date",
        "Transaction Amount", "Awarding Agency", "Mod", "Award Type", "Action Type",
    ]
    seen: set[str] = set()
    page = 1
    with _httpx.Client(
        headers={"User-Agent": "data-engine-x-diag/1.0"},
        timeout=60.0,
    ) as client:
        while len(seen) < max_awards and page <= 60:
            payload = {
                "filters": {
                    "award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{
                        "start_date": target_date_iso,
                        "end_date": target_date_iso,
                        "date_type": "last_modified_date",
                    }],
                },
                "fields": fields,
                "page": page,
                "limit": 100,
                "sort": "Action Date",
                "order": "desc",
            }
            r = client.post(url, json=payload)
            r.raise_for_status()
            body = r.json()
            for row in body.get("results") or []:
                gid = row.get("generated_internal_id")
                if gid:
                    seen.add(gid)
                if len(seen) >= max_awards:
                    break
            if not (body.get("page_metadata") or {}).get("hasNext"):
                break
            page += 1
    return sorted(seen)[:max_awards]


# ============================================================================
# Variant A: one container, internal async fan-out (matches canonical app shape)
# ============================================================================

# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60 * 30,
    memory=2048,
)
def variant_a_internal_async(
    target_date: str,
    max_awards: int,
    concurrency: int,
    batch_size: int,
) -> dict[str, Any]:
    """Single container; mirrors the canonical app's PR #701 shape."""
    import asyncio
    import httpx as _httpx

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    LOG = logging.getLogger("variant_a")

    started = time.time()
    edge_info_before = _capture_dns_and_edge()
    LOG.info("variant_a start; egress=%s dns=%s", edge_info_before.get("egress_ip"), edge_info_before.get("dns_v4"))

    award_ids = _collect_award_ids(target_date, max_awards)
    LOG.info("variant_a collected award_ids=%d (max_awards=%d)", len(award_ids), max_awards)

    per_request: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)

    async def _fetch_one(client, aid: str, req_idx: int) -> dict[str, Any]:
        url = f"https://api.usaspending.gov/api/v2/awards/{aid}/"
        async with sem:
            t0 = time.time()
            try:
                r = await client.get(url, timeout=60)
                elapsed = time.time() - t0
                return {
                    "idx": req_idx, "award_id": aid, "ok": r.status_code == 200,
                    "status": r.status_code, "elapsed_s": round(elapsed, 3),
                    "exc": None, "t_offset_s": round(time.time() - started, 2),
                }
            except Exception as exc:  # noqa: BLE001
                elapsed = time.time() - t0
                return {
                    "idx": req_idx, "award_id": aid, "ok": False, "status": None,
                    "elapsed_s": round(elapsed, 3), "exc": _classify(exc),
                    "t_offset_s": round(time.time() - started, 2),
                }

    async def _drive():
        results: list[dict[str, Any]] = []
        num_batches = (len(award_ids) + batch_size - 1) // batch_size
        for batch_idx in range(num_batches):
            batch = award_ids[batch_idx * batch_size : (batch_idx + 1) * batch_size]
            async with _httpx.AsyncClient(
                headers={"User-Agent": "data-engine-x-diag/1.0"},
                timeout=60,
                limits=_httpx.Limits(max_connections=concurrency, max_keepalive_connections=0, keepalive_expiry=0.0),
                http2=False,
            ) as client:
                tasks = [_fetch_one(client, aid, batch_idx * batch_size + i) for i, aid in enumerate(batch)]
                results.extend(await asyncio.gather(*tasks))
            LOG.info("variant_a batch %d/%d done; t_offset=%.1fs", batch_idx + 1, num_batches, time.time() - started)
            if batch_idx + 1 < num_batches:
                await asyncio.sleep(0.5)
        return results

    per_request = asyncio.run(_drive())
    edge_info_after = _capture_dns_and_edge()

    # Aggregate
    ok = sum(1 for r in per_request if r["ok"])
    exc_buckets: dict[str, int] = {}
    for r in per_request:
        if not r["ok"]:
            exc_buckets[r["exc"] or "no_exc_but_not_ok"] = exc_buckets.get(r["exc"] or "no_exc_but_not_ok", 0) + 1

    duration = time.time() - started
    LOG.info("variant_a done: ok=%d/%d duration=%.1fs exc=%s", ok, len(per_request), duration, exc_buckets)

    # Capture timing-by-position to see if failures concentrate later in the run
    fail_offsets = [r["t_offset_s"] for r in per_request if not r["ok"]]

    return {
        "variant": "A_internal_async",
        "target_date": target_date,
        "max_awards": max_awards,
        "concurrency": concurrency,
        "batch_size": batch_size,
        "award_ids_collected": len(award_ids),
        "ok": ok,
        "fail": len(per_request) - ok,
        "ok_pct": round(100.0 * ok / max(1, len(per_request)), 1),
        "exception_buckets": exc_buckets,
        "duration_s": round(duration, 1),
        "edge_info_before": edge_info_before,
        "edge_info_after": edge_info_after,
        "first_5_fail_offsets_s": sorted(fail_offsets)[:5] if fail_offsets else [],
        "last_5_fail_offsets_s": sorted(fail_offsets, reverse=True)[:5] if fail_offsets else [],
        "per_request_sample": per_request[:5] + per_request[-5:] if len(per_request) >= 10 else per_request,
    }


# ============================================================================
# Variant B: per-batch container via modal.Function.map()
# ============================================================================

# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=600,
    memory=1024,
)
def fetch_batch_isolated(batch: list[str]) -> dict[str, Any]:
    """Sequential httpx in a fresh per-batch container.

    Returns per-request results so the orchestrator can aggregate.
    """
    import httpx as _httpx
    started = time.time()
    edge = _capture_dns_and_edge()
    results: list[dict[str, Any]] = []
    with _httpx.Client(
        headers={"User-Agent": "data-engine-x-diag/1.0"},
        timeout=60,
        http2=False,
    ) as client:
        for i, aid in enumerate(batch):
            url = f"https://api.usaspending.gov/api/v2/awards/{aid}/"
            t0 = time.time()
            try:
                r = client.get(url)
                elapsed = time.time() - t0
                results.append({
                    "idx": i, "award_id": aid, "ok": r.status_code == 200,
                    "status": r.status_code, "elapsed_s": round(elapsed, 3),
                    "exc": None,
                })
            except Exception as exc:  # noqa: BLE001
                elapsed = time.time() - t0
                results.append({
                    "idx": i, "award_id": aid, "ok": False, "status": None,
                    "elapsed_s": round(elapsed, 3), "exc": _classify(exc),
                })
    ok = sum(1 for r in results if r["ok"])
    return {
        "batch_size": len(batch),
        "ok": ok,
        "fail": len(results) - ok,
        "duration_s": round(time.time() - started, 2),
        "egress_ip": edge.get("egress_ip"),
        "dns_v4": edge.get("dns_v4"),
        "per_request": results,
    }


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60 * 30,
    memory=1024,
)
def variant_b_map_fanout(
    target_date: str,
    max_awards: int,
    batch_size: int,
) -> dict[str, Any]:
    """Orchestrator: collect award IDs, fan-out via .map() across containers."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    LOG = logging.getLogger("variant_b")

    started = time.time()
    award_ids = _collect_award_ids(target_date, max_awards)
    LOG.info("variant_b collected award_ids=%d", len(award_ids))

    batches = [award_ids[i : i + batch_size] for i in range(0, len(award_ids), batch_size)]
    LOG.info("variant_b dispatching %d batches of <= %d awards via .map()", len(batches), batch_size)

    container_results = list(fetch_batch_isolated.map(batches, return_exceptions=True))
    duration = time.time() - started

    # Aggregate
    total_ok = 0
    total_fail = 0
    exc_buckets: dict[str, int] = {}
    egress_ips: set[str] = set()
    container_errors = 0
    for r in container_results:
        if isinstance(r, BaseException):
            container_errors += 1
            continue
        total_ok += r["ok"]
        total_fail += r["fail"]
        if r.get("egress_ip"):
            egress_ips.add(r["egress_ip"])
        for req in r.get("per_request", []):
            if not req["ok"]:
                exc_buckets[req["exc"] or "no_exc"] = exc_buckets.get(req["exc"] or "no_exc", 0) + 1

    LOG.info("variant_b done: ok=%d fail=%d duration=%.1fs exc=%s", total_ok, total_fail, duration, exc_buckets)

    return {
        "variant": "B_map_per_batch",
        "target_date": target_date,
        "max_awards": max_awards,
        "batch_size": batch_size,
        "container_count": len(batches),
        "container_errors": container_errors,
        "award_ids_collected": len(award_ids),
        "ok": total_ok,
        "fail": total_fail,
        "ok_pct": round(100.0 * total_ok / max(1, total_ok + total_fail), 1),
        "exception_buckets": exc_buckets,
        "duration_s": round(duration, 1),
        "egress_ip_count": len(egress_ips),
        "egress_ip_sample": sorted(egress_ips)[:5],
    }


# ============================================================================
# Single-shot test: fresh container, sustained sequential fan-out
# ============================================================================

# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60 * 40,
    memory=2048,
)
def fresh_container_sustained(
    target_date: str,
    max_awards: int,
    concurrency: int = 4,
) -> dict[str, Any]:
    """One fresh Modal container, async httpx, NO batched-client (raw config).

    Tests: does ANY long-lived container fail at sustained volume, regardless
    of batched-client mitigation?
    """
    import asyncio
    import httpx as _httpx

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    LOG = logging.getLogger("fresh_sustained")

    started = time.time()
    edge_before = _capture_dns_and_edge()
    LOG.info("fresh_sustained start; egress=%s", edge_before.get("egress_ip"))

    award_ids = _collect_award_ids(target_date, max_awards)
    LOG.info("collected award_ids=%d", len(award_ids))

    sem = asyncio.Semaphore(concurrency)
    per_request: list[dict[str, Any]] = []

    async def _fetch(client, aid, idx):
        url = f"https://api.usaspending.gov/api/v2/awards/{aid}/"
        async with sem:
            t0 = time.time()
            try:
                r = await client.get(url, timeout=60)
                return {"idx": idx, "ok": r.status_code == 200, "status": r.status_code, "exc": None, "t_offset_s": round(time.time() - started, 1)}
            except Exception as exc:  # noqa: BLE001
                return {"idx": idx, "ok": False, "status": None, "exc": _classify(exc), "t_offset_s": round(time.time() - started, 1)}

    async def _drive():
        # SINGLE long-lived AsyncClient (NOT batched per PR #701)
        limits = _httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=0,
            keepalive_expiry=0.0,
        )
        async with _httpx.AsyncClient(
            headers={"User-Agent": "data-engine-x-diag/1.0"},
            timeout=60,
            limits=limits,
            http2=False,
        ) as client:
            tasks = [_fetch(client, aid, i) for i, aid in enumerate(award_ids)]
            return await asyncio.gather(*tasks)

    per_request = asyncio.run(_drive())
    duration = time.time() - started
    edge_after = _capture_dns_and_edge()

    ok = sum(1 for r in per_request if r["ok"])
    exc_buckets: dict[str, int] = {}
    for r in per_request:
        if not r["ok"]:
            exc_buckets[r["exc"] or "no_exc"] = exc_buckets.get(r["exc"] or "no_exc", 0) + 1

    # Detect: when do failures START? (helps distinguish "always fails" from "after N seconds")
    fail_offsets = sorted([r["t_offset_s"] for r in per_request if not r["ok"]])
    fail_indices = sorted([r["idx"] for r in per_request if not r["ok"]])

    LOG.info("fresh_sustained: ok=%d/%d duration=%.1fs exc=%s", ok, len(per_request), duration, exc_buckets)

    return {
        "variant": "fresh_sustained_single_client",
        "target_date": target_date,
        "max_awards": max_awards,
        "concurrency": concurrency,
        "award_ids_collected": len(award_ids),
        "ok": ok,
        "fail": len(per_request) - ok,
        "ok_pct": round(100.0 * ok / max(1, len(per_request)), 1),
        "exception_buckets": exc_buckets,
        "duration_s": round(duration, 1),
        "edge_info_before": edge_before,
        "edge_info_after": edge_after,
        "first_failure_t_s": fail_offsets[0] if fail_offsets else None,
        "first_failure_idx": fail_indices[0] if fail_indices else None,
        "last_failure_t_s": fail_offsets[-1] if fail_offsets else None,
    }


@app.local_entrypoint()
def probe(target_date: str = "2026-05-20", max_awards: int = 600):
    """Run all three variants serially and print comparison."""
    print(f"=== diag run target_date={target_date} max_awards={max_awards} ===")

    print("\n--- variant A (canonical: internal async + batched client) ---")
    a = variant_a_internal_async.remote(target_date, max_awards, 4, 50)
    print(json.dumps(a, indent=2, default=str))

    print("\n--- variant B (modal.Function.map per-batch containers) ---")
    b = variant_b_map_fanout.remote(target_date, max_awards, 25)
    print(json.dumps(b, indent=2, default=str))

    print("\n--- fresh_container_sustained (single long-lived client) ---")
    f = fresh_container_sustained.remote(target_date, max_awards, 4)
    print(json.dumps(f, indent=2, default=str))

    print("\n=== SUMMARY ===")
    summary = [
        {"variant": a["variant"], "ok_pct": a["ok_pct"], "duration_s": a["duration_s"], "exc": a["exception_buckets"]},
        {"variant": b["variant"], "ok_pct": b["ok_pct"], "duration_s": b["duration_s"], "exc": b["exception_buckets"]},
        {"variant": f["variant"], "ok_pct": f["ok_pct"], "duration_s": f["duration_s"], "exc": f["exception_buckets"]},
    ]
    print(json.dumps(summary, indent=2, default=str))
