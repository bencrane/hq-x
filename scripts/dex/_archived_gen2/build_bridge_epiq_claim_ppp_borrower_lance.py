#!/usr/bin/env python3
"""Bridge: Epiq claims (resolved) × SBA PPP borrowers — claim-grain.

Replaces the prior identity-grain `epiq_creditor_ppp_borrower_lance`
(decommissioned). The new design joins at CLAIM grain so every bridge row
carries the full claim context (project_code, case_number, debtor entity,
claim_id, filed_date, value_display, amount buckets, PDF document URL,
remarks) alongside the matched PPP borrower context. Per-claim fan-out is
preserved — e.g. a creditor with 50 protective claims against affiliated
co-debtors produces 50 distinct bridge rows, each tied back to a specific
debtor entity in the joint case.

Join axis: (creditor_legal_name_normalized, creditor_state) ⨝
            (legal_name_normalized, borrstate) — exact equality on the
canonical `_lib.entity_name_normalize` v1.0.0 rule.

Both sides come with the join keys pre-baked at emit time. No UDF calls
during the JOIN.

Confidence tiering: computed at IDENTITY grain (one fan-out count per
unique creditor identity, one per unique PPP borrower identity), then
propagated to all claim-grain rows for that identity pair. Tier rules
unchanged from prior bridges:

    platinum = 1:1 on (epiq_identity, ppp_identity)
    gold     = 1:N or N:1
    silver   = N:M (≤50 on each side)
    rejected = >50 on either side

Identity definitions:
    epiq_identity = (creditor_legal_name_normalized, creditor_state, creditor_zip5)
    ppp_identity  = (legal_name_normalized, borrstate, borrzip)

Output URI: `polaris-warehouse/bridges/epiq_claim_ppp_borrower_lance/`
Audit:      ops.bridge_generation_runs (bridge_name='epiq_claim_ppp_borrower')
Floor:      ≥ 25,000 rows.

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/build_bridge_epiq_claim_ppp_borrower_lance.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NAME_NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("build_bridge_epiq_claim_ppp_borrower_lance")

BRIDGE_NAME = "epiq_claim_ppp_borrower"
METHOD_NAME = "legal_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "epiq_claims_resolved_lance"
SOURCE_RIGHT = "ppp_borrowers_lance"

EPIQ_CLAIMS_RESOLVED_URI = "s3://dex-raw-landing-zone/polaris-warehouse/epiq/claims_resolved_lance"
PPP_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/epiq_claim_ppp_borrower_lance"
DATASET_SLUG = "epiq_claim_ppp_borrower_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 25_000
TMP_DIR = f"/tmp/lance/epiq_claim_ppp_borrower_{os.getpid()}"


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict) -> tuple:
    import lance
    import pyarrow.compute as pc

    log.info("opening epiq.claims_resolved_lance …")
    cr_ds = lance.dataset(EPIQ_CLAIMS_RESOLVED_URI, storage_options=storage_options)
    cr_filter = (
        pc.field("creditor_legal_name_normalized").is_valid()
        & pc.field("creditor_state").is_valid()
        & ~pc.field("is_generic_creditor_marker")
    )
    cr_cols = [
        # claim context (left side carries full per-claim detail)
        "project_code",
        "source_notice_id",
        "case_name",
        "case_number",
        "debtor_name",
        "claim_id",
        "claim_number",
        "search_type",
        "filed_date",
        "filed_date_display",
        "value_display",
        "amount_list_json",
        "document_urls",
        "image_document_id",
        "dockets_json",
        "docket_numbers_json",
        "remarks",
        "creditor_name",
        # identity-resolution
        "creditor_legal_name_normalized",
        "creditor_state",
        "creditor_zip5",
        "creditor_address_base_normalized",
    ]
    cr_arrow = cr_ds.scanner(columns=cr_cols, filter=cr_filter).to_table()
    log.info("  claims_resolved (post-filter): %d rows", len(cr_arrow))

    log.info("opening sba/ppp_borrowers_lance …")
    pp_ds = lance.dataset(PPP_BORROWERS_LANCE_URI, storage_options=storage_options)
    pp_filter = (
        pc.field("legal_name_normalized").is_valid()
        & pc.field("borrstate").is_valid()
    )
    pp_cols = [
        "legal_name_normalized",
        "borrname_sample",
        "borrstate",
        "borrzip",
        "total_ppp_loans",
        "total_ppp_approval",
        "max_approval_date",
        "min_approval_date",
        "latest_loanstatus",
        "has_pending_commit",
        "franchise_brands_set",
        "naics_codes_set",
        "lender_set",
        "borrower_address_normalized",
    ]
    pp_arrow = pp_ds.scanner(columns=pp_cols, filter=pp_filter).to_table()
    log.info("  ppp borrowers (post-filter): %d rows", len(pp_arrow))

    return cr_arrow, pp_arrow, len(cr_arrow), len(pp_arrow)


def _build_match_table(
    cr_arrow,
    pp_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
):
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("cr", cr_arrow)
    con.register("ppp", pp_arrow)

    log.info(
        "  registered: claims_resolved=%d  ppp=%d",
        con.execute("SELECT COUNT(*) FROM cr").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM ppp").fetchone()[0],
    )

    # Step 1 — claim × ppp_borrower JOIN at (name, state). Carries full
    # per-claim context onto every matched row.
    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            -- claim context
            c.project_code                                    AS epiq_project_code,
            c.source_notice_id                                AS epiq_source_notice_id,
            c.case_name                                       AS epiq_case_name,
            c.case_number                                     AS epiq_case_number,
            c.debtor_name                                     AS epiq_debtor_name,
            c.claim_id                                        AS epiq_claim_id,
            c.claim_number                                    AS epiq_claim_number,
            c.search_type                                     AS epiq_search_type,
            c.filed_date                                      AS epiq_filed_date,
            c.filed_date_display                              AS epiq_filed_date_display,
            c.value_display                                   AS epiq_value_display,
            c.amount_list_json                                AS epiq_amount_list_json,
            c.document_urls                                   AS epiq_document_urls,
            c.image_document_id                               AS epiq_image_document_id,
            c.dockets_json                                    AS epiq_dockets_json,
            c.docket_numbers_json                             AS epiq_docket_numbers_json,
            c.remarks                                         AS epiq_remarks,
            c.creditor_name                                   AS epiq_creditor_name,
            -- identity (join axis — preserved for re-resolution downstream)
            c.creditor_legal_name_normalized                  AS epiq_legal_name_normalized,
            c.creditor_state                                  AS epiq_state,
            c.creditor_zip5                                   AS epiq_zip5,
            c.creditor_address_base_normalized                AS epiq_address_base_normalized,
            -- PPP context
            p.legal_name_normalized                           AS ppp_legal_name_normalized,
            p.borrname_sample                                 AS ppp_borrname_sample,
            UPPER(TRIM(p.borrstate))                          AS ppp_borrstate,
            p.borrzip                                         AS ppp_borrzip,
            p.total_ppp_loans                                 AS ppp_total_loans,
            p.total_ppp_approval                              AS ppp_total_approval,
            p.max_approval_date                               AS ppp_max_approval_date,
            p.min_approval_date                               AS ppp_min_approval_date,
            p.latest_loanstatus                               AS ppp_latest_loanstatus,
            p.has_pending_commit                              AS ppp_has_pending_commit,
            p.franchise_brands_set                            AS ppp_franchise_brands_set,
            p.naics_codes_set                                 AS ppp_naics_codes_set,
            p.lender_set                                      AS ppp_lender_set,
            p.borrower_address_normalized                     AS ppp_borrower_address_normalized,
            'legal_name_state'                                AS match_path
        FROM cr c
        JOIN ppp p
          ON p.legal_name_normalized = c.creditor_legal_name_normalized
         AND UPPER(TRIM(p.borrstate)) = c.creditor_state
        """
    )
    rows_matched_raw = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    log.info("  matched (claim × ppp_borrower, pre-tier): %d rows", rows_matched_raw)

    # Step 2 — compute fan-out at IDENTITY grain, then propagate to claim rows.
    #
    # epiq_identity = (epiq_legal_name, epiq_state, epiq_zip5) — distinct
    #   PPP borrower identities matched to this creditor identity
    # ppp_identity  = (ppp_legal_name, ppp_borrstate, ppp_borrzip) — distinct
    #   epiq creditor identities matched to this PPP borrower
    #
    # Confidence is an attribute of the IDENTITY PAIR, not the individual claim
    # — all claims from the same creditor identity inherit the same tier.
    con.execute(
        """
        CREATE TEMP TABLE epiq_id_fanout AS
        SELECT epiq_legal_name_normalized, epiq_state, epiq_zip5,
               COUNT(DISTINCT (ppp_legal_name_normalized, ppp_borrstate, ppp_borrzip))
                                                            AS epiq_fan_out
        FROM matched
        GROUP BY 1, 2, 3
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ppp_id_fanout AS
        SELECT ppp_legal_name_normalized, ppp_borrstate, ppp_borrzip,
               COUNT(DISTINCT (epiq_legal_name_normalized, epiq_state, epiq_zip5))
                                                            AS ppp_fan_out
        FROM matched
        GROUP BY 1, 2, 3
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.*,
            ef.epiq_fan_out,
            pf.ppp_fan_out,
            CASE
                WHEN ef.epiq_fan_out > {COLLISION_THRESHOLD}
                  OR pf.ppp_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ef.epiq_fan_out = 1 AND pf.ppp_fan_out = 1
                    THEN 'platinum'
                WHEN ef.epiq_fan_out = 1 OR  pf.ppp_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'                   AS generated_at,
            '{BRIDGE_VERSION}'                               AS bridge_version,
            '{bridge_run_id}'                                AS bridge_run_id
        FROM matched m
        JOIN epiq_id_fanout ef
          ON ef.epiq_legal_name_normalized = m.epiq_legal_name_normalized
         AND ef.epiq_state                 = m.epiq_state
         AND ef.epiq_zip5 IS NOT DISTINCT FROM m.epiq_zip5
        JOIN ppp_id_fanout pf
          ON pf.ppp_legal_name_normalized = m.ppp_legal_name_normalized
         AND pf.ppp_borrstate             = m.ppp_borrstate
         AND pf.ppp_borrzip IS NOT DISTINCT FROM m.ppp_borrzip
        """
    )
    con.execute(
        "CREATE TEMP TABLE bridge_match AS "
        "SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'"
    )

    row_counts = con.execute(
        """
        SELECT
          COUNT(*),
          COUNT(*) FILTER (WHERE confidence_tier='platinum'),
          COUNT(*) FILTER (WHERE confidence_tier='gold'),
          COUNT(*) FILTER (WHERE confidence_tier='silver')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        log.info("writing bridge to Lance at %s …", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        log.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        for col in (
            "epiq_legal_name_normalized",
            "epiq_state",
            "epiq_project_code",
            "epiq_case_number",
            "ppp_legal_name_normalized",
            "confidence_tier",
        ):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                log.info("BTREE on %s", col)
            except Exception as e:
                log.warning("BTREE on %s failed (non-fatal): %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            log.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            log.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def _ensure_registry() -> None:
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (legal_name_normalized, 2-letter US state). "
            "Applies _lib/entity_name_normalize.py "
            f"v{NAME_NORMALIZER_VERSION} (canonical SQL v1.0.0 rule). Both sides "
            "are pre-normalized at emit time — no UDF calls during this JOIN."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NAME_NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NAME_NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50 "
            "(fan-out computed at IDENTITY grain, propagated to claim rows)"
        ),
        rejection_rule_description="fan-out >50 on either identity side → rejected",
        input_columns_left=["creditor_legal_name_normalized", "creditor_state"],
        input_columns_right=["legal_name_normalized", "borrstate"],
        output_value_description=(
            "normalized entity name + 2-letter US state join key, "
            "fanned out to claim grain"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "Epiq11 claims (resolved) × SBA PPP borrowers — claim-grain "
            "join with full per-claim context (case, debtor, value, PDF URL) "
            "attached to matched PPP borrower identity. Each creditor's "
            "protective filings across affiliated co-debtors expand to "
            "individual bridge rows. Confidence tier computed at identity "
            "grain, propagated to claim rows."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + ledger row")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _storage_options()

    log.info(
        "bridge: %s  method=%s v%s  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NAME_NORMALIZER_VERSION,
    )
    log.info("inputs: %s + %s", SOURCE_LEFT, SOURCE_RIGHT)
    log.info("output: %s", BRIDGE_LANCE_URI)

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        log.info("bridge_run_id=%s", bridge_run_id)

    try:
        cr_arrow, pp_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            cr_arrow,
            pp_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        log.info("-" * 60)
        log.info("bridge tier distribution:")
        log.info("  rows_matched (claim grain): %d", counts["rows_matched"])
        log.info("    platinum (1:1 identity):     %d", counts["rows_tier1"])
        log.info("    gold     (1:N | N:1 ident):  %d", counts["rows_tier2"])
        log.info(
            "    silver   (N:M ≤%d ident):     %d",
            COLLISION_THRESHOLD, counts["rows_tier3"],
        )
        log.info("  rows_collision_rejected:    %d", counts["rows_collision_rejected"])

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            log.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            log.info("DRY RUN OK — no Lance / Postgres writes.  duration=%.1fs",
                     time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        log.info("OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0)
        log.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        log.exception("bridge build FAILED: %s", exc)
        if run_uuid is not None:
            fail_bridge_run(run_uuid, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
