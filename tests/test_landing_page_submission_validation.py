"""Pure tests for `validate_against_schema`."""

from __future__ import annotations

import pytest

from app.services.landing_page_submissions import (
    FormValidationError,
    validate_against_schema,
)


def _schema():
    return {
        "fields": [
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "email", "label": "Email", "type": "email", "required": True},
            {"name": "phone", "label": "Phone", "type": "tel", "required": False},
            {"name": "company", "label": "Company", "type": "text", "required": False},
        ]
    }


def test_clean_valid_form():
    clean, extras = validate_against_schema(
        form_data={"name": "Jane", "email": "jane@example.com"},
        form_schema=_schema(),
    )
    assert clean == {"name": "Jane", "email": "jane@example.com"}
    assert extras == {}


def test_lowercases_email():
    clean, _ = validate_against_schema(
        form_data={"name": "x", "email": "Foo@Bar.COM"},
        form_schema=_schema(),
    )
    assert clean["email"] == "foo@bar.com"


def test_strips_whitespace_on_text():
    clean, _ = validate_against_schema(
        form_data={"name": "  Jane  ", "email": "jane@example.com"},
        form_schema=_schema(),
    )
    assert clean["name"] == "Jane"


def test_required_missing_raises_with_field_map():
    with pytest.raises(FormValidationError) as exc:
        validate_against_schema(
            form_data={"name": "Jane"},  # email missing
            form_schema=_schema(),
        )
    assert exc.value.errors == {"email": "required"}


def test_required_empty_string_raises():
    with pytest.raises(FormValidationError) as exc:
        validate_against_schema(
            form_data={"name": "  ", "email": "jane@example.com"},
            form_schema=_schema(),
        )
    assert "name" in exc.value.errors


def test_invalid_email_raises():
    with pytest.raises(FormValidationError) as exc:
        validate_against_schema(
            form_data={"name": "Jane", "email": "not-an-email"},
            form_schema=_schema(),
        )
    assert exc.value.errors["email"] == "invalid email"


def test_invalid_phone_raises():
    with pytest.raises(FormValidationError) as exc:
        validate_against_schema(
            form_data={"name": "Jane", "email": "j@x.com", "phone": "??"},
            form_schema=_schema(),
        )
    assert "phone" in exc.value.errors


def test_optional_phone_blank_is_allowed():
    clean, _ = validate_against_schema(
        form_data={"name": "Jane", "email": "j@x.com", "phone": ""},
        form_schema=_schema(),
    )
    assert "phone" not in clean


def test_extras_quarantined_not_dropped():
    clean, extras = validate_against_schema(
        form_data={
            "name": "Jane",
            "email": "j@x.com",
            "stale_field": "from old form",
        },
        form_schema=_schema(),
    )
    assert clean == {"name": "Jane", "email": "j@x.com"}
    assert extras == {"stale_field": "from old form"}


def test_select_rejects_value_not_in_options():
    schema = {
        "fields": [
            {
                "name": "color",
                "label": "Color",
                "type": "select",
                "required": True,
                "options": [{"value": "red", "label": "Red"}, {"value": "blue", "label": "Blue"}],
            }
        ]
    }
    with pytest.raises(FormValidationError) as exc:
        validate_against_schema(
            form_data={"color": "green"},
            form_schema=schema,
        )
    assert exc.value.errors["color"] == "value not in options"


def test_checkbox_truthy_coercion():
    schema = {
        "fields": [
            {"name": "opt_in", "label": "Subscribe?", "type": "checkbox", "required": False}
        ]
    }
    clean, _ = validate_against_schema(form_data={"opt_in": "on"}, form_schema=schema)
    assert clean["opt_in"] is True
    clean, _ = validate_against_schema(form_data={"opt_in": "false"}, form_schema=schema)
    assert clean["opt_in"] is False


def test_url_field():
    schema = {
        "fields": [
            {"name": "homepage", "label": "Homepage", "type": "url", "required": True}
        ]
    }
    clean, _ = validate_against_schema(
        form_data={"homepage": "https://acme.com"}, form_schema=schema
    )
    assert clean["homepage"] == "https://acme.com"

    with pytest.raises(FormValidationError):
        validate_against_schema(form_data={"homepage": "acme.com"}, form_schema=schema)


def test_multiple_errors_aggregated():
    with pytest.raises(FormValidationError) as exc:
        validate_against_schema(
            form_data={"name": "", "email": "nope"},
            form_schema=_schema(),
        )
    assert "name" in exc.value.errors
    assert "email" in exc.value.errors
