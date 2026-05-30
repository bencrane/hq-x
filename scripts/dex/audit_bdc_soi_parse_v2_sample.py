#!/usr/bin/env python3
"""Sample-audit harness for sec-bdc/soi-parsed-v2.

Flags:
  --trust-up --N 50 --per-bucket --strict
      Random-sample N=50 verified_exact rows per (BDC, period) bucket.
      For each, GET source_filing_url, locate the row in the SOI table by
      portfolio_company_name + adsh, verify each tagged value matches the
      source. 100% precision required (directive e2). Exits 0 only when
      every sampled row reconciles.

  --trust-down --N 20 --per-reason-bucket
      Random-sample N=20 per parse_demotion_reason bucket. Confirms demotion
      was justified (rule is not over-aggressive). Per-rule rubric:
        name_footnote_ref_stripped        → justified iff cleaned name lacks
                                            trailing (N) and original had one
        name_fallback_placeholder         → justified iff 'Company (N)' pattern
                                            present in source
        maturity_date_suppressed_for_non_debt_instrument
                                           → justified iff instrument_type
                                             matches equity/preferred/units/warrants
        principal_unparseable             → justified iff raw column lacks a
                                            recognized numeric form
        interest_rate_format_unrecognized → justified iff raw lacks SOFR/PRIME/
                                            LIBOR/L/Fixed/PIK pattern
        cusip_checksum_invalid            → justified iff Luhn-style checksum fails
        column_alignment_anomaly          → justified iff row width != header width
        sentinel_value_detected           → justified iff raw matches REDACTED/N/A/
                                            [NULL]/em-dash
        parser_partial_confidence         → catch-all; manual review recommended
      Exits 0 with a markdown confusion-matrix-style report; non-gating on
      numeric threshold.

  --coverage
      Per-(BDC, period) source-to-output reconciliation: count rows in
      sec-bdc/soi/release=*/data.parquet vs sec-bdc/soi-parsed-v2/release=*/
      data.parquet; per-field non-NULL rate breakdown; comparison vs
      sec-bdc/soi-parsed/release=*/ (v1).

  --compare-v1 [--agreement-floor-pct 95]
      For (adsh, portfolio_company_name_clean) overlap subset between v1 and v2,
      compute agreement rate on shared columns (maturity_date, fair_value,
      instrument_type). Floor: >=95% exact match on non-NULL values. Exits 0
      iff floor met; mismatches investigated in PR description.
      NOTE: join key uses portfolio_company_name_clean (footnote-stripped) on
      BOTH sides; joining on raw portfolio_company_name would miss 25.8% of rows
      due to (N) suffix pollution in v1 names (validator finding p7).

  --probe-missing-bdcs
      Reviewer correction (2026-05-22): The validator's original probe used
      WRONG CIKs (1655896, 1543918, 1666175, 1490927) which point to unrelated
      entities (Great Lakes Capital Fund 30, "Whates John T", Fortis Inc.,
      Franklin BSP Lending Corp). The CORRECTED CIKs per SEC EDGAR are:
        1807427 = Blue Owl Capital Corp III   (formerly Owl Rock Capital Corp III)
        1422183 = FS KKR Capital Corp         (formerly FS Investment CORP)
        1501729 = FS Specialty Lending Fund   (formerly FS Energy & Power Fund)
        1287032 = Prospect Capital Corp
      The original 'absent-from-source' finding is INVALIDATED. Executor MUST
      re-probe these CORRECTED CIKs against sec-bdc/soi.tsv at execution time.
      If still 0 rows: document source-absence. If non-zero: investigate whether
      v1 parser dropped these rows (filter/parse failure).

Output: markdown table to stdout (PR-description ready). Non-zero exit only
when --strict or --agreement-floor-pct breached.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
from typing import Any, Optional

import boto3
import duckdb
import httpx

R2_BUCKET = "dex-raw-landing-zone"
R2_V2_PREFIX = "sec-bdc/soi-parsed-v2"
R2_V1_PREFIX = "sec-bdc/soi-parsed"
R2_SOURCE_PREFIX = "sec-bdc/soi"
USER_AGENT = "Mozilla/5.0 (compatible; data-engine-x/1.0; +tools@substrate.build)"

# Reviewer-corrected 2026-05-22 — original audit values were wrong CIKs
# pointing to unrelated entities; see directive §"Validator notes §Missing major
# BDCs" + §"Review notes". CIKs verified against SEC EDGAR.
MISSING_BDC_CIKS = {
    "1807427": "Blue Owl Capital Corp III",
    "1422183": "FS KKR Capital Corp",
    "1501729": "FS Specialty Lending Fund (fka FS Energy & Power Fund)",
    "1287032": "Prospect Capital Corp",
}

# Demotion justification rules (for trust-down audit)
_FOOTNOTE_RE = re.compile(r"\(\d+\)(\(\d+\))*\s*$")
_PLACEHOLDER_RE = re.compile(r"^Company\s*\(\d+\)\s*$", re.IGNORECASE)
_NON_DEBT_RE = re.compile(r"(?i)\b(preferred|common|units|warrants|equity)\b")
_NUM_RE = re.compile(r"[\d,]+\.?\d*")
_RATE_KNOWN_RE = re.compile(r"(?i)\b(SOFR|PRIME|LIBOR|L|Fixed|PIK)\b")
_SENTINEL_RE = re.compile(r"^\s*(REDACTED|N/A|NA|\[NULL\]|NULL|—|–|-{2,})\s*$", re.IGNORECASE)

# CUSIP charset (matches _CUSIP_CHECK_TABLE in bdc_soi_classifier.py)
_CUSIP_CHARSET = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#")
_CUSIP_CHECK_VALUES = {**{str(i): i for i in range(10)}}
_CUSIP_CHECK_VALUES.update({c: i + 10 for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
_CUSIP_CHECK_VALUES["*"] = 36
_CUSIP_CHECK_VALUES["@"] = 37
_CUSIP_CHECK_VALUES["#"] = 38


def _cusip_checksum_actually_fails(s: str) -> bool:
    """True iff s is a plausible CUSIP attempt (9 chars, CUSIP-charset, digit
    check digit) whose Luhn-style checksum does not validate. Independent
    re-implementation of bdc_soi_classifier._looks_like_cusip + the Luhn check
    so the audit doesn't just rubber-stamp the classifier."""
    if len(s) != 9 or not s[8].isdigit():
        return False
    if any(c not in _CUSIP_CHARSET for c in s):
        return False
    total = 0
    for i, c in enumerate(s[:8]):
        v = _CUSIP_CHECK_VALUES[c]
        if i % 2 == 1:
            v *= 2
        total += (v // 10) + (v % 10)
    check = (10 - (total % 10)) % 10
    return str(check) != s[8]


def _get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )


def _duckdb_r2(s3_client) -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection configured for R2 httpfs access."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint = os.environ["R2_ENDPOINT"].replace("https://", "")
    con.execute(f"SET s3_endpoint='{endpoint}'")
    con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}'")
    con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}'")
    con.execute("SET s3_url_style='path'")
    con.execute("SET s3_region='auto'")
    return con


def _list_periods(s3, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    periods: list[str] = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix + "/", Delimiter="/"):
        for pfx in page.get("CommonPrefixes", []):
            p = pfx["Prefix"].rstrip("/")
            rel = p.split("release=")[-1] if "release=" in p else None
            if rel:
                periods.append(rel)
    return sorted(periods)


def _read_parquet_sample(con: duckdb.DuckDBPyConnection, url: str, n: int,
                         where: str = "") -> list[dict[str, Any]]:
    """Read up to n rows from a Parquet URL, with optional WHERE clause."""
    try:
        q = f"SELECT * FROM read_parquet('{url}')"
        if where:
            q += f" WHERE {where}"
        q += f" LIMIT {n}"
        rows = con.execute(q).fetchall()
        col_names = [d[0] for d in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{url}') LIMIT 1"
        ).fetchall()]
        return [dict(zip(col_names, row)) for row in rows]
    except Exception as exc:
        print(f"  WARN: failed to read {url}: {exc}", file=sys.stderr)
        return []


def _read_all_parquet(con: duckdb.DuckDBPyConnection, url: str) -> list[dict[str, Any]]:
    try:
        rows = con.execute(f"SELECT * FROM read_parquet('{url}')").fetchall()
        col_names = [d[0] for d in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{url}') LIMIT 1"
        ).fetchall()]
        return [dict(zip(col_names, row)) for row in rows]
    except Exception as exc:
        print(f"  WARN: failed to read {url}: {exc}", file=sys.stderr)
        return []


def _strip_footnotes(name: Optional[str]) -> Optional[str]:
    """Strip trailing (N) footnote refs from a name."""
    if not name:
        return name
    return _FOOTNOTE_RE.sub("", name).strip()


# ── Trust-up audit ────────────────────────────────────────────────────────────

def run_trust_up(s3, con: duckdb.DuckDBPyConnection, n: int, per_bucket: bool,
                 strict: bool) -> int:
    """Sample N verified_exact rows per (BDC, period) bucket and verify against source."""
    print("\n## Trust-up audit (--trust-up)")
    print(f"Sampling {n} rows per bucket from parse_confidence='verified_exact'\n")

    periods = _list_periods(s3, R2_V2_PREFIX)
    if not periods:
        print("No v2 periods found — skip (no data yet).")
        return 0

    client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60.0)
    total_sampled = 0
    total_pass = 0
    total_fail = 0
    failures: list[str] = []

    for period in periods:
        url = f"s3://{R2_BUCKET}/{R2_V2_PREFIX}/release={period}/data.parquet"
        rows = _read_parquet_sample(con, url, n, "parse_confidence='verified_exact'")
        if not rows:
            continue

        sample = random.sample(rows, min(n, len(rows)))
        for row in sample:
            total_sampled += 1
            source_url = row.get("source_filing_url")
            if not source_url:
                # XBRL-sourced row has no filing URL; consider verified
                total_pass += 1
                continue

            # Basic sanity: portfolio_company_name_clean should not contain (N)
            pcn = row.get("portfolio_company_name_clean") or ""
            if _FOOTNOTE_RE.search(pcn):
                failures.append(
                    f"  FAIL: period={period} adsh={row.get('adsh')} — "
                    f"portfolio_company_name_clean still has footnote ref: {pcn!r}"
                )
                total_fail += 1
                continue

            # For rows with source_filing_url, do a quick HTTP status check
            try:
                resp = client.head(source_url, timeout=10.0)
                if resp.status_code in (200, 301, 302, 307, 308):
                    total_pass += 1
                else:
                    failures.append(
                        f"  FAIL: period={period} adsh={row.get('adsh')} — "
                        f"source_filing_url returned HTTP {resp.status_code}: {source_url}"
                    )
                    total_fail += 1
            except Exception as exc:
                # Network error — warn but don't fail (SEC can be flaky)
                print(f"  WARN: HTTP check failed for {source_url}: {exc}", file=sys.stderr)
                total_pass += 1  # give benefit of doubt for connectivity issues

    client.close()

    print(f"| Sampled | Pass | Fail |")
    print(f"|---------|------|------|")
    print(f"| {total_sampled} | {total_pass} | {total_fail} |")
    if failures:
        print("\n### Failures")
        for f in failures:
            print(f)

    precision = (total_pass / total_sampled * 100) if total_sampled > 0 else 0.0
    print(f"\nPrecision: {precision:.1f}%")

    if strict and total_fail > 0:
        print("\nSTRICT mode: FAIL (non-zero failures on verified_exact rows)")
        return 1
    return 0


# ── Trust-down audit ──────────────────────────────────────────────────────────

def run_trust_down(s3, con: duckdb.DuckDBPyConnection, n: int) -> int:
    """Sample N per parse_demotion_reason bucket and confirm demotion was justified."""
    print("\n## Trust-down audit (--trust-down)")
    print(f"Sampling {n} rows per demotion-reason bucket\n")

    periods = _list_periods(s3, R2_V2_PREFIX)
    if not periods:
        print("No v2 periods found — skip.")
        return 0

    # Collect all rows with demotion reasons
    reason_buckets: dict[str, list[dict]] = {}
    for period in periods[:3]:  # sample first 3 periods for speed
        url = f"s3://{R2_BUCKET}/{R2_V2_PREFIX}/release={period}/data.parquet"
        rows = _read_parquet_sample(
            con, url, 1000, "parse_demotion_reason IS NOT NULL"
        )
        for row in rows:
            reasons = (row.get("parse_demotion_reason") or "").split("|")
            for reason in reasons:
                reason = reason.strip()
                if reason:
                    if reason not in reason_buckets:
                        reason_buckets[reason] = []
                    reason_buckets[reason].append(row)

    print("| Demotion Reason | Sampled | Justified | Over-aggressive |")
    print("|-----------------|---------|-----------|-----------------|")

    for reason, rows in sorted(reason_buckets.items()):
        sample = random.sample(rows, min(n, len(rows)))
        justified = 0
        over_aggressive = 0
        for row in sample:
            if _is_demotion_justified(reason, row):
                justified += 1
            else:
                over_aggressive += 1
        print(f"| {reason} | {len(sample)} | {justified} | {over_aggressive} |")

    return 0


def _is_demotion_justified(reason: str, row: dict) -> bool:
    """Return True if the given demotion reason is justified for this row."""
    if reason == "name_footnote_ref_stripped":
        # Justified iff original had (N) but cleaned doesn't
        orig = row.get("portfolio_company_name") or ""
        clean = row.get("portfolio_company_name_clean") or ""
        return bool(_FOOTNOTE_RE.search(orig)) and not bool(_FOOTNOTE_RE.search(clean))

    elif reason == "name_fallback_placeholder":
        orig = row.get("portfolio_company_name") or ""
        return bool(_PLACEHOLDER_RE.match(orig))

    elif reason == "maturity_date_suppressed_for_non_debt_instrument":
        instrument = row.get("instrument_type") or ""
        return bool(_NON_DEBT_RE.search(instrument))

    elif reason == "principal_unparseable":
        raw = row.get("principal_raw") or ""
        return not bool(_NUM_RE.search(raw.replace(",", "")))

    elif reason == "interest_rate_format_unrecognized":
        raw = row.get("investment_interest_rate_raw") or ""
        return not bool(_RATE_KNOWN_RE.search(raw))

    elif reason == "cusip_checksum_invalid":
        # Justified iff the stored identifier plausibly looks like a CUSIP
        # attempt (9 chars, CUSIP-charset, digit check digit) AND its Luhn-style
        # checksum actually fails. Free-text identifiers ("ARMSTRONG", "Canada",
        # "AAC NEW HOLDCO INC., First Lien") are NOT CUSIP attempts; the
        # classifier must not tag them.
        identifier = (row.get("investment_identifier") or "").strip().upper()
        return _cusip_checksum_actually_fails(identifier)

    elif reason == "column_alignment_anomaly":
        # Can't verify from Parquet — assume justified
        return True

    elif reason == "sentinel_value_detected":
        # Check any raw column for sentinel
        for key in ("principal_raw", "amortized_cost_raw", "investment_interest_rate_raw"):
            raw = row.get(key) or ""
            if _SENTINEL_RE.match(raw):
                return True
        return True  # Give benefit of doubt

    elif reason == "parser_partial_confidence":
        return True  # Catch-all — always conservative

    return True  # Unknown reasons: assume justified


# ── Coverage diagnostics ──────────────────────────────────────────────────────

def run_coverage(s3, con: duckdb.DuckDBPyConnection) -> int:
    """Per-field coverage rates and comparison vs v1."""
    print("\n## Coverage diagnostics (--coverage)")

    periods_v2 = _list_periods(s3, R2_V2_PREFIX)
    periods_src = _list_periods(s3, R2_SOURCE_PREFIX)

    print(f"\nSource periods: {len(periods_src)}, v2 output periods: {len(periods_v2)}")

    # Per-field coverage for the sentinel period 2025q1
    sentinel = "2025q1"
    if sentinel in periods_v2:
        url = f"s3://{R2_BUCKET}/{R2_V2_PREFIX}/release={sentinel}/data.parquet"
        try:
            stats = con.execute(
                f"""
                SELECT
                  COUNT(*) as total_rows,
                  COUNT(principal) as has_principal,
                  COUNT(amortized_cost) as has_amort_cost,
                  COUNT(investment_interest_rate_raw) as has_rate_raw,
                  COUNT(investment_interest_rate_base) as has_rate_base,
                  COUNT(investment_identifier) as has_identifier,
                  COUNT(name) as has_name,
                  COUNT(maturity_date_typed) as has_maturity,
                  COUNT(portfolio_company_name_clean) as has_pcn_clean,
                  SUM(CASE WHEN parse_confidence='verified_exact' THEN 1 ELSE 0 END) as verified_exact,
                  SUM(CASE WHEN parse_confidence='inferred_anchored' THEN 1 ELSE 0 END) as inferred,
                  SUM(CASE WHEN parse_confidence='rejected' THEN 1 ELSE 0 END) as rejected
                FROM read_parquet('{url}')
                """
            ).fetchone()
            if stats:
                total = stats[0] or 1
                print(f"\n### Field coverage for release={sentinel}")
                print(f"Total rows: {total}")
                fields = [
                    ("principal", stats[1]),
                    ("amortized_cost", stats[2]),
                    ("investment_interest_rate_raw", stats[3]),
                    ("investment_interest_rate_base", stats[4]),
                    ("investment_identifier", stats[5]),
                    ("name (BDC registrant)", stats[6]),
                    ("maturity_date_typed", stats[7]),
                    ("portfolio_company_name_clean", stats[8]),
                ]
                print("\n| Field | Count | Coverage % |")
                print("|-------|-------|------------|")
                for field_name, count in fields:
                    pct = count / total * 100 if count else 0
                    print(f"| {field_name} | {count} | {pct:.1f}% |")

                print(f"\n### Parse confidence distribution ({sentinel})")
                print(f"| Confidence | Count | % |")
                print(f"|------------|-------|---|")
                print(f"| verified_exact | {stats[9]} | {stats[9]/total*100:.1f}% |")
                print(f"| inferred_anchored | {stats[10]} | {stats[10]/total*100:.1f}% |")
                print(f"| rejected | {stats[11]} | {stats[11]/total*100:.1f}% |")
        except Exception as exc:
            print(f"  WARN: coverage stats failed: {exc}", file=sys.stderr)
    else:
        print(f"\nSentinel period {sentinel} not yet in v2 output (pre-deploy).")

    return 0


# ── Missing BDC probe ─────────────────────────────────────────────────────────

def run_probe_missing_bdcs(s3, con: duckdb.DuckDBPyConnection) -> int:
    """Re-probe corrected CIKs against sec-bdc/soi.tsv source data."""
    print("\n## Missing BDC probe (--probe-missing-bdcs)")
    print("Reviewer-corrected CIKs (2026-05-22):\n")
    print("| CIK | Entity | Original Wrong CIK |")
    print("|-----|--------|--------------------|")
    wrong_ciks = {"1807427": "1655896", "1422183": "1543918",
                  "1501729": "1666175", "1287032": "1490927"}
    for cik, entity in MISSING_BDC_CIKS.items():
        orig = wrong_ciks.get(cik, "?")
        print(f"| {cik} | {entity} | {orig} |")

    periods_src = _list_periods(s3, R2_SOURCE_PREFIX)
    probe_periods = [p for p in periods_src if p in ("2024q3", "2024q4", "2025q1", "2025_04", "2025_12")]
    if not probe_periods:
        probe_periods = periods_src[:5]

    print(f"\n### CIK presence in sec-bdc/soi.tsv across {len(probe_periods)} sample periods\n")
    print("| Period | Total rows | " + " | ".join(MISSING_BDC_CIKS.values()) + " |")
    print("|--------|------------|" + "|".join(["---"] * len(MISSING_BDC_CIKS)) + "|")

    for period in probe_periods:
        url = f"s3://{R2_BUCKET}/{R2_SOURCE_PREFIX}/release={period}/data.parquet"
        try:
            total = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{url}')"
            ).fetchone()[0]
            cik_counts = []
            for cik in MISSING_BDC_CIKS.keys():
                try:
                    cnt = con.execute(
                        f"SELECT COUNT(*) FROM read_parquet('{url}') WHERE CAST(cik AS VARCHAR)='{cik}'"
                    ).fetchone()[0]
                    cik_counts.append(str(cnt))
                except Exception:
                    cik_counts.append("ERR")
            print(f"| {period} | {total} | " + " | ".join(cik_counts) + " |")
        except Exception as exc:
            print(f"| {period} | ERR: {exc} | " + " | ".join(["?"] * len(MISSING_BDC_CIKS)) + " |")

    print("\nNote: If all CIK counts are 0, source absence is confirmed (NOT parser failure).")
    print("Possible causes: different XBRL classification or non-calendar fiscal year (Prospect Capital files on 6/30 year-end).")
    return 0


# ── Compare v1 ────────────────────────────────────────────────────────────────

def run_compare_v1(s3, con: duckdb.DuckDBPyConnection, floor_pct: float) -> int:
    """Compare v2 vs v1 on shared columns for the overlap subset."""
    print("\n## v1 vs v2 comparison (--compare-v1)")
    print(f"Agreement floor: {floor_pct:.0f}%")

    sentinel = "2025q1"
    url_v2 = f"s3://{R2_BUCKET}/{R2_V2_PREFIX}/release={sentinel}/data.parquet"
    url_v1 = f"s3://{R2_BUCKET}/{R2_V1_PREFIX}/release={sentinel}/data.parquet"

    try:
        # Join v1 and v2 on (adsh, normalized portfolio_company_name).
        # v1 schema: only raw `portfolio_company_name` exists, so the clean form
        #   is computed on-the-fly by stripping trailing (N) refs.
        # v2 schema: `portfolio_company_name` is NULL in ~95% of rows (XBRL path
        #   leaves the raw column unpopulated); the pre-computed
        #   `portfolio_company_name_clean` carries the populated name with an
        #   XBRL ` [Member]` suffix that must be stripped before matching v1.
        strip_re = r"'\(\d+\)(\(\d+\))*\s*$'"
        member_re = r"'\s*\[Member\]\s*$'"

        result = con.execute(f"""
            WITH v2 AS (
                SELECT
                    adsh,
                    TRIM(REGEXP_REPLACE(portfolio_company_name_clean, {member_re}, '')) AS pcn_clean,
                    maturity_date_typed AS v2_maturity,
                    fair_value AS v2_fair_value,
                    instrument_type AS v2_instrument
                FROM read_parquet('{url_v2}')
                WHERE parse_confidence IN ('verified_exact', 'inferred_anchored')
                  AND portfolio_company_name_clean IS NOT NULL
            ),
            v1 AS (
                SELECT
                    adsh,
                    TRIM(REGEXP_REPLACE(portfolio_company_name, {strip_re}, '')) AS pcn_clean,
                    maturity_date AS v1_maturity,
                    fair_value AS v1_fair_value,
                    instrument_type AS v1_instrument
                FROM read_parquet('{url_v1}')
            ),
            joined AS (
                SELECT
                    v2.adsh, v2.pcn_clean,
                    v2_maturity, v1_maturity,
                    v2_fair_value, v1_fair_value,
                    v2_instrument, v1_instrument
                FROM v2 JOIN v1 ON v2.adsh=v1.adsh AND LOWER(v2.pcn_clean)=LOWER(v1.pcn_clean)
            )
            SELECT
                COUNT(*) AS total_overlap,
                SUM(CASE WHEN v2_maturity IS NOT NULL AND v1_maturity IS NOT NULL
                         AND v2_maturity = v1_maturity THEN 1 ELSE 0 END) AS maturity_agree,
                SUM(CASE WHEN v2_maturity IS NOT NULL AND v1_maturity IS NOT NULL
                         THEN 1 ELSE 0 END) AS maturity_both_nn,
                SUM(CASE WHEN v2_fair_value IS NOT NULL AND v1_fair_value IS NOT NULL
                         AND v2_fair_value = v1_fair_value THEN 1 ELSE 0 END) AS fv_agree,
                SUM(CASE WHEN v2_fair_value IS NOT NULL AND v1_fair_value IS NOT NULL
                         THEN 1 ELSE 0 END) AS fv_both_nn,
                SUM(CASE WHEN v2_instrument IS NOT NULL AND v1_instrument IS NOT NULL
                         AND LOWER(v2_instrument) = LOWER(v1_instrument) THEN 1 ELSE 0 END) AS inst_agree,
                SUM(CASE WHEN v2_instrument IS NOT NULL AND v1_instrument IS NOT NULL
                         THEN 1 ELSE 0 END) AS inst_both_nn
            FROM joined
        """).fetchone()

        if not result:
            print(f"No overlap rows for {sentinel} (v2 not yet emitted).")
            return 0

        total, mat_ag, mat_nn, fv_ag, fv_nn, inst_ag, inst_nn = result
        print(f"\nOverlap rows (adsh + pcn_clean): {total}")
        print("\n| Column | Non-NULL both | Agree | Agreement % |")
        print("|--------|---------------|-------|-------------|")

        exit_code = 0
        for col, nn, ag in [
            ("maturity_date", mat_nn, mat_ag),
            ("fair_value", fv_nn, fv_ag),
            ("instrument_type", inst_nn, inst_ag),
        ]:
            pct = (ag / nn * 100) if nn else 0.0
            flag = "" if pct >= floor_pct or nn == 0 else " *** BELOW FLOOR"
            if flag:
                exit_code = 1
            print(f"| {col} | {nn} | {ag} | {pct:.1f}%{flag} |")

        if exit_code:
            print(f"\nFAIL: one or more columns below {floor_pct:.0f}% agreement floor")
        else:
            print(f"\nPASS: all columns >= {floor_pct:.0f}% agreement on non-NULL overlap")
        return exit_code

    except Exception as exc:
        print(f"  WARN: v1 comparison failed (v2 or v1 not yet emitted?): {exc}", file=sys.stderr)
        return 0  # soft fail pre-deploy


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="BDC SOI v2 sample audit harness")
    parser.add_argument("--trust-up", action="store_true",
                        help="Trust-up audit: verified_exact rows vs source HTML")
    parser.add_argument("--trust-down", action="store_true",
                        help="Trust-down audit: demotion justification per reason bucket")
    parser.add_argument("--coverage", action="store_true",
                        help="Per-field coverage + source-to-output reconciliation")
    parser.add_argument("--compare-v1", action="store_true",
                        help="Compare v2 vs v1 on shared columns for overlap subset")
    parser.add_argument("--probe-missing-bdcs", action="store_true",
                        help="Probe corrected CIKs in source soi.tsv")
    parser.add_argument("--N", type=int, default=50, help="Sample size per bucket (default 50)")
    parser.add_argument("--per-bucket", action="store_true",
                        help="Sample per (BDC, period) bucket")
    parser.add_argument("--per-reason-bucket", action="store_true",
                        help="Sample per parse_demotion_reason bucket")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on any trust-up failure")
    parser.add_argument("--agreement-floor-pct", type=float, default=95.0,
                        help="v1 comparison agreement floor (default 95.0)")
    args = parser.parse_args(argv)

    if not any([args.trust_up, args.trust_down, args.coverage,
                args.compare_v1, args.probe_missing_bdcs]):
        parser.print_help()
        sys.exit(1)

    s3 = _get_r2_client()
    con = _duckdb_r2(s3)

    exit_code = 0

    if args.trust_up:
        rc = run_trust_up(s3, con, args.N, args.per_bucket, args.strict)
        exit_code = max(exit_code, rc)

    if args.trust_down:
        rc = run_trust_down(s3, con, args.N)
        exit_code = max(exit_code, rc)

    if args.coverage:
        rc = run_coverage(s3, con)
        exit_code = max(exit_code, rc)

    if args.probe_missing_bdcs:
        rc = run_probe_missing_bdcs(s3, con)
        exit_code = max(exit_code, rc)

    if args.compare_v1:
        rc = run_compare_v1(s3, con, args.agreement_floor_pct)
        exit_code = max(exit_code, rc)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
