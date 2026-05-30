#!/usr/bin/env python3
"""ProPublica Nonprofit Explorer 990 — per-filing ingest.

Walks the ProPublica API v2 (/search.json + /organizations/{ein}.json)
and lands one row per filing into entities.source_propublica_nonprofits.
Source-first: 1:1 column mirror of every key in the API response (32 org
fields + up to 87 filing-level fields), raw_source_row jsonb preserved.

PK is composite (ein, tax_prd, formtype) — ProPublica has no dedicated
per-filing record ID; (ein, tax_prd, formtype) is unique within the API.

Idempotency: ON CONFLICT (ein, tax_prd, formtype) DO UPDATE SET ...
Audit: ops.propublica_nonprofit_ingest_runs.

Usage:
  # Smoke test with fixture (single org JSON, 2 filings):
  PYTHONPATH=. doppler run -- python3 scripts/run_propublica_nonprofit_ingest.py \\
      --fixture tests/fixtures/propublica_nonprofit_smoke.json

  # Live: all orgs (paginate /search.json; very slow — use state/ntee filters):
  PYTHONPATH=. doppler run -- python3 scripts/run_propublica_nonprofit_ingest.py \\
      --state NY --ntee A

  PYTHONPATH=. doppler run -- python3 scripts/run_propublica_nonprofit_ingest.py \\
      --ein 142007220

  PYTHONPATH=. doppler run -- python3 scripts/run_propublica_nonprofit_ingest.py \\
      --state NY --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
import psycopg
from psycopg.types.json import Jsonb


PROPUBLICA_API = "https://projects.propublica.org/nonprofits/api/v2/"
DEFAULT_BATCH_SIZE = 1_000   # API rows; each row is a filing JSON
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("propublica-nonprofit-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED not set — run via doppler run --")
    return url


def _safe(val: Any) -> Any:
    """Return None for empty string / missing key."""
    if val is None:
        return None
    if isinstance(val, str):
        return val.strip() if val.strip() else None
    return val


# Filing-level fields that are numeric in the API response.
FILING_NUMERIC_KEYS = {
    "totrevenue", "totfuncexpns", "totassetsend", "totliabend",
    "pct_compnsatncurrofcr", "totcntrbs", "prgmservrev", "duesassesmnts",
    "othrinvstinc", "grsamtsalesastothr", "basisalesexpnsothr",
    "gnsaleofastothr", "grsincgaming", "grsrevnuefndrsng", "direxpns",
    "netincfndrsng", "grsalesminusret", "costgoodsold", "grsprft",
    "othrevnue", "totrevnue", "totexpns", "totexcessyr",
    "othrchgsnetassetfnd", "totnetassetsend", "unrelbusincd",
    "initiationfee", "initiationfees", "grspublicrcpts",
    "grsrcptspublicuse", "grsincmembers", "grsincother",
    "totcntrbgfts", "totprgmrevnue", "invstmntinc", "txexmptbndsproceeds",
    "royaltsinc", "grsrntsreal", "grsrntsprsnl", "rntlexpnsreal",
    "rntlexpnsprsnl", "rntlincreal", "rntlincprsnl", "netrntlinc",
    "grsalesecur", "grsalesothr", "cstbasisecur", "cstbasisothr",
    "gnlsecur", "gnlsothr", "netgnls", "grsincfndrsng",
    "lessdirfndrsng", "lessdirgaming", "netincgaming",
    "grsalesinvent", "lesscstofgoods", "netincsales", "miscrevtot11e",
    "compnsatncurrofcr", "othrsalwages", "payrolltx", "profndraising",
    "txexmptbndsend", "secrdmrtgsend", "unsecurednotesend",
    "retainedearnend", "totnetassetend", "nonpfrea",
    "gftgrntrcvd170", "gftgrntsrcvd170", "txrevnuelevied170",
    "srvcsval170", "grsinc170", "grsrcptsrelatd170", "grsrcptsrelated170",
    "totgftgrntrcvd509", "grsrcptsadmiss509", "grsrcptsadmissn509",
    "txrevnuelevied509", "srvcsval509", "subtotsuppinc509", "totsupp509",
}

ORG_NUMERIC_KEYS = {"asset_amount", "income_amount", "revenue_amount"}


def _build_row(
    org: dict[str, Any],
    filing: dict[str, Any],
    *,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime,
    source_run_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build a DB row dict from the API's org + filing objects."""
    row: dict[str, Any] = {}

    # PK
    row["ein"] = str(filing.get("ein") or org.get("ein") or "").strip() or None
    row["tax_prd"] = _safe(filing.get("tax_prd"))
    raw_formtype = filing.get("formtype")
    try:
        row["formtype"] = int(raw_formtype) if raw_formtype is not None else None
    except (TypeError, ValueError):
        row["formtype"] = None

    # Filing-level fields
    for key in (
        "tax_prd_yr", "pdf_url", "updated", "tax_pd", "subseccd",
        "unrelbusinccd",
    ):
        row[key] = _safe(filing.get(key))

    # tax_prd_yr as smallint
    if row["tax_prd_yr"] is not None:
        try:
            row["tax_prd_yr"] = int(row["tax_prd_yr"])
        except (TypeError, ValueError):
            row["tax_prd_yr"] = None

    for key in FILING_NUMERIC_KEYS:
        val = filing.get(key)
        row[key] = None if val is None else val

    # Organization fields (prefixed org_*)
    row["org_id"] = org.get("id")
    row["org_strein"] = _safe(org.get("strein"))
    row["org_name"] = _safe(org.get("name"))
    row["org_sub_name"] = _safe(org.get("sub_name"))
    row["org_careofname"] = _safe(org.get("careofname"))
    row["org_address"] = _safe(org.get("address"))
    row["org_city"] = _safe(org.get("city"))
    row["org_state"] = _safe(org.get("state"))
    row["org_zipcode"] = _safe(org.get("zipcode"))
    row["org_exemption_number"] = _safe(org.get("exemption_number"))
    row["org_subsection_code"] = _safe(org.get("subsection_code"))
    row["org_affiliation_code"] = _safe(org.get("affiliation_code"))
    row["org_classification_codes"] = _safe(org.get("classification_codes"))
    row["org_ruling_date"] = _safe(org.get("ruling_date"))
    row["org_deductibility_code"] = _safe(org.get("deductibility_code"))
    row["org_foundation_code"] = _safe(org.get("foundation_code"))
    row["org_activity_codes"] = _safe(org.get("activity_codes"))
    row["org_organization_code"] = _safe(org.get("organization_code"))
    row["org_exempt_organization_status_code"] = _safe(
        org.get("exempt_organization_status_code")
    )
    row["org_tax_period"] = _safe(org.get("tax_period"))
    row["org_asset_code"] = _safe(org.get("asset_code"))
    row["org_income_code"] = _safe(org.get("income_code"))
    row["org_filing_requirement_code"] = _safe(org.get("filing_requirement_code"))
    row["org_pf_filing_requirement_code"] = _safe(org.get("pf_filing_requirement_code"))
    row["org_accounting_period"] = _safe(org.get("accounting_period"))
    row["org_asset_amount"] = org.get("asset_amount")
    row["org_income_amount"] = org.get("income_amount")
    row["org_revenue_amount"] = org.get("revenue_amount")
    row["org_ntee_code"] = _safe(org.get("ntee_code"))
    row["org_sort_name"] = _safe(org.get("sort_name"))
    row["org_created_at"] = _safe(org.get("created_at"))
    row["org_updated_at"] = _safe(org.get("updated_at"))
    row["org_data_source"] = _safe(org.get("data_source"))
    row["org_have_extracts"] = org.get("have_extracts")
    row["org_have_pdfs"] = org.get("have_pdfs")
    row["org_latest_object_id"] = org.get("latest_object_id")

    # Provenance
    row["raw_source_row"] = Jsonb({"organization": org, "filing": filing})
    row["source_provider"] = "propublica_nonprofit_explorer"
    row["source_filename"] = source_filename
    row["source_download_url"] = source_download_url
    row["source_observed_at"] = source_observed_at
    row["source_run_metadata"] = Jsonb(source_run_metadata)
    row["source_task_id"] = os.environ.get("TRIGGER_TASK_ID")
    row["source_schedule_id"] = os.environ.get("TRIGGER_SCHEDULE_ID")

    return row


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #

_ALL_COLS = [
    "ein", "tax_prd", "formtype",
    "tax_prd_yr", "pdf_url", "updated", "totrevenue", "totfuncexpns",
    "totassetsend", "totliabend", "pct_compnsatncurrofcr", "tax_pd",
    "subseccd", "totcntrbs", "prgmservrev", "duesassesmnts", "othrinvstinc",
    "grsamtsalesastothr", "basisalesexpnsothr", "gnsaleofastothr",
    "grsincgaming", "grsrevnuefndrsng", "direxpns", "netincfndrsng",
    "grsalesminusret", "costgoodsold", "grsprft", "othrevnue", "totrevnue",
    "totexpns", "totexcessyr", "othrchgsnetassetfnd", "totnetassetsend",
    "unrelbusincd", "unrelbusinccd", "initiationfee", "initiationfees",
    "grspublicrcpts", "grsrcptspublicuse", "grsincmembers", "grsincother",
    "totcntrbgfts", "totprgmrevnue", "invstmntinc", "txexmptbndsproceeds",
    "royaltsinc", "grsrntsreal", "grsrntsprsnl", "rntlexpnsreal",
    "rntlexpnsprsnl", "rntlincreal", "rntlincprsnl", "netrntlinc",
    "grsalesecur", "grsalesothr", "cstbasisecur", "cstbasisothr",
    "gnlsecur", "gnlsothr", "netgnls", "grsincfndrsng", "lessdirfndrsng",
    "lessdirgaming", "netincgaming", "grsalesinvent", "lesscstofgoods",
    "netincsales", "miscrevtot11e", "compnsatncurrofcr", "othrsalwages",
    "payrolltx", "profndraising", "txexmptbndsend", "secrdmrtgsend",
    "unsecurednotesend", "retainedearnend", "totnetassetend", "nonpfrea",
    "gftgrntrcvd170", "gftgrntsrcvd170", "txrevnuelevied170", "srvcsval170",
    "grsinc170", "grsrcptsrelatd170", "grsrcptsrelated170",
    "totgftgrntrcvd509", "grsrcptsadmiss509", "grsrcptsadmissn509",
    "txrevnuelevied509", "srvcsval509", "subtotsuppinc509", "totsupp509",
    "org_id", "org_strein", "org_name", "org_sub_name", "org_careofname",
    "org_address", "org_city", "org_state", "org_zipcode",
    "org_exemption_number", "org_subsection_code", "org_affiliation_code",
    "org_classification_codes", "org_ruling_date", "org_deductibility_code",
    "org_foundation_code", "org_activity_codes", "org_organization_code",
    "org_exempt_organization_status_code", "org_tax_period",
    "org_asset_code", "org_income_code", "org_filing_requirement_code",
    "org_pf_filing_requirement_code", "org_accounting_period",
    "org_asset_amount", "org_income_amount", "org_revenue_amount",
    "org_ntee_code", "org_sort_name", "org_created_at", "org_updated_at",
    "org_data_source", "org_have_extracts", "org_have_pdfs",
    "org_latest_object_id",
    "raw_source_row", "source_provider", "source_filename",
    "source_download_url", "source_observed_at", "source_run_metadata",
    "source_task_id", "source_schedule_id",
]

_PK_COLS = {"ein", "tax_prd", "formtype"}
_UPDATE_COLS = [c for c in _ALL_COLS if c not in _PK_COLS]

_INSERT_COLS_SQL = ", ".join(_ALL_COLS)
_UPDATE_SQL = ",\n    ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATE_COLS)

_UPSERT_SQL = f"""
INSERT INTO entities.source_propublica_nonprofits ({_INSERT_COLS_SQL})
SELECT {_INSERT_COLS_SQL} FROM _stage_source_propublica_nonprofits
ON CONFLICT (ein, tax_prd, formtype) DO UPDATE SET
    {_UPDATE_SQL},
    updated_at = now()
"""


def _flush_batch(
    conn: psycopg.Connection,
    batch: list[dict[str, Any]],
    dry_run: bool,
) -> int:
    if not batch:
        return 0
    if dry_run:
        log.info("dry-run: would upsert %d rows", len(batch))
        return 0

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _stage_source_propublica_nonprofits (
                LIKE entities.source_propublica_nonprofits INCLUDING DEFAULTS
            ) ON COMMIT DELETE ROWS
        """)
        cur.execute("TRUNCATE _stage_source_propublica_nonprofits")

        for row in batch:
            cols = list(row.keys())
            placeholders = ", ".join(f"%({c})s" for c in cols)
            cur.execute(
                f"INSERT INTO _stage_source_propublica_nonprofits "
                f"({', '.join(cols)}) VALUES ({placeholders})",
                row,
            )

        cur.execute(_UPSERT_SQL)
        affected = cur.rowcount

    conn.commit()
    return affected


# --------------------------------------------------------------------------- #
# Run record helpers
# --------------------------------------------------------------------------- #


def _open_run(conn: psycopg.Connection, source_url: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.propublica_nonprofit_ingest_runs
                (dataset_form, status, source_url)
            VALUES ('NONPROFIT_EXPLORER_990', 'running', %s)
            RETURNING id::text
            """,
            (source_url,),
        )
        run_id: str = cur.fetchone()[0]  # type: ignore[index]
    conn.commit()
    return run_id


def _close_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_in_csv: int,
    rows_inserted: int,
    started_at: datetime,
    error_message: str | None = None,
) -> None:
    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.propublica_nonprofit_ingest_runs SET
                status            = %s,
                rows_in_csv       = %s,
                rows_inserted     = %s,
                finished_at       = %s,
                duration_seconds  = %s,
                error_message     = %s
            WHERE id = %s::uuid
            """,
            (
                status,
                rows_in_csv,
                rows_inserted,
                finished_at,
                duration,
                error_message,
                run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# API iteration
# --------------------------------------------------------------------------- #


def _api_get(client: httpx.Client, url: str, params: dict | None = None) -> dict:
    retries = 0
    while True:
        resp = client.get(url, params=params, timeout=60)
        if resp.status_code in RETRY_STATUSES and retries < MAX_RETRIES:
            retries += 1
            time.sleep(2**retries)
            continue
        resp.raise_for_status()
        return resp.json()


def _iter_org_eins(
    client: httpx.Client,
    *,
    state: str | None,
    ntee: str | None,
) -> Iterator[int]:
    """Paginate /search.json and yield EIN integers."""
    page = 0
    while True:
        params: dict[str, Any] = {"page": page}
        if state:
            params["state[id]"] = state
        if ntee:
            params["ntee[id]"] = ntee
        data = _api_get(client, PROPUBLICA_API + "search.json", params=params)
        orgs = data.get("organizations", [])
        if not orgs:
            break
        for org in orgs:
            yield int(org["ein"])
        total = data.get("total_results", 0)
        if (page + 1) * PAGE_SIZE >= total:
            break
        page += 1


def _fetch_org(client: httpx.Client, ein: int) -> dict:
    url = f"{PROPUBLICA_API}organizations/{ein}.json"
    return _api_get(client, url)


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


def _ingest(
    conn: psycopg.Connection,
    *,
    eins: list[int] | None,
    state: str | None,
    ntee: str | None,
    fixture: Path | None,
    dry_run: bool,
    batch_size: int,
) -> None:
    source_observed_at = datetime.now(timezone.utc)
    source_url = PROPUBLICA_API if not fixture else str(fixture)
    run_id = _open_run(conn, source_url)
    started_at = datetime.now(timezone.utc)

    rows_in_csv = 0
    rows_inserted = 0

    try:
        batch: list[dict[str, Any]] = []

        if fixture:
            log.info("loading fixture %s", fixture)
            payload = json.loads(fixture.read_text())
            org = payload.get("organization", {})
            filings = payload.get("filings_with_data", [])
            for filing in filings:
                rows_in_csv += 1
                row = _build_row(
                    org,
                    filing,
                    source_filename=fixture.name,
                    source_download_url=f"fixture://{fixture}",
                    source_observed_at=source_observed_at,
                    source_run_metadata={"api_version": payload.get("api_version")},
                )
                batch.append(row)
                if len(batch) >= batch_size:
                    rows_inserted += _flush_batch(conn, batch, dry_run)
                    batch.clear()
        else:
            with httpx.Client(
                headers={"User-Agent": "data-engine-x/1.0 ingest"},
                timeout=60,
            ) as client:
                if eins:
                    ein_iter: Iterator[int] = iter(eins)
                else:
                    ein_iter = _iter_org_eins(client, state=state, ntee=ntee)

                for ein in ein_iter:
                    try:
                        payload = _fetch_org(client, ein)
                    except httpx.HTTPStatusError as e:
                        log.warning("EIN %s fetch failed: %s", ein, e)
                        continue
                    org = payload.get("organization", {})
                    for filing in payload.get("filings_with_data", []):
                        rows_in_csv += 1
                        row = _build_row(
                            org,
                            filing,
                            source_filename=f"propublica_org_{ein}.json",
                            source_download_url=(
                                f"{PROPUBLICA_API}organizations/{ein}.json"
                            ),
                            source_observed_at=source_observed_at,
                            source_run_metadata={
                                "api_version": payload.get("api_version"),
                                "data_source": payload.get("data_source"),
                            },
                        )
                        batch.append(row)
                    if len(batch) >= batch_size:
                        rows_inserted += _flush_batch(conn, batch, dry_run)
                        batch.clear()

        if batch:
            rows_inserted += _flush_batch(conn, batch, dry_run)

        log.info("%d filings processed, ~%d upserted", rows_in_csv, rows_inserted)
        _close_run(
            conn,
            run_id,
            status="completed",
            rows_in_csv=rows_in_csv,
            rows_inserted=rows_inserted,
            started_at=started_at,
        )

    except Exception as exc:
        log.exception("ingest failed: %s", exc)
        _close_run(
            conn,
            run_id,
            status="failed",
            rows_in_csv=rows_in_csv,
            rows_inserted=0,
            started_at=started_at,
            error_message=str(exc),
        )
        raise


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ProPublica Nonprofit Explorer 990 ingest → "
            "entities.source_propublica_nonprofits"
        )
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Path to local JSON fixture (single org API response)",
    )
    parser.add_argument(
        "--ein",
        type=int,
        nargs="+",
        default=None,
        help="One or more EINs to ingest",
    )
    parser.add_argument("--state", default=None, help="Filter /search.json by state")
    parser.add_argument("--ntee", default=None, help="Filter /search.json by NTEE code")
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse + validate but do not write to DB"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    db_url = _database_url()
    with psycopg.connect(db_url, autocommit=False) as conn:
        _ingest(
            conn,
            eins=args.ein,
            state=args.state,
            ntee=args.ntee,
            fixture=args.fixture,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
