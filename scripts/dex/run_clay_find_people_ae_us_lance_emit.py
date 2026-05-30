"""Entrypoint: Clay Find People AE/US → Lance emit.

Thin wrapper that launches the Modal app. Run with --detach (MANDATORY).

Usage (DETACH IS MANDATORY):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach modal/clay_find_people_ae_us_lance_emit_app.py::run

With explicit snapshot:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach modal/clay_find_people_ae_us_lance_emit_app.py::run \\
      --snapshot 2026-05-17

With full Phase 1 row floor (80,000):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach modal/clay_find_people_ae_us_lance_emit_app.py::run \\
      --snapshot 2026-05-17 --min-rows 80000

See modal/clay_find_people_ae_us_lance_emit_app.py for the full implementation.
Snapshot date precedence:
  1. --snapshot CLI arg.
  2. CLAY_FIND_PEOPLE_SNAPSHOT env var.
  3. Default: SELECT MAX(_snapshot_date) FROM entities.source_clay_find_people.
"""
# This script is intentionally minimal — all logic lives in the Modal app.
# Import it to confirm the app parses cleanly before launching.
import modal  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(
        "Run this via: modal run --detach modal/clay_find_people_ae_us_lance_emit_app.py::run\n"
        "DETACH IS MANDATORY per L47 operational discipline."
    )
