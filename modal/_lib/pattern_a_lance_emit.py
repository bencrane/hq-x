"""Generic Pattern A — Lance emit Modal scaffold.

Use this to wrap any `scripts/run_<source>_<table>_lance_emit.py` script
behind a Modal cron with the standard observability + ledger discipline.

Modal requires `@app.function` decorators to apply to functions defined at
module global scope. The scaffold therefore exports:

- :class:`PatternALanceEmitConfig` — typed config dataclass.
- :func:`build_image` — returns the canonical Modal image with `dex-db` +
  `bulk-ingest-r2` secret mounts implied (the secrets list is also exported).
- :data:`ORCHESTRATOR_SECRETS` — canonical secret list.
- :func:`run_emit` — the inner work (resolve source_id → record_start →
  subprocess → record_complete). The per-app `emit` function calls this.

Each per-app file declares its `@app.function` at module scope (Modal's
requirement) and calls :func:`run_emit` inside the body. Pattern:

    # apps/data-engine-x/modal/<source>_<table>_lance_emit_app.py
    from __future__ import annotations
    import sys
    import modal
    from _lib.pattern_a_lance_emit import (
        ORCHESTRATOR_SECRETS, build_image, run_emit, PatternALanceEmitConfig,
    )

    CONFIG = PatternALanceEmitConfig(
        app_name="data-engine-x-<source>-<table>-lance-emit",
        script_path="/root/scripts/run_<source>_<table>_lance_emit.py",
        display_name="<source>_<table>_lance",
        cron_schedule="30 6 * * *",
        memory_mb=8192,
    )

    app = modal.App(CONFIG.app_name)
    image = build_image(CONFIG)

    # retry-policy: no-retry-orchestrator
    @app.function(
        image=image,
        secrets=ORCHESTRATOR_SECRETS,
        timeout=CONFIG.timeout_seconds,
        memory=CONFIG.memory_mb,
        schedule=modal.Cron(CONFIG.cron_schedule),
    )
    def emit():
        return run_emit(CONFIG)

    @app.local_entrypoint()
    def main():
        import json
        print(json.dumps(emit.remote(), indent=2, default=str))

That's ~25 LOC vs the original ~250 LOC, and the @app.function lives at
module scope (Modal's hard requirement).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import modal


@dataclass(frozen=True)
class PatternALanceEmitConfig:
    """Per-app configuration for the Pattern A Lance-emit scaffold."""

    app_name: str
    """Modal app name (e.g. 'data-engine-x-fmcsa-carrier-essentials-lance-emit')."""

    script_path: str
    """Path inside the container to the emit script
    (e.g. '/root/scripts/run_fmcsa_carrier_essentials_lance_emit.py')."""

    display_name: str
    """ops.data_sources.display_name — looked up at run start so the
    `source_id` can be stamped on the ingest-run row."""

    cron_schedule: str
    """Cron expression for `modal.Cron` (e.g. '30 6 * * *')."""

    memory_mb: int = 4096
    """Container memory. Default 4GB; bump to 8192 for >5M-row datasets,
    16384 for FMCSA-Carrier-class loads."""

    timeout_seconds: int = 60 * 60
    """Max wall-clock per container. Default 1h."""

    extra_pip_packages: tuple[str, ...] = field(default_factory=tuple)
    """Extra pip packages on top of the canonical Lance-emit set
    (DuckDB, psycopg, pylance, lancedb)."""

    extra_apt_packages: tuple[str, ...] = field(default_factory=tuple)
    """Extra apt packages."""


ORCHESTRATOR_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]


_BASE_PIP_PACKAGES = (
    "duckdb",
    "psycopg[binary]",
    "pylance>=6.0,<7.0",
    "lancedb>=0.30,<0.32",
)


def build_image(config: PatternALanceEmitConfig) -> modal.Image:
    """Construct the canonical Pattern A Lance-emit image."""
    image = (
        modal.Image.debian_slim(python_version="3.11")
        .pip_install_from_pyproject("modal/pyproject.toml")
        .pip_install(*_BASE_PIP_PACKAGES, *config.extra_pip_packages)
        .add_local_dir("modal/landing", remote_path="/root/landing")
        .add_local_dir("modal/_lib", remote_path="/root/_lib")
        .add_local_dir("scripts/dex", remote_path="/root/scripts")
    )
    if config.extra_apt_packages:
        image = image.apt_install(*config.extra_apt_packages)
    return image


# --------------------------------------------------------------------------- #
# Inner work — runs inside the Modal container.
# --------------------------------------------------------------------------- #


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _ensure_tmpdir() -> None:
    os.environ["TMPDIR"] = "/tmp/lance"
    os.makedirs("/tmp/lance", exist_ok=True)


def _connect() -> Any:
    import psycopg
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DATABASE_URL"]
    return psycopg.connect(url, autocommit=True)


def _resolve_source_id(display_name: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
            (display_name,),
        ).fetchone()
        return str(row[0]) if row else None


def _record_start(source_id: str, metadata: dict[str, Any]) -> str:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO ops.data_source_ingest_runs
                (source_id, started_at, status, run_metadata)
            VALUES (%s, NOW(), 'running', %s)
            RETURNING run_id
            """,
            (source_id, json.dumps(metadata)),
        ).fetchone()
        assert row is not None
        return str(row[0])


def _record_complete(
    run_id: str,
    *,
    status: str,
    rows_ingested: int = 0,
    bytes_written: int = 0,
    error_message: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ops.data_source_ingest_runs
            SET status        = %s,
                completed_at  = NOW(),
                rows_ingested = %s,
                bytes_written = %s,
                error_message = %s,
                run_metadata  = run_metadata || %s::jsonb
            WHERE run_id = %s
            """,
            (status, rows_ingested, bytes_written, error_message,
             json.dumps(extra_metadata or {}), run_id),
        )


def _parse_metrics_from_stdout(stdout: str) -> int:
    import ast
    for line in stdout.splitlines():
        if "OK — metrics:" in line:
            try:
                metrics_str = line.split("metrics:", 1)[1].strip()
                metrics = ast.literal_eval(metrics_str)
                return int(metrics.get("lance_rows", 0))
            except Exception:  # noqa: BLE001
                pass
    return 0


def run_emit(config: PatternALanceEmitConfig) -> dict[str, Any]:
    """Inner work for every Pattern A Lance-emit cron. The per-app
    @app.function calls this. Records start/complete in
    ops.data_source_ingest_runs; raises on subprocess failure."""
    log = logging.getLogger(config.app_name)

    _bridge_database_url()
    _ensure_tmpdir()

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    source_id = _resolve_source_id(config.display_name)
    if source_id is None:
        log.warning(
            "ops.data_sources has no row %r; skipping run "
            "(operator: run the corresponding seed script)",
            config.display_name,
        )
        return {"status": "skipped", "reason": "observability seed not applied"}

    metadata = {
        "writer": f"{config.display_name}-lance-emit",
        "started_at": started_at,
    }
    run_id = _record_start(source_id, metadata)
    log.info("recorded run start: run_id=%s source_id=%s", run_id, source_id)

    try:
        result = subprocess.run(
            [sys.executable, config.script_path, "--apply"],
            capture_output=True,
            text=True,
            env=os.environ,
            check=False,
            timeout=config.timeout_seconds - 60,
        )
    except Exception as e:  # noqa: BLE001
        _record_complete(
            run_id, status="failed",
            error_message=f"{type(e).__name__}: {e}",
            extra_metadata={"exception_type": type(e).__name__},
        )
        raise

    duration_s = round(time.time() - t0, 1)
    stdout_tail = result.stdout[-2000:] if result.stdout else ""
    stderr_tail = result.stderr[-2000:] if result.stderr else ""

    if result.returncode != 0:
        log.error(
            "emit failed (exit=%d) in %.1fs\nstdout tail:\n%s\nstderr tail:\n%s",
            result.returncode, duration_s, stdout_tail, stderr_tail,
        )
        _record_complete(
            run_id, status="failed",
            error_message=f"emit exited {result.returncode}: {stderr_tail[-500:]}",
            extra_metadata={
                "exit_code": result.returncode,
                "duration_s": duration_s,
                "stdout_tail": stdout_tail[-1000:],
            },
        )
        raise RuntimeError(
            f"{config.display_name} Lance emit failed (exit={result.returncode})"
        )

    rows = _parse_metrics_from_stdout(result.stdout)
    _record_complete(
        run_id, status="succeeded",
        rows_ingested=rows,
        extra_metadata={
            "duration_s": duration_s,
            "stdout_tail": stdout_tail[-1000:],
        },
    )
    log.info("emit OK in %.1fs (rows=%d, run_id=%s)", duration_s, rows, run_id)
    return {
        "status": "succeeded",
        "duration_s": duration_s,
        "rows_ingested": rows,
        "run_id": run_id,
    }


__all__ = [
    "ORCHESTRATOR_SECRETS",
    "PatternALanceEmitConfig",
    "build_image",
    "run_emit",
]
