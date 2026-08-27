"""Strict, bounded-memory loading for the eGramSwaraj ``voucher`` table.

Issue #46 has two source revisions which must not be conflated:

* ``Accounting_vouchers_flat.csv`` is a derived, one-row-per-voucher CSV.
* The raw accounting extract is nested JSON, with independent ``payments``
  and ``receipts`` arrays per GP and fiscal year.

The two public entry points below deliberately keep those wire formats
separate.  Both produce the same warehouse-shaped columns, but neither falls
back from one revision to the other.  The loader is intentionally not wired
into ``warehouse.build`` yet; a later integration can consume the bounded
batch iterator and reindex away the provenance columns before inserting into
the current DDL.

There are two passes over a source.  The first pass validates every row and
stores only natural keys and GP/FY aggregate state in a disk-backed SQLite
database.  The second pass parses one chunk at a time and emits rows after a
SQL ``ROW_NUMBER`` assignment over the sorted natural keys.  Consequently:

* duplicate ``(gp_lgd_code, fiscal_year, voucher_no)`` keys are rejected
  globally, including when the duplicate crosses a chunk boundary;
* repeated annual aggregates are checked per GP/FY and are never summed once
  per voucher row; and
* integer ``voucher_pk`` values are deterministic and invariant to chunk size
  and source iteration order.

The SQLite state contains no source payload.  It is bounded by the number of
distinct voucher keys and GP/FY groups, not by the number of rows held in
Python memory.  A nested JSON source is read one file at a time because each
full-state file is already scoped to one GP and fiscal year; a complete
full-state tree is never loaded into one Python object.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from .load_common import (
    LoaderError,
    clean_identifier,
    deterministic_provenance_id,
    normalize_fiscal_year,
    parse_date,
    parse_date_series,
    parse_money,
    read_csv_chunks,
)


VOUCHER_SOURCE_COLUMNS: tuple[str, ...] = (
    "gp_name",
    "gp_lgd_code",
    "state",
    "district",
    "block",
    "fiscal_year",
    "year_status",
    "direction",
    "month",
    "date",
    "voucher_no",
    "type",
    "amount",
    "voucher_id",
    "year_receipt_count",
    "year_payment_count",
    "year_total_receipts",
    "year_total_payments",
)

VOUCHER_TARGET_COLUMNS: tuple[str, ...] = (
    "voucher_pk",
    "gp_lgd_code",
    "fiscal_year",
    "voucher_no",
    "voucher_id",
    "direction",
    "type",
    "date",
    "month",
    "amount",
)

# These are deliberately not target columns.  They make the batch auditable
# and allow a future caller to populate a separate GP dimension batch without
# ever using gp_name as a voucher key.
VOUCHER_PROVENANCE_COLUMNS: tuple[str, ...] = (
    "source_system",
    "source_run_id",
    "source_revision",
    "source_file",
    "source_row_number",
    "source_record_id",
    "gram_panchayat_name",
)
VOUCHER_BATCH_COLUMNS: tuple[str, ...] = (
    *VOUCHER_TARGET_COLUMNS,
    *VOUCHER_PROVENANCE_COLUMNS,
)

NESTED_VOUCHER_ROW_COLUMNS: tuple[str, ...] = (
    "amount",
    "date",
    "month",
    "type",
    "voucher_id",
    "voucher_no",
)

_DIRECTIONS = frozenset({"payment", "receipt"})
_COUNT_RE = re.compile(r"^[0-9]+$")
# DuckDB DECIMAL(16,2) has fourteen integer digits and two fractional digits.
# Rejecting an otherwise parseable larger Decimal here keeps a future insert
# from failing only after the loader has emitted a partial batch.
_DECIMAL_16_2_MAX = Decimal("99999999999999.99")


class VoucherLoaderError(LoaderError):
    """Base class for a voucher source-contract failure."""


class VoucherModeError(VoucherLoaderError):
    """The loader was asked to mix source revisions in one state namespace."""


class VoucherNaturalKeyError(VoucherLoaderError):
    """A voucher natural key is missing or duplicated."""


class VoucherAggregateError(VoucherLoaderError):
    """A GP/FY annual aggregate is inconsistent with its voucher rows."""


class VoucherSourceSchemaError(VoucherLoaderError):
    """A nested JSON source does not satisfy the documented shape."""


@dataclass(frozen=True, slots=True)
class VoucherLoadReport:
    """Aggregate facts produced by a completed validation pass."""

    source_system: str
    source_run_id: str
    source_revision: str
    source_rows: int
    natural_key_count: int
    aggregate_group_count: int
    payment_rows: int
    receipt_rows: int
    total_amount: Decimal
    direction_amounts: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True, slots=True)
class _VoucherInput:
    """One normalized voucher before its generated integer PK is attached."""

    gp_lgd_code: str
    gp_name: str | None
    fiscal_year: str
    voucher_no: str
    voucher_id: str | None
    direction: str
    type_label: str | None
    date: pd.Timestamp | None
    month: str
    amount: Decimal
    source_file: str
    source_row_number: int
    source_position: int | None
    # Annual values are retained only in the validation pass.  They are not
    # included in the target-shaped output.
    receipt_count: int
    payment_count: int
    total_receipts: Decimal
    total_payments: Decimal

    @property
    def natural_key(self) -> tuple[str, str, str]:
        return self.gp_lgd_code, self.fiscal_year, self.voucher_no


def _required_text(value: object, *, field: str, row: object | None = None) -> str:
    if value is None:
        location = f" at row {row!r}" if row is not None else ""
        raise VoucherSourceSchemaError(f"{field}{location} must be non-blank")
    text = str(value).strip()
    if not text:
        location = f" at row {row!r}" if row is not None else ""
        raise VoucherSourceSchemaError(f"{field}{location} must be non-blank")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_count(value: object, *, field: str, row: object | None = None) -> int:
    text = _required_text(value, field=field, row=row)
    if not _COUNT_RE.fullmatch(text):
        location = f" at row {row!r}" if row is not None else ""
        raise VoucherSourceSchemaError(
            f"{field}{location} must be a non-negative integer, got {value!r}"
        )
    return int(text)


def _parse_gp(value: object, *, row: object | None = None) -> str:
    try:
        result = clean_identifier(value, column="gp_lgd_code", row=row, allow_null=False)
    except LoaderError as exc:
        raise VoucherSourceSchemaError(str(exc)) from exc
    assert result is not None
    return result


def _parse_fiscal_year(value: object, *, row: object | None = None) -> str:
    try:
        result = normalize_fiscal_year(value, column="fiscal_year", row=row, allow_null=False)
    except LoaderError as exc:
        raise VoucherSourceSchemaError(str(exc)) from exc
    assert result is not None
    return result


def _parse_voucher_no(value: object, *, row: object | None = None) -> str:
    try:
        result = clean_identifier(value, column="voucher_no", row=row, allow_null=False)
    except LoaderError as exc:
        raise VoucherSourceSchemaError(str(exc)) from exc
    assert result is not None
    return result


def _parse_voucher_id(value: object, *, row: object | None = None) -> str | None:
    try:
        return clean_identifier(value, column="voucher_id", row=row, allow_null=True)
    except LoaderError as exc:
        raise VoucherSourceSchemaError(str(exc)) from exc


def _parse_direction(value: object, *, row: object | None = None) -> str:
    # The portal contract supplies these two lower-case values. Preserve
    # source semantics and fail closed on a differently-cased category.
    direction = _required_text(value, field="direction", row=row)
    if direction not in _DIRECTIONS:
        location = f" at row {row!r}" if row is not None else ""
        raise VoucherSourceSchemaError(
            f"direction{location} must be exactly 'payment' or 'receipt', got {value!r}"
        )
    return direction


def _parse_month(value: object, *, row: object | None = None) -> str:
    # Month is descriptive portal text, not a numeric month.  Preserve the
    # source spelling/case.  Do not impose a full-English-name vocabulary:
    # abbreviations and future localized labels remain valid text values.
    month = _required_text(value, field="month", row=row)
    if not any(character.isalpha() for character in month):
        location = f" at row {row!r}" if row is not None else ""
        raise VoucherSourceSchemaError(
            f"month{location} must be textual rather than numeric, got {value!r}"
        )
    return month


def _parse_money_field(
    value: object,
    *,
    column: str,
    row: object | None = None,
    enforce_decimal_16_2: bool = False,
) -> Decimal:
    """Parse a required money field and optionally enforce target precision."""

    try:
        parsed = parse_money(value, column=column, places=2, allow_null=False)
    except LoaderError as exc:
        location = f" at row {row!r}" if row is not None else ""
        raise VoucherSourceSchemaError(f"{column}{location}: {exc}") from exc
    assert isinstance(parsed, Decimal)
    if enforce_decimal_16_2 and abs(parsed) > _DECIMAL_16_2_MAX:
        location = f" at row {row!r}" if row is not None else ""
        raise VoucherSourceSchemaError(
            f"{column}{location}: value {value!r} does not fit DECIMAL(16,2)"
        )
    return parsed


def _parse_annual_values(
    row: Mapping[str, object], *, row_label: object | None = None
) -> tuple[int, int, Decimal, Decimal]:
    try:
        receipt_count = _parse_count(
            row.get("year_receipt_count"), field="year_receipt_count", row=row_label
        )
        payment_count = _parse_count(
            row.get("year_payment_count"), field="year_payment_count", row=row_label
        )
        total_receipts = _parse_money_field(
            row.get("year_total_receipts"),
            column="year_total_receipts",
            row=row_label,
        )
        total_payments = _parse_money_field(
            row.get("year_total_payments"),
            column="year_total_payments",
            row=row_label,
        )
    except LoaderError as exc:
        raise VoucherSourceSchemaError(str(exc)) from exc
    return receipt_count, payment_count, total_receipts, total_payments


def _parse_flat_chunk(
    frame: pd.DataFrame,
    *,
    source_file: str,
    source_row_offset: int,
) -> tuple[_VoucherInput, ...]:
    if frame.empty:
        return ()
    dates = parse_date_series(
        frame["date"], date_format="%d/%m/%Y", column="date", allow_null=True
    )
    records: list[_VoucherInput] = []
    for offset, (_, row) in enumerate(frame.iterrows()):
        logical_row = source_row_offset + offset + 2  # one-based CSV row after header
        fy = _parse_fiscal_year(row["fiscal_year"], row=logical_row)
        gp = _parse_gp(row["gp_lgd_code"], row=logical_row)
        voucher_no = _parse_voucher_no(row["voucher_no"], row=logical_row)
        direction = _parse_direction(row["direction"], row=logical_row)
        receipt_count, payment_count, total_receipts, total_payments = _parse_annual_values(
            row.to_dict(), row_label=logical_row
        )
        parsed_date = dates.iloc[offset]
        amount = _parse_money_field(
            row["amount"], column="amount", row=logical_row, enforce_decimal_16_2=True
        )
        source_position = None
        records.append(
            _VoucherInput(
                gp_lgd_code=gp,
                gp_name=_optional_text(row["gp_name"]),
                fiscal_year=fy,
                voucher_no=voucher_no,
                voucher_id=_parse_voucher_id(row["voucher_id"], row=logical_row),
                direction=direction,
                type_label=_optional_text(row["type"]),
                date=None if pd.isna(parsed_date) else parsed_date,
                month=_parse_month(row["month"], row=logical_row),
                amount=amount,
                source_file=source_file,
                source_row_number=logical_row,
                source_position=source_position,
                receipt_count=receipt_count,
                payment_count=payment_count,
                total_receipts=total_receipts,
                total_payments=total_payments,
            )
        )
    return tuple(records)


def _nested_root_years(payload: Mapping[str, object], *, source_file: str) -> tuple[tuple[str, Mapping[str, object]], ...]:
    years = payload.get("years")
    if years is not None:
        if not isinstance(years, Mapping) or not years:
            raise VoucherSourceSchemaError(
                f"{source_file}: 'years' must be a non-empty object"
            )
        invalid = tuple(str(year) for year, value in years.items() if not isinstance(value, Mapping))
        if invalid:
            raise VoucherSourceSchemaError(
                f"{source_file}: fiscal-year payload(s) are not objects: {invalid}"
            )
        return tuple((str(year), value) for year, value in years.items())
    year = payload.get("year")
    if year is None:
        raise VoucherSourceSchemaError(
            f"{source_file}: expected either a 'years' object or a root 'year'"
        )
    return ((str(year), payload),)


def _parse_nested_file(
    path: Path,
    *,
    annual_callback: Callable[[str, str, int, int, Decimal, Decimal], None] | None = None,
) -> Iterator[_VoucherInput]:
    # Store a portable source label rather than leaking an absolute Box or
    # workstation path into warehouse provenance.
    source_file = path.name
    try:
        # parse_float=Decimal is essential: the raw source uses JSON numeric
        # amounts, and loading them as native float would lose the exact
        # decimal representation before the shared money parser sees it.
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle, parse_float=Decimal)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VoucherSourceSchemaError(f"cannot read nested voucher JSON {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise VoucherSourceSchemaError(f"{source_file}: root must be an object")

    try:
        gp = _parse_gp(payload.get("gp_lgd_code"), row=source_file)
    except VoucherSourceSchemaError:
        raise
    gp_name = _optional_text(payload.get("gp_name"))
    for fiscal_year_raw, year_payload in _nested_root_years(payload, source_file=source_file):
        if not isinstance(year_payload, Mapping):
            raise VoucherSourceSchemaError(
                f"{source_file}: fiscal-year payload must be an object"
            )
        fy = _parse_fiscal_year(fiscal_year_raw, row=source_file)
        required_year_fields = (
            "payment_count",
            "receipt_count",
            "total_payments",
            "total_receipts",
            "payments",
            "receipts",
        )
        missing = tuple(field for field in required_year_fields if field not in year_payload)
        if missing:
            raise VoucherSourceSchemaError(
                f"{source_file} ({fy}): missing year field(s) {missing}"
            )
        payment_count = _parse_count(
            year_payload["payment_count"], field="payment_count", row=source_file
        )
        receipt_count = _parse_count(
            year_payload["receipt_count"], field="receipt_count", row=source_file
        )
        total_payments = _parse_money_field(
            year_payload["total_payments"],
            column="total_payments",
            row=source_file,
        )
        total_receipts = _parse_money_field(
            year_payload["total_receipts"],
            column="total_receipts",
            row=source_file,
        )
        arrays: list[tuple[str, str, list[object], int]] = []
        for direction, array_name, expected_fields in (
            ("receipt", "receipts", receipt_count),
            ("payment", "payments", payment_count),
        ):
            values = year_payload[array_name]
            if not isinstance(values, list):
                raise VoucherSourceSchemaError(
                    f"{source_file} ({fy}): {array_name} must be an array"
                )
            if len(values) != expected_fields:
                raise VoucherAggregateError(
                    f"{source_file} ({fy}): {array_name} has {len(values)} rows but "
                    f"declared count is {expected_fields}"
                )
            arrays.append((direction, array_name, values, expected_fields))
        if annual_callback is not None:
            annual_callback(
                gp,
                fy,
                receipt_count,
                payment_count,
                total_receipts,
                total_payments,
            )
        for direction, array_name, values, _ in arrays:
            for index, value in enumerate(values):
                if not isinstance(value, Mapping):
                    raise VoucherSourceSchemaError(
                        f"{source_file} ({fy}) {array_name}[{index}] must be an object"
                    )
                missing_row = tuple(field for field in NESTED_VOUCHER_ROW_COLUMNS if field not in value)
                unexpected_row = tuple(field for field in value if field not in NESTED_VOUCHER_ROW_COLUMNS)
                if missing_row or unexpected_row:
                    raise VoucherSourceSchemaError(
                        f"{source_file} ({fy}) {array_name}[{index}] schema mismatch: "
                        f"missing={missing_row}, unexpected={unexpected_row}"
                    )
                parsed_date = parse_date(
                    value["date"],
                    date_format="%d/%m/%Y",
                    column=f"{array_name}.date",
                    allow_null=True,
                )
                amount = _parse_money_field(
                    value["amount"],
                    column=f"{array_name}.amount",
                    row=index,
                    enforce_decimal_16_2=True,
                )
                yield _VoucherInput(
                    gp_lgd_code=gp,
                    gp_name=gp_name,
                    fiscal_year=fy,
                    voucher_no=_parse_voucher_no(value["voucher_no"], row=index),
                    voucher_id=_parse_voucher_id(value["voucher_id"], row=index),
                    direction=direction,
                    type_label=_optional_text(value["type"]),
                    date=parsed_date,
                    month=_parse_month(value["month"], row=index),
                    amount=amount,
                    source_file=source_file,
                    source_row_number=index + 2,
                    # Receipt/payment positions must differ even when both
                    # arrays have an item at index zero in the same file.
                    source_position=index * 2 + (0 if direction == "receipt" else 1),
                    receipt_count=receipt_count,
                    payment_count=payment_count,
                    total_receipts=total_receipts,
                    total_payments=total_payments,
                )


class _VoucherState:
    """Disk-backed key and aggregate state for one source revision."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        source_system: str,
        source_run_id: str,
        source_revision: str,
        pk_start: int,
    ) -> None:
        self.connection = connection
        self.source_system = source_system
        self.source_run_id = source_run_id
        self.source_revision = source_revision
        self.pk_start = pk_start
        self.source_rows = 0
        self.payment_rows = 0
        self.receipt_rows = 0
        self.total_amount = Decimal("0.00")
        self.direction_amounts = {"payment": Decimal("0.00"), "receipt": Decimal("0.00")}
        self._create_tables()
        existing_keys = self.connection.execute(
            "SELECT COUNT(*) FROM voucher_keys"
        ).fetchone()[0]
        existing_annual = self.connection.execute(
            "SELECT COUNT(*) FROM voucher_annual"
        ).fetchone()[0]
        if existing_keys or existing_annual:
            raise VoucherModeError(
                "state_path already contains voucher state; use a new state path "
                "for a separate source revision"
            )

    def _create_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS voucher_keys (
                gp_lgd_code TEXT NOT NULL,
                fiscal_year TEXT NOT NULL,
                voucher_no TEXT NOT NULL,
                voucher_pk INTEGER,
                PRIMARY KEY (gp_lgd_code, fiscal_year, voucher_no)
            );
            CREATE TABLE IF NOT EXISTS voucher_annual (
                gp_lgd_code TEXT NOT NULL,
                fiscal_year TEXT NOT NULL,
                receipt_count INTEGER NOT NULL,
                payment_count INTEGER NOT NULL,
                total_receipts TEXT NOT NULL,
                total_payments TEXT NOT NULL,
                actual_receipts INTEGER NOT NULL DEFAULT 0,
                actual_payments INTEGER NOT NULL DEFAULT 0,
                actual_total_receipts TEXT NOT NULL DEFAULT '0.00',
                actual_total_payments TEXT NOT NULL DEFAULT '0.00',
                PRIMARY KEY (gp_lgd_code, fiscal_year)
            );
            CREATE TABLE IF NOT EXISTS voucher_lookup (
                sequence_number INTEGER PRIMARY KEY,
                gp_lgd_code TEXT NOT NULL,
                fiscal_year TEXT NOT NULL,
                voucher_no TEXT NOT NULL
            );
            """
        )

    def commit(self) -> None:
        """Persist validation state at an explicit source batch boundary."""

        self.connection.commit()

    def declare_annual(
        self,
        gp_lgd_code: str,
        fiscal_year: str,
        receipt_count: int,
        payment_count: int,
        total_receipts: Decimal,
        total_payments: Decimal,
    ) -> None:
        """Register or compare one repeated GP/FY annual aggregate.

        Nested JSON permits a year with zero voucher rows.  Registering its
        aggregate before row iteration makes a non-zero declared total (or a
        conflicting repeat in another file) fail instead of disappearing
        merely because there is no row on which to carry the metadata.
        """

        declared = (
            receipt_count,
            payment_count,
            str(total_receipts),
            str(total_payments),
        )
        existing = self.connection.execute(
            "SELECT receipt_count, payment_count, total_receipts, total_payments "
            "FROM voucher_annual WHERE gp_lgd_code = ? AND fiscal_year = ?",
            (gp_lgd_code, fiscal_year),
        ).fetchone()
        if existing is None:
            self.connection.execute(
                "INSERT INTO voucher_annual("
                "gp_lgd_code, fiscal_year, receipt_count, payment_count, "
                "total_receipts, total_payments) VALUES (?, ?, ?, ?, ?, ?)",
                (gp_lgd_code, fiscal_year, *declared),
            )
        elif tuple(existing) != declared:
            raise VoucherAggregateError(
                "inconsistent repeated annual aggregate for GP/FY "
                f"({gp_lgd_code!r}, {fiscal_year!r})"
            )

    def accept(self, record: _VoucherInput) -> None:
        try:
            self.connection.execute(
                "INSERT INTO voucher_keys(gp_lgd_code, fiscal_year, voucher_no) VALUES (?, ?, ?)",
                record.natural_key,
            )
        except sqlite3.IntegrityError as exc:
            raise VoucherNaturalKeyError(
                "duplicate voucher natural key "
                f"(gp_lgd_code={record.gp_lgd_code!r}, fiscal_year={record.fiscal_year!r}, "
                f"voucher_no={record.voucher_no!r})"
            ) from exc

        self.declare_annual(
            record.gp_lgd_code,
            record.fiscal_year,
            record.receipt_count,
            record.payment_count,
            record.total_receipts,
            record.total_payments,
        )

        if record.direction == "receipt":
            self.connection.execute(
                "UPDATE voucher_annual SET actual_receipts = actual_receipts + 1 "
                "WHERE gp_lgd_code = ? AND fiscal_year = ?",
                (record.gp_lgd_code, record.fiscal_year),
            )
            # SQLite cannot add Decimal values; update the textual decimal in
            # Python while keeping only one scalar per GP/FY in state.
            self._add_actual_amount(record, receipt=True)
            self.receipt_rows += 1
        else:
            self.connection.execute(
                "UPDATE voucher_annual SET actual_payments = actual_payments + 1 "
                "WHERE gp_lgd_code = ? AND fiscal_year = ?",
                (record.gp_lgd_code, record.fiscal_year),
            )
            self._add_actual_amount(record, receipt=False)
            self.payment_rows += 1
        self.source_rows += 1
        self.total_amount += record.amount
        self.direction_amounts[record.direction] += record.amount

    def _add_actual_amount(self, record: _VoucherInput, *, receipt: bool) -> None:
        field = "actual_total_receipts" if receipt else "actual_total_payments"
        current = self.connection.execute(
            f"SELECT {field} FROM voucher_annual WHERE gp_lgd_code = ? AND fiscal_year = ?",
            (record.gp_lgd_code, record.fiscal_year),
        ).fetchone()
        assert current is not None
        updated = Decimal(current[0]) + record.amount
        self.connection.execute(
            f"UPDATE voucher_annual SET {field} = ? WHERE gp_lgd_code = ? AND fiscal_year = ?",
            (str(updated), record.gp_lgd_code, record.fiscal_year),
        )

    def finish(self) -> None:
        for row in self.connection.execute(
            "SELECT gp_lgd_code, fiscal_year, receipt_count, payment_count, "
            "total_receipts, total_payments, actual_receipts, actual_payments, "
            "actual_total_receipts, actual_total_payments FROM voucher_annual"
        ):
            (
                gp,
                fy,
                receipt_count,
                payment_count,
                total_receipts,
                total_payments,
                actual_receipts,
                actual_payments,
                actual_total_receipts,
                actual_total_payments,
            ) = row
            if receipt_count != actual_receipts or payment_count != actual_payments:
                raise VoucherAggregateError(
                    f"annual count aggregate does not match voucher rows for GP/FY ({gp!r}, {fy!r})"
                )
            if Decimal(total_receipts) != Decimal(actual_total_receipts):
                raise VoucherAggregateError(
                    f"annual receipt total does not match voucher rows for GP/FY ({gp!r}, {fy!r})"
                )
            if Decimal(total_payments) != Decimal(actual_total_payments):
                raise VoucherAggregateError(
                    f"annual payment total does not match voucher rows for GP/FY ({gp!r}, {fy!r})"
                )
        self._assign_surrogate_keys()
        self.connection.commit()

    def _assign_surrogate_keys(self) -> None:
        self.connection.execute("UPDATE voucher_keys SET voucher_pk = NULL")
        self.connection.execute(
            """
            UPDATE voucher_keys
            SET voucher_pk = (
                SELECT ? + ranked.rank_number - 1
                FROM (
                    SELECT gp_lgd_code, fiscal_year, voucher_no,
                           ROW_NUMBER() OVER (
                               ORDER BY gp_lgd_code, fiscal_year, voucher_no
                           ) AS rank_number
                    FROM voucher_keys
                ) AS ranked
                WHERE ranked.gp_lgd_code = voucher_keys.gp_lgd_code
                  AND ranked.fiscal_year = voucher_keys.fiscal_year
                  AND ranked.voucher_no = voucher_keys.voucher_no
            )
            """,
            (self.pk_start,),
        )

    def lookup_pks(self, records: Sequence[_VoucherInput]) -> tuple[int, ...]:
        self.connection.execute("DELETE FROM voucher_lookup")
        self.connection.executemany(
            "INSERT INTO voucher_lookup(sequence_number, gp_lgd_code, fiscal_year, voucher_no) "
            "VALUES (?, ?, ?, ?)",
            ((index, *record.natural_key) for index, record in enumerate(records)),
        )
        found = self.connection.execute(
            "SELECT lookup.sequence_number, keys.voucher_pk "
            "FROM voucher_lookup AS lookup "
            "JOIN voucher_keys AS keys USING (gp_lgd_code, fiscal_year, voucher_no) "
            "ORDER BY lookup.sequence_number"
        ).fetchall()
        if len(found) != len(records):
            raise VoucherNaturalKeyError("a validated voucher natural key was not assigned a surrogate")
        return tuple(int(row[1]) for row in found)

    def report(self) -> VoucherLoadReport:
        return VoucherLoadReport(
            source_system=self.source_system,
            source_run_id=self.source_run_id,
            source_revision=self.source_revision,
            source_rows=self.source_rows,
            natural_key_count=self.connection.execute(
                "SELECT COUNT(*) FROM voucher_keys"
            ).fetchone()[0],
            aggregate_group_count=self.connection.execute(
                "SELECT COUNT(*) FROM voucher_annual"
            ).fetchone()[0],
            payment_rows=self.payment_rows,
            receipt_rows=self.receipt_rows,
            total_amount=self.total_amount,
            direction_amounts=tuple(
                (direction, self.direction_amounts[direction])
                for direction in ("payment", "receipt")
            ),
        )


class VoucherLoader:
    """Load one voucher source revision using bounded output batches.

    A loader instance is intentionally single-run/single-revision.  Reusing
    the same instance for a second source would make it too easy to combine a
    flat derived CSV with a nested raw revision while believing the result was
    one coherent source.  Create another instance for another revision.
    """

    def __init__(self, state_path: str | Path | None = None) -> None:
        self._requested_state_path = None if state_path is None else Path(state_path)
        self._state_path: Path | None = None
        self._temporary_state = False
        self._connection: sqlite3.Connection | None = None
        self._state: _VoucherState | None = None
        self._identity: tuple[str, str, str] | None = None
        self._report: VoucherLoadReport | None = None

    def __enter__(self) -> "VoucherLoader":
        if self._connection is not None:
            raise VoucherModeError("VoucherLoader cannot be entered twice")
        if self._requested_state_path is None:
            handle = tempfile.NamedTemporaryFile(
                prefix="warehouse-voucher-state-", suffix=".sqlite3", delete=False
            )
            self._state_path = Path(handle.name)
            handle.close()
            self._temporary_state = True
        else:
            self._state_path = self._requested_state_path
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self._state_path))
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection = None
        if self._temporary_state and self._state_path is not None:
            self._state_path.unlink(missing_ok=True)
        self._state_path = None

    @property
    def report(self) -> VoucherLoadReport:
        if self._report is None:
            raise VoucherModeError("no voucher source has completed validation")
        return self._report

    @property
    def state_path(self) -> Path:
        if self._state_path is None:
            raise VoucherModeError("VoucherLoader must be used as a context manager")
        return self._state_path

    def _start(
        self,
        *,
        source_system: str,
        source_run_id: str,
        source_revision: str,
        pk_start: int,
    ) -> _VoucherState:
        if self._connection is None:
            raise VoucherModeError("VoucherLoader must be used as a context manager")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (source_system, source_run_id, source_revision)
        ):
            raise VoucherModeError("source_system, source_run_id, and source_revision are required")
        source_system = source_system.strip()
        source_run_id = source_run_id.strip()
        source_revision = source_revision.strip()
        if not isinstance(pk_start, int) or isinstance(pk_start, bool) or pk_start < 1:
            raise VoucherModeError("pk_start must be a positive integer")
        identity = (source_system, source_run_id, source_revision)
        if self._identity is not None:
            raise VoucherModeError(
                f"loader already owns source revision {self._identity!r}; create a new loader"
            )
        self._identity = identity
        self._state = _VoucherState(
            self._connection,
            source_system=source_system,
            source_run_id=source_run_id,
            source_revision=source_revision,
            pk_start=pk_start,
        )
        return self._state

    def load_flat_csv(
        self,
        path: str | Path,
        *,
        source_system: str,
        source_run_id: str,
        source_revision: str = "flat-csv-v1",
        chunk_size: int = 100_000,
        pk_start: int = 1,
    ) -> Iterator[pd.DataFrame]:
        """Validate and stream the strict 18-column flat voucher CSV."""

        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
            raise VoucherModeError("chunk_size must be a positive integer")
        source_path = Path(path)
        state = self._start(
            source_system=source_system,
            source_run_id=source_run_id,
            source_revision=source_revision,
            pk_start=pk_start,
        )
        self._scan_flat(
            source_path,
            state=state,
            chunk_size=chunk_size,
        )
        state.finish()
        self._report = state.report()
        return self._emit_flat(source_path, state=state, chunk_size=chunk_size)

    def _flat_chunks(self, path: Path, *, chunk_size: int) -> Iterator[pd.DataFrame]:
        yield from read_csv_chunks(
            path,
            expected_columns=VOUCHER_SOURCE_COLUMNS,
            dtype="string",
            chunksize=chunk_size,
        )

    def _scan_flat(self, path: Path, *, state: _VoucherState, chunk_size: int) -> None:
        offset = 0
        for frame in self._flat_chunks(path, chunk_size=chunk_size):
            records = _parse_flat_chunk(
                frame,
                source_file=path.name,
                source_row_offset=offset,
            )
            for record in records:
                state.accept(record)
            state.commit()
            offset += len(frame)

    def _emit_flat(
        self, path: Path, *, state: _VoucherState, chunk_size: int
    ) -> Iterator[pd.DataFrame]:
        offset = 0
        for frame in self._flat_chunks(path, chunk_size=chunk_size):
            records = _parse_flat_chunk(
                frame,
                source_file=path.name,
                source_row_offset=offset,
            )
            yield self._output_batch(records, state=state)
            offset += len(frame)

    def load_nested_json(
        self,
        paths: Iterable[str | Path],
        *,
        source_system: str,
        source_run_id: str,
        source_revision: str = "nested-json-v1",
        batch_size: int = 100_000,
        pk_start: int = 1,
    ) -> Iterator[pd.DataFrame]:
        """Validate and stream per-GP/FY nested accounting JSON files.

        ``paths`` is sorted once by path name; only one JSON file and one
        output batch are materialized at a time.  The source revision is
        explicit so a raw tree and a derived flat CSV cannot be silently
        merged by a convenience fallback.
        """

        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise VoucherModeError("batch_size must be a positive integer")
        source_paths = tuple(sorted(Path(path) for path in paths))
        if not source_paths:
            raise VoucherSourceSchemaError("nested voucher source contains no files")
        state = self._start(
            source_system=source_system,
            source_run_id=source_run_id,
            source_revision=source_revision,
            pk_start=pk_start,
        )
        self._scan_nested(source_paths, state=state)
        state.finish()
        self._report = state.report()
        return self._emit_nested(source_paths, state=state, batch_size=batch_size)

    def _scan_nested(self, paths: Sequence[Path], *, state: _VoucherState) -> None:
        for path in paths:
            for record in _parse_nested_file(path, annual_callback=state.declare_annual):
                state.accept(record)
            state.commit()

    def _emit_nested(
        self, paths: Sequence[Path], *, state: _VoucherState, batch_size: int
    ) -> Iterator[pd.DataFrame]:
        pending: list[_VoucherInput] = []
        for path in paths:
            for record in _parse_nested_file(path):
                pending.append(record)
                if len(pending) >= batch_size:
                    yield self._output_batch(tuple(pending), state=state)
                    pending.clear()
        if pending:
            yield self._output_batch(tuple(pending), state=state)

    def _output_batch(
        self, records: Sequence[_VoucherInput], *, state: _VoucherState
    ) -> pd.DataFrame:
        if not records:
            return pd.DataFrame(columns=VOUCHER_BATCH_COLUMNS)
        pks = state.lookup_pks(records)
        rows: list[dict[str, object]] = []
        for pk, record in zip(pks, records):
            position = record.source_position
            source_record_id = deterministic_provenance_id(
                source_system=state.source_system,
                source_run_id=state.source_run_id,
                source_file=record.source_file,
                source_row_number=record.source_row_number,
                source_kind=f"voucher:{state.source_revision}",
                gp_code=record.gp_lgd_code,
                fiscal_year=record.fiscal_year,
                position=position,
            )
            rows.append(
                {
                    "voucher_pk": pk,
                    "gp_lgd_code": record.gp_lgd_code,
                    "fiscal_year": record.fiscal_year,
                    "voucher_no": record.voucher_no,
                    "voucher_id": record.voucher_id,
                    "direction": record.direction,
                    "type": record.type_label,
                    "date": record.date,
                    "month": record.month,
                    "amount": record.amount,
                    "source_system": state.source_system,
                    "source_run_id": state.source_run_id,
                    "source_revision": state.source_revision,
                    "source_file": record.source_file,
                    "source_row_number": record.source_row_number,
                    "source_record_id": source_record_id,
                    "gram_panchayat_name": record.gp_name,
                }
            )
        return pd.DataFrame(rows, columns=VOUCHER_BATCH_COLUMNS)


__all__ = [
    "NESTED_VOUCHER_ROW_COLUMNS",
    "VOUCHER_BATCH_COLUMNS",
    "VOUCHER_PROVENANCE_COLUMNS",
    "VOUCHER_SOURCE_COLUMNS",
    "VOUCHER_TARGET_COLUMNS",
    "VoucherAggregateError",
    "VoucherLoadReport",
    "VoucherLoader",
    "VoucherLoaderError",
    "VoucherModeError",
    "VoucherNaturalKeyError",
    "VoucherSourceSchemaError",
]
