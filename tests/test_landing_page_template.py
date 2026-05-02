"""Pure tests for the Jinja2 landing-page template + personalization."""

from __future__ import annotations

from app.services.landing_page_render import apply_personalization
from app.services.landing_page_template import (
    render_landing_page_html,
    render_not_found_html,
    render_thank_you_html,
)

# ── apply_personalization ────────────────────────────────────────────────


def test_personalization_substitutes_recipient_token():
    out = apply_personalization(
        "Hi {recipient.display_name}, welcome.",
        {"recipient": {"display_name": "Jane"}},
    )
    assert out == "Hi Jane, welcome."


def test_personalization_handles_nested_path():
    out = apply_personalization(
        "We picked you in {recipient.mailing_address.city}.",
        {"recipient": {"mailing_address": {"city": "Brooklyn"}}},
    )
    assert "Brooklyn" in out


def test_personalization_missing_token_renders_empty():
    out = apply_personalization(
        "Hello, {recipient.display_name}!",
        {"recipient": {}},
    )
    assert out == "Hello, !"


def test_personalization_unknown_namespace_renders_empty():
    out = apply_personalization(
        "Foo {weird.path} bar",
        {"recipient": {"display_name": "x"}},
    )
    assert out == "Foo  bar"


def test_personalization_leaves_non_token_braces_alone():
    out = apply_personalization(
        "JSON: {key: value} and {a}",
        {"recipient": {}},
    )
    # Single-segment braces aren't tokens; left as-is.
    assert "{key: value}" in out
    assert "{a}" in out


def test_personalization_handles_none_value():
    out = apply_personalization(
        "Hi {recipient.display_name}",
        {"recipient": {"display_name": None}},
    )
    assert out == "Hi "


def test_personalization_empty_input_returns_empty():
    assert apply_personalization("", {}) == ""
    assert apply_personalization(None, {}) == ""  # type: ignore[arg-type]


# ── render_landing_page_html ─────────────────────────────────────────────


def _ctx_with_form() -> dict:
    return {
        "headline": "Hi Jane",
        "body": "Welcome.",
        "cta": {
            "type": "form",
            "label": "Confirm",
            "form_schema": {
                "fields": [
                    {"name": "name", "label": "Name", "type": "text", "required": True},
                    {"name": "email", "label": "Email", "type": "email", "required": True},
                ]
            },
        },
        "theme": {"primary_color": "#FF6B35", "font_family": "Inter"},
        "submit_url": "/lp/abc/xyz/submit",
    }


def test_render_includes_headline_and_body():
    html = render_landing_page_html(_ctx_with_form())
    assert "Hi Jane" in html
    assert "Welcome." in html


def test_render_emits_form_inputs_for_each_field():
    html = render_landing_page_html(_ctx_with_form())
    assert 'name="name"' in html
    assert 'name="email"' in html
    assert 'type="email"' in html
    assert 'required' in html


def test_render_includes_honeypot_field():
    html = render_landing_page_html(_ctx_with_form())
    assert "company_website" in html
    assert "lp-honeypot" in html


def test_render_substitutes_theme_variables():
    html = render_landing_page_html(_ctx_with_form())
    assert "#FF6B35" in html
    assert "Inter" in html


def test_render_with_external_url_cta_uses_anchor():
    ctx = _ctx_with_form()
    ctx["cta"] = {
        "type": "external_url",
        "label": "Visit",
        "target_url": "https://acme.com/promo",
    }
    html = render_landing_page_html(ctx)
    assert 'href="https://acme.com/promo"' in html
    assert "<form" not in html


def test_render_escapes_html_in_user_strings():
    """Pydantic validation forbids most special characters, but the
    render path still needs to escape what slips through."""
    ctx = _ctx_with_form()
    ctx["headline"] = '<script>alert("xss")</script>'
    html = render_landing_page_html(ctx)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_includes_logo_when_supplied():
    ctx = _ctx_with_form()
    ctx["theme"] = {"logo_url": "https://cdn.acme.com/logo.png"}
    html = render_landing_page_html(ctx)
    assert "https://cdn.acme.com/logo.png" in html


def test_render_omits_logo_when_missing():
    html = render_landing_page_html(_ctx_with_form())
    assert "<img" not in html


def test_render_thank_you_renders_message():
    html = render_thank_you_html(message="Thanks!", theme={"primary_color": "#000000"})
    assert "Thanks!" in html


def test_render_not_found_returns_html():
    html = render_not_found_html(theme={"primary_color": "#000000"})
    assert "Page not found" in html
    assert "<html" in html
