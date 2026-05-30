"""Shared CLI scaffold for the 6 Shovels entity ingests.

Every entity CLI is the same shape — fetch (entity-specific) → R2 → emit →
register → ledger — differing only in (a) which ``EntityIngestSpec``, (b) which
``LanceEmitConfig``/Polaris table, and (c) how it turns a ``ShovelsQuerySpec``
into the stream of raw records. This module owns (a)-shared orchestration; each
entity file supplies a ``record_builder(client, query_spec) -> Iterator[dict]``
plus its source-endpoint slug.

CLI surface (Trigger-ready — the scheduler varies ONLY ``--query-spec``):

    python -m scripts.shovels.ingest_<entity> \\
        --query-spec '<json>' \\
        --snapshot-date YYYY-MM-DD \\
        [--apply | --dry-run] \\
        [--no-emit] [--size N] [--max-pages N]

``--apply`` does the full rail. ``--dry-run`` fetches + projects + counts but
writes nothing (no R2, no ledger, no emit). ``--no-emit`` lands R2 + ledger but
skips the Lance emit/register tail (used by the verify harness to control phase).
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Callable, Iterator

from scripts.shovels import _client
from scripts.shovels._client import (
    EntityIngestSpec,
    ShovelsClient,
    default_snapshot_date,
    new_run_id,
    parse_snapshot_date,
    read_usage_credits,
    require_api_key,
    run_entity_ingest,
)
from scripts.shovels._emit_register import emit_and_register
from scripts.shovels.lance_emit_configs import EMIT_CONFIGS, POLARIS_TABLES
from scripts.shovels.query_spec import ShovelsQuerySpec

RecordBuilder = Callable[[ShovelsClient, ShovelsQuerySpec], Iterator[dict]]


def run_entity_cli(
    *,
    table: str,                 # 'permits' | 'contractors' | ...
    spec: EntityIngestSpec,
    source_endpoint: str,
    record_builder: RecordBuilder,
    doc: str,
    argv: list[str] | None = None,
) -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )
    ap = argparse.ArgumentParser(description=f"Shovels {table} ingest → R2 → Lance → Polaris → ledger")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="run the full rail (fetch→R2→emit→register→ledger)")
    grp.add_argument("--dry-run", action="store_true", help="fetch + count only; write nothing")
    ap.add_argument("--query-spec", default="{}", help="ShovelsQuerySpec as JSON")
    ap.add_argument("--snapshot-date", default=None, help="YYYY-MM-DD partition (default: today UTC)")
    ap.add_argument("--no-emit", action="store_true", help="with --apply: land R2+ledger but skip Lance emit/register")
    ap.add_argument("--size", type=int, default=None, help="override query-spec page size")
    ap.add_argument("--max-pages", type=int, default=None, help="override query-spec max pages (credit guardrail)")
    ap.add_argument("--invoked-by", default="cli", help="ledger invoked_by label")
    args = ap.parse_args(argv)

    try:
        query_spec = ShovelsQuerySpec.from_json(args.query_spec)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        logging.error("invalid --query-spec: %s", exc)
        return 64
    if args.size is not None:
        query_spec.size = args.size
    if args.max_pages is not None:
        query_spec.max_pages = args.max_pages

    snapshot_date = parse_snapshot_date(args.snapshot_date) if args.snapshot_date else default_snapshot_date()
    run_id = new_run_id()
    query_spec_json = query_spec.to_json()

    api_key = require_api_key()
    apply = bool(args.apply)

    logging.info("entity=%s table=shovels_%s_lance snapshot_date=%s run_id=%s apply=%s",
                 spec.entity, table, snapshot_date, run_id, apply)
    logging.info("query_spec=%s", query_spec_json)

    with ShovelsClient(api_key) as client:
        credits_before = read_usage_credits(client) if apply else None

        records = record_builder(client, query_spec)
        result = run_entity_ingest(
            spec=spec,
            source_endpoint=source_endpoint,
            record_iter=records,
            client=client,
            query_spec_json=query_spec_json,
            snapshot_date=snapshot_date,
            run_id=run_id,
            invoked_by=args.invoked_by,
            apply=apply,
        )

        logging.info(
            "FETCH DONE: rows=%d credits_spent(run)=%d api_calls=%d r2_key=%s",
            result.parquet_row_count, result.credits_spent, result.api_calls, result.r2_key,
        )

        if not apply:
            logging.info("DRY RUN complete — no writes performed.")
            return 0

        if args.no_emit:
            logging.info("--no-emit: skipping Lance emit/register (R2 + ledger r2_landed only).")
            credits_after = read_usage_credits(client)
            _log_credit_delta(credits_before, credits_after)
            return 0

    # Emit + register outside the HTTP client context (no more API calls needed).
    if result.parquet_row_count == 0:
        logging.warning(
            "0 rows fetched for %s — skipping Lance emit (nothing to rebuild). "
            "Ledger left at r2_landed.", spec.entity,
        )
        _client.ledger_finalize(run_id=run_id, status="completed_empty", lance_rows=0)
        return 0

    emit_config = EMIT_CONFIGS[table]
    polaris_table = POLARIS_TABLES[table]
    metrics = emit_and_register(
        emit_config=emit_config,
        polaris_table=polaris_table,
        run_id=run_id,
        entity=spec.entity,
        doc=doc,
    )
    logging.info(
        "RAIL COMPLETE: shovels_%s_lance rows=%s (parquet_this_run=%d, credits_this_run=%d)",
        table, metrics.get("lance_rows"), result.parquet_row_count, result.credits_spent,
    )
    return 0


def _log_credit_delta(before: int | None, after: int | None) -> None:
    if before is not None and after is not None:
        logging.info("CREDITS: usage_before=%d usage_after=%d delta=%d", before, after, after - before)
