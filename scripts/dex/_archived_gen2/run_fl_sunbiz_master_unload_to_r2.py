"""s1 - FL Sunbiz Quarterly Data zips to 3 ZSTD Parquet objects on R2.

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Modal-hosted (validator p2 / p191: 17.7 GB cordata + 9.6 GB corevent
uncompressed will OOM local-Mac with the same shape as the 2026-05-15 zip5
28-GiB spill failure mode).

Reads (operator-staged copies of /Users/benjamincrane/Downloads/*):
    s3://dex-raw-landing-zone/sos-fl/incoming/cordata.zip   (1.74 GB)
    s3://dex-raw-landing-zone/sos-fl/incoming/corevent.zip  (188 MB)

Writes:
    s3://dex-raw-landing-zone/sos-fl/release=2026-05-16/entities/data.parquet
    s3://dex-raw-landing-zone/sos-fl/release=2026-05-16/officers/data.parquet
    s3://dex-raw-landing-zone/sos-fl/release=2026-05-16/events/data.parquet

Field layout (validator p122-141 - pinned + self-tested):
    Source of truth: scripts/migration-checks/hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-cor-layout.py
    - COR_FIELDS: 79 fixed-width fields covering bytes [0..1440] of each cordata record.
      Per-record stride is 1442 bytes (1440 chars + CRLF). 10 shards cordata0.txt..cordata9.txt.
    - COREVENT_FIELDS: 25 fixed-width fields covering bytes [0..662] of each corevt record.
      Per-record stride is 664 bytes (662 chars + CRLF). 1 file corevt.txt.
    - parse_officer_name(name_42) splits the 42-char officer/agent name fields
      into (first, middle, last) - validator-confirmed format last(20)+first(20)+mid(2).

Encoding (validator p193 - L41 carve-out): FL Sunbiz is ASCII-only per the
canonical Sunbiz docs + an empirical probe of the first 1 MB of cordata0.txt
returning 0 non-printable bytes. cp1252 fallback is unnecessary here (unlike
the CA SoS predecessor that needed it for tail-row smart-quotes). Decoder is
ASCII-strict with an `errors='replace'` floor for paranoia.

L42 constraint (ZSTD Parquet on R2):
    boto3 upload uses ExtraArgs={'ContentType': 'application/x-parquet'} ONLY.
    Plain '.parquet' suffix per L42 lessons-learned. The Parquet file uses
    internal column-chunk ZSTD compression (pyarrow write_table with
    compression='zstd'), which RisingWave/DuckDB/pyarrow all decode
    natively without any HTTP-layer wire encoding header.

Normalizer (validator p1 / p189 - PR #459/#460 root cause):
    ONLY scripts._lib.entity_name_normalize.normalize_entity_name on the
    ENTITY_NAME field. NEVER the divergent name-normalization pair flagged in
    p189 - their terminal-only vs global suffix-strip divergence caused the
    UCC x Overture x PDL reverts within the last ~3h of repo history.

Construction NAICS column-absence finding (validator p153-161 / p5):
    The FL COR layout has NO industry-classification code column and NO
    business-type-description column. The directive's "operator context"
    instruction to project these for future Shovels.ai construction-industry
    lender-matching CANNOT be honored - those columns do not exist in the source.
    The downstream construction-industry layer must source industry codes from
    a different ingest (USAspending / IRS BMF / FL DBPR / FCC permits) joined
    on fei_number (= IRS EIN, byte offset 480..494). DO NOT fabricate these
    columns.

Officers grain (validator notes / audit p369-376):
    Each cordata record carries up to 6 inline officer slots at offsets 668..1436.
    Many slots are blank-padded for entities with <=2 officers. The s1 projector
    emits officers in long form: one row per (entity_num, officer_slot in {1..6})
    where the slot's name field is non-blank. Each officer row carries
    entity_num + entity_name_normalized (from the parent entity) + officer_slot +
    officer_title + officer_type + raw 42-char officer_name + parsed
    first/middle/last + full_name_normalized + address/city/state/zip4.

Modal hosting (validator p191):
    @app.function(cpu=8, memory=32768, timeout=14400)
    secrets=[bulk-ingest-r2]

Deploy / run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run scripts/run_fl_sunbiz_master_unload_to_r2.py::run
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-fl-sunbiz-master-unload-to-r2")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("unzip")
    .pip_install("pyarrow>=16.0", "boto3>=1.34")
    .add_local_dir(
        Path(__file__).resolve().parent,
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [modal.Secret.from_name("bulk-ingest-r2")]

FUNC_CPU = 8
FUNC_MEMORY_MB = 32768  # validator p191 - 17.7 GB cordata + 9.6 GB corevent
FUNC_TIMEOUT_SECONDS = 14400  # 4h cap

# ---------------------------------------------------------------------------
# Constants (load-bearing - match harness greps)
# ---------------------------------------------------------------------------

CORDATA_ZIP_KEY = "sos-fl/incoming/cordata.zip"
COREVENT_ZIP_KEY = "sos-fl/incoming/corevent.zip"
CORDATA_LOCAL_HINT = "/Users/benjamincrane/Downloads/cordata.zip"
COREVENT_LOCAL_HINT = "/Users/benjamincrane/Downloads/corevent.zip"

R2_BUCKET = "dex-raw-landing-zone"
RELEASE = "2026-05-16"
R2_OUTPUT_PREFIX = f"sos-fl/release={RELEASE}"

# Shard naming inside cordata.zip
CORDATA_SHARDS = [f"cordata{i}.txt" for i in range(10)]
COREVENT_FILE = "corevt.txt"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_ascii(raw: bytes) -> str:
    """ASCII-strict decode with errors='replace' floor (validator p193: FL is ASCII-only)."""
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        # Defensive - Sunbiz docs say ASCII-only but be paranoid on tail rows.
        logger.warning("non-ASCII byte detected; falling back to ASCII with errors='replace'")
        return raw.decode("ascii", errors="replace")


def _strip(s: str) -> str | None:
    """Trim a fixed-width field; return None if blank."""
    t = s.strip()
    return t if t else None


def _parse_officer_name_slice(raw_42: str) -> tuple[str | None, str | None, str | None]:
    """Split a 42-char officer/agent name field into (first, middle, last).

    Mirrors `parse_officer_name` in the pinned cor-layout module so the s1
    projection is the same shape as what the harness validates downstream.
    """
    # last(20) + first(20) + mid(2) per validator-confirmed empirical probe.
    last = raw_42[0:20].strip()
    first = raw_42[20:40].strip()
    middle = raw_42[40:42].strip()
    return (first or None, middle or None, last or None)


def _project_cordata_record(
    rec: str,
    COR_FIELDS: dict,
    OFFICER_SLOTS: tuple,
    normalize_entity_name,
) -> tuple[dict, list[dict]]:
    """Project ONE cordata record into (entity_cols_row, [officer_rows]).

    The entity row is a dict of the 79 source fields + entity_name_normalized.
    Officer rows are emitted for each non-blank officer slot 1..6 with
    (entity_num, entity_name_normalized, officer_slot, title, type, raw name,
    first, middle, last, full_name_normalized, address, city, state, zip4).
    """
    # 1. Slice all 79 fields per the pinned layout.
    row: dict = {}
    for name, (start, end, _length, _desc) in COR_FIELDS.items():
        row[name] = _strip(rec[start:end])

    # 2. Compute entity_name_normalized (canonical normalizer, validator p189).
    entity_name = row.get("entity_name")
    row["entity_name_normalized"] = (
        normalize_entity_name(entity_name) if entity_name else None
    )

    # 3. Extract officer slots - long-form rows for non-blank slots.
    officer_rows: list[dict] = []
    entity_num = row.get("entity_num")
    entity_name_normalized = row.get("entity_name_normalized")
    for slot in OFFICER_SLOTS:
        title = row.get(f"officer_{slot}_title")
        otype = row.get(f"officer_{slot}_type")
        raw_name_field = rec[
            COR_FIELDS[f"officer_{slot}_name"][0]:COR_FIELDS[f"officer_{slot}_name"][1]
        ]
        raw_name_stripped = raw_name_field.strip()
        if not raw_name_stripped:
            # Empty slot - skip.
            continue
        first, middle, last = _parse_officer_name_slice(raw_name_field)
        # full_name_normalized - mirror the CA SoS principals shape.
        full_parts = [p for p in (first, middle, last) if p]
        full_name_normalized = " ".join(full_parts).lower().strip() or None
        officer_rows.append(
            {
                "entity_num": entity_num,
                "entity_name_normalized": entity_name_normalized,
                "officer_slot": slot,
                "officer_title": title,
                "officer_type": otype,
                "officer_name": raw_name_stripped,
                "first_name": first,
                "middle_name": middle,
                "last_name": last,
                "full_name_normalized": full_name_normalized,
                "officer_address": row.get(f"officer_{slot}_address"),
                "officer_city": row.get(f"officer_{slot}_city"),
                "officer_state": row.get(f"officer_{slot}_state"),
                "officer_zip4": row.get(f"officer_{slot}_zip4"),
            }
        )

    return row, officer_rows


def _project_corevent_record(rec: str, COREVENT_FIELDS: dict) -> dict:
    """Project ONE corevt record into a dict of 25 fields."""
    row: dict = {}
    for name, (start, end, _length, _desc) in COREVENT_FIELDS.items():
        row[name] = _strip(rec[start:end])
    return row


def _stream_fixed_width_records(
    fh, content_bytes: int, stride_bytes: int
):
    """Yield ASCII-decoded content-only strings, stride_bytes per record.

    Reads the entire shard into memory (Modal container has 32 GiB; a single
    1.8 GiB shard is well below that). Splits on stride; returns the content
    portion (stride - 2 for CRLF) of each record. Skips short tail records.
    """
    raw = fh.read()
    n = len(raw)
    logger.info("    read %d bytes (%.1f MB) for fixed-width split", n, n / 1024 / 1024)
    # Sanity-check: should be a multiple of stride (with CRLF terminator).
    if n % stride_bytes != 0:
        logger.warning(
            "    shard size %d is not a multiple of stride %d (remainder %d) - "
            "last record may be truncated; will skip short tail",
            n, stride_bytes, n % stride_bytes,
        )
    # Decode once (ASCII-only per validator p193).
    text = _decode_ascii(raw)
    del raw
    # Stride-walk.
    pos = 0
    total = len(text)
    while pos + stride_bytes <= total:
        # content is the first `content_bytes` chars of this stride window.
        yield text[pos : pos + content_bytes]
        pos += stride_bytes
    # Drop the variable-length tail (no more complete records).


def _unzip_shard_to_local(zip_path: str, shard: str, extract_dir: str) -> str:
    """Extract one shard via the `unzip` CLI (handles PKZip Deflate64 / Def64N
    which Python's zipfile cannot decode). Returns the local extracted path.
    """
    out_path = os.path.join(extract_dir, shard)
    # `unzip -o` overwrites without prompt; `-d` sets the dest dir.
    logger.info("    unzip -o %s %s -d %s", zip_path, shard, extract_dir)
    t0 = time.time()
    result = subprocess.run(
        ["unzip", "-o", zip_path, shard, "-d", extract_dir],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "unzip failed (rc=%d): stdout=%s stderr=%s",
            result.returncode, result.stdout, result.stderr,
        )
        raise RuntimeError(f"unzip failed for {shard}: {result.stderr}")
    size = os.path.getsize(out_path)
    logger.info(
        "    extracted %s (%d bytes, %.1f MB) in %.1fs",
        shard, size, size / 1024 / 1024, time.time() - t0,
    )
    return out_path


def _transcode_cordata(zip_path: str):
    """Read ALL 10 cordata shards from cordata.zip, project to 2 pyarrow Tables.

    cordata.zip uses PKZip Deflate64 (Def64N) which Python's zipfile module
    does NOT support. We shell out to the `unzip` CLI for each shard,
    process, then delete the extracted file to keep disk usage bounded.

    Returns (entities_table, officers_table, total_entities, total_officers).
    """
    import pyarrow as pa

    sys.path.insert(0, "/root")
    from scripts.migration_checks.cor_layout import (  # type: ignore  # noqa: F401
        COR_FIELDS, COR_RECORD_BYTES, COR_LINE_STRIDE_BYTES, OFFICER_SLOTS,
    )
    from scripts._lib.entity_name_normalize import normalize_entity_name

    # Pre-allocate column-oriented dicts for entity + officer rows.
    entity_cols: dict[str, list] = {name: [] for name in COR_FIELDS.keys()}
    entity_cols["entity_name_normalized"] = []
    officer_cols: dict[str, list] = {
        "entity_num": [], "entity_name_normalized": [], "officer_slot": [],
        "officer_title": [], "officer_type": [], "officer_name": [],
        "first_name": [], "middle_name": [], "last_name": [],
        "full_name_normalized": [],
        "officer_address": [], "officer_city": [], "officer_state": [],
        "officer_zip4": [],
    }

    # Use a dedicated extract dir for streamed-out shards.
    extract_dir = tempfile.mkdtemp(prefix="fl-cordata-", dir="/tmp")
    logger.info("cordata extract dir: %s", extract_dir)

    total_entities = 0
    total_officers = 0
    t0 = time.time()
    try:
        # Use the unzip CLI to enumerate the archive (avoid loading entire zip
        # into Python's zipfile, which can't read Deflate64).
        list_result = subprocess.run(
            ["unzip", "-Z", "-1", zip_path],
            capture_output=True, text=True, check=True,
        )
        names_in_zip = set(list_result.stdout.strip().split("\n"))
        logger.info("cordata.zip members: %s", sorted(names_in_zip))

        for shard in CORDATA_SHARDS:
            if shard not in names_in_zip:
                logger.warning("  shard %s NOT in zip - skipping", shard)
                continue
            t_shard = time.time()
            logger.info("  --- shard %s ---", shard)
            shard_path = _unzip_shard_to_local(zip_path, shard, extract_dir)
            shard_entity_count = 0
            shard_officer_count = 0
            with open(shard_path, "rb") as fh:
                for rec in _stream_fixed_width_records(
                    fh, COR_RECORD_BYTES, COR_LINE_STRIDE_BYTES
                ):
                    if len(rec) != COR_RECORD_BYTES:
                        # Defensive - drop malformed tail.
                        continue
                    entity_row, officer_rows = _project_cordata_record(
                        rec, COR_FIELDS, OFFICER_SLOTS, normalize_entity_name
                    )
                    # Append entity columns.
                    for k, v in entity_row.items():
                        entity_cols[k].append(v)
                    shard_entity_count += 1
                    # Append officer rows.
                    for orow in officer_rows:
                        for k, v in orow.items():
                            officer_cols[k].append(v)
                        shard_officer_count += 1
            # Free disk between shards.
            try:
                os.unlink(shard_path)
            except OSError:
                pass
            logger.info(
                "    %s: %d entities, %d officers (%.1fs)",
                shard, shard_entity_count, shard_officer_count,
                time.time() - t_shard,
            )
            total_entities += shard_entity_count
            total_officers += shard_officer_count
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    logger.info(
        "TOTAL cordata: %d entities, %d officers across %d shards (%.1fs)",
        total_entities, total_officers, len(CORDATA_SHARDS), time.time() - t0,
    )

    # Build pyarrow Tables.
    entities_table = pa.table(
        {name: pa.array(vals, type=pa.string()) for name, vals in entity_cols.items()
         if name != "officer_slot"}
    )
    # Officers: officer_slot is int8; everything else is string.
    officer_arrays = {}
    for k, vals in officer_cols.items():
        if k == "officer_slot":
            officer_arrays[k] = pa.array(vals, type=pa.int8())
        else:
            officer_arrays[k] = pa.array(vals, type=pa.string())
    officers_table = pa.table(officer_arrays)

    return entities_table, officers_table, total_entities, total_officers


def _transcode_corevent(zip_path: str):
    """Read corevt.txt from corevent.zip, project to a pyarrow Table.

    Uses the `unzip` CLI for consistency with cordata's Deflate64 path.

    Returns (events_table, total_events).
    """
    import pyarrow as pa

    sys.path.insert(0, "/root")
    from scripts.migration_checks.cor_layout import (  # type: ignore
        COREVENT_FIELDS, COREVENT_RECORD_BYTES, COREVENT_LINE_STRIDE_BYTES,
    )

    event_cols: dict[str, list] = {name: [] for name in COREVENT_FIELDS.keys()}

    extract_dir = tempfile.mkdtemp(prefix="fl-corevt-", dir="/tmp")
    logger.info("corevt extract dir: %s", extract_dir)

    t0 = time.time()
    total_events = 0
    try:
        # Confirm corevt.txt is present in the archive.
        list_result = subprocess.run(
            ["unzip", "-Z", "-1", zip_path],
            capture_output=True, text=True, check=True,
        )
        names_in_zip = set(list_result.stdout.strip().split("\n"))
        if COREVENT_FILE not in names_in_zip:
            raise FileNotFoundError(
                f"{COREVENT_FILE} not in corevent.zip (saw: {sorted(names_in_zip)})"
            )

        logger.info("  --- %s ---", COREVENT_FILE)
        corevt_path = _unzip_shard_to_local(zip_path, COREVENT_FILE, extract_dir)
        with open(corevt_path, "rb") as fh:
            for rec in _stream_fixed_width_records(
                fh, COREVENT_RECORD_BYTES, COREVENT_LINE_STRIDE_BYTES
            ):
                if len(rec) != COREVENT_RECORD_BYTES:
                    continue
                event_row = _project_corevent_record(rec, COREVENT_FIELDS)
                for k, v in event_row.items():
                    event_cols[k].append(v)
                total_events += 1
        try:
            os.unlink(corevt_path)
        except OSError:
            pass
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
    logger.info(
        "TOTAL corevt: %d events (%.1fs)", total_events, time.time() - t0,
    )
    events_table = pa.table(
        {name: pa.array(vals, type=pa.string()) for name, vals in event_cols.items()}
    )
    return events_table, total_events


def _download_zip_from_r2(s3_client, key: str, local_path: str) -> None:
    """Download one of the operator-staged zips from R2."""
    logger.info("downloading r2://%s/%s ...", R2_BUCKET, key)
    t0 = time.time()
    s3_client.download_file(R2_BUCKET, key, local_path)
    size = os.path.getsize(local_path)
    logger.info(
        "downloaded %d bytes (%.1f MB) in %.1fs",
        size, size / 1024 / 1024, time.time() - t0,
    )


def _upload_parquet_to_r2(s3_client, local_path: str, table_name: str) -> str:
    """Upload local Parquet to R2 with L42-safe ExtraArgs (ContentType ONLY).

    The R2 object key suffix is plain '.parquet' per L42 lessons-learned.
    Internal column-chunk ZSTD via pyarrow's compression='zstd' parameter is
    decoded by DuckDB/pyarrow/RW without any HTTP wire-encoding header.
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


def _write_parquet_and_upload(
    table,
    table_name: str,
    s3_client,
) -> dict:
    """Write a pyarrow Table to a local ZSTD Parquet, upload to R2."""
    from pyarrow import parquet as pq

    with tempfile.NamedTemporaryFile(
        suffix=".parquet", delete=False, dir="/tmp"
    ) as tmp:
        local_parquet = tmp.name
    t0 = time.time()
    pq.write_table(
        table,
        local_parquet,
        compression="zstd",
        compression_level=9,
        row_group_size=100_000,
    )
    write_dur = time.time() - t0
    size = os.path.getsize(local_parquet)
    logger.info(
        "wrote %d bytes (%.1f MB) ZSTD Parquet for %s in %.1fs",
        size, size / 1024 / 1024, table_name, write_dur,
    )
    key = _upload_parquet_to_r2(s3_client, local_parquet, table_name)
    try:
        os.unlink(local_parquet)
    except OSError:
        pass
    return {
        "table": table_name,
        "rows": table.num_rows,
        "r2_key": key,
        "parquet_bytes": size,
        "write_duration_s": round(write_dur, 1),
    }


def _s3_client_r2(boto3):
    """boto3 S3 client targeting R2."""
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
# Modal entry - Note: the cor_layout module name on the Modal container
# resolves to scripts/migration_checks/cor_layout.py via a small shim at
# import time (the hyphenated filename can't be imported directly). This is
# wired below by mounting the migration-checks directory and aliasing.
# ---------------------------------------------------------------------------


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=FUNC_TIMEOUT_SECONDS,
    memory=FUNC_MEMORY_MB,
    cpu=FUNC_CPU,
)
def transcode() -> dict:
    """Read cordata.zip + corevent.zip from R2, project, write 3 ZSTD Parquet to R2."""
    sys.path.insert(0, "/root")

    # Wire the pinned cor-layout module via a sys.modules alias. The on-disk
    # filename uses hyphens which Python's import system rejects; mount it
    # under a plain importable name.
    _wire_cor_layout_alias()

    import boto3  # noqa: E402

    s3 = _s3_client_r2(boto3)
    started_at = time.time()

    # Download both zips to local /tmp.
    cordata_local = None
    corevent_local = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".zip", delete=False, dir="/tmp"
        ) as tmp:
            cordata_local = tmp.name
        with tempfile.NamedTemporaryFile(
            suffix=".zip", delete=False, dir="/tmp"
        ) as tmp:
            corevent_local = tmp.name

        _download_zip_from_r2(s3, CORDATA_ZIP_KEY, cordata_local)
        _download_zip_from_r2(s3, COREVENT_ZIP_KEY, corevent_local)
        logger.info(
            "operator-staged paths: cordata=%s corevent=%s",
            CORDATA_LOCAL_HINT, COREVENT_LOCAL_HINT,
        )

        # cordata -> 2 tables (entities + officers)
        logger.info("=== cordata transcode (10 shards) ===")
        entities_table, officers_table, total_entities, total_officers = (
            _transcode_cordata(cordata_local)
        )

        # corevt -> 1 table (events)
        logger.info("=== corevt transcode ===")
        events_table, total_events = _transcode_corevent(corevent_local)

        # Write & upload 3 parquet objects.
        outputs = []
        outputs.append(_write_parquet_and_upload(entities_table, "entities", s3))
        del entities_table
        outputs.append(_write_parquet_and_upload(officers_table, "officers", s3))
        del officers_table
        outputs.append(_write_parquet_and_upload(events_table, "events", s3))
        del events_table

        summary = {
            "release": RELEASE,
            "encoding": "ascii",
            "line_strides": {
                "cordata": 1442,  # COR_LINE_STRIDE_BYTES
                "corevent": 664,  # COREVENT_LINE_STRIDE_BYTES
            },
            "outputs": outputs,
            "total_entities": total_entities,
            "total_officers": total_officers,
            "total_events": total_events,
            "duration_s": round(time.time() - started_at, 1),
        }
        logger.info("DONE: %s", summary)
        return summary
    finally:
        for p in (cordata_local, corevent_local):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _wire_cor_layout_alias() -> None:
    """Import the hyphen-named pinned layout module under
    `scripts.migration_checks.cor_layout` so the transcoders can `from ... import COR_FIELDS`.
    """
    import importlib.util
    import types

    layout_path = (
        "/root/scripts/migration-checks/"
        "hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-cor-layout.py"
    )
    spec = importlib.util.spec_from_file_location(
        "scripts.migration_checks.cor_layout", layout_path
    )
    module = importlib.util.module_from_spec(spec)
    # Ensure parent packages exist in sys.modules.
    if "scripts.migration_checks" not in sys.modules:
        sys.modules["scripts.migration_checks"] = types.ModuleType("scripts.migration_checks")
    sys.modules["scripts.migration_checks.cor_layout"] = module
    spec.loader.exec_module(module)


@app.local_entrypoint()
def run() -> None:
    """`modal run scripts/run_fl_sunbiz_master_unload_to_r2.py::run`"""
    import json
    out = transcode.remote()
    print(json.dumps(out, indent=2, default=str))
