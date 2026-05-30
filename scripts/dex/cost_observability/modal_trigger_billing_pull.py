#!/usr/bin/env python3
"""Cost-observability pull for Modal + Trigger.dev (operator-runnable).

LIMITATIONS (per validator pre-flight 2026-05-13):
  - Modal CLI v1.4.1 has no `modal app stats` subcommand. We use `modal app history`
    to count recent invocations as a cost proxy; multiply by EST_PER_RUN_USD per app.
  - Trigger.dev v3 has no public billing API. We link the dashboard URL only.

This script does NOT alert. It prints structured data the operator inspects.
No cron — invoke on demand:

    doppler run --project hq-all --config prd -- bash -c \\
        'python apps/data-engine-x/scripts/cost_observability/modal_trigger_billing_pull.py'

Output: Markdown table on stdout; structured JSON on /tmp (not committed).

To get authoritative numbers, open:
  - Modal dashboard:        https://modal.com/apps
  - Trigger.dev dashboard:  https://cloud.trigger.dev (per CLAUDE.md §"Deploy targets")
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Estimated per-run cost in USD. These are rough — the operator should
# calibrate via Modal/Trigger.dev dashboards. Modal pricing varies by
# CPU/GPU + memory + duration; this is a placeholder constant per app.
EST_PER_RUN_USD: dict[str, float] = {
    # FMCSA pipeline crons (data-engine-x).
    "data-engine-x-fmcsa-factory-daily": 0.40,        # ~30min, 16GB, sequential
    "data-engine-x-fmcsa-daily-verify": 0.01,         # <30s, 2GB
    "data-engine-x-fmcsa-weekly-coverage": 0.01,      # <30s, 2GB
    "data-engine-x-material-change-cron": 0.08,       # 2-5min, 4GB
    "data-engine-x-fmcsa-ingest": 0.30,               # 15min cron
    # Lance emit family.
    "data-engine-x-fmcsa-carrier-essentials-lance-emit": 0.20,
    "data-engine-x-fmcsa-carrier-essentials-embedding-emit": 0.50,
}

# Apps to track explicitly (covers daily/weekly cost surface for FMCSA).
TRACKED_APPS = list(EST_PER_RUN_USD.keys())

# Trigger.dev dashboard URL (no public billing API).
TRIGGER_DEV_DASHBOARD = "https://cloud.trigger.dev"


def _modal_app_history(app_name: str, since: datetime) -> int:
    """Count Modal app deployments in `modal app history`.

    NOTE: This is `app history` (deployment history) — not run/invocation history.
    Modal does not expose run history via CLI. For invocation count, the operator
    must check the Modal dashboard. This count proxies cron-fire-frequency by
    counting deployments; for crons that are 1 deploy + N scheduled runs, this
    UNDERCOUNTS — adjust by manually checking dashboard for actual invocation
    count (the operator's calibration step).
    """
    try:
        result = subprocess.run(
            ["modal", "app", "history", app_name, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        print(f"WARN: modal CLI not found (skipping {app_name})", file=sys.stderr)
        return 0

    if result.returncode != 0:
        # App may not exist yet — that's OK for newly-added apps.
        return 0

    try:
        history = json.loads(result.stdout)
    except json.JSONDecodeError:
        return 0

    if not isinstance(history, list):
        return 0

    count = 0
    for entry in history:
        # Modal returns ISO timestamps; safe-parse and filter by since.
        ts_raw = entry.get("created_at") or entry.get("createdAt") or entry.get("date")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts >= since:
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Window in days for invocation/deployment-count proxy (default 30)",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="Path to write structured JSON (default: /tmp/...)",
    )
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"# Cost observability pull (window: last {args.days} days; since {since.isoformat()})")
    print()
    print(
        "**Caveats:** Modal CLI v1.4.1 has no `modal app stats`. Trigger.dev v3 has no "
        "public billing API. The Modal counts below come from `modal app history` "
        "(deployment count, NOT invocation count); use the Modal dashboard for the "
        "definitive number."
    )
    print()
    print("## Modal apps")
    print()
    print("| app | deployments_in_window | est_per_run_usd | est_window_cost_proxy_usd |")
    print("|-----|----------------------|-----------------|---------------------------|")

    rows: list[dict[str, object]] = []
    for app in TRACKED_APPS:
        count = _modal_app_history(app, since)
        per_run = EST_PER_RUN_USD.get(app, 0.0)
        est = round(count * per_run, 2)
        print(f"| `{app}` | {count} | ${per_run:.2f} | ${est:.2f} |")
        rows.append(
            {
                "app": app,
                "deployments_in_window": count,
                "est_per_run_usd": per_run,
                "est_window_cost_proxy_usd": est,
            }
        )

    print()
    print("## Trigger.dev")
    print()
    print(f"No public billing API. Open the dashboard: {TRIGGER_DEV_DASHBOARD}")
    print()
    print("## Modal dashboard")
    print()
    print("For authoritative invocation count + actual spend, open: https://modal.com/apps")

    # Persist structured output to /tmp for piping or follow-on diff.
    out_path = args.json_out or os.path.join(
        tempfile.gettempdir(),
        f"modal_trigger_billing_pull_{int(datetime.now(timezone.utc).timestamp())}.json",
    )
    with open(out_path, "w") as fh:
        json.dump(
            {
                "window_days": args.days,
                "since": since.isoformat(),
                "modal_apps": rows,
                "trigger_dashboard": TRIGGER_DEV_DASHBOARD,
                "modal_dashboard": "https://modal.com/apps",
            },
            fh,
            indent=2,
        )
    print()
    print(f"Structured JSON → {out_path}")


if __name__ == "__main__":
    main()
