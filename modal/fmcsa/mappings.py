from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from .feed_catalog import FeedConfig


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    cleaned = str(value).strip()
    return cleaned or None


def parse_mmddyyyy_date(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    try:
        return datetime.strptime(cleaned, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def parse_yyyymmdd_date(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    try:
        return datetime.strptime(cleaned, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def parse_iso_date(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def parse_fmcsa_date(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    for date_format in ("%m/%d/%Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(cleaned, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_int(value: Any) -> int | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_bool(value: Any) -> bool | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    normalized = cleaned.lower()
    if normalized in {"true", "t", "yes", "y", "1"}:
        return True
    if normalized in {"false", "f", "no", "n", "0"}:
        return False
    return None


def parse_x_flag(value: Any) -> bool | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    return cleaned.upper() == "X"


def is_blank_or_zero(value: Any) -> bool:
    cleaned = clean_text(value)
    if cleaned is None:
        return True
    return all(character == "0" for character in cleaned)


def _parse_mcmis_oos_indicator(value: Any) -> bool | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    normalized = cleaned.upper()
    if normalized in {"Y", "Z"}:
        return True
    if normalized == "N":
        return False
    return None


def _parse_classification_flags(classdef_text: str | None) -> dict[str, Any]:
    if classdef_text is None:
        return {
            "private_only": None,
            "authorized_for_hire": None,
            "exempt_for_hire": None,
            "private_property": None,
            "private_passenger_business": None,
            "private_passenger_nonbusiness": None,
            "migrant": None,
            "us_mail": None,
            "federal_government": None,
            "state_government": None,
            "local_government": None,
            "indian_tribe": None,
            "other_operation_description": None,
        }

    values = [part.strip().upper() for part in classdef_text.split(";") if part.strip()]
    values_set = set(values)
    other_values = [value for value in values if value.startswith("OTHER")]

    has_private = bool(
        values_set.intersection(
            {"PRIVATE PROPERTY", "PRIVATE PASSENGER, BUSINESS", "PRIVATE PASSENGER, NONBUSINESS"}
        )
    )
    has_for_hire = bool(values_set.intersection({"AUTHORIZED FOR HIRE", "EXEMPT FOR HIRE"}))

    return {
        "private_only": has_private and not has_for_hire,
        "authorized_for_hire": "AUTHORIZED FOR HIRE" in values_set,
        "exempt_for_hire": "EXEMPT FOR HIRE" in values_set,
        "private_property": "PRIVATE PROPERTY" in values_set,
        "private_passenger_business": "PRIVATE PASSENGER, BUSINESS" in values_set,
        "private_passenger_nonbusiness": "PRIVATE PASSENGER, NONBUSINESS" in values_set,
        "migrant": "MIGRANT" in values_set,
        "us_mail": "U. S. MAIL" in values_set or "US MAIL" in values_set,
        "federal_government": "FEDERAL GOVERNMENT" in values_set,
        "state_government": "STATE GOVERNMENT" in values_set,
        "local_government": "LOCAL GOVERNMENT" in values_set,
        "indian_tribe": "INDIAN TRIBE" in values_set,
        "other_operation_description": "; ".join(other_values) if other_values else None,
    }


INSURANCE_TYPE_DESCRIPTIONS = {
    "1": "BI&PD",
    "2": "Cargo",
    "3": "Bond",
    "4": "Trust Fund",
}

SNAPSHOT_HISTORY_TABLES = {
    "operating_authority_histories",
    "operating_authority_revocations",
    "insurance_policies",
    "insurance_policy_filings",
    "insurance_policy_history_events",
}


@dataclass(frozen=True)
class ParsedSourceRow:
    row_number: int
    raw_values: list[str]
    raw_fields: dict[str, str]


def build_record_fingerprint(*, table_name: str, feed_date: str, feed_name: str, row_position: int) -> str:
    identity = f"{table_name}|{feed_date}|{feed_name}|{row_position}"
    return sha256(identity.encode("utf-8")).hexdigest()


def build_typed_row(feed: FeedConfig, row: ParsedSourceRow) -> dict[str, Any]:
    fields = row.raw_fields
    feed_name = feed.feed_name

    if feed.table_name == "operating_authority_histories":
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "usdot_number": clean_text(fields.get("USDOT Number")),
            "sub_number": clean_text(fields.get("Sub Number")),
            "operating_authority_type": clean_text(fields.get("Operating Authority Type")),
            "original_authority_action_description": clean_text(
                fields.get("Original Authority Action Description")
            ),
            "original_authority_action_served_date": parse_mmddyyyy_date(
                fields.get("Original Authority Action Served Date")
            ),
            "final_authority_action_description": clean_text(fields.get("Final Authority Action Description")),
            "final_authority_decision_date": parse_mmddyyyy_date(fields.get("Final Authority Decision Date")),
            "final_authority_served_date": parse_mmddyyyy_date(fields.get("Final Authority Served Date")),
        }

    if feed.table_name == "operating_authority_revocations":
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "usdot_number": clean_text(fields.get("USDOT Number")),
            "operating_authority_registration_type": clean_text(
                fields.get("Operating Authority Registration Type")
            ),
            "serve_date": parse_mmddyyyy_date(fields.get("Serve Date")),
            "revocation_type": clean_text(fields.get("Revocation Type")),
            "effective_date": parse_mmddyyyy_date(fields.get("Effective Date")),
        }

    if feed.table_name == "insurance_policies":
        is_removal_signal = all(
            is_blank_or_zero(fields.get(name))
            for name in (
                "Insurance Type",
                "BI&PD Class",
                "BI&PD Maximum Dollar Limit",
                "BI&PD Underlying Dollar Limit",
                "Policy Number",
                "Effective Date",
                "Form Code",
                "Insurance Company Name",
            )
        )
        insurance_type_code = clean_text(fields.get("Insurance Type"))
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "insurance_type_code": insurance_type_code,
            "insurance_type_description": INSURANCE_TYPE_DESCRIPTIONS.get(insurance_type_code or ""),
            "bipd_class_code": clean_text(fields.get("BI&PD Class")),
            "bipd_maximum_dollar_limit_thousands_usd": parse_int(fields.get("BI&PD Maximum Dollar Limit")),
            "bipd_underlying_dollar_limit_thousands_usd": parse_int(
                fields.get("BI&PD Underlying Dollar Limit")
            ),
            "policy_number": clean_text(fields.get("Policy Number")),
            "effective_date": parse_mmddyyyy_date(fields.get("Effective Date")),
            "form_code": clean_text(fields.get("Form Code")),
            "insurance_company_name": clean_text(fields.get("Insurance Company Name")),
            "is_removal_signal": is_removal_signal,
            "removal_signal_reason": "daily_diff_blank_or_zero_row" if is_removal_signal else None,
        }

    if feed.table_name == "insurance_policy_filings":
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "usdot_number": clean_text(fields.get("USDOT Number")),
            "form_code": clean_text(fields.get("Form Code")),
            "insurance_type_description": clean_text(fields.get("Insurance Type Description")),
            "insurance_company_name": clean_text(fields.get("Insurance Company Name")),
            "policy_number": clean_text(fields.get("Policy Number")),
            "posted_date": parse_mmddyyyy_date(fields.get("Posted Date")),
            "bipd_underlying_limit_thousands_usd": parse_int(fields.get("BI&PD Underlying Limit")),
            "bipd_maximum_limit_thousands_usd": parse_int(fields.get("BI&PD Maximum Limit")),
            "effective_date": parse_mmddyyyy_date(fields.get("Effective Date")),
            "cancel_effective_date": parse_mmddyyyy_date(fields.get("Cancel Effective Date")),
        }

    if feed.table_name == "insurance_policy_history_events":
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "usdot_number": clean_text(fields.get("USDOT Number")),
            "form_code": clean_text(fields.get("Form Code")),
            "cancellation_method": clean_text(fields.get("Cancellation Method")),
            "cancellation_form_code": clean_text(fields.get("Cancel/Replace/Name Change/Transfer Form")),
            "insurance_type_indicator": clean_text(fields.get("Insurance Type Indicator")),
            "insurance_type_description": clean_text(fields.get("Insurance Type Description")),
            "policy_number": clean_text(fields.get("Policy Number")),
            "minimum_coverage_amount_thousands_usd": parse_int(fields.get("Minimum Coverage Amount")),
            "insurance_class_code": clean_text(fields.get("Insurance Class Code")),
            "effective_date": parse_mmddyyyy_date(fields.get("Effective Date")),
            "bipd_underlying_limit_amount_thousands_usd": parse_int(
                fields.get("BI&PD Underlying Limit Amount")
            ),
            "bipd_max_coverage_amount_thousands_usd": parse_int(fields.get("BI&PD Max Coverage Amount")),
            "cancel_effective_date": parse_mmddyyyy_date(fields.get("Cancel Effective Date")),
            "specific_cancellation_method": clean_text(fields.get("Specific Cancellation Method")),
            "insurance_company_branch": clean_text(fields.get("Insurance Company Branch")),
            "insurance_company_name": clean_text(fields.get("Insurance Company Name")),
        }

    if feed.table_name == "carrier_registrations":
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "usdot_number": clean_text(fields.get("USDOT Number")),
            "mx_type": clean_text(fields.get("MX Type")),
            "rfc_number": clean_text(fields.get("RFC Number")),
            "common_authority_status": clean_text(fields.get("Common Authority")),
            "contract_authority_status": clean_text(fields.get("Contract Authority")),
            "broker_authority_status": clean_text(fields.get("Broker Authority")),
            "pending_common_authority": clean_text(fields.get("Pending Common Authority")),
            "pending_contract_authority": clean_text(fields.get("Pending Contract Authority")),
            "pending_broker_authority": clean_text(fields.get("Pending Broker Authority")),
            "common_authority_revocation": clean_text(fields.get("Common Authority Revocation")),
            "contract_authority_revocation": clean_text(fields.get("Contract Authority Revocation")),
            "broker_authority_revocation": clean_text(fields.get("Broker Authority Revocation")),
            "property_authority": clean_text(fields.get("Property")),
            "passenger_authority": clean_text(fields.get("Passenger")),
            "household_goods_authority": clean_text(fields.get("Household Goods")),
            "private_check": clean_text(fields.get("Private Check")),
            "enterprise_check": clean_text(fields.get("Enterprise Check")),
            "bipd_required_thousands_usd": parse_int(fields.get("BIPD Required")),
            "cargo_required": clean_text(fields.get("Cargo Required")),
            "bond_surety_required": clean_text(fields.get("Bond/Surety Required")),
            "bipd_on_file_thousands_usd": parse_int(fields.get("BIPD on File")),
            "cargo_on_file": clean_text(fields.get("Cargo on File")),
            "bond_surety_on_file": clean_text(fields.get("Bond/Surety on File")),
            "address_status": clean_text(fields.get("Address Status")),
            "dba_name": clean_text(fields.get("DBA Name")),
            "legal_name": clean_text(fields.get("Legal Name")),
            "business_address_street": clean_text(fields.get("Business Address - PO Box/Street")),
            "business_address_colonia": clean_text(fields.get("Business Address - Colonia")),
            "business_address_city": clean_text(fields.get("Business Address - City")),
            "business_address_state_code": clean_text(fields.get("Business Address - State Code")),
            "business_address_country_code": clean_text(fields.get("Business Address - Country Code")),
            "business_address_zip_code": clean_text(fields.get("Business Address - Zip Code")),
            "business_address_telephone_number": clean_text(
                fields.get("Business Address - Telephone Number")
            ),
            "business_address_fax_number": clean_text(fields.get("Business Address - Fax Number")),
            "mailing_address_street": clean_text(fields.get("Mailing Address - PO Box/Street")),
            "mailing_address_colonia": clean_text(fields.get("Mailing Address - Colonia")),
            "mailing_address_city": clean_text(fields.get("Mailing Address - City")),
            "mailing_address_state_code": clean_text(fields.get("Mailing Address - State Code")),
            "mailing_address_country_code": clean_text(fields.get("Mailing Address - Country Code")),
            "mailing_address_zip_code": clean_text(fields.get("Mailing Address - Zip Code")),
            "mailing_address_telephone_number": clean_text(
                fields.get("Mailing Address - Telephone Number")
            ),
            "mailing_address_fax_number": clean_text(fields.get("Mailing Address - Fax Number")),
        }

    if feed.table_name == "insurance_filing_rejections":
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "usdot_number": clean_text(fields.get("USDOT Number")),
            "form_code": clean_text(fields.get("Form Code (Insurance or Cancel)")),
            "insurance_type_description": clean_text(fields.get("Insurance Type Description")),
            "policy_number": clean_text(fields.get("Policy Number")),
            "received_date": parse_mmddyyyy_date(fields.get("Received Date")),
            "insurance_class_code": clean_text(fields.get("Insurance Class Code")),
            "insurance_type_code": clean_text(fields.get("Insurance Type Code")),
            "underlying_limit_amount_thousands_usd": parse_int(fields.get("Underlying Limit Amount")),
            "maximum_coverage_amount_thousands_usd": parse_int(fields.get("Maximum Coverage Amount")),
            "rejected_date": parse_mmddyyyy_date(fields.get("Rejected Date")),
            "insurance_branch": clean_text(fields.get("Insurance Branch")),
            "insurance_company_name": clean_text(fields.get("Company Name")),
            "rejected_reason": clean_text(fields.get("Rejected Reason")),
            "minimum_coverage_amount_thousands_usd": parse_int(fields.get("Minimum Coverage Amount")),
        }

    if feed.table_name == "process_agent_filings":
        return {
            "docket_number": clean_text(fields.get("Docket Number")),
            "usdot_number": clean_text(fields.get("USDOT Number")),
            "process_agent_company_name": clean_text(fields.get("Company Name")),
            "attention_to_or_title": clean_text(fields.get("Attention to or Title")),
            "street_or_po_box": clean_text(fields.get("Street or PO Box")),
            "city": clean_text(fields.get("City")),
            "state": clean_text(fields.get("State")),
            "country": clean_text(fields.get("Country")),
            "zip_code": clean_text(fields.get("Zip Code")),
        }

    if feed.table_name == "commercial_vehicle_crashes":
        return {
            "change_date_text": clean_text(fields.get("CHANGE_DATE")),
            "crash_id": clean_text(fields.get("CRASH_ID")),
            "report_state": clean_text(fields.get("REPORT_STATE")),
            "report_number": clean_text(fields.get("REPORT_NUMBER")),
            "report_date": parse_yyyymmdd_date(fields.get("REPORT_DATE")),
            "report_time_text": clean_text(fields.get("REPORT_TIME")),
            "report_sequence_number": parse_int(fields.get("REPORT_SEQ_NO")),
            "dot_number": clean_text(fields.get("DOT_NUMBER")),
            "ci_status_code": clean_text(fields.get("CI_STATUS_CODE")),
            "final_status_date": parse_yyyymmdd_date(fields.get("FINAL_STATUS_DATE")),
            "location": clean_text(fields.get("LOCATION")),
            "city_code": clean_text(fields.get("CITY_CODE")),
            "city": clean_text(fields.get("CITY")),
            "state": clean_text(fields.get("STATE")),
            "county_code": clean_text(fields.get("COUNTY_CODE")),
            "truck_bus_indicator": clean_text(fields.get("TRUCK_BUS_IND")),
            "trafficway_id": clean_text(fields.get("TRAFFICWAY_ID")),
            "access_control_id": clean_text(fields.get("ACCESS_CONTROL_ID")),
            "road_surface_condition_id": clean_text(fields.get("ROAD_SURFACE_CONDITION_ID")),
            "cargo_body_type_id": clean_text(fields.get("CARGO_BODY_TYPE_ID")),
            "gvw_rating_id": clean_text(fields.get("GVW_RATING_ID")),
            "vehicle_identification_number": clean_text(fields.get("VEHICLE_IDENTIFICATION_NUMBER")),
            "vehicle_license_number": clean_text(fields.get("VEHICLE_LICENSE_NUMBER")),
            "vehicle_license_state": clean_text(fields.get("VEHICLE_LIC_STATE")),
            "vehicle_hazmat_placard": parse_bool(fields.get("VEHICLE_HAZMAT_PLACARD")),
            "weather_condition_id": clean_text(fields.get("WEATHER_CONDITION_ID")),
            "vehicle_configuration_id": clean_text(fields.get("VEHICLE_CONFIGURATION_ID")),
            "light_condition_id": clean_text(fields.get("LIGHT_CONDITION_ID")),
            "hazmat_released": parse_bool(fields.get("HAZMAT_RELEASED")),
            "agency": clean_text(fields.get("AGENCY")),
            "vehicles_in_accident": parse_int(fields.get("VEHICLES_IN_ACCIDENT")),
            "fatalities": parse_int(fields.get("FATALITIES")),
            "injuries": parse_int(fields.get("INJURIES")),
            "tow_away": parse_bool(fields.get("TOW_AWAY")),
            "federal_recordable": parse_bool(fields.get("FEDERAL_RECORDABLE")),
            "state_recordable": parse_bool(fields.get("STATE_RECORDABLE")),
            "snet_version_number": clean_text(fields.get("SNET_VERSION_NUMBER")),
            "snet_sequence_id": clean_text(fields.get("SNET_SEQUENCE_ID")),
            "transaction_code": clean_text(fields.get("TRANSACTION_CODE")),
            "transaction_date_text": clean_text(fields.get("TRANSACTION_DATE")),
            "upload_first_byte": clean_text(fields.get("UPLOAD_FIRST_BYTE")),
            "upload_dot_number": clean_text(fields.get("UPLOAD_DOT_NUMBER")),
            "upload_search_indicator": clean_text(fields.get("UPLOAD_SEARCH_INDICATOR")),
            "upload_date_text": clean_text(fields.get("UPLOAD_DATE")),
            "add_date_text": clean_text(fields.get("ADD_DATE")),
            "crash_carrier_id": clean_text(fields.get("CRASH_CARRIER_ID")),
            "crash_carrier_name": clean_text(fields.get("CRASH_CARRIER_NAME")),
            "crash_carrier_street": clean_text(fields.get("CRASH_CARRIER_STREET")),
            "crash_carrier_city": clean_text(fields.get("CRASH_CARRIER_CITY")),
            "crash_carrier_city_code": clean_text(fields.get("CRASH_CARRIER_CITY_CODE")),
            "crash_carrier_state": clean_text(fields.get("CRASH_CARRIER_STATE")),
            "crash_carrier_zip_code": clean_text(fields.get("CRASH_CARRIER_ZIP_CODE")),
            "crash_colonia": clean_text(fields.get("CRASH_COLONIA")),
            "docket_number": clean_text(fields.get("DOCKET_NUMBER")),
            "crash_carrier_interstate_code": clean_text(fields.get("CRASH_CARRIER_INTERSTATE")),
            "no_id_flag": clean_text(fields.get("NO_ID_FLAG")),
            "state_number": clean_text(fields.get("STATE_NUMBER")),
            "state_issuing_number": clean_text(fields.get("STATE_ISSUING_NUMBER")),
            "crash_event_sequence_description": clean_text(fields.get("CRASH_EVENT_SEQ_ID_DESC")),
        }

    if feed.table_name == "out_of_service_orders":
        return {
            "dot_number": clean_text(fields.get("DOT_NUMBER")),
            "legal_name": clean_text(fields.get("LEGAL_NAME")),
            "dba_name": clean_text(fields.get("DBA_NAME")),
            "oos_date": parse_iso_date(fields.get("OOS_DATE")),
            "oos_reason": clean_text(fields.get("OOS_REASON")),
            "status": clean_text(fields.get("STATUS")),
            "oos_rescind_date": parse_iso_date(fields.get("OOS_RESCIND_DATE")),
        }

    if feed.table_name == "vehicle_inspection_units":
        return {
            "change_date_text": clean_text(fields.get("CHANGE_DATE")),
            "inspection_id": clean_text(fields.get("INSPECTION_ID")),
            "inspection_unit_id": clean_text(fields.get("INSP_UNIT_ID")),
            "inspection_unit_type_id": parse_int(fields.get("INSP_UNIT_TYPE_ID")),
            "inspection_unit_number": parse_int(fields.get("INSP_UNIT_NUMBER")),
            "inspection_unit_make": clean_text(fields.get("INSP_UNIT_MAKE")),
            "inspection_unit_company_number": clean_text(fields.get("INSP_UNIT_COMPANY")),
            "inspection_unit_license": clean_text(fields.get("INSP_UNIT_LICENSE")),
            "inspection_unit_license_state": clean_text(fields.get("INSP_UNIT_LICENSE_STATE")),
            "inspection_unit_vin": clean_text(fields.get("INSP_UNIT_VEHICLE_ID_NUMBER")),
            "inspection_unit_decal_flag": clean_text(fields.get("INSP_UNIT_DECAL")),
            "inspection_unit_decal_number": clean_text(fields.get("INSP_UNIT_DECAL_NUMBER")),
        }

    if feed.table_name == "vehicle_inspection_special_studies":
        return {
            "change_date_text": clean_text(fields.get("CHANGE_DATE")),
            "inspection_id": clean_text(fields.get("INSPECTION_ID")),
            "inspection_study_id": clean_text(fields.get("INSP_STUDY_ID")),
            "study": clean_text(fields.get("STUDY")),
            "sequence_number": parse_int(fields.get("SEQ_NO")),
        }

    if feed.table_name == "vehicle_inspection_citations":
        return {
            "change_date_text": clean_text(fields.get("CHANGE_DATE")),
            "inspection_id": clean_text(fields.get("INSPECTION_ID")),
            "violation_sequence_number": parse_int(fields.get("VIOSEQNUM")),
            "adjusted_sequence_number": parse_int(fields.get("ADJSEQ")),
            "citation_code": clean_text(fields.get("CITATION_CODE")),
            "citation_result": clean_text(fields.get("CITATION_RESULT")),
        }

    if feed.table_name == "source_fmcsa_vehicle_inspection_violations":
        return {
            "inspection_unique_id": clean_text(fields.get("INSPECTION_ID")),
            "change_date_text": clean_text(fields.get("CHANGE_DATE")),
            "inspection_violation_id": clean_text(fields.get("INSP_VIOLATION_ID")),
            "violation_sequence_number": parse_int(fields.get("SEQ_NO")),
            "part_number": clean_text(fields.get("PART_NO")),
            "part_number_section": clean_text(fields.get("PART_NO_SECTION")),
            "violation_unit": clean_text(fields.get("INSP_VIOL_UNIT")),
            "inspection_unit_id": clean_text(fields.get("INSP_UNIT_ID")),
            "violation_category_id": parse_int(fields.get("INSP_VIOLATION_CATEGORY_ID")),
            "oos_indicator": _parse_mcmis_oos_indicator(fields.get("OUT_OF_SERVICE_INDICATOR")),
            "out_of_service_indicator_code": clean_text(fields.get("OUT_OF_SERVICE_INDICATOR")),
            "defect_verification_id": parse_int(fields.get("DEFECT_VERIFICATION_ID")),
            "citation_number": clean_text(fields.get("CITATION_NUMBER")),
        }

    if feed.table_name == "source_fmcsa_sms_input_violations":
        return {
            "inspection_unique_id": clean_text(fields.get("Unique_ID")),
            "inspection_date": parse_fmcsa_date(fields.get("Insp_Date")),
            "dot_number": clean_text(fields.get("DOT_Number")),
            "violation_code": clean_text(fields.get("Viol_Code")),
            "basic_description": clean_text(fields.get("BASIC_Desc")),
            "oos_indicator": parse_bool(fields.get("OOS_Indicator")),
            "oos_weight": parse_int(fields.get("OOS_Weight")),
            "severity_weight": parse_int(fields.get("Severity_Weight")),
            "time_weight": parse_int(fields.get("Time_Weight")),
            "total_severity_weight": parse_int(fields.get("Total_Severity_Wght")),
            "section_description": clean_text(fields.get("Section_Desc")),
            "group_description": clean_text(fields.get("Group_Desc")),
            "violation_unit": clean_text(fields.get("Viol_Unit")),
        }

    if feed.table_name == "carrier_inspections":
        if feed_name == "Vehicle Inspection File":
            return {
                "inspection_unique_id": clean_text(fields.get("INSPECTION_ID")),
                "report_number": clean_text(fields.get("REPORT_NUMBER")),
                "report_state": clean_text(fields.get("REPORT_STATE")),
                "dot_number": clean_text(fields.get("DOT_NUMBER")),
                "inspection_date": parse_yyyymmdd_date(fields.get("INSP_DATE")),
                "inspection_level_id": parse_int(fields.get("INSP_LEVEL_ID")),
                "county_code_state": clean_text(fields.get("COUNTY_CODE_STATE")),
                "hazmat_placard_required": parse_bool(fields.get("HAZMAT_PLACARD_REQ")),
                "change_date_text": clean_text(fields.get("CHANGE_DATE")),
                "inspection_start_time_text": clean_text(fields.get("INSP_START_TIME")),
                "inspection_end_time_text": clean_text(fields.get("INSP_END_TIME")),
                "registration_date": parse_yyyymmdd_date(fields.get("REGISTRATION_DATE")),
                "region_code": clean_text(fields.get("REGION")),
                "ci_status_code": clean_text(fields.get("CI_STATUS_CODE")),
                "location_code": clean_text(fields.get("LOCATION")),
                "location_description": clean_text(fields.get("LOCATION_DESC")),
                "county_code": clean_text(fields.get("COUNTY_CODE")),
                "service_center": clean_text(fields.get("SERVICE_CENTER")),
                "census_source_id": parse_int(fields.get("CENSUS_SOURCE_ID")),
                "inspection_facility_code": clean_text(fields.get("INSP_FACILITY")),
                "shipper_name": clean_text(fields.get("SHIPPER_NAME")),
                "shipping_paper_number": clean_text(fields.get("SHIPPING_PAPER_NUMBER")),
                "cargo_tank_code": clean_text(fields.get("CARGO_TANK")),
                "snet_version_number": clean_text(fields.get("SNET_VERSION_NUMBER")),
                "snet_search_date_text": clean_text(fields.get("SNET_SEARCH_DATE")),
                "alcohol_control_substance_code": clean_text(fields.get("ALCOHOL_CONTROL_SUB")),
                "drug_interdiction_search_code": clean_text(fields.get("DRUG_INTRDCTN_SEARCH")),
                "drug_interdiction_arrests": parse_int(fields.get("DRUG_INTRDCTN_ARRESTS")),
                "size_weight_enforcement_code": clean_text(fields.get("SIZE_WEIGHT_ENF")),
                "traffic_enforcement_code": clean_text(fields.get("TRAFFIC_ENF")),
                "local_enforcement_jurisdiction_code": clean_text(fields.get("LOCAL_ENF_JURISDICTION")),
                "pen_census_match_code": clean_text(fields.get("PEN_CEN_MATCH")),
                "final_status_date_text": clean_text(fields.get("FINAL_STATUS_DATE")),
                "post_accident_indicator_code": clean_text(fields.get("POST_ACC_IND")),
                "gross_combination_vehicle_weight_pounds": parse_int(fields.get("GROSS_COMB_VEH_WT")),
                "total_violation_count": parse_int(fields.get("VIOL_TOTAL")),
                "total_out_of_service_count": parse_int(fields.get("OOS_TOTAL")),
                "driver_violation_count": parse_int(fields.get("DRIVER_VIOL_TOTAL")),
                "driver_out_of_service_count": parse_int(fields.get("DRIVER_OOS_TOTAL")),
                "vehicle_violation_count": parse_int(fields.get("VEHICLE_VIOL_TOTAL")),
                "vehicle_out_of_service_count": parse_int(fields.get("VEHICLE_OOS_TOTAL")),
                "hazmat_violation_count": parse_int(fields.get("HAZMAT_VIOL_TOTAL")),
                "hazmat_out_of_service_count": parse_int(fields.get("HAZMAT_OOS_TOTAL")),
                "snet_sequence_id_text": clean_text(fields.get("SNET_SEQUENCE_ID")),
                "transaction_code": clean_text(fields.get("TRANSACTION_CODE")),
                "transaction_date_text": clean_text(fields.get("TRANSACTION_DATE")),
                "upload_date_text": clean_text(fields.get("UPLOAD_DATE")),
                "upload_first_byte": clean_text(fields.get("UPLOAD_FIRST_BYTE")),
                "upload_dot_number": clean_text(fields.get("UPLOAD_DOT_NUMBER")),
                "upload_search_indicator": clean_text(fields.get("UPLOAD_SEARCH_INDICATOR")),
                "census_search_date_text": clean_text(fields.get("CENSUS_SEARCH_DATE")),
                "snet_input_date_text": clean_text(fields.get("SNET_INPUT_DATE")),
                "source_office": clean_text(fields.get("SOURCE_OFFICE")),
                "mcmis_add_date_text": clean_text(fields.get("MCMIS_ADD_DATE")),
                "carrier_name": clean_text(fields.get("INSP_CARRIER_NAME")),
                "carrier_street": clean_text(fields.get("INSP_CARRIER_STREET")),
                "carrier_city": clean_text(fields.get("INSP_CARRIER_CITY")),
                "carrier_state": clean_text(fields.get("INSP_CARRIER_STATE")),
                "carrier_zip_code": clean_text(fields.get("INSP_CARRIER_ZIP_CODE")),
                "carrier_colonia": clean_text(fields.get("INSP_COLONIA")),
                "docket_number": clean_text(fields.get("DOCKET_NUMBER")),
                "interstate_operation_code": clean_text(fields.get("INSP_INTERSTATE")),
                "carrier_state_id": clean_text(fields.get("INSP_CARRIER_STATE_ID")),
            }
        return {
            "inspection_unique_id": clean_text(fields.get("Unique_ID")),
            "report_number": clean_text(fields.get("Report_Number")),
            "report_state": clean_text(fields.get("Report_State")),
            "dot_number": clean_text(fields.get("DOT_Number")),
            "inspection_date": parse_fmcsa_date(fields.get("Insp_Date")),
            "inspection_level_id": parse_int(fields.get("Insp_level_ID")),
            "county_code_state": clean_text(fields.get("County_code_State")),
            "time_weight": parse_int(fields.get("Time_Weight")),
            "driver_oos_total": parse_int(fields.get("Driver_OOS_Total")),
            "vehicle_oos_total": parse_int(fields.get("Vehicle_OOS_Total")),
            "total_hazmat_sent": parse_int(fields.get("Total_Hazmat_Sent")),
            "oos_total": parse_int(fields.get("OOS_Total")),
            "hazmat_oos_total": parse_int(fields.get("Hazmat_OOS_Total")),
            "hazmat_placard_required": parse_bool(fields.get("Hazmat_Placard_req")),
            "primary_unit_type_description": clean_text(fields.get("Unit_Type_Desc")),
            "primary_unit_make": clean_text(fields.get("Unit_Make")),
            "primary_unit_license": clean_text(fields.get("Unit_License")),
            "primary_unit_license_state": clean_text(fields.get("Unit_License_State")),
            "primary_unit_vin": clean_text(fields.get("VIN")),
            "primary_unit_decal_number": clean_text(fields.get("Unit_Decal_Number")),
            "secondary_unit_type_description": clean_text(fields.get("Unit_Type_Desc2")),
            "secondary_unit_make": clean_text(fields.get("Unit_Make2")),
            "secondary_unit_license": clean_text(fields.get("Unit_License2")),
            "secondary_unit_license_state": clean_text(fields.get("Unit_License_State2")),
            "secondary_unit_vin": clean_text(fields.get("VIN2")),
            "secondary_unit_decal_number": clean_text(fields.get("Unit_Decal_Number2")),
            "unsafe_driving_inspection": parse_bool(fields.get("Unsafe_Insp")),
            "hours_of_service_inspection": parse_bool(fields.get("Fatigued_Insp")),
            "driver_fitness_inspection": parse_bool(fields.get("Dr_Fitness_Insp")),
            "controlled_substances_alcohol_inspection": parse_bool(fields.get("Subt_Alcohol_Insp")),
            "vehicle_maintenance_inspection": parse_bool(fields.get("Vh_Maint_Insp")),
            "hazmat_inspection": parse_bool(fields.get("HM_Insp")),
            "basic_violation_total": parse_int(fields.get("BASIC_Viol")),
            "unsafe_driving_violation_total": parse_int(fields.get("Unsafe_Viol")),
            "hours_of_service_violation_total": parse_int(fields.get("Fatigued_Viol")),
            "driver_fitness_violation_total": parse_int(fields.get("Dr_Fitness_Viol")),
            "controlled_substances_alcohol_violation_total": parse_int(fields.get("Subt_Alcohol_Viol")),
            "vehicle_maintenance_violation_total": parse_int(fields.get("Vh_Maint_Viol")),
            "hazmat_violation_total": parse_int(fields.get("HM_Viol")),
        }

    if feed.table_name == "motor_carrier_census_records":
        if feed_name == "Company Census File":
            classdef_text = clean_text(fields.get("CLASSDEF"))
            classification_flags = _parse_classification_flags(classdef_text)
            return {
                "dot_number": clean_text(fields.get("DOT_NUMBER")),
                "legal_name": clean_text(fields.get("LEGAL_NAME")),
                "dba_name": clean_text(fields.get("DBA_NAME")),
                "carrier_operation_code": clean_text(fields.get("CARRIER_OPERATION")),
                "hazmat_flag": parse_bool(fields.get("HM_Ind")),
                "physical_street": clean_text(fields.get("PHY_STREET")),
                "physical_city": clean_text(fields.get("PHY_CITY")),
                "physical_state": clean_text(fields.get("PHY_STATE")),
                "physical_zip": clean_text(fields.get("PHY_ZIP")),
                "physical_country": clean_text(fields.get("PHY_COUNTRY")),
                "mailing_street": clean_text(fields.get("CARRIER_MAILING_STREET")),
                "mailing_city": clean_text(fields.get("CARRIER_MAILING_CITY")),
                "mailing_state": clean_text(fields.get("CARRIER_MAILING_STATE")),
                "mailing_zip": clean_text(fields.get("CARRIER_MAILING_ZIP")),
                "mailing_country": clean_text(fields.get("CARRIER_MAILING_COUNTRY")),
                "telephone": clean_text(fields.get("PHONE")),
                "fax": clean_text(fields.get("FAX")),
                "email_address": clean_text(fields.get("EMAIL_ADDRESS")),
                "mcs150_date": parse_yyyymmdd_date(fields.get("MCS150_DATE")),
                "mcs150_mileage": parse_int(fields.get("MCS150_MILEAGE")),
                "mcs150_mileage_year": parse_int(fields.get("MCS150_MILEAGE_YEAR")),
                "add_date": parse_yyyymmdd_date(fields.get("ADD_DATE")),
                "power_unit_count": parse_int(fields.get("POWER_UNITS")),
                "driver_total": parse_int(fields.get("TOTAL_DRIVERS")),
                "private_only": classification_flags["private_only"],
                "authorized_for_hire": classification_flags["authorized_for_hire"],
                "exempt_for_hire": classification_flags["exempt_for_hire"],
                "private_property": classification_flags["private_property"],
                "private_passenger_business": classification_flags["private_passenger_business"],
                "private_passenger_nonbusiness": classification_flags["private_passenger_nonbusiness"],
                "migrant": classification_flags["migrant"],
                "us_mail": classification_flags["us_mail"],
                "federal_government": classification_flags["federal_government"],
                "state_government": classification_flags["state_government"],
                "local_government": classification_flags["local_government"],
                "indian_tribe": classification_flags["indian_tribe"],
                "other_operation_description": classification_flags["other_operation_description"],
                "status_code": clean_text(fields.get("STATUS_CODE")),
                "dun_bradstreet_number": clean_text(fields.get("DUN_BRADSTREET_NO")),
                "physical_omc_region": parse_int(fields.get("PHY_OMC_REGION")),
                "safety_investigator_territory_code": clean_text(fields.get("SAFETY_INV_TERR")),
                "business_organization_id": clean_text(fields.get("BUSINESS_ORG_ID")),
                "mcs151_mileage": parse_int(fields.get("MCS151_MILEAGE")),
                "total_cars": parse_int(fields.get("TOTAL_CARS")),
                "mcs150_update_code_id": clean_text(fields.get("MCS150_UPDATE_CODE_ID")),
                "prior_revoke_flag": parse_bool(fields.get("PRIOR_REVOKE_FLAG")),
                "prior_revoke_dot_number": clean_text(fields.get("PRIOR_REVOKE_DOT_NUMBER")),
                "cell_phone": clean_text(fields.get("CELL_PHONE")),
                "company_officer_1": clean_text(fields.get("COMPANY_OFFICER_1")),
                "company_officer_2": clean_text(fields.get("COMPANY_OFFICER_2")),
                "business_organization_description": clean_text(fields.get("BUSINESS_ORG_DESC")),
                "truck_units": parse_int(fields.get("TRUCK_UNITS")),
                "bus_units": parse_int(fields.get("BUS_UNITS")),
                "fleet_size_code": clean_text(fields.get("FLEETSIZE")),
                "review_id": clean_text(fields.get("REVIEW_ID")),
                "recordable_crash_rate": parse_float(fields.get("RECORDABLE_CRASH_RATE")),
                "mail_nationality_indicator": clean_text(fields.get("MAIL_NATIONALITY_INDICATOR")),
                "physical_nationality_indicator": clean_text(fields.get("PHY_NATIONALITY_INDICATOR")),
                "physical_barrio": clean_text(fields.get("PHY_BARRIO")),
                "mailing_barrio": clean_text(fields.get("MAIL_BARRIO")),
                "entity_type_code": clean_text(fields.get("CARSHIP")),
                "docket1_prefix": clean_text(fields.get("DOCKET1PREFIX")),
                "docket1_number": clean_text(fields.get("DOCKET1")),
                "docket2_prefix": clean_text(fields.get("DOCKET2PREFIX")),
                "docket2_number": clean_text(fields.get("DOCKET2")),
                "docket3_prefix": clean_text(fields.get("DOCKET3PREFIX")),
                "docket3_number": clean_text(fields.get("DOCKET3")),
                "point_number": clean_text(fields.get("POINTNUM")),
                "total_intrastate_drivers": parse_int(fields.get("TOTAL_INTRASTATE_DRIVERS")),
                "mcsip_step": parse_int(fields.get("MCSIPSTEP")),
                "mcsip_date": parse_yyyymmdd_date(fields.get("MCSIPDATE")),
                "interstate_beyond_100_miles_drivers": parse_int(
                    fields.get("INTERSTATE_BEYOND_100_MILES")
                ),
                "interstate_within_100_miles_drivers": parse_int(
                    fields.get("INTERSTATE_WITHIN_100_MILES")
                ),
                "intrastate_beyond_100_miles_drivers": parse_int(
                    fields.get("INTRASTATE_BEYOND_100_MILES")
                ),
                "intrastate_within_100_miles_drivers": parse_int(
                    fields.get("INTRASTATE_WITHIN_100_MILES")
                ),
                "total_cdl_drivers": parse_int(fields.get("TOTAL_CDL")),
                "average_trip_leased_drivers_per_month": parse_int(
                    fields.get("AVG_DRIVERS_LEASED_PER_MONTH")
                ),
                "classdef_text": classdef_text,
                "physical_county_code": clean_text(fields.get("PHY_CNTY")),
                "mailing_county_code": clean_text(fields.get("CARRIER_MAILING_CNTY")),
                "mailing_undeliverable_date": parse_yyyymmdd_date(fields.get("CARRIER_MAILING_UND_DATE")),
                "driver_inter_total": parse_int(fields.get("DRIVER_INTER_TOTAL")),
                "review_type_code": clean_text(fields.get("REVIEW_TYPE")),
                "review_date": parse_yyyymmdd_date(fields.get("REVIEW_DATE")),
                "safety_rating_code": clean_text(fields.get("SAFETY_RATING")),
                "safety_rating_date": parse_yyyymmdd_date(fields.get("SAFETY_RATING_DATE")),
                "undeliverable_physical_code": clean_text(fields.get("UNDELIV_PHY")),
                "cargo_general_freight": parse_x_flag(fields.get("CRGO_GENFREIGHT")),
                "cargo_household_goods": parse_x_flag(fields.get("CRGO_HOUSEHOLD")),
                "cargo_metal_sheets_coils_rolls": parse_x_flag(fields.get("CRGO_METALSHEET")),
                "cargo_motor_vehicles": parse_x_flag(fields.get("CRGO_MOTOVEH")),
                "cargo_driveaway_towaway": parse_x_flag(fields.get("CRGO_DRIVETOW")),
                "cargo_logs_poles_beams_lumber": parse_x_flag(fields.get("CRGO_LOGPOLE")),
                "cargo_building_materials": parse_x_flag(fields.get("CRGO_BLDGMAT")),
                "cargo_mobile_homes": parse_x_flag(fields.get("CRGO_MOBILEHOME")),
                "cargo_machinery_large_objects": parse_x_flag(fields.get("CRGO_MACHLRG")),
                "cargo_fresh_produce": parse_x_flag(fields.get("CRGO_PRODUCE")),
                "cargo_liquids_gases": parse_x_flag(fields.get("CRGO_LIQGAS")),
                "cargo_intermodal_containers": parse_x_flag(fields.get("CRGO_INTERMODAL")),
                "cargo_passengers": parse_x_flag(fields.get("CRGO_PASSENGERS")),
                "cargo_oilfield_equipment": parse_x_flag(fields.get("CRGO_OILFIELD")),
                "cargo_livestock": parse_x_flag(fields.get("CRGO_LIVESTOCK")),
                "cargo_grain_feed_hay": parse_x_flag(fields.get("CRGO_GRAINFEED")),
                "cargo_coal_coke": parse_x_flag(fields.get("CRGO_COALCOKE")),
                "cargo_meat": parse_x_flag(fields.get("CRGO_MEAT")),
                "cargo_garbage_refuse_trash": parse_x_flag(fields.get("CRGO_GARBAGE")),
                "cargo_us_mail": parse_x_flag(fields.get("CRGO_USMAIL")),
                "cargo_chemicals": parse_x_flag(fields.get("CRGO_CHEM")),
                "cargo_dry_bulk_commodities": parse_x_flag(fields.get("CRGO_DRYBULK")),
                "cargo_refrigerated_food": parse_x_flag(fields.get("CRGO_COLDFOOD")),
                "cargo_beverages": parse_x_flag(fields.get("CRGO_BEVERAGES")),
                "cargo_paper_products": parse_x_flag(fields.get("CRGO_PAPERPROD")),
                "cargo_utility": parse_x_flag(fields.get("CRGO_UTILITY")),
                "cargo_farm_supplies": parse_x_flag(fields.get("CRGO_FARMSUPP")),
                "cargo_construction": parse_x_flag(fields.get("CRGO_CONSTRUCT")),
                "cargo_water_well": parse_x_flag(fields.get("CRGO_WATERWELL")),
                "cargo_other": parse_x_flag(fields.get("CRGO_CARGOOTHR")),
                "cargo_other_description": clean_text(fields.get("CRGO_CARGOOTHR_DESC")),
                "owned_truck_units": parse_int(fields.get("OWNTRUCK")),
                "owned_tractor_units": parse_int(fields.get("OWNTRACT")),
                "owned_trailer_units": parse_int(fields.get("OWNTRAIL")),
                "owned_motor_coach_units": parse_int(fields.get("OWNCOACH")),
                "owned_school_bus_1_8_units": parse_int(fields.get("OWNSCHOOL_1_8")),
                "owned_school_bus_9_15_units": parse_int(fields.get("OWNSCHOOL_9_15")),
                "owned_school_bus_16_plus_units": parse_int(fields.get("OWNSCHOOL_16")),
                "owned_minibus_van_16_plus_units": parse_int(fields.get("OWNBUS_16")),
                "owned_minibus_van_1_8_units": parse_int(fields.get("OWNVAN_1_8")),
                "owned_minibus_van_9_15_units": parse_int(fields.get("OWNVAN_9_15")),
                "owned_limo_1_8_units": parse_int(fields.get("OWNLIMO_1_8")),
                "owned_limo_9_15_units": parse_int(fields.get("OWNLIMO_9_15")),
                "owned_limo_16_plus_units": parse_int(fields.get("OWNLIMO_16")),
                "term_leased_truck_units": parse_int(fields.get("TRMTRUCK")),
                "term_leased_tractor_units": parse_int(fields.get("TRMTRACT")),
                "term_leased_trailer_units": parse_int(fields.get("TRMTRAIL")),
                "term_leased_motor_coach_units": parse_int(fields.get("TRMCOACH")),
                "term_leased_school_bus_1_8_units": parse_int(fields.get("TRMSCHOOL_1_8")),
                "term_leased_school_bus_9_15_units": parse_int(fields.get("TRMSCHOOL_9_15")),
                "term_leased_school_bus_16_plus_units": parse_int(fields.get("TRMSCHOOL_16")),
                "term_leased_minibus_van_16_plus_units": parse_int(fields.get("TRMBUS_16")),
                "term_leased_minibus_van_1_8_units": parse_int(fields.get("TRMVAN_1_8")),
                "term_leased_minibus_van_9_15_units": parse_int(fields.get("TRMVAN_9_15")),
                "term_leased_limo_1_8_units": parse_int(fields.get("TRMLIMO_1_8")),
                "term_leased_limo_9_15_units": parse_int(fields.get("TRMLIMO_9_15")),
                "term_leased_limo_16_plus_units": parse_int(fields.get("TRMLIMO_16")),
                "trip_leased_truck_units": parse_int(fields.get("TRPTRUCK")),
                "trip_leased_tractor_units": parse_int(fields.get("TRPTRACT")),
                "trip_leased_trailer_units": parse_int(fields.get("TRPTRAIL")),
                "trip_leased_motor_coach_units": parse_int(fields.get("TRPCOACH")),
                "trip_leased_school_bus_1_8_units": parse_int(fields.get("TRPSCHOOL_1_8")),
                "trip_leased_school_bus_9_15_units": parse_int(fields.get("TRPSCHOOL_9_15")),
                "trip_leased_school_bus_16_plus_units": parse_int(fields.get("TRPSCHOOL_16")),
                "trip_leased_minibus_van_16_plus_units": parse_int(fields.get("TRPBUS_16")),
                "trip_leased_minibus_van_1_8_units": parse_int(fields.get("TRPVAN_1_8")),
                "trip_leased_minibus_van_9_15_units": parse_int(fields.get("TRPVAN_9_15")),
                "trip_leased_limo_1_8_units": parse_int(fields.get("TRPLIMO_1_8")),
                "trip_leased_limo_9_15_units": parse_int(fields.get("TRPLIMO_9_15")),
                "trip_leased_limo_16_plus_units": parse_int(fields.get("TRPLIMO_16")),
                "docket1_status_code": clean_text(fields.get("DOCKET1_STATUS_CODE")),
                "docket2_status_code": clean_text(fields.get("DOCKET2_STATUS_CODE")),
                "docket3_status_code": clean_text(fields.get("DOCKET3_STATUS_CODE")),
            }
        return {
            "dot_number": clean_text(fields.get("DOT_NUMBER")),
            "legal_name": clean_text(fields.get("LEGAL_NAME")),
            "dba_name": clean_text(fields.get("DBA_NAME")),
            "carrier_operation_code": clean_text(fields.get("CARRIER_OPERATION")),
            "hazmat_flag": parse_bool(fields.get("HM_FLAG")),
            "passenger_carrier_flag": parse_bool(fields.get("PC_FLAG")),
            "physical_street": clean_text(fields.get("PHY_STREET")),
            "physical_city": clean_text(fields.get("PHY_CITY")),
            "physical_state": clean_text(fields.get("PHY_STATE")),
            "physical_zip": clean_text(fields.get("PHY_ZIP")),
            "physical_country": clean_text(fields.get("PHY_COUNTRY")),
            "mailing_street": clean_text(fields.get("MAILING_STREET")),
            "mailing_city": clean_text(fields.get("MAILING_CITY")),
            "mailing_state": clean_text(fields.get("MAILING_STATE")),
            "mailing_zip": clean_text(fields.get("MAILING_ZIP")),
            "mailing_country": clean_text(fields.get("MAILING_COUNTRY")),
            "telephone": clean_text(fields.get("TELEPHONE")),
            "fax": clean_text(fields.get("FAX")),
            "email_address": clean_text(fields.get("EMAIL_ADDRESS")),
            "mcs150_date": parse_fmcsa_date(fields.get("MCS150_DATE")),
            "mcs150_mileage": parse_int(fields.get("MCS150_MILEAGE")),
            "mcs150_mileage_year": parse_int(fields.get("MCS150_MILEAGE_YEAR")),
            "add_date": parse_fmcsa_date(fields.get("ADD_DATE")),
            "oic_state": clean_text(fields.get("OIC_STATE")),
            "power_unit_count": parse_int(fields.get("NBR_POWER_UNIT")),
            "driver_total": parse_int(fields.get("DRIVER_TOTAL")),
            "recent_mileage": parse_int(fields.get("RECENT_MILEAGE")),
            "recent_mileage_year": parse_int(fields.get("RECENT_MILEAGE_YEAR")),
            "vmt_source_id": parse_int(fields.get("VMT_SOURCE_ID")),
            "private_only": parse_bool(fields.get("PRIVATE_ONLY")),
            "authorized_for_hire": parse_bool(fields.get("AUTHORIZED_FOR_HIRE")),
            "exempt_for_hire": parse_bool(fields.get("EXEMPT_FOR_HIRE")),
            "private_property": parse_bool(fields.get("PRIVATE_PROPERTY")),
            "private_passenger_business": parse_bool(fields.get("PRIVATE_PASSENGER_BUSINESS")),
            "private_passenger_nonbusiness": parse_bool(fields.get("PRIVATE_PASSENGER_NONBUSINESS")),
            "migrant": parse_bool(fields.get("MIGRANT")),
            "us_mail": parse_bool(fields.get("US_MAIL")),
            "federal_government": parse_bool(fields.get("FEDERAL_GOVERNMENT")),
            "state_government": parse_bool(fields.get("STATE_GOVERNMENT")),
            "local_government": parse_bool(fields.get("LOCAL_GOVERNMENT")),
            "indian_tribe": parse_bool(fields.get("INDIAN_TRIBE")),
            "other_operation_description": clean_text(fields.get("OP_OTHER")),
        }

    if feed.table_name == "carrier_safety_basic_measures":
        carrier_segment = (
            "interstate_and_intrastate_hazmat_property_or_passenger"
            if feed_name == "SMS AB PassProperty"
            else "intrastate_non_hazmat_property_or_passenger"
        )
        return {
            "carrier_segment": carrier_segment,
            "dot_number": clean_text(fields.get("DOT_NUMBER")),
            "inspection_total": parse_int(fields.get("INSP_TOTAL")),
            "driver_inspection_total": parse_int(fields.get("DRIVER_INSP_TOTAL")),
            "driver_oos_inspection_total": parse_int(fields.get("DRIVER_OOS_INSP_TOTAL")),
            "vehicle_inspection_total": parse_int(fields.get("VEHICLE_INSP_TOTAL")),
            "vehicle_oos_inspection_total": parse_int(fields.get("VEHICLE_OOS_INSP_TOTAL")),
            "unsafe_driving_inspections_with_violations": parse_int(fields.get("UNSAFE_DRIV_INSP_W_VIOL")),
            "unsafe_driving_measure": parse_float(fields.get("UNSAFE_DRIV_MEASURE")),
            "unsafe_driving_acute_critical": parse_bool(fields.get("UNSAFE_DRIV_AC")),
            "hours_of_service_inspections_with_violations": parse_int(fields.get("HOS_DRIV_INSP_W_VIOL")),
            "hours_of_service_measure": parse_float(fields.get("HOS_DRIV_MEASURE")),
            "hours_of_service_acute_critical": parse_bool(fields.get("HOS_DRIV_AC")),
            "driver_fitness_inspections_with_violations": parse_int(fields.get("DRIV_FIT_INSP_W_VIOL")),
            "driver_fitness_measure": parse_float(fields.get("DRIV_FIT_MEASURE")),
            "driver_fitness_acute_critical": parse_bool(fields.get("DRIV_FIT_AC")),
            "controlled_substances_alcohol_inspections_with_violations": parse_int(
                fields.get("CONTR_SUBST_INSP_W_VIOL")
            ),
            "controlled_substances_alcohol_measure": parse_float(fields.get("CONTR_SUBST_MEASURE")),
            "controlled_substances_alcohol_acute_critical": parse_bool(fields.get("CONTR_SUBST_AC")),
            "vehicle_maintenance_inspections_with_violations": parse_int(
                fields.get("VEH_MAINT_INSP_W_VIOL")
            ),
            "vehicle_maintenance_measure": parse_float(fields.get("VEH_MAINT_MEASURE")),
            "vehicle_maintenance_acute_critical": parse_bool(fields.get("VEH_MAINT_AC")),
        }

    if feed.table_name == "carrier_safety_basic_percentiles":
        carrier_segment = (
            "interstate_and_intrastate_hazmat_passenger"
            if feed_name == "SMS AB Pass"
            else "intrastate_passenger"
        )
        return {
            "carrier_segment": carrier_segment,
            "dot_number": clean_text(fields.get("DOT_NUMBER")),
            "inspection_total": parse_int(fields.get("INSP_TOTAL")),
            "driver_inspection_total": parse_int(fields.get("DRIVER_INSP_TOTAL")),
            "driver_oos_inspection_total": parse_int(fields.get("DRIVER_OOS_INSP_TOTAL")),
            "vehicle_inspection_total": parse_int(fields.get("VEHICLE_INSP_TOTAL")),
            "vehicle_oos_inspection_total": parse_int(fields.get("VEHICLE_OOS_INSP_TOTAL")),
            "unsafe_driving_inspections_with_violations": parse_int(fields.get("UNSAFE_DRIV_INSP_W_VIOL")),
            "unsafe_driving_measure": parse_float(fields.get("UNSAFE_DRIV_MEASURE")),
            "unsafe_driving_percentile": parse_float(fields.get("UNSAFE_DRIV_PCT")),
            "unsafe_driving_roadside_alert": parse_bool(fields.get("UNSAFE_DRIV_RD_ALERT")),
            "unsafe_driving_acute_critical": parse_bool(fields.get("UNSAFE_DRIV_AC")),
            "unsafe_driving_basic_alert": parse_bool(fields.get("UNSAFE_DRIV_BASIC_ALERT")),
            "hours_of_service_inspections_with_violations": parse_int(fields.get("HOS_DRIV_INSP_W_VIOL")),
            "hours_of_service_measure": parse_float(fields.get("HOS_DRIV_MEASURE")),
            "hours_of_service_percentile": parse_float(fields.get("HOS_DRIV_PCT")),
            "hours_of_service_roadside_alert": parse_bool(fields.get("HOS_DRIV_RD_ALERT")),
            "hours_of_service_acute_critical": parse_bool(fields.get("HOS_DRIV_AC")),
            "hours_of_service_basic_alert": parse_bool(fields.get("HOS_DRIV_BASIC_ALERT")),
            "driver_fitness_inspections_with_violations": parse_int(fields.get("DRIV_FIT_INSP_W_VIOL")),
            "driver_fitness_measure": parse_float(fields.get("DRIV_FIT_MEASURE")),
            "driver_fitness_percentile": parse_float(fields.get("DRIV_FIT_PCT")),
            "driver_fitness_roadside_alert": parse_bool(fields.get("DRIV_FIT_RD_ALERT")),
            "driver_fitness_acute_critical": parse_bool(fields.get("DRIV_FIT_AC")),
            "driver_fitness_basic_alert": parse_bool(fields.get("DRIV_FIT_BASIC_ALERT")),
            "controlled_substances_alcohol_inspections_with_violations": parse_int(
                fields.get("CONTR_SUBST_INSP_W_VIOL")
            ),
            "controlled_substances_alcohol_measure": parse_float(fields.get("CONTR_SUBST_MEASURE")),
            "controlled_substances_alcohol_percentile": parse_float(fields.get("CONTR_SUBST_PCT")),
            "controlled_substances_alcohol_roadside_alert": parse_bool(
                fields.get("CONTR_SUBST_RD_ALERT")
            ),
            "controlled_substances_alcohol_acute_critical": parse_bool(fields.get("CONTR_SUBST_AC")),
            "controlled_substances_alcohol_basic_alert": parse_bool(
                fields.get("CONTR_SUBST_BASIC_ALERT")
            ),
            "vehicle_maintenance_inspections_with_violations": parse_int(
                fields.get("VEH_MAINT_INSP_W_VIOL")
            ),
            "vehicle_maintenance_measure": parse_float(fields.get("VEH_MAINT_MEASURE")),
            "vehicle_maintenance_percentile": parse_float(fields.get("VEH_MAINT_PCT")),
            "vehicle_maintenance_roadside_alert": parse_bool(fields.get("VEH_MAINT_RD_ALERT")),
            "vehicle_maintenance_acute_critical": parse_bool(fields.get("VEH_MAINT_AC")),
            "vehicle_maintenance_basic_alert": parse_bool(fields.get("VEH_MAINT_BASIC_ALERT")),
        }

    raise ValueError(f"No mapping implemented for feed {feed.feed_name} / table {feed.table_name}")
