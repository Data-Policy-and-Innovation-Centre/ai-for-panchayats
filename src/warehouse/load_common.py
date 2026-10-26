"""Small, strict building blocks shared by source loaders.

The warehouse has several source-specific loaders, but the dangerous edges of
CSV ingestion are source independent.  This module keeps those edges in one
place:

* CSV headers are read with an explicit comma delimiter and UTF-8 BOM
  handling.  Values default to strings so an identifier such as ``0123`` is
  not changed by pandas' type inference, and records are yielded in bounded
  chunks.
* Dates require a caller-supplied format.  A non-blank value that cannot be
  parsed raises instead of disappearing as ``NaT``; the exception reports the
  source-blank and parsed-null counts that make this check auditable.
* Money is parsed directly from text to :class:`decimal.Decimal`.  Missing
  values remain ``None`` and malformed values raise; neither can become a
  made-up zero.
* Fiscal years and identifiers have one canonical representation.
* Provenance identifiers are derived from stable source/run/file/row fields,
  never from the current time or a random UUID.

These functions intentionally do not open or discover project data.  A caller
passes an explicit path (normally an approved source file or a synthetic test
fixture), and all schema decisions are explicit at the call site.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import numbers
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd


class LoaderError(ValueError):
    """Base class for a source-loader validation failure."""


class CsvSchemaError(LoaderError):
    """A CSV cannot satisfy the schema requested by a loader."""


class DateParseError(LoaderError):
    """A date column contained a non-blank value that did not parse."""

    def __init__(
        self,
        *,
        column: str,
        date_format: str,
        source_blank_count: int,
        parsed_null_count: int,
        row: object | None = None,
        value: object | None = None,
    ) -> None:
        self.column = column
        self.date_format = date_format
        self.source_blank_count = source_blank_count
        self.parsed_null_count = parsed_null_count
        self.row = row
        self.value = value
        location = f" at row {row!r}" if row is not None else ""
        super().__init__(
            f"{column!r} parsed with format {date_format!r}{location}: "
            f"source blanks={source_blank_count}, parsed nulls={parsed_null_count}"
            + (f"; invalid value={value!r}" if value is not None else "")
        )


class MoneyParseError(LoaderError):
    """A monetary value is missing, malformed, or not safely representable."""

    def __init__(
        self,
        *,
        column: str,
        value: object,
        row: object | None = None,
        detail: str = "is not a valid finite decimal",
    ) -> None:
        self.column = column
        self.value = value
        self.row = row
        self.detail = detail
        location = f" at row {row!r}" if row is not None else ""
        super().__init__(f"{column!r}{location}: value {value!r} {detail}")


class FiscalYearError(LoaderError):
    """A fiscal year is not in the canonical ``YYYY-YYYY`` form."""

    def __init__(
        self, value: object, *, column: str = "fiscal_year", row: object | None = None
    ) -> None:
        self.column = column
        self.value = value
        self.row = row
        location = f" at row {row!r}" if row is not None else ""
        super().__init__(
            f"{column!r}{location}: {value!r} must be a consecutive four-digit "
            "fiscal year such as '2025-2026'"
        )


class IdentifierError(LoaderError):
    """An identifier cannot be represented safely as a string."""


class ProvenanceError(LoaderError):
    """Required provenance input is missing or invalid."""


DEFAULT_CHUNK_SIZE = 100_000
CSV_ENCODING = "utf-8-sig"
CSV_DELIMITER = ","
PROVENANCE_COLUMNS = (
    "row_id",
    "parent_row_id",
    "pos",
    "source_system",
    "source_run_id",
    "source_record_id",
    "schema_version",
    "source_file",
    "source_kind",
    "gp_code",
    "gram_panchayat_name",
    "fiscal_year",
    "plan_year",
    "business_id",
    "mapping_status",
)
_INTEGER_DOT_ZERO = re.compile(r"^(-?\d+)\.0+$")
_FISCAL_YEAR = re.compile(r"^(?P<start>\d{4})-(?P<end>\d{4})$")
_CURRENCY_PREFIX = re.compile(r"(?i)^\s*(?:₹|rs\.?|inr)\s*")

# Digit grouping this loader accepts, checked BEFORE the commas are stripped.
#
# Stripping unconditionally turns malformed input into a plausible number
# rather than an error: "1,2" becomes 12 and "12,,34" becomes 1234, silently
# corrupting an expenditure amount that the caller was promised would raise.
#
# Both conventions are permitted because this is Odisha government financial
# data. Indian grouping puts the last three digits together and every group
# before that in twos -- 1,00,000 is one lakh, NOT a malformed 100,000 -- so a
# validator that only knows three-digit grouping would reject the real data
# wholesale. That failure would be worse than the one being fixed: silent
# corruption of some rows traded for rejection of nearly all of them.
# Public, deliberately: warehouse.clean parses money on the live transform
# path and must apply the same rule. Two copies of this expression would
# drift, and the one that drifted would silently accept what the other
# rejects.
MONEY_GROUPED = re.compile(
    r"""^[+-]?(?:
          \d{1,3}(?:,\d{3})*          # 1  123  1,234  12,345,678
        | \d{1,2}(?:,\d{2})*,\d{3}    # 1,00,000  12,34,567  1,23,45,678
    )(?:\.\d*)?$""",
    re.X,
)
_NULL_TEXT = frozenset({"", "na", "n/a", "nan", "none", "null", "<na>", "-"})
_IDENTIFIER_NULL_TEXT = frozenset({"", "nan", "none", "null", "<na>", "n/a"})
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _nonfinite_number(value: object) -> bool:
    """Whether a numeric scalar is NaN or infinite.

    ``numpy`` scalar numbers are intentionally handled without importing
    numpy directly.  ``math.isfinite`` accepts the common numpy real scalar
    types and the guarded conversion covers numeric implementations that do
    not expose the exact same protocol.
    """

    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, numbers.Number) and not isinstance(value, bool):
        try:
            return not math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            try:
                return not math.isfinite(float(value))
            except (OverflowError, TypeError, ValueError):
                return False
    return False


def _optional_text(value: object, *, field: str) -> str | None:
    """Normalize a nullable provenance scalar without leaking raw TypeError."""

    if _is_blank(value):
        return None
    try:
        text = str(value).strip()
    except (TypeError, ValueError) as exc:
        raise ProvenanceError(f"{field} cannot be converted to text") from exc
    return text or None


def _required_text(value: object, *, field: str) -> str:
    text = _optional_text(value, field=field)
    if text is None:
        raise ProvenanceError(f"{field} must be a non-empty string")
    return text


def _optional_position(value: object) -> int | None:
    if _nonfinite_number(value):
        raise ProvenanceError("position must be a non-negative integer or null")
    if _is_blank(value):
        return None
    if not isinstance(value, numbers.Integral) or isinstance(value, bool) or value < 0:
        raise ProvenanceError("position must be a non-negative integer or null")
    return int(value)


def _safe_identifier(value: object, *, field: str) -> str | None:
    try:
        return clean_identifier(value, column=field)
    except IdentifierError as exc:
        raise ProvenanceError(f"{field} is not a valid identifier: {exc}") from exc


def _is_blank(value: object) -> bool:
    """Return whether a scalar is an absent source value.

    ``pd.NA`` cannot be used directly in a boolean expression, so this small
    helper keeps all nullable pandas scalar handling in one place.
    """

    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, bool) and missing:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _is_bool_scalar(value: object) -> bool:
    """Return whether *value* is a Python or NumPy boolean scalar.

    ``numpy.bool_`` is deliberately not a ``bool`` subclass and is not part
    of ``numbers.Number``.  Pandas exposes the scalar predicate we need while
    allowing this module to avoid a direct NumPy dependency.
    """

    try:
        return bool(pd.api.types.is_bool(value))
    except (TypeError, ValueError):
        return isinstance(value, bool)


def _source_blank_mask(series: pd.Series) -> pd.Series:
    """Mask null/empty/whitespace-only values in a source column."""

    text = series.astype("string")
    return series.isna() | text.str.strip().eq("")


def validate_columns(
    actual_columns: Iterable[object] | pd.DataFrame,
    *,
    required: Iterable[str] = (),
    expected: Iterable[str] | None = None,
    source: str = "source",
) -> tuple[str, ...]:
    """Validate required (or exact) source columns and return their names.

    ``expected`` is an opt-in exact schema check.  With only ``required`` the
    source may carry additional columns, which is useful for the portal files
    whose optional fields evolve over time.  Duplicate header names are always
    rejected because pandas would otherwise mangle them and a loader could
    silently read the wrong half of a duplicate column.
    """

    if isinstance(actual_columns, pd.DataFrame):
        names = tuple(str(column) for column in actual_columns.columns)
    else:
        names = tuple(str(column) for column in actual_columns)
    duplicates = tuple(sorted({name for name in names if names.count(name) > 1}))
    if duplicates:
        raise CsvSchemaError(f"{source}: duplicate column(s): {duplicates}")

    required_names = tuple(dict.fromkeys(str(name) for name in required))
    missing = tuple(name for name in required_names if name not in names)
    if missing:
        raise CsvSchemaError(
            f"{source}: missing required column(s): {missing}; "
            f"available columns: {names}"
        )

    if expected is not None:
        expected_names = tuple(dict.fromkeys(str(name) for name in expected))
        expected_set = set(expected_names)
        actual_set = set(names)
        missing_exact = tuple(name for name in expected_names if name not in actual_set)
        unexpected = tuple(name for name in names if name not in expected_set)
        if missing_exact or unexpected or len(names) != len(expected_names):
            detail: list[str] = []
            if missing_exact:
                detail.append(f"missing={missing_exact}")
            if unexpected:
                detail.append(f"unexpected={unexpected}")
            raise CsvSchemaError(f"{source}: schema mismatch ({'; '.join(detail)})")
    return names


def _read_header(path: Path, *, encoding: str, delimiter: str) -> tuple[str, ...]:
    """Read only an explicit CSV header, preserving the BOM-safe contract."""

    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            header = next(reader, None)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CsvSchemaError(f"cannot read CSV header from {path}: {exc}") from exc
    if not header:
        raise CsvSchemaError(f"CSV has no header: {path}")
    return validate_columns(header, source=str(path))


def _csv_dtype(
    header: Sequence[str],
    dtype: Mapping[str, str] | str | None,
) -> Mapping[str, str] | str:
    """Build a dtype argument that defaults every untyped field to string."""

    if dtype is None:
        return "string"
    if isinstance(dtype, str):
        return dtype
    unknown = tuple(name for name in dtype if name not in header)
    if unknown:
        raise CsvSchemaError(f"dtype names are not present in CSV header: {unknown}")
    return {name: dtype.get(name, "string") for name in header}


def _validate_csv_field_counts(
    path: Path,
    *,
    expected_field_count: int,
    encoding: str,
    delimiter: str,
) -> None:
    """Validate every CSV record has the header's field count.

    Pandas' C engine can infer a leading field as an index when the first
    data record has too many fields.  In chunked mode that turns
    ``id,value`` plus ``0123,a,EXTRA`` into ``id=a, value=EXTRA`` instead of
    raising, while later malformed records may raise normally.  A streaming
    ``csv.reader`` pass makes that inference impossible: each record is
    checked before pandas is allowed to materialise any chunk.  The reader
    retains only the current CSV record, so validation remains bounded by the
    largest record (including a quoted embedded-newline record), not the file
    size.

    A physically blank line is retained by the pandas call below because
    ``skip_blank_lines=False`` is part of this loader's contract.  The CSV
    reader represents it as an empty record, which pandas expands to empty
    fields, so it is the one intentional exception to exact field counting.
    """

    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter, strict=True)
            header = next(reader, None)
            if header is None:
                raise CsvSchemaError(f"CSV has no header: {path}")
            if len(header) != expected_field_count:
                raise CsvSchemaError(
                    f"{path}: header has {len(header)} fields; "
                    f"expected {expected_field_count}"
                )
            for record_number, row in enumerate(reader, start=2):
                if not row:
                    continue
                if len(row) != expected_field_count:
                    raise CsvSchemaError(
                        f"{path}: row {record_number} has {len(row)} fields; "
                        f"expected {expected_field_count}"
                    )
    except CsvSchemaError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CsvSchemaError(f"cannot validate CSV field counts in {path}: {exc}") from exc


def read_csv_chunks(
    path: str | Path,
    *,
    required_columns: Iterable[str] = (),
    expected_columns: Iterable[str] | None = None,
    dtype: Mapping[str, str] | str | None = None,
    schema: Mapping[str, str] | None = None,
    chunksize: int = DEFAULT_CHUNK_SIZE,
    encoding: str = CSV_ENCODING,
    delimiter: str = CSV_DELIMITER,
) -> Iterator[pd.DataFrame]:
    """Yield an explicitly comma-delimited CSV as bounded pandas frames.

    Header validation happens before the iterator is returned.  The default
    ``utf-8-sig`` removes an optional BOM from the first header while reading
    ordinary UTF-8 unchanged.  ``dtype='string'`` and ``na_filter=False``
    preserve identifier text and source blanks; date/money conversion is
    deliberately left to the strict helpers below.
    """

    csv_path = Path(path)
    required_columns = tuple(required_columns)
    expected_columns = None if expected_columns is None else tuple(expected_columns)
    if schema is not None:
        if dtype is not None:
            raise CsvSchemaError("pass either dtype or schema, not both")
        dtype = schema
        required_columns = tuple(dict.fromkeys((*required_columns, *schema)))
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise CsvSchemaError("CSV delimiter must be one explicit character")
    if not isinstance(chunksize, int) or chunksize <= 0:
        raise CsvSchemaError("chunksize must be a positive integer")
    header = _read_header(csv_path, encoding=encoding, delimiter=delimiter)
    validate_columns(
        header,
        required=required_columns,
        expected=expected_columns,
        source=str(csv_path),
    )
    _validate_csv_field_counts(
        csv_path,
        expected_field_count=len(header),
        encoding=encoding,
        delimiter=delimiter,
    )
    read_dtype = _csv_dtype(header, dtype)
    try:
        reader = pd.read_csv(
            csv_path,
            sep=delimiter,
            encoding=encoding,
            dtype=read_dtype,
            chunksize=chunksize,
            keep_default_na=False,
            na_filter=False,
            skip_blank_lines=False,
            engine="c",
            on_bad_lines="error",
            # Keep pandas from treating an over-wide first row as an implicit
            # index.  The streaming field-count pass above rejects that row;
            # this option preserves the same positional semantics for valid
            # rows and makes the invariant explicit to future maintainers.
            index_col=False,
        )
    except (OSError, UnicodeError, ValueError, pd.errors.ParserError) as exc:
        raise CsvSchemaError(f"cannot open CSV {csv_path}: {exc}") from exc

    def _chunks() -> Iterator[pd.DataFrame]:
        try:
            for chunk in reader:
                # ``read_csv`` has already applied the dtype map.  Re-check
                # the frame because a future pandas change must not turn a
                # schema failure into an all-null column downstream.
                validate_columns(
                    chunk,
                    required=required_columns,
                    expected=expected_columns,
                    source=str(csv_path),
                )
                yield chunk
        except (OSError, UnicodeError, ValueError, pd.errors.ParserError) as exc:
            if isinstance(exc, CsvSchemaError):
                raise
            raise CsvSchemaError(f"failed while reading CSV {csv_path}: {exc}") from exc

    return _chunks()


def read_csv(
    path: str | Path,
    *,
    required_columns: Iterable[str] = (),
    expected_columns: Iterable[str] | None = None,
    dtype: Mapping[str, str] | str | None = None,
    schema: Mapping[str, str] | None = None,
    chunksize: int | None = None,
    encoding: str = CSV_ENCODING,
    delimiter: str = CSV_DELIMITER,
) -> pd.DataFrame | Iterator[pd.DataFrame]:
    """Read a CSV after strict header validation.

    ``chunksize`` is optional only for small dimension files and synthetic
    tests.  Production fact loaders should use the iterator returned by
    :func:`read_csv_chunks`; specifying a positive ``chunksize`` here returns
    that iterator instead of materialising the full file.
    """

    if chunksize is not None:
        return read_csv_chunks(
            path,
            required_columns=required_columns,
            expected_columns=expected_columns,
            dtype=dtype,
            schema=schema,
            chunksize=chunksize,
            encoding=encoding,
            delimiter=delimiter,
        )
    chunks = read_csv_chunks(
        path,
        required_columns=required_columns,
        expected_columns=expected_columns,
        dtype=dtype,
        schema=schema,
        chunksize=DEFAULT_CHUNK_SIZE,
        encoding=encoding,
        delimiter=delimiter,
    )
    frames = list(chunks)
    if not frames:
        # Header validation gave us the schema, so an empty file still
        # returns a frame with stable columns rather than an unlabelled frame.
        header = _read_header(Path(path), encoding=encoding, delimiter=delimiter)
        return pd.DataFrame(columns=header)
    return pd.concat(frames, ignore_index=True)


def parse_date_series(
    series: pd.Series,
    *,
    date_format: str,
    column: str = "date",
    allow_null: bool = True,
) -> pd.Series:
    """Parse one date column with an explicit format and null-count guard.

    ``date_format`` is required so callers choose ``%d/%m/%Y`` for accounting
    vouchers and ``%Y-%m-%d`` for eGramSwaraj approvals.  ``format='mixed'``
    and ``dayfirst=True`` are intentionally not available here: both permit
    a malformed source value to look plausible.  A source blank may remain
    null when ``allow_null`` is true; every additional parsed null is an
    error naming the column and both counts.
    """

    if (
        not isinstance(date_format, str)
        or not date_format.strip()
        or date_format == "mixed"
    ):
        raise DateParseError(
            column=column,
            date_format=str(date_format),
            source_blank_count=int(_source_blank_mask(series).sum()),
            parsed_null_count=-1,
        )
    source_blank_count = int(_source_blank_mask(series).sum())
    source_text = series.astype("string").str.strip()
    strict_iso_invalid = pd.Series(False, index=series.index)
    if date_format == "%Y-%m-%d":
        # pandas accepts unpadded month/day components even with an explicit
        # ``%m``/``%d`` format.  The warehouse contract is stricter: ISO dates
        # must be exactly ``YYYY-MM-DD`` before semantic date parsing.
        strict_iso_invalid = ~_source_blank_mask(series) & ~source_text.str.fullmatch(
            _ISO_DATE
        )
    try:
        parsed = pd.to_datetime(series, format=date_format, errors="coerce", exact=True)
    except (TypeError, ValueError) as exc:
        raise DateParseError(
            column=column,
            date_format=date_format,
            source_blank_count=source_blank_count,
            parsed_null_count=-1,
        ) from exc
    if strict_iso_invalid.any():
        parsed = parsed.mask(strict_iso_invalid)
    parsed_null_count = int(parsed.isna().sum())
    if parsed_null_count != source_blank_count:
        invalid_rows = ~_source_blank_mask(series) & parsed.isna()
        row = invalid_rows[invalid_rows].index[0] if invalid_rows.any() else None
        value = series.loc[row] if row is not None else None
        raise DateParseError(
            column=column,
            date_format=date_format,
            source_blank_count=source_blank_count,
            parsed_null_count=parsed_null_count,
            row=row,
            value=value,
        )
    if not allow_null and source_blank_count:
        row = _source_blank_mask(series)[_source_blank_mask(series)].index[0]
        raise DateParseError(
            column=column,
            date_format=date_format,
            source_blank_count=source_blank_count,
            parsed_null_count=parsed_null_count,
            row=row,
            value=series.loc[row],
        )
    return parsed


def parse_date(
    value: object | pd.Series,
    *,
    date_format: str,
    column: str = "date",
    allow_null: bool = True,
) -> object | pd.Series:
    """Parse a scalar or Series date using :func:`parse_date_series`."""

    if isinstance(value, pd.Series):
        return parse_date_series(
            value, date_format=date_format, column=column, allow_null=allow_null
        )
    result = parse_date_series(
        pd.Series([value]),
        date_format=date_format,
        column=column,
        allow_null=allow_null,
    )
    return result.iloc[0]


def _decimal_from_value(
    value: object,
    *,
    column: str,
    row: object | None,
    allow_null: bool,
    places: int | None,
) -> Decimal | None:
    if _nonfinite_number(value):
        raise MoneyParseError(
            column=column, value=value, row=row, detail="is not finite"
        )
    if _is_blank(value) or (
        isinstance(value, str) and value.strip().casefold() in _NULL_TEXT
    ):
        if allow_null:
            return None
        raise MoneyParseError(
            column=column,
            value=value,
            row=row,
            detail="is blank but null is not allowed",
        )
    if _is_bool_scalar(value):
        raise MoneyParseError(
            column=column, value=value, row=row, detail="boolean values are not money"
        )
    if isinstance(value, Decimal):
        decimal_value = value
    else:
        text = str(value).strip()
        text = _CURRENCY_PREFIX.sub("", text)
        if "," in text:
            # Only grouped values are checked; an ungrouped string falls
            # through to Decimal() below, which rejects its own garbage.
            if not MONEY_GROUPED.match(text):
                raise MoneyParseError(
                    column=column,
                    value=value,
                    row=row,
                    detail="has malformed digit grouping",
                )
            text = text.replace(",", "")
        if not text:
            # A currency prefix alone (no digits) is malformed, not blank.
            raise MoneyParseError(
                column=column,
                value=value,
                row=row,
                detail="is a currency prefix with no amount",
            )
        try:
            decimal_value = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise MoneyParseError(column=column, value=value, row=row) from exc
    if not decimal_value.is_finite():
        raise MoneyParseError(
            column=column, value=value, row=row, detail="is not finite"
        )
    if places is not None:
        if not isinstance(places, int) or isinstance(places, bool) or places < 0:
            raise MoneyParseError(
                column=column,
                value=value,
                row=row,
                detail="has an invalid decimal scale",
            )
        quantum = Decimal(1).scaleb(-places)
        # Quantisation must never silently round a source amount.  Decimal's
        # exponent includes trailing zeroes, so a caller can also detect a
        # source scale mismatch rather than unknowingly accepting it.
        fractional_digits = max(0, -decimal_value.as_tuple().exponent)
        if fractional_digits > places:
            raise MoneyParseError(
                column=column,
                value=value,
                row=row,
                detail=f"has {fractional_digits} fractional digits; at most {places} are allowed",
            )
        try:
            decimal_value = decimal_value.quantize(quantum)
        except (InvalidOperation, ValueError) as exc:
            raise MoneyParseError(
                column=column,
                value=value,
                row=row,
                detail="cannot be represented at the requested scale",
            ) from exc
    return decimal_value


def parse_money_series(
    series: pd.Series,
    *,
    column: str = "money",
    places: int | None = None,
    allow_null: bool = True,
) -> pd.Series:
    """Parse a monetary Series to exact ``Decimal`` values.

    ``places=None`` is lossless.  Pass ``places=2`` when targeting a
    ``DECIMAL(16,2)`` warehouse column; values with more fractional digits are
    rejected instead of rounded.  Invalid non-blank input raises
    :class:`MoneyParseError`.
    """

    values: list[Decimal | None] = []
    for row, value in series.items():
        values.append(
            _decimal_from_value(
                value,
                column=column,
                row=row,
                allow_null=allow_null,
                places=places,
            )
        )
    return pd.Series(values, index=series.index, dtype="object")


def parse_money(
    value: object | pd.Series,
    *,
    column: str = "money",
    places: int | None = None,
    allow_null: bool = True,
) -> Decimal | None | pd.Series:
    """Parse one money value or Series without a float/zero fallback."""

    if isinstance(value, pd.Series):
        return parse_money_series(
            value, column=column, places=places, allow_null=allow_null
        )
    return _decimal_from_value(
        value,
        column=column,
        row=None,
        allow_null=allow_null,
        places=places,
    )


def normalize_fiscal_year(
    value: object,
    *,
    column: str = "fiscal_year",
    row: object | None = None,
    allow_null: bool = True,
) -> str | None:
    """Return a consecutive ``YYYY-YYYY`` fiscal year or raise.

    The short form ``YYYY-YY`` is rejected deliberately.  Filtering a source
    with that form against the warehouse's canonical text would otherwise
    return zero rows while appearing to succeed.
    """

    if _is_blank(value):
        if allow_null:
            return None
        raise FiscalYearError(value, column=column, row=row)
    text = str(value).strip()
    match = _FISCAL_YEAR.fullmatch(text)
    if match is None or int(match.group("end")) != int(match.group("start")) + 1:
        raise FiscalYearError(value, column=column, row=row)
    return f"{int(match.group('start')):04d}-{int(match.group('end')):04d}"


def normalize_fiscal_year_series(
    series: pd.Series,
    *,
    column: str = "fiscal_year",
    allow_null: bool = True,
) -> pd.Series:
    """Normalize a fiscal-year Series while retaining its original index."""

    return pd.Series(
        [
            normalize_fiscal_year(value, column=column, row=row, allow_null=allow_null)
            for row, value in series.items()
        ],
        index=series.index,
        dtype="string",
    )


# British spelling mirrors ``pipeline.normalize``'s existing internal helper
# and lets source-specific code use either spelling without reimplementing it.
normalise_fiscal_year = normalize_fiscal_year
normalise_fiscal_year_series = normalize_fiscal_year_series


def clean_identifier(
    value: object,
    *,
    column: str = "identifier",
    row: object | None = None,
    allow_null: bool = True,
) -> str | None:
    """Represent an identifier as text without losing leading zeroes.

    A terminal ``.0`` or ``.00`` is removed because it is the artefact pandas
    creates when a nullable integer column is promoted to floating point.  A
    string such as ``0123`` remains exactly ``0123``.  No integer conversion
    or zero filling occurs.
    """

    if _nonfinite_number(value):
        raise IdentifierError(f"{column!r} at row {row!r}: identifier is not finite")
    if _is_blank(value):
        if allow_null:
            return None
        raise IdentifierError(f"{column!r} at row {row!r}: identifier is blank")
    if _is_bool_scalar(value):
        raise IdentifierError(
            f"{column!r} at row {row!r}: boolean is not an identifier"
        )
    text = str(value).strip()
    if text.casefold() in _IDENTIFIER_NULL_TEXT:
        if allow_null:
            return None
        raise IdentifierError(f"{column!r} at row {row!r}: identifier is blank")
    match = _INTEGER_DOT_ZERO.fullmatch(text)
    if match is not None:
        text = match.group(1)
    if not text:
        if allow_null:
            return None
        raise IdentifierError(f"{column!r} at row {row!r}: identifier is blank")
    return text


def clean_identifier_series(
    series: pd.Series,
    *,
    column: str = "identifier",
    allow_null: bool = True,
) -> pd.Series:
    """Clean a Series of identifiers using :func:`clean_identifier`."""

    return pd.Series(
        [
            clean_identifier(value, column=column, row=row, allow_null=allow_null)
            for row, value in series.items()
        ],
        index=series.index,
        dtype="string",
    )


def deterministic_provenance_id(
    *,
    source_system: str,
    source_run_id: str,
    source_file: str,
    source_row_number: int,
    source_kind: str = "",
    gp_code: str | None = None,
    fiscal_year: str | None = None,
    parent_row_id: str | None = None,
    position: int | None = None,
    child_collection: str | None = None,
) -> str:
    """Derive a stable row ID from the source identity and row location.

    ``source_run_id`` is validated but deliberately excluded from the hash:
    replaying the same source file and row under a new run ID must not mint a
    new identity (see ``src/pipeline/normalize.py``'s ``_provenance``, where
    ``manifest.run_id`` is likewise recorded as metadata but never folded
    into ``root_key``/child row IDs). ``child_collection`` mirrors that
    module's ``_child_rows``, which folds the sanitized collection key into
    each child's row ID so two collections don't collide at the same
    position under the same parent.
    """

    source_system = _required_text(source_system, field="source_system")
    source_run_id = _required_text(source_run_id, field="source_run_id")
    source_file = _required_text(source_file, field="source_file")
    source_kind = _required_text(source_kind, field="source_kind")
    gp_code = _safe_identifier(gp_code, field="gp_code")
    fiscal_year = _optional_text(fiscal_year, field="fiscal_year") or ""
    parent_row_id = _optional_text(parent_row_id, field="parent_row_id")
    position = _optional_position(position)
    if position is not None:
        child_collection = _required_text(child_collection, field="child_collection")
    else:
        child_collection = _optional_text(child_collection, field="child_collection")
    if (
        not isinstance(source_row_number, numbers.Integral)
        or isinstance(source_row_number, bool)
        or source_row_number < 1
    ):
        raise ProvenanceError("source_row_number must be a positive integer")
    values = (
        "warehouse-loader-provenance-v1",
        source_system,
        Path(source_file).as_posix(),
        source_kind,
        gp_code or "",
        fiscal_year,
        str(int(source_row_number)),
        parent_row_id or "",
        str(position) if position is not None else "",
        child_collection or "",
    )
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def row_provenance(
    *,
    source_system: str,
    source_run_id: str,
    source_file: str,
    source_row_number: int,
    source_kind: str = "",
    schema_version: str = "1",
    gp_code: str | None = None,
    gp_name: str | None = None,
    fiscal_year: str | None = None,
    business_id: str | None = None,
    source_record_id: str | None = None,
    parent_row_id: str | None = None,
    position: int | None = None,
    child_collection: str | None = None,
    mapping_status: str = "mapped",
) -> dict[str, Any]:
    """Return a row matching the existing canonical provenance contract."""

    source_system = _required_text(source_system, field="source_system")
    source_run_id = _required_text(source_run_id, field="source_run_id")
    source_file = _required_text(source_file, field="source_file")
    schema_version = _required_text(schema_version, field="schema_version")
    source_kind = _required_text(source_kind, field="source_kind")
    gp_code = _safe_identifier(gp_code, field="gp_code")
    gp_name = _optional_text(gp_name, field="gp_name")
    fiscal_year = _optional_text(fiscal_year, field="fiscal_year")
    business_id = _safe_identifier(business_id, field="business_id")
    parent_row_id = _optional_text(parent_row_id, field="parent_row_id")
    position = _optional_position(position)
    if position is not None:
        child_collection = _required_text(child_collection, field="child_collection")
    else:
        child_collection = _optional_text(child_collection, field="child_collection")
    mapping_status = _required_text(mapping_status, field="mapping_status")
    explicit_source_record_id = _optional_text(
        source_record_id, field="source_record_id"
    )
    # Validate the row number before deriving either root or child identity.
    if (
        not isinstance(source_row_number, numbers.Integral)
        or isinstance(source_row_number, bool)
        or source_row_number < 1
    ):
        raise ProvenanceError("source_row_number must be a positive integer")
    root_id = deterministic_provenance_id(
        source_system=source_system,
        source_run_id=source_run_id,
        source_file=source_file,
        source_row_number=int(source_row_number),
        source_kind=source_kind,
        gp_code=gp_code,
        fiscal_year=fiscal_year,
    )
    row_id = deterministic_provenance_id(
        source_system=source_system,
        source_run_id=source_run_id,
        source_file=source_file,
        source_row_number=source_row_number,
        source_kind=source_kind,
        gp_code=gp_code,
        fiscal_year=fiscal_year,
        parent_row_id=parent_row_id,
        position=position,
        child_collection=child_collection,
    )
    if explicit_source_record_id is not None:
        record_id = explicit_source_record_id
    else:
        # Children retain the root source record identity even though their
        # row IDs include parent/position and are therefore distinct.
        record_id = root_id
    return {
        "row_id": row_id,
        "parent_row_id": parent_row_id,
        "pos": position,
        "source_system": source_system,
        "source_run_id": source_run_id,
        "source_record_id": record_id,
        "schema_version": schema_version,
        "source_file": Path(source_file).as_posix(),
        "source_kind": source_kind,
        "gp_code": gp_code,
        "gram_panchayat_name": gp_name,
        "fiscal_year": fiscal_year,
        "plan_year": fiscal_year,
        "business_id": business_id,
        "mapping_status": mapping_status,
    }


@dataclass(frozen=True, slots=True)
class ProvenanceSpec:
    """Constant provenance metadata for one CSV source file."""

    source_system: str
    source_run_id: str
    source_file: str
    source_kind: str = ""
    schema_version: str = "1"
    gp_code: str | None = None
    gp_name: str | None = None
    fiscal_year: str | None = None

    def __post_init__(self) -> None:
        """Reject an unusable spec before even an empty frame is processed."""

        self.validate()

    def validate(self) -> None:
        """Validate all required and nullable metadata without filesystem IO."""

        _required_text(self.source_system, field="source_system")
        _required_text(self.source_run_id, field="source_run_id")
        _required_text(self.source_file, field="source_file")
        _required_text(self.source_kind, field="source_kind")
        _required_text(self.schema_version, field="schema_version")
        _safe_identifier(self.gp_code, field="gp_code")
        _optional_text(self.gp_name, field="gp_name")
        _optional_text(self.fiscal_year, field="fiscal_year")


def add_provenance(
    frame: pd.DataFrame,
    spec: ProvenanceSpec,
    *,
    start_row_number: int = 1,
    business_id_column: str | None = None,
    gp_code_column: str | None = None,
    fiscal_year_column: str | None = None,
) -> pd.DataFrame:
    """Add deterministic provenance to a frame without mutating the input.

    ``start_row_number`` is one-based and should advance by each chunk's row
    count when a loader processes a large CSV.  Optional source fields may be
    supplied as constants on ``ProvenanceSpec`` or as existing frame columns.
    """

    if not isinstance(frame, pd.DataFrame):
        raise ProvenanceError("frame must be a pandas DataFrame")
    if not isinstance(spec, ProvenanceSpec):
        raise ProvenanceError("spec must be a ProvenanceSpec")
    # ``ProvenanceSpec.__post_init__`` validates normal construction.  Keep an
    # explicit call here so a future mutable/spec-like implementation still
    # fails before column iteration, including for empty frames.
    spec.validate()
    if (
        not isinstance(start_row_number, numbers.Integral)
        or isinstance(start_row_number, bool)
        or start_row_number < 1
    ):
        raise ProvenanceError("start_row_number must be a positive integer")
    for name in (business_id_column, gp_code_column, fiscal_year_column):
        if name is None:
            continue
        if not isinstance(name, str) or not name.strip():
            raise ProvenanceError(f"provenance source column name is invalid: {name!r}")
        if name not in frame.columns:
            raise ProvenanceError(f"provenance source column is missing: {name}")
    out = frame.copy()
    rows: list[dict[str, Any]] = []
    for offset, (_, row) in enumerate(frame.iterrows()):
        source_row_number = int(start_row_number) + offset
        gp_code = row[gp_code_column] if gp_code_column else spec.gp_code
        fiscal_year = (
            row[fiscal_year_column] if fiscal_year_column else spec.fiscal_year
        )
        business_id = row[business_id_column] if business_id_column else None
        rows.append(
            row_provenance(
                source_system=spec.source_system,
                source_run_id=spec.source_run_id,
                source_file=spec.source_file,
                source_row_number=source_row_number,
                source_kind=spec.source_kind,
                schema_version=spec.schema_version,
                gp_code=gp_code,
                gp_name=spec.gp_name,
                fiscal_year=fiscal_year,
                business_id=business_id,
            )
        )
    provenance = pd.DataFrame(rows, index=frame.index, columns=PROVENANCE_COLUMNS)
    for column in provenance.columns:
        out[column] = provenance[column]
    return out


__all__ = [
    "CSV_DELIMITER",
    "CSV_ENCODING",
    "DEFAULT_CHUNK_SIZE",
    "PROVENANCE_COLUMNS",
    "CsvSchemaError",
    "DateParseError",
    "FiscalYearError",
    "IdentifierError",
    "LoaderError",
    "MoneyParseError",
    "ProvenanceError",
    "ProvenanceSpec",
    "add_provenance",
    "clean_identifier",
    "clean_identifier_series",
    "deterministic_provenance_id",
    "normalise_fiscal_year",
    "normalise_fiscal_year_series",
    "normalize_fiscal_year",
    "normalize_fiscal_year_series",
    "parse_date",
    "parse_date_series",
    "parse_money",
    "parse_money_series",
    "read_csv",
    "read_csv_chunks",
    "row_provenance",
    "validate_columns",
]
