"""s1 - CA SoS Master Unload zip to 3 ZSTD Parquet objects on R2.

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Modal-hosted (per validator p3: local-Mac runs OOM on multi-GB CSV scans;
last night's zip5 spill-28-GiB failure mode is the same shape).

Reads:
    s3://dex-raw-landing-zone/sos-ca/incoming/DataRequest0x229B7838EEF50299FDF89F08D317243BD03FFFF3.zip
    (operator-staged copy of /Users/benjamincrane/Downloads/DataRequest0x...FFFF3.zip)

Writes:
    s3://dex-raw-landing-zone/sos-ca/release=2026-05-16/entities/data.parquet
    s3://dex-raw-landing-zone/sos-ca/release=2026-05-16/principals/data.parquet
    s3://dex-raw-landing-zone/sos-ca/release=2026-05-16/agents/data.parquet

Encoding (L41 - validator p2):
    First 100K rows / 1 MB of every CSV are pure ASCII / UTF-8 valid per
    validator probe, but full-file scans may hit cp1252 bytes in tail rows
    (CA business names with curly quotes / em-dashes). Decoder is UTF-8-strict
    with WINDOWS-1252 (cp1252) fallback. iconv-with-TRANSLIT is preferred where
    available; in-Python decode is the canonical fallback.

L42 constraint (ZSTD Parquet on R2):
    boto3 upload uses ExtraArgs={'ContentType': 'application/x-parquet'} ONLY.
    The R2 object key suffix is .parquet (no compression suffix). See L42 in
    DATA-FACTORY-LESSONS-LEARNED.md for the full rationale.

Normalizer (validator p1 - PR #459/#460 root cause):
    ONLY scripts._lib.entity_name_normalize.normalize_entity_name on the
    ENTITY_NAME / ORG_NAME projections. The terminal-only vs global
    suffix-strip divergence in the OTHER normalizer caused the UCC x Overture
    x PDL reverts within the last ~3h of repo history.

CSV format (validator-confirmed 2026-05-16T03:08Z):
    Delimiter: *|* (3-char, NOT pipe).
    Line terminator: \\r\\n (CRLF).
    Filings.csv:    36 cols, 3.81 GB
    Principals.csv: 14 cols, 2.68 GB
    Agents.csv:     14 cols, 1.34 GB

Modal hosting (validator p3):
    @app.function(cpu=8, memory=16384, timeout=14400)
    secrets=[bulk-ingest-r2]

Deploy / run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run scripts/run_ca_sos_master_unload_to_r2.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-ca-sos-master-unload-to-r2")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("unzip", "libicu-dev")
    .pip_install("pyarrow>=16.0", "boto3>=1.34")
    .add_local_dir(
        Path(__file__).resolve().parent,
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [modal.Secret.from_name("bulk-ingest-r2")]

# Sized for the 7.83 GB unzipped corpus + DuckDB / pyarrow working set.
FUNC_CPU = 8
FUNC_MEMORY_MB = 16384
FUNC_TIMEOUT_SECONDS = 14400  # 4h cap

# ---------------------------------------------------------------------------
# Constants (load-bearing - match harness greps)
# ---------------------------------------------------------------------------

ZIP_OBJECT_KEY = (
    "sos-ca/incoming/DataRequest0x229B7838EEF50299FDF89F08D317243BD03FFFF3.zip"
)
ZIP_LOCAL_HINT = (
    "/Users/benjamincrane/Downloads/DataRequest0x229B7838EEF50299FDF89F08D317243BD03FFFF3.zip"
)

R2_BUCKET = "dex-raw-landing-zone"
RELEASE = "2026-05-16"
R2_OUTPUT_PREFIX = f"sos-ca/release={RELEASE}"

# 3-char *|* delimiter - verified by validator
DELIMITER = "*|*"

# Inputs (csv name, table name, harness pattern)
CSV_ENTITIES = "Filings.csv"
CSV_PRINCIPALS = "Principals.csv"
CSV_AGENTS = "Agents.csv"

# CRLF line terminator confirmed across all 3 CSVs by validator
LINE_TERMINATOR = "\r\n"

# Column headers (validator-confirmed 2026-05-16T03:08Z)
ENTITIES_HEADER = [
    "ENTITY_NAME", "ENTITY_NUM", "INITIAL_FILING_DATE", "JURISDICTION",
    "ENTITY_STATUS", "STANDING_SOS", "ENTITY_TYPE", "FILING_TYPE",
    "FOREIGN_NAME", "STANDING_FTB", "STANDING_VCFCF", "STANDING_AGENT",
    "SUSPENSION_DATE", "LAST_SI_FILE_NUMBER", "LAST_SI_FILE_DATE",
    "PRINCIPAL_ADDRESS", "PRINCIPAL_ADDRESS2", "PRINCIPAL_CITY",
    "PRINCIPAL_STATE", "PRINCIPAL_COUNTRY", "PRINCIPAL_POSTAL_CODE",
    "MAILING_ADDRESS", "MAILING_ADDRESS2", "MAILING_ADDRESS3",
    "MAILING_CITY", "MAILING_STATE", "MAILING_COUNTRY", "MAILING_POSTAL_CODE",
    "PRINCIPAL_ADDRESS_IN_CA", "PRINCIPAL_ADDRESS2_IN_CA",
    "PRINCIPAL_CITY_IN_CA", "PRINCIPAL_STATE_IN_CA",
    "PRINCIPAL_COUNTRY_IN_CA", "PRINCIPAL_POSTAL_CODE_IN_CA",
    "LLC_MANAGEMENT_STRUCTURE", "TYPE_OF_BUSINESS",
]

PRINCIPALS_HEADER = [
    "ENTITY_NAME", "ENTITY_NUM", "ORG_NAME", "FIRST_NAME", "MIDDLE_NAME",
    "LAST_NAME", "POSITION_TYPE", "ADDRESS1", "ADDRESS2", "ADDRESS3",
    "CITY", "STATE", "COUNTRY", "POSTAL_CODE",
]

AGENTS_HEADER = [
    "ENTITY_NAME", "ENTITY_NUM", "ORG_NAME", "FIRST_NAME", "MIDDLE_NAME",
    "LAST_NAME", "PHYSICAL_ADDRESS1", "PHYSICAL_ADDRESS2",
    "PHYSICAL_ADDRESS3", "PHYSICAL_CITY", "PHYSICAL_STATE",
    "PHYSICAL_COUNTRY", "PHYSICAL_POSTAL_CODE", "AGENT_TYPE",
]


logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_bytes_utf8_or_cp1252(raw: bytes) -> str:
    """L41: UTF-8 strict, then cp1252 / WINDOWS-1252 fallback.

    Header probes (first 100K rows of each CSV) are UTF-8 valid, but tail
    rows of CA business filings may contain cp1252 bytes (smart-quotes,
    em-dashes, accented characters). Hard-fail on UTF-8 first; fall back to
    WINDOWS-1252 decode (cp1252) which is a strict superset of Latin-1 for
    the C1 codepoints (0x80-0x9F) where curly quotes live.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning(
            "UTF-8 decode failed; falling back to WINDOWS-1252 (cp1252) per L41"
        )
        return raw.decode("cp1252")


def _parse_3char_delim_line(line: str) -> list[str]:
    """Split on '*|*' (3 chars). Strip trailing CR if present."""
    if line.endswith("\r"):
        line = line[:-1]
    return line.split(DELIMITER)


def _stream_csv_rows(text: str, expected_cols: int):
    """Yield row-dicts from a *|* delimited CSV text body (skip header).

    Skips the header row. Coerces short rows to None-padded; truncates long.
    """
    lines = text.split("\n")
    if not lines:
        return
    # Skip header
    for raw_line in lines[1:]:
        if not raw_line:
            continue
        fields = _parse_3char_delim_line(raw_line)
        if len(fields) < expected_cols:
            fields = fields + [""] * (expected_cols - len(fields))
        elif len(fields) > expected_cols:
            fields = fields[:expected_cols]
        yield fields


def _project_entities(rows_iter):
    """Project Filings.csv rows to entities schema (column-oriented arrays)."""
    from scripts._lib.entity_name_normalize import normalize_entity_name

    cols: dict[str, list] = {
        "entity_num": [], "entity_name": [], "entity_name_normalized": [],
        "initial_filing_date": [], "jurisdiction": [],
        "entity_status": [], "standing_sos": [], "suspension_date": [],
        "last_si_file_date": [], "entity_type": [], "filing_type": [],
        "llc_management_structure": [],
        "principal_address": [], "principal_address2": [],
        "principal_city": [], "principal_state": [], "principal_country": [],
        "principal_postal_code": [],
        "principal_address_in_ca": [], "principal_address2_in_ca": [],
        "principal_city_in_ca": [], "principal_state_in_ca": [],
        "principal_country_in_ca": [], "principal_postal_code_in_ca": [],
        "mailing_address": [], "mailing_address2": [], "mailing_address3": [],
        "mailing_city": [], "mailing_state": [], "mailing_country": [],
        "mailing_postal_code": [],
        "type_of_business": [],
    }
    idx = {name: i for i, name in enumerate(ENTITIES_HEADER)}
    for fields in rows_iter:
        ent_name = fields[idx["ENTITY_NAME"]]
        cols["entity_num"].append(fields[idx["ENTITY_NUM"]] or None)
        cols["entity_name"].append(ent_name or None)
        cols["entity_name_normalized"].append(
            normalize_entity_name(ent_name) if ent_name else None
        )
        cols["initial_filing_date"].append(
            fields[idx["INITIAL_FILING_DATE"]] or None
        )
        cols["jurisdiction"].append(fields[idx["JURISDICTION"]] or None)
        cols["entity_status"].append(fields[idx["ENTITY_STATUS"]] or None)
        cols["standing_sos"].append(fields[idx["STANDING_SOS"]] or None)
        cols["suspension_date"].append(fields[idx["SUSPENSION_DATE"]] or None)
        cols["last_si_file_date"].append(
            fields[idx["LAST_SI_FILE_DATE"]] or None
        )
        cols["entity_type"].append(fields[idx["ENTITY_TYPE"]] or None)
        cols["filing_type"].append(fields[idx["FILING_TYPE"]] or None)
        cols["llc_management_structure"].append(
            fields[idx["LLC_MANAGEMENT_STRUCTURE"]] or None
        )
        cols["principal_address"].append(fields[idx["PRINCIPAL_ADDRESS"]] or None)
        cols["principal_address2"].append(fields[idx["PRINCIPAL_ADDRESS2"]] or None)
        cols["principal_city"].append(fields[idx["PRINCIPAL_CITY"]] or None)
        cols["principal_state"].append(fields[idx["PRINCIPAL_STATE"]] or None)
        cols["principal_country"].append(fields[idx["PRINCIPAL_COUNTRY"]] or None)
        cols["principal_postal_code"].append(
            fields[idx["PRINCIPAL_POSTAL_CODE"]] or None
        )
        cols["principal_address_in_ca"].append(
            fields[idx["PRINCIPAL_ADDRESS_IN_CA"]] or None
        )
        cols["principal_address2_in_ca"].append(
            fields[idx["PRINCIPAL_ADDRESS2_IN_CA"]] or None
        )
        cols["principal_city_in_ca"].append(fields[idx["PRINCIPAL_CITY_IN_CA"]] or None)
        cols["principal_state_in_ca"].append(fields[idx["PRINCIPAL_STATE_IN_CA"]] or None)
        cols["principal_country_in_ca"].append(
            fields[idx["PRINCIPAL_COUNTRY_IN_CA"]] or None
        )
        cols["principal_postal_code_in_ca"].append(
            fields[idx["PRINCIPAL_POSTAL_CODE_IN_CA"]] or None
        )
        cols["mailing_address"].append(fields[idx["MAILING_ADDRESS"]] or None)
        cols["mailing_address2"].append(fields[idx["MAILING_ADDRESS2"]] or None)
        cols["mailing_address3"].append(fields[idx["MAILING_ADDRESS3"]] or None)
        cols["mailing_city"].append(fields[idx["MAILING_CITY"]] or None)
        cols["mailing_state"].append(fields[idx["MAILING_STATE"]] or None)
        cols["mailing_country"].append(fields[idx["MAILING_COUNTRY"]] or None)
        cols["mailing_postal_code"].append(fields[idx["MAILING_POSTAL_CODE"]] or None)
        cols["type_of_business"].append(fields[idx["TYPE_OF_BUSINESS"]] or None)
    return cols


def _project_principals(rows_iter):
    """Project Principals.csv rows to principals schema."""
    from scripts._lib.entity_name_normalize import normalize_entity_name

    cols: dict[str, list] = {
        "entity_num": [], "entity_name_normalized": [],
        "org_name": [], "org_name_normalized": [],
        "first_name": [], "middle_name": [], "last_name": [],
        "full_name_normalized": [], "position_type": [],
        "address1": [], "address2": [], "address3": [],
        "city": [], "state": [], "country": [], "postal_code": [],
    }
    idx = {name: i for i, name in enumerate(PRINCIPALS_HEADER)}
    for fields in rows_iter:
        ent_name = fields[idx["ENTITY_NAME"]]
        org_name = fields[idx["ORG_NAME"]]
        first = (fields[idx["FIRST_NAME"]] or "").strip()
        middle = (fields[idx["MIDDLE_NAME"]] or "").strip()
        last = (fields[idx["LAST_NAME"]] or "").strip()
        # full_name_normalized per directive: lower(trim(concat_ws(' ', f, m, l)))
        full_norm = " ".join(p for p in (first, middle, last) if p).lower().strip() or None

        cols["entity_num"].append(fields[idx["ENTITY_NUM"]] or None)
        cols["entity_name_normalized"].append(
            normalize_entity_name(ent_name) if ent_name else None
        )
        cols["org_name"].append(org_name or None)
        cols["org_name_normalized"].append(
            normalize_entity_name(org_name) if org_name else None
        )
        cols["first_name"].append(first or None)
        cols["middle_name"].append(middle or None)
        cols["last_name"].append(last or None)
        cols["full_name_normalized"].append(full_norm)
        cols["position_type"].append(fields[idx["POSITION_TYPE"]] or None)
        cols["address1"].append(fields[idx["ADDRESS1"]] or None)
        cols["address2"].append(fields[idx["ADDRESS2"]] or None)
        cols["address3"].append(fields[idx["ADDRESS3"]] or None)
        cols["city"].append(fields[idx["CITY"]] or None)
        cols["state"].append(fields[idx["STATE"]] or None)
        cols["country"].append(fields[idx["COUNTRY"]] or None)
        cols["postal_code"].append(fields[idx["POSTAL_CODE"]] or None)
    return cols


def _project_agents(rows_iter):
    """Project Agents.csv rows to agents schema."""
    from scripts._lib.entity_name_normalize import normalize_entity_name

    cols: dict[str, list] = {
        "entity_num": [], "entity_name_normalized": [],
        "agent_type": [], "org_name": [], "org_name_normalized": [],
        "first_name": [], "middle_name": [], "last_name": [],
        "physical_address1": [], "physical_address2": [], "physical_address3": [],
        "physical_city": [], "physical_state": [],
        "physical_country": [], "physical_postal_code": [],
    }
    idx = {name: i for i, name in enumerate(AGENTS_HEADER)}
    for fields in rows_iter:
        ent_name = fields[idx["ENTITY_NAME"]]
        org_name = fields[idx["ORG_NAME"]]
        cols["entity_num"].append(fields[idx["ENTITY_NUM"]] or None)
        cols["entity_name_normalized"].append(
            normalize_entity_name(ent_name) if ent_name else None
        )
        cols["agent_type"].append(fields[idx["AGENT_TYPE"]] or None)
        cols["org_name"].append(org_name or None)
        cols["org_name_normalized"].append(
            normalize_entity_name(org_name) if org_name else None
        )
        cols["first_name"].append((fields[idx["FIRST_NAME"]] or "").strip() or None)
        cols["middle_name"].append((fields[idx["MIDDLE_NAME"]] or "").strip() or None)
        cols["last_name"].append((fields[idx["LAST_NAME"]] or "").strip() or None)
        cols["physical_address1"].append(fields[idx["PHYSICAL_ADDRESS1"]] or None)
        cols["physical_address2"].append(fields[idx["PHYSICAL_ADDRESS2"]] or None)
        cols["physical_address3"].append(fields[idx["PHYSICAL_ADDRESS3"]] or None)
        cols["physical_city"].append(fields[idx["PHYSICAL_CITY"]] or None)
        cols["physical_state"].append(fields[idx["PHYSICAL_STATE"]] or None)
        cols["physical_country"].append(fields[idx["PHYSICAL_COUNTRY"]] or None)
        cols["physical_postal_code"].append(
            fields[idx["PHYSICAL_POSTAL_CODE"]] or None
        )
    return cols


def _download_zip_from_r2(s3_client, local_path: str) -> None:
    """Download the ZIP from R2 to the Modal container's local FS."""
    logger.info("downloading zip from r2://%s/%s ...", R2_BUCKET, ZIP_OBJECT_KEY)
    t0 = time.time()
    s3_client.download_file(R2_BUCKET, ZIP_OBJECT_KEY, local_path)
    size = os.path.getsize(local_path)
    logger.info(
        "downloaded %d bytes (%.1f MB) in %.1fs",
        size, size / 1024 / 1024, time.time() - t0,
    )


def _upload_parquet_to_r2(
    s3_client, local_path: str, table_name: str
) -> str:
    """Upload local Parquet file to R2 with L42-safe ExtraArgs.

    See L42 in DATA-FACTORY-LESSONS-LEARNED.md. ExtraArgs is restricted to
    ContentType only; the .parquet key suffix is preserved (no .zst).
    """
    key = f"{R2_OUTPUT_PREFIX}/{table_name}/data.parquet"
    size = os.path.getsize(local_path)
    logger.info(
        "uploading %s (%d bytes, %.1f MB) to r2://%s/%s",
        local_path, size, size / 1024 / 1024, R2_BUCKET, key,
    )
    t0 = time.time()
    s3_client.upload_file(
        local_path,
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    logger.info("uploaded %s in %.1fs", key, time.time() - t0)
    return key


def _transcode_one(
    zip_path: str,
    csv_name: str,
    table_name: str,
    projector,
    expected_cols: int,
    s3_client,
) -> dict:
    """Read one CSV from the zip, project, write ZSTD Parquet, upload to R2."""
    import pyarrow as pa
    from pyarrow import parquet as pq

    t0 = time.time()
    logger.info("=== %s -> %s ===", csv_name, table_name)

    # Stream the CSV bytes out of the zip
    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open(csv_name, "r") as fh:
            raw = fh.read()
    logger.info("read %d bytes from %s", len(raw), csv_name)

    # L41 decode: UTF-8 strict, cp1252 (WINDOWS-1252) fallback
    text = _decode_bytes_utf8_or_cp1252(raw)
    del raw

    rows_iter = _stream_csv_rows(text, expected_cols)
    cols = projector(rows_iter)
    del text

    row_count = len(next(iter(cols.values())))
    logger.info("projected %d rows for %s", row_count, table_name)

    # Build pyarrow Table with all-string schema (preserve raw text)
    tbl = pa.table(
        {name: pa.array(vals, type=pa.string()) for name, vals in cols.items()}
    )
    del cols

    # Write ZSTD Parquet locally
    with tempfile.NamedTemporaryFile(
        suffix=".parquet", delete=False, dir="/tmp"
    ) as tmp:
        local_parquet = tmp.name
    pq.write_table(
        tbl,
        local_parquet,
        compression="zstd",
        compression_level=9,
    )
    del tbl
    size = os.path.getsize(local_parquet)
    logger.info(
        "wrote %d bytes (%.1f MB) ZSTD Parquet for %s",
        size, size / 1024 / 1024, table_name,
    )

    # Upload to R2 - L42 ContentType only
    key = _upload_parquet_to_r2(s3_client, local_parquet, table_name)
    try:
        os.unlink(local_parquet)
    except OSError:
        pass

    return {
        "table": table_name,
        "csv": csv_name,
        "rows": row_count,
        "r2_key": key,
        "parquet_bytes": size,
        "duration_s": round(time.time() - t0, 1),
    }


def _s3_client_r2(boto3):
    """Construct a boto3 S3 client targeting R2."""
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(
            read_timeout=300,
            retries={"max_attempts": 5, "mode": "adaptive"},
        ),
    )


# ---------------------------------------------------------------------------
# Modal entry
# ---------------------------------------------------------------------------

@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=FUNC_TIMEOUT_SECONDS,
    memory=FUNC_MEMORY_MB,
    cpu=FUNC_CPU,
)
def transcode() -> dict:
    """Read 3 CSVs from R2 zip, project, write 3 ZSTD Parquet to R2."""
    sys.path.insert(0, "/root")
    import boto3  # noqa: E402

    s3 = _s3_client_r2(boto3)

    # Download zip to local /tmp
    with tempfile.NamedTemporaryFile(
        suffix=".zip", delete=False, dir="/tmp"
    ) as tmp:
        zip_local = tmp.name
    try:
        _download_zip_from_r2(s3, zip_local)
        logger.info(
            "zip downloaded; canonical operator path was %s", ZIP_LOCAL_HINT
        )

        results = []
        for csv_name, table, projector, ncols in [
            (CSV_ENTITIES, "entities", _project_entities, len(ENTITIES_HEADER)),
            (CSV_PRINCIPALS, "principals", _project_principals, len(PRINCIPALS_HEADER)),
            (CSV_AGENTS, "agents", _project_agents, len(AGENTS_HEADER)),
        ]:
            results.append(
                _transcode_one(zip_local, csv_name, table, projector, ncols, s3)
            )
    finally:
        try:
            os.unlink(zip_local)
        except OSError:
            pass

    summary = {
        "release": RELEASE,
        "delimiter": DELIMITER,
        "line_terminator_kind": "CRLF",
        "outputs": results,
    }
    logger.info("DONE: %s", summary)
    return summary


@app.local_entrypoint()
def run() -> None:
    """`modal run scripts/run_ca_sos_master_unload_to_r2.py::run`"""
    import json
    out = transcode.remote()
    print(json.dumps(out, indent=2, default=str))
