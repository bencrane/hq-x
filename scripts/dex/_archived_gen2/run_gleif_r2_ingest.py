#!/usr/bin/env python3
"""GLEIF (Global Legal Entity Identifier Foundation) Golden-Copy ->
R2 daily snapshot ingest.

Mirrors the NPPES / FDIC / NCUA pattern adapted for GLEIF's daily
concatenated XML files (one Level-1 file, one Level-2 relationship file):

  s3://dex-raw-landing-zone/gleif/snapshot=YYYY-MM-DD/lei_records.parquet
  s3://dex-raw-landing-zone/gleif/snapshot=YYYY-MM-DD/relationship_records.parquet

Audit ledger: ops.gleif_r2_ingest_runs (one row per (snapshot_date,
file_kind)). Idempotency basis: HEAD Last-Modified per URL.

Pipeline per file:
  1. HEAD Last-Modified, skip-if-unchanged.
  2. Stream-download ZIP.
  3. Stream-parse the inner XML via ``lxml.etree.iterparse`` (memory-
     bounded; the LEI XML is ~8 GB uncompressed).
  4. Project each record into a flat dict; flush to a ZSTD Parquet
     ``ParquetWriter`` in 100 000-row batches.
  5. boto3 upload to R2.
  6. Audit-row write.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_gleif_r2_ingest.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_gleif_r2_ingest.py --snapshot 2026-05-07
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_gleif_r2_ingest.py --file-kind lei2 \\
        --max-records 10000 --r2-prefix-override 'gleif/_smoke/snapshot=2026-05-08/'
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_gleif_r2_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# Ensure ``scripts._lib.*`` resolves when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import boto3
import httpx
import lxml.etree as ET
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

from scripts._lib.gleif_normalize import normalize_legal_name


R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
PARQUET_BATCH_SIZE = 100_000

NS_LEI = "{http://www.gleif.org/data/schema/leidata/2016}"
NS_RR = "{http://www.gleif.org/data/schema/rr/2016}"

LEI_RECORDS_FLOOR = 2_500_000
RR_RECORDS_FLOOR = 300_000
LEI_LENGTH = 20

# ZIP-size sanity floors (bytes). LEI ZIP is ~500 MB; RR ZIP is ~30 MB.
LEI_ZIP_MIN_BYTES = 50 * (1 << 20)
RR_ZIP_MIN_BYTES = 5 * (1 << 20)


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("gleif-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Snapshot / file-kind config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FileSpec:
    file_kind: str  # "lei2" | "rr"
    parquet_basename: str

    @property
    def url_template(self) -> str:
        return (
            "https://leidata.gleif.org/api/v1/concatenated-files/"
            f"{self.file_kind}/{{ymd}}/zip"
        )


LEI2 = FileSpec(file_kind="lei2", parquet_basename="lei_records.parquet")
RR = FileSpec(file_kind="rr", parquet_basename="relationship_records.parquet")
ALL_FILE_SPECS: dict[str, FileSpec] = {LEI2.file_kind: LEI2, RR.file_kind: RR}


@dataclass(frozen=True)
class Snapshot:
    snapshot_date: date

    @property
    def label(self) -> str:
        return self.snapshot_date.isoformat()

    @property
    def ymd_compact(self) -> str:
        return self.snapshot_date.strftime("%Y%m%d")

    @property
    def r2_prefix(self) -> str:
        return f"gleif/snapshot={self.label}/"

    def url_for(self, spec: FileSpec) -> str:
        return spec.url_template.format(ymd=self.ymd_compact)


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None, int]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=60.0)
            if r.status_code == 404:
                return None, None, 404
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            cl = int(r.headers.get("content-length", 0)) or None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(
                        lm_raw, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            return cl, lm, r.status_code
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=3600.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss", url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    last_log = time.monotonic()
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 10.0:
                            log.info(
                                "  download progress: %.1f MB written",
                                written / (1 << 20),
                            )
                            last_log = now
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed: {last_exc}")


# --------------------------------------------------------------------------- #
# ZIP unpack
# --------------------------------------------------------------------------- #


def extract_inner_xml(zip_path: Path, dest_dir: Path) -> Path:
    """Extract the single inner ``.xml`` file from a GLEIF concatenated-file
    ZIP. GLEIF ZIPs always contain exactly one XML payload."""
    with zipfile.ZipFile(zip_path) as z:
        xml_members = [
            i for i in z.infolist()
            if i.filename.lower().endswith(".xml")
        ]
        if not xml_members:
            raise RuntimeError(f"{zip_path}: no .xml inside the ZIP")
        if len(xml_members) > 1:
            raise RuntimeError(
                f"{zip_path}: expected one inner .xml, got {len(xml_members)}: "
                f"{[m.filename for m in xml_members]}"
            )
        member = xml_members[0]
        target = dest_dir / Path(member.filename).name
        with z.open(member) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
    return target


# --------------------------------------------------------------------------- #
# XML stream-parse: LEI records
# --------------------------------------------------------------------------- #


def _text(elem: ET._Element | None) -> str | None:
    if elem is None:
        return None
    t = elem.text
    if t is None:
        return None
    s = t.strip()
    return s or None


def _zip5_or_none(postal_code: str | None, country: str | None) -> str | None:
    """Return the leading 5 digits of a US postal code, else None.

    GLEIF is global, but the directive asks for a ``headquarters_zip5``
    column for compatibility with US-side identity-bridge MVs (HMDA,
    FEC). We only emit a value when the entity is in the US and the
    postal code starts with 5 digits.
    """
    if country != "US" or not postal_code:
        return None
    digits = "".join(c for c in postal_code if c.isdigit())
    return digits[:5] if len(digits) >= 5 else None


def _iso_date(s: str | None) -> date | None:
    """Parse an ISO 8601 datetime stamp to a date. GLEIF dates come as
    ``2014-11-06T00:00:00Z`` or ``2025-11-05T12:54:14Z``."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _addr_block(entity: ET._Element, tag_name: str) -> dict[str, str | None]:
    """Project a ``<lei:LegalAddress>`` or ``<lei:HeadquartersAddress>`` block
    into a flat dict of typed fields.
    """
    blk = entity.find(f"{NS_LEI}{tag_name}")
    if blk is None:
        return {"country": None, "region": None, "city": None, "postal_code": None}
    return {
        "country": _text(blk.find(f"{NS_LEI}Country")),
        "region": _text(blk.find(f"{NS_LEI}Region")),
        "city": _text(blk.find(f"{NS_LEI}City")),
        "postal_code": _text(blk.find(f"{NS_LEI}PostalCode")),
    }


def stream_lei_records(
    xml_path: Path,
    *,
    snapshot: Snapshot,
    max_records: int | None,
) -> Iterator[dict[str, Any]]:
    """Stream-parse LEI-CDF v2.1 XML. Yields a flat dict per
    ``<lei:LEIRecord>``. Memory-bounded — clears each record + its
    preceding siblings after yielding.
    """
    context = ET.iterparse(
        str(xml_path), events=("end",), tag=f"{NS_LEI}LEIRecord",
    )
    yielded = 0
    for _event, elem in context:
        try:
            entity = elem.find(f"{NS_LEI}Entity")
            registration = elem.find(f"{NS_LEI}Registration")

            legal_name_raw: str | None = None
            entity_status: str | None = None
            entity_category: str | None = None
            legal_form_id: str | None = None
            legal_addr = {"country": None, "region": None, "city": None, "postal_code": None}
            hq_addr = {"country": None, "region": None, "city": None, "postal_code": None}
            validation_authority_id: str | None = None
            validation_authority_entity_id: str | None = None
            if entity is not None:
                legal_name_raw = _text(entity.find(f"{NS_LEI}LegalName"))
                entity_status = _text(entity.find(f"{NS_LEI}EntityStatus"))
                entity_category = _text(entity.find(f"{NS_LEI}EntityCategory"))
                legal_form = entity.find(f"{NS_LEI}LegalForm")
                if legal_form is not None:
                    legal_form_id = _text(
                        legal_form.find(f"{NS_LEI}EntityLegalFormCode")
                    )
                legal_addr = _addr_block(entity, "LegalAddress")
                hq_addr = _addr_block(entity, "HeadquartersAddress")

            initial_registration_date: date | None = None
            last_update_date: date | None = None
            next_renewal_date: date | None = None
            registration_status: str | None = None
            managing_lou: str | None = None
            if registration is not None:
                initial_registration_date = _iso_date(_text(
                    registration.find(f"{NS_LEI}InitialRegistrationDate")
                ))
                last_update_date = _iso_date(_text(
                    registration.find(f"{NS_LEI}LastUpdateDate")
                ))
                next_renewal_date = _iso_date(_text(
                    registration.find(f"{NS_LEI}NextRenewalDate")
                ))
                registration_status = _text(
                    registration.find(f"{NS_LEI}RegistrationStatus")
                )
                managing_lou = _text(registration.find(f"{NS_LEI}ManagingLOU"))
                vauth = registration.find(f"{NS_LEI}ValidationAuthority")
                if vauth is not None:
                    validation_authority_id = _text(
                        vauth.find(f"{NS_LEI}ValidationAuthorityID")
                    )
                    validation_authority_entity_id = _text(
                        vauth.find(f"{NS_LEI}ValidationAuthorityEntityID")
                    )

            normalized = normalize_legal_name(legal_name_raw) or None
            yield {
                "lei": _text(elem.find(f"{NS_LEI}LEI")),
                "legal_name": legal_name_raw,
                "legal_name_normalized": normalized,
                "entity_status": entity_status,
                "entity_category": entity_category,
                "legal_form_id": legal_form_id,
                "headquarters_country": hq_addr["country"],
                "headquarters_region": hq_addr["region"],
                "headquarters_city": hq_addr["city"],
                "headquarters_postal_code": hq_addr["postal_code"],
                "headquarters_zip5": _zip5_or_none(
                    hq_addr["postal_code"], hq_addr["country"]
                ),
                "legal_address_country": legal_addr["country"],
                "legal_address_region": legal_addr["region"],
                "legal_address_city": legal_addr["city"],
                "legal_address_postal_code": legal_addr["postal_code"],
                "registration_status": registration_status,
                "initial_registration_date": initial_registration_date,
                "last_update_date": last_update_date,
                "next_renewal_date": next_renewal_date,
                "managing_lou": managing_lou,
                "validation_authority_id": validation_authority_id,
                "validation_authority_entity_id": validation_authority_entity_id,
                "gleif_snapshot_date": snapshot.snapshot_date,
            }
            yielded += 1
            if max_records is not None and yielded >= max_records:
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
                return
        finally:
            # Free memory for this record + any preceding siblings.
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]


# --------------------------------------------------------------------------- #
# XML stream-parse: relationship records
# --------------------------------------------------------------------------- #


def _pick_relationship_period(
    relationship: ET._Element,
) -> tuple[date | None, date | None]:
    """Return (start, end) from the ``RELATIONSHIP_PERIOD`` slot (the
    canonical period type for the relationship lifecycle), falling back
    to the first period if no RELATIONSHIP_PERIOD slot exists.
    """
    periods = relationship.find(f"{NS_RR}RelationshipPeriods")
    if periods is None:
        return None, None
    chosen: ET._Element | None = None
    for period in periods.findall(f"{NS_RR}RelationshipPeriod"):
        ptype = _text(period.find(f"{NS_RR}PeriodType"))
        if ptype == "RELATIONSHIP_PERIOD":
            chosen = period
            break
    if chosen is None:
        first = periods.find(f"{NS_RR}RelationshipPeriod")
        if first is not None:
            chosen = first
    if chosen is None:
        return None, None
    return (
        _iso_date(_text(chosen.find(f"{NS_RR}StartDate"))),
        _iso_date(_text(chosen.find(f"{NS_RR}EndDate"))),
    )


def stream_relationship_records(
    xml_path: Path,
    *,
    snapshot: Snapshot,
    max_records: int | None,
) -> Iterator[dict[str, Any]]:
    """Stream-parse RR-CDF v1.1 XML. Yields a flat dict per
    ``<rr:RelationshipRecord>``. The ``relationship_id`` PK is the
    composite of the two LEIs + the relationship type, joined with
    ``|``. GLEIF doesn't expose a single-field stable PK in the XML.
    """
    context = ET.iterparse(
        str(xml_path), events=("end",), tag=f"{NS_RR}RelationshipRecord",
    )
    yielded = 0
    for _event, elem in context:
        try:
            relationship = elem.find(f"{NS_RR}Relationship")
            registration = elem.find(f"{NS_RR}Registration")

            start_lei: str | None = None
            end_lei: str | None = None
            relationship_type: str | None = None
            relationship_status: str | None = None
            period_start: date | None = None
            period_end: date | None = None
            if relationship is not None:
                start_node = relationship.find(f"{NS_RR}StartNode")
                if start_node is not None:
                    start_lei = _text(start_node.find(f"{NS_RR}NodeID"))
                end_node = relationship.find(f"{NS_RR}EndNode")
                if end_node is not None:
                    end_lei = _text(end_node.find(f"{NS_RR}NodeID"))
                relationship_type = _text(
                    relationship.find(f"{NS_RR}RelationshipType")
                )
                relationship_status = _text(
                    relationship.find(f"{NS_RR}RelationshipStatus")
                )
                period_start, period_end = _pick_relationship_period(relationship)

            initial_registration_date: date | None = None
            last_update_date: date | None = None
            registration_status: str | None = None
            if registration is not None:
                initial_registration_date = _iso_date(_text(
                    registration.find(f"{NS_RR}InitialRegistrationDate")
                ))
                last_update_date = _iso_date(_text(
                    registration.find(f"{NS_RR}LastUpdateDate")
                ))
                registration_status = _text(
                    registration.find(f"{NS_RR}RegistrationStatus")
                )

            relationship_id = (
                f"{start_lei or ''}|{end_lei or ''}|{relationship_type or ''}"
            )
            yield {
                "relationship_id": relationship_id,
                "start_node_lei": start_lei,
                "end_node_lei": end_lei,
                "relationship_type": relationship_type,
                "relationship_status": relationship_status,
                "relationship_period_start": period_start,
                "relationship_period_end": period_end,
                "initial_registration_date": initial_registration_date,
                "last_update_date": last_update_date,
                "registration_status": registration_status,
                "gleif_snapshot_date": snapshot.snapshot_date,
            }
            yielded += 1
            if max_records is not None and yielded >= max_records:
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
                return
        finally:
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]


# --------------------------------------------------------------------------- #
# Parquet schemas
# --------------------------------------------------------------------------- #


LEI_SCHEMA = pa.schema([
    pa.field("lei", pa.string()),
    pa.field("legal_name", pa.string()),
    pa.field("legal_name_normalized", pa.string()),
    pa.field("entity_status", pa.string()),
    pa.field("entity_category", pa.string()),
    pa.field("legal_form_id", pa.string()),
    pa.field("headquarters_country", pa.string()),
    pa.field("headquarters_region", pa.string()),
    pa.field("headquarters_city", pa.string()),
    pa.field("headquarters_postal_code", pa.string()),
    pa.field("headquarters_zip5", pa.string()),
    pa.field("legal_address_country", pa.string()),
    pa.field("legal_address_region", pa.string()),
    pa.field("legal_address_city", pa.string()),
    pa.field("legal_address_postal_code", pa.string()),
    pa.field("registration_status", pa.string()),
    pa.field("initial_registration_date", pa.date32()),
    pa.field("last_update_date", pa.date32()),
    pa.field("next_renewal_date", pa.date32()),
    pa.field("managing_lou", pa.string()),
    pa.field("validation_authority_id", pa.string()),
    pa.field("validation_authority_entity_id", pa.string()),
    pa.field("gleif_snapshot_date", pa.date32()),
])

RR_SCHEMA = pa.schema([
    pa.field("relationship_id", pa.string()),
    pa.field("start_node_lei", pa.string()),
    pa.field("end_node_lei", pa.string()),
    pa.field("relationship_type", pa.string()),
    pa.field("relationship_status", pa.string()),
    pa.field("relationship_period_start", pa.date32()),
    pa.field("relationship_period_end", pa.date32()),
    pa.field("initial_registration_date", pa.date32()),
    pa.field("last_update_date", pa.date32()),
    pa.field("registration_status", pa.string()),
    pa.field("gleif_snapshot_date", pa.date32()),
])


# --------------------------------------------------------------------------- #
# Parquet writer (batched)
# --------------------------------------------------------------------------- #


def write_records_to_parquet(
    records: Iterator[dict[str, Any]],
    parquet_path: Path,
    *,
    schema: pa.Schema,
    log_prefix: str,
) -> tuple[int, int]:
    """Write a stream of dicts to a ZSTD Parquet file in batches. Returns
    ``(rows_yielded, parquet_row_count)``."""
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(
        str(parquet_path), schema, compression="zstd", compression_level=9,
    )
    total = 0
    batch: list[dict[str, Any]] = []
    t0 = time.monotonic()
    last_log = t0
    try:
        for rec in records:
            batch.append(rec)
            if len(batch) >= PARQUET_BATCH_SIZE:
                tbl = pa.Table.from_pylist(batch, schema=schema)
                writer.write_table(tbl)
                total += len(batch)
                batch.clear()
                now = time.monotonic()
                if now - last_log >= 5.0:
                    log.info(
                        "%s   parquet progress: %s rows written (%.1fs)",
                        log_prefix, f"{total:,}", now - t0,
                    )
                    last_log = now
        if batch:
            tbl = pa.Table.from_pylist(batch, schema=schema)
            writer.write_table(tbl)
            total += len(batch)
            batch.clear()
    finally:
        writer.close()

    log.info(
        "%s   parquet write done: %s rows, %.1f MB, %.1fs",
        log_prefix, f"{total:,}",
        parquet_path.stat().st_size / (1 << 20),
        time.monotonic() - t0,
    )

    # Independent row-count verification.
    pf = pq.ParquetFile(str(parquet_path))
    row_count = pf.metadata.num_rows
    return total, row_count


def upload_to_r2(parquet_path: Path, *, key: str, log_prefix: str) -> int:
    s3 = _r2_client()
    n_bytes = parquet_path.stat().st_size
    log.info(
        "%s uploading %.1f MB -> s3://%s/%s",
        log_prefix, n_bytes / (1 << 20), R2_BUCKET, key,
    )
    s3.upload_file(
        str(parquet_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return n_bytes


# --------------------------------------------------------------------------- #
# Audit ledger
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot: Snapshot,
    file_kind: str,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.gleif_r2_ingest_runs (
        snapshot_date, file_kind, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            snapshot.snapshot_date, file_kind, source_url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection,
    *,
    snapshot: Snapshot,
    file_kind: str,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_last_modified
              FROM ops.gleif_r2_ingest_runs
             WHERE snapshot_date = %s AND file_kind = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (snapshot.snapshot_date, file_kind),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    snapshot: Snapshot,
    file_kind: str,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.gleif_r2_ingest_runs (
                snapshot_date, file_kind, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                snapshot.snapshot_date, file_kind, source_url,
                source_last_modified, prior_source_last_modified,
                started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes_downloaded: int,
    xml_record_count: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    r2_key: str | None,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.gleif_r2_ingest_runs
               SET status = %s,
                   zip_bytes_downloaded = %s,
                   xml_record_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_key = %s, r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """,
            (
                status, zip_bytes_downloaded, xml_record_count,
                parquet_row_count, parquet_bytes_written,
                R2_BUCKET if r2_key else None, r2_key, r2_total_bytes,
                duration, error_message,
                Jsonb(notes) if notes else None, run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Validation gate
# --------------------------------------------------------------------------- #


def validate_lei_parquet(parquet_path: Path) -> dict[str, float | int]:
    """Run the directive's §"Validation Gate" sanity checks against the
    just-written ``lei_records.parquet``. Returns a dict of stats; raises
    RuntimeError on failure.
    """
    pf = pq.ParquetFile(str(parquet_path))
    row_count = pf.metadata.num_rows
    if row_count < LEI_RECORDS_FLOOR:
        raise RuntimeError(
            f"lei_records: {row_count:,} rows < floor {LEI_RECORDS_FLOOR:,}"
        )

    # Column-level checks need a scan; load the whole table since the
    # file is well under 500 MB.
    tbl = pq.read_table(str(parquet_path), columns=[
        "lei", "legal_name_normalized",
    ])
    leis = tbl.column("lei").to_pylist()
    norms = tbl.column("legal_name_normalized").to_pylist()

    n = len(leis)
    bad_lei = sum(1 for v in leis if not v or len(v) != LEI_LENGTH)
    null_norm = sum(1 for v in norms if v is None or v == "")

    bad_lei_rate = bad_lei / n if n else 0.0
    null_norm_rate = null_norm / n if n else 0.0

    log.info(
        "  lei-validation rows=%d bad_lei=%d (%.4f%%) null_norm=%d (%.4f%%)",
        n, bad_lei, bad_lei_rate * 100, null_norm, null_norm_rate * 100,
    )

    failures: list[str] = []
    # Spec: > 99.5% of LEIs must be exactly 20 chars.
    if (1 - bad_lei_rate) <= 0.995:
        failures.append(
            f"LEI length=20 rate {(1 - bad_lei_rate):.4%} <= 99.5%"
        )
    # Spec: < 0.5% null legal_name_normalized.
    if null_norm_rate >= 0.005:
        failures.append(
            f"legal_name_normalized null rate {null_norm_rate:.4%} >= 0.5%"
        )

    if failures:
        raise RuntimeError(
            "lei_records validation failed: " + "; ".join(failures)
        )

    return {
        "row_count": row_count,
        "bad_lei_rate": bad_lei_rate,
        "null_norm_rate": null_norm_rate,
    }


def validate_rr_parquet(parquet_path: Path) -> dict[str, int]:
    pf = pq.ParquetFile(str(parquet_path))
    row_count = pf.metadata.num_rows
    if row_count < RR_RECORDS_FLOOR:
        raise RuntimeError(
            f"relationship_records: {row_count:,} rows < floor {RR_RECORDS_FLOOR:,}"
        )
    log.info("  rr-validation rows=%d", row_count)
    return {"row_count": row_count}


# --------------------------------------------------------------------------- #
# Per-file main
# --------------------------------------------------------------------------- #


def ingest_file(
    spec: FileSpec,
    snapshot: Snapshot,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_records: int | None,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[{snapshot.label} {spec.file_kind}]"
    started_wall = time.monotonic()
    source_url = snapshot.url_for(spec)
    log.info("%s start url=%s", log_prefix, source_url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/gleif-r2-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(client, source_url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1

        if status_code == 404:
            log.error("%s HEAD 404 — snapshot not published at expected URL", log_prefix)
            return 1

        floor = LEI_ZIP_MIN_BYTES if spec.file_kind == "lei2" else RR_ZIP_MIN_BYTES
        if content_length is not None and content_length < floor:
            log.error(
                "%s HEAD content-length %d < %d sanity floor — refusing",
                log_prefix, content_length, floor,
            )
            return 1

        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(
                conn, snapshot=snapshot, file_kind=spec.file_kind,
            )
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, snapshot=snapshot, file_kind=spec.file_kind,
                    source_url=source_url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, snapshot=snapshot, file_kind=spec.file_kind,
                source_url=source_url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            zip_path = workdir / f"gleif_{snapshot.label}_{spec.file_kind}.zip"
            extract_dir = workdir / f"gleif_{snapshot.label}_{spec.file_kind}_xml"
            extract_dir.mkdir(parents=True, exist_ok=True)
            parquet_path = workdir / f"gleif_{snapshot.label}_{spec.parquet_basename}"

            try:
                zip_bytes = download_zip(client, source_url, zip_path)
                log.info("%s downloaded %s bytes", log_prefix, f"{zip_bytes:,}")

                xml_path = extract_inner_xml(zip_path, extract_dir)
                log.info("%s extracted XML: %s (%.1f MB)",
                         log_prefix, xml_path.name,
                         xml_path.stat().st_size / (1 << 20))

                if spec.file_kind == "lei2":
                    schema = LEI_SCHEMA
                    record_iter = stream_lei_records(
                        xml_path, snapshot=snapshot, max_records=max_records,
                    )
                else:
                    schema = RR_SCHEMA
                    record_iter = stream_relationship_records(
                        xml_path, snapshot=snapshot, max_records=max_records,
                    )

                rows_yielded, parquet_row_count = write_records_to_parquet(
                    record_iter, parquet_path,
                    schema=schema, log_prefix=log_prefix,
                )

                if max_records is None:
                    if spec.file_kind == "lei2":
                        validate_lei_parquet(parquet_path)
                    else:
                        validate_rr_parquet(parquet_path)
                else:
                    log.info("%s   skipping validation gate — smoke run "
                             "(max_records=%d)", log_prefix, max_records)

                pq_bytes = parquet_path.stat().st_size
                r2_prefix = r2_prefix_override or snapshot.r2_prefix
                if not r2_prefix.endswith("/"):
                    r2_prefix = r2_prefix + "/"
                r2_key = r2_prefix + spec.parquet_basename
                uploaded_bytes = upload_to_r2(
                    parquet_path, key=r2_key, log_prefix=log_prefix,
                )

                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes_downloaded=zip_bytes,
                    xml_record_count=rows_yielded,
                    parquet_row_count=parquet_row_count,
                    parquet_bytes_written=pq_bytes,
                    r2_key=r2_key,
                    r2_total_bytes=uploaded_bytes,
                    started_at=started_wall, error_message=None,
                    notes={
                        "r2_key": r2_key,
                        "max_records": max_records,
                        "smoke_override": r2_prefix_override,
                        "xml_filename": xml_path.name,
                    },
                )
                log.info(
                    "%s DONE rows=%s parquet=%.1f MB upload=%.1f MB wall=%.1fs",
                    log_prefix, f"{parquet_row_count:,}",
                    pq_bytes / (1 << 20),
                    uploaded_bytes / (1 << 20),
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes_downloaded=0, xml_record_count=0,
                    parquet_row_count=0, parquet_bytes_written=0,
                    r2_key=None, r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    parquet_path.unlink(missing_ok=True)
                except Exception:
                    pass
                shutil.rmtree(extract_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_snapshot_arg(s: str | None) -> Snapshot:
    """Parse ``--snapshot YYYY-MM-DD`` or default to today UTC."""
    if s is None or s.lower() == "today":
        return Snapshot(snapshot_date=datetime.now(timezone.utc).date())
    if s.lower() == "yesterday":
        return Snapshot(
            snapshot_date=datetime.now(timezone.utc).date() - timedelta(days=1),
        )
    try:
        return Snapshot(snapshot_date=date.fromisoformat(s))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--snapshot must be YYYY-MM-DD (or 'today'/'yesterday'); got {s!r}"
        ) from exc


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", default=None,
                   help="Snapshot date YYYY-MM-DD. Default: today UTC.")
    p.add_argument("--file-kind", choices=["lei2", "rr", "both"], default="both",
                   help="Which file(s) to ingest. Default: both.")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Skip if HEAD Last-Modified unchanged from prior run.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD only; no download/transform/R2 writes.")
    p.add_argument("--max-records", type=int, default=None,
                   help="Cap records per file (smoke testing). Skips the "
                        "validation gate.")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP/XML/Parquet. "
                        "Default: /tmp/gleif_r2_ingest.")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override R2 prefix (e.g., 'gleif/_smoke/...'). "
                        "Use for smoke runs to keep canonical paths clean.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = parse_snapshot_arg(args.snapshot)
    workdir = Path(args.workdir or "/tmp/gleif_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.file_kind == "both":
        kinds = [LEI2, RR]
    else:
        kinds = [ALL_FILE_SPECS[args.file_kind]]

    log.info("=" * 70)
    log.info("=== GLEIF R2 INGEST: snapshot=%s kinds=%s ===",
             snapshot.label, [s.file_kind for s in kinds])
    log.info("=" * 70)

    # Run lei2 first (the bigger file, more likely to fail) so we don't
    # waste time on rr if lei2 is broken.
    rc = 0
    for spec in kinds:
        rc_one = ingest_file(
            spec, snapshot,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_records=args.max_records,
            r2_prefix_override=args.r2_prefix_override,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("%s failed; continuing", spec.file_kind)

    return rc


if __name__ == "__main__":
    sys.exit(main())
