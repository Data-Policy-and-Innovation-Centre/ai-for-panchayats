"""Aggregate acceptance checks for a snapshot, kept out of the public repo.

Row counts, money totals and known-answer results are derived from protected
source data, so they never enter Git, CI fixtures or issue text. They live in a
JSON object stored beside the artifact in private S3 and are fetched at task
startup, alongside the structural manifest that *is* public.

Money in the existing artifact is stored as floating point, so a sum is not
bit-reproducible across engines or orderings. Every numeric expectation must
therefore state its contract explicitly: an exact match, or a documented
absolute tolerance. There is no implicit default, because a silently tolerant
comparison is how a wrong number passes.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .errors import KnownAnswerError, SnapshotManifestError

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class KnownAnswerQuery:
    """One SQL probe and the answer a correct snapshot must return."""

    name: str
    sql: str
    expected: tuple[tuple[Any, ...], ...]
    tolerance: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SnapshotManifestError("known-answer query needs a non-empty name")
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise SnapshotManifestError(f"known-answer query {self.name!r} needs SQL")
        if self.tolerance is not None:
            if isinstance(self.tolerance, bool) or not isinstance(self.tolerance, (int, float)):
                raise SnapshotManifestError(
                    f"known-answer query {self.name!r} tolerance must be a number or null"
                )
            if self.tolerance < 0 or not math.isfinite(self.tolerance):
                raise SnapshotManifestError(
                    f"known-answer query {self.name!r} tolerance must be finite and non-negative"
                )


@dataclass(frozen=True)
class Expectations:
    """The aggregate contract a snapshot must satisfy before it is served."""

    relation_row_counts: Mapping[str, int]
    queries: tuple[KnownAnswerQuery, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.relation_row_counts, Mapping):
            raise SnapshotManifestError("relation_row_counts must be a JSON object")
        for name, count in self.relation_row_counts.items():
            if not isinstance(name, str) or not name.strip():
                raise SnapshotManifestError("relation_row_counts keys must be relation names")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise SnapshotManifestError(
                    f"relation_row_counts[{name!r}] must be a non-negative integer"
                )


def from_mapping(payload: Mapping[str, Any]) -> Expectations:
    if not isinstance(payload, Mapping):
        raise SnapshotManifestError("expectations payload must be a JSON object")

    version = payload.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise SnapshotManifestError(
            f"unsupported expectations schema_version {version!r}; "
            f"this build understands {SCHEMA_VERSION}"
        )

    raw_queries = payload.get("known_answer_queries", [])
    if not isinstance(raw_queries, list):
        raise SnapshotManifestError("known_answer_queries must be a JSON array")

    queries = []
    for entry in raw_queries:
        if not isinstance(entry, Mapping):
            raise SnapshotManifestError("each known-answer query must be a JSON object")
        if "tolerance" not in entry:
            raise SnapshotManifestError(
                f"known-answer query {entry.get('name')!r} must state a tolerance "
                "explicitly (null means exact match)"
            )
        rows = entry.get("expected")
        if not isinstance(rows, list):
            raise SnapshotManifestError(
                f"known-answer query {entry.get('name')!r} needs an expected row array"
            )
        queries.append(
            KnownAnswerQuery(
                name=entry.get("name"),
                sql=entry.get("sql"),
                expected=tuple(tuple(row) for row in rows),
                tolerance=entry["tolerance"],
            )
        )

    counts = payload.get("relation_row_counts", {})
    return Expectations(relation_row_counts=dict(counts), queries=tuple(queries))


def loads(text: str) -> Expectations:
    try:
        # parse_float=Decimal keeps a hand-written JSON number exact instead of
        # rounding it to binary64 before it is ever compared.
        payload = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise SnapshotManifestError("expectations object is not valid JSON") from exc
    return from_mapping(payload)


def _exact(value: Any) -> Decimal | int | None:
    """Return `value` as an exact number, or None if it is not numeric.

    Never routes through `float`. DuckDB hands back `Decimal` for a DECIMAL
    aggregate, and `float(Decimal(...))` would discard exactly the precision the
    DECIMAL cast exists to preserve -- 9007199254740993 and 9007199254740992
    collapse to the same binary64 value and would compare equal.

    A JSON string is accepted so an expectations file can carry a value that
    JSON's own number syntax cannot represent exactly.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) or isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def _is_finite(value: Decimal | int) -> bool:
    return not isinstance(value, Decimal) or value.is_finite()


def _rows_match(actual: Sequence[Sequence[Any]], query: KnownAnswerQuery) -> str | None:
    """Return a human-readable mismatch, or None when the rows agree."""
    if len(actual) != len(query.expected):
        return f"returned {len(actual)} rows, expected {len(query.expected)}"

    for row_index, (got_row, want_row) in enumerate(zip(actual, query.expected)):
        if len(got_row) != len(want_row):
            return f"row {row_index} has {len(got_row)} columns, expected {len(want_row)}"
        for col_index, (got, want) in enumerate(zip(got_row, want_row)):
            got_num, want_num = _exact(got), _exact(want)
            if got_num is None or want_num is None:
                if got != want:
                    return f"row {row_index} column {col_index}: {got!r} != {want!r}"
                continue

            # NaN fails every comparison, so `abs(NaN - x) > tolerance` is
            # False and a non-finite aggregate would sail through a tolerant
            # check. Reject it before comparing rather than after.
            if not _is_finite(got_num) or not _is_finite(want_num):
                return (
                    f"row {row_index} column {col_index}: non-finite value "
                    f"{got_num} (expected {want_num})"
                )

            if query.tolerance is None:
                if got_num != want_num:
                    return f"row {row_index} column {col_index}: {got_num} != {want_num} (exact)"
            elif abs(Decimal(got_num) - Decimal(want_num)) > Decimal(repr(query.tolerance)):
                return (
                    f"row {row_index} column {col_index}: |{got_num} - {want_num}| "
                    f"> {query.tolerance}"
                )
    return None


def verify(conn: Any, expectations: Expectations, *, catalog: str = "snap") -> None:
    """Run every aggregate check against an attached snapshot, or raise.

    `conn` is an open DuckDB connection with the snapshot attached read-only as
    `catalog`. Raises :class:`KnownAnswerError` on the first failure, naming the
    check but never echoing source rows.
    """
    for relation, expected_count in sorted(expectations.relation_row_counts.items()):
        quoted = relation.replace('"', '""')
        actual = conn.execute(f'SELECT count(*) FROM "{catalog}"."{quoted}"').fetchone()[0]
        if int(actual) != expected_count:
            raise KnownAnswerError(
                f"{relation} has {actual} rows, expected {expected_count}"
            )

    for query in expectations.queries:
        rows = conn.execute(query.sql).fetchall()
        mismatch = _rows_match(rows, query)
        if mismatch is not None:
            raise KnownAnswerError(f"known-answer query {query.name!r}: {mismatch}")
