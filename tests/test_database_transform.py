"""Shaping rules, tested without a database."""

from __future__ import annotations

import pandas as pd
import pytest

from database import transform
from database.clean import (to_code, to_fiscal_year, year_from_voucher_no)
from database.transform import Quarantine


# ---------------------------------------------------------------- cleaning


@pytest.mark.parametrize("value,expected", [
    (119598, "119598"),
    (119598.0, "119598"),
    ("119598.0", "119598"),
    (" 119598 ", "119598"),
])
def test_codes_never_keep_a_float_tail(value, expected):
    assert to_code(pd.Series([value])).iloc[0] == expected


def test_code_keeps_leading_zeros():
    assert to_code(pd.Series(["0119598"])).iloc[0] == "0119598"


@pytest.mark.parametrize("value,expected", [
    (2024, "2024-2025"),
    ("2024", "2024-2025"),
    ("2024-2025", "2024-2025"),
])
def test_fiscal_year_normalises(value, expected):
    assert to_fiscal_year(pd.Series([value])).iloc[0] == expected


def test_voucher_year_comes_from_the_voucher_not_the_plan():
    assert year_from_voucher_no("XVFC/2025-26/P/143") == "2025-2026"


def test_unparseable_voucher_number_yields_no_year():
    assert pd.isna(year_from_voucher_no("no-year-here"))


# ---------------------------------------------------------------- quarantine


def _plan_row(**overrides):
    row = {"plan_code": "A", "gp_lgd_code": "1", "fiscal_year": "2024-2025",
           "plan_type": "GPDP", "approval_date": None}
    row.update(overrides)
    return row


def test_identical_duplicates_collapse_without_noise():
    """A fact table reaching a dimension's grain is expected, not a defect."""
    quarantine = Quarantine()
    frame = pd.DataFrame([_plan_row(), _plan_row()])

    result = transform.plan(frame, quarantine)

    assert len(result) == 1
    assert quarantine.total("plan") == 0


def test_conflicting_duplicates_are_recorded_not_silently_dropped():
    """Two rows share a key but disagree, so keeping the first picks a winner."""
    quarantine = Quarantine()
    frame = pd.DataFrame([_plan_row(), _plan_row(plan_type="OSR")])

    result = transform.plan(frame, quarantine)

    assert len(result) == 1
    assert result["plan_type"].tolist() == ["GPDP"]
    assert quarantine.total("plan") == 1
    record = quarantine.frame().iloc[0]
    assert record["reason"] == "conflicting duplicate business key"
    assert record["key_value"] == "A"


def test_orphan_expenditure_is_quarantined_with_its_key(expenditure_csv):
    quarantine = Quarantine()
    cleaned = transform.clean_expenditure(expenditure_csv)

    result = transform.activity_expenditure(cleaned, {"128856295"}, quarantine)

    assert result["activity_code"].tolist() == ["128856295"]
    record = quarantine.frame().iloc[0]
    assert record["table_name"] == "activity_expenditure"
    assert record["key_value"] == "128856619"
    assert record["reason"] == "activity_code not in planning"


def test_expenditure_ids_are_stable_when_a_row_is_quarantined(expenditure_csv):
    """The surrogate must identify a source row whether or not it loaded."""
    cleaned = transform.clean_expenditure(expenditure_csv)

    everything = transform.activity_expenditure(
        cleaned, {"128856295", "128856619"}, Quarantine())
    partial = transform.activity_expenditure(cleaned, {"128856619"}, Quarantine())

    assert everything.loc[everything["activity_code"] == "128856619",
                          "expenditure_id"].tolist() == \
        partial["expenditure_id"].tolist()


# ---------------------------------------------------------------- bridge


def test_bridge_explodes_pipe_lists_and_matches_on_the_voucher_year(
        expenditure_csv, vouchers_csv):
    quarantine = Quarantine()
    expenditure = transform.clean_expenditure(expenditure_csv)
    vouchers = transform.clean_vouchers(vouchers_csv)

    spend = transform.activity_expenditure(
        expenditure, {"128856295", "128856619"}, quarantine)
    voucher_rows = transform.voucher(vouchers, {"119598"}, quarantine)
    bridge = transform.activity_voucher(expenditure, spend, voucher_rows)

    assert len(bridge) == 2
    # Plan year is 2024-2025; both vouchers are 2025-2026 and must still match.
    assert bridge["fiscal_year"].tolist() == ["2025-2026", "2025-2026"]
    assert bridge["voucher_pk"].notna().all()
    assert bridge["voucher_cost"].tolist() == [30000.0, 20000.0]


def test_bridge_skips_rows_whose_expenditure_was_quarantined(
        expenditure_csv, vouchers_csv):
    expenditure = transform.clean_expenditure(expenditure_csv)
    vouchers = transform.clean_vouchers(vouchers_csv)
    quarantine = Quarantine()

    spend = transform.activity_expenditure(expenditure, set(), quarantine)
    voucher_rows = transform.voucher(vouchers, {"119598"}, quarantine)
    bridge = transform.activity_voucher(expenditure, spend, voucher_rows)

    assert bridge.empty


def test_voucher_id_collisions_do_not_drop_real_vouchers(vouchers_csv):
    """Both rows share voucher_id V1; keying on it would lose one."""
    quarantine = Quarantine()
    vouchers = transform.clean_vouchers(vouchers_csv)

    result = transform.voucher(vouchers, {"119598"}, quarantine)

    assert len(result) == 2
    assert quarantine.total("voucher") == 0


# ---------------------------------------------------------------- extensions


def test_physical_progress_keeps_multi_capture_coordinates(
        physical_progress_csv):
    quarantine = Quarantine()

    result = transform.physical_progress(
        physical_progress_csv, {"128856295"}, quarantine)

    assert result["n_coords"].tolist() == [1, 2]
    assert result["latitude"].tolist() == [20.2961, 20.2961]
    assert result["latitude_raw"].tolist() == ["20.2961", "20.2961,20.2970"]


def test_approval_numbers_lose_leading_zeros(admin_approval_csv):
    result = transform.admin_approval(
        admin_approval_csv, {"128856295"}, {"119598"}, Quarantine())

    assert result["adm_approval_no"].iloc[0] == "7"


def test_approval_dates_parse_as_iso_not_day_first(admin_approval_csv):
    result = transform.admin_approval(
        admin_approval_csv, {"128856295"}, {"119598"}, Quarantine())

    assert result["adm_approval_sanction_date"].iloc[0] == pd.Timestamp("2024-05-01")


def test_approval_for_an_unknown_activity_is_quarantined(admin_approval_csv):
    quarantine = Quarantine()

    result = transform.admin_approval(
        admin_approval_csv, set(), {"119598"}, quarantine)

    assert result.empty
    assert quarantine.total("admin_approval") == 1


# ---------------------------------------------------------------- lookups


def test_dim_code_normalises_float_codes(code_descriptions_csv):
    result = transform.dim_code(code_descriptions_csv, Quarantine())

    assert result["code"].tolist() == ["7", "8"]


def test_nsap_all_null_columns_produce_no_rows(planning_csv):
    assert transform.activity_nsap(planning_csv).empty


def test_nsap_expands_non_zero_counts(planning_csv):
    frame = planning_csv.copy()
    frame.loc[0, "nsap_widow_female"] = 3

    result = transform.activity_nsap(frame)

    assert len(result) == 1
    assert result.iloc[0]["category"] == "widow"
    assert result.iloc[0]["gender"] == "female"
    assert result.iloc[0]["beneficiary_count"] == 3
