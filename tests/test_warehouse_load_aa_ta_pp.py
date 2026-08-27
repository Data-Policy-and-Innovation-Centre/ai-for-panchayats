"""Synthetic adversarial tests for the #47 AA/TA/PP CSV loaders."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from warehouse.load_aa_ta_pp import (
    AA_SCHEME_SOURCE_COLUMNS,
    AA_SOURCE_COLUMNS,
    ADMIN_APPROVAL_SCHEME_COLUMNS,
    ApprovalParentIndex,
    CoordinateMismatchError,
    CoordinateParseError,
    DuplicateKeyError,
    LoaderAudit,
    OrphanReferenceError,
    PP_UPLOAD_SOURCE_COLUMNS,
    SourceYearError,
    TA_SOURCE_COLUMNS,
    SemanticValidationError,
    iter_admin_approval,
    load_admin_approval,
    load_admin_approval_scheme,
    load_admin_approval_with_index,
    load_physical_progress,
    load_technical_approval,
)
from warehouse.load_common import CsvSchemaError, DateParseError, ProvenanceSpec


def _write_csv(
    path: Path,
    columns: tuple[str, ...],
    rows: list[dict[str, object]],
    *,
    bom: bool = False,
) -> None:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(
        {column: row.get(column, "") for column in columns} for row in rows
    )
    text = stream.getvalue()
    path.write_bytes(("\ufeff" + text if bom else text).encode("utf-8"))


def _spec(kind: str, filename: str) -> ProvenanceSpec:
    return ProvenanceSpec(
        source_system="egramswaraj",
        source_run_id="synthetic-run-1",
        source_file=filename,
        source_kind=kind,
        schema_version="47-test-v1",
    )


def _aa_row(
    row_id: str,
    activity: str,
    *,
    approval_no: str = "01",
    year: str = "2025",
    date: str = "2026-02-19",
    cost: str = "10.25",
    gp: str = "0012",
) -> dict[str, object]:
    return {
        "row_id": row_id,
        "lgd_code": gp,
        "gram_panchayat_name": "Synthetic GP",
        "plan_year": year,
        "doc_type": "AA",
        "source_file": "2025_AA.csv",
        "activityCd": activity,
        "wrkPlnYr": year,
        "wrkAdmApprNo": approval_no,
        "wrkAdmApprSnctnOrdrDt": date,
        "wrkProposedCost": cost,
        "wrkAdmApprIssAuthrty": "bdo",
    }


def _scheme_row(
    row_id: str, parent: str, activity: str, *, pos: int = 0
) -> dict[str, object]:
    return {
        "row_id": row_id,
        "parent_row_id": parent,
        "pos": str(pos),
        "activityCd": activity,
        "wrkSchmCd": "001769",
        "wrkSchmCmpntCd": "004249",
        "wrkAdmApprFndSnctnGen": "10.25",
        "wrkAdmApprFndSnctnSt": "",
        "wrkAdmApprFndSnctnSc": "0",
        "fndAllctnSchmTot": "10.25",
    }


def _ta_row(
    row_id: str,
    activity: str,
    *,
    required: str = "R",
    cost: str = "2.50",
    authority: str = "bdo",
    order_no: str = "007",
    date: str = "2026-02-20",
) -> dict[str, object]:
    return {
        "row_id": row_id,
        "lgd_code": "0012",
        "gram_panchayat_name": "Synthetic GP",
        "plan_year": "2025",
        "doc_type": "TA",
        "source_file": "2025_TA.csv",
        "activityCd": activity,
        "wrkTecApprReqFlg": required,
        "wrkTecApprCost": cost,
        "wrkTecApprIssAuthrty": authority,
        "wrkTecApprOrdrNo": order_no,
        "wrkTecApprOrdrDt": date,
    }


def _pp_row(
    row_id: str,
    activity: str,
    *,
    parent: str = "stage-1",
    pos: int = 0,
    longitude: str = "85.1",
    latitude: str = "20.1",
) -> dict[str, object]:
    return {
        "row_id": row_id,
        "parent_row_id": parent,
        "pos": str(pos),
        "activityCd": activity,
        "fileUploadId": "000315",
        "longitude": longitude,
        "latitude": latitude,
        "plnunttypecode": "2.0",
    }


def test_admin_is_bom_safe_chunked_and_keeps_duplicate_approval_numbers(tmp_path: Path):
    path = tmp_path / "aa.csv"
    _write_csv(
        path,
        AA_SOURCE_COLUMNS,
        [
            _aa_row("aa-1", "activity-1", approval_no="01"),
            _aa_row("aa-2", "activity-2", approval_no="1"),
        ],
        bom=True,
    )

    frame = load_admin_approval(path, _spec("AA", "aa.csv"), chunksize=1)

    assert len(frame) == 2
    assert list(frame["row_id"]) == ["aa-1", "aa-2"]
    assert list(frame["adm_approval_no"]) == ["1", "1"]
    assert frame.loc[0, "work_proposed_cost"] == Decimal("10.25")
    assert frame.loc[0, "adm_approval_sanction_date"] == pd.Timestamp("2026-02-19")
    assert list(frame["plan_year"]) == ["2025", "2025"]


def test_admin_repeated_activity_rows_are_preserved_across_chunks(tmp_path: Path):
    path = tmp_path / "repeated-activity.csv"
    _write_csv(
        path,
        AA_SOURCE_COLUMNS,
        [_aa_row("aa-1", "same"), _aa_row("aa-2", "same")],
    )
    frame = load_admin_approval(
        path, _spec("AA", "repeated-activity.csv"), chunksize=1
    )
    assert list(frame["activity_code"]) == ["same", "same"]
    assert list(frame["row_id"]) == ["aa-1", "aa-2"]


def test_admin_duplicate_row_id_is_checked_across_chunks(tmp_path: Path):
    path = tmp_path / "duplicate-row.csv"
    _write_csv(
        path,
        AA_SOURCE_COLUMNS,
        [_aa_row("aa-1", "activity-1"), _aa_row("aa-1", "activity-2")],
    )
    with pytest.raises(DuplicateKeyError, match="row_id"):
        load_admin_approval(path, _spec("AA", "duplicate-row.csv"), chunksize=1)


def test_admin_parent_index_and_child_linkage_survive_chunk_boundaries(tmp_path: Path):
    aa_path = tmp_path / "aa.csv"
    child_path = tmp_path / "aa-scheme.csv"
    _write_csv(
        aa_path,
        AA_SOURCE_COLUMNS,
        [_aa_row("aa-1", "activity-1"), _aa_row("aa-2", "activity-2")],
    )
    _write_csv(
        child_path,
        AA_SCHEME_SOURCE_COLUMNS,
        [
            _scheme_row("child-1", "aa-1", "activity-1"),
            _scheme_row("child-2", "aa-2", "activity-2", pos=1),
        ],
    )
    parents, parent_index = load_admin_approval_with_index(
        aa_path, _spec("AA", "aa.csv"), chunksize=1
    )
    children = load_admin_approval_scheme(
        child_path,
        _spec("AA", "aa-scheme.csv"),
        parent_index=parent_index,
        chunksize=1,
    )

    assert len(parents) == len(children) == 2
    assert set(children["parent_row_id"]) == {"aa-1", "aa-2"}
    assert list(children["source_record_id"]) == ["aa-1", "aa-2"]
    assert list(children["row_id"]) == ["child-1", "child-2"]
    assert list(children["pos"]) == [0, 1]
    assert children.loc[0, "fund_sanctioned_sc"] == Decimal("0")
    assert children.loc[0, "fund_sanctioned_st"] is None


def test_child_orphan_parent_is_typed_and_can_be_quarantined(tmp_path: Path):
    path = tmp_path / "aa-scheme.csv"
    _write_csv(
        path,
        AA_SCHEME_SOURCE_COLUMNS,
        [_scheme_row("child-1", "missing-parent", "activity-1")],
    )
    with pytest.raises(OrphanReferenceError, match="parent_row_id"):
        load_admin_approval_scheme(
            path,
            _spec("AA", "aa-scheme.csv"),
            parent_index=ApprovalParentIndex(frozenset(), {}),
            chunksize=1,
        )

    audit = LoaderAudit()
    frame = load_admin_approval_scheme(
        path,
        _spec("AA", "aa-scheme.csv"),
        parent_index=ApprovalParentIndex(frozenset(), {}),
        chunksize=1,
        audit=audit,
        on_error="quarantine",
    )
    assert frame.empty
    assert audit.quarantined[0].reason_code == "orphan_parent_row_id"


def test_child_parent_activity_mismatch_is_typed(tmp_path: Path):
    path = tmp_path / "aa-scheme.csv"
    _write_csv(
        path,
        AA_SCHEME_SOURCE_COLUMNS,
        [_scheme_row("child-1", "aa-1", "wrong-activity")],
    )
    with pytest.raises(SemanticValidationError, match="does not match"):
        load_admin_approval_scheme(
            path,
            _spec("AA", "aa-scheme.csv"),
            parent_index={"aa-1": "activity-1"},
        )


def test_date_parser_rejects_day_first_iso_mistake(tmp_path: Path):
    path = tmp_path / "bad-date.csv"
    _write_csv(
        path,
        AA_SOURCE_COLUMNS,
        [_aa_row("aa-1", "activity-1", date="19/02/2026")],
    )
    with pytest.raises(DateParseError, match="source blanks=0"):
        load_admin_approval(path, _spec("AA", "bad-date.csv"))


def test_source_year_requires_four_digits(tmp_path: Path):
    path = tmp_path / "bad-year.csv"
    _write_csv(path, AA_SOURCE_COLUMNS, [_aa_row("aa-1", "activity-1", year="2025-26")])
    with pytest.raises(SourceYearError, match="YYYY"):
        load_admin_approval(path, _spec("AA", "bad-year.csv"))


def test_ta_preserves_r_n_and_nr_semantics(tmp_path: Path):
    path = tmp_path / "ta.csv"
    _write_csv(
        path,
        TA_SOURCE_COLUMNS,
        [
            _ta_row("ta-1", "activity-1", required="R", cost="2.50"),
            _ta_row(
                "ta-2",
                "activity-2",
                required="N",
                cost="NR",
                authority="NR",
                order_no="NR",
                date="NR",
            ),
        ],
    )
    frame = load_technical_approval(
        path,
        _spec("TA", "ta.csv"),
        activity_codes={"activity-1", "activity-2"},
        chunksize=1,
    )

    assert list(frame["tec_approval_required"]) == ["R", "N"]
    assert frame.loc[0, "tec_approval_cost"] == Decimal("2.50")
    assert frame.loc[1, "tec_approval_cost"] is None
    assert frame.loc[1, "tec_approval_authority"] == "NR"
    assert frame.loc[1, "tec_approval_order_no"] == "NR"
    assert pd.isna(frame.loc[1, "tec_approval_order_date"])


def test_ta_rejects_cost_for_not_required_rows(tmp_path: Path):
    path = tmp_path / "ta-invalid.csv"
    _write_csv(
        path,
        TA_SOURCE_COLUMNS,
        [
            _ta_row(
                "ta-1",
                "activity-1",
                required="N",
                authority="NR",
                order_no="NR",
                date="NR",
            )
        ],
    )
    with pytest.raises(SemanticValidationError, match="must be null"):
        load_technical_approval(path, _spec("TA", "ta-invalid.csv"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("authority", "issuer", "must be NR"),
        ("order_no", "007", "must be NR"),
        ("date", "2026-02-20", "must be NR or blank"),
    ],
)
def test_ta_rejects_mixed_not_required_sentinels(
    tmp_path: Path, field: str, value: str, message: str
):
    path = tmp_path / f"ta-invalid-{field}.csv"
    kwargs: dict[str, str] = {
        "required": "N",
        "cost": "NR",
        "authority": "NR",
        "order_no": "NR",
        "date": "NR",
    }
    kwargs[field] = value
    _write_csv(path, TA_SOURCE_COLUMNS, [_ta_row("ta-1", "activity-1", **kwargs)])
    with pytest.raises(SemanticValidationError, match=message):
        load_technical_approval(path, _spec("TA", path.name))


def test_orphan_activity_is_typed_for_ta(tmp_path: Path):
    path = tmp_path / "ta-orphan.csv"
    _write_csv(path, TA_SOURCE_COLUMNS, [_ta_row("ta-1", "not-loaded")])
    with pytest.raises(OrphanReferenceError, match="planned_activity"):
        load_technical_approval(
            path, _spec("TA", "ta-orphan.csv"), activity_codes={"a1"}
        )


def test_pp_preserves_raw_multicoordinates_and_bad_outlier(tmp_path: Path):
    path = tmp_path / "pp.csv"
    _write_csv(
        path,
        PP_UPLOAD_SOURCE_COLUMNS,
        [
            _pp_row(
                "pp-1",
                "activity-1",
                longitude="-38.3796994,-38.2",
                latitude="-12.8433276,-12.7",
            ),
        ],
    )
    frame = load_physical_progress(
        path,
        _spec("PP", "pp.csv"),
        activity_codes={"activity-1"},
        chunksize=1,
    )

    assert list(frame["n_coords"]) == [2]
    assert frame.loc[0, "latitude"] == pytest.approx(-12.8433276)
    assert frame.loc[0, "longitude"] == pytest.approx(-38.3796994)
    assert frame.loc[0, "latitude_raw"] == "-12.8433276,-12.7"
    assert frame.loc[0, "longitude_raw"] == "-38.3796994,-38.2"
    assert frame.loc[0, "file_upload_id"] == "000315"
    assert frame.loc[0, "plan_unit_type_code"] == "2"


def test_pp_raw_coordinate_text_is_not_trimmed(tmp_path: Path):
    path = tmp_path / "pp-raw-coordinate-text.csv"
    _write_csv(
        path,
        PP_UPLOAD_SOURCE_COLUMNS,
        [
            _pp_row(
                "pp-1",
                "activity-1",
                longitude=" 85.1, 85.2 ",
                latitude=" 20.1, 20.2 ",
            )
        ],
    )
    frame = load_physical_progress(
        path,
        _spec("PP", "pp-raw-coordinate-text.csv"),
        activity_codes={"activity-1"},
    )
    assert frame.loc[0, "longitude_raw"] == " 85.1, 85.2 "
    assert frame.loc[0, "latitude_raw"] == " 20.1, 20.2 "
    assert frame.loc[0, "longitude"] == pytest.approx(85.1)
    assert frame.loc[0, "latitude"] == pytest.approx(20.1)


def test_pp_repeated_activity_rows_are_preserved(tmp_path: Path):
    path = tmp_path / "pp-repeated-activity.csv"
    _write_csv(
        path,
        PP_UPLOAD_SOURCE_COLUMNS,
        [_pp_row("pp-1", "activity-1"), _pp_row("pp-2", "activity-1", pos=1)],
    )
    frame = load_physical_progress(
        path,
        _spec("PP", "pp-repeated-activity.csv"),
        activity_codes={"activity-1"},
        chunksize=1,
    )
    assert len(frame) == 2
    assert list(frame["activity_code"]) == ["activity-1", "activity-1"]


def test_pp_child_parent_and_position_provenance_are_preserved(tmp_path: Path):
    path = tmp_path / "pp-lineage.csv"
    _write_csv(
        path,
        PP_UPLOAD_SOURCE_COLUMNS,
        [_pp_row("pp-1", "activity-1", parent="stage-9", pos=3)],
    )
    frame = load_physical_progress(
        path, _spec("PP", "pp-lineage.csv"), activity_codes={"activity-1"}
    )
    row = frame.iloc[0]
    assert row["row_id"] == "pp-1"
    assert row["parent_row_id"] == "stage-9"
    assert row["source_record_id"] == "stage-9"
    assert row["pos"] == 3
    assert row["source_system"] == "egramswaraj"
    assert row["source_run_id"] == "synthetic-run-1"
    assert row["schema_version"] == "47-test-v1"
    assert row["source_kind"] == "PP"


def test_child_row_id_cannot_collide_with_parent_id(tmp_path: Path):
    path = tmp_path / "aa-scheme-parent-collision.csv"
    _write_csv(
        path,
        AA_SCHEME_SOURCE_COLUMNS,
        [_scheme_row("aa-1", "aa-1", "activity-1")],
    )
    with pytest.raises(DuplicateKeyError, match="row_id"):
        load_admin_approval_scheme(
            path,
            _spec("AA", "aa-scheme-parent-collision.csv"),
            parent_index={"aa-1": "activity-1"},
        )


def test_pp_coordinate_count_mismatch_fails_closed(tmp_path: Path):
    path = tmp_path / "pp-mismatch.csv"
    _write_csv(
        path,
        PP_UPLOAD_SOURCE_COLUMNS,
        [_pp_row("pp-1", "activity-1", longitude="85.1,85.2", latitude="20.1")],
    )
    with pytest.raises(CoordinateMismatchError, match="captures"):
        load_physical_progress(
            path, _spec("PP", "pp-mismatch.csv"), activity_codes={"activity-1"}
        )


def test_pp_non_numeric_first_coordinate_is_typed(tmp_path: Path):
    path = tmp_path / "pp-bad-coordinate.csv"
    _write_csv(
        path,
        PP_UPLOAD_SOURCE_COLUMNS,
        [_pp_row("pp-1", "activity-1", longitude="not-number", latitude="20.1")],
    )
    with pytest.raises(CoordinateParseError, match="not numeric"):
        load_physical_progress(
            path, _spec("PP", "pp-bad-coordinate.csv"), activity_codes={"activity-1"}
        )


def test_stage_progress_schema_is_not_accepted(tmp_path: Path):
    path = tmp_path / "pp-stage.csv"
    _write_csv(
        path,
        (
            "row_id",
            "parent_row_id",
            "pos",
            "activityCd",
            "physclPrgrssAstStgCd",
            "stage",
            "cmpltnDt",
        ),
        [{"row_id": "stage-1", "parent_row_id": "a1", "pos": 0, "activityCd": "a1"}],
    )
    with pytest.raises(CsvSchemaError, match="schema mismatch"):
        load_physical_progress(path, _spec("PP", "pp-stage.csv"))


def test_chunk_size_does_not_change_deterministic_output(tmp_path: Path):
    path = tmp_path / "aa-invariance.csv"
    _write_csv(
        path,
        AA_SOURCE_COLUMNS,
        [_aa_row(f"aa-{i}", f"activity-{i}", approval_no="1") for i in range(5)],
    )
    one = load_admin_approval(path, _spec("AA", "aa-invariance.csv"), chunksize=1)
    three = load_admin_approval(path, _spec("AA", "aa-invariance.csv"), chunksize=3)
    pd.testing.assert_frame_equal(one, three)


def test_iterator_exposes_bounded_chunks_and_global_audit(tmp_path: Path):
    path = tmp_path / "aa-stream.csv"
    _write_csv(
        path,
        AA_SOURCE_COLUMNS,
        [_aa_row(f"aa-{i}", f"activity-{i}") for i in range(5)],
    )
    audit = LoaderAudit()
    chunks = list(
        iter_admin_approval(
            path,
            _spec("AA", "aa-stream.csv"),
            chunksize=2,
            audit=audit,
        )
    )
    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
    assert audit.rows_read == audit.rows_loaded == 5
    assert len(audit.row_ids) == 5


def test_empty_child_with_valid_parent_index_has_stable_columns(tmp_path: Path):
    path = tmp_path / "empty-child.csv"
    _write_csv(path, AA_SCHEME_SOURCE_COLUMNS, [])
    frame = load_admin_approval_scheme(
        path,
        _spec("AA", "empty-child.csv"),
        parent_index=ApprovalParentIndex(frozenset(), {}),
    )
    assert list(frame.columns) == list(ADMIN_APPROVAL_SCHEME_COLUMNS)
    assert frame.empty
