"""Derivation factory — config-driven R2 → DuckDB → R2 transform executor.

Per the 2026-05-11 adversarial audit (reports/2026-05-11-adversarial-audit-
data-ingest-pipeline.md §3 D2): the existing 11 build_fmcsa_*.py scripts
duplicate ~5,108 LoC across hand-rolled per-derivation pipelines. This
factory compresses the pattern to a YAML row + a single executor.

Substrate-agnostic: this is the BUILD layer only (R2 → DuckDB → R2). The
RW serve layer (admit source, admit MV) is intentionally separate and
pluggable per substrate choice (see substrate evaluation
2026-05-11-substrate-evaluation-tradeoff-matrix.md).

Per L45: each derivation declares its `snapshot_naming` strategy
explicitly. The PreToolUse L45 hook (scripts/hook-pretooluse-l45-snapshot-
cron-accumulation.sh, PR #326) blocks Modal cron deploys that combine
this factory with an L45-unsafe downstream consumer.

Public API:

    DerivationFactory(config_path).run(name, *, dry_run, snapshot_date)
    DerivationFactory(config_path).list()
    DerivationFactory(config_path).get(name)

Run via the CLI: scripts/run_derivation.py.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("derivation_factory")


R2_BUCKET = "dex-raw-landing-zone"


@dataclass
class DerivationResult:
    name: str
    snapshot_date: str
    output_key: str | None
    rows_written: int
    duration_seconds: float
    dry_run: bool
    started_at: str
    completed_at: str


class DerivationFactory:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            raise SystemExit(f"FAIL: config not found: {self.config_path}")
        self._config = self._load_yaml(self.config_path)
        self._validate_schema()

    # ── Public API ────────────────────────────────────────────────────────

    def list(self) -> list[str]:
        """Return all derivation names declared in the config."""
        return [d["name"] for d in self._config["derivations"]]

    def get(self, name: str) -> dict[str, Any]:
        """Return the validated config dict for a single derivation."""
        for d in self._config["derivations"]:
            if d["name"] == name:
                return d
        raise SystemExit(
            f"FAIL: derivation '{name}' not in config. "
            f"Available: {', '.join(self.list())}"
        )

    def run(
        self,
        name: str,
        *,
        dry_run: bool = False,
        snapshot_date: str | None = None,
    ) -> DerivationResult:
        """Execute a single derivation by name.

        - dry_run=True: introspect + count + smoke gate, but no R2 write.
        - snapshot_date=YYYY-MM-DD: override input snapshot detection.

        Returns DerivationResult. Raises SystemExit on any failure path.
        """
        d = self.get(name)
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.time()

        for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            if not os.environ.get(var):
                raise SystemExit(f"FAIL: {var} not set in environment")

        con = self._connect_duckdb_to_r2()

        # ── 1. Resolve input snapshot ─────────────────────────────────────
        input_prefix = d["input"]["r2_prefix"]
        file_pattern = d["input"].get("file_pattern", "*.parquet*")
        snapshot_mode = d["input"].get("snapshot_mode", "by_date")
        snapshot_layout = d["input"].get("snapshot_layout", "direct")
        if snapshot_mode == "static":
            # Static-historical source (no date-partitioned snapshots). The
            # transform_sql hardcodes read_parquet() URLs against R2; the
            # factory just stamps a "snapshot_date" of today onto the output.
            resolved_snapshot = date.today().isoformat()
            logger.info(
                "[%s] static input mode — stamping output snapshot=%s",
                name, resolved_snapshot,
            )
            select_sql = d["transform_sql"]
        else:
            if snapshot_date:
                resolved_snapshot = snapshot_date
            else:
                resolved_snapshot = self._detect_latest_snapshot(con, input_prefix)
            logger.info(
                "[%s] input snapshot resolved to %s (layout=%s)",
                name, resolved_snapshot, snapshot_layout,
            )
            input_glob = self._build_input_glob(
                prefix=input_prefix,
                snapshot=resolved_snapshot,
                file_pattern=file_pattern,
                snapshot_layout=snapshot_layout,
            )
            # ── 2. Build SELECT (transform SQL with input_glob substituted) ───
            select_sql = d["transform_sql"].format(input_glob=input_glob)

        # ── 3. Smoke gate: row count + bounds check ───────────────────────
        cnt_sql = f"SELECT count(*) FROM ({select_sql})"
        rows = con.execute(cnt_sql).fetchone()[0]
        logger.info("[%s] derivation row count: %s", name, f"{rows:,}")

        smoke = d.get("smoke") or {}
        expected_min = smoke.get("expected_min")
        expected_max = smoke.get("expected_max")
        if expected_min is not None and rows < expected_min:
            raise SystemExit(
                f"FAIL: smoke gate violated — rows={rows:,} below expected_min={expected_min:,}"
            )
        if expected_max is not None and rows > expected_max:
            raise SystemExit(
                f"FAIL: smoke gate violated — rows={rows:,} above expected_max={expected_max:,}"
            )

        # ── 4. Compute output key per snapshot_naming strategy ────────────
        output_prefix = d["output"]["r2_prefix"]
        snapshot_naming = d["output"]["snapshot_naming"]
        output_key = self._compute_output_key(
            output_prefix=output_prefix,
            snapshot_naming=snapshot_naming,
            resolved_input_snapshot=resolved_snapshot,
        )
        output_url = f"r2://{R2_BUCKET}/{output_key}"

        # ── 5. Dry-run: stop here ─────────────────────────────────────────
        if dry_run:
            completed_at = datetime.now(timezone.utc).isoformat()
            duration = round(time.time() - t0, 2)
            logger.info(
                "[%s] DRY-RUN — would write %s rows → %s (duration %.1fs)",
                name, f"{rows:,}", output_url, duration,
            )
            return DerivationResult(
                name=name,
                snapshot_date=resolved_snapshot,
                output_key=None,
                rows_written=rows,
                duration_seconds=duration,
                dry_run=True,
                started_at=started_at,
                completed_at=completed_at,
            )

        # ── 6. Write output ───────────────────────────────────────────────
        compression = d["output"].get("compression", "zstd")
        row_group_size = int(d["output"].get("row_group_size", 100000))
        copy_sql = f"""
            COPY ({select_sql})
            TO '{output_url}'
            (FORMAT PARQUET, COMPRESSION {compression.upper()}, ROW_GROUP_SIZE {row_group_size})
        """
        logger.info("[%s] writing → %s", name, output_url)
        con.execute(copy_sql)
        completed_at = datetime.now(timezone.utc).isoformat()
        duration = round(time.time() - t0, 2)
        logger.info(
            "[%s] OK — wrote %s rows to %s (duration %.1fs)",
            name, f"{rows:,}", output_url, duration,
        )

        return DerivationResult(
            name=name,
            snapshot_date=resolved_snapshot,
            output_key=output_key,
            rows_written=rows,
            duration_seconds=duration,
            dry_run=False,
            started_at=started_at,
            completed_at=completed_at,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit("FAIL: PyYAML not installed (uv run --with pyyaml ...)") from exc
        with path.open() as f:
            return yaml.safe_load(f)

    def _validate_schema(self) -> None:
        if not isinstance(self._config, dict) or "derivations" not in self._config:
            raise SystemExit(
                f"FAIL: config root must be a dict with 'derivations' key: {self.config_path}"
            )
        ds = self._config["derivations"]
        if not isinstance(ds, list) or not ds:
            raise SystemExit("FAIL: 'derivations' must be a non-empty list")
        seen_names: set[str] = set()
        for i, d in enumerate(ds):
            self._validate_derivation(i, d, seen_names)

    @staticmethod
    def _validate_derivation(i: int, d: Any, seen: set[str]) -> None:
        ctx = f"derivations[{i}]"
        if not isinstance(d, dict):
            raise SystemExit(f"FAIL: {ctx} must be a dict")
        for k in ("name", "description", "input", "transform_sql", "output"):
            if k not in d:
                raise SystemExit(f"FAIL: {ctx} missing required key '{k}'")
        if d["name"] in seen:
            raise SystemExit(f"FAIL: duplicate derivation name '{d['name']}'")
        seen.add(d["name"])
        if not re.match(r"^[a-z][a-z0-9_]*$", d["name"]):
            raise SystemExit(
                f"FAIL: {ctx}.name '{d['name']}' must be snake_case (^[a-z][a-z0-9_]*$)"
            )
        for k in ("r2_prefix",):
            if k not in d["input"]:
                raise SystemExit(f"FAIL: {ctx}.input missing required key '{k}'")
        for k in ("r2_prefix", "snapshot_naming"):
            if k not in d["output"]:
                raise SystemExit(f"FAIL: {ctx}.output missing required key '{k}'")
        sn = d["output"]["snapshot_naming"]
        if sn not in ("source_date", "today") and not sn.startswith("fixed:"):
            raise SystemExit(
                f"FAIL: {ctx}.output.snapshot_naming '{sn}' must be one of: "
                f"'source_date', 'today', 'fixed:<key>'"
            )
        snapshot_mode = d["input"].get("snapshot_mode", "by_date")
        if snapshot_mode not in ("by_date", "static"):
            raise SystemExit(
                f"FAIL: {ctx}.input.snapshot_mode '{snapshot_mode}' must be 'by_date' or 'static'"
            )
        snapshot_layout = d["input"].get("snapshot_layout", "direct")
        if snapshot_layout not in ("direct", "hive"):
            raise SystemExit(
                f"FAIL: {ctx}.input.snapshot_layout '{snapshot_layout}' must be 'direct' or 'hive'"
            )
        if snapshot_mode == "by_date" and "{input_glob}" not in d["transform_sql"]:
            raise SystemExit(
                f"FAIL: {ctx}.transform_sql must contain {{input_glob}} placeholder when snapshot_mode='by_date'"
            )

    @staticmethod
    def _connect_duckdb_to_r2():
        try:
            import duckdb
        except ImportError as exc:
            raise SystemExit(
                "FAIL: DuckDB not installed (uv run --with duckdb python ...)"
            ) from exc
        endpoint = os.environ["R2_ENDPOINT"]
        endpoint_host = endpoint.replace("https://", "").replace("http://", "")
        account_id = endpoint_host.split(".")[0]
        con = duckdb.connect(":memory:")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"""
            CREATE OR REPLACE SECRET r2_secret (
                TYPE r2,
                KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
                SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
                ACCOUNT_ID '{account_id}'
            )
        """)
        return con

    @staticmethod
    def _detect_latest_snapshot(con, input_prefix: str) -> str:
        """Find the latest YYYY-MM-DD or snapshot=YYYY-MM-DD folder under
        input_prefix in R2. Returns the snapshot date string."""
        glob_url = f"r2://{R2_BUCKET}/{input_prefix}/**/*.parquet*"
        rows = con.execute(f"SELECT file FROM glob('{glob_url}')").fetchall()
        if not rows:
            raise SystemExit(
                f"FAIL: no Parquet files under r2://{R2_BUCKET}/{input_prefix}/"
            )
        snapshot_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
        snapshots: set[str] = set()
        for (path,) in rows:
            for m in snapshot_re.findall(path):
                snapshots.add(m)
        if not snapshots:
            raise SystemExit(
                f"FAIL: no YYYY-MM-DD snapshot directory found under {input_prefix}"
            )
        return max(snapshots)

    @staticmethod
    def _build_input_glob(
        *,
        prefix: str,
        snapshot: str,
        file_pattern: str,
        snapshot_layout: str = "direct",
    ) -> str:
        """Build a DuckDB read_parquet glob URL covering one snapshot folder.

        snapshot_layout='direct': `<prefix>/YYYY-MM-DD/<file_pattern>` —
            legacy FMCSA convention + the POC carrier_essentials shape.
        snapshot_layout='hive':   `<prefix>/snapshot=YYYY-MM-DD/<file_pattern>` —
            canonical Hive partition layout used by the derived layer and the
            post-2026-05-11 retrofit of the raw FMCSA prefixes.
        """
        if snapshot_layout == "hive":
            return f"r2://{R2_BUCKET}/{prefix}/snapshot={snapshot}/{file_pattern}"
        return f"r2://{R2_BUCKET}/{prefix}/{snapshot}/{file_pattern}"

    @staticmethod
    def _compute_output_key(
        *,
        output_prefix: str,
        snapshot_naming: str,
        resolved_input_snapshot: str,
    ) -> str:
        if snapshot_naming == "source_date":
            return f"{output_prefix}/snapshot={resolved_input_snapshot}/data.parquet"
        if snapshot_naming == "today":
            today = date.today().isoformat()
            return f"{output_prefix}/snapshot={today}/data.parquet"
        if snapshot_naming.startswith("fixed:"):
            fixed_key = snapshot_naming[len("fixed:"):]
            return f"{output_prefix}/{fixed_key}/data.parquet"
        raise SystemExit(
            f"FAIL: unsupported snapshot_naming '{snapshot_naming}'"
        )
