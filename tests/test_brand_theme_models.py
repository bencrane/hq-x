"""Pure tests for the BrandTheme + StepLandingPageConfig validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.brands import BrandTheme
from app.models.campaigns import (
    FormField,
    FormSchema,
    LandingPageCta,
    StepLandingPageConfig,
)

# ── BrandTheme ────────────────────────────────────────────────────────────


def test_theme_accepts_full_config():
    t = BrandTheme(
        logo_url="https://cdn.acme.com/logo.png",
        primary_color="#FF6B35",
        secondary_color="#1A1A1A",
        background_color="#FFFFFF",
        text_color="#222222",
        font_family="Inter",
        custom_css=None,
    )
    assert t.primary_color == "#FF6B35"


def test_theme_accepts_empty():
    """All fields are optional — operator can leave the column empty."""
    BrandTheme()


def test_theme_rejects_http_logo():
    with pytest.raises(ValidationError) as exc:
        BrandTheme(logo_url="http://cdn.acme.com/logo.png")
    assert "https://" in str(exc.value)


def test_theme_rejects_3_digit_hex():
    with pytest.raises(ValidationError):
        BrandTheme(primary_color="#FFF")


def test_theme_rejects_named_color():
    with pytest.raises(ValidationError):
        BrandTheme(primary_color="red")


def test_theme_rejects_unknown_font():
    with pytest.raises(ValidationError):
        BrandTheme(font_family="ComicSans")


def test_theme_rejects_oversized_custom_css():
    big = "a" * (10 * 1024 + 1)
    with pytest.raises(ValidationError) as exc:
        BrandTheme(custom_css=big)
    assert "exceeds" in str(exc.value).lower()


def test_theme_accepts_max_size_custom_css():
    BrandTheme(custom_css="a" * (10 * 1024))


def test_theme_rejects_extra_field():
    with pytest.raises(ValidationError):
        BrandTheme.model_validate(
            {"primary_color": "#FF0000", "unknown_field": "x"}
        )


# ── FormField / FormSchema ───────────────────────────────────────────────


def test_form_field_accepts_minimal():
    f = FormField(name="email", label="Email", type="email")
    assert f.required is False


def test_form_field_rejects_uppercase_name():
    with pytest.raises(ValidationError):
        FormField(name="Email", label="Email", type="email")


def test_form_field_rejects_leading_digit_name():
    with pytest.raises(ValidationError):
        FormField(name="1email", label="Email", type="email")


def test_form_field_rejects_unknown_type():
    with pytest.raises(ValidationError):
        FormField(name="x", label="X", type="signature")  # type: ignore[arg-type]


def test_form_schema_rejects_empty_fields():
    with pytest.raises(ValidationError):
        FormSchema(fields=[])


def test_form_schema_rejects_duplicate_names():
    with pytest.raises(ValidationError) as exc:
        FormSchema(
            fields=[
                FormField(name="email", label="A", type="email"),
                FormField(name="email", label="B", type="email"),
            ]
        )
    assert "duplicate" in str(exc.value).lower()


def test_form_schema_caps_at_20_fields():
    fields = [
        FormField(name=f"f{i}", label=f"F{i}", type="text") for i in range(21)
    ]
    with pytest.raises(ValidationError):
        FormSchema(fields=fields)


# ── LandingPageCta + StepLandingPageConfig ──────────────────────────────


def test_cta_form_requires_form_schema():
    with pytest.raises(ValidationError) as exc:
        LandingPageCta(type="form", label="Confirm")
    assert "form_schema" in str(exc.value)


def test_cta_external_url_requires_target_url():
    with pytest.raises(ValidationError) as exc:
        LandingPageCta(type="external_url", label="Visit")
    assert "target_url" in str(exc.value)


def test_cta_form_with_schema_ok():
    schema = FormSchema(fields=[FormField(name="email", label="Email", type="email")])
    cta = LandingPageCta(
        type="form",
        label="Confirm",
        form_schema=schema,
        thank_you_message="Thanks!",
    )
    assert cta.form_schema is not None


def test_step_landing_page_config_full():
    schema = FormSchema(
        fields=[
            FormField(name="name", label="Name", type="text", required=True),
            FormField(name="email", label="Email", type="email", required=True),
        ]
    )
    cfg = StepLandingPageConfig(
        headline="Hi {recipient.display_name}",
        body="Welcome.",
        cta=LandingPageCta(
            type="form", label="Confirm", form_schema=schema
        ),
    )
    assert cfg.cta.form_schema is not None


def test_step_landing_page_config_caps_headline():
    with pytest.raises(ValidationError):
        StepLandingPageConfig(
            headline="x" * 501,
            body="ok",
            cta=LandingPageCta(
                type="form",
                label="Confirm",
                form_schema=FormSchema(
                    fields=[FormField(name="email", label="Email", type="email")]
                ),
            ),
        )


def test_step_landing_page_config_caps_body():
    with pytest.raises(ValidationError):
        StepLandingPageConfig(
            headline="hi",
            body="x" * 2001,
            cta=LandingPageCta(
                type="form",
                label="Confirm",
                form_schema=FormSchema(
                    fields=[FormField(name="email", label="Email", type="email")]
                ),
            ),
        )


def test_step_landing_page_config_rejects_extra():
    with pytest.raises(ValidationError):
        StepLandingPageConfig.model_validate(
            {
                "headline": "hi",
                "body": "ok",
                "cta": {
                    "type": "form",
                    "label": "x",
                    "form_schema": {
                        "fields": [
                            {"name": "email", "label": "E", "type": "email"}
                        ]
                    },
                },
                "unknown": "x",
            }
        )
