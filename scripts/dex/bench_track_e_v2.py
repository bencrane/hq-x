"""Latency benchmark for DealBridge v2 endpoints (directive §8 Step 8 gate).

Runs three reference workloads 10x each and reports p50/p95/p99 per endpoint.
Gate: p95 < 2.0 s. Invocation (must run under Doppler, read-only):

    doppler run --project data-engine-x-api --config prd -- \
      PYTHONPATH=. python3 scripts/bench_track_e_v2.py
"""
from __future__ import annotations

import os
import statistics
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth.models import SuperAdminContext
from app.main import app
from app.auth import require_flexible_auth

N = 10
GATE_P95 = 2.0


def _quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return s[k]


def _run(name: str, client: TestClient, path: str, body: dict) -> list[float]:
    latencies: list[float] = []
    last_status = None
    for _ in range(N):
        t0 = time.perf_counter()
        r = client.post(path, json=body)
        t1 = time.perf_counter()
        latencies.append(t1 - t0)
        last_status = r.status_code
    p50 = _quantile(latencies, 0.50)
    p95 = _quantile(latencies, 0.95)
    p99 = _quantile(latencies, 0.99)
    mean = statistics.fmean(latencies)
    verdict = "OK" if p95 < GATE_P95 else "FAIL"
    print(
        f"{name:<55} n={N} status={last_status} "
        f"p50={p50:.3f}s p95={p95:.3f}s p99={p99:.3f}s mean={mean:.3f}s  [{verdict}]"
    )
    return latencies


def main() -> int:
    assert os.environ.get("DEX_DB_URL_POOLED"), "DEX_DB_URL_POOLED missing — run under Doppler"
    sa = SuperAdminContext(super_admin_id=uuid4(), email="bench@dealbridge-v2.local")
    app.dependency_overrides[require_flexible_auth] = lambda: sa
    client = TestClient(app)

    all_bad = False

    # Workload A — forward /match, construction in CA
    a = _run(
        "A. /match (NAICS 23, CA, limit=50)",
        client,
        "/api/v2/dealbridge/match",
        {"naics_prefix": "23", "states": ["CA"], "limit": 50},
    )
    all_bad |= _quantile(a, 0.95) >= GATE_P95

    # Workload B — company-first /match/companies, construction in CA
    b = _run(
        "B. /match/companies (NAICS 23, CA, limit=50)",
        client,
        "/api/v2/dealbridge/match/companies",
        {"naics_prefix": "23", "state": "CA", "limit": 50},
    )
    all_bad |= _quantile(b, 0.95) >= GATE_P95

    # Workload C — reverse /lenders/{id}/opportunities for top 7(a) lender
    c = _run(
        "C. /lenders/bank_7a:NORTHEAST BANK/opportunities (limit=50)",
        client,
        "/api/v2/dealbridge/lenders/bank_7a:NORTHEAST BANK/opportunities",
        {"limit": 50, "demand_window_days": 90},
    )
    all_bad |= _quantile(c, 0.95) >= GATE_P95

    print()
    if all_bad:
        print(f"LATENCY GATE FAIL — at least one endpoint p95 >= {GATE_P95}s")
        return 1
    print(f"LATENCY GATE PASS — all endpoints p95 < {GATE_P95}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
