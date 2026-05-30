"""IRS Form 990 / 990-EZ / 990-PF e-File XML extractor.

One public function: ``parse_filing(xml_bytes) -> ParsedFiling | None``.

Returns a dataclass holding the four record buckets the ingest writes as
separate Parquet tables (filings / persons / compensation / related_orgs)
plus the filing-type tag. Returns ``None`` when the XML is unparseable or
its `<ReturnTypeCd>` is out of scope (990T or unknown).

Implementation notes
--------------------
- Uses ``lxml.etree.fromstring`` on the full XML bytes. IRS Form 990 XMLs
  are 50KB-5MB each; full-DOM is fast enough and lets us use XPath queries
  with namespace-aware syntax. We avoid ``iterparse`` because the Filer/
  ReturnHeader and ReturnData/IRS990 sections need cross-references.
- All XPath queries use the IRS efile namespace
  (`http://www.irs.gov/efile`) bound to prefix ``e``.
- Schema-version drift across years is handled by:
    * fail-soft on missing elements (return None for absent fields);
    * union XPath against multiple known element-name variants where the
      schema evolved (e.g. ``AverageHoursPerWeekRt`` vs
      ``AverageHrsPerWkDevotedToPosRt`` for hours-per-week).
- Person addresses, when present in 990-PF officer sections, are preserved
  on the persons row. They're personal/contact addresses, not the org's
  mailing address — distinct identity-spine signal for foundation principals.

The four record schemas match the directive's ``filings_990``, ``persons_990``,
``compensation_990``, ``related_orgs`` (and the 990-PF variants are emitted
into the same buckets — the partitioning by filing_type happens at Parquet
write time in the ingest script). One filing-type-tag column on each row
(``filing_type`` ∈ {990, 990EZ, 990PF}) lets the ingest write to the right
Parquet object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from . import irs_990_normalize as N

NS = {"e": "http://www.irs.gov/efile"}

_log = logging.getLogger(__name__)


# In-scope return types. 990O is a legacy-form code, mapped to 990 per directive.
_INSCOPE_TYPES: frozenset[str] = frozenset({"990", "990EZ", "990PF", "990O"})
_RETURN_TYPE_REMAP: dict[str, str] = {"990O": "990"}


@dataclass
class ParsedFiling:
    """Output of one parsed Form 990 / 990-EZ / 990-PF XML."""

    filing: dict[str, Any]
    persons: list[dict[str, Any]] = field(default_factory=list)
    compensation: list[dict[str, Any]] = field(default_factory=list)
    related_orgs: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #


def _text(elem: etree._Element | None, xpath: str) -> str | None:
    """Run an XPath relative to ``elem`` and return the first match's text.

    Returns None for None elements, missing matches, or empty strings.
    """
    if elem is None:
        return None
    found = elem.xpath(xpath, namespaces=NS)
    if not found:
        return None
    node = found[0]
    if isinstance(node, etree._Element):
        t = node.text
    else:
        t = node
    if t is None:
        return None
    s = str(t).strip()
    return s or None


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _to_bool(s: str | None) -> bool | None:
    """IRS XML uses {"true", "false", "1", "0", "X"} for booleans.

    Indicator elements often use ``X`` (presence == true) in the older 2014-
    era schema, then transitioned to explicit ``true``/``false``/``0``/``1``
    in 2018+. None for absent, True for any truthy form, False otherwise.
    """
    if s is None:
        return None
    v = s.strip().lower()
    if v in ("true", "1", "x", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return None


def _us_address(elem: etree._Element | None) -> dict[str, str | None]:
    """Extract a USAddress / ForeignAddress sub-element. Both shapes covered."""
    if elem is None:
        return {"street": None, "city": None, "state": None, "zip": None, "zip5": None}
    street = _text(elem, "e:USAddress/e:AddressLine1Txt") or _text(
        elem, "e:ForeignAddress/e:AddressLine1Txt"
    )
    city = _text(elem, "e:USAddress/e:CityNm") or _text(
        elem, "e:ForeignAddress/e:CityNm"
    )
    state = _text(elem, "e:USAddress/e:StateAbbreviationCd") or _text(
        elem, "e:ForeignAddress/e:ProvinceOrStateNm"
    )
    zipcd = _text(elem, "e:USAddress/e:ZIPCd") or _text(
        elem, "e:ForeignAddress/e:ForeignPostalCd"
    )
    return {
        "street": street,
        "city": city,
        "state": state,
        "zip": zipcd,
        "zip5": N.zip5(zipcd),
    }


# --------------------------------------------------------------------------- #
# Header extractor — common to all return types
# --------------------------------------------------------------------------- #


def _parse_header(root: etree._Element) -> dict[str, Any] | None:
    """Extract the ReturnHeader-level org / tax-period info."""
    header = root.find("e:ReturnHeader", NS)
    if header is None:
        return None
    return_type = _text(header, "e:ReturnTypeCd")
    if return_type is None:
        return None
    return_type = _RETURN_TYPE_REMAP.get(return_type, return_type)
    if return_type not in {"990", "990EZ", "990PF"}:
        return None

    filer = header.find("e:Filer", NS)
    if filer is None:
        return None

    raw_ein = _text(filer, "e:EIN")
    org_name_line1 = _text(filer, "e:BusinessName/e:BusinessNameLine1Txt")
    org_name_line2 = _text(filer, "e:BusinessName/e:BusinessNameLine2Txt")
    if org_name_line1 is None and org_name_line2 is None:
        # Some 990-PFs file under a person's name; fall back.
        org_name_line1 = _text(filer, "e:Name")

    org_name_full_parts = [p for p in (org_name_line1, org_name_line2) if p]
    org_name_full = " ".join(org_name_full_parts) if org_name_full_parts else None

    addr = _us_address(filer)

    tax_period_end = _text(header, "e:TaxPeriodEndDt")
    tax_period_begin = _text(header, "e:TaxPeriodBeginDt")
    tax_yr_str = _text(header, "e:TaxYr")
    tax_yr = int(tax_yr_str) if tax_yr_str and tax_yr_str.isdigit() else None
    return_ts = _text(header, "e:ReturnTs")

    return {
        "filing_type": return_type,
        "org_ein": raw_ein,
        "org_ein_normalized": N.normalize_ein(raw_ein),
        "org_name_line1": org_name_line1,
        "org_name_line2": org_name_line2,
        "org_name": org_name_full,
        "org_name_normalized": N.normalize_org_name(org_name_full),
        "org_address_street": addr["street"],
        "org_address_city": addr["city"],
        "org_address_state": addr["state"],
        "org_address_zip": addr["zip"],
        "org_zip5": addr["zip5"],
        "tax_period_year": tax_yr,
        "tax_period_end_date": tax_period_end,
        "tax_period_begin_date": tax_period_begin,
        "return_ts": return_ts,
    }


# --------------------------------------------------------------------------- #
# 990 / 990-EZ persons
# --------------------------------------------------------------------------- #


def _person_role_priority(roles: dict[str, bool]) -> str:
    """Pick the canonical primary role from the boolean flag set.

    Priority order matches the directive's listed roles, applied in order
    (officer is highest because most 990 filings have one named officer).
    """
    if roles.get("is_officer"):
        return "officer"
    if roles.get("is_trustee"):
        return "trustee"
    if roles.get("is_director"):
        return "director"
    if roles.get("is_key_employee"):
        return "key_employee"
    if roles.get("is_highest_paid_employee"):
        return "highest_paid_employee"
    if roles.get("is_former"):
        return "former"
    return "officer"  # 990-EZ has no role indicators; everyone is officer


def _parse_990_persons(
    irs990: etree._Element, header: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract Form 990 Part VII Section A (officers/directors/trustees/key-emp/
    highest-paid) and Part VII Section B (top contractors).

    Returns (persons, compensation). Both lists; one comp row per person/
    contractor.
    """
    persons: list[dict[str, Any]] = []
    compensation: list[dict[str, Any]] = []

    for grp in irs990.xpath("e:Form990PartVIISectionAGrp", namespaces=NS):
        person_nm = _text(grp, "e:PersonNm") or _text(grp, "e:BusinessName/e:BusinessNameLine1Txt")
        title = _text(grp, "e:TitleTxt")
        # Hours-per-week — schema went from AverageHoursPerWeekRt (2014-2017)
        # to AverageHrsPerWkDevotedToPosRt (2018+); accept both.
        hours = _to_float(
            _text(grp, "e:AverageHoursPerWeekRt")
            or _text(grp, "e:AverageHrsPerWkDevotedToPosRt")
        )
        hours_rltd = _to_float(_text(grp, "e:AverageHoursPerWeekRltdOrgRt"))

        is_officer = _to_bool(_text(grp, "e:OfficerInd")) or False
        is_trustee = (
            _to_bool(_text(grp, "e:IndividualTrusteeOrDirectorInd")) or False
        ) or (_to_bool(_text(grp, "e:InstitutionalTrusteeInd")) or False)
        is_director = is_trustee  # Form 990 Section A bundles trustee+director
        is_key_employee = _to_bool(_text(grp, "e:KeyEmployeeInd")) or False
        is_highest_paid = (
            _to_bool(_text(grp, "e:HighestCompensatedEmployeeInd")) or False
        )
        is_former = _to_bool(_text(grp, "e:FormerOfcrDirectorTrusteeInd")) or False

        roles = {
            "is_officer": is_officer,
            "is_trustee": is_trustee,
            "is_director": is_director,
            "is_key_employee": is_key_employee,
            "is_highest_paid_employee": is_highest_paid,
            "is_former": is_former,
        }
        primary_role = _person_role_priority(roles)

        first, middle, last, suffix = N.normalize_person_name(person_nm)

        person_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_name_raw": person_nm,
            "person_first_normalized": first,
            "person_middle_normalized": middle,
            "person_last_normalized": last,
            "person_suffix_normalized": suffix,
            "person_title": title,
            "person_role": primary_role,
            "hours_per_week": hours,
            "hours_per_week_related_org": hours_rltd,
            "person_address_street": None,
            "person_address_city": None,
            "person_address_state": None,
            "person_zip5": None,
            "is_officer": is_officer,
            "is_director": is_director,
            "is_trustee": is_trustee,
            "is_key_employee": is_key_employee,
            "is_highest_paid_employee": is_highest_paid,
            "is_former": is_former,
            "is_top_contractor": False,
            "contractor_business_name": None,
            "contractor_services_desc": None,
        }
        persons.append(person_row)

        comp_org = _to_float(_text(grp, "e:ReportableCompFromOrgAmt"))
        comp_rltd = _to_float(_text(grp, "e:ReportableCompFromRltdOrgAmt"))
        comp_other = _to_float(_text(grp, "e:OtherCompensationAmt"))
        comp_total = sum(c for c in (comp_org, comp_rltd, comp_other) if c is not None) or None

        comp_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_first_normalized": first,
            "person_last_normalized": last,
            "person_role": primary_role,
            "comp_base_salary": None,         # 990 Part VII doesn't break out base
            "comp_bonus": None,
            "comp_other_reportable": comp_other,
            "comp_deferred": None,
            "comp_retirement": None,
            "comp_other_nontaxable": None,
            "comp_total_w2_1099": comp_org,
            "comp_total_estimated_other": comp_rltd,
            "comp_total_all": comp_total,
        }
        compensation.append(comp_row)

    # Top contractors — Part VII Section B.
    for cgrp in irs990.xpath("e:ContractorCompensationGrp", namespaces=NS):
        contractor_business = _text(
            cgrp, "e:ContractorName/e:BusinessName/e:BusinessNameLine1Txt"
        )
        contractor_person = _text(cgrp, "e:ContractorName/e:PersonNm")
        services_desc = _text(cgrp, "e:ServicesDesc")
        comp_amt = _to_float(_text(cgrp, "e:CompensationAmt"))
        addr_elem = cgrp.find("e:ContractorAddress", NS)
        addr = _us_address(addr_elem) if addr_elem is not None else _us_address(None)

        first, middle, last, suffix = N.normalize_person_name(contractor_person)

        person_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_name_raw": contractor_person,
            "person_first_normalized": first,
            "person_middle_normalized": middle,
            "person_last_normalized": last,
            "person_suffix_normalized": suffix,
            "person_title": None,
            "person_role": "top_contractor",
            "hours_per_week": None,
            "hours_per_week_related_org": None,
            "person_address_street": addr["street"],
            "person_address_city": addr["city"],
            "person_address_state": addr["state"],
            "person_zip5": addr["zip5"],
            "is_officer": False,
            "is_director": False,
            "is_trustee": False,
            "is_key_employee": False,
            "is_highest_paid_employee": False,
            "is_former": False,
            "is_top_contractor": True,
            "contractor_business_name": contractor_business,
            "contractor_services_desc": services_desc,
        }
        persons.append(person_row)

        comp_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_first_normalized": first,
            "person_last_normalized": last,
            "person_role": "top_contractor",
            "comp_base_salary": None,
            "comp_bonus": None,
            "comp_other_reportable": None,
            "comp_deferred": None,
            "comp_retirement": None,
            "comp_other_nontaxable": None,
            "comp_total_w2_1099": comp_amt,
            "comp_total_estimated_other": None,
            "comp_total_all": comp_amt,
        }
        compensation.append(comp_row)

    return persons, compensation


def _parse_990ez_persons(
    irs990ez: etree._Element, header: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract Form 990-EZ Part IV (officer / director / trustee / key-emp).

    990-EZ has a simpler schema: no role flags, just name + title + comp.
    Treat every named person as an officer for the role classification —
    downstream MVs can heuristically infer the actual role from the title.
    """
    persons: list[dict[str, Any]] = []
    compensation: list[dict[str, Any]] = []

    for grp in irs990ez.xpath("e:OfficerDirectorTrusteeEmplGrp", namespaces=NS):
        person_nm = _text(grp, "e:PersonNm") or _text(grp, "e:BusinessName/e:BusinessNameLine1Txt")
        title = _text(grp, "e:TitleTxt")
        hours = _to_float(_text(grp, "e:AverageHrsPerWkDevotedToPosRt"))
        comp = _to_float(_text(grp, "e:CompensationAmt"))
        benefits = _to_float(_text(grp, "e:ContriToEmplBenefitPlansAmt"))
        expenses = _to_float(_text(grp, "e:ExpenseAccountOtherAllwncAmt"))

        first, middle, last, suffix = N.normalize_person_name(person_nm)

        person_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_name_raw": person_nm,
            "person_first_normalized": first,
            "person_middle_normalized": middle,
            "person_last_normalized": last,
            "person_suffix_normalized": suffix,
            "person_title": title,
            "person_role": "officer",
            "hours_per_week": hours,
            "hours_per_week_related_org": None,
            "person_address_street": None,
            "person_address_city": None,
            "person_address_state": None,
            "person_zip5": None,
            "is_officer": True,
            "is_director": False,
            "is_trustee": False,
            "is_key_employee": False,
            "is_highest_paid_employee": False,
            "is_former": False,
            "is_top_contractor": False,
            "contractor_business_name": None,
            "contractor_services_desc": None,
        }
        persons.append(person_row)

        total = sum(x for x in (comp, benefits, expenses) if x is not None) or None

        comp_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_first_normalized": first,
            "person_last_normalized": last,
            "person_role": "officer",
            "comp_base_salary": comp,
            "comp_bonus": None,
            "comp_other_reportable": expenses,
            "comp_deferred": None,
            "comp_retirement": benefits,
            "comp_other_nontaxable": None,
            "comp_total_w2_1099": comp,
            "comp_total_estimated_other": None,
            "comp_total_all": total,
        }
        compensation.append(comp_row)

    return persons, compensation


def _parse_990pf_persons(
    irs990pf: etree._Element, header: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract Form 990-PF Part VIII (officers / directors / trustees / managers).

    990-PF schema: `<OfficerDirTrstKeyEmplInfoGrp>/<OfficerDirTrstKeyEmplGrp>`.
    Each person's group includes a USAddress — these are personal/contact
    addresses, not the foundation's. Preserve them as-is on the person row;
    they're identity-spine signal for downstream foundation-principal MVs.
    """
    persons: list[dict[str, Any]] = []
    compensation: list[dict[str, Any]] = []

    info_grp = irs990pf.find("e:OfficerDirTrstKeyEmplInfoGrp", NS)
    if info_grp is None:
        return persons, compensation

    for grp in info_grp.xpath("e:OfficerDirTrstKeyEmplGrp", namespaces=NS):
        person_nm = _text(grp, "e:PersonNm") or _text(grp, "e:BusinessName/e:BusinessNameLine1Txt")
        title = _text(grp, "e:TitleTxt")
        hours = _to_float(_text(grp, "e:AverageHrsPerWkDevotedToPosRt"))
        comp = _to_float(_text(grp, "e:CompensationAmt"))
        benefits = _to_float(_text(grp, "e:EmployeeBenefitProgramAmt"))
        expenses = _to_float(_text(grp, "e:ExpenseAccountOtherAllwncAmt"))

        addr = _us_address(grp)

        first, middle, last, suffix = N.normalize_person_name(person_nm)

        # Directive's role enum for 990-PF: trustees / managers — title text
        # is the only signal. Default to 'trustee' (common case for foundation
        # families).
        primary_role = "trustee"
        if title:
            tlow = title.lower()
            if "officer" in tlow or "president" in tlow or "ceo" in tlow:
                primary_role = "officer"
            elif "director" in tlow:
                primary_role = "director"
            elif "key" in tlow:
                primary_role = "key_employee"

        person_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_name_raw": person_nm,
            "person_first_normalized": first,
            "person_middle_normalized": middle,
            "person_last_normalized": last,
            "person_suffix_normalized": suffix,
            "person_title": title,
            "person_role": primary_role,
            "hours_per_week": hours,
            "hours_per_week_related_org": None,
            "person_address_street": addr["street"],
            "person_address_city": addr["city"],
            "person_address_state": addr["state"],
            "person_zip5": addr["zip5"],
            "is_officer": primary_role == "officer",
            "is_director": primary_role == "director",
            "is_trustee": primary_role == "trustee",
            "is_key_employee": primary_role == "key_employee",
            "is_highest_paid_employee": False,
            "is_former": False,
            "is_top_contractor": False,
            "contractor_business_name": None,
            "contractor_services_desc": None,
        }
        persons.append(person_row)

        total = sum(x for x in (comp, benefits, expenses) if x is not None) or None

        comp_row = {
            **{k: header[k] for k in (
                "org_ein_normalized", "filing_type", "tax_period_year",
            )},
            "person_first_normalized": first,
            "person_last_normalized": last,
            "person_role": primary_role,
            "comp_base_salary": comp,
            "comp_bonus": None,
            "comp_other_reportable": expenses,
            "comp_deferred": None,
            "comp_retirement": benefits,
            "comp_other_nontaxable": None,
            "comp_total_w2_1099": comp,
            "comp_total_estimated_other": None,
            "comp_total_all": total,
        }
        compensation.append(comp_row)

    return persons, compensation


# --------------------------------------------------------------------------- #
# Schedule R (Related Orgs)
# --------------------------------------------------------------------------- #


def _parse_schedule_r(
    schedule_r: etree._Element, header: dict[str, Any]
) -> list[dict[str, Any]]:
    """Extract Schedule R related-organization records.

    Four sub-sections published by the IRS schema:
      - IdDisregardedEntitiesGrp        (disregarded LLCs)
      - IdRelatedTaxExemptOrgGrp        (related 501(c) orgs)
      - IdRelatedOrgTxblPartnershipGrp  (taxable partnerships)
      - IdRelatedOrgTxblCorpTrGrp       (taxable C-corps / trusts)
    """
    out: list[dict[str, Any]] = []
    if schedule_r is None:
        return out

    sections: tuple[tuple[str, str, str, str], ...] = (
        # (xpath, name_xpath, relationship_type, tax_exempt)
        (
            "e:IdDisregardedEntitiesGrp",
            "e:DisregardedEntityName/e:BusinessNameLine1Txt",
            "disregarded_entity",
            "false",
        ),
        (
            "e:IdRelatedTaxExemptOrgGrp",
            "e:DisregardedEntityName/e:BusinessNameLine1Txt",
            "related_tax_exempt",
            "true",
        ),
        (
            "e:IdRelatedOrgTxblPartnershipGrp",
            "e:RelatedOrganizationName/e:BusinessNameLine1Txt",
            "partnership",
            "false",
        ),
        (
            "e:IdRelatedOrgTxblCorpTrGrp",
            "e:RelatedOrganizationName/e:BusinessNameLine1Txt",
            "corporation_or_trust",
            "false",
        ),
    )

    for xpath, name_xpath, rel_type, tax_exempt in sections:
        for grp in schedule_r.xpath(xpath, namespaces=NS):
            # Tax-exempt section uses RelatedOrganizationName/BusinessNameLine1Txt.
            # Disregarded uses DisregardedEntityName/BusinessNameLine1Txt.
            related_name = (
                _text(grp, "e:RelatedOrganizationName/e:BusinessNameLine1Txt")
                or _text(grp, "e:DisregardedEntityName/e:BusinessNameLine1Txt")
            )
            related_ein = _text(grp, "e:EIN")
            primary_activity = _text(grp, "e:PrimaryActivitiesTxt")
            domicile_state = _text(grp, "e:LegalDomicileStateCd")
            controlling = _text(
                grp,
                "e:DirectControllingEntityName/e:BusinessNameLine1Txt",
            )
            total_income = _to_float(_text(grp, "e:TotalIncomeAmt"))
            eoy_assets = _to_float(_text(grp, "e:EndOfYearAssetsAmt"))

            out.append({
                "org_ein_normalized": header["org_ein_normalized"],
                "parent_org_ein_normalized": header["org_ein_normalized"],
                "filing_type": header["filing_type"],
                "tax_period_year": header["tax_period_year"],
                "related_org_name": related_name,
                "related_org_ein": related_ein,
                "related_org_ein_normalized": N.normalize_ein(related_ein),
                "related_org_name_normalized": N.normalize_org_name(related_name),
                "relationship_type": rel_type,
                "is_tax_exempt": tax_exempt == "true",
                "primary_activities": primary_activity,
                "domicile_state": domicile_state,
                "direct_controlling_entity_name": controlling,
                "related_total_income": total_income,
                "related_eoy_assets": eoy_assets,
            })
    return out


# --------------------------------------------------------------------------- #
# Filing financials
# --------------------------------------------------------------------------- #


def _parse_990_financials(irs990: etree._Element) -> dict[str, Any]:
    """Form 990 Part I summary financials.

    Several of these element names exist multiple places in the XML; we use
    the Part I (summary) section names which the schema has carried forward
    since 2014.
    """
    return {
        "total_revenue": _to_float(_text(irs990, "e:CYTotalRevenueAmt"))
            or _to_float(_text(irs990, "e:PYTotalRevenueAmt"))
            or _to_float(_text(irs990, "e:TotalRevenueAmt")),
        "total_expenses": _to_float(_text(irs990, "e:CYTotalExpensesAmt"))
            or _to_float(_text(irs990, "e:TotalExpensesAmt")),
        "total_assets": _to_float(_text(irs990, "e:TotalAssetsEOYAmt"))
            or _to_float(_text(irs990, "e:TotalAssetsBOYAmt")),
        "net_assets": _to_float(_text(irs990, "e:NetAssetsOrFundBalancesEOYAmt")),
        "gross_receipts": _to_float(_text(irs990, "e:GrossReceiptsAmt")),
        "is_political_active": _to_bool(_text(irs990, "e:PoliticalCampaignActyInd")),
        "lobbying_activity": _to_bool(_text(irs990, "e:LobbyingActivitiesInd")),
        "mission_statement": _text(irs990, "e:MissionDesc"),
    }


def _parse_990ez_financials(irs990ez: etree._Element) -> dict[str, Any]:
    return {
        "total_revenue": _to_float(_text(irs990ez, "e:TotalRevenueAmt")),
        "total_expenses": _to_float(_text(irs990ez, "e:TotalExpensesAmt")),
        "total_assets": _to_float(_text(irs990ez, "e:Form990TotalAssetsGrp/e:EOYAmt"))
            or _to_float(_text(irs990ez, "e:TotalAssetsEOYAmt")),
        "net_assets": _to_float(_text(irs990ez, "e:NetAssetsOrFundBalancesEOYAmt")),
        "gross_receipts": _to_float(_text(irs990ez, "e:GrossReceiptsAmt")),
        "is_political_active": None,
        "lobbying_activity": None,
        "mission_statement": _text(irs990ez, "e:PrimaryExemptPurposeTxt"),
    }


def _parse_990pf_financials(irs990pf: etree._Element) -> dict[str, Any]:
    """Form 990-PF Part I + Part II — figures live under
    ``AnalysisOfRevenueAndExpenses`` and ``Form990PFBalanceSheetsGrp``.

    Verified against IRS 2022/2023 schema (returnVersion 2022v7.0). Key
    decisions:
      - ``total_assets`` reports BOOK value (TotalAssetsEOYAmt) — Fair Market
        Value is in TotalAssetsEOYFMVAmt and exposed separately as
        ``total_assets_fmv``. The book value is the legacy 990-PF reporting
        convention; the FMV column gives the wealth-rank signal.
      - ``net_assets`` uses TotNetAstOrFundBalancesEOYAmt (Form990PFBalanceSheetsGrp).
      - ``total_grants_paid`` uses TotalGrantOrContriPdDurYrAmt (Part XV /
        analysis section), with a fallback to ContriPaidRevAndExpnssAmt.
      - ``investment_income`` uses NetInvestmentIncomeAmt at the
        AnalysisOfRevenueAndExpenses level.
      - ``excise_tax_amount`` uses InvestmentIncomeExciseTaxAmt.
    """
    return {
        "total_revenue": _to_float(
            _text(irs990pf, "e:AnalysisOfRevenueAndExpenses/e:TotalRevAndExpnssAmt")
        ),
        "total_expenses": _to_float(
            _text(irs990pf, "e:AnalysisOfRevenueAndExpenses/e:TotalExpensesRevAndExpnssAmt")
        ),
        "total_assets": _to_float(
            _text(irs990pf, "e:Form990PFBalanceSheetsGrp/e:TotalAssetsEOYAmt")
        ),
        "total_assets_fmv": _to_float(
            _text(irs990pf, "e:Form990PFBalanceSheetsGrp/e:TotalAssetsEOYFMVAmt")
        ),
        "net_assets": _to_float(
            _text(irs990pf, "e:Form990PFBalanceSheetsGrp/e:TotNetAstOrFundBalancesEOYAmt")
        ),
        "gross_receipts": None,
        "total_grants_paid": _to_float(
            _text(irs990pf, "e:TotalGrantOrContriPdDurYrAmt")
        ) or _to_float(
            _text(irs990pf, "e:AnalysisOfRevenueAndExpenses/e:ContriPaidRevAndExpnssAmt")
        ),
        "total_contributions_received": _to_float(
            _text(irs990pf, "e:AnalysisOfRevenueAndExpenses/e:ContriRcvdRevAndExpnssAmt")
        ),
        "investment_income": _to_float(
            _text(irs990pf, "e:AnalysisOfRevenueAndExpenses/e:NetInvestmentIncomeAmt")
        ),
        "excise_tax_amount": _to_float(_text(irs990pf, "e:InvestmentIncomeExciseTaxAmt"))
            or _to_float(_text(irs990pf, "e:TaxBasedOnInvestmentIncomeAmt")),
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def parse_filing(xml_bytes: bytes) -> ParsedFiling | None:
    """Parse one IRS Form 990 / 990-EZ / 990-PF XML and return its records.

    Returns None when:
      - The XML is malformed (lxml.etree.XMLSyntaxError).
      - The Return is a 990-T or unknown filing type (out of scope).
      - The ReturnHeader is missing required fields (EIN, ReturnTypeCd).
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        _log.debug("XML syntax error: %s", exc)
        return None

    header = _parse_header(root)
    if header is None:
        return None

    filing_type = header["filing_type"]
    return_data = root.find("e:ReturnData", NS)
    if return_data is None:
        return None

    persons: list[dict[str, Any]] = []
    compensation: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []

    if filing_type == "990":
        irs990 = return_data.find("e:IRS990", NS)
        if irs990 is not None:
            persons, compensation = _parse_990_persons(irs990, header)
            financials = _parse_990_financials(irs990)
        else:
            financials = {}
    elif filing_type == "990EZ":
        irs990ez = return_data.find("e:IRS990EZ", NS)
        if irs990ez is not None:
            persons, compensation = _parse_990ez_persons(irs990ez, header)
            financials = _parse_990ez_financials(irs990ez)
        else:
            financials = {}
    elif filing_type == "990PF":
        irs990pf = return_data.find("e:IRS990PF", NS)
        if irs990pf is not None:
            persons, compensation = _parse_990pf_persons(irs990pf, header)
            financials = _parse_990pf_financials(irs990pf)
        else:
            financials = {}
    else:
        return None  # unreachable due to header filter

    schedule_r = return_data.find("e:IRS990ScheduleR", NS)
    if schedule_r is not None:
        related = _parse_schedule_r(schedule_r, header)

    filing_row: dict[str, Any] = {**header, **financials}
    return ParsedFiling(
        filing=filing_row,
        persons=persons,
        compensation=compensation,
        related_orgs=related,
    )
