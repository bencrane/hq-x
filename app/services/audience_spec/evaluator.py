"""Spec evaluator skeleton — compile / preview / sign / replenishment.

Pieces:
  - ``compile(spec, snapshot_ts)``  → CompiledQuery (SQL + params + sources)
  - ``preview(spec_id)``            → PreviewResult (count + sample + freshness)
  - ``sign(spec_id, signature)``    → Signing (frozen cohort manifest)
  - ``replenishment_status(signing_id)`` → live cohort vs at-signing baseline

The evaluator reads from the same Iceberg catalog DEX writes to. DuckDB
is the execution engine; Iceberg tables are loaded via PyIceberg's
``table.scan().to_duckdb()`` Arrow bridge. When Polaris ships, the
catalog seam updates; nothing else changes.

Phase 4 vector primitives (``similar_to``, ``semantic_match``) raise
``NotImplementedError`` when used. Forward-compatible.
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.services.audience_spec.catalog import (
    PRODUCTION_WAREHOUSE,
    configure_duckdb_secret,
    get_catalog,
)
from app.services.audience_spec.models import (
    AudienceSpec,
    CatalogRef,
    FreshnessRequirement,
    ScalarPredicate,
)

LOG = logging.getLogger(__name__)

# ─── DuckDB singleton ─────────────────────────────────────────────────

_duckdb_lock = threading.Lock()
_duckdb_con: Any = None


def _get_duckdb() -> Any:
    """One DuckDB connection per process (cheap to share; views reset
    per evaluate)."""
    global _duckdb_con
    import duckdb
    with _duckdb_lock:
        if _duckdb_con is None:
            _duckdb_con = duckdb.connect(":memory:")
            try:
                configure_duckdb_secret(_duckdb_con)
            except Exception as e:
                # Don't crash on dev/test machines without R2 creds; the
                # first query that needs R2 will fail with a clear error.
                LOG.warning("could not configure DuckDB R2 secret: %s", e)
        return _duckdb_con


# ─── compile ──────────────────────────────────────────────────────────


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]
    sources: list[CatalogRef]
    snapshot_ts: datetime


_OP_MAP_SQL = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "LIKE",
    "ilike": "ILIKE",
}


def _duckdb_view_name(ref: CatalogRef) -> str:
    """Naming rule mirrors DEX: ``namespace.table`` → ``namespace_table``
    (dot-flattened so it's a single SQL identifier in FROM)."""
    return ref.qualified().replace(".", "_")


def _filter_to_sql(f: ScalarPredicate, params: list[Any]) -> str:
    """Build one WHERE clause fragment with ``?`` placeholders.

    Column name has been validated as a SQL identifier by the pydantic
    layer; values bind positionally.
    """
    col = f'"{f.column}"'
    if f.op == "is_null":
        return f"{col} IS NULL"
    if f.op == "is_not_null":
        return f"{col} IS NOT NULL"
    if f.op == "in":
        placeholders = ",".join(["?"] * len(f.value))
        params.extend(f.value)
        return f"{col} IN ({placeholders})"
    if f.op == "nin":
        placeholders = ",".join(["?"] * len(f.value))
        params.extend(f.value)
        return f"{col} NOT IN ({placeholders})"
    if f.op == "between":
        params.extend([f.value[0], f.value[1]])
        return f"{col} BETWEEN ? AND ?"
    sql_op = _OP_MAP_SQL[f.op]
    params.append(f.value)
    return f"{col} {sql_op} ?"


def compile(spec: AudienceSpec, snapshot_ts: datetime) -> CompiledQuery:
    """Take the spec + a frozen catalog snapshot timestamp, return SQL + params.

    The ``snapshot_ts`` parameter is reserved for time-travel reads; the
    current implementation uses the catalog's latest snapshot per source.
    Once the FMCSA Iceberg tables are re-registered with snapshot_date as
    a partition column (see views/fmcsa/carrier_latest.sql comment), the
    evaluator can pin every read to ``snapshot_ts`` via PyIceberg's
    ``snapshot_id`` parameter.
    """
    if spec.similar_to is not None:
        raise NotImplementedError(
            "AudienceSpec.similar_to (vector k-NN) is a Phase 4 primitive; "
            "scaffolded but not yet evaluable."
        )
    if spec.semantic_match is not None:
        raise NotImplementedError(
            "AudienceSpec.semantic_match (semantic-text-match) is a Phase 4 "
            "primitive; scaffolded but not yet evaluable."
        )
    if spec.exclude:
        # Exclusion rules are part of the spec language but the evaluator
        # routing-by-kind isn't in the scaffold. Surface clearly.
        raise NotImplementedError(
            "AudienceSpec.exclude rule routing lands with Phase 3 "
            "(cohort-events log + match-invalidation)."
        )

    base = spec.primary_source
    table_name = _duckdb_view_name(base)

    params: list[Any] = []
    where_clauses = [_filter_to_sql(f, params) for f in spec.filters]

    sql = f'SELECT * FROM "{table_name}"'
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    return CompiledQuery(
        sql=sql,
        params=params,
        sources=list(spec.sources),
        snapshot_ts=snapshot_ts,
    )


# ─── source registration ──────────────────────────────────────────────


_FMCSA_VIEW_NS_RE = re.compile(r"^fmcsa$")


def _register_iceberg_source(con: Any, ref: CatalogRef) -> None:
    """Register a CatalogRef as a DuckDB view via PyIceberg's Arrow bridge.

    Per typed_audiences in DEX: ``table.scan().to_duckdb(table_name=...)``
    avoids the DuckDB iceberg extension's add_files() incompatibility
    (see scripts/_lib/iceberg_catalog.py docstring). This is the
    canonical "always-fresh" path.

    Side effect: stamps the read into the request-scoped lineage tracker
    so the X-Data-Lineage response header reflects every catalog table
    this request actually queried (Phase 0b verification gate).
    """
    from app.services.lineage import record_catalog_read
    catalog = get_catalog()
    table = catalog.load_table((ref.namespace, ref.table))
    view_name = _duckdb_view_name(ref)
    table.scan().to_duckdb(table_name=view_name, connection=con)
    snap = table.current_snapshot()
    record_catalog_read(
        table=ref.qualified(),
        snapshot_id=str(snap.snapshot_id) if snap is not None else None,
        format="iceberg",
    )


# ─── preview ──────────────────────────────────────────────────────────


@dataclass
class FreshnessCheck:
    source: str
    max_age_seconds: int
    observed_age_seconds: int | None
    ok: bool


@dataclass
class PreviewResult:
    count: int
    sample: list[dict[str, Any]]
    sources_used: list[str]
    freshness_checks: list[FreshnessCheck]
    snapshot_ts: datetime
    elapsed_s: float


def _measure_source_freshness(ref: CatalogRef) -> datetime | None:
    """Return the timestamp of the latest snapshot for an Iceberg source.

    Uses PyIceberg's metadata: ``table.current_snapshot().timestamp_ms``.
    Returns None if the table has no committed snapshots.
    """
    try:
        catalog = get_catalog()
        table = catalog.load_table((ref.namespace, ref.table))
        snap = table.current_snapshot()
        if snap is None:
            return None
        # timestamp_ms is wall-clock UTC milliseconds.
        return datetime.fromtimestamp(snap.timestamp_ms / 1000, tz=timezone.utc)
    except Exception as e:
        LOG.warning("freshness probe failed for %s.%s: %s",
                    ref.namespace, ref.table, e)
        return None


def _check_freshness(
    requirements: list[FreshnessRequirement],
    source_refs: list[CatalogRef],
) -> list[FreshnessCheck]:
    """Probe every required source's latest snapshot vs the SLA.

    ``source`` matching is loose: a requirement names ``fmcsa.carrier_latest``
    or ``fmcsa.carrier`` and we look up the matching CatalogRef in the
    spec's sources. Mismatches return ok=False.
    """
    refs_by_qualified = {r.qualified(): r for r in source_refs}
    now = datetime.now(timezone.utc)
    out: list[FreshnessCheck] = []
    for req in requirements:
        ref = refs_by_qualified.get(req.source)
        if ref is None:
            out.append(FreshnessCheck(
                source=req.source,
                max_age_seconds=req.max_age_seconds,
                observed_age_seconds=None,
                ok=False,
            ))
            continue
        ts = _measure_source_freshness(ref)
        if ts is None:
            out.append(FreshnessCheck(
                source=req.source,
                max_age_seconds=req.max_age_seconds,
                observed_age_seconds=None,
                ok=False,
            ))
            continue
        age = int((now - ts).total_seconds())
        out.append(FreshnessCheck(
            source=req.source,
            max_age_seconds=req.max_age_seconds,
            observed_age_seconds=age,
            ok=age <= req.max_age_seconds,
        ))
    return out


_SAMPLE_LIMIT = 25


async def preview(spec_id: UUID) -> PreviewResult:
    """Run the spec against fresh catalog. Returns count + sample + freshness.

    Refuses with ``FreshnessSLABreach`` if any ``required_freshness`` SLA
    isn't met right now. Refusal is the point — the partner shouldn't
    sign against a stale catalog.
    """
    spec_row = await _fetch_spec(spec_id)
    spec = AudienceSpec.model_validate(spec_row["content"])

    t0 = time.monotonic()
    snapshot_ts = datetime.now(timezone.utc)
    compiled = compile(spec, snapshot_ts)

    # Enforce freshness SLAs BEFORE running the query — cheap fail-fast.
    fresh_checks = _check_freshness(spec.required_freshness, list(spec.sources))
    if any(not c.ok for c in fresh_checks):
        raise FreshnessSLABreach(checks=fresh_checks)

    con = _get_duckdb()
    for src in compiled.sources:
        _register_iceberg_source(con, src)

    count_sql = f"SELECT COUNT(*) FROM ({compiled.sql})"
    count = con.execute(count_sql, compiled.params).fetchone()[0]

    sample_sql = f"{compiled.sql} LIMIT {_SAMPLE_LIMIT}"
    cur = con.execute(sample_sql, compiled.params)
    cols = [d[0] for d in cur.description]
    sample_rows = cur.fetchall()
    sample = [dict(zip(cols, r, strict=True)) for r in sample_rows]
    # JSON-friendly: stringify everything that isn't trivially serializable.
    sample = [{k: _json_safe(v) for k, v in row.items()} for row in sample]

    return PreviewResult(
        count=int(count),
        sample=sample,
        sources_used=[r.qualified() for r in compiled.sources],
        freshness_checks=fresh_checks,
        snapshot_ts=snapshot_ts,
        elapsed_s=round(time.monotonic() - t0, 3),
    )


def _json_safe(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    return str(v)


# ─── sign ─────────────────────────────────────────────────────────────


@dataclass
class Signing:
    signing_id: UUID
    spec_id: UUID
    signed_at: datetime
    catalog_snapshot_ts: datetime
    count_at_signing: int
    cohort_manifest_uri: str
    contract_term_days: int
    expires_at: datetime
    source_freshness_at_signing: list[dict[str, Any]] = field(default_factory=list)


_COHORT_BUCKET = "dex-raw-landing-zone"
_COHORT_PREFIX = "audience-cohort-manifests"


def _cohort_manifest_uri(signing_id: UUID, signed_at: datetime) -> str:
    """``s3://<bucket>/audience-cohort-manifests/YYYY/MM/DD/<signing_id>.parquet``."""
    return (
        f"s3://{_COHORT_BUCKET}/{_COHORT_PREFIX}/"
        f"{signed_at.year:04d}/{signed_at.month:02d}/{signed_at.day:02d}/"
        f"{signing_id}.parquet"
    )


def _entity_ref_column(spec: AudienceSpec) -> str:
    """The column whose value we use as the cohort's entity_ref.

    v1 heuristic: if the primary source is FMCSA carrier_latest, use
    ``dot_number``. Otherwise look for common identity columns in
    priority order. Specs can override later via an explicit
    ``entity_ref_column`` field on AudienceSpec (deferred).
    """
    base = spec.primary_source
    if base.namespace == "fmcsa" and base.table == "carrier_latest":
        return "dot_number"
    # Generic priority list — first column found in the result set wins.
    return ""  # signal: caller must handle empty


def _stringify_for_arrow(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _freeze_cohort_to_r2(
    signing_id: UUID,
    signed_at: datetime,
    spec: AudienceSpec,
    compiled: CompiledQuery,
    entity_ref_col: str,
) -> tuple[str, int]:
    """Freeze the live cohort to R2 as parquet.

    Returns ``(uri, row_count)``. The parquet has two columns:
      - ``entity_ref``       — TEXT (stringified)
      - ``attribute_snapshot`` — TEXT (JSON)

    Per matches_first_class_surfacing_multichannel.md: the cohort manifest
    can include cold (non-platform) entities; nothing here gates on
    platform participation.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    con = _get_duckdb()
    # Re-register for safety (sign() and preview() share the singleton).
    for src in compiled.sources:
        _register_iceberg_source(con, src)

    cur = con.execute(compiled.sql, compiled.params)
    cols = [d[0] for d in cur.description]

    # Auto-detect the entity_ref column if the heuristic didn't pick one.
    if not entity_ref_col:
        for candidate in ("dot_number", "uei", "ein", "lei", "duns"):
            if candidate in cols:
                entity_ref_col = candidate
                break
    if not entity_ref_col:
        # Last resort: stringify the first column.
        entity_ref_col = cols[0]

    entity_refs: list[str] = []
    snapshots: list[str] = []
    for row in cur.fetchall():
        row_dict = dict(zip(cols, row, strict=True))
        entity_refs.append(str(row_dict.get(entity_ref_col, "")))
        snapshots.append(json.dumps(
            {k: _stringify_for_arrow(v) for k, v in row_dict.items()},
            default=str,
        ))

    table = pa.table({
        "entity_ref": entity_refs,
        "attribute_snapshot": snapshots,
    })

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", compression_level=9)
    buf.seek(0)

    uri = _cohort_manifest_uri(signing_id, signed_at)
    _put_to_r2(uri, buf.getvalue())
    return uri, len(entity_refs)


def _put_to_r2(s3_uri: str, body: bytes) -> None:
    """Upload bytes to ``s3://bucket/key`` using the R2 S3-compat API."""
    import boto3

    if not s3_uri.startswith("s3://"):
        raise ValueError(f"expected s3:// uri, got {s3_uri!r}")
    rest = s3_uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"could not parse bucket/key from {s3_uri!r}")

    import os
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    client.put_object(Bucket=bucket, Key=key, Body=body)


async def sign(spec_id: UUID, partner_signature: dict[str, Any]) -> Signing:
    """Atomically: compute snapshot_ts, freeze cohort to R2, insert signing row.

    The signing row IS the contract artifact. The cohort manifest URI it
    holds is the immutable parquet of (entity_ref, attribute_snapshot)
    that the partner has agreed to. Refund logic anchors here.
    """
    spec_row = await _fetch_spec(spec_id)
    spec = AudienceSpec.model_validate(spec_row["content"])

    # Re-check freshness at sign time; refuse on breach.
    fresh_checks = _check_freshness(spec.required_freshness, list(spec.sources))
    if any(not c.ok for c in fresh_checks):
        raise FreshnessSLABreach(checks=fresh_checks)

    snapshot_ts = datetime.now(timezone.utc)
    compiled = compile(spec, snapshot_ts)

    from uuid import uuid4
    signing_id = uuid4()
    signed_at = snapshot_ts

    entity_ref_col = _entity_ref_column(spec)
    cohort_uri, count = _freeze_cohort_to_r2(
        signing_id, signed_at, spec, compiled, entity_ref_col,
    )

    contract_term_days = 90  # default per directive
    expires_at = signed_at + timedelta(days=contract_term_days)
    fresh_payload = [
        {
            "source": c.source,
            "max_age_seconds": c.max_age_seconds,
            "observed_age_seconds": c.observed_age_seconds,
            "ok": c.ok,
        }
        for c in fresh_checks
    ]

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # expires_at is filled by the BEFORE INSERT trigger
            # (business._audience_spec_signings_set_expires_at).
            await cur.execute(
                """
                INSERT INTO business.audience_spec_signings (
                    signing_id, spec_id, signed_at, catalog_snapshot_ts,
                    count_at_signing, cohort_manifest_uri,
                    partner_signature, contract_term_days,
                    source_freshness_at_signing, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s
                )
                """,
                (
                    str(signing_id),
                    str(spec_id),
                    signed_at,
                    snapshot_ts,
                    count,
                    cohort_uri,
                    json.dumps(partner_signature),
                    contract_term_days,
                    json.dumps(fresh_payload),
                    expires_at,  # trigger will overwrite to ensure consistency
                ),
            )
            await cur.execute(
                """
                UPDATE business.audience_specs
                SET status = 'signed'
                WHERE spec_id = %s
                """,
                (str(spec_id),),
            )
        await conn.commit()

    return Signing(
        signing_id=signing_id,
        spec_id=spec_id,
        signed_at=signed_at,
        catalog_snapshot_ts=snapshot_ts,
        count_at_signing=count,
        cohort_manifest_uri=cohort_uri,
        contract_term_days=contract_term_days,
        expires_at=expires_at,
        source_freshness_at_signing=fresh_payload,
    )


# ─── replenishment ────────────────────────────────────────────────────


@dataclass
class ReplenishmentStatus:
    signing_id: UUID
    spec_id: UUID
    count_at_signing: int
    live_count: int
    delta: int
    days_remaining: int
    at_risk: bool
    freshness_now: list[dict[str, Any]]


async def replenishment_status(signing_id: UUID) -> ReplenishmentStatus:
    """Live cohort vs at-signing baseline + days-remaining + at-risk flag.

    ``at_risk = live_count < count_at_signing * 0.95``. Anything below 95%
    of the at-signing baseline is flagged. The threshold lives here as a
    constant for v1; later it becomes a per-spec / per-partner setting.
    """
    sig_row = await _fetch_signing(signing_id)
    spec_id_raw = sig_row["spec_id"]
    spec_uuid = spec_id_raw if isinstance(spec_id_raw, UUID) else UUID(str(spec_id_raw))
    spec_row = await _fetch_spec(spec_uuid)
    spec = AudienceSpec.model_validate(spec_row["content"])

    # Live count via re-compile against current catalog.
    snapshot_ts = datetime.now(timezone.utc)
    compiled = compile(spec, snapshot_ts)
    con = _get_duckdb()
    for src in compiled.sources:
        _register_iceberg_source(con, src)
    live_count = int(
        con.execute(
            f"SELECT COUNT(*) FROM ({compiled.sql})",
            compiled.params,
        ).fetchone()[0]
    )

    fresh_checks = _check_freshness(spec.required_freshness, list(spec.sources))

    expires_at: datetime = sig_row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    days_remaining = max(0, (expires_at - snapshot_ts).days)

    count_at_signing = int(sig_row["count_at_signing"])
    delta = live_count - count_at_signing
    at_risk = live_count < int(count_at_signing * 0.95)

    return ReplenishmentStatus(
        signing_id=signing_id,
        spec_id=spec_uuid,
        count_at_signing=count_at_signing,
        live_count=live_count,
        delta=delta,
        days_remaining=days_remaining,
        at_risk=at_risk,
        freshness_now=[
            {
                "source": c.source,
                "max_age_seconds": c.max_age_seconds,
                "observed_age_seconds": c.observed_age_seconds,
                "ok": c.ok,
            }
            for c in fresh_checks
        ],
    )


# ─── DB row fetchers ──────────────────────────────────────────────────


async def _fetch_spec(spec_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT spec_id, partner_id, version, parent_spec_id,
                       content, status, required_freshness,
                       created_at, created_by_user_id, notes
                FROM business.audience_specs
                WHERE spec_id = %s
                """,
                (str(spec_id),),
            )
            row = await cur.fetchone()
            if row is None:
                raise SpecNotFound(spec_id)
            cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=True))


async def _fetch_signing(signing_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT signing_id, spec_id, signed_at, catalog_snapshot_ts,
                       count_at_signing, cohort_manifest_uri,
                       partner_signature, contract_term_days, expires_at,
                       source_freshness_at_signing, notes
                FROM business.audience_spec_signings
                WHERE signing_id = %s
                """,
                (str(signing_id),),
            )
            row = await cur.fetchone()
            if row is None:
                raise SigningNotFound(signing_id)
            cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=True))


# ─── exceptions ───────────────────────────────────────────────────────


class SpecNotFound(LookupError):
    def __init__(self, spec_id: UUID):
        super().__init__(f"audience_spec {spec_id} not found")
        self.spec_id = spec_id


class SigningNotFound(LookupError):
    def __init__(self, signing_id: UUID):
        super().__init__(f"audience_spec_signing {signing_id} not found")
        self.signing_id = signing_id


class FreshnessSLABreach(RuntimeError):
    """Raised when a spec's required_freshness SLA isn't met right now.

    The router maps this to HTTP 409 (preview/sign refused on stale data).
    Per operator_data_anxieties_phase_0.md: staleness is a contract
    surface, not a soft signal.
    """

    def __init__(self, checks: list[FreshnessCheck]):
        self.checks = checks
        breached = [c for c in checks if not c.ok]
        msg = "; ".join(
            f"{c.source} stale (observed_age={c.observed_age_seconds}s, "
            f"max={c.max_age_seconds}s)"
            for c in breached
        )
        super().__init__(f"freshness SLA breach: {msg}")


__all__ = [
    "compile",
    "preview",
    "sign",
    "replenishment_status",
    "CompiledQuery",
    "PreviewResult",
    "FreshnessCheck",
    "Signing",
    "ReplenishmentStatus",
    "SpecNotFound",
    "SigningNotFound",
    "FreshnessSLABreach",
    "PRODUCTION_WAREHOUSE",
]
