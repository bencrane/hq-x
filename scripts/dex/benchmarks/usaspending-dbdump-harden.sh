#!/usr/bin/env bash
#
# Acceptance-test harness for the USAspending DB-dump → raw R2 Parquet landing.
#
# Directive: Desktop/hq/directives/2026-05-20-usaspending-dbdump-harden.md
#
# Runs the acceptance suite in tests/scripts/test_usaspending_dbdump_harden.py.
# All tests must pass for the cycle to be COMPLETE.
#
# Usage (from anywhere in the repo):
#
#   bash apps/data-engine-x/scripts/benchmarks/usaspending-dbdump-harden.sh
#
# With Doppler (for production ledger URL):
#
#   doppler run --project hq-all --config prd -- bash -c \
#     'apps/data-engine-x/scripts/benchmarks/usaspending-dbdump-harden.sh'
#
# Reads DEX_DB_URL_DIRECT from env if present; falls back to local /tmp socket.
# Parquet lands to a local stand-in for R2 (no R2 access required).
# Must run in <5 min on fixtures.
#
# Exit code 0 on all-pass, 1 otherwise.

set -euo pipefail

# --------------------------------------------------------------------------- #
# Resolve repo root and change to data-engine-x
# --------------------------------------------------------------------------- #

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && git rev-parse --show-toplevel)"
DEX_DIR="$REPO_ROOT/apps/data-engine-x"

cd "$DEX_DIR"

# --------------------------------------------------------------------------- #
# Source dex.sh helper (per CLAUDE.md §"Helper library")
# --------------------------------------------------------------------------- #

source "$DEX_DIR/scripts/_lib/dex.sh" 2>/dev/null || true

# --------------------------------------------------------------------------- #
# Verify fixtures exist
# --------------------------------------------------------------------------- #

FIXTURES_DIR="$DEX_DIR/tests/fixtures/usaspending_dbdump"

if [[ ! -d "$FIXTURES_DIR/complete" ]]; then
  echo "ERROR: complete fixture not found at $FIXTURES_DIR/complete" >&2
  exit 1
fi

if [[ ! -f "$FIXTURES_DIR/complete/toc.dat" ]]; then
  echo "ERROR: complete fixture missing toc.dat — not a valid pg_dump -Fd directory" >&2
  exit 1
fi

if [[ ! -d "$FIXTURES_DIR/incomplete" ]]; then
  echo "ERROR: incomplete fixture not found at $FIXTURES_DIR/incomplete" >&2
  exit 1
fi

echo "fixtures OK: complete=$(du -sh $FIXTURES_DIR/complete | cut -f1), incomplete=$(du -sh $FIXTURES_DIR/incomplete | cut -f1)"

# --------------------------------------------------------------------------- #
# Verify pg_restore is on PATH (required by the test module)
# --------------------------------------------------------------------------- #

if ! command -v pg_restore &>/dev/null; then
  echo "ERROR: pg_restore not found on PATH" >&2
  exit 1
fi

echo "pg_restore: $(pg_restore --version)"

# --------------------------------------------------------------------------- #
# Run the 6-test acceptance suite
# --------------------------------------------------------------------------- #

echo "--- running acceptance suite ---"

# Run only the test module under test (not the full suite — C1 does that separately)
# Use run-silent.sh for clean output: pass collapses to ✓, fail dumps output
SILENT="$REPO_ROOT/scripts/run-silent.sh"

if [[ -x "$SILENT" ]]; then
  "$SILENT" uv run --project . pytest \
    tests/scripts/test_usaspending_dbdump_harden.py \
    -v --tb=short --no-header \
    -m "not integration"
else
  # Fallback without run-silent.sh
  uv run --project . pytest \
    tests/scripts/test_usaspending_dbdump_harden.py \
    -v --tb=short --no-header \
    -m "not integration"
fi

echo "--- acceptance suite PASSED ---"
exit 0
