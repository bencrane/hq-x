#!/usr/bin/env python3
"""SAM.gov bulk-extract → R2 Fuel Tank ingest.

Three streams, one script:

  monthly             — current Public V2 entity registration extract
                        (~875K rows, ~150 cols, pipe-delimited .dat).
                        File: SAM_PUBLIC_(UTF-8_)?MONTHLY_V2_<YYYYMMDD>.ZIP
                        R2:   sam-gov/monthly/snapshot={YYYY-MM-DD}/data.parquet

  historical_modified — semi-annual MODIFIED archives (delta records, 50K-200K
                        rows each) for 2020-MAY through 2025-NOV. Snapshot
                        date is end-of-period (MAY-31, NOV-30).
                        File: SAM_PUBLIC_MONTHLY_<YYYY>_(MAY|NOV)_MODIFIED.zip
                        R2:   sam-gov/historical/snapshot={YYYY-MM-DD}/data.parquet
                              sam-gov/historical-pre-v2/snapshot={YYYY-MM-DD}/data.parquet (pre-2020-NOV)

  exclusions          — debarment / exclusion list (~168K rows, CSV).
                        File: SAM_Exclusions_Public_Extract_*.zip
                        R2:   sam-gov/exclusions/snapshot={YYYY-MM-DD}/data.parquet

NO API.  Per the directive (~/Desktop/hq/directives/2026-05-08-sam-gov-…),
the SAM.gov Extracts API is explicitly ruled out (10/day rate cap defeats
bulk pulls). All three SAM.gov data-services pages gate ZIP download behind
a logged-in browser session, so direct unauthenticated GET against
s3.amazonaws.com/falextracts/… returns 403.

The script's contract:

  1. Build the list of expected (extract_kind, archive_date, filename) tuples.
  2. Attempt direct unauthenticated GET for each.
  3. For files not fetched, look for them in the staging dir (default
     /Users/benjamincrane/Downloads/sam_gov_bulk/).
  4. If ANY file is still missing — print operator-action list, exit 42.
  5. Otherwise: unzip → DuckDB transform → ZSTD Parquet → R2 → audit row.

Idempotency: re-running with --skip-if-unchanged compares the file's SHA256
against the most recent 'completed' audit row for that slice; matches emit
'no_change' and skip Parquet/R2 work.

RisingWave wiring: DEFERRED to a follow-up directive.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sam_gov_r2_ingest.py --kind historical \\
      --archive 2024-NOV --max-rows 10000

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sam_gov_r2_ingest.py --kind historical --all

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sam_gov_r2_ingest.py --kind exclusions

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sam_gov_r2_ingest.py --kind monthly \\
      --archive 2026-05-08

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sam_gov_r2_ingest.py --discover
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb

# Lift the canonical 142-column SAM.gov Public V2 schema from the existing
# Postgres-side ingest. Keeps the R2 path locked to the same field order /
# names — downstream RW MVs that join the R2 Parquet against the Postgres
# table see consistent column identities.
from app.services.sam_gov_column_map import SAM_GOV_DB_COLUMN_NAMES

# Pre-V2 schema (120 cols, no UEI) for historical archives 2014-NOV through
# 2020-MAY. Derived from the layout xlsx GSA ships inside each archive ZIP.
from scripts._lib.sam_gov_pre_v2_schema import SAM_GOV_PRE_V2_DB_COLUMN_NAMES

# Pure-functional normalizers; the SQL macros below mirror these for in-DuckDB
# vectorized application.
from scripts._lib import sam_gov_normalize  # noqa: F401  (used in tests)

R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

DEFAULT_STAGING_DIR = "/Users/benjamincrane/Downloads/sam_gov_bulk"

# Historical MODIFIED archive span — full ~10-year corpus 2014-NOV through
# 2025-NOV. SAM publishes semi-annual MAY+NOV archives; 2014 only has NOV.
# Schema split: 2014-NOV through 2020-MAY use the 120-column pre-V2 schema
# (no UEI); 2020-NOV onwards use the 142-column V2 schema.
HISTORICAL_YEARS = tuple(range(2014, 2026))
HISTORICAL_HALVES = ("MAY", "NOV")
# 2014-MAY does not exist in SAM's archive — START at 2014-NOV.
HISTORICAL_FIRST_YEAR_NOV_ONLY = 2014
# V2 schema rolled out between 2020-MAY and 2020-NOV. Archives strictly
# before 2020-NOV use pre-V2 (120 cols); 2020-NOV onwards use V2 (142 cols).
V2_SCHEMA_FIRST_DATE = date(2020, 11, 30)

FALEXTRACTS_BASE = "https://s3.amazonaws.com/falextracts"


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sam-gov-r2-ingest")


log = _logger()


# ──────────────────────────────────────────────────────────────────────────────
# Slice descriptor
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Slice:
    """One unit of ingest work — (extract_kind, archive_date, candidate filenames)."""

    extract_kind: str  # 'monthly' | 'historical_modified' | 'exclusions'
    archive_date: date
    archive_label: str  # e.g. '2024-NOV', '2026-05-08'
    candidate_urls: tuple[tuple[str, str], ...]  # (filename, url) pairs

    @property
    def schema_version(self) -> str:
        """'pre_v2' for pre-2020-NOV historical archives, 'v2' for everything else.

        Monthly extracts (post-2020) and exclusions are always V2-flavor.
        """
        if (self.extract_kind == "historical_modified"
                and self.archive_date < V2_SCHEMA_FIRST_DATE):
            return "pre_v2"
        return "v2"

    @property
    def r2_prefix(self) -> str:
        # Hive-style snapshot=YYYY-MM-DD/ for all kinds (DuckDB-on-R2 reads use
        # hive_partitioning=1 + read_parquet('s3://.../snapshot=*/data.parquet')).
        # archive_date is end-of-period for historical (MAY-31, NOV-30) and the
        # actual extract date for monthly + exclusions.
        snapshot = self.archive_date.isoformat()
        if self.extract_kind == "monthly":
            return f"sam-gov/monthly/snapshot={snapshot}/"
        if self.extract_kind == "historical_modified":
            if self.schema_version == "pre_v2":
                return f"sam-gov/historical-pre-v2/snapshot={snapshot}/"
            return f"sam-gov/historical/snapshot={snapshot}/"
        return f"sam-gov/exclusions/snapshot={snapshot}/"

    @property
    def r2_object_key(self) -> str:
        return self.r2_prefix + "data.parquet"


# ──────────────────────────────────────────────────────────────────────────────
# Slice builders
# ──────────────────────────────────────────────────────────────────────────────


def build_historical_slices() -> list[Slice]:
    out: list[Slice] = []
    # SAM publishes MAY archives at end-of-Q2 cycle; NOV at end-of-Q4. Use
    # MAY-31 and NOV-30 as nominal dates — the file's actual contents are
    # snapshotted at month-end of that label.
    for yr in HISTORICAL_YEARS:
        for half in HISTORICAL_HALVES:
            # 2014-MAY does not exist in SAM's archive — only 2014-NOV.
            if yr == HISTORICAL_FIRST_YEAR_NOV_ONLY and half == "MAY":
                continue
            archive_label = f"{yr}-{half}"
            archive_date = date(yr, 5 if half == "MAY" else 11,
                                31 if half == "MAY" else 30)
            base = f"{FALEXTRACTS_BASE}/Entity%20Registration/Public%20-%20Historical"
            # Try multiple filename + case variants the operator may stage.
            stem_v1 = f"SAM_PUBLIC_MONTHLY_{yr}_{half}_MODIFIED"
            stem_utf8 = f"SAM_PUBLIC_UTF-8_MONTHLY_{yr}_{half}_MODIFIED"
            candidates = (
                (f"{stem_utf8}.zip", f"{base}/{stem_utf8}.zip"),
                (f"{stem_utf8}.ZIP", f"{base}/{stem_utf8}.ZIP"),
                (f"{stem_v1}.zip", f"{base}/{stem_v1}.zip"),
                (f"{stem_v1}.ZIP", f"{base}/{stem_v1}.ZIP"),
            )
            out.append(Slice(
                extract_kind="historical_modified",
                archive_date=archive_date,
                archive_label=archive_label,
                candidate_urls=candidates,
            ))
    return out


def build_monthly_slice(archive_date_str: str) -> Slice:
    """Build a slice for a specific monthly extract date (YYYY-MM-DD)."""
    archive_date = date.fromisoformat(archive_date_str)
    ymd = archive_date.strftime("%Y%m%d")
    base = f"{FALEXTRACTS_BASE}/Entity%20Registration/Public%20V2"
    candidates = (
        (f"SAM_PUBLIC_UTF-8_MONTHLY_V2_{ymd}.ZIP",
         f"{base}/SAM_PUBLIC_UTF-8_MONTHLY_V2_{ymd}.ZIP"),
        (f"SAM_PUBLIC_UTF-8_MONTHLY_V2_{ymd}.zip",
         f"{base}/SAM_PUBLIC_UTF-8_MONTHLY_V2_{ymd}.zip"),
        (f"SAM_PUBLIC_MONTHLY_V2_{ymd}.ZIP",
         f"{base}/SAM_PUBLIC_MONTHLY_V2_{ymd}.ZIP"),
        (f"SAM_PUBLIC_MONTHLY_V2_{ymd}.zip",
         f"{base}/SAM_PUBLIC_MONTHLY_V2_{ymd}.zip"),
    )
    return Slice(
        extract_kind="monthly",
        archive_date=archive_date,
        archive_label=archive_date.isoformat(),
        candidate_urls=candidates,
    )


def build_exclusions_slice(archive_date_str: str | None) -> Slice:
    """Build a slice for the exclusions snapshot. archive_date defaults to today."""
    d = date.fromisoformat(archive_date_str) if archive_date_str else date.today()
    base = f"{FALEXTRACTS_BASE}/Exclusions/Public%20V2"
    # We don't know the SAM-published filename a priori — SAM uses YYDDD
    # (Julian-day) suffixes. The operator-staged filename is what we'll match.
    # We still emit a few candidate URLs for the direct-download attempt.
    ydoy = f"{d.year % 100:02d}{d.timetuple().tm_yday:03d}"
    candidates = (
        (f"SAM_Exclusions_Public_Extract_{ydoy}.zip",
         f"{base}/SAM_Exclusions_Public_Extract_{ydoy}.zip"),
        (f"SAM_Exclusions_Public_Extract_{ydoy}.ZIP",
         f"{base}/SAM_Exclusions_Public_Extract_{ydoy}.ZIP"),
        ("SAM_Exclusions_Public_Extract.zip",
         f"{base}/SAM_Exclusions_Public_Extract.zip"),
    )
    return Slice(
        extract_kind="exclusions",
        archive_date=d,
        archive_label=d.isoformat(),
        candidate_urls=candidates,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Direct-download / staging-dir resolution
# ──────────────────────────────────────────────────────────────────────────────


def head(client: httpx.Client, url: str) -> tuple[int, int | None, str | None]:
    try:
        r = client.head(url, follow_redirects=True, timeout=20.0)
        return (
            r.status_code,
            int(r.headers.get("content-length", "0")) or None,
            r.headers.get("last-modified"),
        )
    except Exception as e:
        return -1, None, str(e)


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=3600.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss", url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    last_log = time.monotonic()
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 15.0:
                            log.info(
                                "  download progress: %.1f MB",
                                written / (1 << 20),
                            )
                            last_log = now
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed: {last_exc}")


@dataclass
class ResolvedFile:
    slice_obj: Slice
    local_path: Path
    source_url: str | None  # None if staged manually
    source_filename: str
    bytes_size: int
    sha256: str
    staged_locally: bool


def resolve_slice(
    sl: Slice,
    *,
    client: httpx.Client,
    staging_dir: Path,
    workdir: Path,
    skip_direct: bool,
) -> ResolvedFile | None:
    """Try direct download first; if 403/404/etc., look in staging dir.

    Returns ResolvedFile on success, None if no source could be located.
    """
    log_prefix = f"[{sl.extract_kind} / {sl.archive_label}]"

    # 1. Direct download attempt.
    if not skip_direct:
        for filename, url in sl.candidate_urls:
            sc, cl, lm = head(client, url)
            if sc == 200:
                log.info("%s direct-download HEAD ok: %s (%s bytes)",
                         log_prefix, url, cl)
                target = workdir / filename
                bytes_dl = download_zip(client, url, target)
                sha = sha256_of(target)
                return ResolvedFile(
                    slice_obj=sl,
                    local_path=target,
                    source_url=url,
                    source_filename=filename,
                    bytes_size=bytes_dl,
                    sha256=sha,
                    staged_locally=False,
                )
            log.info("%s direct-download HEAD %d on %s", log_prefix, sc, url)

    # 2. Look in staging dir for any matching filename.
    candidate_filenames = [fn for fn, _ in sl.candidate_urls]
    for fn in candidate_filenames:
        p = staging_dir / fn
        if p.exists():
            log.info("%s found staged file: %s", log_prefix, p)
            return ResolvedFile(
                slice_obj=sl,
                local_path=p,
                source_url=None,
                source_filename=fn,
                bytes_size=p.stat().st_size,
                sha256=sha256_of(p),
                staged_locally=True,
            )

    # 3. Loose match — the operator may have staged a file whose name doesn't
    # exactly match our candidate list. For exclusions, accept anything that
    # matches the SAM_Exclusions_Public_Extract_*.zip pattern.
    if sl.extract_kind == "exclusions":
        pattern = re.compile(
            r"^SAM_Exclusions_Public_Extract_?\d*\.[Zz][Ii][Pp]$"
        )
        for p in sorted(staging_dir.glob("SAM_Exclusions_*")):
            if pattern.match(p.name):
                log.info("%s loose-match staged: %s", log_prefix, p)
                return ResolvedFile(
                    slice_obj=sl,
                    local_path=p,
                    source_url=None,
                    source_filename=p.name,
                    bytes_size=p.stat().st_size,
                    sha256=sha256_of(p),
                    staged_locally=True,
                )

    return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Unzip + extract inner .dat / .csv
# ──────────────────────────────────────────────────────────────────────────────


def extract_inner(zip_path: Path, dest_dir: Path, extract_kind: str) -> tuple[Path, int]:
    """Extract the canonical inner file (.dat for entity registration, .csv for
    exclusions). Returns (path_to_inner, uncompressed_bytes).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        if extract_kind == "exclusions":
            # SAM publishes both .csv (UEI V2) and .CSV files; prefer V2 (UEI fields).
            csv_names = [n for n in z.namelist() if n.upper().endswith(".CSV")]
            if not csv_names:
                raise RuntimeError(f"no .CSV in ZIP: {z.namelist()}")
            # Prefer V2 variant (has Unique Entity ID column).
            v2 = [n for n in csv_names if "V2" in n.upper()]
            chosen = v2[0] if v2 else csv_names[0]
            log.info("  selected exclusions inner file: %s", chosen)
            target = dest_dir / "exclusions.csv"
        else:
            dat_names = [n for n in z.namelist() if n.lower().endswith(".dat")]
            if not dat_names:
                raise RuntimeError(f"no .dat in ZIP: {z.namelist()}")
            if len(dat_names) > 1:
                log.warning("multiple .dat entries in ZIP — using %s", dat_names[0])
            chosen = dat_names[0]
            target = dest_dir / "entity.dat"
        info = z.getinfo(chosen)
        with z.open(info) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        return target, info.file_size


# ──────────────────────────────────────────────────────────────────────────────
# DuckDB transform
# ──────────────────────────────────────────────────────────────────────────────

# DuckDB SQL macros mirroring scripts/_lib/sam_gov_normalize.py — keep parity
# with the Python reference.
_NORMALIZE_MACROS_SQL = r"""
CREATE MACRO sam_normalize_uei(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(upper(raw), '[^A-Z0-9]', '', 'g')) = 12
      THEN regexp_replace(upper(raw), '[^A-Z0-9]', '', 'g')
    ELSE NULL
  END
);

CREATE MACRO sam_normalize_cage(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(upper(raw), '[^A-Z0-9]', '', 'g')) = 5
      THEN regexp_replace(upper(raw), '[^A-Z0-9]', '', 'g')
    ELSE NULL
  END
);

CREATE MACRO sam_normalize_legal_name(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE NULLIF(
      (
        WITH parts AS (
          SELECT string_split(
            trim(regexp_replace(
              regexp_replace(lower(raw), '[.,&]+', ' ', 'g'),
              '\s+', ' ', 'g'
            )),
            ' '
          ) AS p
        )
        SELECT CASE
          WHEN length(p) >= 2 AND p[length(p)] IN
               ('llc','inc','incorporated','corp','corporation','ltd','limited',
                'lp','llp','pc','pa','pllc','co','company','lc')
          THEN array_to_string(p[1:length(p)-1], ' ')
          ELSE array_to_string(p, ' ')
        END FROM parts
      ),
      ''
    )
  END
);

CREATE MACRO sam_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

CREATE MACRO sam_state(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(trim(raw)) <> 2 THEN NULL
    WHEN regexp_matches(upper(trim(raw)), '^[A-Z]{2}$') THEN upper(trim(raw))
    ELSE NULL
  END
);

CREATE MACRO sam_naics_2digit(raw) AS (
  CASE
    WHEN raw IS NULL OR length(trim(raw)) < 2 THEN NULL
    WHEN NOT regexp_matches(substr(trim(raw), 1, 2), '^[0-9]{2}$') THEN NULL
    ELSE substr(trim(raw), 1, 2)
  END
);
"""


def transform_entity_to_parquet(
    dat_path: Path,
    parquet_path: Path,
    *,
    archive_date: date,
    extract_kind: str,
    schema_version: str,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """Read pipe-delimited .dat as VARCHAR, project all raw columns + add
    normalized + partition columns, write ZSTD Parquet.

    schema_version ∈ {'v2', 'pre_v2'}:
      v2     — 142-cols, UEI in col 1, BOF/EOF banner rows present
      pre_v2 — 120-cols, no UEI (DUNS scrubbed to "No longer available"
               in col 1), no BOF/EOF banners

    Returns (rows_in, rows_pq, presence_rates_for_normalized_keys).
    """
    if schema_version == "pre_v2":
        column_names = SAM_GOV_PRE_V2_DB_COLUMN_NAMES
        first_col = "duns"
        uei_source = "NULL"
        phys_zip_col = "physical_address_zip_postal_code"
        mail_zip_col = "mailing_address_zip_postal_code"
    else:
        column_names = SAM_GOV_DB_COLUMN_NAMES
        first_col = "unique_entity_id"
        uei_source = "unique_entity_id"
        phys_zip_col = "physical_address_zippostal_code"
        mail_zip_col = "mailing_address_zippostal_code"

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    con.execute(_NORMALIZE_MACROS_SQL)

    column_list = ", ".join(f"'{c}'" for c in column_names)
    # Pipe-delimited, NO header. V2 has a BOF/EOF banner; pre-V2 doesn't.
    # all_varchar preserves leading-zero ZIPs and date strings.
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv(
          '{dat_path}',
          delim='|', header=FALSE, quote='',
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE,
          names=[{column_list}]
        );
    """)

    # Filter banner rows. For V2, the first column on banner rows starts
    # with 'BOF ' or 'EOF '. Pre-V2 has no banners — the predicate is a
    # no-op there.
    rows_in = int(con.execute(f"""
        SELECT count(*) FROM raw
        WHERE {first_col} IS NOT NULL
          AND {first_col} NOT LIKE 'BOF %'
          AND {first_col} NOT LIKE 'EOF %';
    """).fetchone()[0])
    log.info("%s   .dat data rows (%s): %s",
             log_prefix, schema_version, f"{rows_in:,}")

    # Build the projection: all raw columns + 10 normalized/partition columns.
    raw_select = ", ".join(f'"{c}"' for c in column_names)
    normalized_select = (
        f'sam_normalize_uei({uei_source}) AS uei_normalized, '
        'sam_normalize_cage(cage_code) AS cage_code_normalized, '
        'sam_normalize_legal_name(legal_business_name) AS legal_business_name_normalized, '
        'sam_normalize_legal_name(dba_name) AS dba_name_normalized, '
        f'sam_zip5({phys_zip_col}) AS physical_address_zip5, '
        'sam_state(physical_address_province_or_state) AS physical_address_state_normalized, '
        f'sam_zip5({mail_zip_col}) AS mailing_address_zip5, '
        'sam_naics_2digit(primary_naics) AS naics_primary_2digit, '
        f"DATE '{archive_date.isoformat()}' AS sam_archive_date, "
        f"'{extract_kind}' AS sam_extract_kind"
    )

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"""
        SELECT {raw_select}, {normalized_select}
        FROM raw
        WHERE {first_col} IS NOT NULL
          AND {first_col} NOT LIKE 'BOF %'
          AND {first_col} NOT LIKE 'EOF %'
        {limit_clause}
    """

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info(
        "%s   parquet write: %.1f MB in %.1fs",
        log_prefix,
        parquet_path.stat().st_size / (1 << 20),
        time.monotonic() - t0,
    )

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE uei_normalized IS NOT NULL) AS uei_present,
          count(*) FILTER (WHERE cage_code_normalized IS NOT NULL) AS cage_present,
          count(*) FILTER (WHERE legal_business_name_normalized IS NOT NULL) AS lbn_present
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0])
    if total == 0:
        rates: dict[str, float] = {
            "uei_normalized_present_pct": 0.0,
            "cage_code_normalized_present_pct": 0.0,
            "legal_business_name_normalized_present_pct": 0.0,
        }
    else:
        rates = {
            "uei_normalized_present_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "cage_code_normalized_present_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "legal_business_name_normalized_present_pct": round(100.0 * int(rates_row[3]) / total, 4),
        }
    log.info(
        "%s   pq rows=%s  uei=%.2f%% cage=%.2f%% lbn=%.2f%%",
        log_prefix, f"{total:,}",
        rates["uei_normalized_present_pct"],
        rates["cage_code_normalized_present_pct"],
        rates["legal_business_name_normalized_present_pct"],
    )
    con.close()
    return rows_in, total, rates


def transform_exclusions_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    archive_date: date,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """Read CSV with header, project all columns + add normalized columns,
    write ZSTD Parquet.

    Returns (rows_in, rows_pq, presence_rates).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='4GB';")
    con.execute(_NORMALIZE_MACROS_SQL)

    # SAM exclusions CSV has UTF-8 with header. Use all_varchar to preserve
    # raw column shapes for the audit trail.
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv(
          '{csv_path}',
          delim=',', header=TRUE, quote='"',
          all_varchar=TRUE, ignore_errors=TRUE,
          null_padding=TRUE
        );
    """)

    # Lowercase all columns for portability (SAM publishes inconsistent capping).
    raw_columns = [c[1] for c in con.execute("DESCRIBE raw;").fetchall()]
    log.info("%s   exclusions CSV columns (%d): %s",
             log_prefix, len(raw_columns), ", ".join(raw_columns))

    rows_in = int(con.execute("SELECT count(*) FROM raw;").fetchone()[0])
    log.info("%s   csv data rows: %s", log_prefix, f"{rows_in:,}")

    # Project all raw columns (lowercased aliases) + add normalized columns.
    # SAM exclusions CSV header naming varies between V2 and legacy variants;
    # detect at runtime which columns are present and build normalized
    # references accordingly.
    raw_lower_alias: dict[str, str] = {}
    for c in raw_columns:
        # DuckDB lowercases unquoted identifiers; we want stable lowercased
        # snake-ish aliases. Strip whitespace + slashes.
        alias = re.sub(r"[^a-z0-9]+", "_",
                       c.lower().replace("/", "_").replace(" ", "_")).strip("_")
        raw_lower_alias[c] = alias

    select_parts: list[str] = [f'"{c}" AS "{raw_lower_alias[c]}"' for c in raw_columns]

    name_cols = [c for c in raw_columns if c.lower() in ("name", "excluded_entity_name")]
    uei_cols = [c for c in raw_columns
                if c.lower() in ("unique entity id", "unique_entity_id", "uei")]
    state_cols = [c for c in raw_columns
                  if c.lower().replace(" ", "_") in ("state_province", "state",
                                                     "state_or_province")]
    zip_cols = [c for c in raw_columns
                if c.lower().replace(" ", "_") in ("zip_code", "zip", "zippostal_code")]
    program_cols = [c for c in raw_columns if c.lower() == "exclusion program"]
    classification_cols = [c for c in raw_columns
                            if c.lower() == "classification"]

    def first(cols: list[str]) -> str:
        return f'"{cols[0]}"' if cols else "NULL"

    select_parts.extend([
        f'sam_normalize_legal_name({first(name_cols)}) AS excluded_entity_name_normalized',
        f'sam_normalize_uei({first(uei_cols)}) AS excluded_uei_normalized',
        f'sam_zip5({first(zip_cols)}) AS excluded_address_zip5',
        f'sam_state({first(state_cols)}) AS excluded_address_state_normalized',
        f'upper(trim({first(program_cols)})) AS exclusion_program_normalized',
        f'{first(classification_cols)} AS exclusion_classification_type',
        f"DATE '{archive_date.isoformat()}' AS sam_exclusions_snapshot_date",
    ])

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info(
        "%s   parquet write: %.1f MB in %.1fs",
        log_prefix,
        parquet_path.stat().st_size / (1 << 20),
        time.monotonic() - t0,
    )

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE excluded_uei_normalized IS NOT NULL) AS uei_present,
          count(*) FILTER (WHERE excluded_entity_name_normalized IS NOT NULL) AS name_present
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0])
    rates = {
        "uei_normalized_present_pct": round(100.0 * int(rates_row[1]) / total, 4) if total else 0.0,
        "cage_code_normalized_present_pct": 0.0,  # exclusions CSV has no cage
        "legal_business_name_normalized_present_pct": (
            round(100.0 * int(rates_row[2]) / total, 4) if total else 0.0
        ),
    }
    log.info(
        "%s   exclusions pq rows=%s  uei_present=%.2f%% name_present=%.2f%%",
        log_prefix, f"{total:,}",
        rates["uei_normalized_present_pct"],
        rates["legal_business_name_normalized_present_pct"],
    )
    con.close()
    return rows_in, total, rates


# ──────────────────────────────────────────────────────────────────────────────
# R2 + audit
# ──────────────────────────────────────────────────────────────────────────────


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


def get_prior_sha256(conn: psycopg.Connection, sl: Slice) -> tuple[str | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_zip_sha256, source_last_modified
              FROM ops.sam_gov_r2_ingest_runs
             WHERE extract_kind = %s AND archive_date = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (sl.extract_kind, sl.archive_date),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def insert_run_row(
    conn: psycopg.Connection,
    sl: Slice,
    *,
    rf: ResolvedFile,
    prior_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.sam_gov_r2_ingest_runs (
        extract_kind, archive_date, archive_label,
        status, source_url, source_filename,
        source_zip_bytes, source_zip_sha256,
        staged_locally, prior_source_last_modified
    ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            sl.extract_kind, sl.archive_date, sl.archive_label,
            rf.source_url, rf.source_filename,
            rf.bytes_size, rf.sha256,
            rf.staged_locally, prior_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def write_no_change_run(
    conn: psycopg.Connection,
    sl: Slice,
    *,
    rf: ResolvedFile,
    prior_sha256: str | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.sam_gov_r2_ingest_runs (
                extract_kind, archive_date, archive_label,
                status, source_url, source_filename,
                source_zip_bytes, source_zip_sha256,
                staged_locally,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, %s, 'no_change', %s, %s, %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                sl.extract_kind, sl.archive_date, sl.archive_label,
                rf.source_url, rf.source_filename,
                rf.bytes_size, rf.sha256, rf.staged_locally,
                started, started,
                Jsonb({
                    "reason": "source_zip_sha256 unchanged",
                    "prior_sha256": prior_sha256,
                }),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    inner_bytes: int,
    inner_rows: int,
    parquet_rows: int,
    parquet_bytes: int,
    parquet_columns: int,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_total_bytes: int,
    rates: dict[str, float] | None,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    r2_object_count = 1 if r2_bucket else 0
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.sam_gov_r2_ingest_runs
               SET status = %s,
                   dat_or_csv_uncompressed_bytes = %s,
                   dat_or_csv_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_part_count = 1,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s,
                   r2_object_count = %s,
                   r2_total_bytes = %s,
                   uei_normalized_present_pct = %s,
                   cage_code_normalized_present_pct = %s,
                   legal_business_name_normalized_present_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, inner_bytes, inner_rows,
            parquet_rows, parquet_bytes, parquet_columns,
            r2_bucket, r2_prefix, r2_object_count, r2_total_bytes,
            (rates or {}).get("uei_normalized_present_pct"),
            (rates or {}).get("cage_code_normalized_present_pct"),
            (rates or {}).get("legal_business_name_normalized_present_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Per-slice main
# ──────────────────────────────────────────────────────────────────────────────


def ingest_slice(
    sl: Slice,
    rf: ResolvedFile,
    *,
    skip_if_unchanged: bool,
    workdir: Path,
    max_rows: int | None,
) -> int:
    log_prefix = f"[{sl.extract_kind} / {sl.archive_label}]"
    started_wall = time.monotonic()
    log.info("%s start file=%s sha=%s",
             log_prefix, rf.local_path, rf.sha256[:12])

    with psycopg.connect(_database_url()) as conn:
        prior_sha, prior_lm = get_prior_sha256(conn, sl)
        if skip_if_unchanged and prior_sha is not None and prior_sha == rf.sha256:
            log.info("%s sha256 unchanged — recording no_change", log_prefix)
            write_no_change_run(conn, sl, rf=rf, prior_sha256=prior_sha)
            return 0

        run_id = insert_run_row(conn, sl, rf=rf, prior_last_modified=prior_lm)
        log.info("%s run id: %s", log_prefix, run_id)

        extract_dir = workdir / f"sam_{sl.extract_kind}_{sl.archive_label}"
        parquet_path = workdir / f"sam_{sl.extract_kind}_{sl.archive_label}.parquet"

        try:
            inner_path, inner_bytes = extract_inner(
                rf.local_path, extract_dir, sl.extract_kind,
            )
            log.info(
                "%s extracted inner (%.1f MB uncompressed)",
                log_prefix, inner_bytes / (1 << 20),
            )

            if sl.extract_kind == "exclusions":
                rows_in, rows_pq, rates = transform_exclusions_to_parquet(
                    inner_path, parquet_path,
                    archive_date=sl.archive_date,
                    log_prefix=log_prefix, max_rows=max_rows,
                )
            else:
                rows_in, rows_pq, rates = transform_entity_to_parquet(
                    inner_path, parquet_path,
                    archive_date=sl.archive_date,
                    extract_kind=sl.extract_kind,
                    schema_version=sl.schema_version,
                    log_prefix=log_prefix, max_rows=max_rows,
                )

            uploaded = upload_to_r2(
                parquet_path, bucket=R2_BUCKET, key=sl.r2_object_key,
            )
            log.info(
                "%s uploaded → s3://%s/%s (%.1f MB)",
                log_prefix, R2_BUCKET, sl.r2_object_key, uploaded / (1 << 20),
            )

            # Column counts: V2 entity = 142 raw + 10 normalized = 152.
            #              pre-V2 entity = 120 raw + 10 normalized = 130.
            #              exclusions = N raw + 7 normalized.
            if sl.extract_kind == "exclusions":
                column_count = _exclusions_pq_column_count(parquet_path)
            elif sl.schema_version == "pre_v2":
                column_count = 130
            else:
                column_count = 152

            finalize_run_row(
                conn, run_id, status="completed",
                inner_bytes=inner_bytes,
                inner_rows=rows_in,
                parquet_rows=rows_pq,
                parquet_bytes=uploaded,
                parquet_columns=column_count,
                r2_bucket=R2_BUCKET,
                r2_prefix=sl.r2_prefix,
                r2_total_bytes=uploaded,
                rates=rates,
                started_at=started_wall, error_message=None,
                notes={
                    "max_rows": max_rows,
                    "staged_locally": rf.staged_locally,
                    "source_url": rf.source_url,
                    "r2_object_key": sl.r2_object_key,
                    "schema_version": sl.schema_version,
                },
            )
            log.info(
                "%s DONE rows=%s upload=%.1f MB wall=%.1fs",
                log_prefix, f"{rows_pq:,}",
                uploaded / (1 << 20),
                time.monotonic() - started_wall,
            )
            return 0

        except Exception as exc:
            log.exception("%s ingest failed", log_prefix)
            finalize_run_row(
                conn, run_id, status="failed",
                inner_bytes=0, inner_rows=0,
                parquet_rows=0, parquet_bytes=0, parquet_columns=0,
                r2_bucket=None, r2_prefix=None, r2_total_bytes=0,
                rates=None,
                started_at=started_wall,
                error_message=str(exc), notes=None,
            )
            return 1
        finally:
            try:
                parquet_path.unlink(missing_ok=True)
            except Exception:
                pass
            shutil.rmtree(extract_dir, ignore_errors=True)


def _exclusions_pq_column_count(p: Path) -> int:
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}');").fetchall()
        return len(rows)
    finally:
        con.close()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind", choices=("monthly", "historical", "exclusions"),
                   help="Extract kind to ingest.")
    p.add_argument("--archive", default=None,
                   help="Archive label: 'YYYY-MAY'/'YYYY-NOV' for historical, "
                        "'YYYY-MM-DD' for monthly/exclusions.")
    p.add_argument("--all", action="store_true",
                   help="For --kind historical, ingest all 12 archives.")
    p.add_argument("--discover", action="store_true",
                   help="List candidate URLs + check direct-download status; do nothing else.")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--staging-dir", default=DEFAULT_STAGING_DIR)
    p.add_argument("--workdir", default="/tmp/sam_gov_r2_ingest")
    p.add_argument("--skip-direct", action="store_true",
                   help="Skip direct-download attempts; go straight to staging dir lookup.")
    return p.parse_args()


def emit_operator_action_summary(missing: list[Slice], staging_dir: Path) -> None:
    print()
    print("=" * 72)
    print("OPERATOR ACTION REQUIRED")
    print("=" * 72)
    print()
    print("Direct download from SAM.gov returned 401/403/404 for the ZIPs below.")
    print("This is expected — SAM.gov gates Public V2 + Historical bulk extracts")
    print("behind a logged-in browser session. The Extracts API has a 10/day rate")
    print("cap and is explicitly OUT OF SCOPE for this directive.")
    print()
    print(f"Working directory (place files here): {staging_dir}")
    print()
    print("Files needed:")
    for sl in missing:
        print(f"  [{sl.extract_kind:20}] {sl.archive_label:12}")
        for fn, url in sl.candidate_urls:
            print(f"    expected name: {fn}")
            print(f"    expected url:  {url}")
        print()
    print("Once staged, re-invoke with --skip-direct (or just re-run; the script")
    print("re-attempts direct download then falls back to the staging dir).")
    print("=" * 72)


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(args.staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Build slice list.
    slices: list[Slice] = []
    if args.kind == "historical":
        if args.all:
            slices = build_historical_slices()
        elif args.archive:
            yr, half = args.archive.split("-")
            slices = [s for s in build_historical_slices()
                      if s.archive_label == f"{yr}-{half.upper()}"]
            if not slices:
                log.error("no historical slice matches %s", args.archive)
                return 2
        else:
            log.error("--kind historical requires --archive YYYY-MAY|NOV or --all")
            return 2
    elif args.kind == "monthly":
        if not args.archive:
            log.error("--kind monthly requires --archive YYYY-MM-DD")
            return 2
        slices = [build_monthly_slice(args.archive)]
    elif args.kind == "exclusions":
        slices = [build_exclusions_slice(args.archive)]
    else:
        if not args.discover:
            log.error("must pass --kind or --discover")
            return 2
        # Discover-only: show all kinds.
        slices = build_historical_slices() + [
            build_exclusions_slice(None),
        ]

    # Resolve each slice (direct download → staging lookup).
    resolved: list[tuple[Slice, ResolvedFile]] = []
    missing: list[Slice] = []
    with httpx.Client(headers={"User-Agent": "data-engine-x/sam-r2-ingest"}) as client:
        for sl in slices:
            rf = resolve_slice(
                sl, client=client,
                staging_dir=staging_dir, workdir=workdir,
                skip_direct=args.skip_direct,
            )
            if rf is None:
                missing.append(sl)
                log.warning("[%s / %s] not found via direct download or staging dir",
                            sl.extract_kind, sl.archive_label)
            else:
                resolved.append((sl, rf))

    if args.discover:
        print(f"\nResolved: {len(resolved)} slices")
        for sl, rf in resolved:
            print(f"  ✓ {sl.extract_kind:20} {sl.archive_label:12} "
                  f"{'staged' if rf.staged_locally else 'direct'} "
                  f"{rf.local_path}")
        if missing:
            emit_operator_action_summary(missing, staging_dir)
        return 0

    if missing:
        emit_operator_action_summary(missing, staging_dir)
        return 42

    rc = 0
    for sl, rf in resolved:
        rc_one = ingest_slice(
            sl, rf,
            skip_if_unchanged=args.skip_if_unchanged,
            workdir=workdir,
            max_rows=args.max_rows,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("[%s / %s] failed; continuing",
                      sl.extract_kind, sl.archive_label)
    return rc


if __name__ == "__main__":
    sys.exit(main())
