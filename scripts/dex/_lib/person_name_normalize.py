"""Shared person-name normalizer for cross-source person+state bridges.

Per L35 (lessons learned): person-name normalization is SEPARATE from the
entity-name normalizer in `_lib/entity_name_normalize.py`. Person names need
first/last/middle/suffix parsing, nickname collapse (Bob→Robert, Bill→William),
accent stripping, and a person-specific blacklist — all of which would corrupt
entity-name handling.

Per L31: this module exposes `__version__`. Bumping the constant forces a
version bump on consumer `match_method_versions` rows. Provenance drift
between versions is tracked through the bridge_run → match_method_version
chain.

Per L33 (applied to person names): generic non-person strings ("anonymous",
"unknown", "test test", role placeholders) return None. These appear in
public donor / officer fields and produce massive false-positive cardinality
if matched naively.

The normalizer stack lifts three established public Python libraries:

  - `nameparser==1.1.3` — rule-based personal-name parser (handles
    "Last, First Middle Suffix" comma format, suffix recognition, title
    stripping, compound surname preservation). MIT licensed, deterministic.
  - `unidecode==1.3.8` — Unicode → ASCII transliteration (accent stripping).
  - Carlton Northern's `nicknames` CSV (vendored at pinned upstream SHA
    `__nicknames_csv_sha__`) — ~2.7K nickname↔formal-name relationship
    triples, used for first-name canonicalization (Bob → Robert).

We DO NOT use `probablepeople` (the other commonly-cited library): it uses a
CRF (conditional random field) ML model. Non-deterministic across model
versions; "the model decided" doesn't survive a partner refund audit.

Normalization rule:
  1. Strip whitespace; reject empty.
  2. For `normalize_person_name(raw)`: pass to `nameparser.HumanName`. Extract
     first / last (suffix discarded; middle discarded — see "v1.0.0 design
     notes" below).
  3. For both APIs: lowercase each component. Apply `unidecode` to strip
     accents.
  4. Collapse internal whitespace.
  5. Strip non-alphanumeric except apostrophes and hyphens (keep `O'Brien`,
     `Smith-Jones`).
  6. Look up first name in `NICKNAME_TO_FORMAL`. If found, replace with the
     formal form.
  7. Apply `PERSON_NAME_BLACKLIST` and length-based rejection rules. Return
     None on rejection, else `(last_normalized, first_normalized)`.

DuckDB exposure (per L34): bridge generators register the wrapper
`py_normalize_person_name_for_duckdb` as a UDF returning `"last|first"`
pipe-delimited strings. The bridge SQL splits on `|` to recover both
components. Pipe-delimiting is simpler than DuckDB struct UDFs.

v1.0.0 design notes:
  - Output shape is `(last, first)`. Last is more selective; downstream
    bridges index on `last` first, then narrow by `first` within `last`.
  - Middle name / initial is intentionally NOT part of the canonical form.
    Cross-source middle-initial inconsistency (FEC: "ROBERT J SMITH" vs.
    990: "Robert James Smith") would over-fragment matches.
  - Suffixes (Jr., Sr., II, III, MD, etc.) are detected by `nameparser` and
    discarded from the canonical form for the same reason.
  - Honorifics (Dr., Hon., Prof., etc.) are detected by `nameparser` and
    discarded.
  - Cross-language person-name parsing beyond Latin alphabets (CJK, Arabic,
    etc.) is out of scope. The corpus we care about (FEC, IRS 990, SEC
    EDGAR, USAspending) is overwhelmingly Latin-alphabet US-domestic.

Nickname-dict construction:
  Carlton Northern's CSV is intentionally bidirectional — each pair appears
  in both directions because casual usage flows both ways. For our
  cross-source matching, we need a deterministic nickname → canonical map.

  Algorithm:
    a. Read all `(name1, has_nickname, name2)` triples.
    b. Directional cleanup: for any pair where both `(a,b)` and `(b,a)`
       exist, keep only the direction where `len(a) > len(b)` — single-
       direction pairs in the source data show a strong signal that the
       canonical (name1) is on average ~2 characters longer than the
       nickname (name2), so longer-as-canonical is the high-precision rule.
    c. For each nickname, choose canonical by tiebreak:
       (zero-in-degree-of-canonical preferred, then max out-degree of
       canonical, then longest, then alphabetical-min).
    d. Apply `MANUAL_NICKNAME_OVERRIDES` for ~30 well-known American
       nicknames where step (c) gets it wrong (e.g. genealogy noise produces
       `dick → melchizedek`; we override to `dick → richard`).
    e. Remove any entries where the formal IS itself a canonical American
       given name (would otherwise produce e.g. `william → wilma` from
       feminine-variant noise).
"""

from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import Final

from nameparser import HumanName  # type: ignore[import-untyped]
from unidecode import unidecode


__version__: Final = "1.1.0"

# Pinned upstream commit SHA for the bundled `nicknames.csv`. Source:
# https://github.com/carltonnorthern/nicknames/blob/<sha>/names.csv
__nicknames_csv_sha__: Final = "3d450a015c39a79c305fb9d447c9adeb7dfdace0"

_DATA_DIR: Final = Path(__file__).resolve().parent / "data"
_NICKNAMES_CSV_PATH: Final = _DATA_DIR / "nicknames.csv"


# Per L33: generic non-person strings. These appear in FEC donor records,
# IRS 990 officer-name rows, and SEC EDGAR insider filings whenever the
# field is missing or stubbed. Matching against them produces massive
# false-positive cardinality. Match is applied AFTER lowercasing + punct
# stripping, so "N/A", "n/a", "N / A" all collapse to "n a" first.
PERSON_NAME_BLACKLIST: Final[frozenset[str]] = frozenset({
    "",
    "anonymous",
    "unknown",
    "n a",  # "N/A" → "n a" after punct strip
    "na",
    "none",
    "null",
    "test",
    "test test",
    "test test test",
    "various",
    "donor",
    "contributor",
    "shareholder",
    "manager",
    "director",
    "officer",
    "owner",
    "trustee",
    "trustees",
    "president",
    "secretary",
    "treasurer",
    "chairman",
    "chairperson",
    "ceo",
    "cfo",
    "cto",
    "info requested",
    "information requested",
    "requested",
    "not provided",
    "withheld",
    "redacted",
})

# Single-character first or last name → reject. Two-character names with
# this whitelist still match (Al, Bo, Ed, Jo, Ty, etc.).
_SHORT_NAME_WHITELIST: Final[frozenset[str]] = frozenset({
    "al", "bo", "cy", "ed", "jo", "ty", "lu", "su", "li",
    "vi", "yi", "an", "el", "in",
})


# Manual overrides for the ~30 most-common American nicknames where the
# Carlton Northern genealogy data's bidirectional structure produces
# undesirable canonicals through the algorithmic rule. Documented below per
# entry; final dict is applied after auto-build.
MANUAL_NICKNAME_OVERRIDES: Final[dict[str, str]] = {
    # m  →  William family — bidirectional bill↔william + sibling robert/will/willis
    # cause max-out-degree to land on robert; force to william (modern American
    # default for "Bill" / "Billy").
    "bill": "william",
    "billy": "william",
    "willy": "william",
    # Robert family — clean up rob/robby and force to robert (algorithm picks
    # roberto for "rob" because roberto is one char longer; not what we want).
    "rob": "robert",
    "robby": "robert",
    # Richard family — algorithm picks "melchizedek" for "dick" via raw-length
    # rule (melchizedek really is in the genealogy data).
    "dick": "richard",
    "rick": "richard",
    "ricky": "richard",
    "rich": "richard",
    "richie": "richard",
    # Anthony family — algorithm picks "antoinette" (feminine) for "tony".
    "tony": "anthony",
    # Alexander family — algorithm picks "alexandria" (a place / female form).
    "alex": "alexander",
    "sandy": "alexander",  # ambiguous (also Cassandra etc.); pick Alexander as default
    # Albert / Alan / Alfred — "al" has 14 candidates in the data. Pick the most
    # common modern usage.
    "al": "albert",
    # Nicholas family — algorithm picks "nicodemus" via raw-length.
    "nick": "nicholas",
    "nicky": "nicholas",
    # Margaret family — algorithm picks "margaretta" (rare archaic).
    "maggie": "margaret",
    "peggy": "margaret",
    "meg": "margaret",
    # Patricia family — pick female default (algorithm produces "patience").
    "pat": "patricia",
    "patty": "patricia",
    # Samuel — algorithm produces "samantha" (gender-flip).
    "sammy": "samuel",
    # Mike → Michael (algorithm picks "miguel" via Spanish translation).
    "mike": "michael",
    # Tom → Thomas (algorithm OK but make explicit).
    "tom": "thomas",
    "tommy": "thomas",
    # Jim → James (explicit).
    "jim": "james",
    "jimmy": "james",
    "jimmie": "james",
    # Dave → David (the data does not include this pair at all in some snapshots).
    "dave": "david",
    "davey": "david",
    "davy": "david",
    # Liz → Elizabeth (explicit; algorithm OK but pin it).
    "liz": "elizabeth",
    "lizzy": "elizabeth",
    "lizzie": "elizabeth",
    "beth": "elizabeth",
    "betty": "elizabeth",
    "betsy": "elizabeth",
    "eliza": "elizabeth",
    # Kate → Katherine.
    "kate": "katherine",
    "katie": "katherine",
    "kathy": "katherine",
    "kit": "katherine",
    # Susan → Susan family.
    "sue": "susan",
    "susie": "susan",
    # Mary family.
    "molly": "mary",
    "polly": "mary",
    # Frederick.
    "fred": "frederick",
    "freddy": "frederick",
    "freddie": "frederick",
    # Christopher.
    "chris": "christopher",
    "kris": "christopher",
    # Joseph.
    "joe": "joseph",
    "joey": "joseph",
    # Charles.
    "chuck": "charles",
    "charlie": "charles",
    "chas": "charles",
    # Edward.
    "eddie": "edward",
    "ed": "edward",
    "ned": "edward",
    "ted": "edward",
    # Henry.
    "hank": "henry",
    "harry": "henry",
    # Jonathan / John.
    "jon": "jonathan",
    "johnny": "john",
    "jack": "john",
    # Daniel.
    "danny": "daniel",
    "dan": "daniel",
    # Anthony already above.
    # Andrew.
    "andy": "andrew",
    "drew": "andrew",
    # Stephen / Steven.
    "steve": "steven",
    "stevie": "steven",
    # Matthew.
    "matt": "matthew",
    "matty": "matthew",
    # Benjamin.
    "ben": "benjamin",
    "benny": "benjamin",
    # Lawrence.
    "larry": "lawrence",
    "lar": "lawrence",
    # Donald / Ronald.
    "don": "donald",
    "donnie": "donald",
    "ron": "ronald",
    "ronnie": "ronald",
    # Raymond.
    "ray": "raymond",
    # Walter.
    "walt": "walter",
    # Russell.
    "russ": "russell",
    # Theodore.
    "theo": "theodore",
    # Joshua.
    "josh": "joshua",
}

# Names that appear as the formal canonical for many other nicknames and must
# NEVER be remapped — the bidirectional Carlton Northern data sometimes
# contains feminine variants or archaic alternates as "canonicals" of these
# names (e.g. william → wilma, john → johnathan), which is wrong by every
# modern American standard. Treat this list as "household-recognizable
# formal first names that should never be collapsed."
_PROTECTED_FORMAL_NAMES: Final[frozenset[str]] = frozenset({
    # Male canonical first names (common modern American)
    "robert", "william", "richard", "james", "thomas", "michael", "samuel",
    "charles", "anthony", "alexander", "nicholas", "patrick", "joseph",
    "joshua", "daniel", "matthew", "frederick", "lawrence", "benjamin",
    "christopher", "andrew", "stephen", "steven", "george", "edward",
    "henry", "kenneth", "francis", "raymond", "arthur", "donald", "ronald",
    "albert", "philip", "harold", "walter", "carl", "gerald", "paul",
    "jonathan", "nathaniel", "david", "john", "peter", "mark", "luke",
    "jacob", "noah", "isaac", "ethan", "owen", "liam", "logan", "lucas",
    "ryan", "kevin", "brian", "scott", "todd", "craig", "douglas", "wayne",
    "russell", "dennis", "leonard", "ralph", "eugene", "louis", "wesley",
    "vincent", "victor", "neil", "keith", "calvin", "kelly", "barry",
    "bruce", "byron", "clark", "clarence", "clyde", "dean", "donovan",
    "elliott", "evan", "floyd", "glenn", "grant", "gregg", "gregory",
    "harvey", "herbert", "howard", "ian", "jack", "jeb", "jeffrey", "jerry",
    "julian", "justin", "jude", "leon", "marcus", "martin", "morris",
    "nathan", "norman", "phillip", "preston", "reginald", "rex", "roger",
    "rodney", "roland", "sean", "seth", "sidney", "spencer", "stanley",
    "terry", "timothy", "trevor", "warren", "wendell", "willie",
    # Female canonical first names (common modern American)
    "elizabeth", "margaret", "patricia", "katherine", "victoria", "vanessa",
    "stephanie", "samantha", "sandra", "rebecca", "jessica", "jennifer",
    "deborah", "barbara", "susan", "carolyn", "christina", "cynthia",
    "donna", "evelyn", "frances", "gloria", "helen", "irene", "joan",
    "judith", "linda", "mary", "michelle", "nancy", "rachel", "ruth",
    "sharon", "shirley", "theresa", "virginia", "anne", "ann", "amy",
    "alice", "carol", "diane", "emma", "eva", "grace", "hannah", "isabella",
    "julia", "karen", "laura", "lisa", "lori", "lucy", "marie", "olivia",
    "rose", "sarah", "sara", "sophia", "tina", "wendy", "maria", "abigail",
    "amanda", "andrea", "angela", "ashley", "audrey", "beatrice", "brenda",
    "carla", "carmen", "cassandra", "catherine", "chloe", "claire", "jane",
    "claudia", "constance", "courtney", "danielle", "denise", "diana",
    "ellen", "elaine", "elena", "erica", "erin", "esther", "florence",
    "gail", "gina", "heather", "ingrid", "iris", "ivy", "janet", "janice",
    "jasmine", "jean", "jeanne", "jill", "joanne", "joyce", "judy",
    "juliana", "kathleen", "kim", "kimberly", "kristen", "kristin",
    "kristina", "lauren", "lillian", "lorraine", "louise", "lydia",
    "madeline", "marcia", "margie", "marsha", "martha", "megan", "melinda",
    "melissa", "meredith", "monica", "natalie", "nicole", "norma", "pamela",
    "paula", "phyllis", "priscilla", "rita", "roberta", "sabrina",
    "sandra", "selena", "sheila", "sherry", "sonia", "stella", "tara",
    "teresa", "tonya", "tracey", "tracy", "valerie", "yvonne",
})


def _build_nickname_dict() -> dict[str, str]:
    """Build NICKNAME_TO_FORMAL from the vendored Carlton Northern CSV.

    See module docstring "Nickname-dict construction" for algorithm details.
    Called once at import; result is cached as the module-level dict.
    """
    # 1. Read all has_nickname triples.
    raw_edges: set[tuple[str, str]] = set()
    with _NICKNAMES_CSV_PATH.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("relationship") != "has_nickname":
                continue
            n1 = row["name1"].strip().lower()
            n2 = row["name2"].strip().lower()
            if not n1 or not n2 or n1 == n2:
                continue
            raw_edges.add((n1, n2))

    # 2. Directional cleanup: for bidirectional pairs, keep only the
    #    direction where the canonical (name1) is longer than the nickname.
    clean_edges: set[tuple[str, str]] = set()
    for a, b in raw_edges:
        if (b, a) in raw_edges:
            if len(a) > len(b):
                clean_edges.add((a, b))
            elif len(a) == len(b) and a < b:
                clean_edges.add((a, b))
        else:
            clean_edges.add((a, b))

    # 3. Build out-edges (canonical → nicknames) and in-edges (nickname →
    #    canonicals).
    out_edges: dict[str, set[str]] = {}
    in_edges: dict[str, set[str]] = {}
    for a, b in clean_edges:
        out_edges.setdefault(a, set()).add(b)
        in_edges.setdefault(b, set()).add(a)

    # 4. For each nickname, choose canonical via tiebreak:
    #    (canonical-with-zero-in-degree preferred, then max out-degree, then
    #    longest, then alphabetical).
    auto_dict: dict[str, str] = {}
    for nickname, candidates in in_edges.items():
        candidates = candidates - {nickname}
        if not candidates:
            continue
        chosen = sorted(
            candidates,
            key=lambda c: (
                len(in_edges.get(c, set())) > 0,  # False (zero in-degree) sorts first
                -len(out_edges.get(c, set())),    # max out-degree
                -len(c),                          # longest
                c,                                # alpha
            ),
        )[0]
        auto_dict[nickname] = chosen

    # 5. Apply manual overrides (highest precedence).
    auto_dict.update(MANUAL_NICKNAME_OVERRIDES)

    # 6. Strip any entries where the KEY is a protected formal name (cleans
    #    out e.g. william → wilma, margaret → gretchen produced by step 4).
    for protected in _PROTECTED_FORMAL_NAMES:
        auto_dict.pop(protected, None)

    return auto_dict


try:
    NICKNAME_TO_FORMAL: Final[dict[str, str]] = _build_nickname_dict()
except FileNotFoundError as exc:  # pragma: no cover - import-time failure
    raise ImportError(
        f"_lib/person_name_normalize.py: bundled nicknames CSV missing at "
        f"{_NICKNAMES_CSV_PATH}; this file is required for normalization "
        f"(silent fallback to no-nicknames would change matching behavior). "
        f"Re-vendor from carltonnorthern/nicknames@{__nicknames_csv_sha__}."
    ) from exc


# Inverse map: canonical → frozenset of all known diminutives that point to
# it. Built once at module load by inverting NICKNAME_TO_FORMAL.
#
# Use case (added in v1.1.0): the upstream normalizer's `normalize_person_name`
# collapses diminutives FORWARD (Mike → michael, Bob → robert) — correct for
# entity-resolution joins, where a single canonical is needed per side. But
# the FMCSA email-attribution pipeline needs to match local-parts against
# ALL accepted forms of an officer's first name — when the officer is filed
# as "Michael Smith" but the local-part is `mike@`, the deterministic-
# equality rules in v1 miss entirely.
#
# `expand_to_diminutives(canonical)` returns the set {canonical, *diminutives}.
# Always includes canonical itself; unknown canonicals return {canonical}.
def _build_inverse_nickname_map() -> dict[str, frozenset[str]]:
    acc: dict[str, set[str]] = {}
    for nickname, canonical in NICKNAME_TO_FORMAL.items():
        acc.setdefault(canonical, set()).add(nickname)
    return {c: frozenset(s) for c, s in acc.items()}


_NICKNAME_INVERSE: Final[dict[str, frozenset[str]]] = _build_inverse_nickname_map()


def expand_to_diminutives(canonical_first: str | None) -> set[str]:
    """Return the set of accepted first-name forms for a canonical first name.

    Always includes the canonical form itself. If the canonical has known
    diminutives in the inverted nicknames map, includes those too.

    Examples:
        expand_to_diminutives("jeffrey")  → {"jeffrey", "jeff", "geoff"}
        expand_to_diminutives("william")  → {"william", "bill", "billy", "willy"}
        expand_to_diminutives("zara")     → {"zara"}   # no known diminutives
        expand_to_diminutives("")         → set()
        expand_to_diminutives(None)       → set()

    Input is lowercased + stripped before lookup; output is always lowercase.
    """
    if not canonical_first:
        return set()
    norm = canonical_first.lower().strip()
    if not norm:
        return set()
    result = {norm}
    if norm in _NICKNAME_INVERSE:
        result.update(_NICKNAME_INVERSE[norm])
    return result


_PUNCT_KEEP_HYPHEN_APOS_RE: Final = re.compile(r"[^a-z0-9'\- ]+")
_WS_RE: Final = re.compile(r"\s+")
_SINGLE_CHAR_RE: Final = re.compile(r"^[a-z]$")


def _normalize_component(component: str) -> str:
    """Apply the pure-string normalization pipeline to a single name part.

    Lowercase → unidecode → punct-strip (keep apostrophe + hyphen) →
    whitespace collapse → strip.
    """
    if not component:
        return ""
    s = unidecode(component).lower()
    s = _PUNCT_KEEP_HYPHEN_APOS_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _is_rejected(component: str, *, is_first: bool) -> bool:
    """True if a normalized component should cause the whole name to reject."""
    if not component:
        return True
    if component in PERSON_NAME_BLACKLIST:
        return True
    if _SINGLE_CHAR_RE.match(component):
        return True
    if len(component) == 2 and component not in _SHORT_NAME_WHITELIST:
        # 2-char first/last names that aren't on the whitelist are too generic
        return True
    return False


def normalize_person_name(raw: str | None) -> tuple[str, str] | None:
    """Parse a single name string and return (last_normalized, first_normalized).

    Handles the FEC-style `"LAST, FIRST MIDDLE"` comma format and
    space-separated forms. Returns None if the input is blacklisted,
    unparseable, or too short to constitute a valid person name.

    Examples:
      "SMITH, ROBERT J."         → ("smith", "robert")
      "Bob Smith"                → ("smith", "robert")
      "Maria Von Trapp"          → ("von trapp", "maria")
      "ANONYMOUS"                → None
      ""                         → None
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(
            f"normalize_person_name: expected str | None, got {type(raw).__name__}"
        )
    s = raw.strip()
    if not s:
        return None

    parsed = HumanName(s)
    first = _normalize_component(parsed.first)
    last = _normalize_component(parsed.last)

    if not first or not last:
        return None

    first = NICKNAME_TO_FORMAL.get(first, first)

    if _is_rejected(first, is_first=True) or _is_rejected(last, is_first=False):
        return None

    return last, first


def normalize_first_last(
    first: str | None,
    last: str | None,
    middle: str | None = None,  # noqa: ARG001 — accepted for API symmetry, see v1.0.0 design notes
) -> tuple[str, str] | None:
    """Normalize already-split first/last components.

    Used for sources that publish person names as separate columns (IRS 990,
    Form 5500, NPPES, SEC EDGAR insider filings). The `middle` parameter is
    accepted for API symmetry but discarded — see module "v1.0.0 design notes".

    Returns (last_normalized, first_normalized) or None.
    """
    for arg, name in ((first, "first"), (last, "last")):
        if arg is not None and not isinstance(arg, str):
            raise TypeError(
                f"normalize_first_last: {name}= must be str | None, got "
                f"{type(arg).__name__}"
            )
    if first is None or last is None:
        return None
    f = _normalize_component(first)
    l = _normalize_component(last)
    if not f or not l:
        return None
    f = NICKNAME_TO_FORMAL.get(f, f)
    if _is_rejected(f, is_first=True) or _is_rejected(l, is_first=False):
        return None
    return l, f


def py_normalize_person_name_for_duckdb(raw: str | None) -> str | None:
    """DuckDB-UDF-friendly wrapper.

    Returns "last|first" pipe-delimited string or None. The bridge SQL splits
    this back into two columns via `string_split(... , '|')`. Pipe-delimiting
    is simpler than registering a struct UDF (DuckDB's struct-return UDFs are
    awkward to wire up).

    Per L34 caveat: for bridge inputs of >100M rows (FEC's 300M+ contributions
    universe), the Python UDF path is slow. Downstream bridges may opt for an
    inline DuckDB SQL macro that approximates the rule, with a verified-
    equivalence parity test against this function. Out of scope for v1.0.0
    of the normalizer module.

    Bridge generators register via:

        import duckdb
        from _lib.person_name_normalize import py_normalize_person_name_for_duckdb

        con = duckdb.connect()
        con.create_function(
            "py_normalize_person",
            py_normalize_person_name_for_duckdb,
            [duckdb.typing.VARCHAR],
            duckdb.typing.VARCHAR,
        )
    """
    if raw is None:
        return None
    result = normalize_person_name(raw)
    if result is None:
        return None
    last, first = result
    return f"{last}|{first}"
