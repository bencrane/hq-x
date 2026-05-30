"""Pinned FL Sunbiz fixed-width record layouts (COR + COR-EVENT).

Source of truth: https://dos.sunbiz.org/data-definitions/cor.html
(resolved by the validator on 2026-05-16 UTC via a real-browser User-Agent —
Cloudflare bot-challenge passed; HTTP 200, 17,214 bytes; SHA preserved by
re-fetch.) Layout cross-confirmed against operator-staged
/Users/benjamincrane/Downloads/cordata.zip (first 3 records of cordata0.txt)
and /Users/benjamincrane/Downloads/corevent.zip (first 3 records of
corevt.txt). All offsets re-derived from byte-accurate probes match the
published spec exactly.

This module is re-used by:
  - hq-all-fl-sunbiz-quarterly-ingest-and-sba-bridge-baseline-probe.py
  - scripts/run_fl_sunbiz_master_unload_to_r2.py (s1, executor — Modal-hosted)

Both layouts are 1-indexed in the published spec; this module exposes
0-indexed (start, end) half-open slices for direct Python `bytes[start:end]`
or `str[start:end]` use. `end - start == length` per the spec column.

Notation:
  * `entity_num` is the corporate document number (12 chars).
  * `entity_name` is the 192-char corporate name field — UPPERCASE, padded
    with trailing spaces. Strip + normalize via
    `_lib.entity_name_normalize.normalize_entity_name` before joining to
    SBA `legal_name_normalized` (validator p1 — the same normalizer that
    powered PR #464 SBA × CA SoS and PR #466 UCC × CA SoS).
  * Officer name format (CRITICAL — discovered by empirical probe, NOT
    documented in the Sunbiz spec): the 42-char Officer N Name field is
    NOT free-form. It is broken into:
        last_name   = bytes  0..20    (20 chars, left-padded with spaces)
        first_name  = bytes 20..40    (20 chars, left-padded with spaces)
        middle      = bytes 40..42    (2 chars, often single initial)
    This applies to both Registered Agent Name and Officer 1-6 Name fields.
    Officer-grain `full_name_normalized` is built as
        lower(trim(concat_ws(' ', first_name, middle, last_name)))
    matching the CA SoS principals shape from PR #464.
  * Field #18 `FEI Number` is the entity's IRS EIN equivalent (14-char
    text — Sunbiz prefixes blanks for legacy fields, but live filings carry
    a digit string). Re-use as a cross-source identity key for downstream
    matching to SBA + USAspending + IRS BMF.
  * Officer slots 1..6 are inline per cordata record (positions 668..1428).
    Many slots are blank for entities with ≤2 officers. The
    `more_than_six_officers_flag` at offset 494 is `'Y'` when the file
    truncated past 6 officers; downstream cycles can chain to the per-event
    file for the missing officers.

NEGATIVE FINDING (validator-flagged — CRITICAL for the audit subagent):
  The FL COR layout contains NEITHER `naics_code` NOR `type_of_business`
  columns. The directive's "operator note" instruction to project these
  for the future Shovels.ai construction-NAICS lender-matching cycle
  CANNOT be satisfied from this source — those columns simply do not
  exist in the FL Sunbiz quarterly file. The construction-NAICS
  curation must come from a DIFFERENT source (USAspending NAICS by EIN,
  IRS BMF NAICS by EIN, FL DBPR contractor license rosters, FCC permits,
  etc.) joined on FEI Number once this substrate lands. The audit
  subagent MUST NOT fabricate columns that the source does not publish
  per `apps/data-engine-x/CLAUDE.md §"Source ingest invariant"` rule 1
  (TRUE 1:1 column mirror — no curation at ingest, no fabricated columns).

Encoding (validator-probed): cordata0.txt first 1 MB is pure ASCII
(0 non-printable bytes via `LC_ALL=C grep -c '[^[:print:][:space:]]'`).
The Sunbiz docs confirm 7-bit ASCII. No cp1252 fallback needed (CA SoS
required cp1252 for tail rows; FL does NOT — verified empirically).

Line terminator: CRLF (\\r\\n). Records are EXACTLY 1440 chars + CRLF
(2 bytes) = 1442 byte/line for cordata, and EXACTLY 662 chars + CRLF
= 664 byte/line for corevent. Note: the directive's "~663 chars" for
corevent is off by one — the spec last field ends at position 662
(1-indexed), so the content portion of each line is 662 chars (the
CR is byte index 662, LF index 663).
"""
from __future__ import annotations

from typing import Final


# --------------------------------------------------------------------------- #
# COR (cordata*.txt) — Corporate File Definitions
# --------------------------------------------------------------------------- #
# Record content length: 1440 chars (1-indexed positions 1..1440 from spec;
# byte index 1440 in each line is CR, 1441 is LF; line stride 1442 bytes).
COR_RECORD_BYTES: Final[int] = 1440
COR_LINE_STRIDE_BYTES: Final[int] = 1442  # 1440 + CRLF

# Half-open 0-indexed slices: bytes[start:end], end - start = length.
# Spec column → (start_0idx, end_0idx, length, description).
COR_FIELDS: Final[dict[str, tuple[int, int, int, str]]] = {
    # field#  name                       0-start  0-end  len  description
    "entity_num":                        (   0,     12,   12, "Corporation Number (PK; doc number)"),
    "entity_name":                       (  12,    204,  192, "Corporation Name (UPPERCASE, padded)"),
    "status":                            ( 204,    205,    1, "A=Active, I=Inactive"),
    "filing_type":                       ( 205,    220,   15, "DOMP/DOMNP/FORP/FORNP/DOMLP/FORLP/FLAL/FORL/NPREG/TRUST/AGENT"),
    "address1":                          ( 220,    262,   42, "Principal address line 1"),
    "address2":                          ( 262,    304,   42, "Principal address line 2"),
    "city":                              ( 304,    332,   28, "Principal city"),
    "state":                             ( 332,    334,    2, "Principal state"),
    "zip":                               ( 334,    344,   10, "Principal zip"),
    "country":                           ( 344,    346,    2, "Principal country"),
    "mail_address1":                     ( 346,    388,   42, "Mailing address line 1"),
    "mail_address2":                     ( 388,    430,   42, "Mailing address line 2"),
    "mail_city":                         ( 430,    458,   28, "Mailing city"),
    "mail_state":                        ( 458,    460,    2, "Mailing state"),
    "mail_zip":                          ( 460,    470,   10, "Mailing zip"),
    "mail_country":                      ( 470,    472,    2, "Mailing country"),
    "file_date":                         ( 472,    480,    8, "Formation filing date (MMDDYYYY)"),
    "fei_number":                        ( 480,    494,   14, "FEI / IRS EIN (often blank-padded)"),
    "more_than_six_officers_flag":       ( 494,    495,    1, "Y if entity has >6 officers (subsequent in corevt)"),
    "last_transaction_date":             ( 495,    503,    8, "Date of last filing (MMDDYYYY)"),
    "state_country":                     ( 503,    505,    2, "Issuing state / country"),
    "report_year_1":                     ( 505,    509,    4, "Annual report year #1"),
    "filler_510":                        ( 509,    510,    1, ""),
    "report_date_1":                     ( 510,    518,    8, "Annual report filed date #1"),
    "report_year_2":                     ( 518,    522,    4, "Annual report year #2"),
    "filler_523":                        ( 522,    523,    1, ""),
    "report_date_2":                     ( 523,    531,    8, "Annual report filed date #2"),
    "report_year_3":                     ( 531,    535,    4, "Annual report year #3"),
    "filler_536":                        ( 535,    536,    1, ""),
    "report_date_3":                     ( 536,    544,    8, "Annual report filed date #3"),
    "registered_agent_name":             ( 544,    586,   42, "Registered agent (last(20)+first(20)+mid(2) if Person; full org name if Corp)"),
    "registered_agent_type":             ( 586,    587,    1, "P=Person, C=Corporation"),
    "registered_agent_address":          ( 587,    629,   42, "RA street"),
    "registered_agent_city":             ( 629,    657,   28, "RA city"),
    "registered_agent_state":            ( 657,    659,    2, "RA state"),
    "registered_agent_zip4":             ( 659,    668,    9, "RA zip+4"),
    # Officer 1
    "officer_1_title":                   ( 668,    672,    4, "P/T/C/V/S/D (multi-char allowed e.g. MGR)"),
    "officer_1_type":                    ( 672,    673,    1, "P=Person, C=Corporation"),
    "officer_1_name":                    ( 673,    715,   42, "last(20)+first(20)+mid(2)"),
    "officer_1_address":                 ( 715,    757,   42, ""),
    "officer_1_city":                    ( 757,    785,   28, ""),
    "officer_1_state":                   ( 785,    787,    2, ""),
    "officer_1_zip4":                    ( 787,    796,    9, ""),
    # Officer 2
    "officer_2_title":                   ( 796,    800,    4, ""),
    "officer_2_type":                    ( 800,    801,    1, ""),
    "officer_2_name":                    ( 801,    843,   42, ""),
    "officer_2_address":                 ( 843,    885,   42, ""),
    "officer_2_city":                    ( 885,    913,   28, ""),
    "officer_2_state":                   ( 913,    915,    2, ""),
    "officer_2_zip4":                    ( 915,    924,    9, ""),
    # Officer 3
    "officer_3_title":                   ( 924,    928,    4, ""),
    "officer_3_type":                    ( 928,    929,    1, ""),
    "officer_3_name":                    ( 929,    971,   42, ""),
    "officer_3_address":                 ( 971,   1013,   42, ""),
    "officer_3_city":                    (1013,   1041,   28, ""),
    "officer_3_state":                   (1041,   1043,    2, ""),
    "officer_3_zip4":                    (1043,   1052,    9, ""),
    # Officer 4
    "officer_4_title":                   (1052,   1056,    4, ""),
    "officer_4_type":                    (1056,   1057,    1, ""),
    "officer_4_name":                    (1057,   1099,   42, ""),
    "officer_4_address":                 (1099,   1141,   42, ""),
    "officer_4_city":                    (1141,   1169,   28, ""),
    "officer_4_state":                   (1169,   1171,    2, ""),
    "officer_4_zip4":                    (1171,   1180,    9, ""),
    # Officer 5
    "officer_5_title":                   (1180,   1184,    4, ""),
    "officer_5_type":                    (1184,   1185,    1, ""),
    "officer_5_name":                    (1185,   1227,   42, ""),
    "officer_5_address":                 (1227,   1269,   42, ""),
    "officer_5_city":                    (1269,   1297,   28, ""),
    "officer_5_state":                   (1297,   1299,    2, ""),
    "officer_5_zip4":                    (1299,   1308,    9, ""),
    # Officer 6
    "officer_6_title":                   (1308,   1312,    4, ""),
    "officer_6_type":                    (1312,   1313,    1, ""),
    "officer_6_name":                    (1313,   1355,   42, ""),
    "officer_6_address":                 (1355,   1397,   42, ""),
    "officer_6_city":                    (1397,   1425,   28, ""),
    "officer_6_state":                   (1425,   1427,    2, ""),
    "officer_6_zip4":                    (1427,   1436,    9, ""),
    "filler_tail":                       (1436,   1440,    4, ""),
}

# Officer/Agent name parsing — within a 42-char name field, the format is
# fixed-width: last(20) + first(20) + middle(2). Spec doesn't document this
# explicitly; validator confirmed by inspecting cordata0.txt records.
NAME_LAST_SLICE: Final[tuple[int, int]] = (0, 20)
NAME_FIRST_SLICE: Final[tuple[int, int]] = (20, 40)
NAME_MIDDLE_SLICE: Final[tuple[int, int]] = (40, 42)

# Officer slot enumeration helper.
OFFICER_SLOTS: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 6)


# --------------------------------------------------------------------------- #
# COR-EVENT (corevt.txt) — Corporate event file
# --------------------------------------------------------------------------- #
# Record content length: 662 chars (1-indexed positions 1..662 from spec).
# Line stride: 662 + CRLF = 664 byte/line.
COREVENT_RECORD_BYTES: Final[int] = 662
COREVENT_LINE_STRIDE_BYTES: Final[int] = 664  # 662 + CRLF

COREVENT_FIELDS: Final[dict[str, tuple[int, int, int, str]]] = {
    "event_doc_number":                  (  0,     12,   12, "Charter / entity_num"),
    "event_seq_number":                  ( 12,     17,    5, "Event sequence within entity"),
    "event_code":                        ( 17,     37,   20, "Event code (e.g. CORAMINVOL)"),
    "event_desc":                        ( 37,     77,   40, "Event description"),
    "event_effective_date":              ( 77,     85,    8, "Date event becomes effective (MMDDYYYY)"),
    "event_filed_date":                  ( 85,     93,    8, "Date event was filed (MMDDYYYY)"),
    "event_note_1":                      ( 93,    128,   35, ""),
    "event_note_2":                      (128,    163,   35, ""),
    "event_note_3":                      (163,    198,   35, ""),
    "event_cons_mer_number":             (198,    210,   12, "Conversion/merger number"),
    "event_cor_name":                    (210,    402,  192, "Corporation name"),
    "event_name_seq":                    (402,    407,    5, ""),
    "event_x_name_seq":                  (407,    412,    5, "Cross-reference name sequence"),
    "event_name_chg":                    (412,    413,    1, "Y if event changes corp name"),
    "event_x_name_chg":                  (413,    414,    1, "Y if cross-ref name change"),
    "event_address1":                    (414,    456,   42, ""),
    "event_address2":                    (456,    498,   42, ""),
    "event_city":                        (498,    526,   28, ""),
    "event_state":                       (526,    528,    2, ""),
    "event_zip":                         (528,    538,   10, ""),
    "event_mail_address1":               (538,    580,   42, ""),
    "event_mail_address2":               (580,    622,   42, ""),
    "event_mail_city":                   (622,    650,   28, ""),
    "event_mail_state":                  (650,    652,    2, ""),
    "event_mail_zip":                    (652,    662,   10, ""),
}


# --------------------------------------------------------------------------- #
# Self-test (run as a module to verify offsets sum + sanity-probe a few recs)
# --------------------------------------------------------------------------- #


def _selftest_cor() -> None:
    """Verify all COR field slices cover exactly bytes[0..1440] with no gaps/overlaps."""
    starts_ends = sorted((s, e) for (s, e, _, _) in COR_FIELDS.values())
    assert starts_ends[0][0] == 0, f"COR first field must start at 0, got {starts_ends[0][0]}"
    assert starts_ends[-1][1] == COR_RECORD_BYTES, (
        f"COR last field must end at {COR_RECORD_BYTES}, got {starts_ends[-1][1]}"
    )
    for i in range(len(starts_ends) - 1):
        assert starts_ends[i][1] == starts_ends[i + 1][0], (
            f"COR field gap/overlap between {starts_ends[i]} and {starts_ends[i + 1]}"
        )


def _selftest_corevent() -> None:
    """Verify all COREVENT field slices cover exactly bytes[0..662]."""
    starts_ends = sorted((s, e) for (s, e, _, _) in COREVENT_FIELDS.values())
    assert starts_ends[0][0] == 0
    assert starts_ends[-1][1] == COREVENT_RECORD_BYTES
    for i in range(len(starts_ends) - 1):
        assert starts_ends[i][1] == starts_ends[i + 1][0], (
            f"COREVENT field gap/overlap between {starts_ends[i]} and {starts_ends[i + 1]}"
        )


def parse_officer_name(name_42: bytes | str) -> tuple[str, str, str]:
    """Split a 42-char officer/agent name field into (first, middle, last).

    Returns trimmed strings. Empty strings if the slot is blank-padded.
    """
    if isinstance(name_42, bytes):
        name_42 = name_42.decode("ascii", errors="replace")
    last = name_42[NAME_LAST_SLICE[0]:NAME_LAST_SLICE[1]].strip()
    first = name_42[NAME_FIRST_SLICE[0]:NAME_FIRST_SLICE[1]].strip()
    middle = name_42[NAME_MIDDLE_SLICE[0]:NAME_MIDDLE_SLICE[1]].strip()
    return first, middle, last


if __name__ == "__main__":
    _selftest_cor()
    _selftest_corevent()
    print("PASS: COR layout covers bytes 0..1440 cleanly across",
          len(COR_FIELDS), "fields")
    print("PASS: COREVENT layout covers bytes 0..662 cleanly across",
          len(COREVENT_FIELDS), "fields")
