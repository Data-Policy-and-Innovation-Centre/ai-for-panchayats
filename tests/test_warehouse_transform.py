"""Unit tests for the pure transform layer, on small synthetic frames.

Per the caution about helpers whose call site is never exercised: every
behaviour here is also driven through :func:`warehouse.build.build` in
``tests/test_warehouse_build.py``. These tests exist to pin down the exact
field-level shaping rules cheaply; they do not substitute for the build-path
coverage.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from warehouse import transform as t
from warehouse.clean import to_decimal_money


def _row(**overrides) -> dict:
    base = {
        "source_system": "egramSwaraj", "source_run_id": "run-1", "row_id": "r0",
        "parent_row_id": None, "pos": None, "gp_code": "123", "gram_panchayat_name": "Test GP",
        "fiscal_year": "2021-2022", "plan_year": "2021-2022", "business_id": "7",
        "mapping_status": "mapped",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- decimal money


def test_decimal_money_is_exact_not_float():
    series = pd.Series(["0.10", "0.20"])
    values = to_decimal_money(series)
    assert values.iloc[0] == Decimal("0.10")
    assert values.iloc[1] == Decimal("0.20")
    # The float trap this guards against: 0.1 + 0.2 != 0.3 in binary float.
    assert values.iloc[0] + values.iloc[1] == Decimal("0.30")


def test_decimal_money_handles_currency_noise_and_negative():
    series = pd.Series(["Rs. 1,25,000.5", "-99.999", None, "not a number"])
    values = to_decimal_money(series)
    assert values.iloc[0] == Decimal("125000.50")
    assert values.iloc[1] == Decimal("-100.00")  # ROUND_HALF_UP at 2 places
    assert values.iloc[2] is None
    assert values.iloc[3] is None


# --------------------------------------------------------------------- planned_activity


def test_planned_activity_uses_business_id_and_provenance_gp():
    pl = pd.DataFrame([_row(row_id="r0", activityCd_unused=None, planCode="P1",
                             activityName="Well", totalCost=1500)])
    quarantine = t.Quarantine()
    out = t.planned_activity(pl, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert list(out["activity_code"]) == ["7"]
    assert list(out["gp_lgd_code"]) == ["123"]
    assert out["total_cost"].iloc[0] == Decimal("1500.00")


def test_planned_activity_conflicting_duplicate_is_quarantined_not_overwritten():
    pl = pd.DataFrame([
        _row(row_id="r0", business_id="7", activityName="Well", totalCost=100),
        _row(row_id="r1", business_id="7", activityName="Road", totalCost=200),
    ])
    quarantine = t.Quarantine()
    out = t.planned_activity(pl, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert len(out) == 1
    assert quarantine.total("planned_activity") == 1
    assert quarantine.records[0]["reason_code"] == "conflicting_duplicate_key"


def test_planned_activity_identical_repeat_is_kept_silently():
    pl = pd.DataFrame([
        _row(row_id="r0", business_id="7", activityName="Well", totalCost=100),
        _row(row_id="r1", business_id="7", activityName="Well", totalCost=100),
    ])
    quarantine = t.Quarantine()
    out = t.planned_activity(pl, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert len(out) == 1
    assert quarantine.total() == 0


# --------------------------------------------------------------------- activity_fund / activity_asset (regression)


def test_activity_fund_preserves_identifier_columns_not_just_money():
    """Regression test: FUND_MONEY_COLUMNS once matched fund_scheme_code and
    fund_component_code via a bare ``startswith("fund_")`` filter, routing
    them through decimal parsing and silently nulling every scheme code.
    Confirmed failing before the fix (values were None), passing after.

    Uses two different activities (not two lines for the same activity):
    activity_fund is strictly 1:1 with planned_activity (see schema.py), so
    two lines for one activity are a conflicting duplicate, not a valid
    fixture for this identifier-preservation check.
    """

    child = pd.DataFrame([
        _row(row_id="r0", business_id="7", schemeCode="S1", componentCode="C1", amountTotal=5000.25),
        _row(row_id="r1", business_id="8", schemeCode="S2", componentCode="C2", amountTotal=250.10),
    ])
    quarantine = t.Quarantine()
    out = t.activity_fund(child, {"7", "8"}, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert list(out["fund_scheme_code"]) == ["S1", "S2"]
    assert list(out["fund_component_code"]) == ["C1", "C2"]
    assert list(out["fund_amount_total"]) == [Decimal("5000.25"), Decimal("250.10")]


def test_activity_fund_is_one_to_one_second_line_is_quarantined():
    """activity_asset/activity_fund are strictly 1:1 with planned_activity
    (see schema.py's activity_asset/activity_fund comments): the real
    tables carry no row_id, so a second fund line for the same activity is
    a conflicting duplicate, not a legitimate second row. An earlier
    revision of this module modeled these as one-to-many child tables keyed
    by an invented row_id; that design is reversed here.
    """

    child = pd.DataFrame([
        _row(row_id="r0", schemeCode="S1", amountTotal=100),
        _row(row_id="r1", schemeCode="S2", amountTotal=200),
    ])
    quarantine = t.Quarantine()
    out = t.activity_fund(child, {"7"}, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert len(out) == 1
    assert list(out["fund_scheme_code"]) == ["S1"]
    assert quarantine.total("activity_fund") == 1
    assert quarantine.records[0]["reason_code"] == "conflicting_duplicate_key"


def test_activity_asset_orphan_activity_code_is_quarantined():
    child = pd.DataFrame([_row(row_id="r0", business_id="99", astTyp="well")])
    quarantine = t.Quarantine()
    out = t.activity_asset(child, {"7"}, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert out.empty
    assert quarantine.total("activity_asset") == 1
    assert quarantine.records[0]["reason_code"] == "orphan_reference"


# --------------------------------------------------------------------- activity_nsap grain


def test_activity_nsap_one_row_per_nonzero_category():
    pl = pd.DataFrame([_row(
        row_id="r0", business_id="7",
        activityNsap_old_age_below_eighty_male=3,
        activityNsap_old_age_below_eighty_female=0,
        activityNsap_widow_female=2,
    )])
    out = t.activity_nsap(pl, {"7"}, source_system="egramSwaraj", source_run_id="run-1")
    rows = {(r.category, r.age_band, r.gender): r.beneficiary_count for r in out.itertuples()}
    # beneficiary_count is a COUNT, not money (see schema.py's activity_nsap
    # DDL): an integer, never decimal.Decimal.
    assert rows[("old_age", "lt80", "male")] == 3
    assert not isinstance(rows[("old_age", "lt80", "male")], Decimal)
    assert ("old_age", "lt80", "female") not in rows  # zero counts are dropped
    assert rows[("widow", "na", "female")] == 2


# --------------------------------------------------------------------- activity_expenditure identity


def test_activity_expenditure_identity_and_orphan_gp():
    re_frame = pd.DataFrame([
        _row(row_id="r0", planCode="P1", sNo=1, totalExpenditure=500, gp_code="123"),
        _row(row_id="r1", planCode="P1", sNo=1, totalExpenditure=500, gp_code="999"),  # orphan GP
    ])
    quarantine = t.Quarantine()
    out = t.activity_expenditure(re_frame, {"123"}, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert len(out) == 1
    assert list(out["s_no"]) == ["1"]
    assert quarantine.total("activity_expenditure") == 1
    assert quarantine.records[0]["reason_code"] == "orphan_gp"


def test_activity_expenditure_assigns_expenditure_id_starting_at_start_id():
    """expenditure_id is the table's actual primary key (an INTEGER
    surrogate the source data has no spelling for at all); it must be
    assigned densely starting at the caller-supplied start_id, 1:1 with the
    rows that actually survive quarantine/orphan filtering -- not with the
    rows in the input frame."""

    re_frame = pd.DataFrame([
        _row(row_id="r0", planCode="P1", sNo=1, totalExpenditure=500, gp_code="123"),
        _row(row_id="r1", planCode="P1", sNo=2, totalExpenditure=700, gp_code="999"),  # orphan GP, dropped
        _row(row_id="r2", planCode="P1", sNo=3, totalExpenditure=900, gp_code="123"),
    ])
    quarantine = t.Quarantine()
    out = t.activity_expenditure(
        re_frame, {"123"}, quarantine, source_system="egramSwaraj", source_run_id="run-1", start_id=41,
    )
    assert len(out) == 2  # the orphan-GP row was dropped
    assert list(out["expenditure_id"]) == [41, 42]  # dense, starting at start_id


def test_activity_expenditure_conflicting_serial_number_is_quarantined():
    re_frame = pd.DataFrame([
        _row(row_id="r0", planCode="P1", sNo=1, totalExpenditure=500),
        _row(row_id="r1", planCode="P1", sNo=1, totalExpenditure=999),
    ])
    quarantine = t.Quarantine()
    out = t.activity_expenditure(re_frame, {"123"}, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert len(out) == 1
    assert quarantine.records[-1]["reason_code"] == "conflicting_duplicate_key"


# ------------------------------------------------------ RE field alias resolution


def test_activity_expenditure_raises_when_required_field_has_no_candidate():
    """s_no is part of the documented identity; if the source frame has none
    of its candidate spellings, ``_first_present`` used to hand back a
    silent all-null column. It must now fail loudly and name the field, the
    candidates tried, and the columns actually present."""

    re_frame = pd.DataFrame([_row(row_id="r0", planCode="P1", totalExpenditure=500)])  # no sNo/s_no/etc.
    quarantine = t.Quarantine()
    with pytest.raises(t.RequiredFieldUnresolved) as excinfo:
        t.activity_expenditure(re_frame, {"123"}, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    message = str(excinfo.value)
    assert "s_no" in message
    assert "sNo" in message  # a candidate that was tried
    assert "planCode" in message  # a column that was actually present


def test_activity_expenditure_raises_when_plan_code_has_no_candidate():
    re_frame = pd.DataFrame([_row(row_id="r0", sNo=1, totalExpenditure=500)])  # no planCode/plan_code
    quarantine = t.Quarantine()
    with pytest.raises(t.RequiredFieldUnresolved) as excinfo:
        t.activity_expenditure(re_frame, {"123"}, quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert "plan_code" in str(excinfo.value)


def test_activity_expenditure_optional_field_with_no_candidate_resolves_null_and_is_recorded():
    """approved_cost_action_plan is genuinely optional: a missing alias must
    not raise, but the null resolution must be recorded rather than merely
    commented, per the module docstring's promise."""

    re_frame = pd.DataFrame([_row(row_id="r0", planCode="P1", sNo=1, totalExpenditure=500)])
    quarantine = t.Quarantine()
    resolutions = t.FieldResolutions()
    out = t.activity_expenditure(
        re_frame, {"123"}, quarantine, source_system="egramSwaraj", source_run_id="run-1",
        resolutions=resolutions,
    )
    assert out["approved_cost_action_plan"].isna().all()
    unresolved_fields = {r.field for r in resolutions.unresolved()}
    assert "approved_cost_action_plan" in unresolved_fields
    assert all(r.matched_candidate is None for r in resolutions.unresolved())


def test_activity_expenditure_non_first_candidate_resolves_and_is_recorded():
    """A later-listed spelling (not the first candidate) must still resolve
    correctly, and the specific candidate that matched must be recorded."""

    re_frame = pd.DataFrame([_row(row_id="r0", plan_code="P1", sno=1, totalExpenditure=500)])
    quarantine = t.Quarantine()
    resolutions = t.FieldResolutions()
    out = t.activity_expenditure(
        re_frame, {"123"}, quarantine, source_system="egramSwaraj", source_run_id="run-1",
        resolutions=resolutions,
    )
    assert list(out["plan_code"]) == ["P1"]
    assert list(out["s_no"]) == ["1"]
    matched = {r.field: r.matched_candidate for r in resolutions.records}
    assert matched["plan_code"] == "plan_code"
    assert matched["s_no"] == "sno"


# --------------------------------------------------------------------- gram_panchayat


def test_gram_panchayat_unions_across_kinds_and_dedupes():
    pl = pd.DataFrame([_row(gp_code="123", gram_panchayat_name="Test GP")])
    aa = pd.DataFrame([_row(gp_code="123", gram_panchayat_name="Test GP")])
    quarantine = t.Quarantine()
    out = t.gram_panchayat([pl, aa], quarantine, source_system="egramSwaraj", source_run_id="run-1")
    assert len(out) == 1
    assert out.iloc[0]["gp_lgd_code"] == "123"
