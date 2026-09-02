"""Synthetic tests for the strict shared source-loader utilities."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from pandas._libs.missing import NAType
import pytest

from warehouse.load_common import (
    CsvSchemaError,
    DateParseError,
    FiscalYearError,
    IdentifierError,
    MoneyParseError,
    ProvenanceError,
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


@pytest.mark.parametrize(
    ("name", "records", "bad_record_number"),
    [
        (
            "first-data-row",
            ["0123,a,EXTRA", "1,b"],
            2,
        ),
        (
            "first-row-of-next-chunk",
            ["1,a", "2,b", "0123,a,EXTRA", "3,c"],
            4,
        ),
    ],
)
def test_csv_rejects_extra_fields_before_any_chunk_is_yielded(
    tmp_path: Path, name: str, records: list[str], bad_record_number: int
):
    path = tmp_path / f"{name}.csv"
    path.write_text("id,value\n" + "\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(
        CsvSchemaError,
        match=rf"row {bad_record_number} has 3 fields; expected 2",
    ):
        list(read_csv_chunks(path, chunksize=2))


def test_csv_chunking_preserves_quoted_embedded_newlines(tmp_path: Path):
    path = tmp_path / "quoted-newline-chunks.csv"
    path.write_text(
        'id,value\n0123,"first\nrecord"\n0124,second\n', encoding="utf-8"
    )

    chunks = list(read_csv_chunks(path, chunksize=1))

    assert [row for chunk in chunks for row in chunk.to_dict("records")] == [
        {"id": "0123", "value": "first\nrecord"},
        {"id": "0124", "value": "second"},
    ]


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


def test_iso_date_requires_zero_padded_month_and_day():
    source = pd.Series(["2021-1-02", "2021-01-2"])

    with pytest.raises(DateParseError, match="source blanks=0"):
        parse_date_series(source, date_format="%Y-%m-%d", column="approval_date")


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


@pytest.mark.parametrize(
    "value",
    ["1.001", Decimal("1.001"), "-0.001"],
)
def test_money_with_more_fractional_digits_fails_closed(value: Decimal | Literal['1.001'] | Literal['-0.001']):
    with pytest.raises(MoneyParseError, match="fractional digits"):
        parse_money(value, column="amount", places=2)


@pytest.mark.parametrize(
    "value",
    [
        Decimal("sNaN"),
        Decimal("NaN"),
        Decimal("Infinity"),
        float("nan"),
        float("inf"),
        np.float32("nan"),
    ],
)
def test_nonfinite_money_values_raise_typed_error(value: Decimal | float | np.floating):
    with pytest.raises(MoneyParseError):
        parse_money(value, column="amount")


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


@pytest.mark.parametrize(
    "value", ["", "nan", "NaN", "none", "NULL", "<NA>", None, pd.NA]
)
def test_identifier_missing_sentinels_normalize_to_none(value: None | NAType | Literal[''] | Literal['nan'] | Literal['NaN'] | Literal['none'] | Literal['NULL'] | Literal['<NA>']):
    assert clean_identifier(value) is None


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), np.float32("nan"), Decimal("NaN")]
)
def test_nonfinite_numeric_identifier_raises_typed_error(value: float | np.floating | Decimal):
    with pytest.raises(IdentifierError):
        clean_identifier(value)


@pytest.mark.parametrize("value", [True, False, np.bool_(True), np.bool_(False)])
def test_boolean_identifiers_are_rejected_for_python_and_numpy_scalars(value: np.bool[Literal[True]] | np.bool[Literal[False]] | bool):
    with pytest.raises(IdentifierError, match="boolean"):
        clean_identifier(value)


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


def test_child_provenance_has_distinct_row_id_but_root_source_record_id():
    root = row_provenance(
        source_system="egramswaraj",
        source_run_id="run-1",
        source_file="PL.json",
        source_row_number=3,
        source_kind="PL",
        gp_code="0123",
        fiscal_year="2021-2022",
    )
    child = row_provenance(
        source_system="egramswaraj",
        source_run_id="run-1",
        source_file="PL.json",
        source_row_number=3,
        source_kind="PL",
        gp_code="0123",
        fiscal_year="2021-2022",
        parent_row_id=root["row_id"],
        position=0,
        child_collection="funds",
    )

    assert child["row_id"] != root["row_id"]
    assert child["parent_row_id"] == root["row_id"]
    assert child["source_record_id"] == root["source_record_id"]


def test_row_id_is_stable_when_the_same_row_is_replayed_under_a_new_run_id():
    """A rerun with a new run ID must not mint a new identity for the same row.

    This mirrors the canonical invariant already exercised by
    ``tests/test_normalize.py::
    test_existing_snapshot_is_immutable_and_row_ids_are_cross_run_stable``:
    ``source_run_id`` is provenance metadata, not part of a row's identity.
    """

    kwargs = {
        "source_system": "egramswaraj",
        "source_file": "2021-2022/PL.csv",
        "source_row_number": 7,
        "source_kind": "PL",
        "gp_code": "0123",
        "fiscal_year": "2021-2022",
    }
    first_run = deterministic_provenance_id(source_run_id="run-1", **kwargs)
    replay_run = deterministic_provenance_id(source_run_id="run-2", **kwargs)
    assert first_run == replay_run

    first_row = row_provenance(source_run_id="run-1", **kwargs)
    replay_row = row_provenance(source_run_id="run-2", **kwargs)
    assert first_row["row_id"] == replay_row["row_id"]
    assert first_row["source_record_id"] == replay_row["source_record_id"]
    # The run ID is still recorded as metadata even though it no longer
    # affects identity.
    assert first_row["source_run_id"] == "run-1"
    assert replay_row["source_run_id"] == "run-2"


def test_child_row_ids_differ_across_sibling_collections_at_the_same_position():
    """Position 0 of two different child collections must not collide.

    Mirrors ``src/pipeline/normalize.py::_child_rows``, which folds the
    sanitized collection key into the child row ID alongside position.
    """

    root = row_provenance(
        source_system="egramswaraj",
        source_run_id="run-1",
        source_file="PL.json",
        source_row_number=3,
        source_kind="PL",
        gp_code="0123",
        fiscal_year="2021-2022",
    )
    fund_child = row_provenance(
        source_system="egramswaraj",
        source_run_id="run-1",
        source_file="PL.json",
        source_row_number=3,
        source_kind="PL",
        gp_code="0123",
        fiscal_year="2021-2022",
        parent_row_id=root["row_id"],
        position=0,
        child_collection="funds",
    )
    asset_child = row_provenance(
        source_system="egramswaraj",
        source_run_id="run-1",
        source_file="PL.json",
        source_row_number=3,
        source_kind="PL",
        gp_code="0123",
        fiscal_year="2021-2022",
        parent_row_id=root["row_id"],
        position=0,
        child_collection="assets",
    )

    assert fund_child["row_id"] != asset_child["row_id"]


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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_system": "", "source_run_id": "run", "source_file": "PL.csv"},
        {"source_system": "src", "source_run_id": pd.NA, "source_file": "PL.csv"},
        {"source_system": "src", "source_run_id": "run", "source_file": ""},
        {
            "source_system": "src",
            "source_run_id": "run",
            "source_file": "PL.csv",
            "schema_version": pd.NA,
        },
        {
            "source_system": "src",
            "source_run_id": "run",
            "source_file": "PL.csv",
            "source_kind": "",
        },
    ],
)
def test_invalid_provenance_spec_fails_even_before_empty_frame_processing(kwargs: dict[str, str] | dict[str, str | NAType]):
    with pytest.raises(ProvenanceError):
        ProvenanceSpec(**kwargs)


def test_provenance_normalizes_nullable_optional_fields_without_raw_type_error():
    row = row_provenance(
        source_system="src",
        source_run_id="run",
        source_file="PL.csv",
        source_row_number=1,
        source_kind="PL",
        gp_code=pd.NA,
        gp_name=pd.NA,
        fiscal_year=pd.NA,
        business_id=pd.NA,
        parent_row_id=pd.NA,
        position=pd.NA,
    )
    assert row["gp_code"] is None
    assert row["gram_panchayat_name"] is None
    assert row["fiscal_year"] is None
    assert row["parent_row_id"] is None
    assert row["pos"] is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Indian grouping. 1,00,000 is one lakh -- the dominant format in the
        # source data, and the case a three-digit-only validator would reject.
        ("1,00,000", Decimal("100000")),
        ("12,34,567", Decimal("1234567")),
        ("1,23,45,678", Decimal("12345678")),
        ("1,00,000.50", Decimal("100000.50")),
        ("-1,00,000", Decimal("-100000")),
        ("Rs 1,00,000", Decimal("100000")),
        # International grouping, still accepted.
        ("1,000", Decimal("1000")),
        ("12,345,678", Decimal("12345678")),
        # Ungrouped values never reach the grouping check.
        ("100000", Decimal("100000")),
        ("1234.5", Decimal("1234.5")),
    ],
)
def test_supported_digit_grouping_is_parsed(text: str, expected: Decimal) -> None:
    assert parse_money(text, column="amount") == expected


@pytest.mark.parametrize("text", ["1,2", "12,,34", "1,0000", "1,00,00", ",100", "100,"])
def test_malformed_digit_grouping_raises_instead_of_corrupting(text: str) -> None:
    """Stripping commas unconditionally turned these into plausible numbers.

    "1,2" parsed as 12 and "12,,34" as 1234 -- a corrupted expenditure amount
    reaching the warehouse with no error, which is exactly what this helper
    promises cannot happen.
    """
    with pytest.raises(MoneyParseError):
        parse_money(text, column="amount")

@pytest.mark.parametrize("text", ["₹", "Rs.", "Rs ", "INR "])
def test_bare_currency_prefix_raises_even_with_allow_null(text: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_money(text, column="amount", allow_null=True)


@pytest.mark.parametrize("text", ["", "NA", "-"])
def test_genuine_blank_still_returns_none_under_allow_null(text: str) -> None:
    assert parse_money(text, column="amount", allow_null=True) is None


def test_none_still_returns_none_under_allow_null() -> None:
    assert parse_money(None, column="amount", allow_null=True) is None