#!/usr/bin/env python3
"""Florida DOR NAL (Name-Address-Legal) assessment roll → R2 landing.

Florida Dept. of Revenue publishes the statewide real-property assessment
roll (NAL) as 67 per-county comma-delimited CSVs (zipped) on its SharePoint
data portal. This loader mirrors the **Final 2025** roll into R2 as ZSTD
Parquet, projecting the GTM/property-targeting subset (~45 cols) out of the
165-column raw NAL layout, casting numerics, and adding a normalized owner
key + derived lot_acres.

Why this subset: the raw NAL carries 82 EXMPT_* exemption columns and other
administrative noise irrelevant to property targeting. We keep identity
(parcel/state-parcel/alt key), use code, just/land/special-feature value,
**lot size (LND_SQFOOT)**, year built, building size, unit counts, two most
recent sales, owner mailing identity, situs (physical) address, census block
(HMDA-tract joinable), and legal description.

Layout per county:
  fl-dor-nal/snapshot={YYYY-MM-DD}/county_no={NN}.parquet

Downstream: scripts/run_fl_dor_nal_lance_emit.py reads
  fl-dor-nal/snapshot=*/*.parquet  → Lance (latest_snapshot mode).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fl_dor_nal_r2_ingest.py --counties 40,61
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fl_dor_nal_r2_ingest.py --all --skip-existing
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import boto3
import duckdb
from botocore.config import Config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
LOG = logging.getLogger("fl-dor-nal")

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "fl-dor-nal"
DEFAULT_SNAPSHOT = "2025-10-02"  # Final 2025 roll publish window
PORTAL_BASE = (
    "https://floridarevenue.com/property/dataportal/Documents/"
    "PTO%20Data%20Portal/Tax%20Roll%20Data%20Files/NAL/2025F"
)
TMP = Path("/tmp/fldor")
USER_AGENT = "data-engine-x ingest (operator: tools@substrate.build)"

# Exact portal filenames (enumerated via SharePoint REST 2026-05-29). Some
# carry the portal's own spellings/typos ("Indin River", "Dade"); we parse
# the county code + display name from the filename verbatim so URLs resolve.
ZIP_FILENAMES = [
    "Alachua 11 Final NAL 2025.zip", "Baker 12 Final NAL 2025.zip",
    "Bay 13 Final NAL 2025.zip", "Bradford 14 Final NAL 2025.zip",
    "Brevard 15 Final NAL 2025.zip", "Broward 16 Final NAL 2025.zip",
    "Calhoun 17 Final NAL 2025.zip", "Charlotte 18 Final NAL 2025.zip",
    "Citrus 19 Final NAL 2025.zip", "Clay 20 Final NAL 2025.zip",
    "Collier 21 Final NAL 2025.zip", "Columbia 22 Final NAL 2025.zip",
    "Dade 23 Final NAL 2025.zip", "Desoto 24 Final NAL 2025.zip",
    "Dixie 25 Final NAL 2025.zip", "Duval 26 Final NAL 2025.zip",
    "Escambia 27 Final NAL 2025.zip", "Flagler 28 Final NAL 2025.zip",
    "Franklin 29 Final NAL 2025.zip", "Gadsden 30 Final NAL 2025.zip",
    "Gilchrist 31 Final NAL 2025.zip", "Glades 32 Final NAL 2025.zip",
    "Gulf 33 Final NAL 2025.zip", "Hamilton 34 Final NAL 2025.zip",
    "Hardee 35 Final NAL 2025.zip", "Hendry 36 Final NAL 2025.zip",
    "Hernando 37 Final NAL 2025.zip", "Highlands 38 Final NAL 2025.zip",
    "Hillsborough 39 Final NAL 2025.zip", "Holmes 40 Final NAL 2025.zip",
    "Indin River 41 Final NAL 2025.zip", "Jackson 42 Final NAL 2025.zip",
    "Jefferson 43 Final NAL 2025.zip", "Lafayette 44 Final NAL 2025.zip",
    "Lake 45 Final NAL 2025.zip", "Lee 46 Final NAL 2025.zip",
    "Leon 47 Final NAL 2025.zip", "Levy 48 Final NAL 2025.zip",
    "Liberty 49 Final NAL 2025.zip", "Madison 50 Final NAL 2025.zip",
    "Manatee 51 Final NAL 2025.zip", "Marion 52 Final NAL 2025.zip",
    "Martin 53 Final NAL 2025.zip", "Monroe 54 Final NAL 2025.zip",
    "Nassau 55 Final NAL 2025.zip", "Okaloosa 56 Final NAL 2025.zip",
    "Okeechobee 57 Final NAL 2025.zip", "Orange 58 Final NAL 2025.zip",
    "Osceola 59 Final NAL 2025.zip", "Palm Beach 60 Final NAL 2025.zip",
    "Pasco 61 Final NAL 2025.zip", "Pinellas 62 Final NAL 2025.zip",
    "Polk 63 Final NAL 2025.zip", "Putnam 64 Final NAL 2025.zip",
    "Saint Johns 65 Final NAL 2025.zip", "Saint Lucie 66 Final NAL 2025.zip",
    "Santa Rosa 67 Final NAL 2025.zip", "Sarasota 68 Final NAL 2025.zip",
    "Seminole 69 Final NAL 2025.zip", "Sumter 70 Final NAL 2025.zip",
    "Suwannee 71 Final NAL 2025.zip", "Taylor 72 Final NAL 2025.zip",
    "Union 73 Final NAL 2025.zip", "Volusia 74 Final NAL 2025.zip",
    "Wakulla 75 Final NAL 2025.zip", "Walton 76 Final NAL 2025.zip",
    "Washington 77 Final NAL 2025.zip",
]


def _parse_manifest() -> list[tuple[int, str, str]]:
    """-> list of (county_no, display_name, zip_filename)."""
    out = []
    for fn in ZIP_FILENAMES:
        stem = fn.replace(" Final NAL 2025.zip", "")
        name, code = stem.rsplit(" ", 1)
        out.append((int(code), name.strip(), fn))
    return out


# Projection: raw 165-col NAL -> ~45-col GTM/property-targeting subset.
# Numerics via TRY_CAST (blank strings -> NULL). Owner key normalized inline.
PROJECT_SQL = """
SELECT
  TRY_CAST(CO_NO AS INTEGER)                         AS co_no,
  ?                                                  AS county_name,
  PARCEL_ID                                          AS parcel_id,
  CAST(CO_NO AS VARCHAR) || '-' || PARCEL_ID         AS parcel_uid,
  STATE_PAR_ID                                       AS state_par_id,
  ALT_KEY                                            AS alt_key,
  DOR_UC                                             AS dor_uc,
  PA_UC                                              AS pa_uc,
  TRY_CAST(JV AS BIGINT)                             AS jv,
  TRY_CAST(LND_VAL AS BIGINT)                        AS lnd_val,
  TRY_CAST(AV_SD AS BIGINT)                          AS av_sd,
  TRY_CAST(SPEC_FEAT_VAL AS BIGINT)                  AS spec_feat_val,
  TRY_CAST(LND_SQFOOT AS DOUBLE)                     AS lnd_sqfoot,
  ROUND(TRY_CAST(LND_SQFOOT AS DOUBLE) / 43560.0, 4) AS lot_acres,
  TRY_CAST(NO_LND_UNTS AS DOUBLE)                    AS no_lnd_unts,
  LND_UNTS_CD                                        AS lnd_unts_cd,
  TRY_CAST(ACT_YR_BLT AS INTEGER)                    AS act_yr_blt,
  TRY_CAST(EFF_YR_BLT AS INTEGER)                    AS eff_yr_blt,
  TRY_CAST(TOT_LVG_AREA AS DOUBLE)                   AS tot_lvg_area,
  TRY_CAST(NO_BULDNG AS INTEGER)                     AS no_buldng,
  TRY_CAST(NO_RES_UNTS AS INTEGER)                   AS no_res_unts,
  TRY_CAST(SALE_PRC1 AS BIGINT)                      AS sale_prc1,
  TRY_CAST(SALE_YR1 AS INTEGER)                      AS sale_yr1,
  TRY_CAST(SALE_MO1 AS INTEGER)                      AS sale_mo1,
  QUAL_CD1                                           AS qual_cd1,
  VI_CD1                                             AS vi_cd1,
  TRY_CAST(SALE_PRC2 AS BIGINT)                      AS sale_prc2,
  TRY_CAST(SALE_YR2 AS INTEGER)                      AS sale_yr2,
  TRY_CAST(SALE_MO2 AS INTEGER)                      AS sale_mo2,
  OWN_NAME                                           AS own_name,
  trim(regexp_replace(lower(OWN_NAME), '[^a-z0-9 ]', ' ', 'g')) AS owner_name_normalized,
  OWN_ADDR1                                          AS own_addr1,
  OWN_ADDR2                                          AS own_addr2,
  OWN_CITY                                           AS own_city,
  OWN_STATE                                          AS own_state,
  OWN_ZIPCD                                          AS own_zipcd,
  FIDU_NAME                                          AS fidu_name,
  PHY_ADDR1                                          AS phy_addr1,
  PHY_ADDR2                                          AS phy_addr2,
  PHY_CITY                                           AS phy_city,
  PHY_ZIPCD                                          AS phy_zipcd,
  S_LEGAL                                            AS s_legal,
  CENSUS_BK                                          AS census_bk,
  TWN                                                AS twn,
  RNG                                                AS rng,
  SEC                                                AS sec,
  NBRHD_CD                                           AS nbrhd_cd,
  MKT_AR                                             AS mkt_ar,
  2025                                               AS roll_year,
  'final'                                            AS roll_type
FROM read_csv(?, all_varchar=true, header=true, quote='"', escape='"',
              null_padding=true, ignore_errors=false)
"""


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _download(zip_filename: str, dest: Path) -> None:
    url = f"{PORTAL_BASE}/{urllib.parse.quote(zip_filename)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())


def process_county(
    con: duckdb.DuckDBPyConnection,
    s3,
    code: int,
    name: str,
    zip_filename: str,
    snapshot: str,
    skip_existing: bool,
) -> dict:
    key = f"{R2_PREFIX}/snapshot={snapshot}/county_no={code:02d}.parquet"
    if skip_existing:
        try:
            s3.head_object(Bucket=R2_BUCKET, Key=key)
            LOG.info("[%02d %s] skip (exists in R2)", code, name)
            return {"code": code, "rows": None, "skipped": True}
        except Exception:
            pass

    TMP.mkdir(parents=True, exist_ok=True)
    zpath = TMP / f"{code:02d}.zip"
    t0 = time.time()
    LOG.info("[%02d %s] downloading ...", code, name)
    _download(zip_filename, zpath)

    with zipfile.ZipFile(zpath) as zf:
        csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        zf.extract(csv_name, TMP / f"{code:02d}_x")
    csv_path = str(TMP / f"{code:02d}_x" / csv_name)

    pq_path = str(TMP / f"{code:02d}.parquet")
    con.execute(
        f"COPY ({PROJECT_SQL}) TO '{pq_path}' "
        f"(FORMAT parquet, COMPRESSION zstd)",
        [name, csv_path],
    )
    rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{pq_path}')"
    ).fetchone()[0]

    s3.upload_file(pq_path, R2_BUCKET, key)
    dur = time.time() - t0
    sz = os.path.getsize(pq_path) / 1e6
    LOG.info(
        "[%02d %s] -> r2://%s/%s  rows=%d  %.1fMB  %.1fs",
        code, name, R2_BUCKET, key, rows, sz, dur,
    )
    # cleanup local scratch
    for p in (zpath, Path(pq_path)):
        p.unlink(missing_ok=True)
    return {"code": code, "name": name, "rows": rows, "skipped": False}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FL DOR NAL 2025 -> R2 landing")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="all 67 counties")
    g.add_argument("--counties", help="comma list of county codes, e.g. 40,61")
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args(argv)

    for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(v):
            LOG.error("FAIL: %s not set", v)
            return 64

    manifest = _parse_manifest()
    if args.counties:
        want = {int(x) for x in args.counties.split(",")}
        manifest = [m for m in manifest if m[0] in want]
    LOG.info("counties to process: %d  snapshot=%s", len(manifest), args.snapshot)

    con = duckdb.connect()
    s3 = _r2_client()
    results, total_rows, failures = [], 0, []
    for code, name, zip_filename in manifest:
        try:
            r = process_county(
                con, s3, code, name, zip_filename, args.snapshot,
                args.skip_existing,
            )
            results.append(r)
            if r.get("rows"):
                total_rows += r["rows"]
        except Exception as e:
            LOG.error("[%02d %s] FAILED: %s", code, name, e)
            failures.append((code, name, str(e)))

    LOG.info("=" * 60)
    LOG.info(
        "DONE counties=%d total_rows=%d failures=%d",
        len(results), total_rows, len(failures),
    )
    for code, name, err in failures:
        LOG.error("  FAILED %02d %s: %s", code, name, err)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
