#!/usr/bin/env bash
# Vault thin-shim that locates the canonical apps/data-engine-x/scripts/_lib/dex.sh
# and re-sources it. Vault harness scripts source THIS file rather than duplicating
# the Doppler/psql plumbing inline.
#
# See apps/data-engine-x/CLAUDE.md §"Helper library" for what's exposed.
#
# Lookup order:
#   1. $DEX_LIB_PATH (operator override)
#   2. $HOME/hq-all/apps/data-engine-x/scripts/_lib/dex.sh (canonical monorepo checkout)
#   3. relative to the caller's git repo root + apps/data-engine-x/scripts/_lib/dex.sh
#   4. $HOME/Desktop/hq-all/apps/data-engine-x/scripts/_lib/dex.sh (legacy auxiliary clone; often stale)

_dex_locate_lib() {
  if [[ -n "${DEX_LIB_PATH:-}" && -f "$DEX_LIB_PATH" ]]; then
    echo "$DEX_LIB_PATH"
    return 0
  fi
  local canonical="$HOME/hq-all/apps/data-engine-x/scripts/_lib/dex.sh"
  if [[ -f "$canonical" ]]; then
    echo "$canonical"
    return 0
  fi
  local repo_root
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
  if [[ -n "$repo_root" && -f "$repo_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    echo "$repo_root/apps/data-engine-x/scripts/_lib/dex.sh"
    return 0
  fi
  local legacy="$HOME/Desktop/hq-all/apps/data-engine-x/scripts/_lib/dex.sh"
  if [[ -f "$legacy" ]]; then
    echo "$legacy"
    return 0
  fi
  return 1
}

_DEX_LIB=$(_dex_locate_lib) || {
  echo "FAIL: cannot locate apps/data-engine-x/scripts/_lib/dex.sh — set DEX_LIB_PATH or run from a hq-all checkout" >&2
  exit 1
}
# shellcheck source=/dev/null
source "$_DEX_LIB"
