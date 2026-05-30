"""Modal app: data-engine-x-sec-bdc-soi.

Wraps the SEC BDC Schedule of Investments ingest pipeline for unattended
monthly cadence. Delegates, in order:

  s3  scripts/run_sec_bdc_soi_r2_ingest.py   — BDC Data Set zips → R2 ZSTD Parquet
  s4  scripts/parse_sec_bdc_soi_html.py      — Inline-XBRL HTML → maturity_date parse
  s5  scripts/run_sec_bdc_soi_lance_emit.py  — structured ⋈ parsed → Lance dataset

Monthly cron at 14:00 UTC on the 8th — SEC refreshes the BDC Data Sets
monthly; the 8th leaves a margin after the publish. HEAD-check
skip-if-unchanged (s3) keeps an unchanged month a cheap no-op.

Entrypoints:
  - monthly_refresh()                  — cron-fired; s3 (skip-if-unchanged) → s4 → s5
  - run_bdc_soi_backfill(periods=None) — manual backfill via `modal run --detach`;
                                         ingests ALL discovered BDC Data Set
                                         periods → R2, parses every period's
                                         filing HTML, emits the Lance dataset.

NOTE: Polaris registration is NOT delegated here — it is the discrete s7 step
(`scripts/init_polaris_lance_generic.py --namespace sec_bdc --table soi_lance`),
run outside Modal because Polaris registration fails silently from inside a
Modal container (runbook §Gotchas #3). After the backfill emits the Lance
dataset, run s7 separately.

Secrets: dex-db (DEX_DB_URL_DIRECT — s3's ledger), bulk-ingest-r2 (R2_*).
Image: duckdb + boto3 + httpx + psycopg[binary] + pyarrow + beautifulsoup4 +
       lxml + pylance + lancedb.

Backfill invocation (ingests 23 periods, parses ~150 filings/quarterly,
emits the union Lance dataset):
  doppler run --project hq-all --config prd -- \\
    modal run --detach apps/data-engine-x/modal/sec_bdc_soi_app.py::run_bdc_soi_backfill

DO NOT pipe a long-running `modal run` through `tee | tail` — SIGPIPE kills it.
"""
import pathlib
import uuid

import modal

app = modal.App("data-engine-x-sec-bdc-soi")

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
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
    )
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir(_LANDING_DIR, remote_path="/root/landing")
)

volume = modal.Volume.from_name("sec-bdc-soi-state", create_if_missing=True)

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
    """Run s3 → s4 → s5 in order. Shared by the cron and backfill entrypoints.

    s3 lands every period's soi.tsv + datasets/*.tsv as R2 ZSTD Parquet; s4
    parses each filing's Inline-XBRL HTML for maturity_date; s5 reads the
    structured R2 Parquet, LEFT JOINs the s4 parsed Parquet, and emits the
    Lance dataset.
    """
    import sys
    sys.path.insert(0, "/root")

    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts.run_sec_bdc_soi_r2_ingest import main as ingest_main
    from scripts.parse_sec_bdc_soi_html import main as parse_main
    from scripts.run_sec_bdc_soi_lance_emit import main as emit_main

    period_arg: list[str] = []
    if periods:
        period_arg = ["--periods", ",".join(periods)]

    ingest_args = ["--apply"] + period_arg
    if skip_if_unchanged:
        ingest_args.append("--skip-if-unchanged")

    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function=cron_function,
        run_id=run_id,
    ) as hb:
        hb.set_stage("s3_r2_ingest", {"periods": periods, "skip_if_unchanged": skip_if_unchanged})
        print(f"[sec-bdc-soi] s3 ingest: {ingest_args}", flush=True)
        ingest_main(ingest_args)

        parse_args = ["--apply"] + period_arg
        hb.set_stage("s4_html_parse")
        print(f"[sec-bdc-soi] s4 HTML parse: {parse_args}", flush=True)
        parse_main(parse_args)

        hb.set_stage("s5_lance_emit")
        print("[sec-bdc-soi] s5 Lance emit", flush=True)
        emit_main([])


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    volumes={"/state": volume},
    secrets=secrets,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 14 8 * *"),
    memory=8192,
    timeout=10800,  # 3h
)
def monthly_refresh() -> None:
    """Cron-fired monthly refresh: s3 (skip-if-unchanged) → s4 → s5."""
    _run_pipeline(skip_if_unchanged=True, periods=None, cron_function="monthly_refresh")


# retry-policy: no-retry
@app.function(
    image=image,
    volumes={"/state": volume},
    secrets=secrets,
    memory=16384,
    timeout=21600,  # 6h — full historical backfill: 23 periods + per-filing HTML parse
)
def run_bdc_soi_backfill(periods: list[str] | None = None) -> None:
    """Manual historical backfill — invoke via `modal run --detach`.

    periods=None  =>  every BDC Data Set period discovered from the SEC
                      landing page (quarterly 2022q4..2025q1 + monthly
                      2025_04..present).
    periods=["2025q1","2026_04"]  =>  scoped subset.

    Ingests each period → R2, parses every filing's Inline-XBRL HTML, then
    emits the union Lance dataset. After this completes, run the discrete s7
    Polaris-registration step.
    """
    _run_pipeline(skip_if_unchanged=False, periods=periods, cron_function="run_bdc_soi_backfill")
