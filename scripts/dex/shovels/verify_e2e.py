"""End-to-end verification harness for the Shovels ingest rail.

Asserts the acceptance criteria for all 6 canonical tables:

  1. Each ``shovels_<table>_lance`` dataset exists with row_count > 0.
  2. A BTREE scalar index is present on the table's PK.
  3. The dataset is registered as a Polaris Generic Table (shovels.<table>_lance).
  4. ``ops.shovels_ingest_runs`` has at least one ``completed`` row for the entity.
  5. (with --idempotency) Re-running an entity ingest for the same snapshot_date
     leaves the Lance row count UNCHANGED (dedup-latest-per-PK holds).

Usage:
    # structural checks (no API spend):
    doppler run -p hq-all -c prd -- bash -c 'uv run python -m scripts.shovels.verify_e2e'

    # add the idempotency re-run proof for the billable entities (re-spends the
    # per-record credits for those entities once — pass --idempotency-spec to
    # control which entities + specs are re-run):
    doppler run -p hq-all -c prd -- bash -c 'uv run python -m scripts.shovels.verify_e2e --idempotency'

Exit code 0 iff every asserted check passes.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.shovels.entity_specs import ALL_SPECS  # noqa: E402
from scripts.shovels.lance_emit_configs import (  # noqa: E402
    EMIT_CONFIGS,
    POLARIS_NAMESPACE,
    POLARIS_TABLES,
)

logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout)
LOG = logging.getLogger("shovels.verify")

# table → (entity, pk_column)
_TABLE_ENTITY = {t: (spec.entity, spec.pk_column) for t, spec in {
    "permits": ALL_SPECS["permit"],
    "contractors": ALL_SPECS["contractor"],
    "employees": ALL_SPECS["employee"],
    "residents": ALL_SPECS["resident"],
    "geo": ALL_SPECS["geo"],
    "tags": ALL_SPECS["tag"],
}.items()}

# Default idempotency re-run specs (the snapshot date these were landed under).
# Mirrors the E2E proof parameters; a re-run must not change Lance row counts.
_IDEMPOTENCY_SNAPSHOT = "2026-05-29"
_IDEMPOTENCY_SPECS = {
    "tags": {},
    "geo": {"geo_states": ["VT"], "geo_max_per_state": 25},
    "permits": {"geo_id": "ROA3LFPdyBc", "permit_from": "2025-01-01", "permit_to": "2025-02-01", "size": 10, "max_pages": 1},
    "contractors": {"geo_id": "ROA3LFPdyBc", "permit_from": "2024-01-01", "permit_to": "2025-01-01", "size": 10, "max_pages": 1, "extra_filters": {"include_tallies": True}},
    "employees": {"contractor_ids": ["1tVwtM9LC0"], "size": 10, "max_pages": 1},
    "residents": {"address_geo_ids": ["BJJzSSI7MWQ"], "size": 10, "max_pages": 1},
}


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _open_dataset(uri: str):
    import lance

    return lance.dataset(uri, storage_options=_lance_storage_options())


def check_lance_and_btree(table: str) -> tuple[bool, int, bool]:
    """Return (exists_with_rows, row_count, btree_present)."""
    cfg = EMIT_CONFIGS[table]
    _entity, pk = _TABLE_ENTITY[table]
    try:
        ds = _open_dataset(cfg.lance_uri)
    except Exception as exc:  # noqa: BLE001
        LOG.error("  [%s] Lance dataset open FAILED: %s", table, exc)
        return False, 0, False
    rows = ds.count_rows()
    # BTREE presence: list_indices() returns index metadata; match on the PK col.
    btree = False
    try:
        for idx in ds.list_indices():
            cols = idx.get("fields") or idx.get("columns") or []
            name = (idx.get("name") or "")
            itype = str(idx.get("type") or "").lower()
            on_pk = (pk in cols) or (pk in name)
            if on_pk and "btree" in itype:
                btree = True
                break
    except Exception as exc:  # noqa: BLE001
        LOG.warning("  [%s] index introspection error: %s", table, exc)
    return rows > 0, rows, btree


def check_polaris(table: str) -> bool:
    polaris_table = POLARIS_TABLES[table]
    register_script = Path(__file__).resolve().parent.parent / "init_polaris_lance_generic.py"
    result = subprocess.run(
        [
            sys.executable, str(register_script),
            "--namespace", POLARIS_NAMESPACE, "--table", polaris_table,
            "--check-only",
        ],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def check_ledger(entity: str) -> tuple[bool, int]:
    """Return (has_completed_row, completed_count) for the entity."""
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    with psycopg.connect(db_url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM ops.shovels_ingest_runs "
            "WHERE entity=%s AND status IN ('completed','completed_empty')",
            (entity,),
        ).fetchone()
    n = int(row[0]) if row else 0
    return n > 0, n


def run_idempotency(table: str, before_rows: int) -> tuple[bool, int]:
    """Re-run the entity ingest for the same snapshot and assert row count
    unchanged. Returns (passed, after_rows)."""
    spec_json = json.dumps(_IDEMPOTENCY_SPECS[table])
    module = f"scripts.shovels.ingest_{table}"
    LOG.info("  [%s] idempotency re-run (snapshot=%s) ...", table, _IDEMPOTENCY_SNAPSHOT)
    result = subprocess.run(
        [
            sys.executable, "-m", module,
            "--query-spec", spec_json,
            "--snapshot-date", _IDEMPOTENCY_SNAPSHOT,
            "--apply",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        LOG.error("  [%s] re-run FAILED: %s", table, result.stderr.strip()[-800:])
        return False, before_rows
    cfg = EMIT_CONFIGS[table]
    after = _open_dataset(cfg.lance_uri).count_rows()
    passed = after == before_rows
    LOG.info("  [%s] before=%d after=%d %s", table, before_rows, after,
             "STABLE ✓" if passed else "CHANGED ✗")
    return passed, after


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--idempotency", action="store_true",
                    help="also re-run each entity ingest and assert stable Lance row counts")
    ap.add_argument("--tables", default=None,
                    help="comma-separated subset (default: all 6)")
    args = ap.parse_args()

    tables = args.tables.split(",") if args.tables else list(POLARIS_TABLES.keys())
    all_pass = True
    summary: list[dict] = []

    for table in tables:
        entity, pk = _TABLE_ENTITY[table]
        LOG.info("=== shovels_%s_lance (entity=%s, pk=%s) ===", table, entity, pk)

        has_rows, rows, btree = check_lance_and_btree(table)
        polaris = check_polaris(table)
        ledger_ok, ledger_n = check_ledger(entity)

        LOG.info("  lance_rows=%d (>0: %s) | btree_on_%s=%s | polaris=%s | ledger_completed=%d",
                 rows, has_rows, pk, btree, polaris, ledger_n)

        row_pass = has_rows and btree and polaris and ledger_ok
        idem_pass = True
        after_rows = rows
        if args.idempotency:
            idem_pass, after_rows = run_idempotency(table, rows)

        ok = row_pass and idem_pass
        all_pass = all_pass and ok
        summary.append({
            "table": f"shovels_{table}_lance", "lance_rows": rows,
            "btree_on_pk": btree, "polaris": polaris,
            "ledger_completed_rows": ledger_n,
            "idempotent_after_rows": after_rows if args.idempotency else None,
            "PASS": ok,
        })

    LOG.info("=" * 70)
    LOG.info("SUMMARY:")
    for s in summary:
        LOG.info("  %s", json.dumps(s))
    LOG.info("=" * 70)
    LOG.info("OVERALL: %s", "PASS ✓" if all_pass else "FAIL ✗")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
