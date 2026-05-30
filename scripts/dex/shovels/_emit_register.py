"""Post-ingest tail: Lance emit → Polaris register → ledger lance_rows stamp.

Each entity CLI's ``--apply`` runs fetch→R2 (via ``_client.run_entity_ingest``)
and then this tail to complete the rail:

  1. ``emit_lance(<config>)`` — rebuild the Lance dataset from the full
     ``snapshot=*`` glob, deduped latest-per-PK, with a BTREE on the PK
     (overwrite + compact + cleanup). Reuses ``scripts/_lib/lance_emit.py``.
  2. Polaris Generic Table registration — reuses
     ``scripts/init_polaris_lance_generic.py`` (idempotent; create-or-verify).
  3. Stamp ``lance_rows`` and flip the ledger run to ``completed``.

Kept separate from the fetch driver so a CLI can choose ``--emit/--no-emit`` and
so the verify harness can call emit independently.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from scripts._lib.lance_emit import LanceEmitConfig, emit_lance
from scripts.shovels import _client
from scripts.shovels.lance_emit_configs import POLARIS_NAMESPACE

LOG = logging.getLogger("shovels.emit_register")

_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent  # apps/.../scripts
_REGISTER_SCRIPT = _SCRIPTS_ROOT / "init_polaris_lance_generic.py"


def emit_and_register(
    *,
    emit_config: LanceEmitConfig,
    polaris_table: str,
    run_id: str | None,
    entity: str,
    doc: str,
) -> dict:
    """Run the Lance emit + Polaris registration + ledger stamp. Returns the
    emit metrics dict (includes ``lance_rows``)."""
    LOG.info("=== Lance emit: %s ===", emit_config.dataset_slug)
    metrics = emit_lance(emit_config)
    lance_rows = int(metrics.get("lance_rows", 0))

    LOG.info("=== Polaris register: %s.%s ===", POLARIS_NAMESPACE, polaris_table)
    _register_polaris(table=polaris_table, doc=doc)

    if run_id is not None:
        _client.ledger_finalize(
            run_id=run_id,
            status="completed",
            lance_rows=lance_rows,
        )
        LOG.info("ledger run %s -> completed (lance_rows=%d)", run_id, lance_rows)

    return metrics


def _register_polaris(*, table: str, doc: str) -> None:
    """Invoke the canonical Polaris registration helper as a subprocess.

    Run as a subprocess (not imported) because the helper is a ``main()``-style
    CLI with its own ``sys.exit`` codes; subprocess isolation keeps those from
    tearing down the ingest process and matches how every other source registers.
    """
    cmd = [
        sys.executable,
        str(_REGISTER_SCRIPT),
        "--namespace", POLARIS_NAMESPACE,
        "--table", table,
        "--doc", doc,
    ]
    LOG.info("running: %s", " ".join(cmd[:-1] + ["<doc>"]))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            LOG.info("[polaris] %s", line)
    if result.returncode != 0:
        LOG.error("[polaris] stderr: %s", result.stderr.strip()[:1000])
        raise RuntimeError(
            f"Polaris registration failed for {table} (exit {result.returncode})"
        )
