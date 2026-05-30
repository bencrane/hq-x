#!/usr/bin/env bash
# One-shot wrapper that registers usaspending.recipient_grain_lance in Polaris.
#
# Idempotent: init_polaris_lance_generic.py handles "already exists" gracefully
# (its ensure_generic_table verifies format=lance match if the row already exists).
#
# Usage:
#   doppler run --project hq-all --config prd -- bash scripts/register_polaris_recipient_grain.sh
#
set -euo pipefail

doppler run --project hq-all --config prd -- \
  uv run --quiet --with requests python apps/data-engine-x/scripts/init_polaris_lance_generic.py \
    --namespace usaspending \
    --table recipient_grain_lance \
    --doc "USAspending recipient grain — per-UEI 365d obligation aggregates (PR #435 emit path)."
