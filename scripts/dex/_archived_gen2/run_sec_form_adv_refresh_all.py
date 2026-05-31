#!/usr/bin/env python3
"""Orchestrator: refresh all four SEC Form ADV feeds for a given month.

Sequential by design (PDF feeds upload to one Storage bucket; serial
keeps the SEC.gov politeness story simple). One run_id is shared across
the four feed runs so you can SELECT … FROM ops.sec_form_adv_ingest_runs
WHERE run_id = … to see the whole orchestration.

This is the cron-ready entrypoint. We chose script-only over a
Trigger.dev task for the initial roll-out — see the post-merge note for
rationale. A future Trigger.dev wrapper can simply shell out to this.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_sec_form_adv_refresh_all.py \\
        --part1-zip-url   https://www.sec.gov/files/adv-filing-data-20111105-20241231-part1.zip \\
        --part2-zip-url   https://www.sec.gov/files/adv-brochures-2024-december.zip \\
        --part2-mapping-url https://www.sec.gov/files/adv-brochure-mapping-20241201-20241231.csv \\
        --part3-zip-url   https://www.sec.gov/files/firm_crs_docs_monthly_20241101_429.zip \\
        --part3-mapping-url https://www.sec.gov/files/firm_crs_monthly_20241101_429.csv \\
        --advw-zip-url    https://www.sec.gov/files/advw-20241201-20241231.zip \\
        --compilation-date 2024-12-01

Any of the per-feed flags can be omitted to skip that feed in this run.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import uuid
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("sec_form_adv_refresh_all")


def run_subscript(args: list[str]) -> int:
    logger.info("invoking", extra={"argv": args})
    return subprocess.call(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compilation-date", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--part1-zip-url")
    parser.add_argument("--part1-zip-path")
    parser.add_argument("--part2-zip-url")
    parser.add_argument("--part2-zip-path")
    parser.add_argument("--part2-mapping-url")
    parser.add_argument("--part2-mapping-path")
    parser.add_argument("--part3-zip-url")
    parser.add_argument("--part3-zip-path")
    parser.add_argument("--part3-mapping-url")
    parser.add_argument("--part3-mapping-path")
    parser.add_argument("--advw-zip-url")
    parser.add_argument("--advw-zip-path")
    parser.add_argument(
        "--skip-on-failure",
        action="store_true",
        help="Continue to the next feed if one fails (default: stop).",
    )
    args = parser.parse_args()

    run_id = args.run_id or str(uuid.uuid4())
    repo_root = Path(__file__).resolve().parent.parent
    py = sys.executable

    failures: list[str] = []

    def invoke(label: str, argv: list[str]) -> None:
        rc = run_subscript(argv)
        if rc != 0:
            logger.error("feed_failed", extra={"label": label, "rc": rc})
            failures.append(label)
            if not args.skip_on_failure:
                raise SystemExit(rc)
        else:
            logger.info("feed_ok", extra={"label": label})

    if args.part1_zip_url or args.part1_zip_path:
        argv = [
            py,
            str(repo_root / "scripts" / "run_sec_form_adv_part1_ingest.py"),
            "--compilation-date", args.compilation_date,
            "--run-id", run_id,
        ]
        if args.part1_zip_path:
            argv += ["--zip-path", args.part1_zip_path]
        else:
            argv += ["--source-url", args.part1_zip_url]
        invoke("part1", argv)

    if args.part2_zip_url or args.part2_zip_path:
        argv = [
            py,
            str(repo_root / "scripts" / "run_sec_form_adv_pdfs_ingest.py"),
            "--part", "2",
            "--compilation-date", args.compilation_date,
            "--run-id", run_id,
        ]
        if args.part2_zip_path:
            argv += ["--zip-path", args.part2_zip_path]
        else:
            argv += ["--zip-url", args.part2_zip_url]
        if args.part2_mapping_path:
            argv += ["--mapping-csv", args.part2_mapping_path]
        elif args.part2_mapping_url:
            argv += ["--mapping-url", args.part2_mapping_url]
        invoke("part2", argv)

    if args.part3_zip_url or args.part3_zip_path:
        argv = [
            py,
            str(repo_root / "scripts" / "run_sec_form_adv_pdfs_ingest.py"),
            "--part", "3",
            "--compilation-date", args.compilation_date,
            "--run-id", run_id,
        ]
        if args.part3_zip_path:
            argv += ["--zip-path", args.part3_zip_path]
        else:
            argv += ["--zip-url", args.part3_zip_url]
        if args.part3_mapping_path:
            argv += ["--mapping-csv", args.part3_mapping_path]
        elif args.part3_mapping_url:
            argv += ["--mapping-url", args.part3_mapping_url]
        invoke("part3", argv)

    if args.advw_zip_url or args.advw_zip_path:
        argv = [
            py,
            str(repo_root / "scripts" / "run_sec_form_adv_w_ingest.py"),
            "--compilation-date", args.compilation_date,
            "--run-id", run_id,
        ]
        if args.advw_zip_path:
            argv += ["--zip-path", args.advw_zip_path]
        else:
            argv += ["--source-url", args.advw_zip_url]
        invoke("adv_w", argv)

    logger.info("orchestration_complete", extra={"run_id": run_id, "failures": failures})
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
