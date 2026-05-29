"""GTM-signal parity gate — differential oracle: legacy /preview vs new compiled→/compute.

THE EXECUTABLE DEFINITION OF DONE FOR THE GTM-SIGNALS PR-4 CUTOVER.

This is NOT a unit test. It hits live DEX prod + R2 and runs the real
DuckDB-over-Lance scan (~40s per signal for the 365-day window). Run it manually
or from the PR-4 deploy verifier — never from the default pytest collection:

    cd apps/hq-x && doppler run --project hq-all --config prd -- \
        uv run python -m scripts.gtm_signal_parity_check            # all active signals
    cd apps/hq-x && doppler run --project hq-all --config prd -- \
        uv run python -m scripts.gtm_signal_parity_check usaspending_net_new_100k

Exit 0 iff EVERY active signal's new compiled→/compute cohort matches the legacy
/preview cohort across all parity dimensions AND lands within the hq-x→DEX client
latency budget. Non-zero with a per-dimension diff otherwise.

The new leg is built from primitives that already exist on `main` — the hq-x
compiler (PR-2) and DEX /api/internal/signals/compute (PR-3) — so this gate runs
and reports RED *before* any PR-4 caller code is written. Each RED dimension maps
to a specific cutover defect; PR-4 is done when this gate goes GREEN.

Dimensions:
  matched_count        WHERE + SAM INNER JOIN parity (D1)  — pre-cap counts must match
  keyset               dispatched row key NAMES (D3)       — uei vs recipient_uei, award_type vs type_description
  value_types          per-column value TYPES (D3)         — federal_action_obligation DOUBLE vs raw text
  ordering+membership  top-N sort + sample set (D2)        — must be numeric-DESC, not lexical ("9000" > "100000")
  latency<=budget      hq-x→DEX 30s client ceiling (N1)    — wide-window compute must fit (net_new_100k ~37s today)

Sources: generalized criteria comes from hq-x business.gtm_signals (the new system
of record). The legacy leg lets DEX compile its own ops.gtm_signals criteria via
/preview. allowed_columns for the compiler is read from the live Lance schema —
the same set DEX re-validates against at execute time — so a column the criteria
references but the dataset lacks fails loudly here.

NOTE on the cron's schema source: this gate opens Lance directly for allowed_columns.
The CRON must NOT depend on a live gtm-mcp fetch (see D4) — the decision is to add a
`validate_identifiers=False` compile mode for cron runs and trust DEX execute-time
re-validation. See docs/gtm-signals-cutover-parity-gate.md.

Both legs use a long client timeout so the gate can MEASURE compute that exceeds the
production 30s budget; the latency dimension is what asserts that budget.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

# hq-x compiler — single source of truth for "what is a signal" (PR-2, pure Python).
from app.services.gtm_signal_compiler import CompileError, compile_criteria

R2_BASE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/"
DEX_CLIENT_BUDGET_S = 30.0      # dex_client._DEFAULT_TIMEOUT — the N1 ceiling the cron inherits
SAMPLE_N = 500                  # rows pulled per leg; enough for keyset/types/ordering, cheap over HTTP
HTTP_TIMEOUT_S = 240.0          # long enough to MEASURE compute past the 30s budget (not the gate)
# Transaction-grain key present (unrenamed) in BOTH legs → robust to the D3 key drift.
ORDER_KEY_COLS = ("generated_unique_award_id", "modification_number", "action_date")


# ── R2 / Lance schema (allowed_columns source) ──────────────────────────────

def _storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "auto",
        "aws_virtual_hosted_style_request": "false",
    }


def _dataset_uri(dotted: str) -> str:
    ns, name = dotted.split(".", 1)
    if not name.endswith("_lance"):
        name = f"{name}_lance"
    return f"{R2_BASE_URI}{ns}/{name}"


def _schema_names(dotted: str) -> set[str]:
    import lance  # heavy import; only when a signal needs schema resolution
    ds = lance.dataset(_dataset_uri(dotted), storage_options=_storage_options())
    return set(ds.schema.names)


# ── DEX HTTP legs ───────────────────────────────────────────────────────────

def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and len(payload) == 1:
        return payload["data"]
    return payload


def _dex_post(path: str, body: dict[str, Any], *, token: str, base: str) -> tuple[Any, float]:
    t0 = time.perf_counter()
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        resp = client.post(
            f"{base}{path}", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    elapsed = time.perf_counter() - t0
    resp.raise_for_status()
    return _unwrap(resp.json()), elapsed


def _legacy_leg(slug: str, *, token: str, base: str) -> tuple[dict[str, Any], float]:
    """DEX /preview — the live legacy path (gtm_signal_cohort.fetch_cohort_rows)."""
    return _dex_post(
        f"/api/v1/gtm/signals/{slug}/preview", {"limit": SAMPLE_N}, token=token, base=base
    )


def _new_leg(
    criteria: dict[str, Any], *, token: str, base: str,
    allowed: set[str], allowed_join: set[str] | None,
) -> tuple[dict[str, Any], float, dict[str, Any]]:
    """Compile (hq-x) → DEX /compute — the path PR-4 ships. Forwards the FULL compiled
    shape (join/select/order_by/scan_filter); dropping any of them is defect D1."""
    c = compile_criteria(
        criteria,
        now=datetime.now(timezone.utc),
        allowed_columns=allowed,
        allowed_join_columns=allowed_join,
    )
    body = {
        "spine_target": c.spine_target,
        "where_sql": c.where_sql,
        "bindings": c.bindings,
        # D1-extended: execute_cohort narrows the SPINE COLUMN scan only when
        # project_columns is set — `select` alone does NOT bound it. The compiler
        # emits `select` but not `project_columns`; omitting it scans all ~100 FPDS
        # columns for the full window (measured: 365-day /compute >240s vs 37s for the
        # 11-col /preview). A correct PR-4 caller must pass project_columns (or the
        # executor must default the scan projection to select∪order∪scan∪join-key).
        "project_columns": c.select,
        "select": c.select,
        "join": c.join,
        "order_by": c.order_by,
        "scan_filter": c.scan_filter,
        "max_rows": SAMPLE_N,
        "count_only": False,
    }
    data, elapsed = _dex_post("/api/internal/signals/compute", body, token=token, base=base)
    return data, elapsed, body


# ── Differential comparison ─────────────────────────────────────────────────

def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _compare(
    legacy: dict[str, Any], new: dict[str, Any], *, new_wall: float, legacy_wall: float,
) -> list[tuple[str, bool, str]]:
    lrows = legacy.get("rows") or []
    nrows = new.get("rows") or []
    out: list[tuple[str, bool, str]] = []

    # 1. matched_count — WHERE + SAM INNER JOIN parity (D1)
    lc, nc = legacy.get("matched_count"), new.get("matched_count")
    out.append(("matched_count", lc == nc, f"legacy={lc!r} new={nc!r}"))

    lkeys = set(lrows[0]) if lrows else set()
    nkeys = set(nrows[0]) if nrows else set()

    # 2. keyset — dispatched row key names (D3)
    out.append((
        "keyset",
        bool(lkeys) and lkeys == nkeys,
        f"only_legacy={sorted(lkeys - nkeys)} only_new={sorted(nkeys - lkeys)}",
    ))

    # 3. value_types — per-column JSON value type on shared keys (D3)
    shared = sorted(lkeys & nkeys)
    type_mismatch: list[str] = []
    if lrows and nrows:
        for k in shared:
            lt, nt = _jtype(lrows[0][k]), _jtype(nrows[0][k])
            if lt != nt:
                type_mismatch.append(f"{k}: legacy={lt} new={nt}")
    out.append(("value_types", not type_mismatch, "; ".join(type_mismatch) or "ok"))

    # 4. ordering + sample membership (D2) — robust to D3 renames via shared key cols
    keycols = [c for c in ORDER_KEY_COLS if c in (lkeys & nkeys)] or shared
    lseq = [tuple(str(r.get(c)) for c in keycols) for r in lrows]
    nseq = [tuple(str(r.get(c)) for c in keycols) for r in nrows]
    n = min(len(lseq), len(nseq))
    first_div = next((i for i in range(n) if lseq[i] != nseq[i]), None)
    same = lseq == nseq
    detail = (
        f"identical over {n} rows"
        if same
        else f"diverges at row {first_div}/{n} (keycols={keycols})"
    )
    out.append(("ordering+membership", same, detail))

    # 5. latency <= budget — the N1 ceiling the cron inherits from dex_client
    out.append((
        "latency<=budget",
        new_wall <= DEX_CLIENT_BUDGET_S,
        f"new={new_wall:.1f}s legacy={legacy_wall:.1f}s budget={DEX_CLIENT_BUDGET_S:.0f}s "
        f"server_compute_ms={new.get('sql_elapsed_ms')}",
    ))
    return out


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    base = os.environ["DEX_BASE_URL"].rstrip("/")
    token = os.environ["DEX_SERVICE_TOKEN"]
    db_url = os.environ.get("HQX_DB_URL_POOLED") or os.environ["HQX_DB_URL_DIRECT"]
    only = sys.argv[1] if len(sys.argv) > 1 else None

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT signal_slug, criteria FROM business.gtm_signals "
                "WHERE is_active ORDER BY signal_slug"
            )
            signals = cur.fetchall()

    if only:
        signals = [s for s in signals if s["signal_slug"] == only]
    if not signals:
        print("no matching active signals in business.gtm_signals", flush=True)
        return 1

    gate_ok = True
    for sig in signals:
        slug, criteria = sig["signal_slug"], sig["criteria"]
        print(f"\n=== {slug} ===", flush=True)
        try:
            allowed = _schema_names(criteria["spine_target"])
            allowed_join = (
                _schema_names(criteria["join"]["dataset"]) if criteria.get("join") else None
            )
        except Exception as exc:  # noqa: BLE001 — surface as a gate failure, not a crash
            print(f"  [FAIL] schema_fetch          {type(exc).__name__}: {exc}", flush=True)
            gate_ok = False
            continue

        try:
            legacy, legacy_wall = _legacy_leg(slug, token=token, base=base)
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] legacy_preview        {type(exc).__name__}: {exc}", flush=True)
            gate_ok = False
            continue

        try:
            new, new_wall, _ = _new_leg(
                criteria, token=token, base=base, allowed=allowed, allowed_join=allowed_join
            )
        except CompileError as exc:
            print(f"  [FAIL] compile               {exc}", flush=True)
            gate_ok = False
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] new_compute           {type(exc).__name__}: {exc}", flush=True)
            gate_ok = False
            continue

        for dim, ok, detail in _compare(
            legacy, new, new_wall=new_wall, legacy_wall=legacy_wall
        ):
            gate_ok = gate_ok and ok
            print(f"  [{'PASS' if ok else 'FAIL'}] {dim:20s} {detail}", flush=True)

    print(
        f"\n{'GATE GREEN — parity reached' if gate_ok else 'GATE RED — PR-4 fixes required'}",
        flush=True,
    )
    return 0 if gate_ok else 1


if __name__ == "__main__":
    sys.exit(main())
