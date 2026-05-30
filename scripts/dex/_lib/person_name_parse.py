"""Canonical structured person-name PARSER (faithful, opinion-free).

Distinct from ``person_name_normalize.py``: that module returns a lossy
``(last, first)`` canonical form (drops middle/suffix, substitutes nicknames
Bob→Robert) tuned for a specific bridge's match key. This module does the
opposite — it PARSES a name into all six structured components and keeps every
one of them, verbatim, with NO nickname substitution and NO component dropping.

Use this for spine bedrock where the goal is to store the name as filed and
leave opinionated matching (nickname expansion, suffix-insensitive joins) to
downstream consumers, reversibly.

Engine: ``nameparser.HumanName`` (MIT, deterministic, rule-based — no ML).
Handles the FEC "LAST, FIRST MIDDLE [SUFFIX]" comma convention natively.

Returns, per name:
  name_first, name_middle, name_last, name_suffix, name_title, name_nickname
      — verbatim parsed components (original casing preserved).
  name_last_key, name_first_key
      — deterministic JOIN-KEY variants: lower(strip_accents(...)) only.
        Case/accent folding preserves identity (fixes "José"/"Jose" cross-source
        capture); it is NOT an opinion. Nickname collapse is the opinion and is
        intentionally absent here.

``__version__`` bumps force a version bump on any consumer that stamps it
(spine/Polaris doc, match_method_versions). Keep pure + deterministic.
"""
from __future__ import annotations

import re
from typing import Final

from nameparser import HumanName  # type: ignore[import-untyped]

try:
    from unidecode import unidecode
except Exception:  # pragma: no cover - unidecode is a hard dep in prod images
    def unidecode(s: str) -> str:  # type: ignore[misc]
        return s

__version__: Final = "1.0.0"

_WS: Final = re.compile(r"\s+")

# Fixed component order — mirrored by the DuckDB join map in the spine builder.
COMPONENTS: Final = (
    "name_first",
    "name_middle",
    "name_last",
    "name_suffix",
    "name_title",
    "name_nickname",
)
KEYS: Final = ("name_last_key", "name_first_key")
FIELDS: Final = COMPONENTS + KEYS


def name_key(component: str | None) -> str | None:
    """Deterministic join-key form: accent-strip + lowercase + ws-collapse.

    Identity-preserving (no nickname/abbrev substitution). Returns None for
    empty input."""
    if not component:
        return None
    k = _WS.sub(" ", unidecode(component).lower().strip())
    return k or None


def parse_person_name(raw: str | None) -> dict[str, str | None]:
    """Parse a single name string into all six components + two join keys.

    Never raises on bad input — returns all-None for empty/non-str so it is
    safe to map over a column verbatim. Components keep original casing; keys
    are folded.

    "YANCEY, DELOS JR"   -> last=YANCEY first=DELOS suffix=JR  (keys: yancey/delos)
    "MOORE, JAMES A"     -> last=MOORE first=JAMES middle=A
    "DEGE, BOB"          -> first=BOB  (NOT substituted to Robert)
    """
    empty = {f: None for f in FIELDS}
    if not raw or not isinstance(raw, str):
        return empty
    s = raw.strip()
    if not s:
        return empty
    h = HumanName(s)
    first = h.first or None
    middle = h.middle or None
    last = h.last or None
    return {
        "name_first": first,
        "name_middle": middle,
        "name_last": last,
        "name_suffix": h.suffix or None,
        "name_title": h.title or None,
        "name_nickname": h.nickname or None,
        "name_last_key": name_key(last),
        "name_first_key": name_key(first),
    }


def parse_to_tuple(raw: str | None) -> tuple[str | None, ...]:
    """Same as parse_person_name but returns a fixed-order tuple (FIELDS order).

    Module-level + picklable for multiprocessing.Pool over distinct names."""
    d = parse_person_name(raw)
    return tuple(d[f] for f in FIELDS)
