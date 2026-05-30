"""Shared physical-address (street) normalizer for cross-source address+zip+state bridges.

Sibling to `entity_name_normalize.py`. Produces a stable join key for matching
US physical addresses across heterogeneous sources:

  - SAM `physical_address_line_1` (+ `_line_2`)            — USPS-shape, already abbreviated
  - SBA 7a/504 `borrstreet` (+ raw 7a/504 parquets)        — mixed case, mixed format
  - SBA PPP `borrower_address`                              — mixed case, mixed format
  - Overture `address_freeform`                             — mixed; usually street-only
                                                              but can include city/state/zip
  - PDL `location_street_address`                           — mixed
  - generic CSV ingests                                     — anything

The normalizer is conservative: it lowercases, applies USPS suffix and
directional abbreviations, drops common unit prefixes (STE/APT/UNIT/FL/#…)
*for the BASE form*, normalizes "PO BOX" variants, strips trailing
", city, ST zip" tails that contaminate freeform fields, and rejects empty /
single-word / generic-string results.

Per the L31 normalizer-versioning rule: this module exposes `__version__`.
Bumping the constant forces a version bump on any consumer's
`match_method_versions` row.

Per L34: bridge generators should call `register_address_udf(con)` to expose
the same Python rule as a DuckDB UDF. The SAME logic Python applies row-wise
runs inside DuckDB SQL — no "SQL-approximation of the Python rule" drift.

Two outputs from each input:
  - `address_full_normalized`  : retains unit info (STE 200, APT B, #4)
  - `address_base_normalized`  : strips unit info — recommended for joins
                                  because unit numbers diverge between
                                  registered address (SAM) and operating
                                  storefront (Overture) for the same entity.

Examples (raw → base):
  "123 Main Street, Suite 200"                  → "123 main st"
  "123 MAIN ST STE 200"                         → "123 main st"
  "123 main st #200, chicago, il 60601"         → "123 main st"
  "P.O. Box 4567"                               → "po box 4567"
  "Post Office Box 4567"                        → "po box 4567"
  "501 Maryland Ave"                            → "501 maryland ave"
  "ONE WORLD TRADE CTR FL 100"                  → "1 world trade ctr"
  "highway 27 north"                            → "hwy 27 n"
  None / "" / "RETIRED" / "N/A"                 → None
"""

from __future__ import annotations

import re
from typing import Optional

__version__ = "1.0.0"


# ---------------------------------------------------------------------------
# USPS Publication 28 street-suffix abbreviations (long → short)
# ---------------------------------------------------------------------------
_SUFFIX_MAP = {
    "alley": "aly", "annex": "anx", "arcade": "arc", "avenue": "ave", "ave.": "ave",
    "bayou": "byu", "beach": "bch", "bend": "bnd", "bluff": "blf", "bottom": "btm",
    "boulevard": "blvd", "blvd.": "blvd", "branch": "br", "bridge": "brg",
    "brook": "brk", "brooks": "brks", "burg": "bg", "burgs": "bgs", "bypass": "byp",
    "camp": "cp", "canyon": "cyn", "cape": "cpe", "causeway": "cswy", "center": "ctr",
    "centers": "ctrs", "circle": "cir", "circles": "cirs", "cliff": "clf",
    "cliffs": "clfs", "club": "clb", "common": "cmn", "commons": "cmns", "corner": "cor",
    "corners": "cors", "course": "crse", "court": "ct", "courts": "cts",
    "cove": "cv", "coves": "cvs", "creek": "crk", "crescent": "cres", "crest": "crst",
    "crossing": "xing", "crossroad": "xrd", "crossroads": "xrds", "curve": "curv",
    "dale": "dl", "dam": "dm", "divide": "dv", "drive": "dr", "drives": "drs",
    "estate": "est", "estates": "ests", "expressway": "expy", "extension": "ext",
    "extensions": "exts", "fall": "fall", "falls": "fls", "ferry": "fry", "field": "fld",
    "fields": "flds", "flat": "flt", "flats": "flts", "ford": "frd", "fords": "frds",
    "forest": "frst", "forge": "frg", "forges": "frgs", "fork": "frk", "forks": "frks",
    "fort": "ft", "freeway": "fwy", "garden": "gdn", "gardens": "gdns", "gateway": "gtwy",
    "glen": "gln", "glens": "glns", "green": "grn", "greens": "grns", "grove": "grv",
    "groves": "grvs", "harbor": "hbr", "harbors": "hbrs", "haven": "hvn",
    "heights": "hts", "highway": "hwy", "hill": "hl", "hills": "hls", "hollow": "holw",
    "inlet": "inlt", "island": "is", "islands": "iss", "isle": "isle", "junction": "jct",
    "junctions": "jcts", "key": "ky", "keys": "kys", "knoll": "knl", "knolls": "knls",
    "lake": "lk", "lakes": "lks", "land": "land", "landing": "lndg", "lane": "ln",
    "light": "lgt", "lights": "lgts", "loaf": "lf", "lock": "lck", "locks": "lcks",
    "lodge": "ldg", "loop": "loop", "mall": "mall", "manor": "mnr", "manors": "mnrs",
    "meadow": "mdw", "meadows": "mdws", "mews": "mews", "mill": "ml", "mills": "mls",
    "mission": "msn", "motorway": "mtwy", "mount": "mt", "mountain": "mtn",
    "mountains": "mtns", "neck": "nck", "orchard": "orch", "oval": "oval",
    "overpass": "opas", "park": "park", "parks": "park", "parkway": "pkwy",
    "parkways": "pkwy", "pass": "pass", "passage": "psge", "path": "path", "pike": "pike",
    "pine": "pne", "pines": "pnes", "place": "pl", "plain": "pln", "plains": "plns",
    "plaza": "plz", "point": "pt", "points": "pts", "port": "prt", "ports": "prts",
    "prairie": "pr", "radial": "radl", "ranch": "rnch", "rapid": "rpd", "rapids": "rpds",
    "rest": "rst", "ridge": "rdg", "ridges": "rdgs", "river": "riv", "road": "rd",
    "roads": "rds", "route": "rte", "row": "row", "rue": "rue", "run": "run",
    "shoal": "shl", "shoals": "shls", "shore": "shr", "shores": "shrs", "skyway": "skwy",
    "spring": "spg", "springs": "spgs", "spur": "spur", "square": "sq", "squares": "sqs",
    "station": "sta", "stream": "strm", "street": "st", "streets": "sts", "summit": "smt",
    "terrace": "ter", "throughway": "trwy", "trace": "trce", "track": "trak",
    "trafficway": "trfy", "trail": "trl", "trailer": "trlr", "tunnel": "tunl",
    "turnpike": "tpke", "underpass": "upas", "union": "un", "unions": "uns",
    "valley": "vly", "valleys": "vlys", "viaduct": "via", "view": "vw", "views": "vws",
    "village": "vlg", "villages": "vlgs", "ville": "vl", "vista": "vis", "walk": "walk",
    "walks": "walks", "wall": "wall", "way": "way", "ways": "ways", "well": "wl",
    "wells": "wls",
    # Already-abbreviated forms (idempotency)
    "st.": "st", "rd.": "rd", "dr.": "dr", "ct.": "ct", "ln.": "ln", "pl.": "pl",
    "ter.": "ter", "cir.": "cir", "ctr.": "ctr",
}

_DIRECTIONAL_MAP = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "n.": "n", "s.": "s", "e.": "e", "w.": "w",
    "ne.": "ne", "nw.": "nw", "se.": "se", "sw.": "sw",
}

_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

_UNIT_KEYWORDS = {
    "ste", "suite", "apt", "apartment", "unit", "fl", "floor", "bldg", "building",
    "rm", "room", "lot", "trlr", "trailer", "#",
}

_REJECT_STRINGS = {
    "", "n/a", "na", "none", "null", "tbd", "unknown", "redacted",
    "not provided", "not applicable", "see above", "same as above",
    "retired", "self-employed", "self employed", "homeless",
}

_RE_POBOX = re.compile(
    r"\b(?:p\.?\s*o\.?|post\s+office)\s*\.?\s*box\s*#?\s*([0-9a-z\-]+)",
    re.IGNORECASE,
)
_RE_CITY_STATE_ZIP_TAIL = re.compile(
    r",\s*[a-z][\w\s\.-]+,?\s*[a-z]{2}\s*\d{5}(?:-\d{4})?\s*$",
    re.IGNORECASE,
)
_RE_PUNCT = re.compile(r"[,;:\\\"()\[\]]")
_RE_HASH_NUM = re.compile(r"#\s*([a-z0-9\-]+)", re.IGNORECASE)
_RE_WHITESPACE = re.compile(r"\s+")


def _strip_unit(tokens: list[str]) -> list[str]:
    """Drop unit-indicator tokens and their immediately following token.

    "123 main st ste 200" -> ["123","main","st"]
    "123 main st apt b"   -> ["123","main","st"]
    "123 main st #200"    -> ["123","main","st"] (the #200 is handled upstream)
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in _UNIT_KEYWORDS:
            # Drop this token + the next one (the unit identifier) if it looks alnum.
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def _normalize_tokens(tokens: list[str]) -> list[str]:
    """Apply suffix/directional/number-word standardization.

    USPS street-suffix abbreviation is applied ONLY to the last token (or to
    a second-to-last position if the actual last token is a directional like
    "n"/"s"/"e"/"w") so that "MAIN ST" → "main st" but "UNION ST" stays as
    "union st" (UNION is the street name, not a suffix). Directional
    abbreviation applies anywhere (pre-directional like "N MAIN ST" and
    post-directional like "MAIN ST N" are both common). Number-word
    substitution applies only at the very first position ("ONE WORLD" →
    "1 world").
    """
    n = len(tokens)
    out: list[str] = list(tokens)
    # Number-word at position 0 only.
    if n > 0 and out[0] in _NUMBER_WORDS:
        out[0] = _NUMBER_WORDS[out[0]]
    # Directional anywhere.
    for i in range(n):
        if out[i] in _DIRECTIONAL_MAP:
            out[i] = _DIRECTIONAL_MAP[out[i]]
    # Suffix at last position only (or second-to-last if last is a directional).
    if n >= 1:
        last_idx = n - 1
        # If the very-last token is a directional (post-suffix), the suffix
        # is one slot earlier: "MAIN ST N" → suffix is "st", last is "n".
        if n >= 2 and out[last_idx] in {"n", "s", "e", "w", "ne", "nw", "se", "sw"}:
            suffix_idx = last_idx - 1
        else:
            suffix_idx = last_idx
        if out[suffix_idx] in _SUFFIX_MAP:
            out[suffix_idx] = _SUFFIX_MAP[out[suffix_idx]]
    return out


def normalize_address_street(raw: Optional[str], *, keep_unit: bool = False) -> Optional[str]:
    """Canonicalize a US physical-address street string.

    Returns lowercase USPS-abbreviated form; drops unit info by default
    (keep_unit=False) for permissive cross-source matching. Returns None for
    empty / generic / unparseable input.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in _REJECT_STRINGS:
        return None

    # PO Box normalization (handle BEFORE punctuation strip so the regex matches).
    m = _RE_POBOX.search(s)
    if m:
        return f"po box {m.group(1).strip().lower()}"

    # Strip city/state/zip tail that occasionally rides along on freeform fields.
    s = _RE_CITY_STATE_ZIP_TAIL.sub("", s)

    # Standardize # → "# " so it tokenizes as its own keyword.
    s = _RE_HASH_NUM.sub(r"# \1", s)

    # Punctuation → space (keep dashes inside tokens like "23-45")
    s = _RE_PUNCT.sub(" ", s)
    s = s.replace(".", " ")

    tokens = [t for t in _RE_WHITESPACE.split(s) if t]
    if not tokens:
        return None

    # Strip unit first so that suffix-abbrev sees the correct final token
    # ("main street, suite 200" → drop unit → ["main","street"] → "main st").
    # When keep_unit=True, we *do* want unit tokens, but we apply suffix
    # logic over the unit-stripped view to keep the underlying street form
    # stable, then re-append the original unit tail.
    if keep_unit:
        unit_idx = next(
            (i for i, t in enumerate(tokens) if t in _UNIT_KEYWORDS),
            None,
        )
        base_tokens = tokens[:unit_idx] if unit_idx is not None else tokens
        unit_tail = tokens[unit_idx:] if unit_idx is not None else []
        normalized_base = _normalize_tokens(base_tokens)
        tokens = normalized_base + unit_tail
    else:
        tokens = _strip_unit(tokens)
        tokens = _normalize_tokens(tokens)
    if not tokens:
        return None

    result = " ".join(tokens).strip()
    if not result or result in _REJECT_STRINGS:
        return None
    # Reject pure-numeric or single-token-non-pobox results — too generic
    if len(tokens) < 2 and not result.startswith("po box "):
        return None
    return result


def normalize_address_full(raw: Optional[str]) -> Optional[str]:
    """Same as `normalize_address_street` but retains unit/suite/apt info."""
    return normalize_address_street(raw, keep_unit=True)


def join_sam_line_1_2(line_1: Optional[str], line_2: Optional[str]) -> Optional[str]:
    """SAM splits address into line_1 + line_2. Join into a single string."""
    parts = [p for p in (line_1, line_2) if p and p.strip()]
    if not parts:
        return None
    return " ".join(parts).strip()


def join_address_lines(*lines: Optional[str]) -> Optional[str]:
    """Join N optional address line strings (line_1, line_2, line_3, …) into one.

    Generic N-arity sibling of `join_sam_line_1_2`. Use for sources that split
    street addresses across 2 or 3 fields (UCC CA debtors/secured-parties:
    ADDR1+ADDR2+ADDR3; SoS CA principals: address1+address2+address3; SoS CA
    mailing: mailing_address+mailing_address2+mailing_address3; etc.). Returns
    None when every part is empty/whitespace.
    """
    parts = [p for p in lines if p and p.strip()]
    if not parts:
        return None
    return " ".join(parts).strip()


def register_address_udf(con, fn_name: str = "py_normalize_address_street") -> None:
    """Register the normalizer as a DuckDB UDF for in-SQL use.

    Usage in a bridge generator:
        from scripts._lib.address_normalize import register_address_udf
        register_address_udf(con)
        con.execute("SELECT py_normalize_address_street(borrstreet) FROM ...")
    """
    def _udf(raw):
        return normalize_address_street(raw)
    try:
        con.create_function(
            fn_name, _udf, ["VARCHAR"], "VARCHAR", null_handling="special",
        )
    except Exception:
        try:
            con.remove_function(fn_name)
        except Exception:
            pass
        con.create_function(
            fn_name, _udf, ["VARCHAR"], "VARCHAR", null_handling="special",
        )
