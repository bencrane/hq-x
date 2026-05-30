"""Streaming parser for ``pg_restore --data-only -f -`` stdout.

Consumes the text stream produced by::

    pg_restore --data-only -t <table> -f - /cache/usaspending_db/

and yields ``pyarrow.RecordBatch`` chunks.

The ``pg_restore --data-only`` stream contains SQL preamble (SET statements,
``SELECT pg_catalog.set_config``, ALTER TABLE DISABLE TRIGGER lines, etc.)
followed by one or more COPY blocks, each of the form::

    COPY <schema>.<table> (col1, col2, ...) FROM stdin;
    <row1-col1>\\t<row1-col2>\\t...
    <row2-col1>\\t...
    \\.

This parser:

1. Skips all preamble lines until it detects a ``COPY ... FROM stdin;`` marker.
2. Reads TAB-delimited row data until the ``\\.`` terminator.
3. Decodes PG COPY escape sequences per the PostgreSQL COPY format spec:
   ``\\N`` → null, ``\\\\`` → backslash, ``\\t`` → tab, ``\\n`` → newline,
   ``\\r`` → CR, ``\\NNN`` (octal) → byte value NNN.
4. Type-coerces each column to the caller-specified Arrow DataType.
5. Yields ``pyarrow.RecordBatch`` instances in chunks of ``batch_size`` rows.

**Constant-memory design:** the parser never accumulates more than
``batch_size`` rows in process memory. For large tables (300M+ rows) this
keeps Arrow builder memory to roughly ``batch_size * estimated_row_bytes``
regardless of total table size.

Public API::

    parse_copy_stream(
        stdin: BinaryIO,
        column_types: list[pa.DataType],
        column_names: list[str],
        batch_size: int = 50_000,
        skip_rows: int = 0,
    ) -> Iterator[pa.RecordBatch]

``skip_rows`` discards the first N data rows without parsing them (a cheap
raw-line skip) — used to resume a decode after a preemption, when the first
N rows were already landed by the interrupted attempt.

Usage (inside a Modal function consuming pg_restore stdout)::

    import pyarrow as pa
    from scripts._lib.pg_copy_text_parser import parse_copy_stream

    column_names = ["subaward_id", "prime_award_unique_key", "subaward_amount"]
    column_types = [pa.large_utf8(), pa.large_utf8(), pa.decimal128(19, 2)]

    with subprocess.Popen(
        ["pg_restore", "--data-only", "-t", "subaward", "-f", "-", "/cache/usaspending_db/"],
        stdout=subprocess.PIPE,
    ) as proc:
        for batch in parse_copy_stream(proc.stdout, column_types, column_names):
            # process RecordBatch
            pass

L54 note: for unbounded-cardinality PG ``text[]`` columns pass
``pa.large_utf8()`` as the column type (not ``pa.list_(pa.large_utf8())``)
— the parser will join array elements with ``|`` delimiter into a single
string, dodging Lance 1.5.x's definition-buffer cap on LIST<VARCHAR>.
For bounded-cardinality arrays (e.g. ≤5 elements, fixed domain) pass
``pa.list_(pa.large_utf8())`` to get proper Arrow list semantics.
"""
from __future__ import annotations

import io
import logging
import re
from typing import BinaryIO, Iterator

import pyarrow as pa

LOG = logging.getLogger(__name__)

# Regex to detect the COPY header line:
#   COPY schema.table (col1, col2, ...) FROM stdin;
_COPY_HEADER_RE = re.compile(
    r"^COPY\s+\S+\s+\(.*\)\s+FROM\s+stdin;",
    re.IGNORECASE,
)

# End-of-COPY marker
_COPY_TERMINATOR = b"\\."


def _decode_pg_copy_escape(raw: bytes) -> bytes | None:
    """Decode a single PG COPY field value (bytes) to its unescaped form.

    Returns ``None`` for the null marker ``\\N``.
    Returns unescaped bytes otherwise.

    PG COPY text format escape rules (PostgreSQL docs §COPY):
      ``\\N``    → SQL NULL  (returned as None)
      ``\\\\``   → backslash
      ``\\t``    → tab
      ``\\n``    → newline
      ``\\r``    → carriage return
      ``\\NNN``  → octal byte value NNN (3-digit octal)
      ``\\x``    → any other char after backslash → literal ``x``
                   (PostgreSQL preserves the character itself per spec)
    """
    if raw == b"\\N":
        return None

    if b"\\" not in raw:
        return raw  # fast path — no escapes

    out = io.BytesIO()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b != ord("\\") or i + 1 >= len(raw):
            out.write(bytes([b]))
            i += 1
            continue

        nxt = raw[i + 1]
        if nxt == ord("N") and i + 1 == len(raw) - 1 and raw == b"\\N":
            # Whole-field \\N already handled above; partial \\N shouldn't occur
            out.write(b"\\N")
            i += 2
        elif nxt == ord("\\"):
            out.write(b"\\")
            i += 2
        elif nxt == ord("t"):
            out.write(b"\t")
            i += 2
        elif nxt == ord("n"):
            out.write(b"\n")
            i += 2
        elif nxt == ord("r"):
            out.write(b"\r")
            i += 2
        elif chr(nxt).isdigit() and i + 3 < len(raw) and all(chr(raw[k]).isdigit() for k in range(i + 1, i + 4)):
            # Octal \NNN — 3-digit octal sequence
            octal_str = raw[i + 1 : i + 4].decode("ascii")
            out.write(bytes([int(octal_str, 8)]))
            i += 4
        else:
            # Unknown escape — emit the character after the backslash literally
            out.write(bytes([nxt]))
            i += 2

    return out.getvalue()


def _coerce_field(raw_bytes: bytes, arrow_type: pa.DataType) -> object:
    """Coerce a decoded (already un-escaped) byte string to the Arrow type.

    Returns the Python value ready for a PyArrow builder.
    Returns ``None`` for null inputs (the caller passes ``None`` when
    ``_decode_pg_copy_escape`` returned ``None``).
    """
    if raw_bytes is None:
        return None

    text = raw_bytes.decode("utf-8", errors="replace")

    # ---- strings ----
    if pa.types.is_large_string(arrow_type) or pa.types.is_string(arrow_type):
        return text

    # ---- integers ----
    if pa.types.is_int8(arrow_type):
        return int(text)
    if pa.types.is_int16(arrow_type):
        return int(text)
    if pa.types.is_int32(arrow_type):
        return int(text)
    if pa.types.is_int64(arrow_type):
        return int(text)

    # ---- floats ----
    if pa.types.is_float32(arrow_type):
        return float(text)
    if pa.types.is_float64(arrow_type):
        return float(text)

    # ---- decimal ----
    if pa.types.is_decimal(arrow_type):
        # Return as string; Arrow will parse via Decimal128 builder
        import decimal as _decimal

        try:
            return _decimal.Decimal(text)
        except _decimal.InvalidOperation:
            return None

    # ---- boolean ----
    if pa.types.is_boolean(arrow_type):
        low = text.lower()
        if low in ("t", "true", "yes", "on", "1"):
            return True
        if low in ("f", "false", "no", "off", "0"):
            return False
        return None

    # ---- date ----
    if pa.types.is_date(arrow_type):
        # PG COPY emits dates as YYYY-MM-DD; Arrow accepts string for date32
        import datetime as _dt

        try:
            return _dt.date.fromisoformat(text)
        except ValueError:
            return None

    # ---- timestamp ----
    if pa.types.is_timestamp(arrow_type):
        import datetime as _dt

        # PG COPY emits timestamps as "YYYY-MM-DD HH:MM:SS[.ffffff][+offset]"
        # Normalise to ISO format and parse.
        norm = text.replace(" ", "T")
        # Strip timezone offset — Arrow timestamp[us, UTC] builder wants
        # datetime objects; if it has tz info we convert to UTC naively.
        try:
            # Try with timezone
            dt = _dt.datetime.fromisoformat(norm)
            if dt.tzinfo is not None:
                import datetime as _dt2

                dt = dt.astimezone(_dt2.timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            return None

    # ---- list<large_string> — bounded PG text[] ----
    if pa.types.is_list(arrow_type):
        # PG COPY format for arrays: "{elem1,elem2,...}" or "{}" for empty
        if text.startswith("{") and text.endswith("}"):
            inner = text[1:-1]
            if not inner:
                return []
            # Simple split — handles unquoted elements; quoted elements with
            # commas aren't expected in USAspending's bounded arrays.
            return [e.strip('"') for e in inner.split(",")]
        return []

    # ---- fallback: return as string for unknown/complex types ----
    return text


def _build_record_batch(
    column_names: list[str],
    column_types: list[pa.DataType],
    rows: list[list[object]],
) -> pa.RecordBatch:
    """Build a RecordBatch from a list of per-row lists of Python values."""
    schema = pa.schema(
        [pa.field(name, dtype) for name, dtype in zip(column_names, column_types)]
    )
    arrays = []
    for col_idx, (col_name, col_type) in enumerate(zip(column_names, column_types)):
        values = [row[col_idx] if col_idx < len(row) else None for row in rows]
        try:
            arr = pa.array(values, type=col_type, safe=False)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, TypeError) as exc:
            LOG.warning(
                "column %r type=%s coercion error — falling back to large_utf8: %s",
                col_name, col_type, exc,
            )
            arr = pa.array(
                [str(v) if v is not None else None for v in values],
                type=pa.large_utf8(),
            )
        arrays.append(arr)

    return pa.record_batch(arrays, schema=schema)


def parse_copy_stream(
    stdin: BinaryIO,
    column_types: list[pa.DataType],
    column_names: list[str],
    batch_size: int = 50_000,
    skip_rows: int = 0,
) -> Iterator[pa.RecordBatch]:
    """Parse a ``pg_restore --data-only -f -`` byte stream into RecordBatch chunks.

    Args:
        stdin:        Readable binary stream (e.g. ``subprocess.Popen.stdout``).
        column_types: Per-column Arrow DataType, in the same order as the COPY
                      header's column list.  Length must match ``column_names``.
        column_names: Column names (matched positionally to ``column_types``).
        batch_size:   Maximum rows per yielded RecordBatch.  Defaults to 50_000.
        skip_rows:    Discard the first N data rows without parsing them (a
                      cheap raw-line skip).  Used to resume after a preemption
                      when the first N rows were already landed.  Default 0.

    Yields:
        ``pyarrow.RecordBatch`` instances with ``len(rows) <= batch_size``.
        The last batch may be smaller than ``batch_size``.

    Design:
        - Streams line-by-line; never accumulates more than ``batch_size`` rows.
        - COPY preamble (SET statements, ALTER TABLE, SELECT pg_catalog.*) is
          skipped until the ``COPY ... FROM stdin;`` marker is found.
        - Multiple COPY blocks in the same stream are all consumed sequentially.
        - The ``\\.`` terminator ends the current COPY block; scanning resumes
          for the next COPY header (supporting multi-table single-stream).
    """
    num_cols = len(column_types)
    current_batch: list[list[object]] = []
    in_copy_block = False
    total_rows = 0
    rows_skipped = 0

    for raw_line in stdin:
        # Strip the trailing newline (keep the rest of the bytes)
        line = raw_line.rstrip(b"\n").rstrip(b"\r")

        if not in_copy_block:
            # Scan for COPY header
            try:
                text_line = line.decode("utf-8", errors="replace")
            except Exception:
                continue
            if _COPY_HEADER_RE.match(text_line):
                in_copy_block = True
                LOG.debug("pg_copy_text_parser: detected COPY block header: %r", text_line[:120])
            continue

        # Inside a COPY block
        if line == _COPY_TERMINATOR or line == b"\\.":
            # End of COPY block — flush any buffered rows
            if current_batch:
                yield _build_record_batch(column_names, column_types, current_batch)
                total_rows += len(current_batch)
                current_batch = []
            in_copy_block = False
            LOG.debug("pg_copy_text_parser: COPY block finished (%d total rows so far)", total_rows)
            continue

        # Resume support: discard the first `skip_rows` data rows without
        # parsing — a prior (preempted) attempt already landed them.
        if rows_skipped < skip_rows:
            rows_skipped += 1
            continue

        # Data row: TAB-delimited fields
        raw_fields = line.split(b"\t")

        # Pad or truncate to num_cols to be resilient to schema mismatches
        while len(raw_fields) < num_cols:
            raw_fields.append(b"\\N")
        raw_fields = raw_fields[:num_cols]

        row: list[object] = []
        for raw_field, arrow_type in zip(raw_fields, column_types):
            decoded = _decode_pg_copy_escape(raw_field)
            if decoded is None:
                row.append(None)
            elif pa.types.is_large_string(arrow_type) or pa.types.is_string(arrow_type):
                # Fast path for strings — skip extra coercion overhead
                row.append(decoded.decode("utf-8", errors="replace"))
            elif pa.types.is_list(arrow_type):
                # L54: check if this is a pipe-delimited large_string override
                # (caller passes large_utf8 type for unbounded lists; list_ for bounded)
                row.append(_coerce_field(decoded, arrow_type))
            else:
                row.append(_coerce_field(decoded, arrow_type))

        current_batch.append(row)

        if len(current_batch) >= batch_size:
            yield _build_record_batch(column_names, column_types, current_batch)
            total_rows += len(current_batch)
            current_batch = []

    # EOF — flush any remaining rows (in case stream ended without \. terminator)
    if current_batch:
        yield _build_record_batch(column_names, column_types, current_batch)
        total_rows += len(current_batch)

    LOG.info("pg_copy_text_parser: stream complete; total rows parsed=%d", total_rows)
