#!/usr/bin/env python3
"""DuckDB-on-R2 derived-Parquet builder: FMCSA carrier emails with
rules-based local-part → officer attribution tier.

Reads two upstream Parquets (same snapshot date enforced):
  - fmcsa-carrier-essentials/snapshot=<date>/data.parquet  (filter LIKE '%@%')
  - fmcsa-derived/officer_normalized/snapshot=<date>/data.parquet

Applies deterministic local-part → officer-name match rules per row,
classifies each (dot_number, email_address) row into one of 5 tiers:
  - officer_high     (full first+last, or flast / firstl)
  - officer_medium   (first-only or last-only with len>=2)
  - role_address     (local-part matches the role-address dict)
  - ambiguous        (both officers matched at the same tier)
  - unmapped         (no rule fired and not a role address)

Output: s3://dex-raw-landing-zone/fmcsa-derived/email_attributed/
        snapshot=<YYYY-MM-DD>/data.parquet

40 cols: 32 carry-through from mv_fmcsa_carrier_emails (verbatim) + 8
attribution cols (local_part_normalized, attribution_tier,
attributed_officer_slot, attributed_officer_name_normalized,
attributed_officer_first_normalized, attributed_officer_last_normalized,
match_method, snapshot_date).

Architecture (Pattern B per ~/Desktop/hq/inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md):
  1. DuckDB-on-R2 reads emails-essentials + officers-normalized Parquets.
  2. SQL re-pivot officers to carrier-grain (MAX(CASE WHEN slot=1...)).
  3. SQL LEFT JOIN emails ← officers on dot_number.
  4. Pull rules-input cols to pandas; apply rules per row in Python.
  5. Push 8 new cols + 2 key cols back to DuckDB as attribution_lookup.
  6. DuckDB JOIN full carrier-emails ← attribution_lookup; COPY to local
     Parquet → upload to R2.
  7. Ledger row in ops.fmcsa_derived_email_attributed_r2_ingest_runs.

Per directive ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-emails-attributed.md.
Predecessor: build_fmcsa_carrier_officers_normalized.py (PR #314) — lifted
DuckDB-on-R2 + boto3 upload + ledger architecture.

Lessons applied:
  L0 worktree path / L1 Doppler shell expansion / L2 all-VARCHAR with two
  typed SMALLINT cols (officer_slot_count, attributed_officer_slot) /
  L29 no producer-side casting / L34 dedup-on-unique-tuple available as
  fallback if naive >5min / L37 downstream consumers use static-date
  literal filters / L42 plain .parquet extension, no Content-Encoding /
  L45 snapshot-stamped output keys.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with pandas \\
    python apps/data-engine-x/scripts/build_fmcsa_carrier_emails_attributed.py \\
      --sample-only 10000

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with pandas \\
    python apps/data-engine-x/scripts/build_fmcsa_carrier_emails_attributed.py \\
      --apply

  # Idempotent re-run: if target snapshot key already exists, skip rebuild.
  # NOTE per L45: same-snapshot re-runs against an existing key DO NOT
  # propagate to the downstream RW source (S3_V2 connector marks the key
  # as already-consumed). To force a re-build that propagates, pass
  # --snapshot <next-day>.
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with pandas \\
    python apps/data-engine-x/scripts/build_fmcsa_carrier_emails_attributed.py \\
      --apply --skip-if-unchanged
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

# v1.1: bidirectional nickname expansion. The upstream officers-normalized
# Parquet already canonicalized first names via NICKNAME_TO_FORMAL forward
# lookup (Mike→michael, Bob→robert). We invert that here at match time so
# local-parts like `mike@`, `bob@`, `chris@` can match the canonical-form
# officer record.
from scripts._lib.person_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
    expand_to_diminutives,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_fmcsa_carrier_emails_attributed")

RULES_VERSION = "1.1"

R2_BUCKET = "dex-raw-landing-zone"
INPUT_EMAILS_PREFIX = "fmcsa-derived/carrier_essentials"
INPUT_OFFICERS_PREFIX = "fmcsa-derived/officer_normalized"
OUTPUT_PREFIX = "fmcsa-derived/email_attributed"
AUDIT_TABLE = "ops.fmcsa_derived_email_attributed_r2_ingest_runs"

# Stage 0 hard-fail bounds.
#
# v1.0 baseline (PR #317): named=18.47%, unmapped=77.21%.
# v1.1 directive (§Verification harness §Stage 0):
#   - named cohort must grow by ≥3pp absolute → floor 0.2147 (= 0.1847 + 0.03)
#   - unmapped must shrink by ≥3pp absolute    → ceiling 0.7421 (= 0.7721 − 0.03)
# Symmetric gates catch a rule bug that grows one side without shrinking the
# other; both must hold.
HARD_FAIL_NAMED_FLOOR = 0.2147
HARD_FAIL_UNMAPPED_CEIL = 0.7421

# Baseline v1.0 named/unmapped percentages for regression-check logging.
V1_NAMED_PCT_BASELINE = 0.1847
V1_UNMAPPED_PCT_BASELINE = 0.7721
# Predecessor's completed-run id for ledger `notes.supersedes_run_id`.
V1_SUPERSEDES_RUN_ID = "a527257d-549b-4612-9fa5-2f2d3a63359c"

# Role-address dict — best-effort v1 per directive. Static frozenset.
# If Stage 0 surfaces over/under-coverage outside [5%, 40%] for role_address,
# surface to operator before merging.
ROLE_ADDRESS_DICT: frozenset[str] = frozenset([
    # general/contact
    "info", "contact", "contactus", "hello", "hi", "mail", "email",
    "general", "frontdesk", "mainoffice",
    # support
    "support", "help", "helpdesk", "service", "services", "customerservice",
    "customer", "cs",
    # sales/biz
    "sales", "sale", "business", "biz", "bizdev", "marketing", "leads",
    "newbusiness",
    # billing/finance
    "billing", "accounts", "accounting", "ar", "ap", "payments", "payment",
    "invoices", "invoice", "finance", "collections",
    # admin/hr
    "admin", "administration", "office", "hr", "humanresources",
    "recruiting", "recruit", "recruiter", "jobs", "careers", "hiring",
    # operations
    "ops", "operations", "operator", "operations1", "operations2",
    "dispatch", "dispatcher", "dispatchers", "dispatching",
    "fleet", "drivers", "driver",
    # safety/compliance
    "safety", "compliance", "regulatory", "dot", "permits",
    # legal
    "legal", "counsel",
    # tech
    "it", "tech", "technology", "web", "webmaster", "sysadmin",
    # auto/no-reply
    "noreply", "no_reply", "donotreply", "donotrespond",
    # logistics-domain specifics (FMCSA carriers skew here)
    "logistics", "transport", "transportation", "shipping", "freight",
    "trucking", "carrier", "broker", "owner",
])

# Tokenize on these separators AND digits per directive §Attribution rule set.
TOKEN_SPLIT_RE = re.compile(r"[._\-+0-9]+")


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    return endpoint.split("//")[-1].split(".")[0]


def _connect_duckdb_to_r2():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id_from_endpoint(os.environ["R2_ENDPOINT"])}'
        );
        """
    )
    return con


def _detect_latest_snapshot(s3, bucket: str, prefix: str) -> str:
    """List under prefix; return lexicographically-greatest snapshot=YYYY-MM-DD."""
    paginator = s3.get_paginator("list_objects_v2")
    snapshots: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/snapshot="):
        for obj in page.get("Contents", []):
            for part in obj["Key"].split("/"):
                if part.startswith("snapshot=") and len(part) == len("snapshot=YYYY-MM-DD"):
                    snapshots.add(part.split("=", 1)[1])
    if not snapshots:
        raise SystemExit(
            f"FAIL: no snapshot=YYYY-MM-DD under r2://{bucket}/{prefix}/"
        )
    return max(snapshots)


def _verify_snapshot_aligned(s3, snapshot: str) -> tuple[str, int, str, int]:
    """Verify both input keys exist at the same snapshot. Return (emails_key,
    emails_bytes, officers_key, officers_bytes)."""
    emails_key = f"{INPUT_EMAILS_PREFIX}/snapshot={snapshot}/data.parquet"
    officers_key = f"{INPUT_OFFICERS_PREFIX}/snapshot={snapshot}/data.parquet"
    try:
        head = s3.head_object(Bucket=R2_BUCKET, Key=emails_key)
        emails_bytes = head["ContentLength"]
    except Exception as exc:
        raise SystemExit(
            f"FAIL: emails-essentials Parquet missing at "
            f"r2://{R2_BUCKET}/{emails_key} — {exc}"
        )
    try:
        head = s3.head_object(Bucket=R2_BUCKET, Key=officers_key)
        officers_bytes = head["ContentLength"]
    except Exception as exc:
        raise SystemExit(
            f"FAIL: officers-normalized Parquet missing at "
            f"r2://{R2_BUCKET}/{officers_key} — {exc}"
        )
    return emails_key, emails_bytes, officers_key, officers_bytes


def _audit_open(snapshot: str, emails_prefix: str, officers_prefix: str,
                output_prefix: str) -> str:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}
                  (snapshot_date, upstream_emails_input_prefix,
                   upstream_officers_input_prefix, output_r2_prefix,
                   status, triggered_by)
                VALUES (%s, %s, %s, %s, 'running',
                        'manual:build_fmcsa_carrier_emails_attributed')
                RETURNING run_id;
                """,
                (snapshot, emails_prefix, officers_prefix, output_prefix),
            )
            return str(cur.fetchone()[0])


def _audit_close(
    run_id: str,
    *,
    status: str,
    input_emails_row_count: int | None = None,
    input_officers_row_count: int | None = None,
    output_row_count: int | None = None,
    tier_counts: dict[str, int] | None = None,
    duration_s: float | None = None,
    error: str | None = None,
    upstream_emails_keys: list[str] | None = None,
    upstream_officers_keys: list[str] | None = None,
    upstream_total_bytes: int | None = None,
    output_total_bytes: int | None = None,
    notes: dict | None = None,
) -> None:
    import psycopg

    tc = tier_counts or {}
    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {AUDIT_TABLE}
                   SET status = %s,
                       input_emails_row_count = %s,
                       input_officers_row_count = %s,
                       output_row_count = %s,
                       tier_officer_high_count = %s,
                       tier_officer_medium_count = %s,
                       tier_role_address_count = %s,
                       tier_ambiguous_count = %s,
                       tier_unmapped_count = %s,
                       duration_seconds = %s,
                       error_message = %s,
                       upstream_emails_object_keys = %s,
                       upstream_officers_object_keys = %s,
                       upstream_total_bytes = %s,
                       output_object_count = 1,
                       output_total_bytes = %s,
                       notes = %s,
                       ended_at = now()
                 WHERE run_id = %s;
                """,
                (
                    status,
                    input_emails_row_count,
                    input_officers_row_count,
                    output_row_count,
                    tc.get("officer_high"),
                    tc.get("officer_medium"),
                    tc.get("role_address"),
                    tc.get("ambiguous"),
                    tc.get("unmapped"),
                    duration_s,
                    error,
                    upstream_emails_keys,
                    upstream_officers_keys,
                    upstream_total_bytes,
                    output_total_bytes,
                    json.dumps(notes) if notes else None,
                    run_id,
                ),
            )


def _audit_no_change(snapshot: str, emails_prefix: str, officers_prefix: str,
                     output_prefix: str) -> None:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}
                  (snapshot_date, upstream_emails_input_prefix,
                   upstream_officers_input_prefix, output_r2_prefix,
                   status, triggered_by, ended_at)
                VALUES (%s, %s, %s, %s, 'no_change',
                        'manual:build_fmcsa_carrier_emails_attributed', now());
                """,
                (snapshot, emails_prefix, officers_prefix, output_prefix),
            )


def _tokenize_local_part(email: str | None) -> tuple[list[str], str]:
    """Per directive §Attribution rule set. Returns (tokens, concat)."""
    if not email:
        return [], ""
    if "@" not in email:
        return [], ""
    local = email.split("@", 1)[0].lower()
    raw_tokens = TOKEN_SPLIT_RE.split(local)
    # Keep only purely alpha tokens (drop empties + any non-alpha residue)
    tokens = [t for t in raw_tokens if t and t.isalpha()]
    concat = "".join(tokens)
    return tokens, concat


def _match_against_officer(
    tokens: list[str], concat: str,
    first: str, last: str,
) -> tuple[str, str | None]:
    """Match local-part against one officer's first/last normalized name.

    Returns (tier, method) where tier ∈ {'officer_high', 'officer_medium',
    'none'}. Tiers 1-4 require both first AND last; 5/6 require only the
    relevant component (with len>=2 guard).
    """
    if not first and not last:
        return "none", None

    have_both = bool(first) and bool(last)

    # Tier 1: full first+last as separate tokens
    if have_both and set(tokens) == {first, last}:
        return "officer_high", "full_first_last"

    # Tier 2: full concat (either order)
    if have_both:
        if concat == first + last or concat == last + first:
            return "officer_high", "full_concat"

    # Tier 3: flast = first[0] + last
    if have_both and len(first) >= 1:
        if concat == first[0] + last:
            return "officer_high", "flast"

    # Tier 4: firstl = first + last[0]
    if have_both and len(last) >= 1:
        if concat == first + last[0]:
            return "officer_high", "firstl"

    # Tier 5: first only (len>=2 guard)
    if first and len(first) >= 2 and concat == first:
        return "officer_medium", "first_only"

    # Tier 6: last only (len>=2 guard)
    if last and len(last) >= 2 and concat == last:
        return "officer_medium", "last_only"

    return "none", None


def _match_against_officer_expanded(
    tokens: list[str], concat: str,
    canonical_first: str, last: str,
) -> tuple[str, str | None]:
    """v1.1 nickname-expansion wrapper around _match_against_officer.

    Strategy:
      1. Try the canonical-first form first (no nickname suffix on method).
         Canonical match wins ties — keeps match_method clean for the common
         case where canonical first matches.
      2. If canonical didn't match (tier='none'), iterate diminutive forms
         in deterministic order. On first match, suffix method with
         `_via_nickname:<form>`. Tier 6 (last_only) doesn't need expansion
         (last names don't have nicknames) — covered by the canonical pass.

    Returns (tier, method) with method possibly carrying a `_via_nickname:`
    suffix.
    """
    # Canonical attempt — passing "" for canonical_first is fine because
    # _match_against_officer skips first-dependent tiers on empty first
    # but still tries tier 6 (last_only).
    tier, method = _match_against_officer(tokens, concat, canonical_first or "", last)
    if tier != "none":
        return tier, method

    # Diminutive iteration (only if canonical didn't match).
    if not canonical_first:
        return "none", None
    forms = expand_to_diminutives(canonical_first) - {canonical_first}
    if not forms:
        return "none", None
    for form in sorted(forms):
        if not form:
            continue
        tier, method = _match_against_officer(tokens, concat, form, last)
        if tier != "none" and method is not None and not method.startswith("last_only"):
            # last_only didn't use the first-form anyway; never suffix it.
            return tier, f"{method}_via_nickname:{form}"
        if tier != "none":
            # Defensive: if we somehow matched on last_only via the form
            # iteration (shouldn't happen since canonical pass would have
            # caught it), don't suffix.
            return tier, method
    return "none", None


def _resolve(o1_tier: str, o1_method: str | None,
             o2_tier: str, o2_method: str | None,
             concat: str) -> tuple[str, int | None, str | None]:
    """Apply the resolution decision table per directive. Returns
    (tier, slot, method)."""
    # Both high → ambiguous
    if o1_tier == "officer_high" and o2_tier == "officer_high":
        return "ambiguous", None, None
    # High beats medium/none
    if o1_tier == "officer_high":
        return "officer_high", 1, o1_method
    if o2_tier == "officer_high":
        return "officer_high", 2, o2_method
    # Both medium → ambiguous
    if o1_tier == "officer_medium" and o2_tier == "officer_medium":
        return "ambiguous", None, None
    # Medium beats none
    if o1_tier == "officer_medium":
        return "officer_medium", 1, o1_method
    if o2_tier == "officer_medium":
        return "officer_medium", 2, o2_method
    # Both none → role-address dict check
    if concat and concat in ROLE_ADDRESS_DICT:
        return "role_address", None, f"role:{concat}"
    return "unmapped", None, None


def _apply_rules_to_df(df, *, role_dict: frozenset = ROLE_ADDRESS_DICT):
    """Per-row attribution. Mutates df in place, adding 8 attribution cols.

    Required input cols: email_address, officer1_first_norm, officer1_last_norm,
    officer2_first_norm, officer2_last_norm.
    """
    import pandas as pd

    # Coerce string cols to str + fill NaN with empty string before iteration
    # — pandas reads NULL parquet vals as NaN floats which break len(). The
    # rules-relevant cols all participate in string comparisons; empty string
    # is the "missing" sentinel for the rule logic.
    str_cols = [
        "email_address", "company_officer_1", "company_officer_2",
        "officer1_first_norm", "officer1_last_norm", "officer1_name_norm",
        "officer2_first_norm", "officer2_last_norm", "officer2_name_norm",
    ]
    for col in str_cols:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)

    local_part_normalized = [None] * len(df)
    attribution_tier = [None] * len(df)
    attributed_officer_slot: list[int | None] = [None] * len(df)
    attributed_first: list[str | None] = [None] * len(df)
    attributed_last: list[str | None] = [None] * len(df)
    attributed_name: list[str | None] = [None] * len(df)
    match_method: list[str | None] = [None] * len(df)

    # itertuples is the fast path for per-row Python work on pandas. The
    # row tuple's field order is positional — we look up by attribute name
    # for safety.
    for i, row in enumerate(df.itertuples(index=False)):
        email = row.email_address
        o1_first = row.officer1_first_norm
        o1_last = row.officer1_last_norm
        o2_first = row.officer2_first_norm
        o2_last = row.officer2_last_norm

        tokens, concat = _tokenize_local_part(email)
        # v1.1: nickname-expansion at both officer slots
        o1_tier, o1_method = _match_against_officer_expanded(
            tokens, concat, o1_first, o1_last,
        )
        o2_tier, o2_method = _match_against_officer_expanded(
            tokens, concat, o2_first, o2_last,
        )
        tier, slot, method = _resolve(o1_tier, o1_method, o2_tier, o2_method, concat)

        local_part_normalized[i] = concat
        attribution_tier[i] = tier
        attributed_officer_slot[i] = slot
        match_method[i] = method
        if slot == 1:
            attributed_first[i] = o1_first or None
            attributed_last[i] = o1_last or None
            if o1_first and o1_last:
                attributed_name[i] = f"{o1_first} {o1_last}"
        elif slot == 2:
            attributed_first[i] = o2_first or None
            attributed_last[i] = o2_last or None
            if o2_first and o2_last:
                attributed_name[i] = f"{o2_first} {o2_last}"

    df["local_part_normalized"] = local_part_normalized
    df["attribution_tier"] = attribution_tier
    df["attributed_officer_slot"] = attributed_officer_slot
    df["attributed_officer_first_normalized"] = attributed_first
    df["attributed_officer_last_normalized"] = attributed_last
    df["attributed_officer_name_normalized"] = attributed_name
    df["match_method"] = match_method
    return df


def _build_carrier_emails_view(con, *, emails_url: str, officers_url: str) -> None:
    """Materialize TEMP TABLEs: carrier_emails (32 cols, mirroring
    mv_fmcsa_carrier_emails projection) and officers_carrier_grain
    (re-pivoted from officer-grain officers-normalized to carrier-grain)."""
    logger.info("reading emails-essentials …")
    con.execute(
        f"""
        CREATE TEMP TABLE carrier_emails AS
        SELECT
          email_address,
          email_domain_normalized,
          is_free_mail_domain,
          dot_number,
          legal_name,
          dba_name,
          company_officer_1,
          company_officer_2,
          (
            CASE WHEN company_officer_1 IS NOT NULL AND TRIM(company_officer_1) <> '' THEN 1 ELSE 0 END
            +
            CASE WHEN company_officer_2 IS NOT NULL AND TRIM(company_officer_2) <> '' THEN 1 ELSE 0 END
          )::SMALLINT AS officer_slot_count,
          phone,
          cell_phone,
          phy_street,
          phy_city,
          phy_state,
          phy_zip,
          phy_country,
          carrier_mailing_street  AS mailing_street,
          carrier_mailing_city    AS mailing_city,
          carrier_mailing_state   AS mailing_state,
          carrier_mailing_zip     AS mailing_zip,
          power_units,
          fleetsize,
          total_drivers,
          status_code,
          mcs150_date,
          add_date,
          business_org_desc,
          carrier_operation,
          safety_rating,
          hm_ind,
          bus_units,
          dun_bradstreet_no
        FROM read_parquet('{emails_url}')
        WHERE email_address IS NOT NULL
          AND TRIM(email_address) <> ''
          AND email_address LIKE '%@%';
        """
    )

    logger.info("reading officers-normalized …")
    # Re-pivot officer-grain → carrier-grain. The officers-normalized Parquet
    # has 1 row per (dot_number, officer_slot); we widen to one row per
    # dot_number with officer1_/officer2_ cols.
    con.execute(
        f"""
        CREATE TEMP TABLE officers_carrier_grain AS
        SELECT
          dot_number,
          MAX(CASE WHEN officer_slot = 1 THEN officer_first_normalized END) AS officer1_first_norm,
          MAX(CASE WHEN officer_slot = 1 THEN officer_last_normalized  END) AS officer1_last_norm,
          MAX(CASE WHEN officer_slot = 1 THEN officer_name_normalized   END) AS officer1_name_norm,
          MAX(CASE WHEN officer_slot = 2 THEN officer_first_normalized END) AS officer2_first_norm,
          MAX(CASE WHEN officer_slot = 2 THEN officer_last_normalized  END) AS officer2_last_norm,
          MAX(CASE WHEN officer_slot = 2 THEN officer_name_normalized   END) AS officer2_name_norm
        FROM read_parquet('{officers_url}')
        GROUP BY dot_number;
        """
    )


def _print_spot_check(df, n: int = 10) -> None:
    """Print n sample rows across tiers for hand-verification, plus 5 rows
    from nickname-recovered methods (v1.1) when available."""
    import pandas as pd

    cols = [
        "email_address",
        "company_officer_1",
        "company_officer_2",
        "local_part_normalized",
        "attribution_tier",
        "attributed_officer_name_normalized",
        "match_method",
    ]
    available_cols = [c for c in cols if c in df.columns]
    logger.info("Spot-check rows (sampled across tiers):")
    # Try to get 2 per tier where present
    samples_per_tier = []
    for tier in ["officer_high", "officer_medium", "role_address",
                 "ambiguous", "unmapped"]:
        tier_df = df[df["attribution_tier"] == tier]
        if len(tier_df) >= 1:
            samples_per_tier.append(tier_df.head(2))
    if samples_per_tier:
        spot = pd.concat(samples_per_tier).head(n)
        with pd.option_context("display.max_colwidth", 60,
                               "display.width", 200,
                               "display.max_columns", None):
            logger.info("\n%s", spot[available_cols].to_string(index=False))

    # v1.1: 5 rows from nickname-recovered methods for hand-verification
    if "match_method" in df.columns:
        via = df[df["match_method"].astype(str).str.contains(
            "_via_nickname:", na=False
        )]
        if len(via) >= 1:
            logger.info("Nickname-recovered spot-check (v1.1):")
            sample = via.head(5)
            with pd.option_context("display.max_colwidth", 60,
                                   "display.width", 200,
                                   "display.max_columns", None):
                logger.info("\n%s", sample[available_cols].to_string(index=False))


def _print_tier_distribution(df) -> tuple[dict[str, int], dict[str, float]]:
    """Log + return tier counts and percentages."""
    counts: dict[str, int] = {}
    total = len(df)
    for tier in ["officer_high", "officer_medium", "role_address",
                 "ambiguous", "unmapped"]:
        c = int((df["attribution_tier"] == tier).sum())
        counts[tier] = c
    pcts = {k: (v / total if total else 0.0) for k, v in counts.items()}

    logger.info("─" * 60)
    logger.info("Tier distribution (n=%s):", f"{total:,}")
    for tier in ["officer_high", "officer_medium", "role_address",
                 "ambiguous", "unmapped"]:
        logger.info("  %-15s  %10s  (%.2f%%)",
                    tier, f"{counts[tier]:,}", pcts[tier] * 100)
    named_pct = pcts["officer_high"] + pcts["officer_medium"]
    logger.info("  named (high+medium): %.2f%%  unmapped: %.2f%%",
                named_pct * 100, pcts["unmapped"] * 100)
    # v1.1 baseline comparison
    named_delta_pp = (named_pct - V1_NAMED_PCT_BASELINE) * 100
    unmapped_delta_pp = (pcts["unmapped"] - V1_UNMAPPED_PCT_BASELINE) * 100
    logger.info("  vs v1.0 baseline: named %+.2fpp / unmapped %+.2fpp",
                named_delta_pp, unmapped_delta_pp)
    return counts, pcts


def _log_nickname_recovery_audit(df, top_n: int = 20) -> dict[str, int]:
    """Log top-N `_via_nickname:<form>` match_method values by row count.

    Returns the form→count dict for the ledger notes payload.
    """
    if "match_method" not in df.columns:
        return {}
    via = df["match_method"].dropna()
    via = via[via.astype(str).str.contains("_via_nickname:")]
    if len(via) == 0:
        logger.info("nickname-recovered rows: 0 (no _via_nickname: methods)")
        return {}
    total_recovered = len(via)
    counts_series = via.value_counts().head(top_n)
    logger.info("─" * 60)
    logger.info("Nickname-recovered audit (n=%s, top-%d methods):",
                f"{total_recovered:,}", top_n)
    out: dict[str, int] = {}
    for method, cnt in counts_series.items():
        logger.info("  %-50s  %10s", method, f"{int(cnt):,}")
        out[str(method)] = int(cnt)
    return out


def _check_stage0_bounds(counts: dict[str, int], pcts: dict[str, float],
                        total: int) -> tuple[bool, list[str]]:
    """Stage 0 hard-fail bounds. Returns (ok, list_of_failures)."""
    failures: list[str] = []
    named = pcts.get("officer_high", 0.0) + pcts.get("officer_medium", 0.0)
    if named < HARD_FAIL_NAMED_FLOOR:
        failures.append(
            f"named (high+medium)={named:.1%} below floor {HARD_FAIL_NAMED_FLOOR:.0%}"
        )
    if pcts.get("unmapped", 0.0) > HARD_FAIL_UNMAPPED_CEIL:
        failures.append(
            f"unmapped={pcts['unmapped']:.1%} above ceiling {HARD_FAIL_UNMAPPED_CEIL:.0%}"
        )
    # NULL tier check (should never happen by construction)
    if total > 0 and sum(counts.values()) != total:
        failures.append(
            f"NULL tier rows: sum={sum(counts.values()):,} != total={total:,}"
        )
    return (not failures), failures


def _build_attribution_lookup(con, *, sample_only: int | None = None):
    """Pull (dot_number, email_address, officer pair cols) from DuckDB to
    pandas, apply rules, register the result back as DuckDB table
    `attribution_lookup`. Returns (df_attribution, tier_counts, tier_pcts).
    """
    import pandas as pd  # noqa: F401

    if sample_only:
        logger.info("SAMPLE mode: %s random rows", f"{sample_only:,}")
        rules_input = con.execute(
            f"""
            SELECT
              e.dot_number,
              e.email_address,
              e.company_officer_1,
              e.company_officer_2,
              o.officer1_first_norm,
              o.officer1_last_norm,
              o.officer1_name_norm,
              o.officer2_first_norm,
              o.officer2_last_norm,
              o.officer2_name_norm
              FROM carrier_emails e
              LEFT JOIN officers_carrier_grain o
                ON o.dot_number = e.dot_number
             USING SAMPLE reservoir({sample_only} ROWS) REPEATABLE (42)
            """
        ).df()
    else:
        rules_input = con.execute(
            """
            SELECT
              e.dot_number,
              e.email_address,
              e.company_officer_1,
              e.company_officer_2,
              o.officer1_first_norm,
              o.officer1_last_norm,
              o.officer1_name_norm,
              o.officer2_first_norm,
              o.officer2_last_norm,
              o.officer2_name_norm
              FROM carrier_emails e
              LEFT JOIN officers_carrier_grain o
                ON o.dot_number = e.dot_number
            """
        ).df()

    logger.info("rules-input rows in pandas: %s", f"{len(rules_input):,}")
    t_rules = time.time()
    _apply_rules_to_df(rules_input)
    logger.info("rules applied in %.1fs", time.time() - t_rules)

    counts, pcts = _print_tier_distribution(rules_input)
    nickname_audit = _log_nickname_recovery_audit(rules_input, top_n=20)
    return rules_input, counts, pcts, nickname_audit


def _write_output_parquet(con, snapshot: str, output_url: str) -> None:
    """JOIN carrier_emails ← attribution_lookup on (dot_number,
    email_address); COPY to local Parquet via DuckDB."""
    logger.info("writing output Parquet → %s", output_url)
    # All cols VARCHAR (writer side) per L2 #1 except two SMALLINT typed cols:
    # officer_slot_count and attributed_officer_slot. Source DDL declares
    # those two SMALLINT to match.
    con.execute(
        f"""
        COPY (
          SELECT
            e.email_address::VARCHAR                          AS email_address,
            e.email_domain_normalized::VARCHAR                AS email_domain_normalized,
            e.is_free_mail_domain::VARCHAR                    AS is_free_mail_domain,
            e.dot_number::VARCHAR                             AS dot_number,
            e.legal_name::VARCHAR                             AS legal_name,
            e.dba_name::VARCHAR                               AS dba_name,
            e.company_officer_1::VARCHAR                      AS company_officer_1,
            e.company_officer_2::VARCHAR                      AS company_officer_2,
            e.officer_slot_count,
            e.phone::VARCHAR                                  AS phone,
            e.cell_phone::VARCHAR                             AS cell_phone,
            e.phy_street::VARCHAR                             AS phy_street,
            e.phy_city::VARCHAR                               AS phy_city,
            e.phy_state::VARCHAR                              AS phy_state,
            e.phy_zip::VARCHAR                                AS phy_zip,
            e.phy_country::VARCHAR                            AS phy_country,
            e.mailing_street::VARCHAR                         AS mailing_street,
            e.mailing_city::VARCHAR                           AS mailing_city,
            e.mailing_state::VARCHAR                          AS mailing_state,
            e.mailing_zip::VARCHAR                            AS mailing_zip,
            e.power_units::VARCHAR                            AS power_units,
            e.fleetsize::VARCHAR                              AS fleetsize,
            e.total_drivers::VARCHAR                          AS total_drivers,
            e.status_code::VARCHAR                            AS status_code,
            e.mcs150_date::VARCHAR                            AS mcs150_date,
            e.add_date::VARCHAR                               AS add_date,
            e.business_org_desc::VARCHAR                      AS business_org_desc,
            e.carrier_operation::VARCHAR                      AS carrier_operation,
            e.safety_rating::VARCHAR                          AS safety_rating,
            e.hm_ind::VARCHAR                                 AS hm_ind,
            e.bus_units::VARCHAR                              AS bus_units,
            e.dun_bradstreet_no::VARCHAR                      AS dun_bradstreet_no,
            a.local_part_normalized::VARCHAR                  AS local_part_normalized,
            a.attribution_tier::VARCHAR                       AS attribution_tier,
            CAST(a.attributed_officer_slot AS SMALLINT)       AS attributed_officer_slot,
            a.attributed_officer_name_normalized::VARCHAR     AS attributed_officer_name_normalized,
            a.attributed_officer_first_normalized::VARCHAR    AS attributed_officer_first_normalized,
            a.attributed_officer_last_normalized::VARCHAR     AS attributed_officer_last_normalized,
            a.match_method::VARCHAR                           AS match_method,
            '{snapshot}'::VARCHAR                             AS snapshot_date
            FROM carrier_emails e
            LEFT JOIN attribution_lookup a
              ON a.dot_number = e.dot_number
             AND a.email_address = e.email_address
        )
        TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="build + write Parquet to R2 + record ledger row")
    parser.add_argument("--dry-run", action="store_true",
                        help="build to local /tmp Parquet (no R2 upload, no ledger)")
    parser.add_argument("--sample-only", type=int, default=None, metavar="N",
                        help="Stage 0 pre-flight: rules over N sampled rows, "
                             "print tier distribution + 10-row spot-check, exit. "
                             "No R2 upload, no ledger.")
    parser.add_argument("--snapshot", default=None,
                        help="YYYY-MM-DD snapshot for BOTH inputs + output "
                             "(default: latest officers-normalized snapshot; "
                             "requires matching emails-essentials snapshot)")
    parser.add_argument("--skip-if-unchanged", action="store_true",
                        help="if target output key exists, skip rebuild "
                             "(logs no_change ledger row)")
    parser.add_argument("--workdir", default="/tmp/fmcsa_email_attributed",
                        help="local working dir (default /tmp/fmcsa_email_attributed)")
    args = parser.parse_args()

    mode_flags = [bool(args.apply), bool(args.dry_run), args.sample_only is not None]
    if sum(mode_flags) != 1:
        parser.error("must pass exactly one of --apply / --dry-run / --sample-only N")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    t0 = time.time()
    started_at = datetime.now(tz=timezone.utc)
    logger.info("started_at: %s", started_at.isoformat())

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    # Resolve snapshot — directive default is latest officers-normalized
    snapshot = args.snapshot or _detect_latest_snapshot(
        s3, R2_BUCKET, INPUT_OFFICERS_PREFIX,
    )
    logger.info("snapshot (pinned to BOTH inputs): %s", snapshot)

    emails_key, emails_bytes, officers_key, officers_bytes = _verify_snapshot_aligned(
        s3, snapshot,
    )
    emails_url = f"r2://{R2_BUCKET}/{emails_key}"
    officers_url = f"r2://{R2_BUCKET}/{officers_key}"
    output_key = f"{OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info("input emails:    %s  (%.1f MB)", emails_url, emails_bytes / 1e6)
    logger.info("input officers:  %s  (%.1f MB)", officers_url, officers_bytes / 1e6)
    logger.info("output:          %s", output_url)
    upstream_total_bytes = emails_bytes + officers_bytes

    # Idempotency check
    # Uses r2_object_is_landed so a 0-byte object is treated as nonexistent
    # and does NOT trigger the skip (poison-file defense-in-depth).
    if args.apply and args.skip_if_unchanged:
        from scripts._lib.r2_keys import r2_object_is_landed

        if r2_object_is_landed(s3, bucket=R2_BUCKET, key=output_key):
            logger.info("output key already present; skip-if-unchanged")
            _audit_no_change(snapshot, emails_url, officers_url, output_url)
            return 0

    con = _connect_duckdb_to_r2()
    _build_carrier_emails_view(con, emails_url=emails_url, officers_url=officers_url)
    emails_total = con.execute("SELECT count(*) FROM carrier_emails").fetchone()[0]
    officers_total = con.execute(
        "SELECT count(*) FROM officers_carrier_grain"
    ).fetchone()[0]
    logger.info(
        "carrier_emails (with-@): %s   officers_carrier_grain: %s",
        f"{emails_total:,}", f"{officers_total:,}",
    )

    # Sample-only mode (Stage 0): rules on N rows, print, exit.
    if args.sample_only:
        df_sample, counts, pcts, _nickname_audit = _build_attribution_lookup(
            con, sample_only=args.sample_only,
        )
        _print_spot_check(df_sample, n=10)
        ok, failures = _check_stage0_bounds(counts, pcts, len(df_sample))
        if not ok:
            logger.error("Stage 0 hard-fail bounds breached:")
            for f in failures:
                logger.error("  • %s", f)
            return 1
        logger.info("Stage 0 hard-fail bounds passed.")
        logger.info("SAMPLE-ONLY done in %.1fs", time.time() - t0)
        return 0

    # Full corpus rules application.
    df_attr, counts, pcts, nickname_audit = _build_attribution_lookup(con, sample_only=None)
    output_row_count = len(df_attr)
    logger.info("full-corpus rules applied; rows=%s", f"{output_row_count:,}")

    # Hard-gate: row count must match upstream emails count (no drops).
    if output_row_count != emails_total:
        msg = (
            f"row count mismatch post-rules: rules_output={output_row_count:,} vs "
            f"upstream emails={emails_total:,}"
        )
        logger.error("HARD FAIL: %s", msg)
        if args.apply:
            # Open a failed audit row to record this
            try:
                rid = _audit_open(snapshot, emails_url, officers_url, output_url)
                _audit_close(rid, status="failed", error=msg,
                             duration_s=time.time() - t0,
                             upstream_emails_keys=[emails_key],
                             upstream_officers_keys=[officers_key],
                             upstream_total_bytes=upstream_total_bytes)
            except Exception:
                logger.exception("also failed to record failed audit row")
        return 1

    # Hard-gate: zero NULL attribution_tier rows
    null_tier = int(df_attr["attribution_tier"].isna().sum())
    if null_tier != 0:
        msg = f"{null_tier:,} rows have NULL attribution_tier (impossible by construction)"
        logger.error("HARD FAIL: %s", msg)
        if args.apply:
            try:
                rid = _audit_open(snapshot, emails_url, officers_url, output_url)
                _audit_close(rid, status="failed", error=msg,
                             duration_s=time.time() - t0,
                             upstream_emails_keys=[emails_key],
                             upstream_officers_keys=[officers_key],
                             upstream_total_bytes=upstream_total_bytes)
            except Exception:
                logger.exception("also failed to record failed audit row")
        return 1

    ok, failures = _check_stage0_bounds(counts, pcts, output_row_count)
    if not ok:
        msg = "Stage 0 bounds breached on full corpus: " + "; ".join(failures)
        logger.error("HARD FAIL: %s", msg)
        if args.apply:
            try:
                rid = _audit_open(snapshot, emails_url, officers_url, output_url)
                _audit_close(rid, status="failed", error=msg,
                             duration_s=time.time() - t0,
                             output_row_count=output_row_count,
                             tier_counts=counts,
                             input_emails_row_count=emails_total,
                             input_officers_row_count=officers_total,
                             upstream_emails_keys=[emails_key],
                             upstream_officers_keys=[officers_key],
                             upstream_total_bytes=upstream_total_bytes)
            except Exception:
                logger.exception("also failed to record failed audit row")
        return 1

    # Register attribution_lookup as DuckDB table for the JOIN+COPY step.
    # The pandas DataFrame contains the 8 attribution cols + (dot_number,
    # email_address) join keys. NULL slot is allowed (None in pandas →
    # NULL in DuckDB).
    con.register("attribution_lookup_pdf", df_attr[[
        "dot_number", "email_address",
        "local_part_normalized", "attribution_tier",
        "attributed_officer_slot",
        "attributed_officer_name_normalized",
        "attributed_officer_first_normalized",
        "attributed_officer_last_normalized",
        "match_method",
    ]])
    con.execute(
        "CREATE TEMP TABLE attribution_lookup AS SELECT * FROM attribution_lookup_pdf"
    )

    if args.dry_run:
        # Local Parquet only — no R2 upload, no ledger
        Path(args.workdir).mkdir(parents=True, exist_ok=True)
        local_path = Path(args.workdir) / f"data_{snapshot}.parquet"
        local_url = f"'{local_path}'"
        # Reuse _write_output_parquet by faking the URL; but DuckDB's COPY
        # to a local path uses no scheme. Do the COPY inline here.
        logger.info("DRY RUN — writing local Parquet → %s", local_path)
        con.execute(
            f"""
            COPY (
              SELECT
                e.email_address::VARCHAR                          AS email_address,
                e.email_domain_normalized::VARCHAR                AS email_domain_normalized,
                e.is_free_mail_domain::VARCHAR                    AS is_free_mail_domain,
                e.dot_number::VARCHAR                             AS dot_number,
                e.legal_name::VARCHAR                             AS legal_name,
                e.dba_name::VARCHAR                               AS dba_name,
                e.company_officer_1::VARCHAR                      AS company_officer_1,
                e.company_officer_2::VARCHAR                      AS company_officer_2,
                e.officer_slot_count,
                e.phone::VARCHAR                                  AS phone,
                e.cell_phone::VARCHAR                             AS cell_phone,
                e.phy_street::VARCHAR                             AS phy_street,
                e.phy_city::VARCHAR                               AS phy_city,
                e.phy_state::VARCHAR                              AS phy_state,
                e.phy_zip::VARCHAR                                AS phy_zip,
                e.phy_country::VARCHAR                            AS phy_country,
                e.mailing_street::VARCHAR                         AS mailing_street,
                e.mailing_city::VARCHAR                           AS mailing_city,
                e.mailing_state::VARCHAR                          AS mailing_state,
                e.mailing_zip::VARCHAR                            AS mailing_zip,
                e.power_units::VARCHAR                            AS power_units,
                e.fleetsize::VARCHAR                              AS fleetsize,
                e.total_drivers::VARCHAR                          AS total_drivers,
                e.status_code::VARCHAR                            AS status_code,
                e.mcs150_date::VARCHAR                            AS mcs150_date,
                e.add_date::VARCHAR                               AS add_date,
                e.business_org_desc::VARCHAR                      AS business_org_desc,
                e.carrier_operation::VARCHAR                      AS carrier_operation,
                e.safety_rating::VARCHAR                          AS safety_rating,
                e.hm_ind::VARCHAR                                 AS hm_ind,
                e.bus_units::VARCHAR                              AS bus_units,
                e.dun_bradstreet_no::VARCHAR                      AS dun_bradstreet_no,
                a.local_part_normalized::VARCHAR                  AS local_part_normalized,
                a.attribution_tier::VARCHAR                       AS attribution_tier,
                CAST(a.attributed_officer_slot AS SMALLINT)       AS attributed_officer_slot,
                a.attributed_officer_name_normalized::VARCHAR     AS attributed_officer_name_normalized,
                a.attributed_officer_first_normalized::VARCHAR    AS attributed_officer_first_normalized,
                a.attributed_officer_last_normalized::VARCHAR     AS attributed_officer_last_normalized,
                a.match_method::VARCHAR                           AS match_method,
                '{snapshot}'::VARCHAR                             AS snapshot_date
                FROM carrier_emails e
                LEFT JOIN attribution_lookup a
                  ON a.dot_number = e.dot_number
                 AND a.email_address = e.email_address
            )
            TO '{local_path}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
            """
        )
        verify = con.execute(
            f"SELECT count(*) FROM read_parquet('{local_path}')"
        ).fetchone()[0]
        logger.info("local Parquet rows: %s (target %s)",
                    f"{verify:,}", f"{emails_total:,}")
        logger.info("DRY RUN done in %.1fs", time.time() - t0)
        return 0 if verify == emails_total else 1

    # --apply path
    run_id = _audit_open(snapshot, emails_url, officers_url, output_url)
    logger.info("audit run_id: %s", run_id)
    try:
        _write_output_parquet(con, snapshot, output_url)

        # Verify R2 readability + zero NULL tiers
        verify = con.execute(
            f"""
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE attribution_tier IS NULL) AS null_tier
              FROM read_parquet('{output_url}')
            """
        ).fetchone()
        wrote_total, wrote_null = int(verify[0]), int(verify[1])
        if wrote_total != emails_total or wrote_null != 0:
            err = (
                f"post-upload verify failed: rows={wrote_total:,} "
                f"(target {emails_total:,}), null_tier={wrote_null:,}"
            )
            _audit_close(
                run_id, status="failed", error=err, duration_s=time.time() - t0,
                input_emails_row_count=emails_total,
                input_officers_row_count=officers_total,
                output_row_count=wrote_total,
                tier_counts=counts,
                upstream_emails_keys=[emails_key],
                upstream_officers_keys=[officers_key],
                upstream_total_bytes=upstream_total_bytes,
            )
            logger.error("HARD FAIL: %s", err)
            return 1

        head = s3.head_object(Bucket=R2_BUCKET, Key=output_key)
        output_total_bytes = head["ContentLength"]

        duration = time.time() - t0
        _audit_close(
            run_id, status="completed",
            input_emails_row_count=emails_total,
            input_officers_row_count=officers_total,
            output_row_count=wrote_total,
            tier_counts=counts,
            duration_s=duration,
            upstream_emails_keys=[emails_key],
            upstream_officers_keys=[officers_key],
            upstream_total_bytes=upstream_total_bytes,
            output_total_bytes=output_total_bytes,
            notes={
                "tier_pcts": {k: round(v, 4) for k, v in pcts.items()},
                "role_address_dict_size": len(ROLE_ADDRESS_DICT),
                "rules_version": RULES_VERSION,
                "lib_version": NORMALIZER_VERSION,
                "supersedes_run_id": V1_SUPERSEDES_RUN_ID,
                "nickname_recovery_top20": nickname_audit,
                "nickname_recovered_total": (
                    int(df_attr["match_method"].dropna().astype(str)
                        .str.contains("_via_nickname:").sum())
                    if "match_method" in df_attr.columns else 0
                ),
            },
        )
        logger.info("─" * 60)
        logger.info("OK — run_id=%s  duration=%.1fs", run_id, duration)
        logger.info("     output: %s (%.1f MB)",
                    output_url, output_total_bytes / 1e6)
        logger.info("     rows=%s tier_high=%s tier_medium=%s tier_role=%s "
                    "tier_ambiguous=%s tier_unmapped=%s",
                    f"{wrote_total:,}",
                    f"{counts['officer_high']:,}",
                    f"{counts['officer_medium']:,}",
                    f"{counts['role_address']:,}",
                    f"{counts['ambiguous']:,}",
                    f"{counts['unmapped']:,}")
        return 0

    except Exception as exc:
        logger.exception("build failed")
        try:
            _audit_close(
                run_id, status="failed", error=str(exc)[:500],
                duration_s=time.time() - t0,
                upstream_emails_keys=[emails_key],
                upstream_officers_keys=[officers_key],
                upstream_total_bytes=upstream_total_bytes,
            )
        except Exception:
            logger.exception("also failed to mark run as failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
