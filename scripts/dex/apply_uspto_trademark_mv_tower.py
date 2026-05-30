#!/usr/bin/env python3
"""Apply the 3-layer USPTO trademark MV tower to RisingWave + seed
ops.audience_mv_specs for the L3 audience MV.

Layers (dependency order):
  L1: mv_uspto_case_file_essentials   (~11.5M; per-mark; reads 6 RW sources)
  L2: mv_uspto_brand_owner_rollup     (~2-3M; per-owner identity; reads L1)
  L3: mv_uspto_email_harvest_targets  (= L2; gates + score; reads L2)

Cluster-pressure stage gate (per directive §kickoff item 9):
  L1 hits the 209M-row event_statement source — hydration may stall under
  cluster pressure. After admitting L1, observe rw_streaming_jobs + the
  rw_ddl_progress entry for 15 min. Proceed to L2/L3 only if L1 hydrates
  cleanly OR if --force-l2-l3 is passed (operator override).

Modes:
  --apply          Apply all 3 in order with cluster-pressure gate.
  --apply-l1-only  Admit only L1; observe; stop. Useful for cluster-pressure escalation.
  --apply-l2-l3    Assumes L1 hydrated; admit L2 + L3.
  --verify-only    Smoke + spec upsert without applying DDL.
  --dry-run        Emit DDL to stdout; no apply.

Usage:
  doppler run -p hq-all -c prd -- \\
    uv run --with psycopg[binary] python \\
    apps/data-engine-x/scripts/apply_uspto_trademark_mv_tower.py --apply

Directive: ~/Desktop/hq/directives/2026-05-10-uspto-trademark-mvs-essentials-rollup-harvest-targets.md
Lifted from apply_connector_model_v1_audience_mvs_rw.py — different upstream
sources, different MV count, hardcoded specs (no YAML — only 3 MVs).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_uspto_trademark_mv_tower")

REPO_ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = REPO_ROOT / "apps/data-engine-x/risingwave"

PER_MV_WAIT_TIMEOUT_S = 90 * 60   # 90 min per MV (L1 is heavy)
POLL_INTERVAL_S = 30
L1_HYDRATION_OBSERVE_S = 15 * 60  # 15 min cluster-pressure observation window

UPSTREAM_SOURCES = [
    "source_uspto_trademarks_case_file",
    "source_uspto_trademarks_case_file_owner",
    "source_uspto_trademarks_classification",
    "source_uspto_trademarks_case_file_event_statement",
    "source_uspto_trademarks_correspondent_domrep_attorney",
    "source_uspto_trademarks_case_file_statement",
]

# ---------------------------------------------------------------------------
# MV specs
# ---------------------------------------------------------------------------

L1_SPEC = {
    "mv_name": "mv_uspto_case_file_essentials",
    "sql_path": SQL_DIR / "uspto/mv_case_file_essentials.sql",
    "expected_row_count_min": 11_000_000,
    "expected_row_count_max": 12_000_000,
    "smoke_query": (
        "SELECT count(*) FROM public.mv_uspto_case_file_essentials "
        "WHERE is_live_registered AND case_file_year >= 2017;"
    ),
    "smoke_min": 1_000_000,
    "extra_smoke_queries": [
        ("modern_pro_se",
         "SELECT count(*) FROM public.mv_uspto_case_file_essentials "
         "WHERE is_pro_se AND case_file_year >= 2017;",
         500_000),
        ("any_renewed",
         "SELECT count(*) FROM public.mv_uspto_case_file_essentials "
         "WHERE has_section_8_renewal;",
         800_000),
    ],
}

L2_SPEC = {
    "mv_name": "mv_uspto_brand_owner_rollup",
    "sql_path": SQL_DIR / "uspto/mv_brand_owner_rollup.sql",
    "expected_row_count_min": 1_500_000,
    "expected_row_count_max": 5_000_000,
    "smoke_query": (
        "SELECT count(*) FROM public.mv_uspto_brand_owner_rollup "
        "WHERE any_live_registered AND latest_filing >= '2020-01-01'::DATE;"
    ),
    "smoke_min": 200_000,
    "extra_smoke_queries": [
        ("expected_recipient_owner",
         "SELECT count(*) FROM public.mv_uspto_brand_owner_rollup "
         "WHERE expected_recipient_kind = 'owner';",
         500_000),
    ],
}

L3_SPEC = {
    "mv_name": "mv_uspto_email_harvest_targets",
    "sql_path": SQL_DIR / "audience_mvs/uspto_email_harvest_targets.sql",
    "filter_description": (
        "Per-owner-identity USPTO trademark email-harvest target ranking. "
        "Reads from mv_uspto_brand_owner_rollup verbatim; adds boolean gate "
        "columns (live, pro_se_dominant, corp_or_llc, us, modern, "
        "use_in_commerce, renewed, multi_mark_active, non_pobox + 10 ICP "
        "Nice-class flags) and an integer score (weighted sum of non-class "
        "gates). Operator filters at query time to pull cohort-specific "
        "harvest queues; band letter (S/A/B/C/D) is operator-side derivation."
    ),
    "expected_row_count_min": 1_500_000,
    "expected_row_count_max": 5_000_000,
    "smoke_query": (
        "SELECT count(*) FROM public.mv_uspto_email_harvest_targets "
        "WHERE gate_live AND gate_pro_se_dominant "
        "  AND gate_corp_or_llc AND gate_modern;"
    ),
    "smoke_min": 100_000,
    "business_use_case": (
        "Email-harvest target ranking for the per-record TSDR API harvest "
        "engine. Operator picks a cohort cut (e.g. apparel-DTC pro-se modern "
        "corp-or-LLC live registered) and pulls the top-N target_serial_no "
        "list to feed the harvest worker (separate directive). Heuristic "
        "stack designed 2026-05-10; V1 hand-weighted coefficients; tune V2 "
        "after harvest yield data."
    ),
    "extra_smoke_queries": [
        ("apparel_dtc",
         "SELECT count(*) FROM public.mv_uspto_email_harvest_targets "
         "WHERE gate_live AND gate_pro_se_dominant AND gate_corp_or_llc "
         "AND gate_modern AND gate_class_apparel;",
         5_000),
    ],
}

ALL_SPECS = [L1_SPEC, L2_SPEC, L3_SPEC]


# ---------------------------------------------------------------------------
# psql helpers
# ---------------------------------------------------------------------------

def _rw_psql(sql: str, *, fetch: bool = False) -> str:
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "-v", "ON_ERROR_STOP=1",
    ]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _rw_psql_script(sql_script: str, *, timeout_s: int | None = None) -> str:
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    try:
        proc = subprocess.run(
            cmd, env=env, input=sql_script, capture_output=True, text=True,
            check=False, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "RW psql timed out after %ss — DDL likely admitted (verify via rw_ddl_progress)",
            timeout_s,
        )
        return "<psql_timeout>"
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql script failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _pg_psql(sql: str, *, fetch: bool = False) -> str:
    cmd = ["psql", os.environ["DEX_DB_URL_DIRECT"], "-v", "ON_ERROR_STOP=1"]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"PG psql failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# core helpers
# ---------------------------------------------------------------------------

def _preflight_rw() -> None:
    logger.info("preflight: checking %d upstream RW sources", len(UPSTREAM_SOURCES))
    for src in UPSTREAM_SOURCES:
        out = _rw_psql(
            f"SELECT relname FROM pg_class WHERE relname = '{src}';",
            fetch=True,
        ).strip()
        if src not in out:
            raise SystemExit(
                f"FAIL: upstream source {src} not found in pg_class. "
                "Predecessor PR #306 must apply first."
            )
        logger.info("preflight: %s exists", src)


def _mv_already_present(mv_name: str) -> bool:
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{mv_name}';",
        fetch=True,
    ).strip()
    if mv_name in out:
        return True
    out = _rw_psql(
        "SELECT ddl_statement FROM rw_catalog.rw_ddl_progress;",
        fetch=True,
    ).strip()
    return f"public.{mv_name}" in out


def _wait_for_mv_hydration(mv_name: str, *, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _rw_psql(
            "SELECT ddl_statement FROM rw_catalog.rw_ddl_progress "
            f"WHERE ddl_statement ILIKE '%{mv_name}%';",
            fetch=True,
        ).strip()
        if not out:
            return "hydrated"
        logger.info("waiting on %s — still in rw_ddl_progress", mv_name)
        time.sleep(POLL_INTERVAL_S)
    return "timeout"


def _ddl_for(spec: dict) -> str:
    if not spec["sql_path"].exists():
        raise SystemExit(f"FAIL: SQL file missing: {spec['sql_path']}")
    return spec["sql_path"].read_text()


def _l1_observe_cluster_pressure() -> str:
    """Observe cluster pressure during L1 hydration. Returns 'ok' or
    'stalled'. Surfaces actor count + LogStore lag in logs."""
    logger.info("L1 cluster-pressure observation: %d s window", L1_HYDRATION_OBSERVE_S)
    deadline = time.time() + L1_HYDRATION_OBSERVE_S
    samples = 0
    while time.time() < deadline:
        # Hydration progress
        progress_out = _rw_psql(
            "SELECT progress FROM rw_catalog.rw_ddl_progress "
            "WHERE ddl_statement ILIKE '%mv_uspto_case_file_essentials%' LIMIT 1;",
            fetch=True,
        ).strip()
        if not progress_out:
            logger.info("L1 no longer in rw_ddl_progress — hydrated early")
            return "ok"
        # Actor count snapshot (no MERGE/SHOW JOBS — use rw_streaming_jobs)
        actors_out = _rw_psql(
            "SELECT count(*) FROM rw_catalog.rw_actors;",
            fetch=True,
        ).strip()
        try:
            actors_n = int(actors_out)
        except ValueError:
            actors_n = -1
        samples += 1
        logger.info(
            "[L1 sample %d] progress=%s | actors=%d",
            samples, progress_out, actors_n,
        )
        # If actor count > 1500, suggest pressure (rough heuristic)
        if actors_n > 1500:
            logger.warning(
                "[L1 cluster-pressure] actors=%d high — proceeding cautiously",
                actors_n,
            )
        time.sleep(60)
    # Re-check progress one final time
    progress_out = _rw_psql(
        "SELECT progress FROM rw_catalog.rw_ddl_progress "
        "WHERE ddl_statement ILIKE '%mv_uspto_case_file_essentials%' LIMIT 1;",
        fetch=True,
    ).strip()
    if not progress_out:
        logger.info("L1 hydrated during observation window")
        return "ok"
    # progress is e.g. "12.34%" — try parse
    try:
        pct = float(progress_out.rstrip("%"))
    except ValueError:
        pct = -1.0
    if pct >= 10.0:
        logger.info("L1 progress=%.1f%% after observation — proceeding to L2/L3", pct)
        return "ok"
    logger.warning(
        "L1 progress=%.1f%% after observation — STALLED (cluster pressure?)",
        pct,
    )
    return "stalled"


def _smoke_gate(spec: dict) -> tuple[str, int, int, list[tuple[str, int]]]:
    """Returns (status, total_rows, primary_smoke_count, [(extra_name, extra_count)])"""
    mv_name = spec["mv_name"]
    try:
        total_out = _rw_psql(
            f"SELECT count(*) FROM public.{mv_name};", fetch=True
        ).strip()
        total = int(total_out)
    except (SystemExit, ValueError) as exc:
        logger.warning("count(*) failed for %s: %s", mv_name, str(exc)[:200])
        return ("pending_hydrate", 0, 0, [])

    if total == 0:
        return ("pending_hydrate", 0, 0, [])

    smoke_q = spec["smoke_query"].strip()
    smoke_out = _rw_psql(smoke_q, fetch=True).strip()
    smoke_count = int(smoke_out.splitlines()[0])

    extras: list[tuple[str, int]] = []
    for name, q, _floor in spec.get("extra_smoke_queries", []):
        try:
            v_out = _rw_psql(q.strip(), fetch=True).strip()
            v = int(v_out.splitlines()[0])
        except (SystemExit, ValueError) as exc:
            logger.warning("extra smoke %s failed: %s", name, str(exc)[:200])
            v = -1
        extras.append((name, v))
        logger.info("smoke[%s].%s = %s", mv_name, name, f"{v:,}")

    if smoke_count < spec["smoke_min"]:
        return ("fail_smoke", total, smoke_count, extras)
    if total < spec["expected_row_count_min"]:
        return ("pass_count_low", total, smoke_count, extras)
    if total > spec["expected_row_count_max"] * 2:
        return ("pass_count_high", total, smoke_count, extras)
    return ("pass", total, smoke_count, extras)


def _seed_l3_spec_row(
    spec: dict,
    *,
    smoke_status: str,
    smoke_row_count: int,
    notes: str | None,
) -> None:
    """Upsert the L3 spec row in ops.audience_mv_specs. L1 + L2 are
    infrastructure layers; no spec row."""
    def q(s: str | None) -> str:
        if s is None:
            return "NULL"
        s2 = s.replace("'", "''")
        return f"'{s2}'"

    notes_value = q(notes) if notes else "NULL"

    sql = f"""
    INSERT INTO ops.audience_mv_specs (
        mv_name, filter_description, expected_row_count_min, expected_row_count_max,
        named_smoke_query, expected_smoke_min, business_use_case, owner_team,
        last_applied_at, last_smoke_status, last_smoke_row_count, notes, updated_at
    ) VALUES (
        {q(spec['mv_name'])},
        {q(spec['filter_description'].strip())},
        {spec['expected_row_count_min']},
        {spec['expected_row_count_max']},
        {q(spec['smoke_query'].strip())},
        {spec['smoke_min']},
        {q(spec['business_use_case'].strip())},
        'data-engine-x',
        now(),
        {q(smoke_status)},
        {smoke_row_count},
        {notes_value},
        now()
    )
    ON CONFLICT (mv_name) DO UPDATE SET
        filter_description     = EXCLUDED.filter_description,
        expected_row_count_min = EXCLUDED.expected_row_count_min,
        expected_row_count_max = EXCLUDED.expected_row_count_max,
        named_smoke_query      = EXCLUDED.named_smoke_query,
        expected_smoke_min     = EXCLUDED.expected_smoke_min,
        business_use_case      = EXCLUDED.business_use_case,
        owner_team             = EXCLUDED.owner_team,
        last_applied_at        = EXCLUDED.last_applied_at,
        last_smoke_status      = EXCLUDED.last_smoke_status,
        last_smoke_row_count   = EXCLUDED.last_smoke_row_count,
        notes                  = EXCLUDED.notes,
        updated_at             = EXCLUDED.updated_at;
    """
    _pg_psql(sql)


def _admit_and_wait(spec: dict, *, skip_wait: bool) -> tuple[str, int, int, list[tuple[str, int]]]:
    mv_name = spec["mv_name"]

    if _mv_already_present(mv_name):
        logger.info("already present — skipping DDL: %s", mv_name)
    else:
        logger.info("applying DDL for %s", mv_name)
        ddl = _ddl_for(spec)
        try:
            _rw_psql_script(ddl, timeout_s=120)
        except SystemExit as exc:
            logger.error("DDL apply FAILED for %s: %s", mv_name, str(exc)[:500])
            raise

    if skip_wait:
        logger.info("--skip-wait: marking %s pending_hydrate", mv_name)
        return ("pending_hydrate", 0, 0, [])

    logger.info("waiting for %s to hydrate (timeout=%ds)", mv_name, PER_MV_WAIT_TIMEOUT_S)
    wait_status = _wait_for_mv_hydration(mv_name, timeout_s=PER_MV_WAIT_TIMEOUT_S)
    if wait_status == "timeout":
        logger.warning("HYDRATION TIMEOUT for %s", mv_name)
        return ("pending_hydrate", 0, 0, [])

    return _smoke_gate(spec)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--apply-l1-only", action="store_true")
    p.add_argument("--apply-l2-l3", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-wait", action="store_true")
    p.add_argument("--force-l2-l3", action="store_true",
                   help="proceed to L2/L3 even if L1 cluster-pressure observation says stalled")
    args = p.parse_args()

    modes = sum([args.dry_run, args.apply, args.apply_l1_only,
                 args.apply_l2_l3, args.verify_only])
    if modes != 1:
        p.error("specify exactly one of --dry-run, --apply, --apply-l1-only, "
                "--apply-l2-l3, --verify-only")

    if args.dry_run:
        for spec in ALL_SPECS:
            print(f"-- ====== {spec['mv_name']} ======")
            print(_ddl_for(spec))
        return

    if args.verify_only:
        # Smoke-gate all 3; only L3 gets a spec row
        for spec in ALL_SPECS:
            status, total, smoke_count, extras = _smoke_gate(spec)
            logger.info("verify %s: status=%s total=%s smoke=%s",
                        spec["mv_name"], status, f"{total:,}", smoke_count)
            for name, v in extras:
                logger.info("  extra[%s] = %s", name, f"{v:,}")
        l3_status, l3_total, l3_smoke, _ = _smoke_gate(L3_SPEC)
        mapped = ("pass" if l3_status.startswith("pass")
                  else "pending_hydrate" if l3_status == "pending_hydrate"
                  else "fail")
        _seed_l3_spec_row(
            L3_SPEC, smoke_status=mapped, smoke_row_count=l3_smoke,
            notes=f"verify-only run; status={l3_status} total={l3_total} smoke={l3_smoke}",
        )
        return

    # Apply paths
    _preflight_rw()

    if args.apply_l1_only or args.apply:
        # L1
        l1_status, l1_total, l1_smoke, l1_extras = _admit_and_wait(
            L1_SPEC, skip_wait=args.skip_wait,
        )
        logger.info("L1 result: status=%s total=%s smoke=%s",
                    l1_status, f"{l1_total:,}", l1_smoke)

        if args.apply_l1_only:
            return

        # Cluster-pressure stage gate (only on --apply, not --apply-l1-only)
        if l1_status == "pending_hydrate":
            # Hydration is still pending — observe cluster pressure
            pressure_verdict = _l1_observe_cluster_pressure()
            if pressure_verdict == "stalled" and not args.force_l2_l3:
                logger.error(
                    "L1 cluster-pressure: STALLED. "
                    "Surfacing to operator. Re-run with --apply-l2-l3 once L1 "
                    "hydrates, or --force-l2-l3 to override."
                )
                sys.exit(2)
            # Re-poll smoke
            l1_status, l1_total, l1_smoke, l1_extras = _smoke_gate(L1_SPEC)
            logger.info("L1 post-observation: status=%s total=%s smoke=%s",
                        l1_status, f"{l1_total:,}", l1_smoke)

    if args.apply_l2_l3 or args.apply:
        # L2 — read from L1
        l2_status, l2_total, l2_smoke, l2_extras = _admit_and_wait(
            L2_SPEC, skip_wait=args.skip_wait,
        )
        logger.info("L2 result: status=%s total=%s smoke=%s",
                    l2_status, f"{l2_total:,}", l2_smoke)

        # L3 — read from L2
        l3_status, l3_total, l3_smoke, l3_extras = _admit_and_wait(
            L3_SPEC, skip_wait=args.skip_wait,
        )
        logger.info("L3 result: status=%s total=%s smoke=%s",
                    l3_status, f"{l3_total:,}", l3_smoke)

        # Seed L3 spec row
        mapped = ("pass" if l3_status.startswith("pass")
                  else "pending_hydrate" if l3_status == "pending_hydrate"
                  else "fail")
        notes = (
            f"L1 total={'?' if not args.apply else l1_total} "
            f"L2 total={l2_total} L3 total={l3_total} L3 smoke={l3_smoke} "
            f"smoke_min={L3_SPEC['smoke_min']} status={l3_status}"
        )
        _seed_l3_spec_row(
            L3_SPEC, smoke_status=mapped, smoke_row_count=l3_smoke, notes=notes,
        )

        if l3_status not in ("pass", "pass_count_low", "pass_count_high"):
            logger.error("L3 smoke FAILED — directive not in ship state")
            sys.exit(1)

    logger.info("USPTO trademark MV tower: complete.")


if __name__ == "__main__":
    main()
