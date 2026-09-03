"""Synthetic, data-free tests for the strict PL.csv source loader."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from warehouse.load_common import CsvSchemaError, FiscalYearError, MoneyParseError, ProvenanceSpec
from warehouse.load_pl import (
    PLDuplicateError,
    PLIntegrityError,
    PLSchemaError,
    load_pl_csv,
)


def _write_pl(path: Path, rows: list[dict[str, object]], *, bom: bool = True) -> None:
    header: list[str] = []
    for row in rows:
        for name in row:
            if name not in header:
                header.append(name)
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def _spec() -> ProvenanceSpec:
    return ProvenanceSpec(
        source_system="egramswaraj",
        source_run_id="run-pl-1",
        source_file="PL.csv",
        source_kind="PL",
        schema_version="v1",
    )


def _rows() -> list[dict[str, object]]:
    common = {
        "gpCode": "00123",
        "gpName": "Synthetic GP",
        "planYear": "2021",
        "plan_typ": "1",
        "planCodeStts": "2",
        "approvalDate": "2021-06-30",
        "activityType": "7",
        "activityDesc": "description",
        "focusArea": "01",
        "activityFor": "9",
        "workTyp": "3",
        "activityForCostlessFlag": "N",
        "operationType": "1",
        "operationRemarks": "remark",
        "outputTyp": "4",
        "activityStts": "2",
        "mainAstCtgry": "08",
        "mainAstSubCtgry": "09",
        "mainAstUntTyp": "10",
        "mainAstNumOfUnt": "2",
        "dlagtdFlag": "N",
        "dlagtdPlnUntCd": "0007",
        "dlagtdPlnUntTyp": "2",
        "dlagtdPlnUntLvl": "3",
        "dlagtdPlnUntCat": "4",
        "shareable": "Y",
        "dlagtdPerentPlnUntCd": "0008",
        "trainingCapacity_trngCatCd": "11",
        "trainingCapacity_trngOrgByCd": "12",
        "trainingCapacity_trngSubject": "subject",
        "trainingCapacity_totTrainees": "6",
        "trainingCapacity_totDurationDays": "4",
        "communityService_serviCd": "13",
        "communityService_serviDuration": "5",
        "communityService_totalexpBeneficiares": "20",
        "assetDetails_astTyp": "14",
        "assetDetails_astCtgry": "15",
        "assetDetails_astSubCtgry": "16",
        "assetDetails_astCvrgCd": "17",
        "assetDetails_astNm": "asset",
        "assetDetails_astUntTyp": "18",
        "assetDetails_astNumOfUnt": "1",
        "assetDetails_astUnitCost": "₹1,234.50",
        "assetDetails_astParameterTyp": "19",
        "assetDetails_assetLocationDetails_astLocCd": "20",
        "assetDetails_assetLocationDetails_astPlnUntCd": "21",
        "assetDetails_assetLocationDetails_astPlnUntTyp": "22",
        "assetDetails_assetLocationDetails_astNoOfUnt": "1",
        "assetDetails_assetLocationDetails_astUnitCostTot": "1234.50",
        "assetDetails": '[{"asset":"first"},{"asset":"overflow"}]',
        "assetDetails_assetLocationDetails": '[{"loc":"A"},{"loc":"B"}]',
        "fundList_schemeCode": "SCHEME-01",
        "fundList_componentCode": "COMP-01",
        "fundList_tiedAmountGen": "10.00",
        "fundList_tiedAmountSc": "11.00",
        "fundList_tiedAmountSt": "12.00",
        "fundList_untiedAmountGen": "13.00",
        "fundList_untiedAmountSc": "14.00",
        "fundList_untiedAmountSt": "15.00",
        "fundList_amountTotal": "75.00",
        "fundList_tiedAbundonAmountGen": "1.00",
        "fundList_tiedAbundonAmountSc": "2.00",
        "fundList_tiedAbundonAmountSt": "3.00",
        "fundList_untiedAbundonAmountGen": "4.00",
        "fundList_untiedAbundonAmountSc": "5.00",
        "fundList_untiedAbundonAmountSt": "6.00",
        "fundList": '[{"schemeCode":"SCHEME-01"}]',
        "trainingCapacity": '{"extra":"capacity"}',
        "communityService": '{"extra":"community"}',
        "activityNsap": '{"payload":"retained"}',
        "activityNsap_old_age_below_eighty_male": "",
        "activityNsap_old_age_below_eighty_female": "0",
        "activityNsap_widow_female": "NA",
        "unmapped_future_column": "extension value",
    }
    first = {**common, "activityCd": "0007", "planCode": "P-1", "activityName": "first\nactivity", "totalCost": "100.25", "source_file": "2021-2022/PL.csv"}
    second = {**common, "activityCd": "0008", "planCode": "P-1", "activityName": "second", "totalCost": "200.00", "source_file": "2022-2023/PL.csv", "unmapped_future_column": ""}
    third = {**common, "activityCd": "0009", "planCode": "P-2", "activityName": "third", "totalCost": "300.00", "source_file": "2023-2024/PL.csv", "assetDetails": ""}
    return [first, second, third]


def test_pl_csv_loads_all_tables_and_preserves_extensions(tmp_path: Path):
    path = tmp_path / "PL.csv"
    _write_pl(path, _rows())

    result = load_pl_csv(path, spec=_spec(), chunk_size=2)

    assert result.source_rows == 3
    assert result.counts == {
        "plan": 2,
        "planned_activity": 3,
        "activity_asset": 3,
        "activity_fund": 3,
        "activity_training": 3,
        "activity_delegation": 3,
        "activity_community_service": 3,
        "activity_nsap": 0,
    }
    assert result.nsap_empty_asserted is True
    assert result["quarantine"].equals(result.unmapped_extensions)

    planned = result["planned_activity"]
    assert list(planned["activity_code"]) == ["0007", "0008", "0009"]
    assert planned.loc[0, "activity_name"] == "first\nactivity"
    assert planned.loc[0, "focus_area"] == "01"  # raw code, no decode
    assert planned.loc[0, "total_cost"] == pytest.approx(100.25)
    assert planned.loc[0, "source_file"] == "2021-2022/PL.csv"
    assert planned.loc[0, "source_record_id"] == planned.loc[0, "row_id"]

    asset = result["activity_asset"]
    assert asset.loc[0, "asset_category"] == "15"
    assert asset.loc[0, "asset_unit_cost"] == pytest.approx(1234.5)
    assert asset.loc[0, "source_record_id"] == planned.loc[0, "source_record_id"]
    assert asset.loc[0, "row_id"] != planned.loc[0, "row_id"]
    assert set(asset["activity_code"]) == set(planned["activity_code"])

    extensions = result.unmapped_extensions
    assert set(extensions["extension_name"]) == {
        "assetDetails",
        "assetDetails_assetLocationDetails",
        "fundList",
        "trainingCapacity",
        "communityService",
        "activityNsap",
        "unmapped_future_column",
    }
    location = extensions.loc[
        extensions["extension_name"] == "assetDetails_assetLocationDetails", "raw_value"
    ].iloc[0]
    assert location == '[{"loc":"A"},{"loc":"B"}]'
    assert set(extensions["mapping_status"]) == {"unmapped"}


def test_satellite_child_row_ids_do_not_collide_across_tables(tmp_path: Path):
    path = tmp_path / "PL.csv"
    _write_pl(path, _rows())

    result = load_pl_csv(path, spec=_spec(), chunk_size=2)

    asset = result["activity_asset"]
    fund = result["activity_fund"]
    training = result["activity_training"]
    delegation = result["activity_delegation"]
    community = result["activity_community_service"]

    # Every satellite shares the same parent activity and position (0) for
    # the first row; only the child_collection key differs, so row_id must
    # differ too or two collections would collide at the same position
    # under the same parent (the bug child_collection exists to prevent).
    row_ids = [
        asset.loc[0, "row_id"],
        fund.loc[0, "row_id"],
        training.loc[0, "row_id"],
        delegation.loc[0, "row_id"],
        community.loc[0, "row_id"],
    ]
    assert len(set(row_ids)) == len(row_ids)
    assert asset.loc[0, "parent_row_id"] == fund.loc[0, "parent_row_id"]
    assert asset.loc[0, "pos"] == fund.loc[0, "pos"] == 0


def test_pl_csv_output_is_chunk_size_invariant(tmp_path: Path):
    path = tmp_path / "PL.csv"
    _write_pl(path, _rows())
    one = load_pl_csv(path, spec=_spec(), chunk_size=1)
    large = load_pl_csv(path, spec=_spec(), chunk_size=100)

    assert one.source_rows == large.source_rows
    assert one.counts == large.counts
    for table in one.tables:
        pd.testing.assert_frame_equal(one[table], large[table])
    pd.testing.assert_frame_equal(one.unmapped_extensions, large.unmapped_extensions)


def test_pl_csv_rejects_global_activity_duplicate_across_chunk_boundary(tmp_path: Path):
    path = tmp_path / "duplicate.csv"
    rows = _rows()
    rows[2]["activityCd"] = "0007"
    _write_pl(path, rows)

    with pytest.raises(PLDuplicateError, match="globally unique"):
        load_pl_csv(path, spec=_spec(), chunk_size=2)


def test_pl_csv_rejects_conflicting_repeated_plan_across_chunk_boundary(tmp_path: Path):
    path = tmp_path / "conflicting-plan.csv"
    rows = _rows()
    rows[1]["plan_typ"] = "DIFFERENT"
    _write_pl(path, rows)

    with pytest.raises(PLIntegrityError, match="plan_code"):
        load_pl_csv(path, spec=_spec(), chunk_size=1)


@pytest.mark.parametrize(
    ("mutator", "error", "message"),
    [
        (lambda row: row.pop("planCode"), PLSchemaError, "plan_code"),
        (lambda row: row.__setitem__("gpCode", ""), PLSchemaError, "gp_lgd_code"),
        (lambda row: row.__setitem__("planYear", "2021-23"), FiscalYearError, "fiscal_year"),
        (lambda row: row.__setitem__("planYear", "2021-22"), FiscalYearError, "fiscal_year"),
        (lambda row: row.__setitem__("totalCost", "not-money"), MoneyParseError, "total_cost"),
        (lambda row: row.__setitem__("activityNsap_old_age_below_eighty_male", "1.5"), PLSchemaError, "integer"),
    ],
)
def test_pl_csv_fails_closed_on_bad_source_contract(tmp_path: Path, mutator, error, message):
    path = tmp_path / "invalid.csv"
    row = _rows()[0]
    mutator(row)
    _write_pl(path, [row])

    with pytest.raises(error, match=message):
        load_pl_csv(path, spec=_spec(), chunk_size=1)


def test_pl_csv_rejects_malformed_width_before_yielding_any_batch(tmp_path: Path):
    path = tmp_path / "malformed.csv"
    path.write_text("activityCd,planCode,gpCode,planYear\n7,P,123,2021,EXTRA\n", encoding="utf-8")

    with pytest.raises(CsvSchemaError, match="row 2 has 5 fields; expected 4"):
        list(load_pl_csv(path, spec=_spec(), chunk_size=1) for _ in [0])


def test_pl_csv_can_use_constant_context_and_melts_nonzero_nsap(tmp_path: Path):
    path = tmp_path / "minimal.csv"
    _write_pl(
        path,
        [
            {
                "activityCd": "0007",
                "planCode": "P-1",
                "planYear": "2021-2022",
                "totalCost": "1",
                "activityNsap_old_age_below_eighty_male": "3",
            }
        ],
        bom=False,
    )
    result = load_pl_csv(path, spec=ProvenanceSpec("src", "run", "PL.csv", "PL", gp_code="123"))
    assert result["activity_nsap"].loc[0, "beneficiary_count"] == 3
    assert result["activity_nsap"].loc[0, "category"] == "old_age"
    assert result.nsap_empty_asserted is False


def test_nonzero_nsap_categories_have_distinct_surrogate_and_lineage_ids(tmp_path: Path):
    path = tmp_path / "nsap.csv"
    _write_pl(
        path,
        [
            {
                "activityCd": "7",
                "planCode": "P",
                "gpCode": "123",
                "planYear": "2021",
                "activityNsap_old_age_below_eighty_male": "3",
                "activityNsap_widow_female": "2",
            }
        ],
    )

    result = load_pl_csv(path, spec=_spec(), chunk_size=1)
    nsap = result["activity_nsap"]
    assert list(nsap["nsap_id"]) == [1, 2]
    assert nsap["row_id"].nunique() == 2
    assert nsap["source_record_id"].nunique() == 1


def test_empty_pl_csv_has_explicit_empty_tables_but_still_validates_header(tmp_path: Path):
    path = tmp_path / "empty.csv"
    path.write_text("activityCd,planCode,gpCode,planYear\n", encoding="utf-8")

    result = load_pl_csv(path, spec=_spec(), chunk_size=1)

    assert result.source_rows == 0
    assert set(result.counts) == {
        "plan",
        "planned_activity",
        "activity_asset",
        "activity_fund",
        "activity_training",
        "activity_delegation",
        "activity_community_service",
        "activity_nsap",
    }
    assert all(count == 0 for count in result.counts.values())
    assert "source_record_id" in result["planned_activity"]
