"""Pure tests for the Lob audience CSV builder."""

from __future__ import annotations

import csv
import io

import pytest

from app.services.lob_audience_csv import (
    LOB_MERGE_VARIABLE_COLUMN_MAPPING,
    LOB_OPTIONAL_COLUMN_MAPPING,
    LOB_REQUIRED_COLUMN_MAPPING,
    AudienceRow,
    AudienceRowInvalid,
    build_audience_csv,
    validate_row,
)


def _row(**overrides) -> AudienceRow:
    base = {
        "recipient_name": "Acme Inc.",
        "primary_line": "123 Main St",
        "secondary_line": None,
        "city": "San Francisco",
        "state": "CA",
        "zip_code": "94103",
        "country": "US",
        "qr_code_redirect_url": "https://dub.sh/abc123",
    }
    base.update(overrides)
    return AudienceRow(**base)


def _parse(csv_bytes: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    return list(reader)


def test_build_csv_emits_header_and_rows():
    rows = [_row(), _row(recipient_name="Beta LLC", zip_code="10001", city="NYC")]
    out = build_audience_csv(rows)
    parsed = _parse(out)
    assert len(parsed) == 2
    assert parsed[0]["recipient_name"] == "Acme Inc."
    assert parsed[0]["primary_line"] == "123 Main St"
    assert parsed[0]["city"] == "San Francisco"
    assert parsed[0]["state"] == "CA"
    assert parsed[0]["zip_code"] == "94103"
    assert parsed[0]["qr_code_redirect_url"] == "https://dub.sh/abc123"
    assert parsed[1]["city"] == "NYC"


def test_build_csv_handles_optional_fields():
    """Secondary line, country, and QR URL are nullable; None → empty cell."""
    out = build_audience_csv(
        [
            _row(
                recipient_name="Solo",
                secondary_line=None,
                country=None,
                qr_code_redirect_url=None,
            )
        ]
    )
    parsed = _parse(out)
    assert parsed[0]["secondary_line"] == ""
    assert parsed[0]["country"] == ""
    assert parsed[0]["qr_code_redirect_url"] == ""


def test_build_csv_carries_secondary_line_when_present():
    out = build_audience_csv([_row(secondary_line="Suite 42")])
    parsed = _parse(out)
    assert parsed[0]["secondary_line"] == "Suite 42"


def test_build_csv_is_deterministic_for_same_input():
    """Same input rows in same order → identical CSV bytes."""
    rows = [_row(), _row(recipient_name="Beta LLC", zip_code="10001")]
    out1 = build_audience_csv(rows)
    out2 = build_audience_csv(rows)
    assert out1 == out2


def test_build_csv_empty_rows_raises():
    with pytest.raises(ValueError):
        build_audience_csv([])


@pytest.mark.parametrize(
    "field",
    ["recipient_name", "primary_line", "city", "state", "zip_code"],
)
def test_validate_row_rejects_missing_required_field(field):
    row = _row(**{field: ""})
    with pytest.raises(AudienceRowInvalid) as excinfo:
        validate_row(row)
    assert field in str(excinfo.value)


def test_validate_row_treats_whitespace_only_as_missing():
    with pytest.raises(AudienceRowInvalid):
        validate_row(_row(city="   "))


def test_validate_row_accepts_full_address():
    # No raise → we're good.
    validate_row(_row())


def test_build_csv_runs_validation_on_each_row():
    """A bad row in the middle of a list still aborts the whole build."""
    good = _row()
    bad = _row(zip_code="")
    with pytest.raises(AudienceRowInvalid):
        build_audience_csv([good, bad])


def test_column_mappings_align_with_header_columns():
    """Every CSV column referenced by the Lob mapping objects must
    actually be a real header in the emitted file. If we ever rename a
    header, this test forces us to update the mapping in lockstep."""
    out = build_audience_csv([_row()])
    headers = set(_parse(out)[0].keys())
    for csv_col in LOB_REQUIRED_COLUMN_MAPPING.values():
        assert csv_col in headers, csv_col
    for csv_col in LOB_OPTIONAL_COLUMN_MAPPING.values():
        assert csv_col in headers, csv_col
    for csv_col in LOB_MERGE_VARIABLE_COLUMN_MAPPING.values():
        assert csv_col in headers, csv_col
