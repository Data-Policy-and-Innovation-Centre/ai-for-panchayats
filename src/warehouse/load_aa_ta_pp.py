"""Strict, chunked loaders for the eGramSwaraj AA/TA/PP extracts.

The four CSVs handled here are the filtered, flattened eGramSwaraj extracts
used by issue #47:

* ``egramswaraj_aa_filtered.csv``;
* ``egramswaraj_aa__admapprovalschemewebservice_filtered.csv``;
* ``egramswaraj_ta_filtered.csv``; and
* ``egramswaraj_pp__physicalprogressassetstageuploadwebservice_filtered.csv``.

The stage-progress endpoint is deliberately *not* accepted by this module.
The source rows already carry stable ``row_id``/``parent_row_id`` values.  A
loader must therefore preserve those values rather than replacing them with
an unrelated hash.  ``source_record_id`` records the root source identity
(the row itself for a root row and its parent for a child row), while the
source/run/schema fields come from :class:`warehouse.load_common.ProvenanceSpec`.

``iter_*`` functions are the production interface.  They materialise one
pandas frame per requested chunk and retain source-payload-free validation
state (seen identifiers and reference indexes); those indexes grow with the
number of unique keys.  ``load_*`` functions are small convenience wrappers
for tests and pilot-scale callers that explicitly want one concatenated frame.
No function discovers files or auto-detects a CSV schema.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

import pandas as pd

from .load_common import (
    LoaderError,
    ProvenanceError,
    ProvenanceSpec,
    clean_identifier,
    clean_identifier_series,
    parse_date_series,
    parse_money_series,
    read_csv_chunks,
)


# The source schema is intentionally exact.  Optional fields are not silently
# invented because an extra/missing field can otherwise move the wrong value
# into a target column while leaving a plausible-looking table behind.
AA_SOURCE_COLUMNS = (
    "row_id",
    "lgd_code",
    "gram_panchayat_name",
    "plan_year",
    "doc_type",
    "source_file",
    "activityCd",
    "wrkPlnYr",
    "wrkAdmApprNo",
    "wrkAdmApprSnctnOrdrDt",
    "wrkProposedCost",
    "wrkAdmApprIssAuthrty",
)

AA_SCHEME_SOURCE_COLUMNS = (
    "row_id",
    "parent_row_id",
    "pos",
    "activityCd",
    "wrkSchmCd",
    "wrkSchmCmpntCd",
    "wrkAdmApprFndSnctnGen",
    "wrkAdmApprFndSnctnSt",
    "wrkAdmApprFndSnctnSc",
    "fndAllctnSchmTot",
)

TA_SOURCE_COLUMNS = (
    "row_id",
    "lgd_code",
    "gram_panchayat_name",
    "plan_year",
    "doc_type",
    "source_file",
    "activityCd",
    "wrkTecApprReqFlg",
    "wrkTecApprCost",
    "wrkTecApprIssAuthrty",
    "wrkTecApprOrdrNo",
    "wrkTecApprOrdrDt",
)

PP_UPLOAD_SOURCE_COLUMNS = (
    "row_id",
    "parent_row_id",
    "pos",
    "activityCd",
    "fileUploadId",
    "longitude",
    "latitude",
    "plnunttypecode",
)


# Outputs include the source/run lineage required by the current warehouse
# contract.  ``source_record_id`` and ``pos`` are retained as non-lossy
# lineage fields even though the current DDL projection does not yet expose
# both of them; ``warehouse.load.insert`` can project to the DDL explicitly.
# Keeping them here is important for the AA child and PP upload relationships.
LINEAGE_COLUMNS = (
    "source_system",
    "source_run_id",
    "source_record_id",
    "schema_version",
    "source_file",
    "source_kind",
    "row_id",
    "parent_row_id",
    "pos",
)

ADMIN_APPROVAL_COLUMNS = LINEAGE_COLUMNS + (
    "gp_lgd_code",
    "gp_name",
    "plan_year",
    "activity_code",
    "work_plan_year",
    "adm_approval_no",
    "adm_approval_sanction_date",
    "work_proposed_cost",
    "adm_approval_authority",
)

ADMIN_APPROVAL_SCHEME_COLUMNS = LINEAGE_COLUMNS + (
    "activity_code",
    "scheme_code",
    "scheme_component_code",
    "fund_sanctioned_general",
    "fund_sanctioned_sc",
    "fund_sanctioned_st",
    "fund_sanctioned_total",
)

TECHNICAL_APPROVAL_COLUMNS = LINEAGE_COLUMNS + (
    "gp_lgd_code",
    "gp_name",
    "plan_year",
    "activity_code",
    "tec_approval_required",
    "tec_approval_cost",
    "tec_approval_authority",
    "tec_approval_order_no",
    "tec_approval_order_date",
)

PHYSICAL_PROGRESS_COLUMNS = LINEAGE_COLUMNS + (
    "activity_code",
    "file_upload_id",
    "longitude",
    "latitude",
    "n_coords",
    "longitude_raw",
    "latitude_raw",
    "plan_unit_type_code",
)


ErrorMode = Literal["raise", "quarantine"]
_FOUR_DIGIT_YEAR = re.compile(r"^\d{4}$")


class ApprovalLoaderError(LoaderError):
    """Base class for a typed AA/TA/PP loading failure."""


class RequiredValueError(ApprovalLoaderError):
    """A required source value is absent or unusable."""

    def __init__(self, *, table: str, column: str, row: object | None = None) -> None:
        self.table = table
        self.column = column
        self.row = row
        location = f" at row {row!r}" if row is not None else ""
        super().__init__(f"{table}.{column} is required{location}")


class SourceYearError(ApprovalLoaderError):
    """A source plan year is not a zero-padded four-digit year."""

    def __init__(
        self, *, column: str, value: object, row: object | None = None
    ) -> None:
        self.column = column
        self.value = value
        self.row = row
        location = f" at row {row!r}" if row is not None else ""
        super().__init__(f"{column!r}{location}: {value!r} is not YYYY")


class DuplicateKeyError(ApprovalLoaderError):
    """A source key that must be globally unique was repeated."""

    def __init__(self, *, table: str, key_column: str, value: object) -> None:
        self.table = table
        self.key_column = key_column
        self.value = value
        super().__init__(
            f"{table}: duplicate {key_column} {value!r} across source chunks"
        )


class OrphanReferenceError(ApprovalLoaderError):
    """A child or fact row references no loaded parent/activity."""

    def __init__(
        self, *, table: str, key_column: str, value: object, parent: str = "reference"
    ) -> None:
        self.table = table
        self.key_column = key_column
        self.value = value
        self.parent = parent
        super().__init__(f"{table}: {key_column} {value!r} has no matching {parent}")


class SemanticValidationError(ApprovalLoaderError):
    """A source value violates a documented domain rule."""


class CoordinateParseError(ApprovalLoaderError):
    """A coordinate's first capture is not a finite number."""


class CoordinateMismatchError(ApprovalLoaderError):
    """Latitude and longitude capture counts differ."""


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    """One aggregated rejected-key record; raw source rows are never retained."""

    table: str
    reason_code: str
    reason: str
    key_column: str
    key_value: str
    row_count: int = 1


@dataclass
class LoaderAudit:
    """Counters and identity/reference indexes for one stream.

    The indexes intentionally grow with the number of unique keys so
    cross-chunk validation remains fail-closed, but they retain no raw source
    payload rows.
    """

    _REASONS: ClassVar[dict[str, str]] = {
        "duplicate_row_id": "row_id is duplicated or collides with a parent identity",
        "orphan_activity": "activity_code does not resolve to planned_activity",
        "orphan_gp": "gp_lgd_code does not resolve to gram_panchayat",
        "orphan_parent_row_id": "parent_row_id does not resolve to admin_approval",
        "parent_activity_mismatch": "activity_code does not match the parent activity",
    }

    rows_read: int = 0
    rows_loaded: int = 0
    quarantined: list[QuarantineRecord] = field(default_factory=list)
    row_ids: set[str] = field(default_factory=set)
    # Every row_id ever seen in this stream, whether or not it was accepted.
    # ``row_ids`` above stays "accepted" identities only (it feeds downstream
    # FK indexes like ApprovalParentIndex); duplicate detection must instead
    # be checked against every observed id so a quarantined-then-repeated
    # row_id is still reported as a duplicate.
    observed_row_ids: set[str] = field(default_factory=set)
    activity_codes: set[str] = field(default_factory=set)
    activity_by_row_id: dict[str, str] = field(default_factory=dict)
    _quarantine_index: dict[tuple[str, str, str, str], int] = field(
        default_factory=dict, repr=False, compare=False
    )

    def add_quarantine(
        self, *, table: str, reason_code: str, key_column: str, key_value: object
    ) -> None:
        reason = self._REASONS.get(reason_code, "row rejected by source validation")
        rendered = "<null>" if key_value is None else str(key_value)
        key = (table, reason_code, key_column, rendered)
        index = self._quarantine_index.get(key)
        if index is not None:
            record = self.quarantined[index]
            self.quarantined[index] = QuarantineRecord(
                table=record.table,
                reason_code=record.reason_code,
                reason=record.reason,
                key_column=record.key_column,
                key_value=record.key_value,
                row_count=record.row_count + 1,
            )
            return
        self._quarantine_index[key] = len(self.quarantined)
        self.quarantined.append(
            QuarantineRecord(
                table=table,
                reason_code=reason_code,
                reason=reason,
                key_column=key_column,
                key_value=rendered,
            )
        )

    def quarantine_frame(
        self, *, source_system: str, source_run_id: str
    ) -> pd.DataFrame:
        columns = [
            "source_system",
            "source_run_id",
            "table_name",
            "reason_code",
            "reason",
            "key_column",
            "key_value",
            "row_count",
        ]
        rows = [
            {
                "source_system": source_system,
                "source_run_id": source_run_id,
                "table_name": record.table,
                "reason_code": record.reason_code,
                "reason": record.reason,
                "key_column": record.key_column,
                "key_value": record.key_value,
                "row_count": record.row_count,
            }
            for record in self.quarantined
        ]
        return pd.DataFrame(rows, columns=columns)


@dataclass(frozen=True, slots=True)
class ApprovalParentIndex:
    """Global AA parent identities needed before streaming its child file."""

    row_ids: frozenset[str]
    activity_by_row_id: Mapping[str, str]

    @property
    def activity_codes(self) -> frozenset[str]:
        return frozenset(self.activity_by_row_id.values())


@dataclass(frozen=True, slots=True)
class AATAPPBundle:
    """Materialised convenience result for the four explicit source files."""

    tables: Mapping[str, pd.DataFrame]
    audits: Mapping[str, LoaderAudit]


def _validate_spec(spec: ProvenanceSpec, *, expected_kind: str | Sequence[str]) -> None:
    if not isinstance(spec, ProvenanceSpec):
        raise ProvenanceError("spec must be a ProvenanceSpec")
    spec.validate()
    expected = (
        (expected_kind,) if isinstance(expected_kind, str) else tuple(expected_kind)
    )
    if spec.source_kind not in expected:
        raise ProvenanceError(
            f"source_kind must be one of {expected!r} for this loader, "
            f"got {spec.source_kind!r}"
        )


def _text_series(series: pd.Series, *, fallback: str | None = None) -> pd.Series:
    out = series.astype("string").str.strip()
    out = out.mask(out.eq(""))
    if fallback is not None:
        out = out.fillna(fallback)
    return out


def _required_ids(series: pd.Series, *, table: str, column: str) -> pd.Series:
    cleaned = clean_identifier_series(series, column=column, allow_null=True)
    missing = cleaned.isna()
    if bool(missing.any()):
        row = missing[missing].index[0]
        raise RequiredValueError(table=table, column=column, row=row)
    return cleaned


def _optional_id_series(series: pd.Series, *, column: str) -> pd.Series:
    return clean_identifier_series(series, column=column, allow_null=True)


def _normalize_year_series(series: pd.Series, *, column: str) -> pd.Series:
    values: list[str | None] = []
    for row, value in series.items():
        cleaned = clean_identifier(value, column=column, row=row, allow_null=True)
        if cleaned is None:
            raise SourceYearError(column=column, value=value, row=row)
        if _FOUR_DIGIT_YEAR.fullmatch(cleaned) is None:
            raise SourceYearError(column=column, value=value, row=row)
        values.append(cleaned)
    return pd.Series(values, index=series.index, dtype="string")


def _normalize_position_series(series: pd.Series, *, table: str) -> pd.Series:
    values: list[int | None] = []
    for row, value in series.items():
        if pd.isna(value) or (isinstance(value, str) and not value.strip()):
            values.append(None)
            continue
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise SemanticValidationError(
                f"{table}.pos at row {row!r} is not an integer"
            ) from exc
        if not math.isfinite(number) or number < 0 or not number.is_integer():
            raise SemanticValidationError(
                f"{table}.pos at row {row!r} must be a non-negative integer"
            )
        values.append(int(number))
    return pd.Series(values, index=series.index, dtype="Int64")


def _lineage(
    frame: pd.DataFrame,
    *,
    spec: ProvenanceSpec,
    row_id: pd.Series,
    parent_row_id: pd.Series,
    pos: pd.Series,
    source_file: pd.Series | None = None,
) -> pd.DataFrame:
    root_id = row_id.where(parent_row_id.isna(), parent_row_id)
    return pd.DataFrame(
        {
            "source_system": spec.source_system,
            "source_run_id": spec.source_run_id,
            "source_record_id": root_id,
            "schema_version": spec.schema_version,
            "source_file": source_file if source_file is not None else spec.source_file,
            "source_kind": spec.source_kind,
            "row_id": row_id,
            "parent_row_id": parent_row_id,
            "pos": pos,
        },
        index=frame.index,
    )


def _ensure_activity_set(
    values: Iterable[object] | None, *, column: str = "activity_code"
) -> set[str] | None:
    if values is None:
        return None
    out: set[str] = set()
    for value in values:
        cleaned = clean_identifier(value, column=column, allow_null=False)
        assert cleaned is not None
        out.add(cleaned)
    return out


def _handle_relationship_failure(
    exc: ApprovalLoaderError,
    *,
    audit: LoaderAudit,
    on_error: ErrorMode,
    table: str,
    reason_code: str,
    key_column: str,
    key_value: object,
) -> bool:
    """Return whether a bad row should be retained; coordinate mismatches fail."""

    if isinstance(exc, CoordinateMismatchError):
        raise exc
    if on_error == "raise":
        raise exc
    audit.add_quarantine(
        table=table,
        reason_code=reason_code,
        key_column=key_column,
        key_value=key_value,
    )
    return False


def _check_unique_root_rows(
    frame: pd.DataFrame,
    *,
    table: str,
    audit: LoaderAudit,
    activity_column: str,
    on_error: ErrorMode,
    activity_codes: set[str] | None,
    gp_column: str | None = None,
    gp_codes: set[str] | None = None,
) -> pd.Series:
    """Validate row identities and optional activity/GP membership.

    ``activity_code`` is deliberately *not* a uniqueness key here.  The
    warehouse only declares ``row_id`` as the primary key for these tables,
    and the same activity can legitimately occur more than once (for example
    when an activity has multiple PP uploads).  Treating a bare activity code
    as globally unique would also be wrong across GPs and plan years.  The
    activity/GP sets, when supplied, are FK-style membership checks only.

    Duplicate detection is checked against ``audit.observed_row_ids``, which
    is updated for every row regardless of whether it is later quarantined
    for an orphan activity/GP.  Checking against ``audit.row_ids`` (accepted
    rows only) would make the result depend on row order: a row_id whose
    first occurrence was quarantined would never be recorded, so a later
    occurrence with a valid activity would load without ever reporting the
    duplicate.
    """

    keep = pd.Series(True, index=frame.index)
    for index, row in frame.iterrows():
        row_id = row["row_id"]
        activity = row[activity_column]
        try:
            if row_id in audit.observed_row_ids:
                raise DuplicateKeyError(table=table, key_column="row_id", value=row_id)
            audit.observed_row_ids.add(row_id)
            if activity_codes is not None and activity not in activity_codes:
                raise OrphanReferenceError(
                    table=table,
                    key_column=activity_column,
                    value=activity,
                    parent="planned_activity",
                )
            if gp_codes is not None and row[gp_column] not in gp_codes:
                raise OrphanReferenceError(
                    table=table,
                    key_column=gp_column,
                    value=row[gp_column],
                    parent="gram_panchayat",
                )
        except ApprovalLoaderError as exc:
            is_gp_orphan = (
                isinstance(exc, OrphanReferenceError) and exc.key_column == gp_column
            )
            reason_code = (
                "duplicate_row_id"
                if isinstance(exc, DuplicateKeyError) and exc.key_column == "row_id"
                else "orphan_gp"
                if is_gp_orphan
                else "orphan_activity"
            )
            keep.loc[index] = _handle_relationship_failure(
                exc,
                audit=audit,
                on_error=on_error,
                table=table,
                reason_code=reason_code,
                key_column=exc.key_column
                if isinstance(exc, (DuplicateKeyError, OrphanReferenceError))
                else activity_column,
                key_value=(
                    row[gp_column]
                    if is_gp_orphan
                    else activity
                    if isinstance(exc, OrphanReferenceError)
                    else row_id
                ),
            )
            continue
        audit.row_ids.add(row_id)
        audit.activity_codes.add(activity)
        audit.activity_by_row_id[row_id] = activity
    return keep


def _read_source_chunks(
    path: str | Path,
    *,
    expected_columns: Sequence[str],
    chunksize: int,
) -> Iterator[pd.DataFrame]:
    # Passing an exact schema and string dtype is the important part: pandas
    # must never infer integer/floating identifiers or reinterpret a comma in
    # a source coordinate as a delimiter chosen by autodetection.
    yield from read_csv_chunks(
        path,
        expected_columns=expected_columns,
        dtype="string",
        chunksize=chunksize,
    )


def iter_admin_approval(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    activity_codes: Iterable[object] | None = None,
    gp_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> Iterator[pd.DataFrame]:
    """Yield target-shaped AA frames while checking global root identities."""

    _validate_spec(spec, expected_kind="AA")
    if on_error not in ("raise", "quarantine"):
        raise ValueError("on_error must be 'raise' or 'quarantine'")
    audit = audit or LoaderAudit()
    allowed_activities = _ensure_activity_set(activity_codes)
    allowed_gps = _ensure_activity_set(gp_codes, column="gp_lgd_code")
    for raw in _read_source_chunks(
        path, expected_columns=AA_SOURCE_COLUMNS, chunksize=chunksize
    ):
        audit.rows_read += len(raw)
        row_id = _required_ids(raw["row_id"], table="admin_approval", column="row_id")
        activity = _required_ids(
            raw["activityCd"], table="admin_approval", column="activityCd"
        )
        gp_lgd_code = _required_ids(
            raw["lgd_code"], table="admin_approval", column="lgd_code"
        )
        parent = pd.Series(pd.NA, index=raw.index, dtype="string")
        pos = pd.Series(pd.NA, index=raw.index, dtype="Int64")
        keep = _check_unique_root_rows(
            pd.DataFrame(
                {"row_id": row_id, "activity_code": activity, "gp_lgd_code": gp_lgd_code},
                index=raw.index,
            ),
            table="admin_approval",
            audit=audit,
            activity_column="activity_code",
            on_error=on_error,
            activity_codes=allowed_activities,
            gp_column="gp_lgd_code",
            gp_codes=allowed_gps,
        )
        date = parse_date_series(
            raw["wrkAdmApprSnctnOrdrDt"],
            date_format="%Y-%m-%d",
            column="adm_approval_sanction_date",
        )
        money = parse_money_series(
            raw["wrkProposedCost"],
            column="work_proposed_cost",
            places=None,
        )
        approval_no = (
            _required_ids(
                raw["wrkAdmApprNo"], table="admin_approval", column="wrkAdmApprNo"
            )
            .str.lstrip("0")
            .replace("", "0")
        )
        lineage = _lineage(
            raw,
            spec=spec,
            row_id=row_id,
            parent_row_id=parent,
            pos=pos,
            source_file=_text_series(raw["source_file"], fallback=spec.source_file),
        )
        out = lineage.assign(
            gp_lgd_code=gp_lgd_code,
            gp_name=_text_series(raw["gram_panchayat_name"]),
            plan_year=_normalize_year_series(raw["plan_year"], column="plan_year"),
            activity_code=activity,
            work_plan_year=_normalize_year_series(
                raw["wrkPlnYr"], column="work_plan_year"
            ),
            adm_approval_no=approval_no,
            adm_approval_sanction_date=date,
            work_proposed_cost=money,
            adm_approval_authority=_text_series(raw["wrkAdmApprIssAuthrty"]),
        )
        out = out.loc[keep].reindex(columns=ADMIN_APPROVAL_COLUMNS)
        audit.rows_loaded += len(out)
        yield out


def iter_admin_approval_scheme(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    parent_index: ApprovalParentIndex | Mapping[object, object] | Iterable[object],
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> Iterator[pd.DataFrame]:
    """Yield AA-scheme child frames after global parent/activity validation."""

    # Canonical normalization treats the nested scheme endpoint as part of
    # the AA source kind.  ``AA_SCHEME`` is accepted as an explicit label for
    # callers that keep endpoint-level provenance instead.
    _validate_spec(spec, expected_kind=("AA", "AA_SCHEME"))
    if on_error not in ("raise", "quarantine"):
        raise ValueError("on_error must be 'raise' or 'quarantine'")
    audit = audit or LoaderAudit()
    if isinstance(parent_index, ApprovalParentIndex):
        parent_ids = set(parent_index.row_ids)
        parent_activity = dict(parent_index.activity_by_row_id)
    elif isinstance(parent_index, Mapping):
        parent_activity = {
            clean_identifier(
                key, column="parent_row_id", allow_null=False
            ): clean_identifier(value, column="activity_code", allow_null=False)
            for key, value in parent_index.items()
        }
        parent_ids = set(parent_activity)
    else:
        parent_activity = {}
        parent_ids = {
            clean_identifier(value, column="parent_row_id", allow_null=False)
            for value in parent_index
        }
    for raw in _read_source_chunks(
        path, expected_columns=AA_SCHEME_SOURCE_COLUMNS, chunksize=chunksize
    ):
        audit.rows_read += len(raw)
        row_id = _required_ids(
            raw["row_id"], table="admin_approval_scheme", column="row_id"
        )
        parent = _required_ids(
            raw["parent_row_id"],
            table="admin_approval_scheme",
            column="parent_row_id",
        )
        activity = _required_ids(
            raw["activityCd"],
            table="admin_approval_scheme",
            column="activityCd",
        )
        pos = _normalize_position_series(raw["pos"], table="admin_approval_scheme")
        keep = pd.Series(True, index=raw.index)
        for index, row in pd.DataFrame(
            {"row_id": row_id, "parent_row_id": parent, "activity_code": activity},
            index=raw.index,
        ).iterrows():
            try:
                if row["row_id"] in audit.observed_row_ids:
                    raise DuplicateKeyError(
                        table="admin_approval_scheme",
                        key_column="row_id",
                        value=row["row_id"],
                    )
                audit.observed_row_ids.add(row["row_id"])
                if row["row_id"] in parent_ids:
                    # A child row ID colliding with an AA parent would make
                    # the two source domains indistinguishable at the
                    # warehouse PK/FK boundary.  The pilot/full-state
                    # extracts contain no such collision, so fail closed if
                    # a revision introduces one.
                    raise DuplicateKeyError(
                        table="admin_approval_scheme",
                        key_column="row_id",
                        value=row["row_id"],
                    )
                if row["parent_row_id"] not in parent_ids:
                    raise OrphanReferenceError(
                        table="admin_approval_scheme",
                        key_column="parent_row_id",
                        value=row["parent_row_id"],
                        parent="admin_approval",
                    )
                expected_activity = parent_activity.get(row["parent_row_id"])
                if (
                    expected_activity is not None
                    and row["activity_code"] != expected_activity
                ):
                    raise SemanticValidationError(
                        "admin_approval_scheme.activityCd does not match its parent activity"
                    )
            except ApprovalLoaderError as exc:
                keep.loc[index] = _handle_relationship_failure(
                    exc,
                    audit=audit,
                    on_error=on_error,
                    table="admin_approval_scheme",
                    reason_code=(
                        "duplicate_row_id"
                        if isinstance(exc, DuplicateKeyError)
                        else "orphan_parent_row_id"
                        if isinstance(exc, OrphanReferenceError)
                        else "parent_activity_mismatch"
                    ),
                    key_column=(
                        exc.key_column
                        if isinstance(exc, (DuplicateKeyError, OrphanReferenceError))
                        else "activityCd"
                    ),
                    key_value=(
                        row["row_id"]
                        if isinstance(exc, DuplicateKeyError)
                        else row["parent_row_id"]
                        if isinstance(exc, OrphanReferenceError)
                        else row["activity_code"]
                    ),
                )
                continue
            audit.row_ids.add(row["row_id"])
        lineage = _lineage(
            raw,
            spec=spec,
            row_id=row_id,
            parent_row_id=parent,
            pos=pos,
        )
        out = lineage.assign(
            activity_code=activity,
            scheme_code=_required_ids(
                raw["wrkSchmCd"],
                table="admin_approval_scheme",
                column="wrkSchmCd",
            ),
            scheme_component_code=_required_ids(
                raw["wrkSchmCmpntCd"],
                table="admin_approval_scheme",
                column="wrkSchmCmpntCd",
            ),
            fund_sanctioned_general=parse_money_series(
                raw["wrkAdmApprFndSnctnGen"],
                column="fund_sanctioned_general",
                places=None,
            ),
            fund_sanctioned_sc=parse_money_series(
                raw["wrkAdmApprFndSnctnSc"],
                column="fund_sanctioned_sc",
                places=None,
            ),
            fund_sanctioned_st=parse_money_series(
                raw["wrkAdmApprFndSnctnSt"],
                column="fund_sanctioned_st",
                places=None,
            ),
            fund_sanctioned_total=parse_money_series(
                raw["fndAllctnSchmTot"],
                column="fund_sanctioned_total",
                places=None,
            ),
        )
        out = out.loc[keep].reindex(columns=ADMIN_APPROVAL_SCHEME_COLUMNS)
        audit.rows_loaded += len(out)
        yield out


def iter_technical_approval(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    activity_codes: Iterable[object] | None = None,
    gp_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> Iterator[pd.DataFrame]:
    """Yield target-shaped TA frames with strict R/N and ISO semantics."""

    _validate_spec(spec, expected_kind="TA")
    if on_error not in ("raise", "quarantine"):
        raise ValueError("on_error must be 'raise' or 'quarantine'")
    audit = audit or LoaderAudit()
    allowed_activities = _ensure_activity_set(activity_codes)
    allowed_gps = _ensure_activity_set(gp_codes, column="gp_lgd_code")
    for raw in _read_source_chunks(
        path, expected_columns=TA_SOURCE_COLUMNS, chunksize=chunksize
    ):
        audit.rows_read += len(raw)
        row_id = _required_ids(
            raw["row_id"], table="technical_approval", column="row_id"
        )
        activity = _required_ids(
            raw["activityCd"], table="technical_approval", column="activityCd"
        )
        gp_lgd_code = _required_ids(
            raw["lgd_code"], table="technical_approval", column="lgd_code"
        )
        keep = _check_unique_root_rows(
            pd.DataFrame(
                {"row_id": row_id, "activity_code": activity, "gp_lgd_code": gp_lgd_code},
                index=raw.index,
            ),
            table="technical_approval",
            audit=audit,
            activity_column="activity_code",
            on_error=on_error,
            activity_codes=allowed_activities,
            gp_column="gp_lgd_code",
            gp_codes=allowed_gps,
        )
        required = _text_series(raw["wrkTecApprReqFlg"])
        bad_required = ~required.isin(["R", "N"])
        if bool(bad_required.any()):
            row = bad_required[bad_required].index[0]
            raise SemanticValidationError(
                f"technical_approval.tec_approval_required at row {row!r} must be R or N"
            )
        n_rows = required.eq("N")
        authority = _text_series(raw["wrkTecApprIssAuthrty"])
        order_no_text = _text_series(raw["wrkTecApprOrdrNo"])

        # A cost is absent for N rows (the source may spell that absence as
        # ``NR``).  For R rows it remains an exact Decimal until a later
        # warehouse projection chooses its planning type.
        cost_text = raw["wrkTecApprCost"].astype("string").str.strip()
        nr_cost = n_rows & cost_text.eq("NR")
        invalid_n_cost = n_rows & ~(
            cost_text.isna() | cost_text.eq("") | nr_cost
        )
        if bool(invalid_n_cost.any()):
            row = invalid_n_cost[invalid_n_cost].index[0]
            raise SemanticValidationError(
                f"technical_approval.tec_approval_cost at row {row!r} must be null when requirement is N"
            )

        # ``NR`` is the source's not-required sentinel.  Preserve it in text
        # columns, while typed money/date columns map that sentinel to null.
        # The source contract says N rows use NR for their text fields and R
        # rows must not carry that sentinel; checking both prevents a source
        # revision from turning a missing approval into a plausible one.
        for column, values in (
            ("wrkTecApprIssAuthrty", authority),
            ("wrkTecApprOrdrNo", order_no_text),
        ):
            invalid_n = n_rows & (values.isna() | values.ne("NR").fillna(True))
            if bool(invalid_n.any()):
                row = invalid_n[invalid_n].index[0]
                raise SemanticValidationError(
                    f"technical_approval.{column} at row {row!r} must be NR when requirement is N"
                )
            invalid_r = required.eq("R") & (values.isna() | values.eq("NR"))
            if bool(invalid_r.any()):
                row = invalid_r[invalid_r].index[0]
                raise RequiredValueError(
                    table="technical_approval", column=column, row=row
                )

        # A non-sentinel nonblank date still has to be strict ISO.  ``NR`` is
        # only accepted for N rows; seeing it on an R row must fail through the
        # shared strict date parser rather than become a null silently.
        order_date_text = raw["wrkTecApprOrdrDt"].astype("string").str.strip()
        nr_date = n_rows & order_date_text.eq("NR")
        invalid_n_date = n_rows & ~(
            order_date_text.isna() | order_date_text.eq("") | nr_date
        )
        if bool(invalid_n_date.any()):
            row = invalid_n_date[invalid_n_date].index[0]
            raise SemanticValidationError(
                f"technical_approval.wrkTecApprOrdrDt at row {row!r} must be NR or blank when requirement is N"
            )
        date_source = raw["wrkTecApprOrdrDt"].mask(nr_date, "")
        cost_source = raw["wrkTecApprCost"].mask(nr_cost, "")
        cost = parse_money_series(
            cost_source, column="tec_approval_cost", places=None
        )
        if bool(cost.loc[n_rows].notna().any()):
            row = cost.loc[n_rows].loc[cost.loc[n_rows].notna()].index[0]
            raise SemanticValidationError(
                f"technical_approval.tec_approval_cost at row {row!r} must be null when requirement is N"
            )
        order_date = parse_date_series(
            date_source,
            date_format="%Y-%m-%d",
            column="tec_approval_order_date",
        )
        lineage = _lineage(
            raw,
            spec=spec,
            row_id=row_id,
            parent_row_id=pd.Series(pd.NA, index=raw.index, dtype="string"),
            pos=pd.Series(pd.NA, index=raw.index, dtype="Int64"),
            source_file=_text_series(raw["source_file"], fallback=spec.source_file),
        )
        out = lineage.assign(
            gp_lgd_code=gp_lgd_code,
            gp_name=_text_series(raw["gram_panchayat_name"]),
            plan_year=_normalize_year_series(raw["plan_year"], column="plan_year"),
            activity_code=activity,
            tec_approval_required=required,
            tec_approval_cost=cost,
            tec_approval_authority=authority,
            tec_approval_order_no=_required_ids(
                order_no_text,
                table="technical_approval",
                column="wrkTecApprOrdrNo",
            )
            .str.lstrip("0")
            .replace("", "0"),
            tec_approval_order_date=order_date,
        )
        out = out.loc[keep].reindex(columns=TECHNICAL_APPROVAL_COLUMNS)
        audit.rows_loaded += len(out)
        yield out


def _coordinates(
    raw: pd.DataFrame,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """Return parsed first coordinates, raw strings, and matched capture counts."""

    lat_values: list[float | None] = []
    lon_values: list[float | None] = []
    lat_raw_values: list[str | None] = []
    lon_raw_values: list[str | None] = []
    counts: list[int] = []
    for index, row in raw.iterrows():
        # Preserve the source capture verbatim.  Stripping only the token
        # used for numeric conversion would lose evidence about a source
        # formatting change; the raw columns are intentionally not cleaned.
        lat_raw = None if pd.isna(row["latitude"]) else str(row["latitude"])
        lon_raw = None if pd.isna(row["longitude"]) else str(row["longitude"])
        lat_parts = [] if lat_raw is None or not lat_raw.strip() else lat_raw.split(",")
        lon_parts = [] if lon_raw is None or not lon_raw.strip() else lon_raw.split(",")
        if len(lat_parts) != len(lon_parts):
            raise CoordinateMismatchError(
                f"physical_progress row {index!r}: latitude has {len(lat_parts)} captures, "
                f"longitude has {len(lon_parts)}"
            )
        counts.append(len(lat_parts))
        lat_raw_values.append(lat_raw)
        lon_raw_values.append(lon_raw)
        if not lat_parts:
            lat_values.append(None)
            lon_values.append(None)
            continue
        try:
            latitude = float(lat_parts[0].strip())
            longitude = float(lon_parts[0].strip())
        except (TypeError, ValueError) as exc:
            raise CoordinateParseError(
                f"physical_progress row {index!r}: first coordinate is not numeric"
            ) from exc
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise CoordinateParseError(
                f"physical_progress row {index!r}: first coordinate is not finite"
            )
        lat_values.append(latitude)
        lon_values.append(longitude)
    return (
        pd.Series(lon_values, index=raw.index, dtype="Float64"),
        pd.Series(lat_values, index=raw.index, dtype="Float64"),
        pd.Series(counts, index=raw.index, dtype="Int64"),
        pd.Series(lon_raw_values, index=raw.index, dtype="string"),
        pd.Series(lat_raw_values, index=raw.index, dtype="string"),
    )


def iter_physical_progress(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    activity_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> Iterator[pd.DataFrame]:
    """Yield only PP upload rows, preserving all coordinate captures/raw text."""

    _validate_spec(spec, expected_kind="PP")
    if on_error not in ("raise", "quarantine"):
        raise ValueError("on_error must be 'raise' or 'quarantine'")
    audit = audit or LoaderAudit()
    allowed_activities = _ensure_activity_set(activity_codes)
    for raw in _read_source_chunks(
        path, expected_columns=PP_UPLOAD_SOURCE_COLUMNS, chunksize=chunksize
    ):
        audit.rows_read += len(raw)
        row_id = _required_ids(
            raw["row_id"], table="physical_progress", column="row_id"
        )
        parent = _required_ids(
            raw["parent_row_id"],
            table="physical_progress",
            column="parent_row_id",
        )
        activity = _required_ids(
            raw["activityCd"], table="physical_progress", column="activityCd"
        )
        keep = _check_unique_root_rows(
            pd.DataFrame(
                {"row_id": row_id, "activity_code": activity}, index=raw.index
            ),
            table="physical_progress",
            audit=audit,
            activity_column="activity_code",
            on_error=on_error,
            activity_codes=allowed_activities,
        )
        # Parent IDs are source relationship metadata for the PP upload
        # endpoint.  They are not treated as AA parents; the stage endpoint is
        # intentionally outside #47's scope.
        longitude, latitude, n_coords, longitude_raw, latitude_raw = _coordinates(raw)
        lineage = _lineage(
            raw,
            spec=spec,
            row_id=row_id,
            parent_row_id=parent,
            pos=_normalize_position_series(raw["pos"], table="physical_progress"),
        )
        out = lineage.assign(
            activity_code=activity,
            file_upload_id=_required_ids(
                raw["fileUploadId"],
                table="physical_progress",
                column="fileUploadId",
            ),
            longitude=longitude,
            latitude=latitude,
            n_coords=n_coords,
            longitude_raw=longitude_raw,
            latitude_raw=latitude_raw,
            plan_unit_type_code=_required_ids(
                raw["plnunttypecode"],
                table="physical_progress",
                column="plnunttypecode",
            ),
        )
        out = out.loc[keep].reindex(columns=PHYSICAL_PROGRESS_COLUMNS)
        audit.rows_loaded += len(out)
        yield out


def _materialize(
    chunks: Iterator[pd.DataFrame], columns: Sequence[str]
) -> pd.DataFrame:
    frames = list(chunks)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).reindex(columns=columns)


def load_admin_approval(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    activity_codes: Iterable[object] | None = None,
    gp_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> pd.DataFrame:
    """Materialise :func:`iter_admin_approval`; use the iterator for scale."""

    return _materialize(
        iter_admin_approval(
            path,
            spec,
            activity_codes=activity_codes,
            gp_codes=gp_codes,
            chunksize=chunksize,
            audit=audit,
            on_error=on_error,
        ),
        ADMIN_APPROVAL_COLUMNS,
    )


def load_admin_approval_with_index(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    activity_codes: Iterable[object] | None = None,
    gp_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> tuple[pd.DataFrame, ApprovalParentIndex]:
    """Materialise AA and return its global parent index for the child stream."""

    audit = audit or LoaderAudit()
    frame = load_admin_approval(
        path,
        spec,
        activity_codes=activity_codes,
        gp_codes=gp_codes,
        chunksize=chunksize,
        audit=audit,
        on_error=on_error,
    )
    return frame, ApprovalParentIndex(
        row_ids=frozenset(audit.row_ids),
        activity_by_row_id=dict(audit.activity_by_row_id),
    )


def load_admin_approval_scheme(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    parent_index: ApprovalParentIndex | Mapping[object, object] | Iterable[object],
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> pd.DataFrame:
    """Materialise :func:`iter_admin_approval_scheme`."""

    return _materialize(
        iter_admin_approval_scheme(
            path,
            spec,
            parent_index=parent_index,
            chunksize=chunksize,
            audit=audit,
            on_error=on_error,
        ),
        ADMIN_APPROVAL_SCHEME_COLUMNS,
    )


def load_technical_approval(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    activity_codes: Iterable[object] | None = None,
    gp_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> pd.DataFrame:
    """Materialise :func:`iter_technical_approval`."""

    return _materialize(
        iter_technical_approval(
            path,
            spec,
            activity_codes=activity_codes,
            gp_codes=gp_codes,
            chunksize=chunksize,
            audit=audit,
            on_error=on_error,
        ),
        TECHNICAL_APPROVAL_COLUMNS,
    )


def load_physical_progress(
    path: str | Path,
    spec: ProvenanceSpec,
    *,
    activity_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    audit: LoaderAudit | None = None,
    on_error: ErrorMode = "raise",
) -> pd.DataFrame:
    """Materialise only the PP upload endpoint; PP stage files are rejected."""

    return _materialize(
        iter_physical_progress(
            path,
            spec,
            activity_codes=activity_codes,
            chunksize=chunksize,
            audit=audit,
            on_error=on_error,
        ),
        PHYSICAL_PROGRESS_COLUMNS,
    )


def load_aa_ta_pp(
    *,
    aa_path: str | Path,
    aa_scheme_path: str | Path,
    ta_path: str | Path,
    pp_upload_path: str | Path,
    aa_spec: ProvenanceSpec,
    aa_scheme_spec: ProvenanceSpec,
    ta_spec: ProvenanceSpec,
    pp_spec: ProvenanceSpec,
    activity_codes: Iterable[object],
    gp_codes: Iterable[object] | None = None,
    chunksize: int = 100_000,
    on_error: ErrorMode = "raise",
) -> AATAPPBundle:
    """Load the four explicit #47 sources in dependency order.

    This convenience function materialises frames.  A production build that
    writes batches should call the four ``iter_*`` functions instead, first
    exhausting AA into a source-payload-free :class:`ApprovalParentIndex`,
    then streaming the child and the two activity-referencing sources.  The
    parent index grows with the number of unique parent keys.  ``gp_codes``
    is optional; when omitted, GP membership is not checked (matching every
    existing caller's behaviour).
    """

    allowed = _ensure_activity_set(activity_codes)
    assert allowed is not None
    aa_audit = LoaderAudit()
    aa, parent_index = load_admin_approval_with_index(
        aa_path,
        aa_spec,
        activity_codes=allowed,
        gp_codes=gp_codes,
        chunksize=chunksize,
        audit=aa_audit,
        on_error=on_error,
    )
    scheme_audit = LoaderAudit()
    scheme = load_admin_approval_scheme(
        aa_scheme_path,
        aa_scheme_spec,
        parent_index=parent_index,
        chunksize=chunksize,
        audit=scheme_audit,
        on_error=on_error,
    )
    ta_audit = LoaderAudit()
    ta = load_technical_approval(
        ta_path,
        ta_spec,
        activity_codes=allowed,
        gp_codes=gp_codes,
        chunksize=chunksize,
        audit=ta_audit,
        on_error=on_error,
    )
    pp_audit = LoaderAudit()
    pp = load_physical_progress(
        pp_upload_path,
        pp_spec,
        activity_codes=allowed,
        chunksize=chunksize,
        audit=pp_audit,
        on_error=on_error,
    )
    return AATAPPBundle(
        tables={
            "admin_approval": aa,
            "admin_approval_scheme": scheme,
            "technical_approval": ta,
            "physical_progress": pp,
        },
        audits={
            "admin_approval": aa_audit,
            "admin_approval_scheme": scheme_audit,
            "technical_approval": ta_audit,
            "physical_progress": pp_audit,
        },
    )


__all__ = [
    "AA_SCHEME_SOURCE_COLUMNS",
    "AA_SOURCE_COLUMNS",
    "ADMIN_APPROVAL_COLUMNS",
    "ADMIN_APPROVAL_SCHEME_COLUMNS",
    "AATAPPBundle",
    "ApprovalLoaderError",
    "ApprovalParentIndex",
    "CoordinateMismatchError",
    "CoordinateParseError",
    "DuplicateKeyError",
    "ErrorMode",
    "LINEAGE_COLUMNS",
    "LoaderAudit",
    "OrphanReferenceError",
    "PHYSICAL_PROGRESS_COLUMNS",
    "PP_UPLOAD_SOURCE_COLUMNS",
    "QuarantineRecord",
    "RequiredValueError",
    "SemanticValidationError",
    "SourceYearError",
    "TA_SOURCE_COLUMNS",
    "TECHNICAL_APPROVAL_COLUMNS",
    "iter_admin_approval",
    "iter_admin_approval_scheme",
    "iter_physical_progress",
    "iter_technical_approval",
    "load_aa_ta_pp",
    "load_admin_approval",
    "load_admin_approval_scheme",
    "load_admin_approval_with_index",
    "load_physical_progress",
    "load_technical_approval",
]
