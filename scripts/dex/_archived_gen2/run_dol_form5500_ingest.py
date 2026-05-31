#!/usr/bin/env python3
"""DOL Form 5500 — bulk-CSV ingest from EBSA FOIA distribution.

Three forms × three years (2023, 2024, 2025-partial). Source-first per
CLAUDE.md (2026-04-16): each form lands in its own entities.source_*
table. No identity resolution, no canonical merge.

  f-5500       F_5500             entities.source_dol_form_5500
  f-5500-sf    F_5500_SF          entities.source_dol_form_5500_sf
  sch-c-p1i2   F_SCH_C_PART1_ITEM2 entities.source_dol_form_5500_sch_c_providers

Source URL pattern:
  https://askebsa.dol.gov/FOIA Files/{year}/Latest/{FORM}_{year}_Latest.zip

Idempotency:
  - F_5500 and F_5500_SF: PK=ack_id, ON CONFLICT (ack_id) DO UPDATE.
  - F_SCH_C_PART1_ITEM2: PK=(ack_id, row_order), ON CONFLICT (ack_id,row_order) DO UPDATE.

Audit: ops.dol_form5500_ingest_runs.
Skip-if-unchanged: HEAD Last-Modified compared to prior successful run.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_dol_form5500_ingest.py f-5500 2024
  PYTHONPATH=. doppler run -- python3 scripts/run_dol_form5500_ingest.py all all
  PYTHONPATH=. doppler run -- python3 scripts/run_dol_form5500_ingest.py all all --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_dol_form5500_ingest.py f-5500 2024 --dry-run
  PYTHONPATH=. doppler run -- python3 scripts/run_dol_form5500_ingest.py all all --recon-only
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
import psycopg
from psycopg.types.json import Jsonb


SUPPORTED_YEARS = (2023, 2024, 2025)
DEFAULT_BATCH_SIZE = 50_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("dol-form5500-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Column lists — order matches the migration table definitions exactly.
# All are stored as text or numeric per migration. The script coerces empty
# strings to None and lets Postgres parse numerics.
# --------------------------------------------------------------------------- #

# Columns marked "numeric" → empty-string is converted to None; non-empty is
# left as a string for Postgres to parse during COPY.
F_5500_NUMERIC_COLS = {
    "tot_partcp_boy_cnt", "tot_active_partcp_cnt", "rtd_sep_partcp_rcvg_cnt",
    "rtd_sep_partcp_fut_cnt", "subtl_act_rtd_sep_cnt", "benef_rcvg_bnft_cnt",
    "tot_act_rtd_sep_benef_cnt", "partcp_account_bal_cnt",
    "sep_partcp_partl_vstd_cnt", "contrib_emplrs_cnt",
    "tot_act_partcp_boy_cnt", "partcp_account_bal_cnt_boy",
    "num_sch_dcg_attached_cnt",
}

F_5500_COLS: list[str] = [
    "ack_id", "form_plan_year_begin_date", "form_tax_prd",
    "type_plan_entity_cd", "type_dfe_plan_entity_cd",
    "initial_filing_ind", "amended_ind", "final_filing_ind",
    "short_plan_yr_ind", "collective_bargain_ind",
    "f5558_application_filed_ind", "ext_automatic_ind", "dfvc_program_ind",
    "ext_special_ind", "ext_special_text",
    "plan_name", "spons_dfe_pn", "plan_eff_date",
    "sponsor_dfe_name", "spons_dfe_dba_name", "spons_dfe_care_of_name",
    "spons_dfe_mail_us_address1", "spons_dfe_mail_us_address2",
    "spons_dfe_mail_us_city", "spons_dfe_mail_us_state",
    "spons_dfe_mail_us_zip",
    "spons_dfe_mail_foreign_addr1", "spons_dfe_mail_foreign_addr2",
    "spons_dfe_mail_foreign_city", "spons_dfe_mail_forgn_prov_st",
    "spons_dfe_mail_foreign_cntry", "spons_dfe_mail_forgn_postal_cd",
    "spons_dfe_loc_us_address1", "spons_dfe_loc_us_address2",
    "spons_dfe_loc_us_city", "spons_dfe_loc_us_state",
    "spons_dfe_loc_us_zip",
    "spons_dfe_loc_foreign_address1", "spons_dfe_loc_foreign_address2",
    "spons_dfe_loc_foreign_city", "spons_dfe_loc_forgn_prov_st",
    "spons_dfe_loc_foreign_cntry", "spons_dfe_loc_forgn_postal_cd",
    "spons_dfe_ein", "spons_dfe_phone_num", "business_code",
    "admin_name", "admin_care_of_name",
    "admin_us_address1", "admin_us_address2",
    "admin_us_city", "admin_us_state", "admin_us_zip",
    "admin_foreign_address1", "admin_foreign_address2",
    "admin_foreign_city", "admin_foreign_prov_state",
    "admin_foreign_cntry", "admin_foreign_postal_cd",
    "admin_ein", "admin_phone_num",
    "last_rpt_spons_name", "last_rpt_spons_ein", "last_rpt_plan_num",
    "admin_signed_date", "admin_signed_name",
    "spons_signed_date", "spons_signed_name",
    "dfe_signed_date", "dfe_signed_name",
    "tot_partcp_boy_cnt", "tot_active_partcp_cnt",
    "rtd_sep_partcp_rcvg_cnt", "rtd_sep_partcp_fut_cnt",
    "subtl_act_rtd_sep_cnt", "benef_rcvg_bnft_cnt",
    "tot_act_rtd_sep_benef_cnt", "partcp_account_bal_cnt",
    "sep_partcp_partl_vstd_cnt", "contrib_emplrs_cnt",
    "type_pension_bnft_code", "type_welfare_bnft_code",
    "funding_insurance_ind", "funding_sec412_ind",
    "funding_trust_ind", "funding_gen_asset_ind",
    "benefit_insurance_ind", "benefit_sec412_ind",
    "benefit_trust_ind", "benefit_gen_asset_ind",
    "sch_r_attached_ind", "sch_mb_attached_ind",
    "sch_sb_attached_ind", "sch_h_attached_ind",
    "sch_i_attached_ind", "sch_a_attached_ind",
    "num_sch_a_attached_cnt",
    "sch_c_attached_ind", "sch_d_attached_ind", "sch_g_attached_ind",
    "filing_status", "date_received",
    "valid_admin_signature", "valid_dfe_signature",
    "valid_sponsor_signature",
    "admin_phone_num_foreign", "spons_dfe_phone_num_foreign",
    "admin_name_same_spon_ind", "admin_address_same_spon_ind",
    "preparer_name", "preparer_firm_name",
    "preparer_us_address1", "preparer_us_address2",
    "preparer_us_city", "preparer_us_state", "preparer_us_zip",
    "preparer_foreign_address1", "preparer_foreign_address2",
    "preparer_foreign_city", "preparer_foreign_prov_state",
    "preparer_foreign_cntry", "preparer_foreign_postal_cd",
    "preparer_phone_num", "preparer_phone_num_foreign",
    "tot_act_partcp_boy_cnt",
    "subj_m1_filing_req_ind", "compliance_m1_filing_req_ind",
    "m1_receipt_confirmation_code",
    "admin_manual_signed_date", "admin_manual_signed_name",
    "last_rpt_plan_name",
    "spons_manual_signed_date", "spons_manual_signed_name",
    "dfe_manual_signed_date", "dfe_manual_signed_name",
    "adopted_plan_perm_sec_act", "partcp_account_bal_cnt_boy",
    "sch_dcg_attached_ind", "num_sch_dcg_attached_cnt",
    "sch_mep_attached_ind",
]

F_5500_SF_NUMERIC_COLS = {
    "sf_admin_srvc_providers_amt", "sf_broker_fees_paid_amt",
    "sf_corrective_deemed_distr_amt", "sf_emplr_contrib_income_amt",
    "sf_emplr_contrib_paid_amt", "sf_fail_provide_benef_due_amt",
    "sf_fail_transmit_contrib_amt", "sf_funding_deficiency_amt",
    "sf_loss_discv_dur_year_amt", "sf_net_assets_boy_amt",
    "sf_net_assets_eoy_amt", "sf_net_income_amt",
    "sf_oth_contrib_rcvd_amt", "sf_oth_expenses_amt",
    "sf_other_income_amt", "sf_partcp_account_bal_cnt",
    "sf_partcp_account_bal_cnt_boy", "sf_partcp_loans_eoy_amt",
    "sf_particip_contrib_income_amt", "sf_party_in_int_not_rptd_amt",
    "sf_plan_ins_fdlty_bond_amt", "sf_res_term_plan_adpt_amt",
    "sf_sec_412_req_contrib_amt", "sf_sep_partcp_partl_vstd_cnt",
    "sf_tot_act_partcp_boy_cnt", "sf_tot_act_partcp_eoy_cnt",
    "sf_tot_act_rtd_sep_benef_cnt", "sf_tot_assets_boy_amt",
    "sf_tot_assets_eoy_amt", "sf_tot_distrib_bnft_amt",
    "sf_tot_expenses_amt", "sf_tot_income_amt",
    "sf_tot_liabilities_boy_amt", "sf_tot_liabilities_eoy_amt",
    "sf_tot_partcp_boy_cnt", "sf_tot_plan_transfers_amt",
    "sf_unp_min_cont_cur_yrtot_amt",
}

F_5500_SF_COLS: list[str] = [
    "ack_id", "sf_plan_year_begin_date", "sf_tax_prd",
    "sf_plan_entity_cd", "sf_initial_filing_ind", "sf_amended_ind",
    "sf_final_filing_ind", "sf_short_plan_yr_ind",
    "sf_5558_application_filed_ind", "sf_ext_automatic_ind",
    "sf_dfvc_program_ind", "sf_ext_special_ind", "sf_ext_special_text",
    "sf_plan_name", "sf_plan_num", "sf_plan_eff_date",
    "sf_sponsor_name", "sf_sponsor_dfe_dba_name",
    "sf_spons_us_address1", "sf_spons_us_address2",
    "sf_spons_us_city", "sf_spons_us_state", "sf_spons_us_zip",
    "sf_spons_foreign_address1", "sf_spons_foreign_address2",
    "sf_spons_foreign_city", "sf_spons_foreign_prov_state",
    "sf_spons_foreign_cntry", "sf_spons_foreign_postal_cd",
    "sf_spons_ein", "sf_spons_phone_num", "sf_business_code",
    "sf_admin_name", "sf_admin_care_of_name",
    "sf_admin_us_address1", "sf_admin_us_address2",
    "sf_admin_us_city", "sf_admin_us_state", "sf_admin_us_zip",
    "sf_admin_foreign_address1", "sf_admin_foreign_address2",
    "sf_admin_foreign_city", "sf_admin_foreign_prov_state",
    "sf_admin_foreign_cntry", "sf_admin_foreign_postal_cd",
    "sf_admin_ein", "sf_admin_phone_num",
    "sf_last_rpt_spons_name", "sf_last_rpt_spons_ein",
    "sf_last_rpt_plan_num",
    "sf_tot_partcp_boy_cnt", "sf_tot_act_rtd_sep_benef_cnt",
    "sf_partcp_account_bal_cnt", "sf_eligible_assets_ind",
    "sf_iqpa_waiver_ind",
    "sf_tot_assets_boy_amt", "sf_tot_liabilities_boy_amt",
    "sf_net_assets_boy_amt", "sf_tot_assets_eoy_amt",
    "sf_tot_liabilities_eoy_amt", "sf_net_assets_eoy_amt",
    "sf_emplr_contrib_income_amt", "sf_particip_contrib_income_amt",
    "sf_oth_contrib_rcvd_amt", "sf_other_income_amt",
    "sf_tot_income_amt", "sf_tot_distrib_bnft_amt",
    "sf_corrective_deemed_distr_amt", "sf_admin_srvc_providers_amt",
    "sf_oth_expenses_amt", "sf_tot_expenses_amt",
    "sf_net_income_amt", "sf_tot_plan_transfers_amt",
    "sf_type_pension_bnft_code", "sf_type_welfare_bnft_code",
    "sf_fail_transmit_contrib_ind", "sf_fail_transmit_contrib_amt",
    "sf_party_in_int_not_rptd_ind", "sf_party_in_int_not_rptd_amt",
    "sf_plan_ins_fdlty_bond_ind", "sf_plan_ins_fdlty_bond_amt",
    "sf_loss_discv_dur_year_ind", "sf_loss_discv_dur_year_amt",
    "sf_broker_fees_paid_ind", "sf_broker_fees_paid_amt",
    "sf_fail_provide_benef_due_ind", "sf_fail_provide_benef_due_amt",
    "sf_partcp_loans_ind", "sf_partcp_loans_eoy_amt",
    "sf_plan_blackout_period_ind", "sf_comply_blackout_notice_ind",
    "sf_db_plan_funding_reqd_ind", "sf_dc_plan_funding_reqd_ind",
    "sf_ruling_letter_grant_date", "sf_sec_412_req_contrib_amt",
    "sf_emplr_contrib_paid_amt", "sf_funding_deficiency_amt",
    "sf_funding_deadline_ind",
    "sf_res_term_plan_adpt_ind", "sf_res_term_plan_adpt_amt",
    "sf_all_plan_ast_distrib_ind",
    "sf_admin_signed_date", "sf_admin_signed_name",
    "sf_spons_signed_date", "sf_spons_signed_name",
    "filing_status", "date_received",
    "valid_admin_signature", "valid_sponsor_signature",
    "sf_admin_phone_num_foreign", "sf_spons_care_of_name",
    "sf_spons_loc_foreign_address1", "sf_spons_loc_foreign_address2",
    "sf_spons_loc_foreign_city", "sf_spons_loc_foreign_cntry",
    "sf_spons_loc_foreign_postal_cd", "sf_spons_loc_foreign_prov_stat",
    "sf_spons_loc_us_address1", "sf_spons_loc_us_address2",
    "sf_spons_loc_us_city", "sf_spons_loc_us_state",
    "sf_spons_loc_us_zip",
    "sf_spons_phone_num_foreign",
    "sf_admin_name_same_spon_ind", "sf_admin_addrss_same_spon_ind",
    "sf_preparer_name", "sf_preparer_firm_name",
    "sf_preparer_us_address1", "sf_preparer_us_address2",
    "sf_preparer_us_city", "sf_preparer_us_state", "sf_preparer_us_zip",
    "sf_preparer_foreign_address1", "sf_preparer_foreign_address2",
    "sf_preparer_foreign_city", "sf_preparer_foreign_prov_state",
    "sf_preparer_foreign_cntry", "sf_preparer_foreign_postal_cd",
    "sf_preparer_phone_num", "sf_preparer_phone_num_foreign",
    "sf_fdcry_trust_name", "sf_fdcry_trust_ein",
    "sf_unp_min_cont_cur_yrtot_amt",
    "sf_covered_pbgc_insurance_ind",
    "sf_tot_act_partcp_boy_cnt", "sf_tot_act_partcp_eoy_cnt",
    "sf_sep_partcp_partl_vstd_cnt",
    "sf_trus_inc_unrel_tax_inc_ind", "sf_trus_inc_unrel_tax_inc_amt",
    "sf_fdcry_truste_cust_name", "sf_fdcry_truste_cust_phone_num",
    "sf_fdcry_trus_cus_phon_numfore",
    "sf_401k_plan_ind", "sf_401k_satisfy_rqmts_ind",
    "sf_adp_acp_test_ind", "sf_mthd_used_satisfy_rqmts_ind",
    "sf_plan_satisfy_tests_ind", "sf_plan_timely_amended_ind",
    "sf_last_plan_amendment_date", "sf_tax_code",
    "sf_last_opin_advi_date", "sf_last_opin_advi_serial_num",
    "sf_fav_determ_ltr_date", "sf_plan_maintain_us_terri_ind",
    "sf_in_service_distrib_ind", "sf_in_service_distrib_amt",
    "sf_min_req_distrib_ind",
    "sf_admin_manual_sign_date", "sf_admin_manual_signed_name",
    "sf_401k_design_based_safe_ind",
    "sf_401k_prior_year_adp_ind", "sf_401k_current_year_adp_ind",
    "sf_401k_na_ind",
    "sf_mthd_ratio_prcnt_test_ind", "sf_mthd_avg_bnft_test_ind",
    "sf_mthd_na_ind", "sf_distrib_made_employe_62_ind",
    "sf_last_rpt_plan_name", "sf_premium_filing_confirm_no",
    "sf_spons_manual_signed_date", "sf_spons_manual_signed_name",
    "sf_pbgc_notified_cd", "sf_pbgc_notified_explan_text",
    "sf_adopted_plan_perm_sec_act", "collectively_bargained",
    "sf_partcp_account_bal_cnt_boy",
    "sf_401k_design_based_safe_harbor_ind",
    "sf_401k_prior_year_adp_test_ind", "sf_401k_current_year_adp_test_ind",
    "sf_opin_letter_date", "sf_opin_letter_serial_num",
]

SCH_C_NUMERIC_COLS = {
    "row_order", "provider_other_direct_comp_amt",
    "prov_other_tot_ind_comp_amt",
}

SCH_C_COLS: list[str] = [
    "ack_id", "row_order",
    "provider_other_name", "provider_other_ein",
    "provider_other_us_address1", "provider_other_us_address2",
    "provider_other_us_city", "provider_other_us_state",
    "provider_other_us_zip",
    "prov_other_foreign_address1", "prov_other_foreign_address2",
    "prov_other_foreign_city", "prov_other_foreign_prov_state",
    "prov_other_foreign_cntry", "prov_other_foreign_postal_cd",
    "provider_other_srvc_codes", "provider_other_relation",
    "provider_other_direct_comp_amt",
    "prov_other_indirect_comp_ind", "prov_other_elig_ind_comp_ind",
    "prov_other_tot_ind_comp_amt", "provider_other_amt_formula_ind",
]


# --------------------------------------------------------------------------- #
# Per-form configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormConfig:
    key: str                # CLI subcommand
    dataset_form: str       # Audit-table value
    file_prefix: str        # E.g. "F_5500", "F_5500_SF", "F_SCH_C_PART1_ITEM2"
    schema: str             # 'entities'
    table: str
    cols: list[str]         # Postgres columns in COPY order (excluding dataset_year/source_file_last_modified/ingested_at)
    numeric_cols: set[str]
    pk_cols: list[str]      # ('ack_id',) or ('ack_id', 'row_order')

    def url(self, year: int) -> str:
        # Path has a literal space; URL-encode.
        path = f"/FOIA Files/{year}/Latest/{self.file_prefix}_{year}_Latest.zip"
        return "https://askebsa.dol.gov" + urllib.parse.quote(path, safe="/")

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"


F_5500 = FormConfig(
    key="f-5500",
    dataset_form="F_5500",
    file_prefix="F_5500",
    schema="entities",
    table="source_dol_form_5500",
    cols=F_5500_COLS,
    numeric_cols=F_5500_NUMERIC_COLS,
    pk_cols=["ack_id"],
)

F_5500_SF = FormConfig(
    key="f-5500-sf",
    dataset_form="F_5500_SF",
    file_prefix="F_5500_SF",
    schema="entities",
    table="source_dol_form_5500_sf",
    cols=F_5500_SF_COLS,
    numeric_cols=F_5500_SF_NUMERIC_COLS,
    pk_cols=["ack_id"],
)

SCH_C_P1I2 = FormConfig(
    key="sch-c-p1i2",
    dataset_form="F_SCH_C_PART1_ITEM2",
    file_prefix="F_SCH_C_PART1_ITEM2",
    schema="entities",
    table="source_dol_form_5500_sch_c_providers",
    cols=SCH_C_COLS,
    numeric_cols=SCH_C_NUMERIC_COLS,
    pk_cols=["ack_id", "row_order"],
)

FORMS: dict[str, FormConfig] = {f.key: f for f in (F_5500, F_5500_SF, SCH_C_P1I2)}


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED is not set in the environment.")
    return url


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None]:
    """Returns (content_length, last_modified) following redirects."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            cl = int(r.headers.get("content-length", 0)) or None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(lm_raw, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            return cl, lm
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    """Download ZIP to dest, returning bytes written."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=600.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss",
                                url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed: {last_exc}")


# --------------------------------------------------------------------------- #
# CSV → Postgres COPY pipeline
# --------------------------------------------------------------------------- #


def open_csv_in_zip(zip_path: Path, file_prefix: str, year: int) -> tuple[zipfile.ZipFile, io.TextIOWrapper, str]:
    """Open the bundled CSV (case-insensitive match) for streaming read."""
    z = zipfile.ZipFile(zip_path)
    target_name = None
    for name in z.namelist():
        lower = name.lower()
        if lower.endswith(".csv") and lower.startswith(file_prefix.lower()):
            target_name = name
            break
    if target_name is None:
        z.close()
        raise RuntimeError(
            f"No CSV starting with '{file_prefix}' (case-insensitive) found in {zip_path.name}; "
            f"contents: {z.namelist()}"
        )
    f = io.TextIOWrapper(z.open(target_name, "r"), encoding="utf-8", errors="replace", newline="")
    return z, f, target_name


def build_row_tuple(
    cfg: FormConfig,
    raw_row: dict[str, str],
    *,
    dataset_year: int,
    source_file_last_modified: datetime | None,
) -> tuple[Any, ...]:
    """Pull each configured column from the raw row (case-insensitively),
    coerce empty → None for everything, leave non-empty strings as-is for
    Postgres to parse on COPY."""
    out: list[Any] = []
    for col in cfg.cols:
        # CSV header is uppercase (DOL field names); raw_row is keyed with
        # whatever case the DictReader saw. We pre-uppercased the header.
        v = raw_row.get(col.upper())
        if v is None or v == "":
            out.append(None)
        else:
            out.append(v)
    out.append(dataset_year)
    out.append(source_file_last_modified)
    return tuple(out)


def stage_create_sql(cfg: FormConfig) -> str:
    cols = ",\n  ".join(
        f"{c} {'numeric' if c in cfg.numeric_cols else 'text'}"
        for c in cfg.cols
    )
    return f"""
CREATE TEMP TABLE IF NOT EXISTS {cfg.stage_table} (
  {cols},
  dataset_year smallint,
  source_file_last_modified timestamptz
);
"""


def truncate_stage_sql(cfg: FormConfig) -> str:
    return f"TRUNCATE {cfg.stage_table};"


def copy_sql(cfg: FormConfig) -> str:
    cols = list(cfg.cols) + ["dataset_year", "source_file_last_modified"]
    return f"COPY {cfg.stage_table} ({', '.join(cols)}) FROM STDIN"


def upsert_from_stage_sql(cfg: FormConfig) -> str:
    natural_cols = list(cfg.cols)
    target_cols = natural_cols + ["dataset_year", "source_file_last_modified", "ingested_at"]
    select_cols = natural_cols + ["dataset_year", "source_file_last_modified", "now()"]
    pk = ", ".join(cfg.pk_cols)
    update_cols = [c for c in natural_cols if c not in cfg.pk_cols]
    update_assigns = ",\n      ".join(
        f"{c} = EXCLUDED.{c}" for c in update_cols
    )
    update_assigns += ",\n      dataset_year = EXCLUDED.dataset_year"
    update_assigns += ",\n      source_file_last_modified = EXCLUDED.source_file_last_modified"
    update_assigns += ",\n      ingested_at = now()"
    where_clause = " OR ".join(
        f"{cfg.fully_qualified}.{c} IS DISTINCT FROM EXCLUDED.{c}"
        for c in update_cols + ["dataset_year", "source_file_last_modified"]
    )
    return f"""
WITH upserted AS (
  INSERT INTO {cfg.fully_qualified} ({', '.join(target_cols)})
  SELECT {', '.join(select_cols)}
    FROM {cfg.stage_table}
   ON CONFLICT ({pk}) DO UPDATE SET
      {update_assigns}
   WHERE {where_clause}
   RETURNING (xmax = 0) AS inserted
)
SELECT
  count(*) FILTER (WHERE inserted)     AS rows_inserted,
  count(*) FILTER (WHERE NOT inserted) AS rows_updated
FROM upserted;
"""


def copy_chunk_to_stage(
    conn: psycopg.Connection,
    cfg: FormConfig,
    rows: list[tuple[Any, ...]],
) -> tuple[int, int]:
    """COPY into stage, then upsert into target. Returns (inserted, updated)."""
    if not rows:
        return 0, 0
    with conn.cursor() as cur:
        cur.execute(truncate_stage_sql(cfg))
        with cur.copy(copy_sql(cfg)) as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute(upsert_from_stage_sql(cfg))
        ins, upd = cur.fetchone()
    conn.commit()
    return int(ins), int(upd)


def stream_csv_to_db(
    conn: psycopg.Connection,
    cfg: FormConfig,
    csv_fh: io.TextIOWrapper,
    *,
    dataset_year: int,
    source_file_last_modified: datetime | None,
    batch_size: int,
    log_prefix: str,
) -> tuple[int, int, int]:
    """Yields (inserted, updated, rows_seen) totals."""
    reader = csv.reader(csv_fh)
    try:
        header = next(reader)
    except StopIteration:
        return 0, 0, 0
    header_upper = [h.strip().upper() for h in header]
    idx_by_name = {name: i for i, name in enumerate(header_upper)}

    expected_upper = {c.upper() for c in cfg.cols}
    missing = sorted(expected_upper - set(header_upper))
    extra = sorted(set(header_upper) - expected_upper)
    if missing:
        log.warning("%s CSV missing %d columns expected by migration: %s",
                    log_prefix, len(missing), missing[:10])
    if extra:
        log.warning("%s CSV has %d unexpected columns (will be dropped): %s",
                    log_prefix, len(extra), extra[:10])

    # Pre-build column→index lookup once for speed.
    col_indexes = [idx_by_name.get(c.upper()) for c in cfg.cols]

    rows_seen = total_inserted = total_updated = 0
    chunk: list[tuple[Any, ...]] = []
    page_started = time.monotonic()
    for raw in reader:
        rows_seen += 1
        out: list[Any] = []
        for col, idx in zip(cfg.cols, col_indexes):
            if idx is None:
                out.append(None)
                continue
            if idx >= len(raw):
                out.append(None)
                continue
            v = raw[idx]
            if v is None or v == "":
                out.append(None)
            else:
                out.append(v)
        out.append(dataset_year)
        out.append(source_file_last_modified)
        chunk.append(tuple(out))
        if len(chunk) >= batch_size:
            ins, upd = copy_chunk_to_stage(conn, cfg, chunk)
            total_inserted += ins
            total_updated += upd
            log.info(
                "%s chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
                log_prefix, rows_seen, ins, upd,
                total_inserted, total_updated,
                time.monotonic() - page_started,
            )
            chunk.clear()
            page_started = time.monotonic()
    if chunk:
        ins, upd = copy_chunk_to_stage(conn, cfg, chunk)
        total_inserted += ins
        total_updated += upd
        log.info(
            "%s final chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
            log_prefix, rows_seen, ins, upd,
            total_inserted, total_updated,
            time.monotonic() - page_started,
        )
    return total_inserted, total_updated, rows_seen


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    cfg: FormConfig,
    *,
    year: int,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.dol_form5500_ingest_runs (
        dataset_form, dataset_year, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cfg.dataset_form, year, url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, cfg: FormConfig, year: int
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.dol_form5500_ingest_runs
             WHERE dataset_form = %s AND dataset_year = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (cfg.dataset_form, year),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    cfg: FormConfig,
    *,
    year: int,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.dol_form5500_ingest_runs (
                dataset_form, dataset_year, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                cfg.dataset_form, year, url, source_last_modified,
                prior_source_last_modified, started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes: int,
    csv_bytes: int,
    rows_in_csv: int,
    rows_inserted: int,
    rows_updated: int,
    rows_unchanged: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.dol_form5500_ingest_runs
               SET status = %s, zip_bytes_downloaded = %s,
                   csv_bytes_extracted = %s, rows_in_csv = %s,
                   rows_inserted = %s, rows_updated = %s, rows_unchanged = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, rows_in_csv,
            rows_inserted, rows_updated, rows_unchanged,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Recon report
# --------------------------------------------------------------------------- #


@dataclass
class ReconStats:
    form_key: str
    table_fqn: str
    total_rows: int = 0
    distinct_years: list[int] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


def gather_recon_5500(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="f-5500", table_fqn=F_5500.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {F_5500.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(
            f"SELECT dataset_year, count(*) FROM {F_5500.fully_qualified} "
            f"GROUP BY dataset_year ORDER BY dataset_year;"
        )
        s.notes["rows_by_year"] = {int(r[0]): int(r[1]) for r in cur.fetchall()}
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE spons_dfe_ein IS NOT NULL),
              count(*) FILTER (WHERE spons_dfe_mail_us_address1 IS NOT NULL),
              count(*) FILTER (WHERE preparer_firm_name IS NOT NULL),
              count(*) FILTER (WHERE sch_c_attached_ind = '1')
              FROM {F_5500.fully_qualified};
        """)
        ein, addr, prep, sch_c = cur.fetchone()
        s.notes["sponsor_ein_populated"] = int(ein)
        s.notes["sponsor_us_address_populated"] = int(addr)
        s.notes["preparer_firm_populated"] = int(prep)
        s.notes["sch_c_attached_count"] = int(sch_c)
        cur.execute(f"""
            SELECT business_code, count(*) c
              FROM {F_5500.fully_qualified}
             WHERE business_code IS NOT NULL
             GROUP BY business_code ORDER BY c DESC LIMIT 10;
        """)
        s.notes["top_business_codes"] = [
            {"code": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
    return s


def gather_recon_sf(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="f-5500-sf", table_fqn=F_5500_SF.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {F_5500_SF.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(
            f"SELECT dataset_year, count(*) FROM {F_5500_SF.fully_qualified} "
            f"GROUP BY dataset_year ORDER BY dataset_year;"
        )
        s.notes["rows_by_year"] = {int(r[0]): int(r[1]) for r in cur.fetchall()}
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE sf_spons_ein IS NOT NULL),
              count(*) FILTER (WHERE sf_spons_us_address1 IS NOT NULL),
              count(*) FILTER (WHERE sf_preparer_firm_name IS NOT NULL),
              count(DISTINCT sf_spons_ein)
              FROM {F_5500_SF.fully_qualified};
        """)
        ein, addr, prep, distinct_ein = cur.fetchone()
        s.notes["sponsor_ein_populated"] = int(ein)
        s.notes["sponsor_us_address_populated"] = int(addr)
        s.notes["preparer_firm_populated"] = int(prep)
        s.notes["distinct_sponsor_eins"] = int(distinct_ein)
        cur.execute(f"""
            SELECT sf_spons_us_state, count(*) c
              FROM {F_5500_SF.fully_qualified}
             WHERE sf_spons_us_state IS NOT NULL
             GROUP BY sf_spons_us_state ORDER BY c DESC LIMIT 10;
        """)
        s.notes["top_states"] = [
            {"state": r[0], "count": int(r[1])} for r in cur.fetchall()
        ]
    return s


def gather_recon_sch_c(conn: psycopg.Connection) -> ReconStats:
    s = ReconStats(form_key="sch-c-p1i2", table_fqn=SCH_C_P1I2.fully_qualified)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SCH_C_P1I2.fully_qualified};")
        s.total_rows = int(cur.fetchone()[0])
        if s.total_rows == 0:
            return s
        cur.execute(
            f"SELECT dataset_year, count(*) FROM {SCH_C_P1I2.fully_qualified} "
            f"GROUP BY dataset_year ORDER BY dataset_year;"
        )
        s.notes["rows_by_year"] = {int(r[0]): int(r[1]) for r in cur.fetchall()}
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE provider_other_name IS NOT NULL),
              count(*) FILTER (WHERE provider_other_ein IS NOT NULL),
              count(DISTINCT provider_other_name),
              count(DISTINCT provider_other_ein),
              count(DISTINCT ack_id)
              FROM {SCH_C_P1I2.fully_qualified};
        """)
        nm_pop, ein_pop, dn, dein, dack = cur.fetchone()
        s.notes["provider_name_populated"] = int(nm_pop)
        s.notes["provider_ein_populated"] = int(ein_pop)
        s.notes["distinct_provider_names"] = int(dn)
        s.notes["distinct_provider_eins"] = int(dein)
        s.notes["distinct_filings_with_providers"] = int(dack)
        cur.execute(f"""
            SELECT provider_other_name, count(*) c, sum(provider_other_direct_comp_amt) total_comp
              FROM {SCH_C_P1I2.fully_qualified}
             WHERE provider_other_name IS NOT NULL
             GROUP BY provider_other_name ORDER BY c DESC LIMIT 15;
        """)
        s.notes["top_providers_by_filing_count"] = [
            {"name": r[0], "filings": int(r[1]), "total_direct_comp_dollars": float(r[2] or 0)}
            for r in cur.fetchall()
        ]
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE provider_other_direct_comp_amt > 0),
              avg(provider_other_direct_comp_amt) FILTER (WHERE provider_other_direct_comp_amt > 0),
              percentile_cont(0.5) WITHIN GROUP (ORDER BY provider_other_direct_comp_amt)
                FILTER (WHERE provider_other_direct_comp_amt > 0),
              max(provider_other_direct_comp_amt)
              FROM {SCH_C_P1I2.fully_qualified};
        """)
        cnt, avg, med, mx = cur.fetchone()
        s.notes["direct_comp_rows_with_dollars"] = int(cnt or 0)
        s.notes["direct_comp_avg_dollars"] = float(avg) if avg else None
        s.notes["direct_comp_median_dollars"] = float(med) if med else None
        s.notes["direct_comp_max_dollars"] = float(mx) if mx else None
    return s


def print_recon(s: ReconStats) -> None:
    print(f"=== RECON: {s.form_key}  ({s.table_fqn}) ===")
    print(f"  total rows: {s.total_rows:,}")
    for k, v in s.notes.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for kk, vv in v.items():
                print(f"      {kk}: {vv:,}" if isinstance(vv, int) else f"      {kk}: {vv}")
        elif isinstance(v, list):
            print(f"  {k}:")
            for item in v:
                print(f"      {item}")
        elif isinstance(v, int):
            print(f"  {k}: {v:,}")
        elif isinstance(v, float):
            print(f"  {k}: {v:,.2f}")
        else:
            print(f"  {k}: {v}")
    print(f"=== END RECON ===\n")


# --------------------------------------------------------------------------- #
# Per-(form, year) main
# --------------------------------------------------------------------------- #


def ensure_stage_table(conn: psycopg.Connection, cfg: FormConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(stage_create_sql(cfg))
    conn.commit()


def ingest_one(
    cfg: FormConfig,
    *,
    year: int,
    batch_size: int,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
) -> int:
    url = cfg.url(year)
    log_prefix = f"[{cfg.key} {year}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/dol-form5500-ingest"}) as client:
        try:
            content_length, source_last_modified = head_url(client, url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)

        if dry_run:
            log.info("%s DRY RUN — fetching ZIP and inspecting CSV header only", log_prefix)
            zip_path = workdir / f"{cfg.file_prefix}_{year}.zip"
            zip_bytes = download_zip(client, url, zip_path)
            log.info("%s downloaded %d bytes", log_prefix, zip_bytes)
            try:
                z, fh, name = open_csv_in_zip(zip_path, cfg.file_prefix, year)
                with z, fh:
                    header_line = fh.readline()
                    sample = fh.readline()
                    cols = header_line.rstrip("\n").split(",")
                    log.info("%s CSV name=%s cols=%d sample=%s",
                             log_prefix, name, len(cols), sample[:200])
            finally:
                zip_path.unlink(missing_ok=True)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, cfg, year)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source_last_modified unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, cfg, year=year, url=url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, cfg, year=year, url=url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)
            ensure_stage_table(conn, cfg)

            zip_path = workdir / f"{cfg.file_prefix}_{year}.zip"
            try:
                zip_bytes = download_zip(client, url, zip_path)
                log.info("%s downloaded %d bytes -> %s", log_prefix, zip_bytes, zip_path)

                z, fh, csv_name = open_csv_in_zip(zip_path, cfg.file_prefix, year)
                with z, fh:
                    csv_bytes = z.getinfo(csv_name).file_size
                    log.info("%s extracting %s (%d bytes uncompressed)",
                             log_prefix, csv_name, csv_bytes)
                    ins, upd, rows_seen = stream_csv_to_db(
                        conn, cfg, fh,
                        dataset_year=year,
                        source_file_last_modified=source_last_modified,
                        batch_size=batch_size,
                        log_prefix=log_prefix,
                    )

                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes, csv_bytes=csv_bytes,
                    rows_in_csv=rows_seen,
                    rows_inserted=ins, rows_updated=upd,
                    rows_unchanged=max(0, rows_seen - ins - upd),
                    started_at=started_wall, error_message=None, notes=None,
                )
                log.info(
                    "%s DONE rows_in_csv=%d ins=%d upd=%d unch=%d wall=%.1fs",
                    log_prefix, rows_seen, ins, upd,
                    max(0, rows_seen - ins - upd),
                    time.monotonic() - started_wall,
                )
                return 0
            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes=0, csv_bytes=0, rows_in_csv=0,
                    rows_inserted=0, rows_updated=0, rows_unchanged=0,
                    started_at=started_wall, error_message=str(exc), notes=None,
                )
                return 1
            finally:
                zip_path.unlink(missing_ok=True)


def run_recon_only() -> None:
    with psycopg.connect(_database_url()) as conn:
        for fn in (gather_recon_5500, gather_recon_sf, gather_recon_sch_c):
            try:
                s = fn(conn)
                print_recon(s)
            except psycopg.errors.UndefinedTable:
                log.error("Table missing — apply the migration first.")
                return


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("form", choices=list(FORMS.keys()) + ["all"],
                   help="Form key (f-5500, f-5500-sf, sch-c-p1i2) or 'all'.")
    p.add_argument("year", help="Year (2023, 2024, 2025) or 'all'.")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help="Rows per COPY chunk (default: 50000).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if source Last-Modified has not advanced "
                        "since the prior successful run.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD + download + read CSV header only; no DB writes.")
    p.add_argument("--recon-only", action="store_true",
                   help="Run recon SELECTs against existing table contents and exit.")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP downloads (default: /tmp/dol_form5500_ingest).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.recon_only:
        run_recon_only()
        return 0

    forms = list(FORMS.values()) if args.form == "all" else [FORMS[args.form]]
    years: list[int]
    if args.year == "all":
        years = list(SUPPORTED_YEARS)
    else:
        try:
            yr = int(args.year)
        except ValueError:
            log.error("year must be an int or 'all'")
            return 2
        if yr not in SUPPORTED_YEARS:
            log.error("year %s not in supported set %s", yr, SUPPORTED_YEARS)
            return 2
        years = [yr]

    workdir = Path(args.workdir or "/tmp/dol_form5500_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    rc = 0
    for cfg in forms:
        for year in years:
            ds_rc = ingest_one(
                cfg,
                year=year,
                batch_size=args.batch_size,
                skip_if_unchanged=args.skip_if_unchanged,
                dry_run=args.dry_run,
                workdir=workdir,
            )
            rc = rc or ds_rc

    if not args.dry_run:
        run_recon_only()
    return rc


if __name__ == "__main__":
    sys.exit(main())
