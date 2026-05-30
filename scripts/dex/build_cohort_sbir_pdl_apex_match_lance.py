"""One-shot CSV → Lance for the SBIR×PDL apex-domain match cohort.

Reads /tmp/sbir_x_pdl_apex_match.csv (8,927 rows of SBIR firms whose
apex domain matched a PDL company row, with pdl_linkedin_url already
resolved) and emits a Lance dataset at
s3://dex-raw-landing-zone/polaris-warehouse/cohorts/sbir_pdl_apex_match_lance.

Schema (kept lean — only what the cohort emit + downstream firmo need):
    uei              string (BTREE)
    domain           string  (apex_domain, pre-normalized)
    linkedin_url     string  (pdl_linkedin_url; may lack scheme)
    cohort_version   string  ('1.0.0')
    generated_at     timestamp[us, UTC]
    source_label     string  ('sbir_pdl_apex')
"""
from __future__ import annotations

import csv
import os
import sys
import time
import re
from datetime import datetime, timezone

import lance
import pyarrow as pa

CSV_PATH = "/tmp/sbir_x_pdl_apex_match.csv"
OUT_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/"
    "cohorts/sbir_pdl_apex_match_lance"
)
COHORT_VERSION = "1.0.0"

_RE_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_RE_WWW = re.compile(r"^www\.", re.IGNORECASE)
_RE_PATH = re.compile(r"[/?#].*$")


def norm(raw):
    if not raw:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _RE_SCHEME.sub("", s)
    s = _RE_WWW.sub("", s)
    s = _RE_PATH.sub("", s)
    return s.strip() or None


def main() -> int:
    storage = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }
    generated_at = datetime.now(tz=timezone.utc)

    ueis: list[str] = []
    domains: list[str] = []
    linkedins: list[str] = []
    dropped_no_uei = 0
    dropped_no_dom = 0
    dropped_no_li = 0
    seen: set[str] = set()
    with open(CSV_PATH, newline="") as fh:
        for row in csv.DictReader(fh):
            uei = (row.get("sbir_uei") or "").strip()
            dom = norm(row.get("apex_domain") or "")
            li = (row.get("pdl_linkedin_url") or "").strip()
            if not uei:
                dropped_no_uei += 1
                continue
            if not dom:
                dropped_no_dom += 1
                continue
            if not li or "linkedin.com" not in li.lower():
                dropped_no_li += 1
                continue
            if uei in seen:
                continue
            seen.add(uei)
            ueis.append(uei)
            domains.append(dom)
            linkedins.append(li)

    print(f"input rows -> kept={len(ueis):,} "
          f"dropped_no_uei={dropped_no_uei} "
          f"dropped_no_dom={dropped_no_dom} "
          f"dropped_no_linkedin={dropped_no_li}")

    n = len(ueis)
    tbl = pa.table({
        "uei":            pa.array(ueis, type=pa.string()),
        "domain":         pa.array(domains, type=pa.string()),
        "linkedin_url":   pa.array(linkedins, type=pa.string()),
        "cohort_version": pa.array([COHORT_VERSION] * n, type=pa.string()),
        "generated_at":   pa.array([generated_at] * n,
                                   type=pa.timestamp("us", tz="UTC")),
        "source_label":   pa.array(["sbir_pdl_apex"] * n, type=pa.string()),
    })

    t0 = time.time()
    ds = lance.write_dataset(
        tbl, OUT_URI, mode="overwrite", storage_options=storage,
    )
    dur = time.time() - t0
    rows = ds.count_rows()
    print(f"wrote {rows:,} rows in {dur:.1f}s (version={ds.version})")
    try:
        ds.create_scalar_index("uei", index_type="BTREE", replace=True)
        print("BTREE on uei: OK")
    except Exception as e:
        print(f"BTREE failed (non-fatal): {e}")
    try:
        ds.optimize.compact_files()
    except Exception as e:
        print(f"compact_files failed (non-fatal): {e}")
    print(f"URI: {OUT_URI}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
