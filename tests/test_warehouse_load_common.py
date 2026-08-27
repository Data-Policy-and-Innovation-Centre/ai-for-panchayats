"""Synthetic tests for the strict shared source-loader utilities."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from warehouse.load_common import (
    CsvSchemaError,
    DateParseError,
    FiscalYearError,
    MoneyParseError,
    ProvenanceSpec,
    add_provenance,
    clean_identifier,
    clean_identifier_series,
    deterministic_provenance_id,
    normalize_fiscal_year,
    normalize_fiscal_year_series,
    parse_date,
    parse_date_series,
    parse_money,
    parse_money_series,
    read_csv,
    read_csv_chunks,
    row_provenance,
    validate_columns,
)


def test_bom_safe_csv_preserves_header_and_identifier_text(tmp_path: Path):
    path = tmp_path / "with-bom.csv"
    path.write_bytes("\ufeffgp_code,amount\n0123,0.10\n0124,0\n".encode("utf-8"))

    frame = read_csv(path, required_columns=("gp_code", "amount"))

    assert list(frame["gp_code"]) == ["0123", "0124"]
    assert list(frame["amount"]) == ["0.10", "0"]


def test_csv_is_read_in_explicit_bounded_chunks(tmp_path: Path):
    path = tmp_path / "rows.csv"
    path.write_text("id,value\n1,a\n2,b\n3,c\n", encoding="utf-8")

    chunks = list(read_csv_chunks(path, required_columns=("id",), chunksize=2))

    assert [len(chunk) for chunk in chunks] == [2, 1]
    assert [value for chunk in chunks for value in chunk["id"]] == ["1", "2", "3"]


def test_missing_and_duplicate_schema_columns_fail_before_loading(tmp_path: Path):
    missing = tmp_path / "missing.csv"
    missing.write_text("gp_code,name\n0123,Test\n", encoding="utf-8")
    with pytest.raises(CsvSchemaError, match="missing required.*amount"):
        list(read_csv_chunks(missing, required_columns=("gp_code", "amount")))

    with pytest.raises(CsvSchemaError, match="duplicate"):
        validate_columns(("gp_code", "gp_code"), source="synthetic")


def test_exact_schema_rejects_unexpected_column():
    with pytest.raises(CsvSchemaError, match="unexpected"):
        validate_columns(
            ("gp_code", "name", "extra"),
            expected=("gp_code", "name"),
            source="synthetic",
        )


def test_iso_date_invalid_nonblank_trips_null_count_assertion():
    source = pd.Series(["2021-01-31", "", "31/01/2021"], name="approval_date")

    with pytest.raises(DateParseError) as error:
        parse_date_series(source, date_format="%Y-%m-%d", column="approval_date")

    assert error.value.column == "approval_date"
    assert error.value.source_blank_count == 1
    assert error.value.parsed_null_count == 2
    assert "source blanks=1" in str(error.value)
    assert "parsed nulls=2" in str(error.value)


def test_date_parser_requires_explicit_format_and_preserves_source_blanks():
    source = pd.Series(["31/01/2021", "", "01/02/2021"])
    parsed = parse_date(source, date_format="%d/%m/%Y", column="voucher_date")

    assert isinstance(parsed, pd.Series)
    assert parsed.iloc[0] == pd.Timestamp("2021-01-31")
    assert pd.isna(parsed.iloc[1])
    assert parsed.iloc[2] == pd.Timestamp("2021-02-01")

    with pytest.raises(DateParseError):
        parse_date(source, date_format="mixed", column="voucher_date")


def test_money_is_exact_decimal_and_absent_is_not_zero():
    source = pd.Series(["0.10", "0.20", "", "0"])
    parsed = parse_money_series(source, column="amount", places=2)

    assert parsed.iloc[0] == Decimal("0.10")
    assert parsed.iloc[0] + parsed.iloc[1] == Decimal("0.30")
    assert parsed.iloc[2] is None
    assert parsed.iloc[3] == Decimal("0.00")
    assert parsed.iloc[2] != parsed.iloc[3]


def test_unparseable_money_raises_instead_of_becoming_zero():
    with pytest.raises(MoneyParseError, match="amount"):
        parse_money("not-a-number", column="amount", places=2)

    # Parsing without a target scale remains lossless for planning-side input.
    assert parse_money("Rs. 1,25,000.5", column="planned_cost") == Decimal("125000.5")


def test_fiscal_year_short_form_and_nonconsecutive_year_fail():
    assert normalize_fiscal_year("2025-2026") == "2025-2026"
    with pytest.raises(FiscalYearError):
        normalize_fiscal_year("2025-26")
    with pytest.raises(FiscalYearError):
        normalize_fiscal_year("2025-2027")

    years = normalize_fiscal_year_series(pd.Series(["2021-2022", "", "2022-2023"]))
    assert list(years) == ["2021-2022", pd.NA, "2022-2023"]


def test_identifier_cleaning_preserves_leading_zeroes_and_strips_float_artifact():
    assert clean_identifier("0123") == "0123"
    assert clean_identifier(123.0) == "123"
    assert clean_identifier("0123.0") == "0123"
    assert clean_identifier(123.5) == "123.5"

    cleaned = clean_identifier_series(pd.Series(["0123", 123.0, None]))
    assert cleaned.iloc[0] == "0123"
    assert cleaned.iloc[1] == "123"
    assert pd.isna(cleaned.iloc[2])


def test_provenance_is_deterministic_and_matches_canonical_contract():
    kwargs = {
        "source_system": "egramswaraj",
        "source_run_id": "run-1",
        "source_file": "2021-2022/PL.csv",
        "source_row_number": 7,
        "source_kind": "PL",
        "gp_code": "0123",
        "fiscal_year": "2021-2022",
    }
    first = deterministic_provenance_id(**kwargs)
    second = deterministic_provenance_id(**kwargs)
    assert first == second
    assert first != deterministic_provenance_id(**{**kwargs, "source_row_number": 8})

    row = row_provenance(**kwargs, business_id="0007")
    assert row["row_id"] == row["source_record_id"] == first
    assert row["gp_code"] == "0123"
    assert row["business_id"] == "0007"
    assert row["source_file"] == "2021-2022/PL.csv"


def test_add_provenance_advances_source_rows_and_does_not_mutate_input():
    source = pd.DataFrame({"activity_code": ["0007", "0008"], "gp": ["0123", "0123"]})
    original = source.copy(deep=True)
    spec = ProvenanceSpec(
        source_system="egramswaraj",
        source_run_id="run-1",
        source_file="PL.csv",
        source_kind="PL",
        fiscal_year="2021-2022",
    )

    out = add_provenance(
        source,
        spec,
        start_row_number=10,
        business_id_column="activity_code",
        gp_code_column="gp",
    )

    assert source.equals(original)
    assert list(out["source_record_id"]) == [
        deterministic_provenance_id(
            source_system="egramswaraj",
            source_run_id="run-1",
            source_file="PL.csv",
            source_row_number=10,
            source_kind="PL",
            gp_code="0123",
            fiscal_year="2021-2022",
        ),
        deterministic_provenance_id(
            source_system="egramswaraj",
            source_run_id="run-1",
            source_file="PL.csv",
            source_row_number=11,
            source_kind="PL",
            gp_code="0123",
            fiscal_year="2021-2022",
        ),
    ]
    assert list(out["business_id"]) == ["0007", "0008"]


def test_add_provenance_empty_frame_still_declares_contract_columns():
    source = pd.DataFrame(columns=["activity_code"])
    out = add_provenance(
        source,
        ProvenanceSpec("egramswaraj", "run-1", "PL.csv", "PL"),
    )
    assert "row_id" in out.columns
    assert "source_record_id" in out.columns
