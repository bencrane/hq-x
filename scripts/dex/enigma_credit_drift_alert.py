"""Daily drift detector for Enigma credit accounting.

Compares actual charged credits against declared `max_cost_declared`
over the last 7 days. Output:

- Structured JSON report saved to
  `reports/enigma_credit_drift_YYYY-MM-DD.json`.
- If drift > 20%, a one-line Telegram message via
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env.

Read-only. No writes to the database. Safe to run from a scheduled job.

Drift formula:
    drift = (sum_actual - sum_declared) / sum_declared
where sum_actual = SUM(credits_charged) over the window,
      sum_declared = SUM(catalog[operation_name].max_cost_declared)
                     over the same rows (by graphql_operation_name).

Rows whose `graphql_operation_name` doesn't map to a catalog entry are
counted in `unknown_ops` and their declared cost is treated as 0. They
still contribute to `sum_actual`, so an unknown-op presence inflates
drift — that's intentional (unregistered queries are themselves a bug).

Directive: EXECUTOR_DIRECTIVE_ENIGMA_CREDIT_ACCOUNTING_COMMIT_3.md §5.5.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIFT_THRESHOLD = 0.20


def _fetch_rows(conninfo: str, since: _dt.datetime) -> list[dict[str, Any]]:
    """Fetch (graphql_operation_name, credits_charged) pairs since
    `since`. Requires psycopg; imported lazily so the script can be
    imported for testing without a DB."""
    import psycopg

    rows: list[dict[str, Any]] = []
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT graphql_operation_name, credits_charged, status
            FROM entities.enigma_enrichment_log
            WHERE created_at >= %s
              AND status = 'success'
            """,
            (since,),
        )
        for op_name, credits, status in cur.fetchall():
            rows.append(
                {
                    "graphql_operation_name": op_name,
                    "credits_charged": float(credits or 0),
                    "status": status,
                }
            )
    return rows


def _build_operation_to_catalog_map() -> dict[str, int]:
    """Map GraphQL operation name (as sent in the request body) to the
    declared max. The catalog is keyed by Python constant name, not the
    `operationName` literal inside the GraphQL text — extract the
    operation name from the first line of each query string.
    """
    from app.providers.enigma_adapter.query_catalog import CATALOG

    mapping: dict[str, int] = {}
    for spec in CATALOG.values():
        # First line: `query SomeName(...)` or `mutation Foo(...)`.
        head = spec.query_string.strip().splitlines()[0]
        parts = head.split()
        if len(parts) >= 2 and parts[0] in ("query", "mutation"):
            name = parts[1].split("(")[0]
            mapping[name] = spec.max_cost_declared
    return mapping


def compute_drift(
    rows: list[dict[str, Any]],
    op_to_declared: dict[str, int],
) -> dict[str, Any]:
    sum_actual = 0.0
    sum_declared = 0.0
    unknown_ops: dict[str, int] = {}
    per_op_actual: dict[str, float] = {}
    per_op_declared: dict[str, float] = {}
    for row in rows:
        op = row["graphql_operation_name"]
        actual = float(row["credits_charged"] or 0)
        sum_actual += actual
        per_op_actual[op] = per_op_actual.get(op, 0.0) + actual
        declared = op_to_declared.get(op)
        if declared is None:
            unknown_ops[op] = unknown_ops.get(op, 0) + 1
            continue
        sum_declared += declared
        per_op_declared[op] = per_op_declared.get(op, 0.0) + declared

    if sum_declared <= 0:
        # Either no rows matched the catalog, or all declared values are
        # zero (Free-tier searches only). Drift is undefined; return 0
        # to avoid division-by-zero.
        drift = 0.0
    else:
        drift = (sum_actual - sum_declared) / sum_declared

    return {
        "row_count": len(rows),
        "sum_actual": sum_actual,
        "sum_declared": sum_declared,
        "drift_ratio": drift,
        "threshold": DRIFT_THRESHOLD,
        "exceeds_threshold": drift > DRIFT_THRESHOLD,
        "unknown_ops": unknown_ops,
        "per_op_actual": per_op_actual,
        "per_op_declared": per_op_declared,
    }


def _send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError):
        return False


def build_alert_message(report: dict[str, Any], window_days: int) -> str:
    drift_pct = report["drift_ratio"] * 100
    return (
        f"*Enigma credit drift alert*\n"
        f"Window: last {window_days} days\n"
        f"Actual: {report['sum_actual']:.0f} credits\n"
        f"Declared max: {report['sum_declared']:.0f} credits\n"
        f"Drift: +{drift_pct:.1f}% (threshold {DRIFT_THRESHOLD * 100:.0f}%)\n"
        f"Unknown ops: {len(report['unknown_ops'])}"
    )


def write_report(report: dict[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    path = report_dir / f"enigma_credit_drift_{today}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=7, help="Window size in days. Default 7."
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=REPO_ROOT / "reports",
        help="Where to write the JSON report.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip DB query and Telegram send. Used by tests.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        print("--dry-run: exiting without DB access.")
        return 0

    conninfo = os.environ.get("DEX_DB_URL_POOLED")
    if not conninfo:
        print("DEX_DB_URL_POOLED not set.", file=sys.stderr)
        return 2

    since = _dt.datetime.utcnow() - _dt.timedelta(days=args.days)
    rows = _fetch_rows(conninfo, since)
    op_to_declared = _build_operation_to_catalog_map()
    report = compute_drift(rows, op_to_declared)
    report["window_days"] = args.days
    report["window_since"] = since.isoformat()

    path = write_report(report, args.report_dir)
    print(f"Report written: {path}")

    if report["exceeds_threshold"]:
        message = build_alert_message(report, args.days)
        delivered = _send_telegram(message)
        print(f"Drift exceeds threshold; telegram_delivered={delivered}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
