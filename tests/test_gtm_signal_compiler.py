"""Unit tests for the generalized GTM signal criteria compiler.

Pure / offline — no DB, no network. Covers each op, identifier-injection
defenses, and parity with the two legacy USAspending seeds (incl. the
action_type IS NULL OR-branch and the TRY_CAST numeric coercion).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.gtm_signal_compiler import (
    CompileError,
    compile_criteria,
)

NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)

FPDS_COLS = {
    "recipient_uei",
    "generated_unique_award_id",
    "piid",
    "fain",
    "type_description",
    "action_type",
    "modification_number",
    "action_date",
    "federal_action_obligation",
    "awarding_toptier_agency_name",
    "awarding_subtier_agency_name",
}
SAM_COLS = {"uei", "cage_code", "legal_business_name"}


def _c(criteria, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("allowed_columns", FPDS_COLS)
    return compile_criteria(criteria, **kw)


# ── individual ops ────────────────────────────────────────────────────────


def test_eq():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "piid", "op": "eq", "value": "ABC"}]})
    assert r.where_sql == '"piid" = ?'
    assert r.bindings == ["ABC"]


def test_gte_numeric_wraps_try_cast():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "federal_action_obligation", "op": "gte", "value": 100000}]})
    assert r.where_sql == 'TRY_CAST("federal_action_obligation" AS DOUBLE) >= ?'
    assert r.bindings == [100000]
    assert "100000" not in r.where_sql  # value is a binding, never interpolated


def test_gte_nonnumeric_no_cast():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "action_date", "op": "gte", "value": "2026-01-01"}]})
    assert r.where_sql == '"action_date" >= ?'
    assert r.bindings == ["2026-01-01"]


def test_between_numeric():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "federal_action_obligation", "op": "between", "value": [1000, 5000]}]})
    assert r.where_sql == '(TRY_CAST("federal_action_obligation" AS DOUBLE) >= ? AND TRY_CAST("federal_action_obligation" AS DOUBLE) <= ?)'
    assert r.bindings == [1000, 5000]


def test_in_and_is_null_branches():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "type_description", "op": "in",
                            "value": ["A", "B", "C"]}]})
    assert r.where_sql == '("type_description" IN (?,?,?))'
    assert r.bindings == ["A", "B", "C"]


def test_in_with_null_emits_or_is_null():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "action_type", "op": "in", "value": ["C", None]}]})
    assert r.where_sql == '("action_type" IN (?) OR "action_type" IS NULL)'
    assert r.bindings == ["C"]


def test_in_only_null():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "action_type", "op": "in", "value": [None]}]})
    assert r.where_sql == '("action_type" IS NULL)'
    assert r.bindings == []


def test_is_null_not_null_like():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [
                {"column": "piid", "op": "not_null"},
                {"column": "fain", "op": "is_null"},
                {"column": "awarding_toptier_agency_name", "op": "like", "value": "%DEFENSE%"},
            ]})
    assert r.where_sql == '"piid" IS NOT NULL AND "fain" IS NULL AND "awarding_toptier_agency_name" LIKE ?'
    assert r.bindings == ["%DEFENSE%"]


def test_time_window_sets_clause_and_scan_filter():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "time_window": {"column": "action_date", "hours": 24}})
    assert r.where_sql == '"action_date" >= ? AND "action_date" <= ?'
    assert r.bindings == ["2026-05-28", "2026-05-29"]
    assert r.scan_filter == {"column": "action_date", "gte": "2026-05-28", "lte": "2026-05-29"}


def test_empty_predicates_is_true():
    r = _c({"spine_target": "usaspending.transaction_fpds_lance"})
    assert r.where_sql == "TRUE"
    assert r.bindings == []


# ── injection / validation defenses ───────────────────────────────────────


def test_unknown_column_raises():
    with pytest.raises(CompileError):
        _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "evil_col", "op": "eq", "value": 1}]})


def test_identifier_injection_raises():
    with pytest.raises(CompileError):
        _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": 'action_date" ; DROP TABLE x; --', "op": "eq", "value": 1}]})


def test_unsupported_op_raises():
    with pytest.raises(CompileError):
        _c({"spine_target": "usaspending.transaction_fpds_lance",
            "predicates": [{"column": "piid", "op": "regexp_or_die", "value": "x"}]})


def test_invalid_spine_target_raises():
    with pytest.raises(CompileError):
        _c({"spine_target": "not a dotted id"})
    with pytest.raises(CompileError):
        _c({"spine_target": "s3://dex-raw-landing-zone/x"})


def test_select_validates_columns():
    with pytest.raises(CompileError):
        _c({"spine_target": "usaspending.transaction_fpds_lance",
            "select": ["piid", "nope_col"]})
    r = _c({"spine_target": "usaspending.transaction_fpds_lance", "select": ["piid", "fain"]})
    assert r.select == ["piid", "fain"]


def test_order_by_validates():
    with pytest.raises(CompileError):
        _c({"spine_target": "usaspending.transaction_fpds_lance",
            "order_by": {"column": "x", "dir": "desc"}})
    with pytest.raises(CompileError):
        _c({"spine_target": "usaspending.transaction_fpds_lance",
            "order_by": {"column": "piid", "dir": "sideways"}})
    r = _c({"spine_target": "usaspending.transaction_fpds_lance",
            "order_by": {"column": "federal_action_obligation", "dir": "desc"}})
    assert r.order_by == {"column": "federal_action_obligation", "dir": "desc"}


def test_join_requires_allowed_join_columns():
    crit = {"spine_target": "usaspending.transaction_fpds_lance",
            "join": {"dataset": "spines.sam_entities_lance",
                     "on": ["recipient_uei", "uei"],
                     "select": ["cage_code", "legal_business_name"]}}
    with pytest.raises(CompileError):
        _c(crit)  # no allowed_join_columns
    r = _c(crit, allowed_join_columns=SAM_COLS)
    assert r.join == {"dataset": "spines.sam_entities_lance",
                      "on": ["recipient_uei", "uei"],
                      "select": ["cage_code", "legal_business_name"]}


def test_join_unknown_join_column_raises():
    with pytest.raises(CompileError):
        _c({"spine_target": "usaspending.transaction_fpds_lance",
            "join": {"dataset": "spines.sam_entities_lance",
                     "on": ["recipient_uei", "uei"],
                     "select": ["cage_code", "ssn"]}},
           allowed_join_columns=SAM_COLS)


# ── legacy seed parity ─────────────────────────────────────────────────────


def _seed_net_new_100k():
    return {
        "spine_target": "usaspending.transaction_fpds_lance",
        "time_window": {"column": "action_date", "hours": 24},
        "predicates": [
            {"column": "federal_action_obligation", "op": "gte", "value": 100000},
            {"column": "type_description", "op": "in",
             "value": ["DEFINITIVE CONTRACT", "DELIVERY ORDER", "PURCHASE ORDER", "BPA CALL"]},
            {"column": "action_type", "op": "in", "value": [None]},
        ],
        "join": {"dataset": "spines.sam_entities_lance",
                 "on": ["recipient_uei", "uei"],
                 "select": ["cage_code", "legal_business_name"]},
        "order_by": {"column": "federal_action_obligation", "dir": "desc"},
    }


def test_seed_net_new_100k_parity():
    r = _c(_seed_net_new_100k(), allowed_join_columns=SAM_COLS)
    assert r.where_sql == (
        '"action_date" >= ? AND "action_date" <= ? '
        'AND TRY_CAST("federal_action_obligation" AS DOUBLE) >= ? '
        'AND ("type_description" IN (?,?,?,?)) '
        'AND ("action_type" IS NULL)'
    )
    assert r.bindings == [
        "2026-05-28", "2026-05-29", 100000,
        "DEFINITIVE CONTRACT", "DELIVERY ORDER", "PURCHASE ORDER", "BPA CALL",
    ]
    assert r.scan_filter == {"column": "action_date", "gte": "2026-05-28", "lte": "2026-05-29"}
    assert r.join is not None and r.order_by == {"column": "federal_action_obligation", "dir": "desc"}


def test_seed_expansion_event_parity():
    crit = {
        "spine_target": "usaspending.transaction_fpds_lance",
        "time_window": {"column": "action_date", "hours": 24},
        "predicates": [
            {"column": "federal_action_obligation", "op": "gte", "value": 50000},
            {"column": "type_description", "op": "in",
             "value": ["DEFINITIVE CONTRACT", "DELIVERY ORDER"]},
            {"column": "action_type", "op": "in", "value": ["C", "G", "A"]},
        ],
    }
    r = _c(crit)
    assert r.where_sql == (
        '"action_date" >= ? AND "action_date" <= ? '
        'AND TRY_CAST("federal_action_obligation" AS DOUBLE) >= ? '
        'AND ("type_description" IN (?,?)) '
        'AND ("action_type" IN (?,?,?))'
    )
    assert r.bindings == [
        "2026-05-28", "2026-05-29", 50000,
        "DEFINITIVE CONTRACT", "DELIVERY ORDER", "C", "G", "A",
    ]
