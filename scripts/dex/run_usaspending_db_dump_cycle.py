"""CLI wrapper for the USAspending DB-dump → raw R2 Parquet landing cycle.

Resolves the latest dump URL via the USAspending discovery API, then fires
the Modal job via ``modal run --detach`` (L47 — multi-hour job, detach mandatory).

Usage:
    # Default: resolve latest dump URL, fire Modal job
    python scripts/run_usaspending_db_dump_cycle.py

    # Override dump URL (e.g. for a specific monthly snapshot)
    python scripts/run_usaspending_db_dump_cycle.py --dump-url https://files.usaspending.gov/database_download/usaspending-db_20260506.zip

    # Dry-run: resolve URL and print Modal command without executing
    python scripts/run_usaspending_db_dump_cycle.py --check-only

    # Override URL + dry-run
    python scripts/run_usaspending_db_dump_cycle.py --dump-url <url> --check-only

The ``--check-only`` flag is the verification gate for harness s7: it
confirms the script can resolve the discovery API and build a valid command
without actually dispatching the 4-6h Modal job.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests

# Discovery API — returns .full_download_file.url pointing at the latest
# monthly dump ZIP on files.usaspending.gov (Q4-resolved by audit 2026-05-17).
DISCOVERY_API_URL = (
    "https://api.usaspending.gov/api/v2/bulk_download/list_database_download_files/"
)

# Modal-app path relative to the data-engine-x app root (where Modal must be invoked
# from, so .add_local_dir("modal/landing", ...) and .add_local_dir("scripts", ...) in
# the app file resolve correctly).
MODAL_APP_RELPATH = "modal/usaspending_db_dump_to_r2.py"


def find_dex_app_root() -> Path:
    """Locate apps/data-engine-x/ from this script's path.

    The script lives at apps/data-engine-x/scripts/run_usaspending_db_dump_cycle.py,
    so its parent.parent is the dex app root. This makes the wrapper cwd-independent.
    """
    return Path(__file__).resolve().parent.parent


def resolve_latest_dump_url() -> str:
    """Call the USAspending discovery API and return the latest dump URL."""
    resp = requests.get(DISCOVERY_API_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    url = data["full_download_file"]["url"]
    return url


def build_modal_command(dump_url: str) -> list[str]:
    """Build the ``modal run --detach`` command for the given dump URL.

    Path is relative to the data-engine-x app root (where the subprocess cwd is set
    in main()), so .add_local_dir("modal/landing", ...) resolves correctly.
    """
    return [
        "modal", "run", "--detach",
        f"{MODAL_APP_RELPATH}::run",
        "--dump-url", dump_url,
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fire the USAspending DB-dump → raw R2 Parquet landing via Modal.",
    )
    parser.add_argument(
        "--dump-url",
        default="",
        help=(
            "Override the dump URL (default: resolve from discovery API at "
            f"{DISCOVERY_API_URL})"
        ),
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help=(
            "Resolve the dump URL and print the Modal command without executing. "
            "Exits 0 on success; useful for pre-flight validation."
        ),
    )
    args = parser.parse_args()

    # Resolve dump URL
    if args.dump_url:
        dump_url = args.dump_url
        print(f"[run_usaspending_db_dump_cycle] using override dump URL: {dump_url}")
    else:
        print(f"[run_usaspending_db_dump_cycle] resolving latest dump URL via {DISCOVERY_API_URL}")
        try:
            dump_url = resolve_latest_dump_url()
            print(f"[run_usaspending_db_dump_cycle] resolved dump URL: {dump_url}")
        except Exception as exc:
            print(f"[run_usaspending_db_dump_cycle] ERROR resolving dump URL: {exc}", file=sys.stderr)
            sys.exit(1)

    cmd = build_modal_command(dump_url)
    cmd_str = " ".join(cmd)

    if args.check_only:
        print(f"[run_usaspending_db_dump_cycle] --check-only: resolved URL + Modal command:")
        print(f"  dump_url: {dump_url}")
        print(f"  command:  {cmd_str}")
        sys.exit(0)

    # Fire the Modal job from the data-engine-x app root so .add_local_dir(...)
    # paths in the Modal app resolve correctly regardless of the caller's cwd.
    dex_root = find_dex_app_root()
    print(f"[run_usaspending_db_dump_cycle] dispatching Modal job (--detach, 4-6h wall-clock):")
    print(f"  cwd:     {dex_root}")
    print(f"  command: {cmd_str}")
    result = subprocess.run(cmd, cwd=str(dex_root), check=False)
    if result.returncode != 0:
        print(
            f"[run_usaspending_db_dump_cycle] modal run --detach exited {result.returncode}",
            file=sys.stderr,
        )
        sys.exit(result.returncode)
    print("[run_usaspending_db_dump_cycle] Modal job dispatched. Monitor via `modal app logs`.")


if __name__ == "__main__":
    main()
