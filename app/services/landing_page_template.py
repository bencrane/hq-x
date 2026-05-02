"""Jinja2 template + theme variable substitution for hosted landing pages.

Pure rendering: takes a fully-resolved context dict (theme + content +
form schema + personalization tokens already applied) and returns the
HTML body. The template is single-column, mobile-first, with theme
variables injected into a small inline `<style>` block. No JS framework
in V1 — the form posts back to the same backend.

Why Jinja2 inline (string constant) rather than a templates/*.html file:
the V1 layout is small enough to read here, and ships as one file to
review. A future swap to a directory-based template loader is trivial
(point Environment(loader=…) at templates/) but would force this PR to
ship empty fixture files alongside the code.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, select_autoescape

_LANDING_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>{{ headline }}</title>
  <style>
    :root {
      --lp-bg: {{ theme.background_color or "#FFFFFF" }};
      --lp-text: {{ theme.text_color or "#222222" }};
      --lp-primary: {{ theme.primary_color or "#1A1A1A" }};
      --lp-secondary: {{ theme.secondary_color or "#888888" }};
      --lp-font: {{ theme.font_family or "system-ui" }}, -apple-system,
                 Segoe UI, Roboto, sans-serif;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0; padding: 0;
      background: var(--lp-bg);
      color: var(--lp-text);
      font-family: var(--lp-font);
      font-size: 16px;
      line-height: 1.5;
    }
    .lp-shell {
      max-width: 560px;
      margin: 0 auto;
      padding: 32px 20px 64px 20px;
    }
    .lp-logo { max-height: 56px; margin-bottom: 32px; }
    h1.lp-headline {
      margin: 0 0 16px 0;
      font-size: 28px;
      line-height: 1.2;
      color: var(--lp-text);
    }
    .lp-body {
      margin: 0 0 32px 0;
      font-size: 17px;
      color: var(--lp-text);
      white-space: pre-wrap;
    }
    .lp-form { display: flex; flex-direction: column; gap: 14px; }
    .lp-field { display: flex; flex-direction: column; gap: 6px; }
    .lp-field label {
      font-size: 14px;
      color: var(--lp-secondary);
    }
    .lp-field input,
    .lp-field select,
    .lp-field textarea {
      padding: 10px 12px;
      font-size: 16px;
      border: 1px solid var(--lp-secondary);
      border-radius: 6px;
      background: var(--lp-bg);
      color: var(--lp-text);
      font-family: inherit;
    }
    .lp-field textarea { min-height: 96px; resize: vertical; }
    .lp-cta {
      margin-top: 12px;
      padding: 14px 20px;
      border: none;
      border-radius: 6px;
      background: var(--lp-primary);
      color: #fff;
      font-size: 17px;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;
    }
    .lp-cta:hover { opacity: 0.92; }
    .lp-honeypot { position: absolute; left: -9999px; opacity: 0; height: 0; }
    {% if theme.custom_css %}
    /* operator-supplied custom CSS */
    {{ theme.custom_css | safe }}
    {% endif %}
  </style>
</head>
<body>
  <main class="lp-shell">
    {% if theme.logo_url %}
      <img class="lp-logo" src="{{ theme.logo_url }}" alt="">
    {% endif %}
    <h1 class="lp-headline">{{ headline }}</h1>
    <p class="lp-body">{{ body }}</p>

    {% if cta.type == "form" and cta.form_schema %}
    <form class="lp-form" method="post" action="{{ submit_url }}" autocomplete="on">
      {# Honeypot: visually hidden; bots fill it, humans don't. #}
      <input class="lp-honeypot" type="text" name="company_website"
             tabindex="-1" autocomplete="off">
      {% for field in cta.form_schema.fields %}
      <div class="lp-field">
        <label for="lp-f-{{ field.name }}">
          {{ field.label }}{% if field.required %} *{% endif %}
        </label>
        {% if field.type == "textarea" %}
          <textarea
            id="lp-f-{{ field.name }}"
            name="{{ field.name }}"
            {% if field.required %}required{% endif %}
            {% if field.placeholder %}placeholder="{{ field.placeholder }}"{% endif %}></textarea>
        {% elif field.type == "select" %}
          <select
            id="lp-f-{{ field.name }}"
            name="{{ field.name }}"
            {% if field.required %}required{% endif %}>
            {% for opt in (field.options or []) %}
              <option value="{{ opt.value }}">{{ opt.label }}</option>
            {% endfor %}
          </select>
        {% elif field.type == "checkbox" %}
          <input
            id="lp-f-{{ field.name }}"
            type="checkbox"
            name="{{ field.name }}"
            value="true"
            {% if field.required %}required{% endif %}>
        {% else %}
          <input
            id="lp-f-{{ field.name }}"
            type="{{ field.type }}"
            name="{{ field.name }}"
            {% if field.required %}required{% endif %}
            {% if field.placeholder %}placeholder="{{ field.placeholder }}"{% endif %}>
        {% endif %}
      </div>
      {% endfor %}
      <button class="lp-cta" type="submit">{{ cta.label }}</button>
    </form>
    {% elif cta.type == "external_url" and cta.target_url %}
      <a class="lp-cta" href="{{ cta.target_url }}"
         style="text-decoration:none; display:inline-block;">{{ cta.label }}</a>
    {% endif %}
  </main>
</body>
</html>
"""


_THANK_YOU_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Thanks</title>
  <style>
    :root {
      --lp-bg: {{ theme.background_color or "#FFFFFF" }};
      --lp-text: {{ theme.text_color or "#222222" }};
      --lp-font: {{ theme.font_family or "system-ui" }}, -apple-system,
                 Segoe UI, Roboto, sans-serif;
    }
    body {
      margin: 0; padding: 64px 20px;
      background: var(--lp-bg);
      color: var(--lp-text);
      font-family: var(--lp-font);
      text-align: center;
    }
    .ty { max-width: 480px; margin: 0 auto; font-size: 18px; line-height: 1.5; }
    {% if theme.custom_css %}{{ theme.custom_css | safe }}{% endif %}
  </style>
</head>
<body>
  <main class="ty">
    <p>{{ message }}</p>
  </main>
</body>
</html>
"""


_NOT_FOUND_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Not found</title>
  <style>
    body {
      margin: 0; padding: 64px 20px;
      background: {{ theme.background_color or "#FFFFFF" }};
      color: {{ theme.text_color or "#222222" }};
      font-family: {{ theme.font_family or "system-ui" }}, -apple-system, Segoe UI, sans-serif;
      text-align: center;
    }
    main { max-width: 480px; margin: 0 auto; }
  </style>
</head>
<body>
  <main>
    <h1>Page not found</h1>
    <p>That link is no longer active.</p>
  </main>
</body>
</html>
"""


def _build_env() -> Environment:
    env = Environment(
        autoescape=select_autoescape(default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


_env = _build_env()
_landing_template = _env.from_string(_LANDING_PAGE_TEMPLATE)
_thank_you_template = _env.from_string(_THANK_YOU_TEMPLATE)
_not_found_template = _env.from_string(_NOT_FOUND_TEMPLATE)


def render_landing_page_html(context: dict[str, Any]) -> str:
    """Render the landing page from a fully-resolved context.

    Required keys: `headline`, `body`, `cta`, `theme`, `submit_url`.
    All HTML in user-supplied strings is escaped (autoescape on).
    `theme.custom_css` rides through unsafe-marked because it's
    operator-supplied (validated at the brand-theme PUT boundary, max
    10 KB) and IS supposed to inject CSS.
    """
    return _landing_template.render(**context)


def render_thank_you_html(*, message: str, theme: dict[str, Any]) -> str:
    return _thank_you_template.render(message=message, theme=theme or {})


def render_not_found_html(*, theme: dict[str, Any] | None = None) -> str:
    return _not_found_template.render(theme=theme or {})


__all__ = [
    "render_landing_page_html",
    "render_not_found_html",
    "render_thank_you_html",
]
