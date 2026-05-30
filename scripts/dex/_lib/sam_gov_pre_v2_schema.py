"""Pre-V2 SAM.gov Entity Registration extract schema (120 sequential .dat fields).

Used for historical archives 2014-NOV through 2020-MAY — the era before SAM
introduced the V2 schema (which added the 12-char Unique Entity Identifier
in column 1 and grew the column count to 142).

Source of truth: 'SAM Mapping Public File Layout - Modified.xlsx' shipped
inside any pre-2021 historical archive ZIP. The xlsx lists 150 column orders
with 30 gaps for FOUO/Sensitive fields that GSA strips from the public
extract; the public .dat has 120 sequential pipe-delimited fields, indexed
1..120, mapped here with their xlsx col_order in the trailing comment.

Notes:
  * Column 1 (`duns`) — GSA replaced the actual DUNS value with the literal
    string "No longer available" when the privacy-update was applied
    (2022-05-17). The placeholder is retained so the field count matches.
  * `legal_business_name` is column 11 here vs column 12 in V2 (UEI bumps
    everything down by one).
  * No `unique_entity_id` field exists; downstream cross-source identity-
    resolution MVs join on `legal_business_name_normalized` and
    `cage_code_normalized` for these archives.
"""

from __future__ import annotations


SAM_GOV_PRE_V2_DB_COLUMN_NAMES: list[str] = [
    'duns',  # .dat field 1 (xlsx col_order 1)
    'dunsplus4',  # .dat field 2 (xlsx col_order 2)
    'cage_code',  # .dat field 3 (xlsx col_order 3)
    'dodaac',  # .dat field 4 (xlsx col_order 4)
    'sam_extract_code',  # .dat field 5 (xlsx col_order 5)
    'purpose_of_registration',  # .dat field 6 (xlsx col_order 6)
    'initial_registration_date',  # .dat field 7 (xlsx col_order 7)
    'expiration_date',  # .dat field 8 (xlsx col_order 8)
    'last_update_date',  # .dat field 9 (xlsx col_order 9)
    'activation_date',  # .dat field 10 (xlsx col_order 10)
    'legal_business_name',  # .dat field 11 (xlsx col_order 11)
    'dba_name',  # .dat field 12 (xlsx col_order 12)
    'company_division',  # .dat field 13 (xlsx col_order 13)
    'division_number',  # .dat field 14 (xlsx col_order 14)
    'physical_address_line_1',  # .dat field 15 (xlsx col_order 15)
    'physical_address_line_2',  # .dat field 16 (xlsx col_order 16)
    'physical_address_city',  # .dat field 17 (xlsx col_order 17)
    'physical_address_province_or_state',  # .dat field 18 (xlsx col_order 18)
    'physical_address_zip_postal_code',  # .dat field 19 (xlsx col_order 19)
    'physical_address_zip_code_plus4',  # .dat field 20 (xlsx col_order 20)
    'physical_address_country_code',  # .dat field 21 (xlsx col_order 21)
    'entity_congressional_district',  # .dat field 22 (xlsx col_order 22)
    'business_start_date',  # .dat field 23 (xlsx col_order 23)
    'fiscal_year_end_close_date',  # .dat field 24 (xlsx col_order 24)
    'corporate_url',  # .dat field 25 (xlsx col_order 25)
    'entity_structure',  # .dat field 26 (xlsx col_order 26)
    'state_of_incorporation',  # .dat field 27 (xlsx col_order 27)
    'country_of_incorporation',  # .dat field 28 (xlsx col_order 28)
    'business_type_counter',  # .dat field 29 (xlsx col_order 29)
    'bus_type_string',  # .dat field 30 (xlsx col_order 30)
    'primary_naics',  # .dat field 31 (xlsx col_order 31)
    'naics_code_counter',  # .dat field 32 (xlsx col_order 32)
    'naics_code_string',  # .dat field 33 (xlsx col_order 33)
    'psc_code_counter',  # .dat field 34 (xlsx col_order 34)
    'psc_code_string',  # .dat field 35 (xlsx col_order 35)
    'credit_card_usage',  # .dat field 36 (xlsx col_order 36)
    'correspondence_flag',  # .dat field 37 (xlsx col_order 37)
    'mailing_address_line_1',  # .dat field 38 (xlsx col_order 38)
    'mailing_address_line_2',  # .dat field 39 (xlsx col_order 39)
    'mailing_address_city',  # .dat field 40 (xlsx col_order 40)
    'mailing_address_zip_postal_code',  # .dat field 41 (xlsx col_order 41)
    'mailing_address_zip_code_plus4',  # .dat field 42 (xlsx col_order 42)
    'mailing_address_country',  # .dat field 43 (xlsx col_order 43)
    'mailing_address_state_or_province',  # .dat field 44 (xlsx col_order 44)
    'govt_bus_poc_first_name',  # .dat field 45 (xlsx col_order 45)
    'govt_bus_poc_middle_initial',  # .dat field 46 (xlsx col_order 46)
    'govt_bus_poc_last_name',  # .dat field 47 (xlsx col_order 47)
    'govt_bus_poc_title',  # .dat field 48 (xlsx col_order 48)
    'govt_bus_poc_st_add_1',  # .dat field 49 (xlsx col_order 49)
    'govt_bus_poc_st_add_2',  # .dat field 50 (xlsx col_order 50)
    'govt_bus_poc_city',  # .dat field 51 (xlsx col_order 51)
    'govt_bus_poc_zip_postal_code',  # .dat field 52 (xlsx col_order 52)
    'govt_bus_poc_zip_code_plus4',  # .dat field 53 (xlsx col_order 53)
    'govt_bus_poc_country_code',  # .dat field 54 (xlsx col_order 54)
    'govt_bus_poc_state_or_province',  # .dat field 55 (xlsx col_order 55)
    'alt_govt_bus_poc_first_name',  # .dat field 56 (xlsx col_order 61)
    'alt_govt_bus_poc_middle_initial',  # .dat field 57 (xlsx col_order 62)
    'alt_govt_bus_poc_last_name',  # .dat field 58 (xlsx col_order 63)
    'alt_govt_bus_poc_title',  # .dat field 59 (xlsx col_order 64)
    'alt_govt_bus_poc_st_add_1',  # .dat field 60 (xlsx col_order 65)
    'alt_govt_bus_poc_st_add_2',  # .dat field 61 (xlsx col_order 66)
    'alt_govt_bus_poc_city',  # .dat field 62 (xlsx col_order 67)
    'alt_govt_bus_poc_zip_postal_code',  # .dat field 63 (xlsx col_order 68)
    'alt_govt_bus_poc_zip_code_plus4',  # .dat field 64 (xlsx col_order 69)
    'alt_govt_bus_poc_country_code',  # .dat field 65 (xlsx col_order 70)
    'alt_govt_bus_poc_state_or_province',  # .dat field 66 (xlsx col_order 71)
    'past_perf_poc_poc_first_name',  # .dat field 67 (xlsx col_order 77)
    'past_perf_poc_poc_middle_initial',  # .dat field 68 (xlsx col_order 78)
    'past_perf_poc_poc_last_name',  # .dat field 69 (xlsx col_order 79)
    'past_perf_poc_poc_title',  # .dat field 70 (xlsx col_order 80)
    'past_perf_poc_st_add_1',  # .dat field 71 (xlsx col_order 81)
    'past_perf_poc_st_add_2',  # .dat field 72 (xlsx col_order 82)
    'past_perf_poc_city',  # .dat field 73 (xlsx col_order 83)
    'past_perf_poc_zip_postal_code',  # .dat field 74 (xlsx col_order 84)
    'past_perf_poc_zip_code_plus4',  # .dat field 75 (xlsx col_order 85)
    'past_perf_poc_country_code',  # .dat field 76 (xlsx col_order 86)
    'past_perf_poc_state_or_province',  # .dat field 77 (xlsx col_order 87)
    'alt_past_perf_poc_first_name',  # .dat field 78 (xlsx col_order 93)
    'alt_past_perf_poc_middle_initial',  # .dat field 79 (xlsx col_order 94)
    'alt_past_perf_poc_last_name',  # .dat field 80 (xlsx col_order 95)
    'alt_past_perf_poc_title',  # .dat field 81 (xlsx col_order 96)
    'alt_past_perf_poc_st_add_1',  # .dat field 82 (xlsx col_order 97)
    'alt_past_perf_poc_st_add_2',  # .dat field 83 (xlsx col_order 98)
    'alt_past_perf_poc_city',  # .dat field 84 (xlsx col_order 99)
    'alt_past_perf_poc_zip_postal_code',  # .dat field 85 (xlsx col_order 100)
    'alt_past_perf_poc_zip_code_plus4',  # .dat field 86 (xlsx col_order 101)
    'alt_past_perf_poc_country_code',  # .dat field 87 (xlsx col_order 102)
    'alt_past_perf_poc_state_or_province',  # .dat field 88 (xlsx col_order 103)
    'elec_bus_poc_first_name',  # .dat field 89 (xlsx col_order 109)
    'elec_bus_poc_middle_initial',  # .dat field 90 (xlsx col_order 110)
    'elec_bus_poc_last_name',  # .dat field 91 (xlsx col_order 111)
    'elec_bus_poc_title',  # .dat field 92 (xlsx col_order 112)
    'elec_bus_poc_st_add_1',  # .dat field 93 (xlsx col_order 113)
    'elec_bus_poc_st_add_2',  # .dat field 94 (xlsx col_order 114)
    'elec_bus_poc_city',  # .dat field 95 (xlsx col_order 115)
    'elec_bus_poc_zip_postal_code',  # .dat field 96 (xlsx col_order 116)
    'elec_bus_poc_zip_code_plus4',  # .dat field 97 (xlsx col_order 117)
    'elec_bus_poc_country_code',  # .dat field 98 (xlsx col_order 118)
    'elec_bus_poc_state_or_province',  # .dat field 99 (xlsx col_order 119)
    'alt_elec_poc_bus_poc_first_name',  # .dat field 100 (xlsx col_order 125)
    'alt_elec_poc_bus_poc_middle_initial',  # .dat field 101 (xlsx col_order 126)
    'alt_elec_poc_bus_poc_last_name',  # .dat field 102 (xlsx col_order 127)
    'alt_elec_poc_bus_poc_title',  # .dat field 103 (xlsx col_order 128)
    'alt_elec_poc_bus_st_add_1',  # .dat field 104 (xlsx col_order 129)
    'alt_elec_poc_bus_st_add_2',  # .dat field 105 (xlsx col_order 130)
    'alt_elec_poc_bus_city',  # .dat field 106 (xlsx col_order 131)
    'alt_elec_poc_bus_zip_postal_code',  # .dat field 107 (xlsx col_order 132)
    'alt_elec_poc_bus_zip_code_plus4',  # .dat field 108 (xlsx col_order 133)
    'alt_elec_poc_bus_country_code',  # .dat field 109 (xlsx col_order 134)
    'alt_elec_poc_bus_state_or_province',  # .dat field 110 (xlsx col_order 135)
    'naics_exception_counter',  # .dat field 111 (xlsx col_order 141)
    'naics_exception_string',  # .dat field 112 (xlsx col_order 142)
    'debt_subject_to_offset_flag',  # .dat field 113 (xlsx col_order 143)
    'exclusion_status_flag',  # .dat field 114 (xlsx col_order 144)
    'sba_business_types_counter',  # .dat field 115 (xlsx col_order 145)
    'sba_business_types_string',  # .dat field 116 (xlsx col_order 146)
    'no_public_display_flag',  # .dat field 117 (xlsx col_order 147)
    'disaster_response_counter',  # .dat field 118 (xlsx col_order 148)
    'disaster_response_string',  # .dat field 119 (xlsx col_order 149)
    'end_of_record_indicator',  # .dat field 120 (xlsx col_order 150)
]

assert len(SAM_GOV_PRE_V2_DB_COLUMN_NAMES) == 120, (
    f"expected 120 cols, got {len(SAM_GOV_PRE_V2_DB_COLUMN_NAMES)}"
)
assert len(set(SAM_GOV_PRE_V2_DB_COLUMN_NAMES)) == 120, "duplicate column names"


if __name__ == "__main__":
    print(f"SAM.gov pre-V2 column map: {len(SAM_GOV_PRE_V2_DB_COLUMN_NAMES)} columns OK")
    print(f"  First: {SAM_GOV_PRE_V2_DB_COLUMN_NAMES[0]}")
    print(f"  Last:  {SAM_GOV_PRE_V2_DB_COLUMN_NAMES[-1]}")
