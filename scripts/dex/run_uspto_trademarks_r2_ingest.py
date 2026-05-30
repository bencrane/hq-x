#!/usr/bin/env python3
"""USPTO Trademark Case Files Dataset (TCFD) → R2 ZSTD Parquet ingest.

Pulls the publication-year ZIP (full historical 1870-present), extracts the
five critical relational CSVs, partitions output by case-file filing year,
and writes one ZSTD Parquet per (case_file_year, table) to R2 at:

  s3://dex-raw-landing-zone/uspto-trademarks/year={YYYY}/<table>.parquet

Five tables:
  case_file                  — one row per trademark application (PK serial_number)
  case_file_owner            — owners + applicants (names + addresses; identity gold)
  classification             — Nice/US class assignments
  case_file_statement        — mark text + descriptions
  case_file_event_statement  — case-file event history (status changes)

Source URL convention (verified 2026-05-08): the USPTO Open Data Portal
serves an annual snapshot ZIP at
  https://data.uspto.gov/ui/datasets/products/files/TRCFECO2/{YEAR}/csv.zip
where YEAR is the publication vintage (2011..2023 available; 2023 covers Oct
1870 – Mar 2024 as the latest publish at directive write-time).

NOTE: The Open Data Portal URL is gated by an AWS WAF JS challenge —
direct curl/httpx/requests downloads return a 20 KB SPA HTML wrapper
instead of the ZIP. The script supports a `--source-zip` local-path mode so
the operator can browser-download once and feed the ZIP in. The HTTP-mode
path is wired but will fail until USPTO offers a non-WAF-gated mirror or
the operator configures a browser-cookie injection.

Audit ledger: ops.uspto_trademarks_r2_ingest_runs (one row per partition × table).
Idempotency: per (publication_year, case_file_year, table) — re-running with the
same publication_year is a no-op when prior runs are 'completed'.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_uspto_trademarks_r2_ingest.py \\
      --publication-year 2023 \\
      --source-zip /tmp/TRCFECO2_2023_csv.zip
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_uspto_trademarks_r2_ingest.py \\
      --publication-year 2023 \\
      --source-zip /tmp/TRCFECO2_2023_csv.zip \\
      --max-rows 50000 \\
      --r2-prefix-override 'uspto-trademarks/_smoke/'
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_uspto_trademarks_r2_ingest.py \\
      --publication-year 2023 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb

# scripts/_lib/uspto_normalize.py is the canonical Python implementation of
# the normalizers. The DuckDB SQL fragments below mirror that algorithm
# (registered functions weren't viable: DuckDB scalar UDFs require numpy,
# which is not in the runtime image). Unit tests in
# tests/unit/test_uspto_normalize.py exercise the Python form; smoke tests
# of this script exercise the SQL form on shared cases.


R2_BUCKET = "dex-raw-landing-zone"
DEFAULT_PUBLICATION_YEAR = 2023
DEFAULT_FILING_YEAR_FLOOR = 1870
DEFAULT_FILING_YEAR_CEILING = 2025  # generous; future-proof one year forward

URL_TEMPLATE = (
    "https://data.uspto.gov/ui/datasets/products/files/TRCFECO2/{year}/csv.zip"
)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# DuckDB tuning for the heavy 4–10 GB CSVs (event ~1.13 GB compressed → ~7 GB
# decompressed → 100 M rows). Memory limit lets DuckDB spill to disk for the
# event/statement tables on a 16 GB box.
DUCKDB_MEMORY_LIMIT = "8GB"
DUCKDB_THREADS = 4


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("uspto-tm-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Per-table configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TableConfig:
    output_name: str        # Output Parquet basename + audit table_name value
    csv_basenames: tuple[str, ...]  # Possible CSV file basenames in the ZIP

    @property
    def parquet_filename(self) -> str:
        return f"{self.output_name}.parquet"


# csv_basenames lists candidate filenames inside the publication ZIP. USPTO
# publishes some tables with a `case_file_` prefix and others not (e.g. the
# 2023 release publishes `event.csv` rather than `case_file_event.csv`); we
# probe both forms and use the first that resolves.
TABLE_CASE_FILE = TableConfig(
    output_name="case_file",
    csv_basenames=("case_file.csv",),
)
TABLE_OWNER = TableConfig(
    output_name="case_file_owner",
    csv_basenames=("owner.csv", "case_file_owner.csv"),
)
TABLE_CLASSIFICATION = TableConfig(
    output_name="classification",
    csv_basenames=("classification.csv",),
)
TABLE_STATEMENT = TableConfig(
    output_name="case_file_statement",
    csv_basenames=("statement.csv", "case_file_statement.csv"),
)
TABLE_EVENT = TableConfig(
    output_name="case_file_event_statement",
    csv_basenames=("event.csv", "case_file_event_statement.csv"),
)
TABLE_CORRESPONDENT = TableConfig(
    output_name="correspondent_domrep_attorney",
    csv_basenames=("correspondent_domrep_attorney.csv",),
)

# case_file MUST come first — the related tables join to its serial_number→year
# lookup. Don't reorder.
TABLES: tuple[TableConfig, ...] = (
    TABLE_CASE_FILE,
    TABLE_OWNER,
    TABLE_CLASSIFICATION,
    TABLE_STATEMENT,
    TABLE_EVENT,
    TABLE_CORRESPONDENT,
)


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# Source resolution: HTTP URL or local-path
# --------------------------------------------------------------------------- #


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None, int]:
    """HEAD the publication ZIP. NOTE: the Open Data Portal returns a 20 KB
    HTML SPA wrapper rather than the actual ZIP unless a JS-solved AWS WAF
    challenge cookie is supplied. We log content-type loudly so the operator
    sees what we got."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code == 404:
                return None, None, 404
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            cl_raw = r.headers.get("content-length")
            cl = int(cl_raw) if cl_raw and cl_raw.isdigit() else None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(
                        lm_raw, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            log.info("  HEAD content-type=%s content-length=%s last-modified=%s",
                     ct, cl, lm)
            if "text/html" in ct.lower():
                log.warning(
                    "  HEAD returned text/html (%s bytes) — likely the Open Data "
                    "Portal SPA wrapper. The actual ZIP is gated by an AWS WAF JS "
                    "challenge. Use --source-zip <local-path> to feed a "
                    "browser-downloaded ZIP instead.", cl,
                )
            return cl, lm, r.status_code
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


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
                        if now - last_log >= 10.0:
                            log.info(
                                "  download progress: %.1f MB written",
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


def _unwrap_zip(zip_path: Path, dest_dir: Path,
                out: dict[str, Path], indent: str = "  ") -> None:
    """Extract one ZIP into dest_dir; nested .csv.zip files are recursively unwrapped."""
    with zipfile.ZipFile(zip_path) as outer:
        for member in outer.namelist():
            if member.endswith("/"):
                continue
            base = Path(member).name
            extracted = dest_dir / base
            with outer.open(member) as src, extracted.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            log.info("%sextracted %s (%s bytes)", indent, base, extracted.stat().st_size)
            if base.lower().endswith(".csv.zip"):
                _unwrap_zip(extracted, dest_dir, out, indent + "  ")
                extracted.unlink(missing_ok=True)
            elif base.lower().endswith(".csv"):
                out[base.lower()] = extracted


def extract_csvs(sources: list[Path], dest_dir: Path) -> dict[str, Path]:
    """Materialize all USPTO TCFD CSVs into dest_dir. Sources can be any mix of:

      - a single bundled ZIP (e.g. csv.zip) — recursively unwrapped
      - one or more per-table .csv.zip files
      - one or more raw .csv files (copied verbatim)
      - a directory — walked for .csv / .csv.zip / .zip; subdirs scanned

    Returns {basename_lower: extracted_path}.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}

    def _ingest_one(p: Path) -> None:
        if p.is_dir():
            for child in sorted(p.iterdir()):
                _ingest_one(child)
            return
        if not p.is_file():
            return
        name = p.name.lower()
        if name.endswith(".csv.zip") or name.endswith(".zip"):
            log.info("opening %s (%.1f MB)", p, p.stat().st_size / (1 << 20))
            _unwrap_zip(p, dest_dir, out)
        elif name.endswith(".csv"):
            target = dest_dir / p.name
            if target.resolve() != p.resolve():
                shutil.copyfile(p, target)
            log.info("  staged raw csv %s (%s bytes)",
                     p.name, target.stat().st_size)
            out[p.name.lower()] = target
        # else: ignore (.dta, .pdf, README, etc.)

    for src in sources:
        _ingest_one(src)
    return out


def resolve_csv(
    extracted: dict[str, Path], cfg: TableConfig
) -> Path:
    for cand in cfg.csv_basenames:
        p = extracted.get(cand.lower())
        if p is not None:
            return p
    raise RuntimeError(
        f"None of the candidate CSVs {cfg.csv_basenames!r} found in extract; "
        f"available={sorted(extracted)}"
    )


# --------------------------------------------------------------------------- #
# DuckDB transform: case_file (the year-derivation pivot)
# --------------------------------------------------------------------------- #


_FILING_YEAR_REGEX_NOTE = """
USPTO TCFD's filing_dt is published in 'YYYY-MM-DD' format in modern releases
and 'YYYYMMDD' (no separator) in older ones. Some pre-1900 records use
'0000-00-00' as a sentinel for unknown dates. We extract a 4-digit year via
regex and clamp to [floor, ceiling] to drop the obvious bogus values.
"""


def _filing_year_sql(col: str) -> str:
    """SQL fragment that extracts a SMALLINT year from a varchar date col."""
    return (
        f"CASE WHEN regexp_matches({col}, '^[0-9]{{4}}') "
        f"  THEN TRY_CAST(substr({col}, 1, 4) AS SMALLINT) "
        f"  ELSE NULL END"
    )


# Trailing corporate-form alternation. Multi-token forms come first so RE2
# prefers the longest match per iteration (RE2 is leftmost-first within an
# alternation). Mirrors _CORPORATE_SUFFIXES in scripts/_lib/uspto_normalize.py.
_OWNER_SUFFIX_ALT = (
    "l l l p|p l l c|p t e ltd|s p a|s r l|"
    "l l c|l l p|p l c|s a|a g|b v|n v|k k|k g|p t e|"
    "incorporated|corporation|partnership|association|limited|company|"
    "trust|llc|lllc|llp|lllp|inc|corp|ltd|lp|pllc|plc|pc|pa|co|gmbh|ag|sa|"
    "nv|bv|kk|kg|oy|ab|as|spa|srl"
)


def _normalize_owner_name_sql(col_expr: str) -> str:
    """Emit SQL that mirrors uspto_normalize.normalize_owner_name on `col_expr`.

    Steps:
      1. lower()
      2. replace any non-[a-z0-9] with ' '
      3. trim + collapse adjacent spaces
      4. strip one OR MORE trailing ` <suffix>` tokens in a single regex pass.
         The `+` after the outer group makes RE2 iteratively consume stacked
         suffixes (e.g. "acme holdings inc llc" → "acme holdings"), and
         anchoring to end-of-string ensures we don't strip mid-string suffix
         tokens (e.g. "beta corp holdings inc" should keep "corp holdings").
      5. trim again, NULLIF empty.
    """
    return f"""NULLIF(
        trim(regexp_replace(
            trim(regexp_replace(
                lower({col_expr}), '[^a-z0-9]+', ' ', 'g'
            )),
            '( ({_OWNER_SUFFIX_ALT}))+ *$',
            '',
            'g'
        )),
        ''
    )"""


def _zip5_sql(col_expr: str) -> str:
    return f"""CASE
        WHEN length(trim({col_expr})) >= 5
         AND regexp_matches(substr(trim({col_expr}), 1, 5), '^[0-9]{{5}}$')
        THEN substr(trim({col_expr}), 1, 5)
        ELSE NULL
    END"""


def _normalize_state_sql(col_expr: str) -> str:
    return f"""CASE
        WHEN length(trim({col_expr})) BETWEEN 1 AND 3
        THEN upper(trim({col_expr}))
        ELSE NULL
    END"""


def _normalize_country_sql(col_expr: str) -> str:
    return f"NULLIF(upper(trim({col_expr})), '')"


def _classify_owner_kind_sql(
    code_col: str | None, name_col: str
) -> str:
    """SQL CASE expression mirroring uspto_normalize.classify_owner_kind.

    Code lookups come first; falls back to name-suffix inspection.
    """
    name_expr = f'lower("{name_col}")'
    code_branches = ""
    if code_col is not None:
        # USPTO TCFD `own_entity_cd` uses single-digit codes for the most
        # common kinds and 2- or 3-digit codes for less-common foreign forms.
        # We accept both single-digit and zero-padded variants ('1' / '01')
        # for portability across older 2-char releases. Codes outside this
        # confident map fall through to the name-suffix branch (which catches
        # LLC/Inc/Corp/Ltd/GmbH/AG/PLC/etc. on the name itself).
        code_branches = f"""
            WHEN trim("{code_col}") IN ('1', '01') THEN 'individual'
            WHEN trim("{code_col}") IN ('2', '02') THEN 'individual'
            WHEN trim("{code_col}") IN ('3', '03') THEN 'corporation'
            WHEN trim("{code_col}") IN ('4', '04') THEN 'partnership'
            WHEN trim("{code_col}") IN ('5', '05') THEN 'partnership'
            WHEN trim("{code_col}") IN ('6', '06') THEN 'partnership'
            WHEN trim("{code_col}") IN ('7', '07') THEN 'trust'
            WHEN trim("{code_col}") IN ('8', '08') THEN 'llc'
            WHEN trim("{code_col}") = '9' THEN 'government'
            WHEN trim("{code_col}") = '10' THEN 'association'
            WHEN trim("{code_col}") IN ('11', '12') THEN 'corporation'
            WHEN trim("{code_col}") = '13' THEN 'partnership'
        """
    # Suffix patterns: anchor near end-of-string. \\W matches non-word char.
    return f"""CASE
        {code_branches}
        WHEN regexp_matches({name_expr},
            '(limited liability company)[^a-z0-9]*$') THEN 'llc'
        WHEN regexp_matches({name_expr},
            '(limited liability partnership|limited partnership|partnership|llp|lllp|lp)[^a-z0-9]*$') THEN 'partnership'
        WHEN regexp_matches({name_expr},
            '(llc|l\\.l\\.c)[^a-z0-9]*$') THEN 'llc'
        WHEN regexp_matches({name_expr},
            '(incorporated|corporation|inc|corp|limited|ltd|plc|gmbh|ag|sa)[^a-z0-9]*$') THEN 'corporation'
        WHEN regexp_matches({name_expr}, '(trust)[^a-z0-9]*$') THEN 'trust'
        WHEN regexp_matches({name_expr}, '(association)[^a-z0-9]*$') THEN 'association'
        WHEN position(',' in "{name_col}") > 0 THEN 'individual'
        ELSE 'unknown'
    END"""


def transform_case_file(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    *,
    out_dir: Path,
    publication_year: int,
    floor: int,
    ceiling: int,
    log_prefix: str,
    max_rows: int | None,
) -> dict[int, tuple[Path, int]]:
    """Read case_file CSV, derive case_file_year, write one Parquet per year.

    Side effect: registers a `tmp_case_file_year` view (serial_number→year)
    that subsequent transforms join against.

    Returns {year: (parquet_path, row_count)}.
    """
    log.info("%s reading case_file CSV", log_prefix)
    con.execute(f"""
        CREATE OR REPLACE VIEW raw_case_file AS
        SELECT * FROM read_csv_auto('{csv_path}', all_varchar=TRUE,
                                    sample_size=-1, header=TRUE,
                                    ignore_errors=TRUE);
    """)
    cols_info = con.execute("DESCRIBE raw_case_file;").fetchall()
    csv_cols = [c[0] for c in cols_info]
    log.info("%s case_file columns: %d", log_prefix, len(csv_cols))

    # Heuristically locate the filing-date and serial-number columns.
    serial_col = _pick_col(csv_cols, ("serial_no", "serial_number"))
    filing_col = _pick_col(csv_cols, ("filing_dt", "filing_date"))
    mark_col = _pick_col(csv_cols, ("mark_id_char", "mark_text", "mark_id"))
    log.info(
        "%s detected serial=%s filing=%s mark=%s",
        log_prefix, serial_col, filing_col, mark_col,
    )

    # Build the projection. All raw cols preserved, plus normalized + partition.
    select_parts: list[str] = []
    for col in csv_cols:
        select_parts.append(f'"{col}"')
    select_parts.append(
        f'TRY_CAST("{serial_col}" AS BIGINT) AS serial_number_bigint'
    )
    select_parts.append(
        f"{_filing_year_sql(f'\"{filing_col}\"')} AS case_file_year_raw"
    )
    if mark_col is not None:
        # Mark-text normalization: pure SQL via DuckDB's regexp_replace +
        # lower. Equivalent to scripts/_lib/uspto_normalize.normalize_mark_text.
        select_parts.append(
            f"NULLIF(trim(regexp_replace(lower(\"{mark_col}\"), "
            f"'[^a-z0-9]+', ' ', 'g')), '') AS mark_text_normalized"
        )
    else:
        select_parts.append("NULL AS mark_text_normalized")
    select_parts.append(f"CAST({publication_year} AS SMALLINT) AS publication_year")

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    con.execute(f"""
        CREATE OR REPLACE TABLE projected_case_file AS
        SELECT {', '.join(select_parts)},
               CASE WHEN case_file_year_raw IS NULL THEN NULL
                    WHEN case_file_year_raw < {floor} THEN NULL
                    WHEN case_file_year_raw > {ceiling} THEN NULL
                    ELSE case_file_year_raw END AS case_file_year
        FROM raw_case_file
        {limit_clause};
    """)

    # Build a small (serial_number, case_file_year) lookup view for the
    # related-table joins.
    con.execute(f"""
        CREATE OR REPLACE VIEW tmp_case_file_year AS
        SELECT "{serial_col}" AS serial_number, case_file_year
        FROM projected_case_file
        WHERE case_file_year IS NOT NULL;
    """)

    years_rows = con.execute("""
        SELECT case_file_year, count(*)
          FROM projected_case_file
         WHERE case_file_year IS NOT NULL
         GROUP BY case_file_year
         ORDER BY case_file_year;
    """).fetchall()
    log.info("%s case_file rows by year: %d distinct years",
             log_prefix, len(years_rows))

    # Write one Parquet per year. Drop case_file_year_raw scratch column.
    out: dict[int, tuple[Path, int]] = {}
    for year, _ in years_rows:
        pq = out_dir / f"case_file_year={year}_case_file.parquet"
        con.execute(f"""
            COPY (
              SELECT * EXCLUDE (case_file_year_raw)
                FROM projected_case_file
               WHERE case_file_year = {year}
            ) TO '{pq}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
        """)
        rc = con.execute(
            f"SELECT count(*) FROM read_parquet('{pq}');"
        ).fetchone()
        out[year] = (pq, int(rc[0]) if rc else 0)
    return out


# --------------------------------------------------------------------------- #
# DuckDB transform: owner table (the identity gold)
# --------------------------------------------------------------------------- #


def transform_owner(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    *,
    out_dir: Path,
    publication_year: int,
    log_prefix: str,
    max_rows: int | None,
) -> dict[int, tuple[Path, int]]:
    log.info("%s reading owner CSV", log_prefix)
    con.execute(f"""
        CREATE OR REPLACE VIEW raw_owner AS
        SELECT * FROM read_csv_auto('{csv_path}', all_varchar=TRUE,
                                    sample_size=-1, header=TRUE,
                                    ignore_errors=TRUE);
    """)
    cols_info = con.execute("DESCRIBE raw_owner;").fetchall()
    csv_cols = [c[0] for c in cols_info]
    log.info("%s owner columns: %d", log_prefix, len(csv_cols))

    serial_col = _pick_col(csv_cols, ("serial_no", "serial_number"))
    # USPTO TCFD owner-table columns are own_* prefixed (own_name, own_entity_cd,
    # own_addr_postal, etc.). Older or alternate releases may use unprefixed
    # names — preserve those as fallbacks.
    name_col = _pick_col(
        csv_cols,
        ("own_name", "party_name", "owner_name", "name"),
    )
    entity_type_col = _pick_col(
        csv_cols,
        (
            "own_entity_cd",
            "legal_entity_type_code",
            "owner_legal_entity_type_code",
            "entity_type_code",
        ),
    )
    postal_col = _pick_col(
        csv_cols,
        ("own_addr_postal", "postcode", "owner_postcode", "zip"),
    )
    state_col = _pick_col(
        csv_cols,
        ("own_addr_state_cd", "state", "owner_state"),
    )
    country_col = _pick_col(
        csv_cols,
        (
            "own_nalty_country_cd",
            "own_addr_country_cd",
            "nationality_country",
            "country",
            "owner_country",
        ),
    )
    log.info(
        "%s detected serial=%s name=%s entity_type=%s postal=%s state=%s country=%s",
        log_prefix, serial_col, name_col, entity_type_col, postal_col, state_col, country_col,
    )

    select_parts: list[str] = [f'"{c}"' for c in csv_cols]
    if name_col:
        select_parts.append(
            f"{_normalize_owner_name_sql('\"%s\"' % name_col)} AS owner_name_normalized"
        )
    else:
        select_parts.append("NULL AS owner_name_normalized")
    if postal_col:
        select_parts.append(f"{_zip5_sql('\"%s\"' % postal_col)} AS owner_zip5")
    else:
        select_parts.append("NULL AS owner_zip5")
    if state_col:
        select_parts.append(
            f"{_normalize_state_sql('\"%s\"' % state_col)} AS owner_state_normalized"
        )
    else:
        select_parts.append("NULL AS owner_state_normalized")
    if country_col:
        select_parts.append(
            f"{_normalize_country_sql('\"%s\"' % country_col)} AS owner_country_normalized"
        )
    else:
        select_parts.append("NULL AS owner_country_normalized")
    if entity_type_col and name_col:
        select_parts.append(
            f"{_classify_owner_kind_sql(entity_type_col, name_col)} AS owner_kind_normalized"
        )
    elif name_col:
        select_parts.append(
            f"{_classify_owner_kind_sql(None, name_col)} AS owner_kind_normalized"
        )
    else:
        select_parts.append("'unknown' AS owner_kind_normalized")
    select_parts.append(f"CAST({publication_year} AS SMALLINT) AS publication_year")

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    join_sql = f"""
        SELECT {', '.join(select_parts)}, y.case_file_year
          FROM raw_owner r
          LEFT JOIN tmp_case_file_year y
                 ON r."{serial_col}" = y.serial_number
         {limit_clause}
    """

    con.execute(f"""
        CREATE OR REPLACE TABLE projected_owner AS
        {join_sql};
    """)

    return _write_per_year_parquets(
        con,
        source_table="projected_owner",
        out_dir=out_dir,
        output_name=TABLE_OWNER.output_name,
        log_prefix=log_prefix,
    )


# --------------------------------------------------------------------------- #
# Generic per-year writer for the related tables
# --------------------------------------------------------------------------- #


def transform_related(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    cfg: TableConfig,
    *,
    out_dir: Path,
    publication_year: int,
    log_prefix: str,
    max_rows: int | None,
) -> dict[int, tuple[Path, int]]:
    """Pass-through transform for classification / statement / event /
    correspondent_domrep_attorney: JOIN the table to the case_file year-lookup,
    write per-year Parquets. For correspondent_domrep_attorney, also derive
    `attorney_name_normalized` and `domestic_rep_name_normalized` (same
    suffix-stripping algorithm as owner names — captures attorney-firm joins
    against the FEC/SBA/etc. corpus the same way owner names do)."""
    log.info("%s reading %s CSV", log_prefix, cfg.output_name)
    con.execute(f"""
        CREATE OR REPLACE VIEW raw_related AS
        SELECT * FROM read_csv_auto('{csv_path}', all_varchar=TRUE,
                                    sample_size=-1, header=TRUE,
                                    ignore_errors=TRUE);
    """)
    cols_info = con.execute("DESCRIBE raw_related;").fetchall()
    csv_cols = [c[0] for c in cols_info]
    log.info("%s %s columns: %d", log_prefix, cfg.output_name, len(csv_cols))

    serial_col = _pick_col(csv_cols, ("serial_no", "serial_number"))
    log.info("%s detected serial=%s", log_prefix, serial_col)

    select_parts: list[str] = [f'r."{c}"' for c in csv_cols]
    # Correspondent table specific normalizations.
    if cfg is TABLE_CORRESPONDENT:
        attorney_col = _pick_col(csv_cols, ("attorney_name",))
        domrep_col = _pick_col(csv_cols, ("domestic_rep_name",))
        if attorney_col:
            select_parts.append(
                f"{_normalize_owner_name_sql('r.\"%s\"' % attorney_col)} "
                "AS attorney_name_normalized"
            )
        else:
            select_parts.append("NULL AS attorney_name_normalized")
        if domrep_col:
            select_parts.append(
                f"{_normalize_owner_name_sql('r.\"%s\"' % domrep_col)} "
                "AS domestic_rep_name_normalized"
            )
        else:
            select_parts.append("NULL AS domestic_rep_name_normalized")
    select_parts.append(f"CAST({publication_year} AS SMALLINT) AS publication_year")

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    con.execute(f"""
        CREATE OR REPLACE TABLE projected_related AS
        SELECT {', '.join(select_parts)}, y.case_file_year
          FROM raw_related r
          LEFT JOIN tmp_case_file_year y
                 ON r."{serial_col}" = y.serial_number
         {limit_clause};
    """)

    return _write_per_year_parquets(
        con,
        source_table="projected_related",
        out_dir=out_dir,
        output_name=cfg.output_name,
        log_prefix=log_prefix,
    )


def _write_per_year_parquets(
    con: duckdb.DuckDBPyConnection,
    *,
    source_table: str,
    out_dir: Path,
    output_name: str,
    log_prefix: str,
) -> dict[int, tuple[Path, int]]:
    rows = con.execute(f"""
        SELECT case_file_year, count(*)
          FROM {source_table}
         WHERE case_file_year IS NOT NULL
         GROUP BY case_file_year
         ORDER BY case_file_year;
    """).fetchall()
    log.info("%s %s rows by year: %d distinct years",
             log_prefix, output_name, len(rows))
    out: dict[int, tuple[Path, int]] = {}
    for year, _ in rows:
        pq = out_dir / f"case_file_year={year}_{output_name}.parquet"
        con.execute(f"""
            COPY (
              SELECT *
                FROM {source_table}
               WHERE case_file_year = {year}
            ) TO '{pq}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
        """)
        rc = con.execute(
            f"SELECT count(*) FROM read_parquet('{pq}');"
        ).fetchone()
        out[year] = (pq, int(rc[0]) if rc else 0)
    return out


def _pick_col(cols: list[str], candidates: tuple[str, ...]) -> str | None:
    lower_map = {c.lower(): c for c in cols}
    for cand in candidates:
        v = lower_map.get(cand.lower())
        if v is not None:
            return v
    return None


# --------------------------------------------------------------------------- #
# R2 upload + audit
# --------------------------------------------------------------------------- #


def upload_to_r2(
    parquet_path: Path,
    *,
    bucket: str,
    key: str,
    log_prefix: str,
) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    log.info("%s uploading %s (%.1f MB) → s3://%s/%s",
             log_prefix, parquet_path, file_bytes / (1 << 20), bucket, key)
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


def insert_run_row(
    conn: psycopg.Connection,
    *,
    case_file_year: int,
    table_name: str,
    publication_year: int,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.uspto_trademarks_r2_ingest_runs (
        case_file_year, table_name, publication_year, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            case_file_year, table_name, publication_year, source_url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_completed(
    conn: psycopg.Connection,
    *,
    publication_year: int,
    table_name: str,
    case_file_year: int,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.uspto_trademarks_r2_ingest_runs
             WHERE publication_year = %s
               AND table_name = %s
               AND case_file_year = %s
               AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
        """, (publication_year, table_name, case_file_year))
        row = cur.fetchone()
    return row[0] if row else None


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_in_partition: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    r2_key: str | None,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.uspto_trademarks_r2_ingest_runs
               SET status = %s,
                   rows_in_partition = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_key = %s, r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
        """, (
            status, rows_in_partition, parquet_row_count,
            parquet_bytes_written, R2_BUCKET, r2_key, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run_ingest(
    *,
    publication_year: int,
    source_paths: list[Path] | None,
    workdir: Path,
    dry_run: bool,
    max_rows: int | None,
    r2_prefix_override: str | None,
    only_tables: tuple[str, ...] | None,
    filing_year_floor: int,
    filing_year_ceiling: int,
) -> int:
    url = URL_TEMPLATE.format(year=publication_year)
    log.info(
        "USPTO TCFD ingest start: publication_year=%d source_paths=%s url=%s",
        publication_year, source_paths, url,
    )

    # Pick tables
    sel_tables = TABLES
    if only_tables:
        wanted = set(only_tables)
        sel_tables = tuple(t for t in TABLES if t.output_name in wanted)
        if not sel_tables:
            log.error("No tables match --only %s", only_tables)
            return 2

    # Phase 1: HEAD (advisory). Skip if local sources provided.
    source_last_modified: datetime | None = None
    if not source_paths:
        with httpx.Client(headers={"User-Agent": "data-engine-x/uspto-tm-r2-ingest"}) as client:
            try:
                _, source_last_modified, status_code = head_url(client, url)
            except Exception:
                log.exception("HEAD failed")
                return 1
            if status_code == 404:
                log.error("HEAD 404 for %s", url)
                return 1
    else:
        try:
            mtimes = [
                p.stat().st_mtime for p in source_paths if p.exists()
            ]
            if mtimes:
                source_last_modified = datetime.fromtimestamp(
                    max(mtimes), tz=timezone.utc,
                )
        except Exception:
            source_last_modified = None

    if dry_run:
        log.info("DRY RUN — exiting before download/extract/transform")
        return 0

    # Phase 2: obtain ZIP(s)
    workdir.mkdir(parents=True, exist_ok=True)
    extract_dir = workdir / f"extract_{publication_year}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    if source_paths:
        for p in source_paths:
            if not p.exists():
                log.error("--source-zip %s does not exist", p)
                return 1
        log.info("Using %d local source path(s):", len(source_paths))
        for p in source_paths:
            kind = "dir" if p.is_dir() else "file"
            size_mb = (
                sum(c.stat().st_size for c in p.rglob('*') if c.is_file()) / (1 << 20)
                if p.is_dir() else p.stat().st_size / (1 << 20)
            )
            log.info("  - %s [%s] (%.1f MB)", p, kind, size_mb)
        sources = source_paths
    else:
        zip_path = workdir / f"TRCFECO2_{publication_year}_csv.zip"
        log.info("Downloading from %s", url)
        with httpx.Client(headers={"User-Agent": "data-engine-x/uspto-tm-r2-ingest"}) as client:
            try:
                n = download_zip(client, url, zip_path)
            except Exception:
                log.exception("download failed")
                return 1
        # The Open Data Portal returns a ~20KB SPA HTML rather than a real ZIP
        # under WAF challenge — surface fast.
        if zip_path.stat().st_size < 100_000:
            log.error(
                "Downloaded file is only %d bytes — likely the AWS WAF SPA "
                "wrapper. Aborting; use --source-zip <path> to feed a "
                "browser-downloaded ZIP.", zip_path.stat().st_size,
            )
            return 1
        log.info("Downloaded %d bytes", n)
        sources = [zip_path]

    # Phase 3: extract
    log.info("Extracting CSV bundle(s) ...")
    extracted = extract_csvs(sources, extract_dir)
    log.info("Extracted %d CSVs", len(extracted))

    # Phase 4: transform
    parquet_dir = workdir / f"parquet_{publication_year}"
    if parquet_dir.exists():
        shutil.rmtree(parquet_dir, ignore_errors=True)
    parquet_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(":memory:")
    con.execute(f"PRAGMA threads={DUCKDB_THREADS};")
    con.execute(f"PRAGMA memory_limit='{DUCKDB_MEMORY_LIMIT}';")

    # case_file FIRST so the year lookup is built before related-table joins.
    table_outputs: dict[str, dict[int, tuple[Path, int]]] = {}
    for cfg in sel_tables:
        log_prefix = f"[{cfg.output_name}]"
        try:
            csv_path = resolve_csv(extracted, cfg)
        except RuntimeError as exc:
            log.error("%s %s", log_prefix, exc)
            return 1
        log.info("%s csv=%s (%.1f MB)",
                 log_prefix, csv_path, csv_path.stat().st_size / (1 << 20))
        if cfg is TABLE_CASE_FILE:
            outs = transform_case_file(
                con, csv_path,
                out_dir=parquet_dir,
                publication_year=publication_year,
                floor=filing_year_floor,
                ceiling=filing_year_ceiling,
                log_prefix=log_prefix,
                max_rows=max_rows,
            )
        elif cfg is TABLE_OWNER:
            outs = transform_owner(
                con, csv_path,
                out_dir=parquet_dir,
                publication_year=publication_year,
                log_prefix=log_prefix,
                max_rows=max_rows,
            )
        else:
            outs = transform_related(
                con, csv_path, cfg,
                out_dir=parquet_dir,
                publication_year=publication_year,
                log_prefix=log_prefix,
                max_rows=max_rows,
            )
        table_outputs[cfg.output_name] = outs
        log.info("%s wrote %d per-year Parquets", log_prefix, len(outs))

    con.close()

    # Phase 5: upload + audit
    rc = 0
    with psycopg.connect(_database_url()) as conn:
        for cfg in sel_tables:
            outs = table_outputs.get(cfg.output_name, {})
            for case_file_year, (pq_path, row_count) in sorted(outs.items()):
                started_wall = time.monotonic()
                prior = get_prior_completed(
                    conn, publication_year=publication_year,
                    table_name=cfg.output_name, case_file_year=case_file_year,
                )
                run_id = insert_run_row(
                    conn,
                    case_file_year=case_file_year,
                    table_name=cfg.output_name,
                    publication_year=publication_year,
                    source_url=url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                pq_bytes = pq_path.stat().st_size
                if r2_prefix_override is not None:
                    base = r2_prefix_override.rstrip("/")
                    r2_prefix = f"{base}/year={case_file_year}/"
                else:
                    r2_prefix = f"uspto-trademarks/year={case_file_year}/"
                r2_key = r2_prefix + cfg.parquet_filename
                try:
                    uploaded = upload_to_r2(
                        pq_path, bucket=R2_BUCKET, key=r2_key,
                        log_prefix=f"[{cfg.output_name} year={case_file_year}]",
                    )
                    finalize_run_row(
                        conn, run_id, status="completed",
                        rows_in_partition=row_count,
                        parquet_row_count=row_count,
                        parquet_bytes_written=pq_bytes,
                        r2_key=r2_key, r2_total_bytes=uploaded,
                        started_at=started_wall, error_message=None,
                        notes={
                            "r2_key": r2_key,
                            "publication_year": publication_year,
                            "max_rows": max_rows,
                        },
                    )
                except Exception as exc:
                    log.exception(
                        "[%s year=%s] upload failed",
                        cfg.output_name, case_file_year,
                    )
                    finalize_run_row(
                        conn, run_id, status="failed",
                        rows_in_partition=row_count,
                        parquet_row_count=row_count,
                        parquet_bytes_written=pq_bytes,
                        r2_key=None, r2_total_bytes=0,
                        started_at=started_wall,
                        error_message=str(exc), notes=None,
                    )
                    rc = 1
                finally:
                    pq_path.unlink(missing_ok=True)

    log.info("USPTO TCFD ingest %s rc=%d",
             "completed" if rc == 0 else "had failures", rc)
    return rc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--publication-year", type=int, default=DEFAULT_PUBLICATION_YEAR,
                   help="USPTO TCFD publication-year vintage (default: 2023).")
    p.add_argument("--source-zip", type=Path, nargs="+", default=None,
                   help="One or more local paths to feed the ingest. Each path "
                        "may be: a single bundled csv.zip, one of the per-table "
                        ".csv.zip files, a raw .csv, or a directory of any of "
                        "the above (walked recursively). Required while "
                        "data.uspto.gov serves a JS-gated SPA instead of the "
                        "file. Examples: "
                        "--source-zip ~/Downloads/csv.zip ; "
                        "--source-zip ~/Downloads/uspto_tcfd_2023/ ; "
                        "--source-zip case_file.csv.zip owner.csv.zip.")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP/CSV/Parquet temp files "
                        "(default: /tmp/uspto_tm_r2_ingest).")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD only; no download, transform, upload, or DB write.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows in each table (smoke testing only).")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override the R2 prefix — used by smoke tests to land "
                        "in /uspto-trademarks/_smoke/ instead of canonical paths.")
    p.add_argument("--only", nargs="+", default=None,
                   help="Subset of tables to ingest (e.g. case_file case_file_owner). "
                        "case_file always runs first if selected.")
    p.add_argument("--filing-year-floor", type=int, default=DEFAULT_FILING_YEAR_FLOOR)
    p.add_argument("--filing-year-ceiling", type=int, default=DEFAULT_FILING_YEAR_CEILING)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/uspto_tm_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)
    return run_ingest(
        publication_year=args.publication_year,
        source_paths=args.source_zip,
        workdir=workdir,
        dry_run=args.dry_run,
        max_rows=args.max_rows,
        r2_prefix_override=args.r2_prefix_override,
        only_tables=tuple(args.only) if args.only else None,
        filing_year_floor=args.filing_year_floor,
        filing_year_ceiling=args.filing_year_ceiling,
    )


if __name__ == "__main__":
    sys.exit(main())
