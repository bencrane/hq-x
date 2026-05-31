#!/usr/bin/env python3
"""GLEIF Golden Copy — bulk-CSV ingest from goldencopy.gleif.org.

Two datasets per publish (3x daily). Source-first per CLAUDE.md (2026-04-16):
each dataset lands in its own entities.source_gleif_* table. No identity
resolution, no canonical merge. Repex (reporting exceptions) is intentionally
out of scope first pass.

  lei2   LEI2  -> entities.source_gleif_lei_records      (~3.3M rows)
  rr     RR    -> entities.source_gleif_relationships    (~472k rows)

Source URL discovery: hit the publishes API at
  https://goldencopy.gleif.org/api/v2/golden-copies/publishes?page[size]=1
which returns the latest publish with stable signed-style URLs:
  https://goldencopy.gleif.org/storage/golden-copy-files/{date}/{publish-id}/...
The publish-id rotates each run; the URL must be discovered, not hardcoded.

Subset: lei2 source has ~390 columns of mostly-null repeated arrays. The
script lands ~32 columns of practical value matching the migration schema.
rr source has 54 columns; we land 12.

Idempotency: PK=(lei) for lei2; (start_node_id, end_node_id, relationship_type)
for rr. ON CONFLICT DO UPDATE on PK.

Audit: ops.gleif_ingest_runs.
Skip-if-unchanged: compare publishes API publish_date to prior successful run.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_gleif_ingest.py lei2
  PYTHONPATH=. doppler run -- python3 scripts/run_gleif_ingest.py rr
  PYTHONPATH=. doppler run -- python3 scripts/run_gleif_ingest.py all
  PYTHONPATH=. doppler run -- python3 scripts/run_gleif_ingest.py all --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_gleif_ingest.py lei2 --dry-run
  PYTHONPATH=. doppler run -- python3 scripts/run_gleif_ingest.py all --recon-only
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


PUBLISHES_API_URL = (
    "https://goldencopy.gleif.org/api/v2/golden-copies/publishes?page%5Bsize%5D=1"
)

DEFAULT_BATCH_SIZE = 25_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# Avoid surprising memory blow-ups on the ~5 GB uncompressed lei2 CSV.
csv.field_size_limit(sys.maxsize)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("gleif-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Column maps — Postgres column → CSV header in the golden copy.
# Order matches the CREATE TABLE column order (excluding PK / audit columns).
# Several CSV headers contain dots (e.g. Entity.LegalAddress.City) — we keep
# the full header as the lookup key.
# --------------------------------------------------------------------------- #

LEI2_COL_MAP: list[tuple[str, str]] = [
    ("legal_name",                       "Entity.LegalName"),
    ("legal_name_lang",                  "Entity.LegalName.xmllang"),

    ("other_entity_name_1",              "Entity.OtherEntityNames.OtherEntityName.1"),
    ("other_entity_name_1_type",         "Entity.OtherEntityNames.OtherEntityName.1.type"),
    ("other_entity_name_2",              "Entity.OtherEntityNames.OtherEntityName.2"),
    ("other_entity_name_2_type",         "Entity.OtherEntityNames.OtherEntityName.2.type"),
    ("other_entity_name_3",              "Entity.OtherEntityNames.OtherEntityName.3"),
    ("other_entity_name_3_type",         "Entity.OtherEntityNames.OtherEntityName.3.type"),
    ("other_entity_name_4",              "Entity.OtherEntityNames.OtherEntityName.4"),
    ("other_entity_name_4_type",         "Entity.OtherEntityNames.OtherEntityName.4.type"),
    ("other_entity_name_5",              "Entity.OtherEntityNames.OtherEntityName.5"),
    ("other_entity_name_5_type",         "Entity.OtherEntityNames.OtherEntityName.5.type"),

    ("legal_form_code",                  "Entity.LegalForm.EntityLegalFormCode"),
    ("legal_form_other",                 "Entity.LegalForm.OtherLegalForm"),
    ("entity_category",                  "Entity.EntityCategory"),
    ("entity_subcategory",               "Entity.EntitySubCategory"),
    ("entity_status",                    "Entity.EntityStatus"),
    ("entity_creation_date",             "Entity.EntityCreationDate"),
    ("entity_expiration_date",           "Entity.EntityExpirationDate"),
    ("entity_expiration_reason",         "Entity.EntityExpirationReason"),
    ("legal_jurisdiction",               "Entity.LegalJurisdiction"),

    ("legal_address_line1",              "Entity.LegalAddress.FirstAddressLine"),
    ("legal_address_line2",              "Entity.LegalAddress.AdditionalAddressLine.1"),
    ("legal_address_city",               "Entity.LegalAddress.City"),
    ("legal_address_region",             "Entity.LegalAddress.Region"),
    ("legal_address_country",            "Entity.LegalAddress.Country"),
    ("legal_address_postal_code",        "Entity.LegalAddress.PostalCode"),

    ("hq_address_line1",                 "Entity.HeadquartersAddress.FirstAddressLine"),
    ("hq_address_line2",                 "Entity.HeadquartersAddress.AdditionalAddressLine.1"),
    ("hq_address_city",                  "Entity.HeadquartersAddress.City"),
    ("hq_address_region",                "Entity.HeadquartersAddress.Region"),
    ("hq_address_country",               "Entity.HeadquartersAddress.Country"),
    ("hq_address_postal_code",           "Entity.HeadquartersAddress.PostalCode"),

    ("registration_authority_id",        "Entity.RegistrationAuthority.RegistrationAuthorityID"),
    ("registration_authority_entity_id", "Entity.RegistrationAuthority.RegistrationAuthorityEntityID"),

    ("associated_lei",                   "Entity.AssociatedEntity.AssociatedLEI"),
    ("successor_lei",                    "Entity.SuccessorEntity.1.SuccessorLEI"),

    ("registration_status",              "Registration.RegistrationStatus"),
    ("registration_initial_date",        "Registration.InitialRegistrationDate"),
    ("registration_last_update",         "Registration.LastUpdateDate"),
    ("registration_next_renewal",        "Registration.NextRenewalDate"),
    ("managing_lou",                     "Registration.ManagingLOU"),
    ("conformity_flag",                  "ConformityFlag"),
]

RR_COL_MAP: list[tuple[str, str]] = [
    ("start_node_type",                  "Relationship.StartNode.NodeIDType"),
    ("end_node_type",                    "Relationship.EndNode.NodeIDType"),
    ("relationship_status",              "Relationship.RelationshipStatus"),
    ("period_start_date",                "Relationship.Period.1.startDate"),
    ("period_end_date",                  "Relationship.Period.1.endDate"),
    ("registration_status",              "Registration.RegistrationStatus"),
    ("registration_initial_date",        "Registration.InitialRegistrationDate"),
    ("registration_last_update",         "Registration.LastUpdateDate"),
    ("validation_sources",               "Registration.ValidationSources"),
]


# --------------------------------------------------------------------------- #
# Per-form configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormConfig:
    key: str                # CLI subcommand
    dataset_form: str       # Audit-table value
    schema: str             # 'entities'
    table: str
    pk_cols: tuple[str, ...]
    pk_csv_headers: tuple[str, ...]   # Source CSV header for each PK column
    extra_col_map: list[tuple[str, str]]  # Postgres col → CSV header (non-PK, non-audit)

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"

    def all_cols_with_csv(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for pk_col, pk_csv in zip(self.pk_cols, self.pk_csv_headers):
            out.append((pk_col, pk_csv))
        out.extend(self.extra_col_map)
        return out


LEI2_FORM = FormConfig(
    key="lei2",
    dataset_form="LEI2",
    schema="entities",
    table="source_gleif_lei_records",
    pk_cols=("lei",),
    pk_csv_headers=("LEI",),
    extra_col_map=LEI2_COL_MAP,
)

RR_FORM = FormConfig(
    key="rr",
    dataset_form="RR",
    schema="entities",
    table="source_gleif_relationships",
    pk_cols=("start_node_id", "end_node_id", "relationship_type"),
    pk_csv_headers=(
        "Relationship.StartNode.NodeID",
        "Relationship.EndNode.NodeID",
        "Relationship.RelationshipType",
    ),
    extra_col_map=RR_COL_MAP,
)

FORMS: dict[str, FormConfig] = {f.key: f for f in (LEI2_FORM, RR_FORM)}


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED is not set in the environment.")
    return url


# --------------------------------------------------------------------------- #
# Publishes API discovery
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PublishInfo:
    publish_date: str
    lei2_url: str
    lei2_record_count: int
    lei2_size: int
    rr_url: str
    rr_record_count: int
    rr_size: int


def fetch_latest_publish(client: httpx.Client) -> PublishInfo:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(PUBLISHES_API_URL, timeout=30.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("publishes API HTTP %s; retry in %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            payload = r.json()
            data = payload["data"][0]
            publish_date = data["publish_date"]
            lei2 = data["lei2"]["full_file"]["csv"]
            rr = data["rr"]["full_file"]["csv"]
            return PublishInfo(
                publish_date=publish_date,
                lei2_url=lei2["url"],
                lei2_record_count=int(lei2["record_count"]),
                lei2_size=int(lei2["size"]),
                rr_url=rr["url"],
                rr_record_count=int(rr["record_count"]),
                rr_size=int(rr["size"]),
            )
        except (httpx.RequestError, httpx.HTTPStatusError, KeyError,
                IndexError, ValueError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("publishes API error (%s); retry in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"publishes API failed: {last_exc}")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
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
                    lm = datetime.strptime(lm_raw, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            return cl, lm
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
            with client.stream("GET", url, follow_redirects=True, timeout=900.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss",
                                url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed: {last_exc}")


# --------------------------------------------------------------------------- #
# CSV → Postgres COPY pipeline
# --------------------------------------------------------------------------- #


def open_csv_in_zip(zip_path: Path) -> tuple[zipfile.ZipFile, io.TextIOWrapper, str]:
    z = zipfile.ZipFile(zip_path)
    target_name = None
    for name in z.namelist():
        if name.lower().endswith(".csv"):
            target_name = name
            break
    if target_name is None:
        z.close()
        raise RuntimeError(
            f"No CSV found in {zip_path.name}; contents: {z.namelist()}"
        )
    f = io.TextIOWrapper(z.open(target_name, "r"), encoding="utf-8", errors="replace", newline="")
    return z, f, target_name


def stage_create_sql(cfg: FormConfig) -> str:
    cols = list(cfg.pk_cols) + [c for c, _ in cfg.extra_col_map] + [
        "publish_date", "source_file_last_modified",
    ]
    body = ",\n  ".join(f"{c} text" for c in cols)
    return f"""
CREATE TEMP TABLE IF NOT EXISTS {cfg.stage_table} (
  {body}
);
"""


def truncate_stage_sql(cfg: FormConfig) -> str:
    return f"TRUNCATE {cfg.stage_table};"


def copy_sql(cfg: FormConfig) -> str:
    cols = list(cfg.pk_cols) + [c for c, _ in cfg.extra_col_map] + [
        "publish_date", "source_file_last_modified",
    ]
    return f"COPY {cfg.stage_table} ({', '.join(cols)}) FROM STDIN"


def upsert_from_stage_sql(cfg: FormConfig) -> str:
    natural_cols = [c for c, _ in cfg.extra_col_map]
    target_cols = (
        list(cfg.pk_cols)
        + natural_cols
        + ["publish_date", "source_file_last_modified", "ingested_at"]
    )
    select_cols = (
        list(cfg.pk_cols)
        + natural_cols
        + ["publish_date", "source_file_last_modified::timestamptz", "now()"]
    )
    pk = ", ".join(cfg.pk_cols)
    update_cols = natural_cols + ["publish_date", "source_file_last_modified"]
    update_assigns = ",\n      ".join(
        f"{c} = EXCLUDED.{c}" for c in update_cols
    ) + ",\n      ingested_at = now()"
    where_clause = " OR ".join(
        f"{cfg.fully_qualified}.{c} IS DISTINCT FROM EXCLUDED.{c}"
        for c in update_cols
    )
    pk_filter = " AND ".join(f"{c} IS NOT NULL AND {c} <> ''" for c in cfg.pk_cols)
    return f"""
WITH upserted AS (
  INSERT INTO {cfg.fully_qualified} ({', '.join(target_cols)})
  SELECT {', '.join(select_cols)}
    FROM {cfg.stage_table}
   WHERE {pk_filter}
   ON CONFLICT ({pk}) DO UPDATE SET
      {update_assigns}
   WHERE {where_clause}
   RETURNING (xmax = 0) AS inserted
)
SELECT
  count(*) FILTER (WHERE inserted)     AS rows_inserted,
  count(*) FILTER (WHERE NOT inserted) AS rows_updated
FROM upserted;
"""


def copy_chunk_to_stage(
    conn: psycopg.Connection,
    cfg: FormConfig,
    rows: list[tuple[Any, ...]],
) -> tuple[int, int]:
    if not rows:
        return 0, 0
    with conn.cursor() as cur:
        cur.execute(truncate_stage_sql(cfg))
        with cur.copy(copy_sql(cfg)) as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute(upsert_from_stage_sql(cfg))
        ins, upd = cur.fetchone()
    conn.commit()
    return int(ins), int(upd)


def stream_csv_to_db(
    conn: psycopg.Connection,
    cfg: FormConfig,
    csv_fh: io.TextIOWrapper,
    *,
    publish_date: str,
    source_file_last_modified: datetime | None,
    batch_size: int,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, int]:
    reader = csv.reader(csv_fh)
    try:
        header = next(reader)
    except StopIteration:
        return 0, 0, 0

    idx_by_name = {name: i for i, name in enumerate(header)}

    pk_indexes: list[int] = []
    missing_pk_headers: list[str] = []
    for csv_h in cfg.pk_csv_headers:
        if csv_h not in idx_by_name:
            missing_pk_headers.append(csv_h)
        else:
            pk_indexes.append(idx_by_name[csv_h])
    if missing_pk_headers:
        raise RuntimeError(
            f"{log_prefix} CSV missing required PK header(s): {missing_pk_headers}; "
            f"first 10 headers seen: {header[:10]}"
        )

    extra_indexes: list[int | None] = []
    missing_extras: list[str] = []
    for _, csv_h in cfg.extra_col_map:
        i = idx_by_name.get(csv_h)
        extra_indexes.append(i)
        if i is None:
            missing_extras.append(csv_h)
    if missing_extras:
        log.warning("%s CSV missing %d expected non-PK header(s); will leave NULL: %s",
                    log_prefix, len(missing_extras), missing_extras[:5])

    last_mod_str = source_file_last_modified.isoformat() if source_file_last_modified else None

    rows_seen = total_inserted = total_updated = 0
    chunk: list[tuple[Any, ...]] = []
    page_started = time.monotonic()
    progress_every = max(batch_size * 4, 100_000)
    last_progress = 0
    for raw in reader:
        rows_seen += 1
        if max_rows is not None and rows_seen > max_rows:
            log.info("%s --max-rows %d reached, stopping read", log_prefix, max_rows)
            break

        bad = False
        pk_values: list[str] = []
        for idx in pk_indexes:
            if idx >= len(raw):
                bad = True
                break
            v = raw[idx]
            if v is None or v == "":
                bad = True
                break
            pk_values.append(v)
        if bad:
            continue

        out: list[Any] = list(pk_values)
        for idx in extra_indexes:
            if idx is None or idx >= len(raw):
                out.append(None)
                continue
            v = raw[idx]
            out.append(None if v == "" else v)
        out.append(publish_date)
        out.append(last_mod_str)
        chunk.append(tuple(out))

        if len(chunk) >= batch_size:
            ins, upd = copy_chunk_to_stage(conn, cfg, chunk)
            total_inserted += ins
            total_updated += upd
            if rows_seen - last_progress >= progress_every:
                log.info(
                    "%s progress: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
                    log_prefix, rows_seen, ins, upd,
                    total_inserted, total_updated,
                    time.monotonic() - page_started,
                )
                last_progress = rows_seen
                page_started = time.monotonic()
            chunk.clear()
    if chunk:
        ins, upd = copy_chunk_to_stage(conn, cfg, chunk)
        total_inserted += ins
        total_updated += upd
        log.info(
            "%s final chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
            log_prefix, rows_seen, ins, upd,
            total_inserted, total_updated,
            time.monotonic() - page_started,
        )
    return total_inserted, total_updated, rows_seen


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    cfg: FormConfig,
    *,
    url: str,
    publish_date: str,
    source_last_modified: datetime | None,
    prior_publish_date: str | None,
) -> str:
    sql = """
    INSERT INTO ops.gleif_ingest_runs (
        dataset_form, publish_date, status, source_url,
        source_last_modified, prior_publish_date
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cfg.dataset_form, publish_date, url,
            source_last_modified, prior_publish_date,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_publish_date(
    conn: psycopg.Connection, cfg: FormConfig
) -> str | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT publish_date
              FROM ops.gleif_ingest_runs
             WHERE dataset_form = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (cfg.dataset_form,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    cfg: FormConfig,
    *,
    url: str,
    publish_date: str,
    prior_publish_date: str | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.gleif_ingest_runs (
                dataset_form, publish_date, status, source_url,
                prior_publish_date,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, 0, %s);
            """,
            (
                cfg.dataset_form, publish_date, url,
                prior_publish_date, started, started,
                Jsonb({"reason": "publish_date unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes: int,
    csv_bytes: int,
    rows_in_csv: int,
    rows_inserted: int,
    rows_updated: int,
    rows_unchanged: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.gleif_ingest_runs
               SET status = %s, zip_bytes_downloaded = %s,
                   csv_bytes_extracted = %s, rows_in_csv = %s,
                   rows_inserted = %s, rows_updated = %s, rows_unchanged = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, rows_in_csv,
            rows_inserted, rows_updated, rows_unchanged,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Recon report
# --------------------------------------------------------------------------- #


@dataclass
class ReconStats:
    form_key: str
    table_fqn: str
    total_rows: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


def gather_recon_lei2(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="lei2", table_fqn=LEI2_FORM.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {LEI2_FORM.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE legal_address_country = 'US'),
              count(*) FILTER (WHERE entity_status = 'ACTIVE'),
              count(*) FILTER (WHERE registration_status = 'ISSUED'),
              count(DISTINCT legal_jurisdiction),
              count(DISTINCT entity_category),
              count(DISTINCT legal_form_code) FILTER (WHERE legal_form_code IS NOT NULL)
              FROM {LEI2_FORM.fully_qualified};
        """)
        us, active, issued, n_juris, n_cat, n_lf = cur.fetchone()
        s.notes["us_legal_address_rows"] = int(us)
        s.notes["entity_status_active_rows"] = int(active)
        s.notes["registration_status_issued_rows"] = int(issued)
        s.notes["distinct_legal_jurisdictions"] = int(n_juris)
        s.notes["distinct_entity_categories"] = int(n_cat)
        s.notes["distinct_legal_form_codes"] = int(n_lf)
        cur.execute(f"""
            SELECT entity_category, count(*) c
              FROM {LEI2_FORM.fully_qualified}
             WHERE entity_category IS NOT NULL
             GROUP BY entity_category ORDER BY c DESC;
        """)
        s.notes["entity_category_distribution"] = [
            {"category": r[0], "rows": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT registration_status, count(*) c
              FROM {LEI2_FORM.fully_qualified}
             WHERE registration_status IS NOT NULL
             GROUP BY registration_status ORDER BY c DESC;
        """)
        s.notes["registration_status_distribution"] = [
            {"status": r[0], "rows": int(r[1])} for r in cur.fetchall()
        ]
    return s


def gather_recon_rr(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="rr", table_fqn=RR_FORM.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {RR_FORM.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(f"""
            SELECT relationship_type, count(*) c
              FROM {RR_FORM.fully_qualified}
             GROUP BY relationship_type ORDER BY c DESC;
        """)
        s.notes["relationship_type_distribution"] = [
            {"type": r[0], "rows": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT relationship_status, count(*) c
              FROM {RR_FORM.fully_qualified}
             WHERE relationship_status IS NOT NULL
             GROUP BY relationship_status ORDER BY c DESC;
        """)
        s.notes["relationship_status_distribution"] = [
            {"status": r[0], "rows": int(r[1])} for r in cur.fetchall()
        ]
    return s


def gather_recon_hmda_gleif_join(conn: psycopg.Connection) -> ReconStats:
    """HMDA TS LEI ↔ GLEIF lei2 — exact LEI match. No fuzzy matching, no
    persisted match table needed (deterministic, replayable from source)."""
    s = ReconStats(form_key="hmda_gleif_lei_exact",
                   table_fqn="(JOIN entities.source_hmda_transmittal_sheet ↔ entities.source_gleif_lei_records)")
    with conn.cursor() as cur:
        # Existence check
        cur.execute("""
            SELECT
              (SELECT count(*) FROM entities.source_hmda_transmittal_sheet) AS hmda_ts_rows,
              (SELECT count(DISTINCT lei) FROM entities.source_hmda_transmittal_sheet) AS hmda_ts_distinct_lei,
              (SELECT count(*) FROM entities.source_gleif_lei_records)      AS gleif_rows;
        """)
        hmda_rows, hmda_lei, gleif_rows = cur.fetchone()
        s.total_rows = int(hmda_lei)
        s.notes["hmda_ts_rows"] = int(hmda_rows)
        s.notes["hmda_ts_distinct_lei"] = int(hmda_lei)
        s.notes["gleif_lei_records"] = int(gleif_rows)
        if hmda_lei == 0 or gleif_rows == 0:
            return s

        cur.execute("""
            SELECT
              count(DISTINCT ts.lei),
              count(DISTINCT ts.lei) FILTER (WHERE g.lei IS NOT NULL),
              round(100.0 * count(DISTINCT ts.lei) FILTER (WHERE g.lei IS NOT NULL)
                    / NULLIF(count(DISTINCT ts.lei), 0), 2) AS pct
              FROM entities.source_hmda_transmittal_sheet ts
              LEFT JOIN entities.source_gleif_lei_records g ON g.lei = ts.lei;
        """)
        d_lei, matched, pct = cur.fetchone()
        s.notes["hmda_lei_in_gleif"] = int(matched)
        s.notes["hmda_lei_in_gleif_pct"] = float(pct or 0)

        cur.execute("""
            SELECT g.entity_category, count(DISTINCT ts.lei) c
              FROM entities.source_hmda_transmittal_sheet ts
              JOIN entities.source_gleif_lei_records g ON g.lei = ts.lei
             WHERE g.entity_category IS NOT NULL
             GROUP BY g.entity_category ORDER BY c DESC;
        """)
        s.notes["hmda_lender_entity_category"] = [
            {"category": r[0], "distinct_lei": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT g.legal_form_code, count(DISTINCT ts.lei) c
              FROM entities.source_hmda_transmittal_sheet ts
              JOIN entities.source_gleif_lei_records g ON g.lei = ts.lei
             WHERE g.legal_form_code IS NOT NULL
             GROUP BY g.legal_form_code ORDER BY c DESC LIMIT 10;
        """)
        s.notes["hmda_lender_legal_form_top10"] = [
            {"elf_code": r[0], "distinct_lei": int(r[1])} for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT g.legal_jurisdiction, count(DISTINCT ts.lei) c
              FROM entities.source_hmda_transmittal_sheet ts
              JOIN entities.source_gleif_lei_records g ON g.lei = ts.lei
             WHERE g.legal_jurisdiction IS NOT NULL
             GROUP BY g.legal_jurisdiction ORDER BY c DESC LIMIT 10;
        """)
        s.notes["hmda_lender_jurisdiction_top10"] = [
            {"jurisdiction": r[0], "distinct_lei": int(r[1])} for r in cur.fetchall()
        ]
        # HMDA filers' parent linkage via rr
        cur.execute("""
            SELECT
              count(DISTINCT ts.lei) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM entities.source_gleif_relationships rr
                   WHERE rr.start_node_id = ts.lei
                     AND rr.relationship_type = 'IS_DIRECTLY_CONSOLIDATED_BY'
                     AND rr.relationship_status = 'ACTIVE')) AS with_direct_parent,
              count(DISTINCT ts.lei) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM entities.source_gleif_relationships rr
                   WHERE rr.start_node_id = ts.lei
                     AND rr.relationship_type = 'IS_ULTIMATELY_CONSOLIDATED_BY'
                     AND rr.relationship_status = 'ACTIVE')) AS with_ultimate_parent
              FROM entities.source_hmda_transmittal_sheet ts;
        """)
        direct, ultimate = cur.fetchone()
        s.notes["hmda_lei_with_active_direct_parent"] = int(direct)
        s.notes["hmda_lei_with_active_ultimate_parent"] = int(ultimate)
    return s


def gather_recon_hmda_finra_count_only(conn: psycopg.Connection) -> ReconStats:
    """Count-only HMDA respondent-name ↔ FINRA firm-name overlap. NO persisted
    match table — this is a recon stat only. A persistent fuzzy-match MV would
    follow supabase/migrations/096_mv_pdl_to_sam_name_state_matches.sql with
    match_score / confidence_tier / match_reasons[] per row."""
    s = ReconStats(
        form_key="hmda_finra_name_overlap_recon_only",
        table_fqn="(no persisted match table — recon counts only)",
    )
    with conn.cursor() as cur:
        # Skip if FINRA table absent
        cur.execute("""
            SELECT to_regclass('entities.source_finra_brokercheck_firms') IS NOT NULL;
        """)
        if not cur.fetchone()[0]:
            s.notes["status"] = "skipped: entities.source_finra_brokercheck_firms not present"
            return s

        # 2024 HMDA only (most recent universe)
        # Match basis: simple lowercased name equality + state on the latest TS year.
        # This is intentionally minimal — a real fuzzy matcher would normalize
        # via pdl.normalize_company_name() and stamp match_score etc.
        cur.execute("""
            WITH ts AS (
              SELECT lei, lower(respondent_name) AS name_lc, respondent_state
                FROM entities.source_hmda_transmittal_sheet
               WHERE dataset_year = 2024
            ), finra AS (
              SELECT crd_number, lower(firm_name) AS name_lc
                FROM entities.source_finra_brokercheck_firms
               WHERE firm_name IS NOT NULL
            )
            SELECT
              (SELECT count(*) FROM ts) AS hmda_2024_rows,
              (SELECT count(*) FROM finra) AS finra_rows,
              (SELECT count(DISTINCT ts.lei)
                 FROM ts JOIN finra USING (name_lc)) AS exact_name_overlap_distinct_lei,
              (SELECT count(DISTINCT ts.lei)
                 FROM ts
                WHERE EXISTS (
                  SELECT 1 FROM finra
                   WHERE finra.name_lc LIKE ts.name_lc || '%'
                      OR ts.name_lc LIKE finra.name_lc || '%')) AS prefix_either_overlap;
        """)
        ts_rows, finra_rows, exact, prefix = cur.fetchone()
        s.total_rows = int(exact)
        s.notes["hmda_ts_2024_rows"] = int(ts_rows)
        s.notes["finra_brokercheck_rows"] = int(finra_rows)
        s.notes["exact_lowercased_name_match_distinct_lei"] = int(exact)
        s.notes["prefix_either_match_distinct_lei"] = int(prefix)
        s.notes["match_quality_persistence"] = (
            "RECON ONLY — no rows persisted. A persistent fuzzy-match MV must "
            "follow supabase/migrations/096_mv_pdl_to_sam_name_state_matches.sql: "
            "every result row carries match_score (numeric), confidence_tier "
            "('high'|'medium'|'low'), match_reasons (text[] with tags like "
            "'name_normalized_match','state_match','locality_match'). Use "
            "pdl.normalize_company_name() for both sides."
        )
    return s


def print_recon(s: ReconStats) -> None:
    print(f"=== RECON: {s.form_key}  ({s.table_fqn}) ===")
    print(f"  total rows: {s.total_rows:,}")
    for k, v in s.notes.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for kk, vv in v.items():
                if isinstance(vv, int):
                    print(f"      {kk}: {vv:,}")
                else:
                    print(f"      {kk}: {vv}")
        elif isinstance(v, list):
            print(f"  {k}:")
            for item in v:
                print(f"      {item}")
        elif isinstance(v, int):
            print(f"  {k}: {v:,}")
        elif isinstance(v, float):
            print(f"  {k}: {v:,.2f}")
        else:
            print(f"  {k}: {v}")
    print(f"=== END RECON ===\n")


# --------------------------------------------------------------------------- #
# Per-form main
# --------------------------------------------------------------------------- #


def ensure_stage_table(conn: psycopg.Connection, cfg: FormConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(stage_create_sql(cfg))
    conn.commit()


def ingest_one(
    cfg: FormConfig,
    *,
    publish: PublishInfo,
    batch_size: int,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
) -> int:
    if cfg is LEI2_FORM:
        url = publish.lei2_url
        expected_records = publish.lei2_record_count
    elif cfg is RR_FORM:
        url = publish.rr_url
        expected_records = publish.rr_record_count
    else:
        raise RuntimeError(f"Unknown form: {cfg.key}")

    log_prefix = f"[{cfg.key} pub={publish.publish_date}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s expected_records=%d", log_prefix, url, expected_records)

    with httpx.Client(headers={"User-Agent": "data-engine-x/gleif-ingest"}) as client:
        try:
            content_length, source_last_modified = head_url(client, url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)

        if dry_run:
            log.info("%s DRY RUN — fetching ZIP and inspecting CSV header only", log_prefix)
            zip_path = workdir / f"gleif_{cfg.key}.zip"
            zip_bytes = download_zip(client, url, zip_path)
            log.info("%s downloaded %d bytes", log_prefix, zip_bytes)
            try:
                z, fh, name = open_csv_in_zip(zip_path)
                with z, fh:
                    header_line = fh.readline()
                    sample = fh.readline()
                    cols = header_line.rstrip("\n").split(",")
                    log.info("%s CSV name=%s cols=%d header_first8=%s sample_prefix=%s",
                             log_prefix, name, len(cols), cols[:8],
                             sample[:200].rstrip())
            finally:
                zip_path.unlink(missing_ok=True)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_publish_date(conn, cfg)
            log.info("%s prior publish_date: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and prior >= publish.publish_date
            ):
                log.info("%s publish_date unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, cfg, url=url,
                    publish_date=publish.publish_date,
                    prior_publish_date=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, cfg, url=url,
                publish_date=publish.publish_date,
                source_last_modified=source_last_modified,
                prior_publish_date=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)
            ensure_stage_table(conn, cfg)

            zip_path = workdir / f"gleif_{cfg.key}.zip"
            try:
                zip_bytes = download_zip(client, url, zip_path)
                log.info("%s downloaded %d bytes -> %s", log_prefix, zip_bytes, zip_path)

                z, fh, csv_name = open_csv_in_zip(zip_path)
                with z, fh:
                    csv_bytes = z.getinfo(csv_name).file_size
                    log.info("%s extracting %s (%d bytes uncompressed)",
                             log_prefix, csv_name, csv_bytes)
                    ins, upd, rows_seen = stream_csv_to_db(
                        conn, cfg, fh,
                        publish_date=publish.publish_date,
                        source_file_last_modified=source_last_modified,
                        batch_size=batch_size,
                        log_prefix=log_prefix,
                        max_rows=max_rows,
                    )

                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes, csv_bytes=csv_bytes,
                    rows_in_csv=rows_seen,
                    rows_inserted=ins, rows_updated=upd,
                    rows_unchanged=max(0, rows_seen - ins - upd),
                    started_at=started_wall, error_message=None,
                    notes={"expected_records": expected_records},
                )
                log.info(
                    "%s DONE rows_in_csv=%d ins=%d upd=%d unch=%d wall=%.1fs",
                    log_prefix, rows_seen, ins, upd,
                    max(0, rows_seen - ins - upd),
                    time.monotonic() - started_wall,
                )
                return 0
            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes=0, csv_bytes=0, rows_in_csv=0,
                    rows_inserted=0, rows_updated=0, rows_unchanged=0,
                    started_at=started_wall, error_message=str(exc), notes=None,
                )
                return 1
            finally:
                zip_path.unlink(missing_ok=True)


def run_recon_only() -> None:
    with psycopg.connect(_database_url()) as conn:
        for fn in (
            gather_recon_lei2,
            gather_recon_rr,
            gather_recon_hmda_gleif_join,
            gather_recon_hmda_finra_count_only,
        ):
            try:
                s = fn(conn)
                print_recon(s)
            except psycopg.errors.UndefinedTable as exc:
                log.error("Table missing — apply the migration first: %s", exc)
                return


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("form", choices=list(FORMS.keys()) + ["all"],
                   help="Form key (lei2, rr) or 'all'.")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help="Rows per COPY chunk (default: 25000).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if publish_date has not advanced "
                        "since the prior successful run.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD + download + read CSV header only; no DB writes.")
    p.add_argument("--recon-only", action="store_true",
                   help="Run recon SELECTs against existing table contents and exit.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows read per CSV (smoke testing only).")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP downloads (default: /tmp/gleif_ingest).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.recon_only:
        run_recon_only()
        return 0

    forms = list(FORMS.values()) if args.form == "all" else [FORMS[args.form]]

    workdir = Path(args.workdir or "/tmp/gleif_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(headers={"User-Agent": "data-engine-x/gleif-ingest"}) as client:
        publish = fetch_latest_publish(client)
    log.info(
        "publishes API: publish_date=%s lei2=%s lei2_records=%d rr=%s rr_records=%d",
        publish.publish_date, publish.lei2_url, publish.lei2_record_count,
        publish.rr_url, publish.rr_record_count,
    )

    rc = 0
    for cfg in forms:
        ds_rc = ingest_one(
            cfg,
            publish=publish,
            batch_size=args.batch_size,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_rows=args.max_rows,
        )
        rc = rc or ds_rc

    if not args.dry_run:
        run_recon_only()
    return rc


if __name__ == "__main__":
    sys.exit(main())
