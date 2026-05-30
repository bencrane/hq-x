#!/usr/bin/env python3
"""Emit USAspending recipient-grain Lance dataset on R2.

Reads from two Postgres sources mirroring the MV's exact CTEs:

  - recency_agg: ``entities.mv_usaspending_contracts_typed`` (typed MV).
    Closes predecessor PR #433's +13.8% dollar drift — that drift was caused
    by reading raw ``usaspending_contracts``, which carries extra rows at the
    action_date boundary (e.g., 2026-04-15 OPTUM row not present in the
    typed MV). Reading typed MV mirrors the MV's own recency_agg CTE.

  - set_aside_agg: ``entities.usaspending_contracts`` (raw).
    Typed MV does not project the 15 set-aside columns; raw is the only
    source. 5-year window using ``(NULLIF(action_date, '')::date >= ...)``
    to mirror the MV's exact cast and empty-string guard.

Output schema: 26 columns.
  recipient_uei (key)
  total_obligation_30d, total_obligation_90d, total_obligation_180d, total_obligation_365d
  contract_count_30d, contract_count_90d, contract_count_180d, contract_count_365d
  latest_contract_date, earliest_contract_date_365d, top_psc
  is_8a, is_hubzone, is_wosb, is_edwosb, is_sdvosb, is_vosb, is_sdb,
  is_minority_owned, is_native_american_owned, is_alaskan_native_corp,
  is_native_hawaiian_org, is_tribal_corp, is_nonprofit, is_educational, is_jv

Volume floor: 87,200 distinct recipient_uei rows (65% of MV's 134,153).
  If computed result is below this floor, the job aborts WITHOUT writing.

Production path: s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance/
Sandbox path (integration tests): s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance_sandbox/

Source: directive 2026-05-14-usaspending-recipient-grain-lance-emit-v1.md
Audit pins: recency_agg CTE verbatim from pg_matviews at 2026-05-14T01:14Z.
            set_aside_agg CTE verbatim from pg_matviews at 2026-05-14T01:14Z.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from scripts/ dir or from project root.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
# Make modal-side helpers (landing.safety, landing.r2) importable when this
# script is invoked outside Modal (e.g., pytest, local CLI). Inside Modal
# the working directory IS modal/, so 'landing.*' already resolves natively.
# We append (rather than insert) so the Modal SDK's top-level 'modal'
# package wins on 'import modal' — we only want the contents of modal/ on
# the path, not the directory itself competing with the SDK.
_MODAL_DIR = SCRIPT_DIR.parent / "modal"
if _MODAL_DIR.is_dir() and str(_MODAL_DIR) not in sys.path:
    sys.path.append(str(_MODAL_DIR))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATASET_SLUG = "usaspending_recipient_grain_lance"
LANCE_PROD_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance/"
)
LANCE_SANDBOX_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance_sandbox/"
)

# Volume floor: 65% of MV's 134,153 distinct recipient_uei rows.
MIN_RECIPIENT_FLOOR = 87_200

# ---------------------------------------------------------------------------
# SQL — verbatim mirrors of the MV's CTEs (from pg_matviews.definition,
# queried at 2026-05-14T01:14Z, reviewed at 2026-05-14T01:42Z — diff = 0).
# ---------------------------------------------------------------------------

_RECENCY_AGG_SQL = """
SET TIME ZONE 'UTC';
SET statement_timeout = 0;
SELECT
    mv_usaspending_contracts_typed.recipient_uei,
    sum(mv_usaspending_contracts_typed.federal_action_obligation)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '30 days'::interval))
        AS total_obligation_30d,
    sum(mv_usaspending_contracts_typed.federal_action_obligation)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '90 days'::interval))
        AS total_obligation_90d,
    sum(mv_usaspending_contracts_typed.federal_action_obligation)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '180 days'::interval))
        AS total_obligation_180d,
    sum(mv_usaspending_contracts_typed.federal_action_obligation)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '365 days'::interval))
        AS total_obligation_365d,
    count(DISTINCT mv_usaspending_contracts_typed.contract_award_unique_key)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '30 days'::interval))
        AS contract_count_30d,
    count(DISTINCT mv_usaspending_contracts_typed.contract_award_unique_key)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '90 days'::interval))
        AS contract_count_90d,
    count(DISTINCT mv_usaspending_contracts_typed.contract_award_unique_key)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '180 days'::interval))
        AS contract_count_180d,
    count(DISTINCT mv_usaspending_contracts_typed.contract_award_unique_key)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '365 days'::interval))
        AS contract_count_365d,
    max(mv_usaspending_contracts_typed.action_date)
        AS latest_contract_date,
    min(mv_usaspending_contracts_typed.action_date)
        FILTER (WHERE mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '365 days'::interval))
        AS earliest_contract_date_365d,
    mode() WITHIN GROUP (ORDER BY mv_usaspending_contracts_typed.product_or_service_code)
        FILTER (WHERE
            mv_usaspending_contracts_typed.action_date >= (CURRENT_DATE - '365 days'::interval)
            AND mv_usaspending_contracts_typed.product_or_service_code IS NOT NULL
            AND mv_usaspending_contracts_typed.product_or_service_code <> ''
        )
        AS top_psc
FROM entities.mv_usaspending_contracts_typed
WHERE
    mv_usaspending_contracts_typed.recipient_uei IS NOT NULL
    AND mv_usaspending_contracts_typed.recipient_uei <> ''
    AND mv_usaspending_contracts_typed.action_date IS NOT NULL
GROUP BY mv_usaspending_contracts_typed.recipient_uei
"""

_SET_ASIDE_AGG_SQL = """
SET TIME ZONE 'UTC';
SET statement_timeout = 0;
SELECT
    usaspending_contracts.recipient_uei,
    bool_or((NULLIF(usaspending_contracts.c8a_program_participant, ''::text) = 't'::text))
        AS is_8a,
    bool_or((NULLIF(usaspending_contracts.historically_underutilized_business_zone_hubzone_firm, ''::text) = 't'::text))
        AS is_hubzone,
    bool_or((NULLIF(usaspending_contracts.women_owned_small_business, ''::text) = 't'::text))
        AS is_wosb,
    bool_or((NULLIF(usaspending_contracts.economically_disadvantaged_women_owned_small_business, ''::text) = 't'::text))
        AS is_edwosb,
    bool_or((NULLIF(usaspending_contracts.service_disabled_veteran_owned_business, ''::text) = 't'::text))
        AS is_sdvosb,
    bool_or((NULLIF(usaspending_contracts.veteran_owned_business, ''::text) = 't'::text))
        AS is_vosb,
    bool_or((NULLIF(usaspending_contracts.self_certified_small_disadvantaged_business, ''::text) = 't'::text))
        AS is_sdb,
    bool_or((NULLIF(usaspending_contracts.minority_owned_business, ''::text) = 't'::text))
        AS is_minority_owned,
    bool_or((NULLIF(usaspending_contracts.native_american_owned_business, ''::text) = 't'::text))
        AS is_native_american_owned,
    bool_or((NULLIF(usaspending_contracts.alaskan_native_corporation_owned_firm, ''::text) = 't'::text))
        AS is_alaskan_native_corp,
    bool_or((NULLIF(usaspending_contracts.native_hawaiian_organization_owned_firm, ''::text) = 't'::text))
        AS is_native_hawaiian_org,
    bool_or((NULLIF(usaspending_contracts.tribally_owned_firm, ''::text) = 't'::text))
        AS is_tribal_corp,
    bool_or((NULLIF(usaspending_contracts.nonprofit_organization, ''::text) = 't'::text))
        AS is_nonprofit,
    bool_or((NULLIF(usaspending_contracts.educational_institution, ''::text) = 't'::text))
        AS is_educational,
    bool_or((
        (NULLIF(usaspending_contracts.joint_venture_economic_disadvantaged_women_owned_small_bus, ''::text) = 't'::text)
        OR (NULLIF(usaspending_contracts.joint_venture_women_owned_small_business, ''::text) = 't'::text)
    )) AS is_jv
FROM entities.usaspending_contracts
WHERE
    usaspending_contracts.recipient_uei IS NOT NULL
    AND usaspending_contracts.recipient_uei <> ''
    AND (NULLIF(usaspending_contracts.action_date, ''::text))::date >= (CURRENT_DATE - '5 years'::interval)
GROUP BY usaspending_contracts.recipient_uei
"""

# Canonical set-aside flag names (15 flags, ordered as in _SET_ASIDE_AGG_SQL).
# Directive mistakenly said 16; audit corrected to 15 (14 single-col + 1 JV).
SET_ASIDE_FLAG_NAMES = [
    "is_8a", "is_hubzone", "is_wosb", "is_edwosb", "is_sdvosb",
    "is_vosb", "is_sdb", "is_minority_owned", "is_native_american_owned",
    "is_alaskan_native_corp", "is_native_hawaiian_org", "is_tribal_corp",
    "is_nonprofit", "is_educational", "is_jv",
]


# ---------------------------------------------------------------------------
# R2 storage options
# ---------------------------------------------------------------------------

def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


# ---------------------------------------------------------------------------
# Core emit logic
# ---------------------------------------------------------------------------

def _connect_pg():
    import psycopg
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise EnvironmentError("DEX_DB_URL_DIRECT is required")
    return psycopg.connect(url, autocommit=True)


def _run_query_to_dicts(conn, sql: str) -> list[dict]:
    """Execute a SQL string (may contain a SET + SELECT) and return list of dicts."""
    # psycopg3 executes multiple statements in autocommit mode via executescript
    # or we split and run them sequentially.
    statements = [s.strip() for s in sql.strip().split(";") if s.strip()]
    rows = []
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
            if cur.description:
                col_names = [d.name for d in cur.description]
                rows = [dict(zip(col_names, row)) for row in cur.fetchall()]
    return rows


def _build_query_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def emit(target_path_override: str | None = None) -> dict:
    """Run the recipient-grain emit and return metrics dict.

    Args:
        target_path_override: if set, write Lance to this URI instead of the
            production path. Used by the integration test to write to the
            sandbox prefix.
    """
    import pyarrow as pa
    import lance

    from landing.safety import assert_landed_or_explicit_empty

    t0 = time.time()
    checked_at = datetime.now(timezone.utc).isoformat()

    lance_uri = target_path_override or LANCE_PROD_URI
    LOG.info("lance_uri=%s", lance_uri)

    # --- 1. Fetch recency_agg from typed MV (DEX_DB_URL_DIRECT) --------
    LOG.info("fetching recency_agg from entities.mv_usaspending_contracts_typed ...")
    with _connect_pg() as conn_recency:
        recency_rows = _run_query_to_dicts(conn_recency, _RECENCY_AGG_SQL)
    LOG.info("recency_agg: %d rows", len(recency_rows))

    # --- 2. Fetch set_aside_agg from raw usaspending_contracts ---------
    LOG.info("fetching set_aside_agg from entities.usaspending_contracts (raw) ...")
    with _connect_pg() as conn_setaside:
        set_aside_rows = _run_query_to_dicts(conn_setaside, _SET_ASIDE_AGG_SQL)
    LOG.info("set_aside_agg: %d rows", len(set_aside_rows))

    # --- 3. Volume-floor probe BEFORE write ----------------------------
    recipient_count = len(recency_rows)
    if recipient_count < MIN_RECIPIENT_FLOOR:
        raise RuntimeError(
            f"Volume floor breach: recency_agg returned {recipient_count} "
            f"distinct UEIs, which is below the floor of {MIN_RECIPIENT_FLOOR}. "
            f"Aborting WITHOUT writing to Lance."
        )
    LOG.info("volume floor check passed: %d >= %d", recipient_count, MIN_RECIPIENT_FLOOR)

    # --- 4. Safety helper (poison-file guard) -------------------------
    assert_landed_or_explicit_empty(
        rows_loaded=recipient_count,
        upstream_was_empty=False,
        upstream_proof={
            "source": "entities.mv_govcontracts_recipient_targeting",
            "endpoint": "postgres://<DEX_DB_URL_DIRECT>",
            "request_params_hash": _build_query_hash(_RECENCY_AGG_SQL),
            "checked_at": checked_at,
            "recipient_count": recipient_count,
            "feed_date": datetime.now(timezone.utc).date().isoformat(),
        },
    )

    # --- 5. Join recency + set-aside on recipient_uei (LEFT JOIN) -----
    # Build a lookup dict from set_aside_rows keyed by recipient_uei.
    set_aside_by_uei: dict[str, dict] = {r["recipient_uei"]: r for r in set_aside_rows}

    def _bool(val) -> bool:
        """Coerce Postgres bool_or result (bool|None) to Python bool."""
        return bool(val) if val is not None else False

    # Merged column order: uei + 11 recency + 15 set-aside = 27 cols total.
    def _to_float(v):
        # psycopg returns numeric as decimal.Decimal; pyarrow's float64 column
        # rejects implicit Decimal coercion. Cast at the merge boundary.
        return float(v) if v is not None else None

    merged_rows: list[dict] = []
    for rec in recency_rows:
        uei = rec["recipient_uei"]
        sa = set_aside_by_uei.get(uei, {})
        merged_rows.append({
            "recipient_uei": uei,
            "total_obligation_30d": _to_float(rec.get("total_obligation_30d")),
            "total_obligation_90d": _to_float(rec.get("total_obligation_90d")),
            "total_obligation_180d": _to_float(rec.get("total_obligation_180d")),
            "total_obligation_365d": _to_float(rec.get("total_obligation_365d")),
            "contract_count_30d": rec.get("contract_count_30d"),
            "contract_count_90d": rec.get("contract_count_90d"),
            "contract_count_180d": rec.get("contract_count_180d"),
            "contract_count_365d": rec.get("contract_count_365d"),
            "latest_contract_date": rec.get("latest_contract_date"),
            "earliest_contract_date_365d": rec.get("earliest_contract_date_365d"),
            "top_psc": rec.get("top_psc"),
            # 15 set-aside flags — False when no set-aside row for this UEI
            "is_8a": _bool(sa.get("is_8a")),
            "is_hubzone": _bool(sa.get("is_hubzone")),
            "is_wosb": _bool(sa.get("is_wosb")),
            "is_edwosb": _bool(sa.get("is_edwosb")),
            "is_sdvosb": _bool(sa.get("is_sdvosb")),
            "is_vosb": _bool(sa.get("is_vosb")),
            "is_sdb": _bool(sa.get("is_sdb")),
            "is_minority_owned": _bool(sa.get("is_minority_owned")),
            "is_native_american_owned": _bool(sa.get("is_native_american_owned")),
            "is_alaskan_native_corp": _bool(sa.get("is_alaskan_native_corp")),
            "is_native_hawaiian_org": _bool(sa.get("is_native_hawaiian_org")),
            "is_tribal_corp": _bool(sa.get("is_tribal_corp")),
            "is_nonprofit": _bool(sa.get("is_nonprofit")),
            "is_educational": _bool(sa.get("is_educational")),
            "is_jv": _bool(sa.get("is_jv")),
        })

    LOG.info("merged: %d rows, 27 cols", len(merged_rows))

    # --- 6. Build PyArrow Table with typed schema ---------------------
    # 1 uei + 11 recency + 15 set-aside = 27 cols
    # (audit said "10 recency + 15 set-aside = 26" — off-by-one on top_psc;
    # actual MV CTE outputs 11 recency columns including top_psc)
    schema = pa.schema([
        pa.field("recipient_uei", pa.string()),
        pa.field("total_obligation_30d", pa.float64()),
        pa.field("total_obligation_90d", pa.float64()),
        pa.field("total_obligation_180d", pa.float64()),
        pa.field("total_obligation_365d", pa.float64()),
        pa.field("contract_count_30d", pa.int64()),
        pa.field("contract_count_90d", pa.int64()),
        pa.field("contract_count_180d", pa.int64()),
        pa.field("contract_count_365d", pa.int64()),
        pa.field("latest_contract_date", pa.date32()),
        pa.field("earliest_contract_date_365d", pa.date32()),
        pa.field("top_psc", pa.string()),
        # 15 set-aside flags
        pa.field("is_8a", pa.bool_()),
        pa.field("is_hubzone", pa.bool_()),
        pa.field("is_wosb", pa.bool_()),
        pa.field("is_edwosb", pa.bool_()),
        pa.field("is_sdvosb", pa.bool_()),
        pa.field("is_vosb", pa.bool_()),
        pa.field("is_sdb", pa.bool_()),
        pa.field("is_minority_owned", pa.bool_()),
        pa.field("is_native_american_owned", pa.bool_()),
        pa.field("is_alaskan_native_corp", pa.bool_()),
        pa.field("is_native_hawaiian_org", pa.bool_()),
        pa.field("is_tribal_corp", pa.bool_()),
        pa.field("is_nonprofit", pa.bool_()),
        pa.field("is_educational", pa.bool_()),
        pa.field("is_jv", pa.bool_()),
    ])

    # Build columns from merged_rows dict list.
    # date32 fields require Python date objects (not ISO strings) — coerce.
    from datetime import date as _date

    def _to_date(val) -> "_date | None":
        if val is None:
            return None
        if isinstance(val, _date):
            return val
        if isinstance(val, str) and val:
            return _date.fromisoformat(val[:10])
        return None

    date32_fields = {f.name for f in schema if f.type == pa.date32()}

    col_names = [f.name for f in schema]
    arrays = []
    for field in schema:
        if field.name in date32_fields:
            raw = [_to_date(r[field.name]) for r in merged_rows]
        else:
            raw = [r[field.name] for r in merged_rows]
        arrays.append(pa.array(raw, type=field.type))
    table = pa.table(dict(zip(col_names, arrays)), schema=schema)
    LOG.info("arrow table: %d rows × %d cols", table.num_rows, len(table.schema))

    storage_options = _lance_storage_options()

    # --- 7. Write Lance dataset with commit lock ----------------------
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    t_write = time.time()
    with lance_commit_lock(DATASET_SLUG):
        LOG.info("writing Lance dataset (mode=overwrite) to %s ...", lance_uri)
        ds = lance.write_dataset(
            table,
            lance_uri,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = round(time.time() - t_write, 1)
        lance_count = ds.count_rows()
        LOG.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        # --- 8. BTREE scalar index on recipient_uei ------------------
        t_idx = time.time()
        LOG.info("creating BTREE scalar index on recipient_uei ...")
        ds.create_scalar_index("recipient_uei", index_type="BTREE", replace=True)
        idx_dur = round(time.time() - t_idx, 1)
        LOG.info("  index built in %.1fs", idx_dur)

        # --- 9. Compact + cleanup ------------------------------------
        LOG.info("optimize: compact + cleanup_older_than=7d ...")
        try:
            stats = ds.optimize.compact_files()
            LOG.info("  compact_files: %s", stats)
        except Exception as e:
            LOG.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:
            LOG.warning("  cleanup_old_versions failed (non-fatal): %s", e)

    total_dur = round(time.time() - t0, 1)
    metrics = {
        "dataset_slug": DATASET_SLUG,
        "lance_rows": lance_count,
        "lance_version": ds.version,
        "write_seconds": write_dur,
        "index_seconds": idx_dur,
        "total_seconds": total_dur,
        "recency_rows": len(recency_rows),
        "set_aside_rows": len(set_aside_rows),
    }
    LOG.info("OK — metrics: %s", metrics)
    return metrics


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit USAspending recipient-grain Lance dataset"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually run the emit (dry-run otherwise)",
    )
    parser.add_argument(
        "--target-path-override", default=None,
        help="Override the Lance write URI (sandbox testing)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    if not args.apply:
        LOG.info("dry-run: pass --apply to execute")
        return 0

    metrics = emit(target_path_override=args.target_path_override)
    LOG.info("emit complete: %s", metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
