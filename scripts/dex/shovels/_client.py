"""Shared Shovels ingest library — fetch → typed+raw Parquet → R2 → ledger.

This is the reusable rail underneath the 6 entity CLIs. It owns everything
that is identical across entities:

  * ``ShovelsClient`` — a thin sync HTTP client (``X-API-Key`` auth, the §11
    cursor-envelope pagination, defensive error parsing per §9, and live credit
    accounting from the ``X-Credits-Request`` header per §3). It captures the
    FULL verbatim Shovels record (the provider mappers in
    ``app/providers/shovels.py`` are intentionally lossy and are NOT used here —
    raw landing must satisfy the §"Source ingest invariant" 1:1-mirror rule).
  * ``EntityIngestSpec`` — per-entity declaration: the source endpoint slug, the
    typed columns to project out of each raw record (PK + every downstream
    filter/sort/join field), the PK column, and the Arrow schema.
  * ``run_entity_ingest`` — the parameterized fetch→Parquet→R2→ledger driver.
    Each run writes/overwrites a single ``snapshot=<date>`` partition; the Lance
    rebuild (separate step) dedups the full ``snapshot=*`` glob to latest-per-PK.
  * Ledger helpers writing ``ops.shovels_ingest_runs`` (entity-discriminated).

Secrets (Doppler hq-all/prd): ``SHOVELS_API_KEY``, ``R2_ENDPOINT``,
``R2_ACCESS_KEY_ID``, ``R2_SECRET_ACCESS_KEY``, ``DEX_DB_URL_DIRECT``.
The raw API key is NEVER logged, written to Parquet, or echoed.

Encoding (CLAUDE.md §"Volume-King … R2 ZSTD Parquet"): ZSTD Parquet, plain
``.parquet`` extension, ``ContentType=application/x-parquet`` only (no
Content-Encoding header).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

LOG = logging.getLogger("shovels.ingest")

SHOVELS_BASE_URL = "https://api.shovels.ai/v2"
SOURCE_PROVIDER = "shovels"
R2_BUCKET = "dex-raw-landing-zone"

# Provenance columns appended to every entity's typed projection. These satisfy
# the §"Source ingest invariant" provenance set at the Parquet level (raw_json
# is the verbatim-mirror column; the rest is run lineage).
_PROVENANCE_FIELDS: list[pa.Field] = [
    pa.field("raw_json", pa.string(), nullable=False),
    pa.field("source_provider", pa.string(), nullable=False),
    pa.field("source_endpoint", pa.string(), nullable=False),
    pa.field("source_query_spec", pa.string(), nullable=False),
    pa.field("snapshot_date", pa.string(), nullable=False),  # YYYY-MM-DD
    pa.field("source_run_id", pa.string(), nullable=False),
    pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
]
_PROVENANCE_COLUMN_NAMES = {f.name for f in _PROVENANCE_FIELDS}


# --------------------------------------------------------------------------- #
# typed-column coercion
# --------------------------------------------------------------------------- #
def to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    return str(value)


def to_int(value: Any) -> int | None:
    """Integer coercion. Money fields are already integer cents/dollars in the
    Shovels payload (§2/§6) — no scaling applied; we mirror the upstream int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def to_json_str(value: Any) -> str | None:
    """Serialize a nested struct/list (tags, geo_ids, address, …) to a compact
    JSON string so it stays queryable in DuckDB via ``json_extract`` without a
    LIST<…> Lance definition-buffer concern (CLAUDE.md L54)."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# per-entity ingest declaration
# --------------------------------------------------------------------------- #
@dataclass
class EntityIngestSpec:
    """Declares everything entity-specific about a Shovels ingest.

    ``typed_columns`` is an ordered list of (column_name, arrow_type,
    extractor). ``extractor`` maps a raw record dict → the typed value. The
    driver appends the shared provenance columns automatically. ``pk_column``
    must be one of the typed column names (the BTREE/dedup key).
    """

    entity: str                         # 'permit' | 'contractor' | ...
    r2_entity_dir: str                  # R2 prefix segment, e.g. 'permit'
    pk_column: str
    typed_columns: list[tuple[str, pa.DataType, Callable[[dict[str, Any]], Any]]]

    def arrow_schema(self) -> pa.Schema:
        fields = [pa.field(name, dtype, nullable=True) for name, dtype, _ in self.typed_columns]
        # PK is logically non-null but we keep it nullable in Arrow to tolerate
        # the rare upstream null; the dedup SELECT + a WHERE pk IS NOT NULL at
        # emit time guard the Lance layer. (Residents synthesize a non-null key.)
        return pa.schema(fields + _PROVENANCE_FIELDS)

    def project(
        self,
        *,
        raw: dict[str, Any],
        source_endpoint: str,
        query_spec_json: str,
        snapshot_date: str,
        run_id: str,
        ingested_at: datetime,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for name, _dtype, extractor in self.typed_columns:
            if name in _PROVENANCE_COLUMN_NAMES:
                raise ValueError(f"typed column {name!r} collides with a provenance column")
            row[name] = extractor(raw)
        row["raw_json"] = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        row["source_provider"] = SOURCE_PROVIDER
        row["source_endpoint"] = source_endpoint
        row["source_query_spec"] = query_spec_json
        row["snapshot_date"] = snapshot_date
        row["source_run_id"] = run_id
        row["ingested_at"] = ingested_at
        return row


# --------------------------------------------------------------------------- #
# HTTP client — auth, pagination, credit accounting, defensive errors
# --------------------------------------------------------------------------- #
class ShovelsAPIError(RuntimeError):
    def __init__(self, status: int, detail: Any, *, endpoint: str):
        self.status = status
        self.detail = detail
        self.endpoint = endpoint
        super().__init__(f"Shovels {endpoint} -> HTTP {status}: {_summarize_detail(detail)}")


def _summarize_detail(detail: Any) -> str:
    # §9: detail may be an array (framework), an object (domain), or a string
    # (auth/402). Parse defensively; never surface the API key.
    try:
        return json.dumps(detail)[:300]
    except (TypeError, ValueError):
        return str(detail)[:300]


class ShovelsClient:
    """Sync Shovels client. One instance per ingest run.

    Credit accounting: every billable response carries ``X-Credits-Request`` ==
    number of records returned (§3). We sum that header across all pages into
    ``credits_spent``. Free endpoints emit no header → contribute 0.
    """

    def __init__(self, api_key: str, *, timeout: float = 60.0):
        if not api_key:
            raise EnvironmentError("SHOVELS_API_KEY is required")
        self._client = httpx.Client(
            base_url=SHOVELS_BASE_URL,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )
        self.credits_spent = 0
        self.api_calls = 0

    def __enter__(self) -> "ShovelsClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client.close()

    def close(self) -> None:
        self._client.close()

    # -- low-level GET with one-shot 429 backoff + credit tallying --------- #
    def _get(self, path: str, params: list[tuple[str, Any]] | None = None) -> httpx.Response:
        attempt = 0
        while True:
            resp = self._client.get(path, params=params)
            self.api_calls += 1
            if resp.status_code == 429 and attempt == 0:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                LOG.warning("429 on %s — sleeping %ss (one-shot retry)", path, retry_after)
                time.sleep(retry_after)
                attempt += 1
                continue
            # Tally credits regardless of pagination position.
            credit_header = resp.headers.get("X-Credits-Request")
            if credit_header is not None:
                try:
                    self.credits_spent += int(credit_header)
                except ValueError:
                    pass
            return resp

    def get_json(self, path: str, params: list[tuple[str, Any]] | None = None) -> dict[str, Any]:
        resp = self._get(path, params)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw_text": resp.text[:500]}
        if resp.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise ShovelsAPIError(resp.status_code, detail, endpoint=path)
        return body if isinstance(body, dict) else {"items": body}

    def paginate(
        self,
        path: str,
        *,
        base_params: list[tuple[str, Any]],
        size: int,
        max_pages: int | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw record dicts across the §11 cursor envelope.

        First page omits ``cursor``; subsequent pages pass the prior
        ``next_cursor`` until it is null or ``max_pages`` is reached. Works for
        every paginated Shovels list endpoint (search, by-id, sub-resources).
        Endpoints that ignore pagination (addresses/search) simply return one
        page with ``next_cursor=null`` — handled transparently.
        """
        cursor: str | None = None
        page = 0
        while True:
            if max_pages is not None and page >= max_pages:
                LOG.info("max_pages=%d reached for %s", max_pages, path)
                return
            params = list(base_params) + [("size", size)]
            if cursor:
                params.append(("cursor", cursor))
            body = self.get_json(path, params)
            items = body.get("items") or []
            for item in items:
                if isinstance(item, dict):
                    yield item
            page += 1
            cursor = body.get("next_cursor")
            if not cursor:
                return


# --------------------------------------------------------------------------- #
# R2
# --------------------------------------------------------------------------- #
def _r2_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def r2_snapshot_prefix(entity_dir: str, snapshot_date: str) -> str:
    return f"shovels/{entity_dir}/snapshot={snapshot_date}"


def _write_parquet_zstd(table: pa.Table, local_path: str) -> int:
    pq.write_table(table, local_path, compression="zstd", compression_level=9)
    return os.path.getsize(local_path)


def _upload_parquet(s3, *, local_path: str, key: str) -> None:
    # L42 compliance: ContentType only, no Content-Encoding header, .parquet ext.
    s3.upload_file(
        local_path,
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )


def _delete_existing_snapshot(s3, *, prefix: str) -> int:
    """Idempotency: clear any prior objects under this snapshot prefix so a
    re-run of the SAME date overwrites rather than accretes parts. Returns the
    number of objects deleted."""
    deleted = 0
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": R2_BUCKET, "Prefix": prefix + "/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        objs = resp.get("Contents") or []
        if objs:
            s3.delete_objects(
                Bucket=R2_BUCKET,
                Delete={"Objects": [{"Key": o["Key"]} for o in objs]},
            )
            deleted += len(objs)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return deleted


# --------------------------------------------------------------------------- #
# ledger — ops.shovels_ingest_runs (entity-discriminated)
# --------------------------------------------------------------------------- #
def _ledger_conn():
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise EnvironmentError("DEX_DB_URL_DIRECT required for the ingest ledger")
    return psycopg.connect(db_url, autocommit=True)


def ledger_insert_running(
    *,
    run_id: str,
    entity: str,
    query_spec_json: str,
    snapshot_date: str,
    r2_prefix: str,
    invoked_by: str,
) -> None:
    with _ledger_conn() as conn:
        conn.execute(
            """
            INSERT INTO ops.shovels_ingest_runs
                (run_id, entity, query_spec, snapshot_date, r2_prefix,
                 status, attempt, started_at, invoked_by)
            VALUES (%s, %s, %s::jsonb, %s, %s, 'running', 1, now(), %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status='running', started_at=now(), updated_at=now()
            """,
            (run_id, entity, query_spec_json, snapshot_date, r2_prefix, invoked_by),
        )


def ledger_finalize(
    *,
    run_id: str,
    status: str,
    r2_object_count: int | None = None,
    r2_total_bytes: int | None = None,
    parquet_row_count: int | None = None,
    lance_rows: int | None = None,
    credits_spent: int | None = None,
    error_message: str | None = None,
) -> None:
    with _ledger_conn() as conn:
        conn.execute(
            """
            UPDATE ops.shovels_ingest_runs
               SET status=%s,
                   r2_object_count=COALESCE(%s, r2_object_count),
                   r2_total_bytes=COALESCE(%s, r2_total_bytes),
                   parquet_row_count=COALESCE(%s, parquet_row_count),
                   lance_rows=COALESCE(%s, lance_rows),
                   credits_spent=COALESCE(%s, credits_spent),
                   error_message=%s,
                   completed_at=now(),
                   duration_seconds=EXTRACT(EPOCH FROM (now() - started_at)),
                   updated_at=now()
             WHERE run_id=%s
            """,
            (
                status, r2_object_count, r2_total_bytes, parquet_row_count,
                lance_rows, credits_spent, error_message, run_id,
            ),
        )


def ledger_set_lance_rows(*, run_id: str, lance_rows: int) -> None:
    with _ledger_conn() as conn:
        conn.execute(
            "UPDATE ops.shovels_ingest_runs SET lance_rows=%s, updated_at=now() WHERE run_id=%s",
            (lance_rows, run_id),
        )


# --------------------------------------------------------------------------- #
# the parameterized ingest driver
# --------------------------------------------------------------------------- #
@dataclass
class IngestResult:
    run_id: str
    entity: str
    snapshot_date: str
    r2_prefix: str
    r2_key: str | None
    parquet_row_count: int
    r2_total_bytes: int
    credits_spent: int
    api_calls: int
    skipped_existing: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def run_entity_ingest(
    *,
    spec: EntityIngestSpec,
    source_endpoint: str,
    record_iter: Iterator[dict[str, Any]],
    client: ShovelsClient,
    query_spec_json: str,
    snapshot_date: str,
    run_id: str,
    invoked_by: str,
    apply: bool,
    write_ledger: bool = True,
) -> IngestResult:
    """Drive one entity ingest: consume ``record_iter`` (raw dicts already
    fetched via ``client``), project to typed+raw+provenance rows, write a
    single ZSTD Parquet, overwrite the dated R2 snapshot partition, and write
    the ledger run row.

    ``record_iter`` is produced by the entity CLI (it knows how to call the
    right endpoint(s) and fan out over id-lists). The driver stays generic.

    Idempotency: the snapshot prefix is cleared before upload, so re-running the
    same ``snapshot_date`` overwrites in place — no duplicate parts, stable Lance
    row count after the rebuild.
    """
    r2_prefix = r2_snapshot_prefix(spec.r2_entity_dir, snapshot_date)
    ingested_at = datetime.now(timezone.utc)

    if apply and write_ledger:
        ledger_insert_running(
            run_id=run_id, entity=spec.entity, query_spec_json=query_spec_json,
            snapshot_date=snapshot_date, r2_prefix=r2_prefix, invoked_by=invoked_by,
        )

    rows: list[dict[str, Any]] = []
    try:
        for raw in record_iter:
            rows.append(
                spec.project(
                    raw=raw,
                    source_endpoint=source_endpoint,
                    query_spec_json=query_spec_json,
                    snapshot_date=snapshot_date,
                    run_id=run_id,
                    ingested_at=ingested_at,
                )
            )
    except Exception as exc:  # noqa: BLE001
        if apply and write_ledger:
            ledger_finalize(
                run_id=run_id, status="failed", credits_spent=client.credits_spent,
                error_message=f"{type(exc).__name__}: {exc}"[:4000],
            )
        raise

    row_count = len(rows)
    LOG.info(
        "entity=%s fetched rows=%d credits_spent=%d api_calls=%d",
        spec.entity, row_count, client.credits_spent, client.api_calls,
    )

    table = pa.Table.from_pylist(rows, schema=spec.arrow_schema()) if rows \
        else pa.Table.from_pylist([], schema=spec.arrow_schema())

    if not apply:
        LOG.info("DRY RUN — not writing R2 / ledger (would write %d rows to %s)", row_count, r2_prefix)
        return IngestResult(
            run_id=run_id, entity=spec.entity, snapshot_date=snapshot_date,
            r2_prefix=r2_prefix, r2_key=None, parquet_row_count=row_count,
            r2_total_bytes=0, credits_spent=client.credits_spent,
            api_calls=client.api_calls,
        )

    # Write Parquet locally then upload, overwriting the dated partition.
    tmp_dir = "/tmp/shovels"
    os.makedirs(tmp_dir, exist_ok=True)
    local_path = os.path.join(tmp_dir, f"{spec.entity}_{snapshot_date}_{run_id[:8]}.parquet")
    byte_size = _write_parquet_zstd(table, local_path)
    r2_key = f"{r2_prefix}/part-00000.parquet"

    s3 = _r2_client()
    try:
        deleted = _delete_existing_snapshot(s3, prefix=r2_prefix)
        if deleted:
            LOG.info("cleared %d prior object(s) under %s (idempotent overwrite)", deleted, r2_prefix)
        _upload_parquet(s3, local_path=local_path, key=r2_key)
        LOG.info("uploaded %s (%d bytes, %d rows)", r2_key, byte_size, row_count)
    except Exception as exc:  # noqa: BLE001
        if write_ledger:
            ledger_finalize(
                run_id=run_id, status="failed", credits_spent=client.credits_spent,
                error_message=f"r2_upload: {type(exc).__name__}: {exc}"[:4000],
            )
        raise
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass

    if write_ledger:
        # status 'r2_landed' — Lance emit (separate step) flips it to 'completed'
        # and stamps lance_rows. This keeps the ledger honest about phase.
        ledger_finalize(
            run_id=run_id, status="r2_landed",
            r2_object_count=1, r2_total_bytes=byte_size,
            parquet_row_count=row_count, credits_spent=client.credits_spent,
        )

    return IngestResult(
        run_id=run_id, entity=spec.entity, snapshot_date=snapshot_date,
        r2_prefix=r2_prefix, r2_key=r2_key, parquet_row_count=row_count,
        r2_total_bytes=byte_size, credits_spent=client.credits_spent,
        api_calls=client.api_calls,
    )


# --------------------------------------------------------------------------- #
# shared CLI scaffolding
# --------------------------------------------------------------------------- #
def default_snapshot_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def new_run_id() -> str:
    return str(uuid.uuid4())


def require_api_key() -> str:
    key = os.environ.get("SHOVELS_API_KEY")
    if not key:
        raise SystemExit("FAIL: SHOVELS_API_KEY not set (Doppler hq-all/prd)")
    return key


def read_usage_credits(client: ShovelsClient) -> int | None:
    """Free /usage read → cumulative credits_used (for before/after deltas)."""
    try:
        body = client.get_json("/usage")
        return int(body.get("credits_used")) if body.get("credits_used") is not None else None
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not read /usage: %s", exc)
        return None


def parse_snapshot_date(value: str) -> str:
    # Validate YYYY-MM-DD; Shovels rejects month-only (§2) but here it is our own
    # partition axis, so just enforce the canonical format.
    date.fromisoformat(value)
    return value
