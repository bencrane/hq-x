"""Shared helpers for ACRIS ingest loaders.

Used by:
    scripts/run_acris_full_backfill.py        — bulk per-dataset CSV
    scripts/run_acris_incremental_refresh.py  — Socrata `:updated_at` cursor

NYCTL Lien Sale List ingest is descoped to a sibling directive
(EXECUTOR_DIRECTIVE_NYCTL_LIEN_SALE_LISTS_SOCRATA_INGEST), which uses
Socrata 9rz4-mjek with cycle-tagged grain rather than per-borough CSV.

Politeness toward NYC Open Data (Socrata):
    Identifies this client with a descriptive User-Agent and operator
    contact. App token (NYC_OPEN_DATA_APP_TOKEN) is sent if set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import httpx
import psycopg

logger = logging.getLogger(__name__)

USER_AGENT = "data-engine-x ingest (operator: benjaminjcrane@gmail.com)"

# Socrata SODA pagination ceiling. Above this Socrata silently caps.
SODA_MAX_LIMIT = 50_000

# Default chunk size for COPY into Postgres. Tunable via env DEX_ACRIS_CHUNK.
DEFAULT_CHUNK_SIZE = 10_000


# ---------------------------------------------------------------------------
# Dataset registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DatasetConfig:
    """Describes one ACRIS Socrata dataset and its target table.

    Attributes:
        key:                short subcommand name, e.g. "rp-master".
        socrata_4x4:        Socrata dataset identifier.
        target_schema:      "entities" or "lookup".
        target_table:       table name (sans schema).
        natural_key_cols:   columns that form the upsert key. For the four
                            tables that use row_sha256, this is
                            ("document_id", "row_sha256").
        uses_row_sha256:    True if the loader must compute row_sha256.
        int_cols:           columns to coerce from string → int.
        numeric_cols:       columns to coerce from string → Decimal/float.
        date_cols:          columns to coerce from "YYYY-MM-DDT00:00:00.000" → date.
        ts_cols:            columns to coerce from "YYYY-MM-DDTHH:MM:SS.000" → timestamptz.
    """

    key: str
    socrata_4x4: str
    target_schema: str
    target_table: str
    natural_key_cols: tuple[str, ...]
    uses_row_sha256: bool
    int_cols: tuple[str, ...] = ()
    numeric_cols: tuple[str, ...] = ()
    date_cols: tuple[str, ...] = ()
    ts_cols: tuple[str, ...] = ()
    # Some lookup tables on Socrata are published with a broken natural key
    # (e.g. doc_control_codes has all-NULL doc__type as of 2026-04-29). For
    # those, run TRUNCATE before each bulk reload — a 100-row table is small
    # enough that "wipe and replace" is cheaper than synthesizing a key.
    truncate_before_load: bool = False


# Source-quirk column names retained: doc__type (double underscore),
# reference_by_crfn_ (trailing underscore).
DATASETS: dict[str, DatasetConfig] = {
    # --- Real Property -------------------------------------------------
    "rp-master": DatasetConfig(
        key="rp-master",
        socrata_4x4="bnx9-e6tj",
        target_schema="entities",
        target_table="acris_rp_master",
        natural_key_cols=("document_id",),
        uses_row_sha256=False,
        int_cols=("recorded_borough", "reel_yr", "reel_nbr", "reel_pg"),
        numeric_cols=("document_amt", "percent_trans"),
        date_cols=("document_date", "good_through_date"),
        ts_cols=("recorded_datetime", "modified_date"),
    ),
    "rp-legals": DatasetConfig(
        key="rp-legals",
        socrata_4x4="8h5j-fqxa",
        target_schema="entities",
        target_table="acris_rp_legals",
        natural_key_cols=("document_id", "row_sha256"),
        uses_row_sha256=True,
        int_cols=("borough", "block", "lot"),
        date_cols=("good_through_date",),
    ),
    "rp-parties": DatasetConfig(
        key="rp-parties",
        socrata_4x4="636b-3b5g",
        target_schema="entities",
        target_table="acris_rp_parties",
        natural_key_cols=("document_id", "row_sha256"),
        uses_row_sha256=True,
        date_cols=("good_through_date",),
    ),
    "rp-references": DatasetConfig(
        key="rp-references",
        socrata_4x4="pwkr-dpni",
        target_schema="entities",
        target_table="acris_rp_references",
        natural_key_cols=("document_id", "row_sha256"),
        uses_row_sha256=True,
        int_cols=(
            "reference_by_reel_year",
            "reference_by_reel_borough",
            "reference_by_reel_nbr",
            "reference_by_reel_page",
        ),
        date_cols=("good_through_date",),
    ),
    "rp-remarks": DatasetConfig(
        key="rp-remarks",
        socrata_4x4="9p4w-7npp",
        target_schema="entities",
        target_table="acris_rp_remarks",
        natural_key_cols=("document_id", "sequence_number"),
        uses_row_sha256=False,
        int_cols=("sequence_number",),
        date_cols=("good_through_date",),
    ),
    # --- Personal Property --------------------------------------------
    "pp-master": DatasetConfig(
        key="pp-master",
        socrata_4x4="sv7x-dduq",
        target_schema="entities",
        target_table="acris_pp_master",
        natural_key_cols=("document_id",),
        uses_row_sha256=False,
        int_cols=("recorded_borough", "reel_yr", "reel_nbr", "reel_pg", "rpttl_nbr"),
        numeric_cols=("document_amt",),
        date_cols=("fedtax_assessment_date", "good_through_date"),
        ts_cols=("recorded_datetime", "modified_date"),
    ),
    "pp-legals": DatasetConfig(
        key="pp-legals",
        socrata_4x4="uqqa-hym2",
        target_schema="entities",
        target_table="acris_pp_legals",
        natural_key_cols=("document_id", "row_sha256"),
        uses_row_sha256=True,
        int_cols=("borough", "block", "lot"),
        date_cols=("good_through_date",),
    ),
    "pp-parties": DatasetConfig(
        key="pp-parties",
        socrata_4x4="nbbg-wtuz",
        target_schema="entities",
        target_table="acris_pp_parties",
        natural_key_cols=("document_id", "row_sha256"),
        uses_row_sha256=True,
        date_cols=("good_through_date",),
    ),
    "pp-references": DatasetConfig(
        key="pp-references",
        socrata_4x4="6y3e-jcrc",
        target_schema="entities",
        target_table="acris_pp_references",
        natural_key_cols=("document_id", "row_sha256"),
        uses_row_sha256=True,
        date_cols=("good_through_date",),
    ),
    "pp-remarks": DatasetConfig(
        key="pp-remarks",
        socrata_4x4="w5p9-w9fw",
        target_schema="entities",
        target_table="acris_pp_remarks",
        natural_key_cols=("document_id", "sequence_number"),
        uses_row_sha256=False,
        int_cols=("sequence_number",),
        date_cols=("good_through_date",),
    ),
    # --- Lookups ------------------------------------------------------
    "lookup-doc-control": DatasetConfig(
        key="lookup-doc-control",
        socrata_4x4="7isb-wh4c",
        target_schema="lookup",
        target_table="acris_doc_control_codes",
        natural_key_cols=(),  # broken-on-publisher; TRUNCATE+RELOAD instead.
        uses_row_sha256=False,
        truncate_before_load=True,
    ),
    "lookup-property-type": DatasetConfig(
        key="lookup-property-type",
        socrata_4x4="94g4-w6xz",
        target_schema="lookup",
        target_table="acris_property_type_codes",
        natural_key_cols=("property_type",),
        uses_row_sha256=False,
    ),
    "lookup-ucc-collateral": DatasetConfig(
        key="lookup-ucc-collateral",
        socrata_4x4="q9kp-jvxv",
        target_schema="lookup",
        target_table="acris_ucc_collateral_codes",
        natural_key_cols=("ucc_collateral_code",),
        uses_row_sha256=False,
    ),
    "lookup-state": DatasetConfig(
        key="lookup-state",
        socrata_4x4="5c9e-33xj",
        target_schema="lookup",
        target_table="acris_state_codes",
        natural_key_cols=("state_code",),
        uses_row_sha256=False,
    ),
    "lookup-country": DatasetConfig(
        key="lookup-country",
        socrata_4x4="j2iz-mwzu",
        target_schema="lookup",
        target_table="acris_country_codes",
        natural_key_cols=("country_code",),
        uses_row_sha256=False,
    ),
}

# Convenience: lookup-codes runs all five lookup loaders in dependency order.
LOOKUP_KEYS = (
    "lookup-doc-control",
    "lookup-property-type",
    "lookup-ucc-collateral",
    "lookup-state",
    "lookup-country",
)

# Order to run "all" — smallest first so a failure surfaces quickly.
ALL_DATASET_KEYS = (
    *LOOKUP_KEYS,
    "rp-remarks",
    "rp-references",
    "rp-master",
    "rp-legals",
    "rp-parties",
    "pp-remarks",
    "pp-references",
    "pp-master",
    "pp-legals",
    "pp-parties",
)


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def _get_db_url(direct: bool = False) -> str:
    """Return Postgres URL from Doppler-injected env.

    direct=True picks DEX_DB_URL_DIRECT (DDL, COPY, REFRESH MATERIALIZED VIEW
    CONCURRENTLY). Otherwise DEX_DB_URL_POOLED (chunked inserts).
    """
    var = "DEX_DB_URL_DIRECT" if direct else "DEX_DB_URL_POOLED"
    url = os.environ.get(var)
    if not url:
        raise RuntimeError(
            f"{var} not set — invoke under `doppler run --` from a worktree "
            "with Doppler pinned to data-engine-x/prd."
        )
    return url


def connect(*, direct: bool = False) -> psycopg.Connection:
    """Open a psycopg connection. Caller closes."""
    return psycopg.connect(_get_db_url(direct=direct), autocommit=False)


# ---------------------------------------------------------------------------
# Type coercion (Socrata returns numbers as strings, dates with .000 suffix)
# ---------------------------------------------------------------------------

def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        # Some Socrata "number" cols carry ".0" suffix; strip if present.
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None


def _coerce_numeric(v: Any) -> str | None:
    """Coerce to a string representation safe for PG numeric. Avoids
    floating-point round-trip — psycopg accepts strings for numeric columns.
    """
    if v is None or v == "":
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        # Validate parses as a number; return original string for full precision.
        float(s)
        return s
    except (ValueError, TypeError):
        return None


def _coerce_date(v: Any) -> str | None:
    """Socrata calendar_date columns return 'YYYY-MM-DDT00:00:00.000'.
    Strip the time part — PG date column accepts 'YYYY-MM-DD'.
    """
    if v is None or v == "":
        return None
    s = str(v).strip()
    if "T" in s:
        return s.split("T", 1)[0]
    return s if len(s) >= 10 else None


def _coerce_ts(v: Any) -> str | None:
    """Socrata calendar_date with time → ISO timestamp. PG accepts the
    'YYYY-MM-DDTHH:MM:SS.fff' form directly.
    """
    if v is None or v == "":
        return None
    s = str(v).strip()
    return s or None


def coerce_row(raw: dict[str, Any], cfg: DatasetConfig) -> dict[str, Any]:
    """Apply per-dataset type coercions in place. Returns a copy with typed
    values and any null-string-to-None normalization done.
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, str) and v == "":
            out[k] = None
        else:
            out[k] = v

    for col in cfg.int_cols:
        if col in out:
            out[col] = _coerce_int(out[col])
    for col in cfg.numeric_cols:
        if col in out:
            out[col] = _coerce_numeric(out[col])
    for col in cfg.date_cols:
        if col in out:
            out[col] = _coerce_date(out[col])
    for col in cfg.ts_cols:
        if col in out:
            out[col] = _coerce_ts(out[col])
    return out


# ---------------------------------------------------------------------------
# row_sha256
# ---------------------------------------------------------------------------

# Columns excluded from the row_sha256 hash. good_through_date advances
# every monthly snapshot without an underlying data change — including it
# would defeat idempotency. ingested_at and any loader-side bookkeeping are
# excluded by construction (they're not in raw).
_HASH_EXCLUDED = frozenset({"good_through_date"})


def compute_row_sha256(raw: dict[str, Any]) -> str:
    """Deterministic SHA-256 over a Socrata row's source fields, excluding
    monthly-rotating fields. Order-independent: keys are sorted before hashing.
    Values are coerced to strings; None → '\\x00' sentinel.
    """
    parts: list[str] = []
    for k in sorted(raw.keys()):
        if k in _HASH_EXCLUDED:
            continue
        v = raw[k]
        if v is None:
            parts.append(f"{k}=\x00")
        else:
            parts.append(f"{k}={v}")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Socrata HTTP
# ---------------------------------------------------------------------------

def _socrata_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    token = os.environ.get("NYC_OPEN_DATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    return headers


def _socrata_url(four_by_four: str, fmt: str = "json") -> str:
    return f"https://data.cityofnewyork.us/resource/{four_by_four}.{fmt}"


def stream_socrata_csv(
    four_by_four: str,
    out_path: str,
    *,
    timeout: float = 600.0,
) -> int:
    """Stream a full-dataset CSV to disk. Returns bytes written.

    Bulk CSV downloads do not paginate — single streaming response, often
    multi-GB. Caller chunk-loads from disk.
    """
    url = _socrata_url(four_by_four, "csv")
    bytes_written = 0
    with httpx.stream(
        "GET", url, headers=_socrata_headers(), timeout=timeout, follow_redirects=True
    ) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                fh.write(chunk)
                bytes_written += len(chunk)
    return bytes_written


def paginate_socrata_json(
    four_by_four: str,
    *,
    where: str | None = None,
    order: str | None = None,
    limit: int = SODA_MAX_LIMIT,
    timeout: float = 120.0,
) -> Iterator[list[dict[str, Any]]]:
    """Yield page batches from SODA JSON. Pages are at most `limit` rows.

    Use `where` for incremental: e.g. `:updated_at > '2026-04-01T00:00:00.000'`.
    Use `order` for stable pagination (default `:id`).
    """
    if order is None:
        order = ":id"
    offset = 0
    url = _socrata_url(four_by_four, "json")
    headers = _socrata_headers()
    with httpx.Client(timeout=timeout) as client:
        while True:
            params = {
                "$limit": str(limit),
                "$offset": str(offset),
                "$order": order,
            }
            if where:
                params["$where"] = where
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            page: list[dict[str, Any]] = resp.json()
            if not page:
                return
            yield page
            offset += len(page)
            if len(page) < limit:
                return


# ---------------------------------------------------------------------------
# ops.acris_ingest_runs lifecycle
# ---------------------------------------------------------------------------

@dataclass
class IngestRunHandle:
    run_id: uuid.UUID
    dataset_id: str
    row_id: uuid.UUID
    started_monotonic: float


def _start_run(
    conn: psycopg.Connection,
    *,
    run_id: uuid.UUID,
    dataset_id: str,
    ingest_mode: str,
    source_url: str | None = None,
    source_filename: str | None = None,
    source_byte_size: int | None = None,
    watermark_before: datetime | None = None,
    invoked_by: str = "cli",
) -> IngestRunHandle:
    row_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.acris_ingest_runs (
                id, run_id, dataset_id, ingest_mode,
                source_url, source_filename, source_byte_size,
                watermark_before,
                status, attempt, started_at, invoked_by
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s,
                'running', 1, %s, %s
            )
            """,
            (
                str(row_id), str(run_id), dataset_id, ingest_mode,
                source_url, source_filename, source_byte_size,
                watermark_before,
                now, invoked_by,
            ),
        )
    conn.commit()
    return IngestRunHandle(
        run_id=run_id,
        dataset_id=dataset_id,
        row_id=row_id,
        started_monotonic=time.monotonic(),
    )


def _classify_error(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg or name == "TimeoutError":
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "download_failure"
    if isinstance(exc, psycopg.Error):
        return "db_failure"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "parse_failure"
    return "unknown"


def _finalize_run(
    conn: psycopg.Connection,
    handle: IngestRunHandle,
    *,
    status: str,
    rows_loaded: int | None = None,
    rows_skipped_idempotent: int | None = None,
    bytes_downloaded: int | None = None,
    watermark_after: datetime | None = None,
    error_message: str | None = None,
    error_class: str | None = None,
    source_sha256: str | None = None,
) -> None:
    duration = time.monotonic() - handle.started_monotonic
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.acris_ingest_runs
            SET status = %s,
                completed_at = NOW(),
                duration_seconds = %s,
                rows_loaded = COALESCE(%s, rows_loaded),
                rows_skipped_idempotent = COALESCE(%s, rows_skipped_idempotent),
                bytes_downloaded = COALESCE(%s, bytes_downloaded),
                watermark_after = COALESCE(%s, watermark_after),
                source_sha256 = COALESCE(%s, source_sha256),
                error_message = %s,
                error_class = %s
            WHERE id = %s
            """,
            (
                status,
                duration,
                rows_loaded,
                rows_skipped_idempotent,
                bytes_downloaded,
                watermark_after,
                source_sha256,
                error_message,
                error_class,
                str(handle.row_id),
            ),
        )
    conn.commit()


@contextmanager
def ingest_run(
    *,
    run_id: uuid.UUID,
    dataset_id: str,
    ingest_mode: str,
    source_url: str | None = None,
    source_filename: str | None = None,
    source_byte_size: int | None = None,
    watermark_before: datetime | None = None,
    invoked_by: str = "cli",
) -> Iterator[tuple[psycopg.Connection, IngestRunHandle]]:
    """Open a run row, yield (conn, handle); on exit, write final state.

    The caller mutates the handle state via update_run_progress() between
    enter and exit if interim chunk counts are useful.
    """
    audit_conn = connect(direct=True)
    try:
        handle = _start_run(
            audit_conn,
            run_id=run_id,
            dataset_id=dataset_id,
            ingest_mode=ingest_mode,
            source_url=source_url,
            source_filename=source_filename,
            source_byte_size=source_byte_size,
            watermark_before=watermark_before,
            invoked_by=invoked_by,
        )
    except Exception:
        audit_conn.close()
        raise

    try:
        yield audit_conn, handle
    except BaseException as exc:
        _finalize_run(
            audit_conn,
            handle,
            status="failed",
            error_message=str(exc)[:5000],
            error_class=_classify_error(exc),
        )
        audit_conn.close()
        raise
    else:
        # Success path — caller is expected to have populated counts via
        # update_run_progress(); we only flip to 'completed' if not already.
        with audit_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM ops.acris_ingest_runs WHERE id = %s",
                (str(handle.row_id),),
            )
            current_status = cur.fetchone()[0]
        if current_status == "running":
            _finalize_run(audit_conn, handle, status="completed")
        audit_conn.close()


def update_run_progress(
    handle: IngestRunHandle,
    *,
    rows_loaded: int | None = None,
    rows_skipped_idempotent: int | None = None,
    bytes_downloaded: int | None = None,
    watermark_after: datetime | None = None,
    source_sha256: str | None = None,
) -> None:
    """Write interim progress to ops.acris_ingest_runs without flipping
    status. Called from chunk loaders after each successful batch.
    """
    audit_conn = connect(direct=True)
    try:
        with audit_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.acris_ingest_runs
                SET rows_loaded = COALESCE(%s, rows_loaded),
                    rows_skipped_idempotent = COALESCE(%s, rows_skipped_idempotent),
                    bytes_downloaded = COALESCE(%s, bytes_downloaded),
                    watermark_after = COALESCE(%s, watermark_after),
                    source_sha256 = COALESCE(%s, source_sha256)
                WHERE id = %s
                """,
                (
                    rows_loaded,
                    rows_skipped_idempotent,
                    bytes_downloaded,
                    watermark_after,
                    source_sha256,
                    str(handle.row_id),
                ),
            )
        audit_conn.commit()
    finally:
        audit_conn.close()


def mark_run_completed(handle: IngestRunHandle, **kwargs: Any) -> None:
    """Explicit success-path finalize. Use when you want to commit final
    counts atomically with the status flip (rather than relying on the
    context manager's success path)."""
    audit_conn = connect(direct=True)
    try:
        _finalize_run(audit_conn, handle, status="completed", **kwargs)
    finally:
        audit_conn.close()


def get_last_watermark(dataset_id: str) -> datetime | None:
    """Return the last successfully-completed incremental watermark for a
    dataset, or None if there has been no successful incremental run yet.
    """
    conn = connect(direct=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT watermark_after
                FROM ops.acris_ingest_runs
                WHERE dataset_id = %s
                  AND ingest_mode = 'incremental'
                  AND status = 'completed'
                  AND watermark_after IS NOT NULL
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 1
                """,
                (dataset_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chunked persist via psycopg COPY
# ---------------------------------------------------------------------------

def chunked_upsert(
    conn: psycopg.Connection,
    cfg: DatasetConfig,
    rows: list[dict[str, Any]],
) -> tuple[int, int]:
    """Insert a batch of rows with ON CONFLICT DO NOTHING on the natural key.

    Returns (rows_loaded, rows_skipped_idempotent).

    rows: list of dicts already coerced via coerce_row() and (if needed)
    augmented with row_sha256.
    """
    if not rows:
        return (0, 0)

    # Build the column list. Every row carries the same columns by
    # construction; pull them from the first row plus raw_jsonb + row_sha256
    # if applicable.
    base_cols = list(rows[0].keys())
    if cfg.uses_row_sha256 and "row_sha256" not in base_cols:
        raise ValueError("uses_row_sha256=True but row_sha256 missing from row")
    if "raw_jsonb" not in base_cols:
        raise ValueError("raw_jsonb missing from row — loader must populate it")

    placeholders = ", ".join(["%s"] * len(base_cols))
    cols_sql = ", ".join(base_cols)
    table = f"{cfg.target_schema}.{cfg.target_table}"
    if cfg.natural_key_cols:
        conflict_cols = ", ".join(cfg.natural_key_cols)
        sql = (
            f"INSERT INTO {table} ({cols_sql}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_cols}) DO NOTHING"
        )
    else:
        # No natural key (truncate-before-load tables) — plain INSERT.
        sql = f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})"

    values_seq: list[tuple[Any, ...]] = []
    for r in rows:
        row_vals: list[Any] = []
        for c in base_cols:
            v = r.get(c)
            if c == "raw_jsonb" and v is not None and not isinstance(v, str):
                v = psycopg.types.json.Jsonb(v)
            row_vals.append(v)
        values_seq.append(tuple(row_vals))

    with conn.cursor() as cur:
        cur.executemany(sql, values_seq)
        rows_loaded = cur.rowcount
    conn.commit()
    rows_skipped = len(rows) - max(rows_loaded, 0)
    return (max(rows_loaded, 0), max(rows_skipped, 0))


# ---------------------------------------------------------------------------
# Build a row dict from a Socrata raw payload, ready for chunked_upsert.
# ---------------------------------------------------------------------------

# Columns we synthesize on top of the source payload. These never come from
# Socrata and must not appear in the raw_jsonb projection.
_LOADER_SYNTHESIZED = frozenset({
    "raw_jsonb", "row_sha256", "source_dataset_id",
    "ingested_at", "created_at", "updated_at",
})


def build_persist_row(
    raw: dict[str, Any],
    cfg: DatasetConfig,
    *,
    table_columns: set[str],
) -> dict[str, Any]:
    """Project a Socrata raw payload to the target table's columns.

    Steps:
      1. Coerce types per cfg (int/numeric/date/ts).
      2. Compute row_sha256 over the original (pre-coercion) raw payload
         when cfg.uses_row_sha256 — using pre-coercion preserves Socrata's
         exact wire-format which is the most stable hash basis.
      3. Drop any source columns the target table does not declare
         (defensive — Socrata sometimes adds debug columns we don't want).
      4. Always include raw_jsonb.
    """
    coerced = coerce_row(raw, cfg)
    out: dict[str, Any] = {}
    for col in coerced:
        if col in _LOADER_SYNTHESIZED:
            continue
        if col in table_columns:
            out[col] = coerced[col]
        # else: drop unknown source col silently — raw_jsonb still has it.
    out["raw_jsonb"] = raw  # verbatim, pre-coercion
    if cfg.uses_row_sha256:
        out["row_sha256"] = compute_row_sha256(raw)
    return out


def get_table_columns(conn: psycopg.Connection, cfg: DatasetConfig) -> set[str]:
    """Introspect target table column names. Cached in the caller's loop."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (cfg.target_schema, cfg.target_table),
        )
        return {r[0] for r in cur.fetchall()}
