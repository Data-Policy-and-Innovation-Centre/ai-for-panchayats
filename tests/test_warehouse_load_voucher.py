"""Synthetic contract tests for the bounded voucher loader.

The real accounting extract is proprietary and is intentionally not used by
these tests.  Small CSV fixtures still exercise the failure modes that would
otherwise be easy to miss on a large, mostly-clean source.
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.warehouse.load_voucher import (
    VOUCHER_BATCH_COLUMNS,
    VOUCHER_SOURCE_COLUMNS,
    VoucherAggregateError,
    VoucherLoader,
    VoucherNaturalKeyError,
    VoucherSourceSchemaError,
)


def _voucher(
    *,
    gp: str = "GP-1",
    fiscal_year: str = "2021-2022",
    voucher_no: str = "V-1",
    voucher_id: str = "1",
    direction: str = "payment",
    amount: str = "10.00",
    month: str = "April",
    date: str = "02/03/2021",
    type_label: str = "Expenditures",
) -> dict[str, str]:
    return {
        "gp_name": f"Name {gp}",
        "gp_lgd_code": gp,
        "state": "Odisha",
        "district": "District",
        "block": "Block",
        "fiscal_year": fiscal_year,
        "year_status": "Closed",
        "direction": direction,
        "month": month,
        "date": date,
        "voucher_no": voucher_no,
        "type": type_label,
        "amount": amount,
        "voucher_id": voucher_id,
        # Filled by _with_annual_values so the fixture remains internally
        # consistent even when rows are added to a test.
        "year_receipt_count": "0",
        "year_payment_count": "0",
        "year_total_receipts": "0.00",
        "year_total_payments": "0.00",
    }


def _with_annual_values(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault((row["gp_lgd_code"], row["fiscal_year"]), []).append(row)
    for group_rows in groups.values():
        receipts = [row for row in group_rows if row["direction"] == "receipt"]
        payments = [row for row in group_rows if row["direction"] == "payment"]
        receipt_total = sum((Decimal(row["amount"]) for row in receipts), Decimal("0.00"))
        payment_total = sum((Decimal(row["amount"]) for row in payments), Decimal("0.00"))
        for row in group_rows:
            row["year_receipt_count"] = str(len(receipts))
            row["year_payment_count"] = str(len(payments))
            row["year_total_receipts"] = f"{receipt_total:.2f}"
            row["year_total_payments"] = f"{payment_total:.2f}"
    return rows


def _write_csv(
    tmp_path: Path, rows: list[dict[str, str]], *, with_annual_values: bool = True
) -> Path:
    path = tmp_path / "Accounting_vouchers_flat.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VOUCHER_SOURCE_COLUMNS)
        writer.writeheader()
        writer.writerows(_with_annual_values(rows) if with_annual_values else rows)
    return path


def _load(path: Path, *, chunk_size: int = 100) -> tuple[pd.DataFrame, object]:
    with VoucherLoader() as loader:
        batches = list(
            loader.load_flat_csv(
                path,
                source_system="egramswaraj",
                source_run_id="synthetic-run",
                chunk_size=chunk_size,
            )
        )
        frame = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
        report = loader.report
    return frame, report


def test_colliding_voucher_id_and_same_voucher_no_across_gps_get_distinct_pks(tmp_path: Path):
    path = _write_csv(
        tmp_path,
        [
            _voucher(gp="GP-1", voucher_no="V-1", voucher_id="99", amount="10.00"),
            _voucher(
                gp="GP-2",
                fiscal_year="2022-2023",
                voucher_no="V-1",
                voucher_id="99",
                direction="receipt",
                amount="5.00",
            ),
        ],
    )

    frame, report = _load(path, chunk_size=1)

    assert list(frame["voucher_id"]) == ["99", "99"]
    assert frame["voucher_pk"].nunique() == 2
    assert report.source_rows == report.natural_key_count == 2


def test_duplicate_natural_key_is_rejected_across_chunk_boundary(tmp_path: Path):
    path = _write_csv(
        tmp_path,
        [
            _voucher(voucher_no="same", voucher_id="1", amount="10.00"),
            _voucher(voucher_no="same", voucher_id="2", amount="20.00"),
        ],
    )

    with VoucherLoader() as loader:
        with pytest.raises(VoucherNaturalKeyError, match="duplicate voucher natural key"):
            list(
                loader.load_flat_csv(
                    path,
                    source_system="egramswaraj",
                    source_run_id="synthetic-run",
                    chunk_size=1,
                )
            )


def test_date_is_parsed_day_first_and_month_text_is_preserved(tmp_path: Path):
    path = _write_csv(
        tmp_path,
        [_voucher(date="02/03/2021", month="APRIL", amount="1.25")],
    )

    frame, _ = _load(path)

    assert frame.loc[0, "date"] == pd.Timestamp("2021-03-02")
    assert frame.loc[0, "date"] != pd.Timestamp("2021-02-03")
    assert frame.loc[0, "month"] == "APRIL"


def test_textual_month_abbreviation_is_preserved(tmp_path: Path):
    path = _write_csv(tmp_path, [_voucher(month="Sept")])

    frame, _ = _load(path)

    assert frame.loc[0, "month"] == "Sept"


@pytest.mark.parametrize("amount", ["1.234", "NaN", "inf", "-inf"])
def test_amount_scale_and_nonfinite_values_fail_closed(tmp_path: Path, amount: str):
    rows = _with_annual_values([_voucher(amount="0.00")])
    rows[0]["amount"] = amount
    path = _write_csv(tmp_path, rows, with_annual_values=False)

    with VoucherLoader() as loader:
        with pytest.raises(VoucherSourceSchemaError, match="amount"):
            list(
                loader.load_flat_csv(
                    path,
                    source_system="egramswaraj",
                    source_run_id="synthetic-run",
                )
            )


def test_amount_larger_than_decimal_16_2_is_rejected(tmp_path: Path):
    path = _write_csv(tmp_path, [_voucher(amount="100000000000000.00")])

    with VoucherLoader() as loader:
        with pytest.raises(VoucherSourceSchemaError, match=r"DECIMAL\(16,2\)"):
            list(
                loader.load_flat_csv(
                    path,
                    source_system="egramswaraj",
                    source_run_id="synthetic-run",
                )
            )


def test_non_textual_month_is_rejected(tmp_path: Path):
    path = _write_csv(tmp_path, [_voucher(month="04")])

    with VoucherLoader() as loader:
        with pytest.raises(VoucherSourceSchemaError, match="month"):
            list(
                loader.load_flat_csv(
                    path,
                    source_system="egramswaraj",
                    source_run_id="synthetic-run",
                )
            )


def test_repeated_annual_aggregate_must_be_consistent(tmp_path: Path):
    rows = _with_annual_values(
        [
            _voucher(voucher_no="V-1", amount="10.00"),
            _voucher(voucher_no="V-2", amount="20.00"),
        ]
    )
    rows[1]["year_total_payments"] = "999.00"
    path = _write_csv(tmp_path, rows, with_annual_values=False)

    with VoucherLoader() as loader:
        with pytest.raises(VoucherAggregateError, match="inconsistent repeated annual aggregate"):
            list(
                loader.load_flat_csv(
                    path,
                    source_system="egramswaraj",
                    source_run_id="synthetic-run",
                    chunk_size=1,
                )
            )


def test_reversal_amounts_remain_signed_and_are_not_reclassified(tmp_path: Path):
    path = _write_csv(
        tmp_path,
        [
            _voucher(voucher_no="V-1", amount="100.00"),
            _voucher(
                voucher_no="V-2",
                amount="-25.00",
                voucher_id="2",
                type_label="Refund of Excess Payment",
            ),
        ],
    )

    frame, report = _load(path, chunk_size=1)

    assert list(frame.sort_values("voucher_no")["amount"]) == [
        Decimal("100.00"),
        Decimal("-25.00"),
    ]
    assert report.total_amount == Decimal("75.00")
    assert dict(report.direction_amounts)["payment"] == Decimal("75.00")


def test_output_batches_and_surrogate_keys_are_chunk_size_invariant(tmp_path: Path):
    rows = [
        _voucher(gp="GP-2", voucher_no="V-2", voucher_id="2", amount="20.00"),
        _voucher(gp="GP-1", voucher_no="V-3", voucher_id="3", amount="30.00"),
        _voucher(gp="GP-1", voucher_no="V-1", voucher_id="1", amount="10.00"),
        _voucher(gp="GP-2", voucher_no="V-1", voucher_id="1", amount="40.00"),
    ]
    path = _write_csv(tmp_path, rows)

    one, _ = _load(path, chunk_size=1)
    two, _ = _load(path, chunk_size=2)
    all_rows, _ = _load(path, chunk_size=100)

    key_columns = ["gp_lgd_code", "fiscal_year", "voucher_no"]
    compare_columns = [*key_columns, "voucher_pk", "source_record_id"]
    one = one.sort_values(key_columns)[compare_columns].reset_index(drop=True)
    two = two.sort_values(key_columns)[compare_columns].reset_index(drop=True)
    all_rows = all_rows.sort_values(key_columns)[compare_columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(one, two)
    pd.testing.assert_frame_equal(one, all_rows)


def test_provenance_is_present_and_annual_columns_are_omitted(tmp_path: Path):
    path = _write_csv(tmp_path, [_voucher()])

    frame, _ = _load(path)

    assert tuple(frame.columns) == VOUCHER_BATCH_COLUMNS
    assert frame.loc[0, "source_system"] == "egramswaraj"
    assert frame.loc[0, "source_run_id"] == "synthetic-run"
    assert frame.loc[0, "source_file"].endswith("Accounting_vouchers_flat.csv")
    assert frame.loc[0, "source_record_id"]
    assert not set(
        ("year_receipt_count", "year_payment_count", "year_total_receipts", "year_total_payments")
    ).intersection(frame.columns)


def test_nested_revision_is_explicit_and_streamed_separately(tmp_path: Path):
    path = tmp_path / "GP-1_2021-2022.json"
    payload = {
        "gp_lgd_code": "0012",
        "gp_name": "Synthetic GP",
        "years": {
            "2021-2022": {
                "payment_count": 1,
                "receipt_count": 1,
                "total_payments": "10.25",
                "total_receipts": "2.50",
                "payments": [
                    {
                        "amount": "10.25",
                        "date": "02/03/2021",
                        "month": "March",
                        "type": "Expenditures",
                        "voucher_id": "7",
                        "voucher_no": "P-1",
                    }
                ],
                "receipts": [
                    {
                        "amount": "2.50",
                        "date": "03/03/2021",
                        "month": "Mar",
                        "type": "Direct Receipts",
                        "voucher_id": "7",
                        "voucher_no": "R-1",
                    }
                ],
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with VoucherLoader() as loader:
        batches = list(
            loader.load_nested_json(
                [path],
                source_system="egramswaraj",
                source_run_id="nested-run",
                batch_size=1,
            )
        )
        report = loader.report

    frame = pd.concat(batches, ignore_index=True)
    assert len(batches) == 2
    assert frame["voucher_pk"].nunique() == 2
    assert set(frame["direction"]) == {"payment", "receipt"}
    assert set(frame["voucher_id"]) == {"7"}
    assert set(frame["source_revision"]) == {"nested-json-v1"}
    assert set(frame["source_file"]) == {path.name}
    assert report.total_amount == Decimal("12.75")


def test_nested_payment_and_receipt_at_same_index_get_distinct_provenance(tmp_path: Path):
    # Same GP/fiscal-year, both arrays have an item at index zero: the
    # payment and receipt rows must not collide on source_record_id even
    # though their raw within-array position is identical.
    path = tmp_path / "GP-1_2021-2022.json"
    payload = {
        "gp_lgd_code": "0012",
        "gp_name": "Synthetic GP",
        "years": {
            "2021-2022": {
                "payment_count": 1,
                "receipt_count": 1,
                "total_payments": "10.25",
                "total_receipts": "2.50",
                "payments": [
                    {
                        "amount": "10.25",
                        "date": "02/03/2021",
                        "month": "March",
                        "type": "Expenditures",
                        "voucher_id": "7",
                        "voucher_no": "P-1",
                    }
                ],
                "receipts": [
                    {
                        "amount": "2.50",
                        "date": "03/03/2021",
                        "month": "Mar",
                        "type": "Direct Receipts",
                        "voucher_id": "7",
                        "voucher_no": "R-1",
                    }
                ],
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with VoucherLoader() as loader:
        batches = list(
            loader.load_nested_json(
                [path],
                source_system="egramswaraj",
                source_run_id="nested-run",
                batch_size=100,
            )
        )

    frame = pd.concat(batches, ignore_index=True)
    payment_id = frame.loc[frame["direction"] == "payment", "source_record_id"].iloc[0]
    receipt_id = frame.loc[frame["direction"] == "receipt", "source_record_id"].iloc[0]
    assert payment_id != receipt_id


def test_nested_revision_rejects_annual_total_mismatch(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "gp_lgd_code": "0012",
                "year": "2021-2022",
                "payment_count": 1,
                "receipt_count": 0,
                "total_payments": "999.00",
                "total_receipts": "0.00",
                "payments": [
                    {
                        "amount": "10.00",
                        "date": "02/03/2021",
                        "month": "March",
                        "type": "Expenditures",
                        "voucher_id": "1",
                        "voucher_no": "P-1",
                    }
                ],
                "receipts": [],
            }
        ),
        encoding="utf-8",
    )

    with VoucherLoader() as loader:
        with pytest.raises(VoucherAggregateError, match="payment total"):
            list(
                loader.load_nested_json(
                    [path],
                    source_system="egramswaraj",
                    source_run_id="nested-run",
                )
            )
