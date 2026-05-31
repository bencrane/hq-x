#!/usr/bin/env python3
"""Run the NYC PLUTO + MapPLUTO ingest from the CLI.

Subcommands:

  csv [--version 25v4]
      Phase 1: stream PLUTO attributes from Socrata (64uk-42ks) into
      entities.pluto. If --version is omitted, auto-detects the latest
      published version from Socrata.

  geometry [--version 25v4]
      Phase 2: page through MapPLUTO FeatureServer and update
      entities.pluto.geom for the given version.

  latest
      Detect the latest version, then run csv + geometry sequentially.

Example:
  PYTHONPATH=. doppler run -- python scripts/run_pluto_ingest.py latest
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

from app.services.pluto_ingest import (  # noqa: E402
    _detect_latest_version,
    ingest_pluto_csv,
    update_pluto_geometry,
)


def _app_token() -> str | None:
    return os.environ.get("NYC_OPEN_DATA_APP_TOKEN") or None


def cmd_csv(args: argparse.Namespace) -> int:
    started = time.monotonic()
    result = ingest_pluto_csv(
        pluto_version=args.version,
        app_token=_app_token(),
    )
    print(
        f"csv done: version={result['pluto_version']} "
        f"rows_processed={result['rows_processed']} "
        f"rows_inserted={result['rows_inserted']} "
        f"chunks={result['chunks']} "
        f"duration_s={result['duration_seconds']} "
        f"wall_s={round(time.monotonic() - started, 1)}"
    )
    return 0


def cmd_geometry(args: argparse.Namespace) -> int:
    started = time.monotonic()
    result = update_pluto_geometry(
        pluto_version=args.version,
        start_offset=getattr(args, "start_offset", 0) or 0,
    )
    print(
        f"geometry done: version={result['pluto_version']} "
        f"pages={result['pages']} "
        f"rows_processed={result['rows_processed']} "
        f"rows_updated={result['rows_updated']} "
        f"geometries_repaired={result['geometries_repaired']} "
        f"duration_s={result['duration_seconds']} "
        f"wall_s={round(time.monotonic() - started, 1)}"
    )
    return 0


def cmd_latest(args: argparse.Namespace) -> int:
    version, etag = _detect_latest_version(app_token=_app_token())
    if not version:
        print("ERROR: could not detect latest pluto_version from Socrata", file=sys.stderr)
        return 1
    print(f"latest detected: version={version} truth_last_modified={etag}")

    csv_args = argparse.Namespace(version=version)
    rc = cmd_csv(csv_args)
    if rc != 0:
        return rc

    if args.skip_geometry:
        return 0

    geom_args = argparse.Namespace(version=version)
    return cmd_geometry(geom_args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_csv = sub.add_parser("csv", help="Phase 1: ingest PLUTO attributes from Socrata.")
    p_csv.add_argument("--version", help="PLUTO version (e.g. 25v4); default = autodetect")
    p_csv.set_defaults(func=cmd_csv)

    p_geom = sub.add_parser("geometry", help="Phase 2: update geometry from MapPLUTO FeatureServer.")
    p_geom.add_argument("--version", required=True, help="PLUTO version (e.g. 25v4)")
    p_geom.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Resume page-by-page ingest from this ArcGIS resultOffset (default: 0).",
    )
    p_geom.set_defaults(func=cmd_geometry)

    p_latest = sub.add_parser("latest", help="Detect latest version, then run csv + geometry.")
    p_latest.add_argument(
        "--skip-geometry",
        action="store_true",
        help="Run only phase 1 (csv); skip MapPLUTO geometry phase.",
    )
    p_latest.set_defaults(func=cmd_latest)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
