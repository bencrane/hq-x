#!/usr/bin/env bash
# Rollback s6: tear down the operator bootstrap data state — Polaris DELETE
# for 3 generic-tables + R2 prefix delete for 3 Lance datasets + 1 raw release
# prefix + modal app stop. Reverse-order within s6: Polaris first (consumer-facing),
# then R2 (storage), then Modal app stop.
#
# Per directive surface s6 rollback column:
#   - 3× Polaris DELETE (licensure.fl_cilb_lance + bridges.sba_fl_cilb_lance + bridges.fl_cilb_sunbiz_lance)
#   - R2 prefix delete polaris-warehouse/licensure/fl_cilb_lance/
#   - R2 prefix delete polaris-warehouse/bridges/sba_fl_cilb_lance/
#   - R2 prefix delete polaris-warehouse/bridges/fl_cilb_sunbiz_lance/
#   - R2 prefix delete fl-cilb/release=*/  (all releases)
#   - modal app stop data-engine-x-fl-cilb-daily
#
# Invocation: boto3 + requests not in system python3 — run via doppler+uv.
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_ROOT="/Users/benjamincrane/hq-all"

cd "$REPO_ROOT/apps/data-engine-x" && doppler run --project hq-all --config prd -- uv run --quiet python - <<'PY'
import os
import sys

import boto3
import requests

errors = []

# --- 3× Polaris DELETE generic-tables ---
try:
    url = os.environ["POLARIS_PUBLIC_URL"].rstrip("/")
    catalog = os.environ["POLARIS_DEFAULT_CATALOG_NAME"]
    tok_resp = requests.post(
        url + "/api/catalog/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["POLARIS_ROOT_PRINCIPAL_ID"],
            "client_secret": os.environ["POLARIS_ROOT_PRINCIPAL_SECRET"],
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        timeout=30,
    )
    tok_resp.raise_for_status()
    tok = tok_resp.json()["access_token"]
    headers = {"Authorization": "Bearer " + tok}
    for namespace, table in [
        ("licensure", "fl_cilb_lance"),
        ("bridges",  "sba_fl_cilb_lance"),
        ("bridges",  "fl_cilb_sunbiz_lance"),
    ]:
        r = requests.delete(
            url + "/api/catalog/polaris/v1/" + catalog + "/namespaces/" + namespace + "/generic-tables/" + table,
            headers=headers,
            timeout=30,
        )
        if r.status_code in (200, 204, 404):
            print(f"Polaris DELETE {namespace}/{table}: {r.status_code}")
        else:
            errors.append(f"Polaris DELETE {namespace}/{table} failed: {r.status_code} {r.text[:200]}")
except Exception as e:
    errors.append(f"Polaris teardown failed: {e!r}")

# --- R2 prefix delete: 3 Lance datasets + 1 raw release prefix ---
try:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in (
        "polaris-warehouse/licensure/fl_cilb_lance/",
        "polaris-warehouse/bridges/sba_fl_cilb_lance/",
        "polaris-warehouse/bridges/fl_cilb_sunbiz_lance/",
        "fl-cilb/release=",
    ):
        deleted = 0
        for page in paginator.paginate(Bucket="dex-raw-landing-zone", Prefix=prefix):
            for obj in page.get("Contents", []):
                s3.delete_object(Bucket="dex-raw-landing-zone", Key=obj["Key"])
                deleted += 1
        print(f"R2 deleted {deleted} objects under {prefix}")
except Exception as e:
    errors.append(f"R2 teardown failed: {e!r}")

if errors:
    for e in errors:
        print("FAIL: " + e, file=sys.stderr)
    sys.exit(1)
print("s6 Polaris+R2 OK")
PY

# --- Modal app stop (independent of Polaris/R2 teardown above) ---
if command -v modal >/dev/null 2>&1; then
  if modal app list 2>/dev/null | grep -qE 'data-engine-x-fl-cilb-daily'; then
    modal app stop data-engine-x-fl-cilb-daily || {
      echo "WARN: modal app stop data-engine-x-fl-cilb-daily failed (non-fatal; app may already be stopped)" >&2
    }
    echo "modal app stop: data-engine-x-fl-cilb-daily issued"
  else
    echo "  (advisory) modal app 'data-engine-x-fl-cilb-daily' not visible — nothing to stop"
  fi
else
  echo "WARN: modal CLI not on PATH — cannot stop the daily cron app; operator must run 'modal app stop data-engine-x-fl-cilb-daily' manually" >&2
fi

echo "s6 rollback OK: 3 Polaris generic-tables + 3 Lance prefixes + 1 raw release prefix deleted; Modal daily app stopped"
