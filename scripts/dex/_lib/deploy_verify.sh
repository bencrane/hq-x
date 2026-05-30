#!/usr/bin/env bash
# Runtime-probe helpers for the /scope skill's Stage 3.5 deploy-verifier.
#
# Why: a Railway / Vercel / similar "deploy SUCCESS" status only proves the
# container booted. It does NOT prove that imports at module-top of the
# request-handler code succeed, nor that routes register, nor that the
# request path doesn't crash on first hit. The classic failure mode is a
# missing transitive dependency (numpy, lxml, etc.) imported at the top of
# a router module that the health-check route doesn't load — the container
# boots, /healthz returns 200, and every other route 502s.
#
# This file provides functions that curl a non-trivial route per service
# and FAIL the deploy-verify if the route is not reachable. "Reachable" is
# defined as: HTTP 200/204 (success) OR 401/403 (auth-rejected via status)
# OR 307 (auth-rejected via redirect to /login, as Next.js does when a
# server layout calls redirect()). All three prove the route is registered
# and the request path can dispatch to it. The failure cases are 404
# (route not registered), 502/503 (upstream crashed or unhealthy),
# connection-refused, and timeouts.
#
# Sourced — guard against direct invocation.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "deploy_verify.sh is a library — source it, don't execute it" >&2
  exit 1
fi

# --- Service registry: default probe per known service ------------------- #
#
# Each entry maps a service name to a non-trivial route that exercises code
# paths beyond the simple health-check. New services: add their entry here.
#
#   data-engine-x   → /api/v1/internal/observability/sources
#       Exercises Phase 0a observability code which imports widely
#       (sqlalchemy session deps, asyncio, the same import surface that
#       loads every other DEX router). Auth-protected with
#       require_flexible_auth → 401 on unauthenticated call.
#
#   hq-x            → /api/v1/observability/sources
#       Exercises the Phase 0b proxy AND the Phase 4 vector_query import
#       graph (the file that crashed prod on numpy-missing). Auth-protected
#       with require_flexible_auth → 401 on unauthenticated call.
#
#   hq-command      → /admin/system-health
#       Next.js admin route gated by app/admin/layout.tsx, which calls
#       supabase.auth.getUser() server-side and redirect('/login') on no
#       user → 307. The 307 proves middleware ran (supabase env loaded,
#       SSR client created, session-refresh attempted) AND the layout
#       ran (server supabase client created, auth check executed) AND
#       the page bundle resolved. That's a meaningful SSR/dep-chain probe
#       even though the visible page is `'use client'` and fetches the
#       hq-x observability proxy from the browser.

_deploy_verify_default_probe_url() {
  local service="$1" base_url="$2"
  case "$service" in
    data-engine-x)
      echo "${base_url%/}/api/v1/internal/observability/sources"
      ;;
    hq-x)
      echo "${base_url%/}/api/v1/observability/sources"
      ;;
    hq-command)
      echo "${base_url%/}/admin/system-health"
      ;;
    *)
      echo "" # caller passes an explicit probe URL for unknown services
      ;;
  esac
}

# --- _curl_probe: low-level helper. Echoes "<status_code>\n<headers>\n<body>"
# Sets DEPLOY_VERIFY_LAST_STATUS / DEPLOY_VERIFY_LAST_HEADERS / DEPLOY_VERIFY_LAST_BODY
# Returns 0 on any non-zero HTTP response (i.e. curl reached the server);
# returns 1 only on connection-error / DNS-error / timeout.
_curl_probe() {
  local url="$1" timeout="${2:-15}"
  local body_file headers_file status
  body_file=$(mktemp)
  headers_file=$(mktemp)
  status=$(curl -sS -o "$body_file" -D "$headers_file" \
    --max-time "$timeout" \
    -w "%{http_code}" "$url" 2>&1) || {
    # curl itself failed (DNS/connection/timeout). Status will be 000.
    DEPLOY_VERIFY_LAST_STATUS="000"
    DEPLOY_VERIFY_LAST_HEADERS="(curl failed: $status)"
    DEPLOY_VERIFY_LAST_BODY=""
    rm -f "$body_file" "$headers_file"
    return 1
  }
  DEPLOY_VERIFY_LAST_STATUS="$status"
  DEPLOY_VERIFY_LAST_HEADERS=$(cat "$headers_file")
  DEPLOY_VERIFY_LAST_BODY=$(head -c 2000 "$body_file")
  rm -f "$body_file" "$headers_file"
  return 0
}

# --- _evaluate_probe_status: classifies a status code -------------------- #
# 200/204    → PASS (success)
# 307        → PASS (Next.js server-component redirect — e.g. an admin
#              layout calling redirect('/login') for unauthenticated
#              requests. Same signal as 401/403: route registered, request
#              pipeline dispatched, server-side auth check ran)
# 401/403    → PASS (route reachable, auth rejected — proves the route
#              registered AND the request pipeline can dispatch to it)
# anything else → FAIL
_evaluate_probe_status() {
  local status="$1"
  case "$status" in
    200|204|307|401|403) return 0 ;;
    *) return 1 ;;
  esac
}

# --- verify_service_runtime <service> <base_url> [explicit_probe_path] -- #
#
# Curls the default probe URL for <service> against <base_url>. If
# [explicit_probe_path] is provided, curls that path instead.
#
# Returns 0 iff the probe returned 200/204/401/403 with valid HTTP
# response headers. Returns non-zero on 404/5xx/connection-error/timeout.
# On failure, prints status + headers + first 2000 bytes of body to stderr.
#
# Usage from a harness:
#   source apps/data-engine-x/scripts/_lib/deploy_verify.sh
#   if verify_service_runtime data-engine-x "https://api.dataengine.run"; then
#     echo "PASS: data-engine-x runtime probe"
#   fi
verify_service_runtime() {
  local service="$1" base_url="$2" explicit_path="${3:-}"
  local probe_url
  if [[ -n "$explicit_path" ]]; then
    probe_url="${base_url%/}${explicit_path}"
  else
    probe_url=$(_deploy_verify_default_probe_url "$service" "$base_url")
    if [[ -z "$probe_url" ]]; then
      echo "FAIL: no default probe registered for service '$service' — pass an explicit probe path as the 3rd arg" >&2
      return 2
    fi
  fi

  echo "-- runtime-probe $service: GET $probe_url"
  if ! _curl_probe "$probe_url"; then
    echo "FAIL: $service runtime probe — connection error" >&2
    echo "  url: $probe_url" >&2
    echo "  details: $DEPLOY_VERIFY_LAST_HEADERS" >&2
    return 1
  fi

  if _evaluate_probe_status "$DEPLOY_VERIFY_LAST_STATUS"; then
    echo "   PASS ($DEPLOY_VERIFY_LAST_STATUS)"
    return 0
  fi

  echo "FAIL: $service runtime probe returned $DEPLOY_VERIFY_LAST_STATUS" >&2
  echo "  url: $probe_url" >&2
  echo "  --- headers ---" >&2
  echo "$DEPLOY_VERIFY_LAST_HEADERS" >&2
  echo "  --- body (first 2000 bytes) ---" >&2
  echo "$DEPLOY_VERIFY_LAST_BODY" >&2
  # Bonus: pull the last 50 lines of railway logs if RAILWAY_DUMP_LOGS=1
  # and the service is a known Railway service. Deliberately not on by
  # default because logs are slow + need Doppler-wrapped auth.
  if [[ "${RAILWAY_DUMP_LOGS:-0}" == "1" && "$service" =~ ^(data-engine-x|hq-x|hq-command)$ ]]; then
    echo "  --- last 50 lines of railway logs --service $service ---" >&2
    doppler run --project hq-all --config prd -- \
      railway logs --service "$service" 2>/dev/null | tail -50 >&2 || \
      echo "  (railway logs unavailable)" >&2
  fi
  return 1
}

# --- verify_service_with_runtime_probes <service> <base_url> [extra_paths...]
#
# Runs the default probe AND any extra paths supplied. ALL must PASS for
# the function to return 0. Extra paths typically exercise NEW code added
# in the cycle (e.g., a new endpoint introduced by the directive).
#
# Usage from a per-cycle harness:
#   verify_service_with_runtime_probes hq-x "https://api.opsengine.run" \
#     "/api/v1/operator/match-queue" \
#     "/api/v1/internal/matching-engine/evaluate-all"
verify_service_with_runtime_probes() {
  local service="$1" base_url="$2"
  shift 2
  local fail_count=0

  # 1. Default probe.
  if ! verify_service_runtime "$service" "$base_url"; then
    fail_count=$((fail_count+1))
  fi

  # 2. Extra cycle-specific probes.
  while [[ $# -gt 0 ]]; do
    local extra_path="$1"
    shift
    if ! verify_service_runtime "$service" "$base_url" "$extra_path"; then
      fail_count=$((fail_count+1))
    fi
  done

  if (( fail_count > 0 )); then
    echo "FAIL: $fail_count runtime probe(s) failed for $service" >&2
    return 1
  fi
  return 0
}

# --- verify_all_known_services: convenience smoke probe ------------------ #
# Runs default probes for all three core services against their canonical
# prod URLs. Used by the dogfood smoke test in this directive — and by
# operator-driven "is everything alive?" checks.
verify_all_known_services() {
  local fail_count=0
  verify_service_runtime data-engine-x "https://api.dataengine.run" \
    || fail_count=$((fail_count+1))
  verify_service_runtime hq-x          "https://api.opsengine.run" \
    || fail_count=$((fail_count+1))
  verify_service_runtime hq-command    "https://hq-command-production.up.railway.app" \
    || fail_count=$((fail_count+1))
  if (( fail_count > 0 )); then
    echo "FAIL: $fail_count of 3 known services failed runtime probe" >&2
    return 1
  fi
  echo "PASS: all 3 known services responded to runtime probes"
  return 0
}
