"""Modal app: data-engine-x-bdc-soi-parse-v2.

Wraps v2 parse + sample-audit for unattended monthly cadence. Delegates:
  s3  scripts/parse_sec_bdc_soi_html_v2.py    — soi.tsv structured + HTML fallback → v2 Parquet
  s5  scripts/audit_bdc_soi_parse_v2_sample.py — coverage + trust-up + trust-down audit

Monthly cron at 14:00 UTC on the 9th — staggered 1h after the v1 sec-bdc-soi
cron (Cron 0 14 8 * *) so the v1 HTML fetch + parse completes before v2 picks
up the same source HTML for re-parse. (Validator-pinned in §"Validator notes".)

Entrypoints:
  - monthly_refresh()                  — cron-fired; s3 (skip-if-unchanged) → s5 sample-audit
  - backfill_all_periods(periods="")   — manual backfill via `modal run --detach`;
                                         parses all discovered BDC Data Set
                                         periods → R2 + runs sample-audit.

Secrets: dex-db (DB; bridged to DEX_DB_URL_DIRECT/POOLED),
         bulk-ingest-r2 (R2_*).

Image: duckdb + boto3 + httpx + psycopg[binary] + pyarrow + beautifulsoup4 +
       lxml (HTML fallback parse path).

Backfill invocation:
  doppler run --project hq-all --config prd -- \\
    modal run --detach apps/data-engine-x/modal/bdc_soi_parse_v2_app.py::backfill_all_periods

DO NOT pipe a long-running `modal run` through `tee | tail` — SIGPIPE kills it.
"""
import modal
import pathlib
import uuid

app = modal.App("data-engine-x-bdc-soi-parse-v2")

# Resolve scripts dir relative to this file so deploy works from any cwd.
_SCRIPTS_DIR = str(pathlib.Path(__file__).parent.parent / "scripts" / "dex")
_LANDING_DIR = str(pathlib.Path(__file__).parent / "landing")

image = (
    modal.Image.debian_slim()
    .pip_install(
        "duckdb",
        "boto3",
        "httpx",
        "psycopg[binary]",
        "pyarrow",
        "beautifulsoup4",
        "lxml",
    )
    .add_local_dir(_SCRIPTS_DIR, remote_path="/root/scripts")
    .add_local_dir(_LANDING_DIR, remote_path="/root/landing")
)

volume = modal.Volume.from_name("bdc-soi-parse-v2-state", create_if_missing=True)

secrets = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]


def _run_pipeline(
    *,
    skip_if_unchanged: bool,
    periods: list[str] | None,
    cron_function: str,
) -> None:
    """Run s3 → s5 in order. Shared by the cron and backfill entrypoints.

    s3 reads soi.tsv structured XBRL columns (PRIMARY) and per-filing Inline-XBRL
    HTML (FALLBACK for maturity_date) → v2 Parquet at sec-bdc/soi-parsed-v2/.
    s5 runs coverage + missing-BDC probe audit (non-strict in cron — trust-up
    --strict runs operator-on-demand).
    """
    import os
    import sys

    sys.path.insert(0, "/root")

    # Bridge DATABASE_URL → DEX_DB_URL_DIRECT/POOLED (dex-db secret
    # exposes DATABASE_URL; DEX scripts expect the DEX_DB_URL_* names).
    if "DATABASE_URL" in os.environ:
        os.environ.setdefault("DEX_DB_URL_DIRECT", os.environ["DATABASE_URL"])
        os.environ.setdefault("DEX_DB_URL_POOLED", os.environ["DATABASE_URL"])

    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.parse_sec_bdc_soi_html_v2 import main as parse_main
    from scripts.audit_bdc_soi_parse_v2_sample import main as audit_main

    period_arg: list[str] = []
    if periods:
        period_arg = ["--periods", ",".join(periods)]

    parse_args = ["--apply"] + period_arg
    if skip_if_unchanged:
        parse_args.append("--skip-if-unchanged")

    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function=cron_function,
        run_id=run_id,
    ) as hb:
        hb.set_stage("s3_parse", {"periods": periods, "skip_if_unchanged": skip_if_unchanged})
        print(f"[bdc-soi-parse-v2] s3 parse: {parse_args}", flush=True)
        parse_main(parse_args)

        # Sample-audit (coverage + missing-BDC probe; non-strict so monthly cron
        # doesn't gate on one-row reconciliation flakes — trust-up --strict runs
        # in CI or operator-on-demand).
        hb.set_stage("s5_sample_audit")
        print("[bdc-soi-parse-v2] s5 sample-audit (coverage + probe-missing-bdcs)", flush=True)
        audit_main(["--coverage", "--probe-missing-bdcs"])


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    volumes={"/state": volume},
    secrets=secrets,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron("0 14 9 * *"),
    memory=32768,  # Ares 2025q1 = 2,253 rows; full corpus ~70K × 25 cols × 23 periods + HTML fallback fetches
    timeout=14400,  # 4h
)
def monthly_refresh() -> None:
    """Cron-fired monthly refresh: s3 (skip-if-unchanged) → s5 (coverage)."""
    _run_pipeline(skip_if_unchanged=True, periods=None, cron_function="monthly_refresh")


# retry-policy: no-retry
@app.function(
    image=image,
    volumes={"/state": volume},
    secrets=secrets,
    memory=32768,
    timeout=43200,  # 12h — full historical backfill: 23 periods × many filings × HTML fallback
)
def backfill_all_periods(periods: str = "") -> None:
    """Manual historical backfill — invoke via `modal run --detach`.

    periods=""  =>  every BDC Data Set period.
    periods="2025q1,2026_04"  =>  scoped subset (comma-separated).
    """
    period_list = [p.strip() for p in periods.split(",") if p.strip()] or None
    _run_pipeline(skip_if_unchanged=False, periods=period_list, cron_function="backfill_all_periods")
