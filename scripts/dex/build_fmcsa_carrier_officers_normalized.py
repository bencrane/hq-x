#!/usr/bin/env python3
"""DuckDB-on-R2 derived-Parquet builder: FMCSA carrier officers (slot fanout)
with normalized officer-name columns.

Reads `fmcsa-carrier-essentials/snapshot=*/data.parquet` (the Phase 1
derived essentials Parquet), fans out company_officer_1 / company_officer_2
into one officer-row per (dot_number, officer_slot), then applies
`_lib/person_name_normalize.py` v1.0.0 to officer_name_raw to produce
three normalized columns (officer_name_normalized, officer_first_normalized,
officer_last_normalized).

Output: s3://dex-raw-landing-zone/fmcsa-derived/officer_normalized/
        snapshot=<YYYY-MM-DD>/data.parquet

This is the foundation MV for the FMCSA-officer × downstream-source bridge
family (FMCSA-officer × FEC-donor by name+state, FMCSA-officer ×
DEF-14A-executive, FMCSA-officer × DOL-5500-plan-admin). Downstream
consumers join on (officer_first_normalized, officer_last_normalized,
phy_state).

Architecture (Pattern B per ~/Desktop/hq/inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md):
  1. DuckDB-on-R2 reads fmcsa-carrier-essentials Parquet directly.
  2. SQL slot-fanout via UNION ALL of slot-1 + slot-2 with NULL/empty filter.
  3. Pre-aggregate by raw officer_name to apply UDF only on unique names
     (~250K-500K unique strings vs 4.5M rows, 10-20× UDF call reduction).
  4. py_normalize_person_name_for_duckdb UDF returns "last|first" pipe-delim.
  5. Join normalized result back; write Parquet via DuckDB COPY → r2://.
  6. Ledger row in ops.fmcsa_derived_officer_normalized_r2_ingest_runs.

Per directive ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-officers-normalized.md.
Predecessor: build_bridge_fec_990_namestate.py (PR #286) — same lib,
same DuckDB-on-R2 + Python normalization architecture.

Lessons applied:
  L0 worktree path discipline / L1 Doppler shell expansion / L2 all-VARCHAR
  output (with one typed officer_slot SMALLINT col matching DDL) /
  L29 no producer-side casting beyond VARCHAR / L31 normalizer module
  __version__ stays in sync / L34 Python UDF on per-unique-name dedup /
  L42 plain .parquet extension, no Content-Encoding (DuckDB COPY handles
  this correctly) / L45 snapshot-stamped output keys.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with nameparser \\
    --with unidecode python \\
    apps/data-engine-x/scripts/build_fmcsa_carrier_officers_normalized.py --dry-run

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with nameparser \\
    --with unidecode python \\
    apps/data-engine-x/scripts/build_fmcsa_carrier_officers_normalized.py --apply

  # Idempotent re-run: if target snapshot key already exists, skip rebuild.
  # NOTE per L45: same-snapshot re-runs against an existing key DO NOT
  # propagate to the downstream RW source (S3_V2 connector marks the key
  # as already-consumed). To force a re-build that propagates, pass
  # --snapshot <next-day> (e.g. tomorrow's date).
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with nameparser \\
    --with unidecode python \\
    apps/data-engine-x/scripts/build_fmcsa_carrier_officers_normalized.py \\
      --apply --skip-if-unchanged
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.person_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
    py_normalize_person_name_for_duckdb,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_fmcsa_carrier_officers_normalized")

R2_BUCKET = "dex-raw-landing-zone"
INPUT_PREFIX = "fmcsa-derived/carrier_essentials"
OUTPUT_PREFIX = "fmcsa-derived/officer_normalized"
AUDIT_TABLE = "ops.fmcsa_derived_officer_normalized_r2_ingest_runs"

# Normalizer empty-rate gate per directive §Verification harness gate 1c:
# the lib may return empty for unparseable names. If >5% of output rows
# have NULL/empty first OR last, fail the build.
EMPTY_RATE_GATE = 0.05


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
    con.create_function(
        "py_normalize_person",
        py_normalize_person_name_for_duckdb,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
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


def _audit_open(snapshot: str, input_glob: str, output_url: str) -> str:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}
                  (snapshot_date, upstream_input_prefix, output_r2_prefix,
                   status, triggered_by)
                VALUES (%s, %s, %s, 'running',
                        'manual:build_fmcsa_carrier_officers_normalized')
                RETURNING run_id;
                """,
                (snapshot, input_glob, output_url),
            )
            return str(cur.fetchone()[0])


def _audit_close(
    run_id: str,
    *,
    status: str,
    input_row_count: int | None = None,
    output_row_count: int | None = None,
    slot_1_count: int | None = None,
    slot_2_count: int | None = None,
    duration_s: float | None = None,
    error: str | None = None,
    upstream_keys: list[str] | None = None,
    upstream_total_bytes: int | None = None,
    output_total_bytes: int | None = None,
) -> None:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {AUDIT_TABLE}
                   SET status = %s,
                       input_row_count_essentials = %s,
                       output_row_count_officers = %s,
                       slot_1_count = %s,
                       slot_2_count = %s,
                       duration_seconds = %s,
                       error_message = %s,
                       upstream_object_keys = %s,
                       upstream_total_bytes = %s,
                       output_object_count = 1,
                       output_total_bytes = %s,
                       ended_at = now()
                 WHERE run_id = %s;
                """,
                (
                    status,
                    input_row_count,
                    output_row_count,
                    slot_1_count,
                    slot_2_count,
                    duration_s,
                    error,
                    upstream_keys,
                    upstream_total_bytes,
                    output_total_bytes,
                    run_id,
                ),
            )


def _audit_no_change(snapshot: str, input_glob: str, output_url: str) -> None:
    """Insert a no_change row directly (status=no_change skips _audit_open/close)."""
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}
                  (snapshot_date, upstream_input_prefix, output_r2_prefix,
                   status, triggered_by, ended_at)
                VALUES (%s, %s, %s, 'no_change',
                        'manual:build_fmcsa_carrier_officers_normalized', now());
                """,
                (snapshot, input_glob, output_url),
            )


def _build_normalized_table(con, *, input_url: str) -> dict[str, int]:
    """Materialize officers_normalized TEMP TABLE; return row-count metrics."""
    logger.info("reading upstream essentials …")
    con.execute(
        f"""
        CREATE TEMP TABLE essentials AS
        SELECT * FROM read_parquet('{input_url}')
        """
    )
    input_row_count = con.execute("SELECT count(*) FROM essentials").fetchone()[0]
    logger.info(f"  essentials rows: {input_row_count:,}")

    logger.info("slot-fanout (UNION ALL slot-1 + slot-2, filter NULL/empty) …")
    # 31 cols mirroring mv_fmcsa_carrier_officers (Phase 2.5) carrier-context
    # passthrough, then officer_slot + officer_name_raw. is_free_mail_domain
    # is the only typed col here (BOOLEAN); everything else stays VARCHAR.
    con.execute(
        """
        CREATE TEMP TABLE officers_raw AS
        SELECT
          dot_number,
          1::SMALLINT             AS officer_slot,
          company_officer_1       AS officer_name_raw,
          legal_name,
          dba_name,
          email_address,
          email_domain_normalized,
          is_free_mail_domain,
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
        FROM essentials
        WHERE company_officer_1 IS NOT NULL AND TRIM(company_officer_1) <> ''
        UNION ALL
        SELECT
          dot_number,
          2::SMALLINT             AS officer_slot,
          company_officer_2       AS officer_name_raw,
          legal_name,
          dba_name,
          email_address,
          email_domain_normalized,
          is_free_mail_domain,
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
        FROM essentials
        WHERE company_officer_2 IS NOT NULL AND TRIM(company_officer_2) <> ''
        """
    )
    counts_row = con.execute(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE officer_slot = 1) AS s1,
               count(*) FILTER (WHERE officer_slot = 2) AS s2
          FROM officers_raw
        """
    ).fetchone()
    total_officers, s1_count, s2_count = counts_row
    logger.info(
        f"  officers_raw: {total_officers:,} (s1={s1_count:,} / s2={s2_count:,})"
    )

    # L34 mitigation: pre-aggregate by raw officer_name to slash UDF call count.
    # The same officer name (e.g. "ROBERT SMITH") repeats across many DOTs;
    # normalize once per unique string then join back.
    logger.info("pre-aggregating distinct officer_name_raw values for UDF dedup …")
    distinct_names = con.execute(
        """
        CREATE TEMP TABLE name_dedup AS
        SELECT DISTINCT officer_name_raw
          FROM officers_raw
         WHERE officer_name_raw IS NOT NULL
        """
    )
    n_unique = con.execute("SELECT count(*) FROM name_dedup").fetchone()[0]
    logger.info(
        f"  unique raw names: {n_unique:,} "
        f"(reduction: {total_officers / max(n_unique, 1):.1f}x)"
    )

    logger.info("normalizing via py_normalize_person UDF on unique names …")
    con.execute(
        """
        CREATE TEMP TABLE name_normalized AS
        SELECT
          officer_name_raw,
          py_normalize_person(officer_name_raw) AS norm_pipe
          FROM name_dedup
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE name_split AS
        SELECT
          officer_name_raw,
          CASE WHEN norm_pipe IS NULL THEN NULL
               ELSE string_split(norm_pipe, '|')[1] END AS officer_last_normalized,
          CASE WHEN norm_pipe IS NULL THEN NULL
               ELSE string_split(norm_pipe, '|')[2] END AS officer_first_normalized,
          CASE WHEN norm_pipe IS NULL THEN NULL
               ELSE concat(string_split(norm_pipe, '|')[2], ' ', string_split(norm_pipe, '|')[1])
          END AS officer_name_normalized
          FROM name_normalized
        """
    )

    logger.info("joining normalized columns back to officer rows …")
    con.execute(
        """
        CREATE TEMP TABLE officers_normalized AS
        SELECT
          o.dot_number,
          o.officer_slot,
          o.officer_name_raw,
          n.officer_name_normalized,
          n.officer_first_normalized,
          n.officer_last_normalized,
          o.legal_name,
          o.dba_name,
          o.email_address,
          o.email_domain_normalized,
          o.is_free_mail_domain,
          o.phone,
          o.cell_phone,
          o.phy_street,
          o.phy_city,
          o.phy_state,
          o.phy_zip,
          o.phy_country,
          o.mailing_street,
          o.mailing_city,
          o.mailing_state,
          o.mailing_zip,
          o.power_units,
          o.fleetsize,
          o.total_drivers,
          o.status_code,
          o.mcs150_date,
          o.add_date,
          o.business_org_desc,
          o.carrier_operation,
          o.safety_rating,
          o.hm_ind,
          o.bus_units,
          o.dun_bradstreet_no
          FROM officers_raw o
          LEFT JOIN name_split n
            ON n.officer_name_raw = o.officer_name_raw
        """
    )
    output_row_count = con.execute(
        "SELECT count(*) FROM officers_normalized"
    ).fetchone()[0]
    logger.info(f"  officers_normalized: {output_row_count:,}")

    # Empty-rate gate per directive §Verification harness gate 1c.
    empty_first = con.execute(
        """
        SELECT count(*) FROM officers_normalized
         WHERE officer_first_normalized IS NULL
            OR officer_first_normalized = ''
        """
    ).fetchone()[0]
    empty_last = con.execute(
        """
        SELECT count(*) FROM officers_normalized
         WHERE officer_last_normalized IS NULL
            OR officer_last_normalized = ''
        """
    ).fetchone()[0]
    pct_first = empty_first / max(output_row_count, 1)
    pct_last = empty_last / max(output_row_count, 1)
    logger.info(
        f"  empty first_normalized: {empty_first:,} ({pct_first:.1%}) "
        f"/ empty last_normalized: {empty_last:,} ({pct_last:.1%})"
    )

    return {
        "input_row_count": input_row_count,
        "output_row_count": output_row_count,
        "slot_1_count": s1_count,
        "slot_2_count": s2_count,
        "empty_first": empty_first,
        "empty_last": empty_last,
        "empty_first_pct": pct_first,
        "empty_last_pct": pct_last,
    }


def _write_output_parquet(con, snapshot: str) -> str:
    """Write to local file first, then upload via boto3 with explicit
    ContentType (no ContentEncoding per L42)."""
    output_key = f"{OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"writing officer_normalized → {output_url}")
    # All-VARCHAR (writer side) per L2 #1, except officer_slot SMALLINT and
    # is_free_mail_domain (kept as boolean from upstream — DuckDB casts to
    # 'true'/'false' strings via ::VARCHAR explicit cast in the COPY SELECT).
    # snapshot_date is added here as VARCHAR for the writer; RW DDL declares
    # CHARACTER VARYING and the MV consumer can cast.
    con.execute(
        f"""
        COPY (
          SELECT
            dot_number::VARCHAR                  AS dot_number,
            officer_slot,
            officer_name_raw::VARCHAR            AS officer_name_raw,
            officer_name_normalized::VARCHAR     AS officer_name_normalized,
            officer_first_normalized::VARCHAR    AS officer_first_normalized,
            officer_last_normalized::VARCHAR     AS officer_last_normalized,
            legal_name::VARCHAR                  AS legal_name,
            dba_name::VARCHAR                    AS dba_name,
            email_address::VARCHAR               AS email_address,
            email_domain_normalized::VARCHAR     AS email_domain_normalized,
            is_free_mail_domain::VARCHAR         AS is_free_mail_domain,
            phone::VARCHAR                       AS phone,
            cell_phone::VARCHAR                  AS cell_phone,
            phy_street::VARCHAR                  AS phy_street,
            phy_city::VARCHAR                    AS phy_city,
            phy_state::VARCHAR                   AS phy_state,
            phy_zip::VARCHAR                     AS phy_zip,
            phy_country::VARCHAR                 AS phy_country,
            mailing_street::VARCHAR              AS mailing_street,
            mailing_city::VARCHAR                AS mailing_city,
            mailing_state::VARCHAR               AS mailing_state,
            mailing_zip::VARCHAR                 AS mailing_zip,
            power_units::VARCHAR                 AS power_units,
            fleetsize::VARCHAR                   AS fleetsize,
            total_drivers::VARCHAR               AS total_drivers,
            status_code::VARCHAR                 AS status_code,
            mcs150_date::VARCHAR                 AS mcs150_date,
            add_date::VARCHAR                    AS add_date,
            business_org_desc::VARCHAR           AS business_org_desc,
            carrier_operation::VARCHAR           AS carrier_operation,
            safety_rating::VARCHAR               AS safety_rating,
            hm_ind::VARCHAR                      AS hm_ind,
            bus_units::VARCHAR                   AS bus_units,
            dun_bradstreet_no::VARCHAR           AS dun_bradstreet_no,
            '{snapshot}'::VARCHAR                AS snapshot_date
            FROM officers_normalized
        )
        TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    return output_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="build + write Parquet to R2 + record ledger row")
    parser.add_argument("--dry-run", action="store_true",
                        help="row counts + empty-rate only, no R2 / Postgres writes")
    parser.add_argument("--snapshot", default=None,
                        help="YYYY-MM-DD output snapshot label (default: latest "
                             "fmcsa-carrier-essentials snapshot)")
    parser.add_argument("--skip-if-unchanged", action="store_true",
                        help="if a derived Parquet already exists at the target "
                             "snapshot key, skip rebuild (logs no_change ledger row)")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    t0 = time.time()
    started_at = datetime.now(tz=timezone.utc)

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    snapshot = args.snapshot or _detect_latest_snapshot(s3, R2_BUCKET, INPUT_PREFIX)
    logger.info(f"normalizer: _lib/person_name_normalize.py v{NORMALIZER_VERSION}")
    logger.info(f"snapshot: {snapshot}")

    input_key = f"{INPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    input_url = f"r2://{R2_BUCKET}/{input_key}"
    output_key = f"{OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"input:  {input_url}")
    logger.info(f"output: {output_url}")

    # Verify input exists + grab its size for the ledger
    try:
        head = s3.head_object(Bucket=R2_BUCKET, Key=input_key)
        upstream_total_bytes = head["ContentLength"]
        upstream_keys = [input_key]
    except Exception as exc:
        raise SystemExit(f"FAIL: input Parquet missing — {exc}")
    logger.info(f"upstream object size: {upstream_total_bytes/1e6:.1f} MB")

    # Idempotency check (--skip-if-unchanged)
    # Uses r2_object_is_landed so a 0-byte object is treated as nonexistent
    # and does NOT trigger the skip (poison-file defense-in-depth).
    if args.skip_if_unchanged:
        from scripts._lib.r2_keys import r2_object_is_landed

        if r2_object_is_landed(s3, bucket=R2_BUCKET, key=output_key):
            logger.info(f"output key already present at {output_url}; skip-if-unchanged")
            if args.apply:
                _audit_no_change(snapshot, input_url, output_url)
            return 0

    con = _connect_duckdb_to_r2()

    if args.dry_run:
        try:
            metrics = _build_normalized_table(con, input_url=input_url)
        except Exception:
            logger.exception("dry-run build failed")
            raise
        logger.info("─" * 60)
        logger.info("DRY RUN summary:")
        logger.info(f"  input_row_count:    {metrics['input_row_count']:,}")
        logger.info(f"  output_row_count:   {metrics['output_row_count']:,}")
        logger.info(
            f"  slots: s1={metrics['slot_1_count']:,} / s2={metrics['slot_2_count']:,}"
        )
        logger.info(
            f"  empty first: {metrics['empty_first']:,} ({metrics['empty_first_pct']:.1%}) "
            f"/ empty last: {metrics['empty_last']:,} ({metrics['empty_last_pct']:.1%})"
        )
        logger.info(f"  duration: {time.time()-t0:.1f}s")
        if max(metrics["empty_first_pct"], metrics["empty_last_pct"]) > EMPTY_RATE_GATE:
            logger.error(
                f"FAIL gate: empty-rate exceeds {EMPTY_RATE_GATE:.0%} threshold"
            )
            return 1
        logger.info("DRY RUN — no R2 / Postgres writes.")
        return 0

    # --apply path
    run_id = _audit_open(snapshot, input_url, output_url)
    logger.info(f"audit run_id: {run_id}")

    try:
        metrics = _build_normalized_table(con, input_url=input_url)

        # HARD FAIL the empty-rate gate before writing.
        if max(metrics["empty_first_pct"], metrics["empty_last_pct"]) > EMPTY_RATE_GATE:
            err = (
                f"empty-rate gate exceeded: first={metrics['empty_first_pct']:.1%} "
                f"last={metrics['empty_last_pct']:.1%} (threshold {EMPTY_RATE_GATE:.0%})"
            )
            _audit_close(
                run_id,
                status="failed",
                input_row_count=metrics["input_row_count"],
                output_row_count=metrics["output_row_count"],
                slot_1_count=metrics["slot_1_count"],
                slot_2_count=metrics["slot_2_count"],
                duration_s=time.time() - t0,
                error=err,
                upstream_keys=upstream_keys,
                upstream_total_bytes=upstream_total_bytes,
            )
            logger.error(f"HARD FAIL: {err}")
            return 1

        _ = _write_output_parquet(con, snapshot)

        # Verify R2 readability post-upload
        verify = con.execute(
            f"SELECT count(*) FROM read_parquet('{output_url}')"
        ).fetchone()[0]
        if verify != metrics["output_row_count"]:
            err = (
                f"post-upload row count mismatch: wrote {metrics['output_row_count']:,}, "
                f"R2 readback {verify:,}"
            )
            _audit_close(
                run_id, status="failed", duration_s=time.time() - t0, error=err,
                upstream_keys=upstream_keys, upstream_total_bytes=upstream_total_bytes,
            )
            logger.error(f"HARD FAIL: {err}")
            return 1

        head = s3.head_object(Bucket=R2_BUCKET, Key=output_key)
        output_total_bytes = head["ContentLength"]

        duration = time.time() - t0
        _audit_close(
            run_id,
            status="completed",
            input_row_count=metrics["input_row_count"],
            output_row_count=metrics["output_row_count"],
            slot_1_count=metrics["slot_1_count"],
            slot_2_count=metrics["slot_2_count"],
            duration_s=duration,
            upstream_keys=upstream_keys,
            upstream_total_bytes=upstream_total_bytes,
            output_total_bytes=output_total_bytes,
        )
        logger.info("─" * 60)
        logger.info(f"OK — run_id={run_id}  duration={duration:.1f}s")
        logger.info(f"     output: {output_url}  ({output_total_bytes/1e6:.1f} MB)")
        logger.info(f"     rows={metrics['output_row_count']:,} "
                    f"s1={metrics['slot_1_count']:,} s2={metrics['slot_2_count']:,}")
        return 0

    except Exception as exc:
        logger.exception("build failed")
        try:
            _audit_close(
                run_id, status="failed", duration_s=time.time() - t0,
                error=str(exc)[:500],
                upstream_keys=upstream_keys, upstream_total_bytes=upstream_total_bytes,
            )
        except Exception:
            logger.exception("also failed to mark run as failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
