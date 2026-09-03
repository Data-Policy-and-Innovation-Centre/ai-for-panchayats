"""Strict loader for the flattened eGramSwaraj ``PL.csv`` extract.

The planning extract is a deliberately awkward source boundary.  It is a
single wide CSV whose columns contain plan, activity, and six activity
satellite payloads.  The production file is large enough that reading it as a
single pandas frame is avoidable, while the source contract says that
``activity_code`` is globally unique and that the five non-NSAP satellites
are one-to-one with activities.

This module keeps that boundary independent from :mod:`warehouse.build` and
the existing JSON normalizer.  ``iter_pl_csv`` reads bounded chunks and
retains source-payload-free key state needed for cross-chunk validation; that
state grows with the number of unique activity and plan keys.  ``load_pl_csv``
is a convenience materializer for tests and small extracts; the future build
integration can consume the iterator and write each batch transactionally.

The current warehouse DDL does not contain columns for several real source
extensions (the raw asset/fund/training/community/NSAP payloads and possible
asset-location overflows).  A non-null value in any unmapped source column is
therefore emitted as a reason-coded ``unmapped_extensions``/``quarantine``
row.  No source value is discarded merely because the target table has no
column for it.

No database, build wiring, Box path, or repository ``data/`` path is touched
here.  Callers pass an explicit CSV path and provenance metadata.
"""

from __future__ import annotations

import csv
import json
import numbers
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from itertools import chain
from pathlib import Path

import pandas as pd

from .load_common import (
    CsvSchemaError,
    DateParseError,
    IdentifierError,
    LoaderError,
    MoneyParseError,
    ProvenanceError,
    ProvenanceSpec,
    clean_identifier,
    normalize_fiscal_year,
    parse_date,
    parse_money,
    read_csv_chunks,
    row_provenance,
    validate_columns,
)


class PLLoaderError(LoaderError):
    """Base class for a PL source-contract failure."""


class PLSchemaError(PLLoaderError):
    """The PL header or a source cell cannot satisfy the loader contract."""


class PLIntegrityError(PLLoaderError):
    """A primary-key, foreign-key, or one-to-one invariant is violated."""


class PLDuplicateError(PLIntegrityError):
    """A globally unique activity key occurs more than once."""


# The source names below are the flattened names used by the historical
# eGramSwaraj export.  Canonical spellings are accepted as well because a
# later export/revision may have already applied the rename.  Every alias is
# part of the source contract, not a best-effort fuzzy match.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # identity and context
    "activity_code": (
        "activityCd",
        "activityCode",
        "activity_code",
        "Activity Code",
    ),
    "plan_code": ("planCode", "plan_code", "Plan Code"),
    "gp_lgd_code": (
        "gp_lgd_code",
        "gpCode",
        "gp_code",
        "lgd_code",
        "LGD Code",
    ),
    "gp_name": (
        "gp_name",
        "gpName",
        "gram_panchayat_name",
        "gramPanchayatName",
        "Gram Panchayat Name",
    ),
    "fiscal_year": (
        "fiscal_year",
        "fiscalYear",
        "plan_year",
        "planYear",
        "plan_year_full",
    ),
    "source_file": ("source_file", "sourceFile", "file_name", "filename"),
    # plan fields
    "plan_type": ("plan_typ", "planTyp", "plan_type", "planType"),
    "plan_code_status": (
        "planCodeStts",
        "plan_code_status",
        "planCodeStatus",
    ),
    "approval_date": ("approvalDate", "approval_date"),
    # planned activity fields
    "activity_type": ("activityType", "activity_type"),
    "activity_name": ("activityName", "activity_name"),
    "activity_desc": ("activityDesc", "activity_desc"),
    "focus_area": ("focusArea", "focus_area"),
    "activity_for": ("activityFor", "activity_for"),
    "work_type": ("workTyp", "work_type", "workType"),
    "is_costless_activity": (
        "activityForCostlessFlag",
        "is_costless_activity",
    ),
    "total_cost": ("totalCost", "total_cost"),
    "operation_type": ("operationType", "operation_type"),
    "operation_remarks": ("operationRemarks", "operation_remarks"),
    "output_type": ("outputTyp", "output_type", "outputType"),
    "activity_status": ("activityStts", "activity_status", "activityStatus"),
    "main_asset_category": ("mainAstCtgry", "main_asset_category"),
    "main_asset_subcategory": ("mainAstSubCtgry", "main_asset_subcategory"),
    "main_asset_unit_type": ("mainAstUntTyp", "main_asset_unit_type"),
    "main_asset_unit_count": ("mainAstNumOfUnt", "main_asset_unit_count"),
    # delegation
    "is_delegated": ("dlagtdFlag", "is_delegated"),
    "delegated_unit_code": ("dlagtdPlnUntCd", "delegated_unit_code"),
    "delegated_unit_type": ("dlagtdPlnUntTyp", "delegated_unit_type"),
    "delegated_unit_level": ("dlagtdPlnUntLvl", "delegated_unit_level"),
    "delegated_unit_category": ("dlagtdPlnUntCat", "delegated_unit_category"),
    "is_shareable": ("shareable", "is_shareable"),
    "delegated_parent_unit_code": (
        "dlagtdPerentPlnUntCd",
        "delegated_parent_unit_code",
    ),
    # training
    "training_category_code": (
        "trainingCapacity_trngCatCd",
        "training_category_code",
    ),
    "training_organiser_code": (
        "trainingCapacity_trngOrgByCd",
        "training_organiser_code",
    ),
    "training_subject": ("trainingCapacity_trngSubject", "training_subject"),
    "training_trainees_total": (
        "trainingCapacity_totTrainees",
        "training_trainees_total",
    ),
    "training_duration_days": (
        "trainingCapacity_totDurationDays",
        "training_duration_days",
    ),
    # community service
    "community_service_code": (
        "communityService_serviCd",
        "community_service_code",
    ),
    "community_service_duration": (
        "communityService_serviDuration",
        "community_service_duration",
    ),
    "community_beneficiaries_expected": (
        "communityService_totalexpBeneficiares",
        "community_beneficiaries_expected",
    ),
    # asset details
    "asset_type": ("assetDetails_astTyp", "asset_astTyp", "asset_type"),
    "asset_category": (
        "assetDetails_astCtgry",
        "asset_astCtgry",
        "asset_category",
    ),
    "asset_subcategory": (
        "assetDetails_astSubCtgry",
        "asset_astSubCtgry",
        "asset_subcategory",
    ),
    "asset_coverage_code": (
        "assetDetails_astCvrgCd",
        "asset_astCvrgCd",
        "asset_coverage_code",
    ),
    "asset_name": ("assetDetails_astNm", "asset_astNm", "asset_name"),
    "asset_unit_type": (
        "assetDetails_astUntTyp",
        "asset_astUntTyp",
        "asset_unit_type",
    ),
    "asset_unit_count": (
        "assetDetails_astNumOfUnt",
        "asset_astNumOfUnt",
        "asset_unit_count",
    ),
    "asset_unit_cost": (
        "assetDetails_astUnitCost",
        "asset_astUnitCost",
        "asset_unit_cost",
    ),
    "asset_parameter_type": (
        "assetDetails_astParameterTyp",
        "asset_astParameterTyp",
        "asset_parameter_type",
    ),
    "asset_loc_code": (
        "assetDetails_assetLocationDetails_astLocCd",
        "asset_loc_code",
    ),
    "asset_loc_unit_code": (
        "assetDetails_assetLocationDetails_astPlnUntCd",
        "asset_loc_unit_code",
    ),
    "asset_loc_unit_type": (
        "assetDetails_assetLocationDetails_astPlnUntTyp",
        "asset_loc_unit_type",
    ),
    "asset_loc_unit_count": (
        "assetDetails_assetLocationDetails_astNoOfUnt",
        "asset_loc_unit_count",
    ),
    "asset_loc_unit_cost_total": (
        "assetDetails_assetLocationDetails_astUnitCostTot",
        "asset_loc_unit_cost_total",
    ),
    # fund details
    "fund_scheme_code": ("fundList_schemeCode", "fund_scheme_code"),
    "fund_component_code": ("fundList_componentCode", "fund_component_code"),
    "fund_tied_general": ("fundList_tiedAmountGen", "fund_tied_general"),
    "fund_tied_sc": ("fundList_tiedAmountSc", "fund_tied_sc"),
    "fund_tied_st": ("fundList_tiedAmountSt", "fund_tied_st"),
    "fund_untied_general": ("fundList_untiedAmountGen", "fund_untied_general"),
    "fund_untied_sc": ("fundList_untiedAmountSc", "fund_untied_sc"),
    "fund_untied_st": ("fundList_untiedAmountSt", "fund_untied_st"),
    "fund_amount_total": ("fundList_amountTotal", "fund_amount_total"),
    "fund_tied_abandoned_general": (
        "fundList_tiedAbundonAmountGen",
        "fund_tied_abandoned_general",
    ),
    "fund_tied_abandoned_sc": (
        "fundList_tiedAbundonAmountSc",
        "fund_tied_abandoned_sc",
    ),
    "fund_tied_abandoned_st": (
        "fundList_tiedAbundonAmountSt",
        "fund_tied_abandoned_st",
    ),
    "fund_untied_abandoned_general": (
        "fundList_untiedAbundonAmountGen",
        "fund_untied_abandoned_general",
    ),
    "fund_untied_abandoned_sc": (
        "fundList_untiedAbundonAmountSc",
        "fund_untied_abandoned_sc",
    ),
    "fund_untied_abandoned_st": (
        "fundList_untiedAbundonAmountSt",
        "fund_untied_abandoned_st",
    ),
    # NSAP: the source has a historical ``window`` typo for one field.
    "nsap_old_age_lt80_male": (
        "activityNsap_old_age_below_eighty_male",
        "nsap_old_age_lt80_male",
    ),
    "nsap_old_age_lt80_female": (
        "activityNsap_old_age_below_eighty_female",
        "nsap_old_age_lt80_female",
    ),
    "nsap_old_age_lt80_transgender": (
        "activityNsap_old_age_below_eighty_transgender",
        "nsap_old_age_lt80_transgender",
    ),
    "nsap_old_age_ge80_male": (
        "activityNsap_old_age_greater_eighty_male",
        "nsap_old_age_ge80_male",
    ),
    "nsap_old_age_ge80_female": (
        "activityNsap_old_age_greater_eighty_female",
        "nsap_old_age_ge80_female",
    ),
    "nsap_old_age_ge80_transgender": (
        "activityNsap_old_age_greater_eighty_transgender",
        "nsap_old_age_ge80_transgender",
    ),
    "nsap_disabled_male": (
        "activityNsap_disabled_male",
        "nsap_disabled_male",
    ),
    "nsap_disabled_female": (
        "activityNsap_disabled_female",
        "nsap_disabled_female",
    ),
    "nsap_disabled_transgender": (
        "activityNsap_disabled_transgender",
        "nsap_disabled_transgender",
    ),
    "nsap_widow_male": ("activityNsap_widow_male", "nsap_widow_male"),
    "nsap_widow_female": ("activityNsap_widow_female", "nsap_widow_female"),
    "nsap_widow_transgender": (
        "activityNsap_window_transgender",
        "activityNsap_widow_transgender",
        "nsap_widow_transgender",
    ),
}

# These are real source fields but have no destination column in the current
# DDL.  They are always retained in the extension output when non-null.
RAW_EXTENSION_ALIASES: dict[str, tuple[str, ...]] = {
    "asset_details_raw": ("assetDetails", "asset_details_raw"),
    "asset_loc_overflow_json": (
        "assetDetails_assetLocationDetails",
        "asset_loc_overflow_json",
    ),
    "fund_overflow_json": ("fundList", "fund_overflow_json"),
    "training_capacity_raw": ("trainingCapacity", "training_capacity_raw"),
    "community_service_raw": ("communityService", "community_service_raw"),
    "nsap_raw": (
        "activityNsap",
        "activity_nsap_raw",
        "nsap_raw",
        "pmayg_raw",
    ),
}

PLAN_COLUMNS = (
    "source_system",
    "source_run_id",
    "plan_code",
    "gp_lgd_code",
    "fiscal_year",
    "plan_type",
    "plan_code_status",
    "approval_date",
)
PLANNED_ACTIVITY_COLUMNS = (
    "source_system",
    "source_run_id",
    "activity_code",
    "plan_code",
    "gp_lgd_code",
    "fiscal_year",
    "source_file",
    "activity_type",
    "activity_name",
    "activity_desc",
    "focus_area",
    "activity_for",
    "work_type",
    "is_costless_activity",
    "total_cost",
    "operation_type",
    "operation_remarks",
    "output_type",
    "activity_status",
    "main_asset_category",
    "main_asset_subcategory",
    "main_asset_unit_type",
    "main_asset_unit_count",
)
DELEGATION_COLUMNS = (
    "source_system",
    "source_run_id",
    "activity_code",
    "is_delegated",
    "delegated_unit_code",
    "delegated_unit_type",
    "delegated_unit_level",
    "delegated_unit_category",
    "is_shareable",
    "delegated_parent_unit_code",
)
ASSET_COLUMNS = (
    "source_system",
    "source_run_id",
    "activity_code",
    "asset_type",
    "asset_category",
    "asset_subcategory",
    "asset_coverage_code",
    "asset_name",
    "asset_unit_type",
    "asset_unit_count",
    "asset_unit_cost",
    "asset_parameter_type",
    "asset_loc_code",
    "asset_loc_unit_code",
    "asset_loc_unit_type",
    "asset_loc_unit_count",
    "asset_loc_unit_cost_total",
)
FUND_COLUMNS = (
    "source_system",
    "source_run_id",
    "activity_code",
    "fund_scheme_code",
    "fund_component_code",
    "fund_tied_general",
    "fund_tied_sc",
    "fund_tied_st",
    "fund_untied_general",
    "fund_untied_sc",
    "fund_untied_st",
    "fund_amount_total",
    "fund_tied_abandoned_general",
    "fund_tied_abandoned_sc",
    "fund_tied_abandoned_st",
    "fund_untied_abandoned_general",
    "fund_untied_abandoned_sc",
    "fund_untied_abandoned_st",
)
TRAINING_COLUMNS = (
    "source_system",
    "source_run_id",
    "activity_code",
    "training_category_code",
    "training_organiser_code",
    "training_subject",
    "training_trainees_total",
    "training_duration_days",
)
COMMUNITY_COLUMNS = (
    "source_system",
    "source_run_id",
    "activity_code",
    "community_service_code",
    "community_service_duration",
    "community_beneficiaries_expected",
)
NSAP_COLUMNS = (
    "nsap_id",
    "source_system",
    "source_run_id",
    "activity_code",
    "category",
    "age_band",
    "gender",
    "beneficiary_count",
)

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "plan": PLAN_COLUMNS,
    "planned_activity": PLANNED_ACTIVITY_COLUMNS,
    "activity_asset": ASSET_COLUMNS,
    "activity_fund": FUND_COLUMNS,
    "activity_training": TRAINING_COLUMNS,
    "activity_delegation": DELEGATION_COLUMNS,
    "activity_community_service": COMMUNITY_COLUMNS,
    "activity_nsap": NSAP_COLUMNS,
}
TABLE_ORDER = tuple(TABLE_COLUMNS)
SATELLITE_TABLES = (
    "activity_asset",
    "activity_fund",
    "activity_training",
    "activity_delegation",
    "activity_community_service",
)

# Historical flattened pilot/full-state header.  The loader accepts a
# canonical alias spelling or an explicitly-added extension in addition to
# this contract, but exposing the known header is useful to callers building
# synthetic fixtures and to reviewers checking the documented 87-column
# source.  The optional ``source_file`` field is retained when present so a
# single combined CSV can carry the six original file identities.
PL_SOURCE_COLUMNS = (
    "lgd_code",
    "gram_panchayat_name",
    "planYear",
    "planCode",
    "plan_typ",
    "planCodeStts",
    "approvalDate",
    "activityCd",
    "activityType",
    "activityName",
    "activityDesc",
    "focusArea",
    "activityFor",
    "workTyp",
    "activityForCostlessFlag",
    "totalCost",
    "operationType",
    "operationRemarks",
    "outputTyp",
    "activityStts",
    "dlagtdFlag",
    "dlagtdPlnUntCd",
    "dlagtdPlnUntTyp",
    "dlagtdPlnUntLvl",
    "dlagtdPlnUntCat",
    "shareable",
    "dlagtdPerentPlnUntCd",
    "mainAstCtgry",
    "mainAstSubCtgry",
    "mainAstUntTyp",
    "mainAstNumOfUnt",
    "assetDetails",
    "assetDetails_astTyp",
    "assetDetails_astCtgry",
    "assetDetails_astSubCtgry",
    "assetDetails_astCvrgCd",
    "assetDetails_astNm",
    "assetDetails_astUntTyp",
    "assetDetails_astNumOfUnt",
    "assetDetails_astUnitCost",
    "assetDetails_astParameterTyp",
    "assetDetails_assetLocationDetails",
    "assetDetails_assetLocationDetails_astLocCd",
    "assetDetails_assetLocationDetails_astPlnUntCd",
    "assetDetails_assetLocationDetails_astPlnUntTyp",
    "assetDetails_assetLocationDetails_astNoOfUnt",
    "assetDetails_assetLocationDetails_astUnitCostTot",
    "fundList",
    "fundList_schemeCode",
    "fundList_componentCode",
    "fundList_tiedAmountGen",
    "fundList_tiedAmountSc",
    "fundList_tiedAmountSt",
    "fundList_untiedAmountGen",
    "fundList_untiedAmountSc",
    "fundList_untiedAmountSt",
    "fundList_amountTotal",
    "fundList_tiedAbundonAmountGen",
    "fundList_tiedAbundonAmountSc",
    "fundList_tiedAbundonAmountSt",
    "fundList_untiedAbundonAmountGen",
    "fundList_untiedAbundonAmountSc",
    "fundList_untiedAbundonAmountSt",
    "trainingCapacity",
    "trainingCapacity_trngCatCd",
    "trainingCapacity_trngOrgByCd",
    "trainingCapacity_trngSubject",
    "trainingCapacity_totTrainees",
    "trainingCapacity_totDurationDays",
    "communityService",
    "communityService_serviCd",
    "communityService_serviDuration",
    "communityService_totalexpBeneficiares",
    "activityNsap",
    "activityNsap_old_age_below_eighty_male",
    "activityNsap_old_age_below_eighty_female",
    "activityNsap_old_age_below_eighty_transgender",
    "activityNsap_old_age_greater_eighty_male",
    "activityNsap_old_age_greater_eighty_female",
    "activityNsap_old_age_greater_eighty_transgender",
    "activityNsap_disabled_male",
    "activityNsap_disabled_female",
    "activityNsap_disabled_transgender",
    "activityNsap_widow_male",
    "activityNsap_widow_female",
    "activityNsap_window_transgender",
    "source_file",
)

CODE_FIELDS = frozenset(
    {
        "activity_code",
        "plan_code",
        "gp_lgd_code",
        "focus_area",
        "activity_type",
        "activity_status",
        "work_type",
        "asset_category",
        "asset_type",
        "asset_subcategory",
        "asset_coverage_code",
        "asset_loc_code",
        "asset_loc_unit_code",
        "fund_scheme_code",
        "fund_component_code",
        "training_category_code",
        "training_organiser_code",
        "community_service_code",
        "delegated_unit_code",
        "delegated_parent_unit_code",
    }
)
MONEY_FIELDS = frozenset(
    {
        "total_cost",
        "asset_unit_cost",
        "asset_loc_unit_cost_total",
        "fund_tied_general",
        "fund_tied_sc",
        "fund_tied_st",
        "fund_untied_general",
        "fund_untied_sc",
        "fund_untied_st",
        "fund_amount_total",
        "fund_tied_abandoned_general",
        "fund_tied_abandoned_sc",
        "fund_tied_abandoned_st",
        "fund_untied_abandoned_general",
        "fund_untied_abandoned_sc",
        "fund_untied_abandoned_st",
    }
)
NSAP_FIELD_ORDER = (
    ("nsap_old_age_lt80_male", "old_age", "lt80", "male"),
    ("nsap_old_age_lt80_female", "old_age", "lt80", "female"),
    ("nsap_old_age_lt80_transgender", "old_age", "lt80", "transgender"),
    ("nsap_old_age_ge80_male", "old_age", "ge80", "male"),
    ("nsap_old_age_ge80_female", "old_age", "ge80", "female"),
    ("nsap_old_age_ge80_transgender", "old_age", "ge80", "transgender"),
    ("nsap_disabled_male", "disabled", "na", "male"),
    ("nsap_disabled_female", "disabled", "na", "female"),
    ("nsap_disabled_transgender", "disabled", "na", "transgender"),
    ("nsap_widow_male", "widow", "na", "male"),
    ("nsap_widow_female", "widow", "na", "female"),
    ("nsap_widow_transgender", "widow", "na", "transgender"),
)

# ``read_csv_chunks`` deliberately retains source text so that identifiers
# and literal source extensions are not altered by pandas type inference.
NULL_TEXT = frozenset({"", "na", "n/a", "nan", "none", "null", "<na>", "-"})
FOUR_DIGIT_YEAR = re.compile(r"^(?P<start>\d{4})$")
def _is_nullish(value: object) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, bool) and missing:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in NULL_TEXT
    return False


def _text(value: object) -> str | None:
    if _is_nullish(value):
        return None
    return str(value).strip()


def _same_value(left: object, right: object) -> bool:
    if _is_nullish(left) and _is_nullish(right):
        return True
    if _is_nullish(left) or _is_nullish(right):
        return False
    if isinstance(left, pd.Timestamp) or isinstance(right, pd.Timestamp):
        try:
            return pd.Timestamp(left) == pd.Timestamp(right)
        except (TypeError, ValueError):
            return False
    try:
        equal = left == right
        if isinstance(equal, bool):
            return equal
    except (TypeError, ValueError):
        pass
    return str(left) == str(right)


def _csv_header(path: Path) -> tuple[str, ...]:
    """Read only a CSV header for an explicitly empty source file."""

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle, delimiter=","), None)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CsvSchemaError(f"cannot read CSV header from {path}: {exc}") from exc
    if not header:
        raise CsvSchemaError(f"CSV has no header: {path}")
    return validate_columns(header, source=str(path))


def _expand_fiscal_year(value: object, *, column: str, row: object | None = None) -> str | None:
    """Normalize canonical and documented bare source years to ``YYYY-YYYY``.

    Full-state PL filenames use a bare start year such as ``2020``.  The
    shared helper intentionally rejects short years such as ``2020-21`` and
    this loader preserves that fail-closed contract: no inspected PL source
    establishes the short form as a supported wire value.
    """

    text = _text(value)
    if text is None:
        return None
    match = FOUR_DIGIT_YEAR.fullmatch(text)
    if match:
        start = int(match.group("start"))
        return normalize_fiscal_year(f"{start:04d}-{start + 1:04d}", column=column, row=row)
    return normalize_fiscal_year(text, column=column, row=row)


def _parse_date(value: object, *, column: str, row: object | None, formats: Sequence[str]) -> object | None:
    if _is_nullish(value):
        return None
    errors: list[DateParseError] = []
    for date_format in formats:
        try:
            return parse_date(value, date_format=date_format, column=column)
        except DateParseError as exc:
            errors.append(exc)
    if errors:
        error = errors[-1]
        raise DateParseError(
            column=column,
            date_format=" or ".join(formats),
            source_blank_count=0,
            parsed_null_count=1,
            row=row,
            value=value,
        ) from error
    raise DateParseError(
        column=column,
        date_format=" or ".join(formats),
        source_blank_count=0,
        parsed_null_count=1,
        row=row,
        value=value,
    )


def _parse_money(value: object, *, field: str, row: object | None) -> float | None:
    try:
        decimal_value = parse_money(value, column=field)
    except MoneyParseError as exc:
        # ``parse_money`` already carries the field/value; attach the source
        # row while retaining its typed failure for callers.
        raise MoneyParseError(column=field, value=value, row=row, detail=exc.detail) from exc
    if decimal_value is None:
        return None
    assert isinstance(decimal_value, Decimal)
    try:
        result = float(decimal_value)
    except (OverflowError, ValueError) as exc:
        raise MoneyParseError(
            column=field, value=value, row=row, detail="cannot be represented as a finite float"
        ) from exc
    if not pd.api.types.is_number(result) or not pd.notna(result) or result in (float("inf"), float("-inf")):
        raise MoneyParseError(
            column=field, value=value, row=row, detail="cannot be represented as a finite float"
        )
    return result


def _parse_count(value: object, *, field: str, row: object | None) -> int | None:
    if _is_nullish(value):
        return None
    if isinstance(value, bool):
        raise PLSchemaError(f"{field} at source row {row}: boolean is not a beneficiary count")
    text = str(value).strip().replace(",", "")
    try:
        decimal_value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise PLSchemaError(f"{field} at source row {row}: {value!r} is not an integer") from exc
    if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value() or decimal_value < 0:
        raise PLSchemaError(f"{field} at source row {row}: {value!r} is not a non-negative integer")
    return int(decimal_value)


def _target_value(
    values: Mapping[str, object],
    field: str,
    *,
    row_number: int,
    required: bool = False,
    identifier: bool = False,
    money: bool = False,
    date_formats: Sequence[str] = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%d/%m/%Y"),
) -> object | None:
    value = values.get(field)
    if required and _is_nullish(value):
        raise PLSchemaError(f"{field} is required at source row {row_number}")
    if identifier:
        try:
            cleaned = clean_identifier(value, column=field, row=row_number)
        except IdentifierError as exc:
            raise PLSchemaError(str(exc)) from exc
        if cleaned is not None and cleaned.casefold() in {"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
            raise PLSchemaError(
                f"{field!r} at row {row_number!r}: identifier is not finite"
            )
        return cleaned
    if money:
        return _parse_money(value, field=field, row=row_number)
    if field == "approval_date":
        return _parse_date(value, column=field, row=row_number, formats=date_formats)
    return _text(value)


def _field_aliases_present(header: Sequence[str], aliases: Sequence[str]) -> tuple[str, ...]:
    return tuple(alias for alias in aliases if alias in header)


def _resolve_row_values(row: Mapping[str, object], header: Sequence[str]) -> dict[str, object]:
    """Resolve aliases and reject conflicting duplicate source spellings."""

    values: dict[str, object] = {}
    for field_name, aliases in FIELD_ALIASES.items():
        present = _field_aliases_present(header, aliases)
        candidates = [(name, row.get(name)) for name in present if not _is_nullish(row.get(name))]
        if len(candidates) > 1:
            first_name, first = candidates[0]
            for other_name, other in candidates[1:]:
                if not _same_value(first, other):
                    raise PLSchemaError(
                        f"source row has conflicting aliases for {field_name!r}: "
                        f"{first_name}={first!r}, {other_name}={other!r}"
                    )
        values[field_name] = candidates[0][1] if candidates else None
    for field_name, aliases in RAW_EXTENSION_ALIASES.items():
        present = _field_aliases_present(header, aliases)
        candidates = [(name, row.get(name)) for name in present if not _is_nullish(row.get(name))]
        if len(candidates) > 1:
            first_name, first = candidates[0]
            for other_name, other in candidates[1:]:
                if not _same_value(first, other):
                    raise PLSchemaError(
                        f"source row has conflicting aliases for extension {field_name!r}: "
                        f"{first_name}={first!r}, {other_name}={other!r}"
                    )
        values[field_name] = candidates[0][1] if candidates else None
    return values


def _source_columns_known() -> frozenset[str]:
    known = {
        "source_system",
        "source_run_id",
        "source_file",
        "schema_version",
        "row_id",
        "source_record_id",
        "source_row_number",
    }
    for aliases in chain(FIELD_ALIASES.values(), RAW_EXTENSION_ALIASES.values()):
        known.update(aliases)
    return frozenset(known)


KNOWN_SOURCE_COLUMNS = _source_columns_known()
REQUIRED_FIELDS = ("activity_code", "plan_code")


def _extension_reason(name: str) -> tuple[str, str, str]:
    lowered = name.casefold()
    if "asset" in lowered and "loc" in lowered:
        return (
            "unmapped_asset_location_overflow",
            "source asset-location payload is not represented by the one-row target DDL",
            "activity_asset",
        )
    if "asset" in lowered:
        return (
            "unmapped_asset_extension",
            "source asset payload field has no target DDL column",
            "activity_asset",
        )
    if "fund" in lowered:
        return (
            "unmapped_fund_extension",
            "source fund payload field has no target DDL column",
            "activity_fund",
        )
    if "training" in lowered:
        return (
            "unmapped_training_extension",
            "source training payload field has no target DDL column",
            "activity_training",
        )
    if "community" in lowered:
        return (
            "unmapped_community_extension",
            "source community-service payload field has no target DDL column",
            "activity_community_service",
        )
    if "nsap" in lowered or "pmay" in lowered:
        return (
            "unmapped_nsap_extension",
            "source NSAP/PMAY-G payload field has no target DDL column",
            "activity_nsap",
        )
    return (
        "unmapped_source_extension",
        "source field is not mapped by the PL source contract",
        "planned_activity",
    )


EXTENSION_COLUMNS = (
    "source_system",
    "source_run_id",
    "schema_version",
    "source_kind",
    "source_file",
    "source_row_number",
    "source_record_id",
    "row_id",
    "parent_row_id",
    "pos",
    "activity_code",
    "gp_lgd_code",
    "gp_name",
    "fiscal_year",
    "plan_year",
    "business_id",
    "table_name",
    "extension_name",
    "raw_value",
    "reason_code",
    "reason",
    "key_column",
    "key_value",
    "row_count",
    "mapping_status",
)

# Canonical Parquet carries these lineage fields even when the current
# warehouse DDL does not.  Keep them on every emitted frame so a future
# transactional integration cannot lose provenance merely by selecting a
# satellite table.
PROVENANCE_EXTRA_COLUMNS = (
    "row_id",
    "parent_row_id",
    "pos",
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
    "source_row_number",
)


def _output_columns(target_columns: Sequence[str]) -> tuple[str, ...]:
    """Target columns followed by lineage not represented in the DDL."""

    return tuple(dict.fromkeys((*target_columns, *PROVENANCE_EXTRA_COLUMNS)))


def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


@dataclass(frozen=True, slots=True)
class PLBatch:
    """One bounded output batch from :func:`iter_pl_csv`."""

    tables: Mapping[str, pd.DataFrame]
    unmapped_extensions: pd.DataFrame

    @property
    def quarantine(self) -> pd.DataFrame:
        return self.unmapped_extensions

    def __getitem__(self, name: str) -> pd.DataFrame:
        if name in self.tables:
            return self.tables[name]
        if name in {"unmapped_extensions", "quarantine"}:
            return self.unmapped_extensions
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class PLLoadResult:
    """Materialized PL tables plus explicit source-extension quarantine."""

    tables: Mapping[str, pd.DataFrame]
    unmapped_extensions: pd.DataFrame
    source_rows: int
    nsap_empty_asserted: bool

    @property
    def quarantine(self) -> pd.DataFrame:
        """Alias matching the warehouse's reason-coded quarantine concept."""

        return self.unmapped_extensions

    @property
    def counts(self) -> dict[str, int]:
        return {name: int(len(frame)) for name, frame in self.tables.items()}

    @property
    def quarantine_count(self) -> int:
        return int(len(self.unmapped_extensions))

    def __getitem__(self, name: str) -> pd.DataFrame:
        if name in self.tables:
            return self.tables[name]
        if name in {"unmapped_extensions", "quarantine"}:
            return self.unmapped_extensions
        raise KeyError(name)

    def keys(self):
        return (*self.tables.keys(), "unmapped_extensions", "quarantine")

    def items(self):
        return tuple(self.tables.items()) + (
            ("unmapped_extensions", self.unmapped_extensions),
            ("quarantine", self.unmapped_extensions),
        )


class PLLoader:
    """Stateless convenience facade for the chunked PL iterator.

    Unlike the voucher loader, PL validation state is local to one iterator,
    so an instance may be reused for separate files.  The method returns
    :class:`PLBatch` objects; callers that need a small materialized result
    should use :func:`load_pl_csv`.
    """

    def load_csv(self, path: str | Path, **kwargs: object) -> Iterator[PLBatch]:
        return iter_pl_csv(path, **kwargs)

    def load_pl_csv(self, path: str | Path, **kwargs: object) -> Iterator[PLBatch]:
        return self.load_csv(path, **kwargs)

    def load(self, path: str | Path, **kwargs: object) -> Iterator[PLBatch]:
        return self.load_csv(path, **kwargs)

    def __enter__(self) -> "PLLoader":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


@dataclass
class _PLState:
    spec: ProvenanceSpec
    header: tuple[str, ...]
    date_formats: tuple[str, ...]
    next_nsap_id: int
    plans: dict[str, dict[str, object]] = field(default_factory=dict)
    # Keep only the activity key and its plan reference in stream state.  A
    # full activity row belongs in the bounded output batch, not in the
    # cross-chunk index (which would make the full-state file a 4M-row Python
    # object).  The materializer intentionally retains output frames, while
    # ``iter_pl_csv`` retains only key/index state, which still grows with
    # the number of unique activities and must move to disk-backed staging in
    # the full-state #50 integration.
    activities: dict[str, tuple[int, str]] = field(default_factory=dict)
    source_rows: int = 0
    nsap_positive_or_nonzero: bool = False
    satellite_counts: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in SATELLITE_TABLES}
    )

    def validate_header(self) -> None:
        missing_alias_fields = [
            field for field in REQUIRED_FIELDS
            if not _field_aliases_present(self.header, FIELD_ALIASES[field])
        ]
        if missing_alias_fields:
            raise PLSchemaError(
                "PL.csv is missing required field(s): "
                + ", ".join(missing_alias_fields)
                + "; accepted aliases are "
                + ", ".join(
                    f"{field}={FIELD_ALIASES[field]!r}" for field in missing_alias_fields
                )
            )

    def _context(self, values: Mapping[str, object], row_number: int) -> tuple[str, str | None, str]:
        gp_value = values.get("gp_lgd_code")
        if _is_nullish(gp_value):
            gp_value = self.spec.gp_code
        try:
            gp_code = clean_identifier(gp_value, column="gp_lgd_code", row=row_number, allow_null=False)
        except IdentifierError as exc:
            raise PLSchemaError(str(exc)) from exc
        if gp_code.casefold() in {"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
            raise PLSchemaError(
                f"gp_lgd_code at row {row_number!r}: identifier is not finite"
            )

        gp_name = _text(values.get("gp_name")) or _text(self.spec.gp_name)
        fiscal_value = values.get("fiscal_year")
        if _is_nullish(fiscal_value):
            fiscal_value = self.spec.fiscal_year
        fiscal_year = _expand_fiscal_year(fiscal_value, column="fiscal_year", row=row_number)
        if fiscal_year is None:
            raise PLSchemaError(f"fiscal_year is required at source row {row_number}")
        return gp_code, gp_name, fiscal_year

    def _root_provenance(
        self,
        *,
        row_number: int,
        activity_code: str,
        gp_code: str,
        gp_name: str | None,
        fiscal_year: str,
        source_file: str,
    ) -> dict[str, object]:
        result = row_provenance(
            source_system=self.spec.source_system,
            source_run_id=self.spec.source_run_id,
            source_file=source_file,
            source_row_number=row_number,
            source_kind=self.spec.source_kind,
            schema_version=self.spec.schema_version,
            gp_code=gp_code,
            gp_name=gp_name,
            fiscal_year=fiscal_year,
            business_id=activity_code,
            mapping_status="mapped",
        )
        result["source_row_number"] = row_number
        return result

    def _child_provenance(
        self, root: Mapping[str, object], *, position: int = 0, child_collection: str
    ) -> dict[str, object]:
        # ``child_collection`` mirrors normalize.py's sanitized JSON array
        # key: it folds the collection identity into the row ID so two
        # collections don't collide at the same position under the same
        # parent activity. PL source rows are a flat CSV rather than nested
        # JSON, so there is no literal source array key to sanitize; the
        # destination table name already uniquely names each nested entity
        # group (asset/fund/training/delegation/community/nsap) per
        # activity, so it is used as the collection key directly.
        result = row_provenance(
            source_system=self.spec.source_system,
            source_run_id=self.spec.source_run_id,
            source_file=str(root["source_file"]),
            source_row_number=int(root["source_row_number"]),
            source_kind=self.spec.source_kind,
            schema_version=self.spec.schema_version,
            gp_code=root.get("gp_code"),
            gp_name=root.get("gram_panchayat_name"),
            fiscal_year=root.get("fiscal_year"),
            business_id=root.get("business_id"),
            parent_row_id=root.get("row_id"),
            position=position,
            child_collection=child_collection,
            mapping_status="mapped",
        )
        result["source_row_number"] = root["source_row_number"]
        return result

    def _record_extension(
        self,
        *,
        name: str,
        value: object,
        row_number: int,
        root: Mapping[str, object],
    ) -> dict[str, object]:
        reason_code, reason, table_name = _extension_reason(name)
        raw_value = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        activity_code = root.get("business_id")
        return {
            "source_system": self.spec.source_system,
            "source_run_id": self.spec.source_run_id,
            "schema_version": self.spec.schema_version,
            "source_kind": self.spec.source_kind,
            "source_file": root.get("source_file"),
            "source_row_number": row_number,
            "source_record_id": root.get("source_record_id"),
            "row_id": root.get("row_id"),
            "parent_row_id": None,
            "pos": None,
            "activity_code": activity_code,
            "gp_lgd_code": root.get("gp_code"),
            "gp_name": root.get("gram_panchayat_name"),
            "fiscal_year": root.get("fiscal_year"),
            "plan_year": root.get("plan_year"),
            "business_id": activity_code,
            "table_name": table_name,
            "extension_name": name,
            "raw_value": raw_value,
            "reason_code": reason_code,
            "reason": reason,
            "key_column": "activity_code",
            "key_value": activity_code,
            "row_count": 1,
            "mapping_status": "unmapped",
        }

    def _extensions(
        self, row: Mapping[str, object], root: Mapping[str, object], row_number: int
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for name in self.header:
            if name in KNOWN_SOURCE_COLUMNS:
                continue
            value = row.get(name)
            if not _is_nullish(value):
                records.append(
                    self._record_extension(name=name, value=value, row_number=row_number, root=root)
                )
        for _canonical_name, aliases in RAW_EXTENSION_ALIASES.items():
            # Resolve the raw cell again rather than using the canonical
            # trimmed value.  Extension output is an audit copy and should
            # preserve the source text exactly, including whitespace.
            for name in aliases:
                value = row.get(name)
                if not _is_nullish(value):
                    records.append(
                        self._record_extension(name=name, value=value, row_number=row_number, root=root)
                    )
        return records

    def process_chunk(self, chunk: pd.DataFrame, *, start_row_number: int) -> PLBatch:
        output: dict[str, list[dict[str, object]]] = {name: [] for name in TABLE_ORDER}
        extension_rows: list[dict[str, object]] = []
        for offset, raw in enumerate(chunk.to_dict("records")):
            row_number = start_row_number + offset
            self.source_rows += 1
            values = _resolve_row_values(raw, self.header)
            activity_code = _target_value(
                values, "activity_code", row_number=row_number, required=True, identifier=True
            )
            plan_code = _target_value(
                values, "plan_code", row_number=row_number, required=True, identifier=True
            )
            assert isinstance(activity_code, str)
            assert isinstance(plan_code, str)
            gp_code, gp_name, fiscal_year = self._context(values, row_number)
            row_source_file = _text(values.get("source_file")) or self.spec.source_file
            root = self._root_provenance(
                row_number=row_number,
                activity_code=activity_code,
                gp_code=gp_code,
                gp_name=gp_name,
                fiscal_year=fiscal_year,
                source_file=row_source_file,
            )
            if activity_code in self.activities:
                previous = self.activities[activity_code][0]
                raise PLDuplicateError(
                    f"planned_activity.activity_code {activity_code!r} occurs at source "
                    f"rows {previous} and {row_number}; the source key must be globally unique"
                )

            planned = {
                **root,
                "source_system": self.spec.source_system,
                "source_run_id": self.spec.source_run_id,
                "activity_code": activity_code,
                "plan_code": plan_code,
                "gp_lgd_code": gp_code,
                "fiscal_year": fiscal_year,
                "source_file": row_source_file,
            }
            for field_name in PLANNED_ACTIVITY_COLUMNS:
                if field_name in planned:
                    continue
                planned[field_name] = _target_value(
                    values,
                    field_name,
                    row_number=row_number,
                    identifier=field_name in CODE_FIELDS,
                    money=field_name in MONEY_FIELDS,
                    date_formats=self.date_formats,
                )
            # The root provenance uses the activity code as business_id; keep
            # the canonical frame's target values authoritative.
            planned = {name: planned.get(name) for name in PLANNED_ACTIVITY_COLUMNS} | {
                key: root_value
                for key, root_value in root.items()
                if key not in PLANNED_ACTIVITY_COLUMNS
            }
            self.activities[activity_code] = (row_number, plan_code)
            output["planned_activity"].append(planned)

            plan_row = {
                **root,
                "source_system": self.spec.source_system,
                "source_run_id": self.spec.source_run_id,
                "plan_code": plan_code,
                "gp_lgd_code": gp_code,
                "fiscal_year": fiscal_year,
            }
            for field_name in PLAN_COLUMNS:
                if field_name in plan_row:
                    continue
                if field_name == "approval_date":
                    plan_row[field_name] = _target_value(
                        values,
                        field_name,
                        row_number=row_number,
                        date_formats=self.date_formats,
                    )
                else:
                    plan_row[field_name] = _target_value(values, field_name, row_number=row_number)
            plan_business_fields = (
                "gp_lgd_code",
                "fiscal_year",
                "plan_type",
                "plan_code_status",
                "approval_date",
            )
            existing_plan = self.plans.get(plan_code)
            if existing_plan is None:
                self.plans[plan_code] = plan_row
                output["plan"].append(plan_row)
            elif any(not _same_value(existing_plan[field], plan_row[field]) for field in plan_business_fields):
                raise PLIntegrityError(
                    f"plan.plan_code {plan_code!r} has conflicting values at source row "
                    f"{row_number}; plan keys may repeat across activities only when all "
                    "plan attributes agree"
                )

            for table_name, fields in (
                ("activity_asset", ASSET_COLUMNS[3:]),
                ("activity_fund", FUND_COLUMNS[3:]),
                ("activity_training", TRAINING_COLUMNS[3:]),
                ("activity_delegation", DELEGATION_COLUMNS[3:]),
                ("activity_community_service", COMMUNITY_COLUMNS[3:]),
            ):
                child = self._child_provenance(root, child_collection=table_name)
                child.update({
                    "source_system": self.spec.source_system,
                    "source_run_id": self.spec.source_run_id,
                    "activity_code": activity_code,
                })
                for field_name in fields:
                    child[field_name] = _target_value(
                        values,
                        field_name,
                        row_number=row_number,
                        identifier=field_name in CODE_FIELDS,
                        money=field_name in MONEY_FIELDS,
                        date_formats=self.date_formats,
                    )
                output[table_name].append(child)
                self.satellite_counts[table_name] += 1

            nsap_values: list[dict[str, object]] = []
            for nsap_position, (field_name, category, age_band, gender) in enumerate(
                NSAP_FIELD_ORDER
            ):
                value = _parse_count(values.get(field_name), field=field_name, row=row_number)
                if value is None:
                    continue
                self.nsap_positive_or_nonzero = True
                if value == 0:
                    continue
                child = self._child_provenance(
                    root, position=nsap_position, child_collection="activity_nsap"
                )
                child.update({
                    "source_system": self.spec.source_system,
                    "source_run_id": self.spec.source_run_id,
                    "activity_code": activity_code,
                    "category": category,
                    "age_band": age_band,
                    "gender": gender,
                    "beneficiary_count": value,
                    "nsap_id": self.next_nsap_id,
                })
                self.next_nsap_id += 1
                nsap_values.append(child)
            output["activity_nsap"].extend(nsap_values)
            extension_rows.extend(self._extensions(raw, root, row_number))

        frames = {
            name: pd.DataFrame(output[name], columns=list(_output_columns(TABLE_COLUMNS[name])))
            for name in TABLE_ORDER
        }
        extensions = pd.DataFrame(extension_rows, columns=list(EXTENSION_COLUMNS))
        return PLBatch(frames, extensions)

    def finish(self) -> None:
        activity_codes = set(self.activities)
        plan_codes = set(self.plans)
        for activity_code, (_row_number, plan_code) in self.activities.items():
            if plan_code not in plan_codes:
                raise PLIntegrityError(
                    f"planned_activity.activity_code {activity_code!r} references missing "
                    f"plan_code {plan_code!r}"
                )
        # Each non-empty PL row must produce exactly one row in each of the
        # five one-to-one satellites.  This is checked globally, not per
        # chunk, so a boundary cannot hide a missing or duplicated child.
        for table_name in SATELLITE_TABLES:
            if self.satellite_counts[table_name] != len(activity_codes):
                raise PLIntegrityError(
                    f"{table_name} has {self.satellite_counts[table_name]} rows for "
                    f"{len(activity_codes)} planned_activity rows"
                )


def _prepare_spec(
    path: Path,
    *,
    spec: ProvenanceSpec | None,
    source_system: str | None,
    source_run_id: str | None,
    schema_version: str,
    source_file: str | None,
    gp_code: str | None,
    gp_name: str | None,
    fiscal_year: str | None,
) -> ProvenanceSpec:
    if spec is not None:
        if any(value is not None for value in (source_system, source_run_id, source_file, gp_code, gp_name, fiscal_year)):
            raise ProvenanceError("pass either spec or individual provenance arguments, not both")
        spec.validate()
        if spec.source_kind != "PL":
            raise ProvenanceError(
                f"source_kind must be 'PL' for the planning loader, got {spec.source_kind!r}"
            )
        return spec
    if source_system is None or source_run_id is None:
        raise ProvenanceError("source_system and source_run_id are required when spec is omitted")
    return ProvenanceSpec(
        source_system=source_system,
        source_run_id=source_run_id,
        source_file=source_file or path.name,
        source_kind="PL",
        schema_version=schema_version,
        gp_code=gp_code,
        gp_name=gp_name,
        fiscal_year=fiscal_year,
    )


def iter_pl_csv(
    path: str | Path,
    *,
    spec: ProvenanceSpec | None = None,
    source_system: str | None = None,
    source_run_id: str | None = None,
    schema_version: str = "1",
    source_file: str | None = None,
    gp_code: str | None = None,
    gp_name: str | None = None,
    fiscal_year: str | None = None,
    chunk_size: int = 100_000,
    date_formats: Sequence[str] = (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y",
    ),
    start_nsap_id: int = 1,
) -> Iterator[PLBatch]:
    """Yield bounded table batches from an explicit PL CSV path.

    Batch payload memory is bounded by ``chunk_size``; the global key indexes
    grow with unique activities/plans so cross-chunk duplicates fail closed.
    ``activity_code`` and ``plan_code`` are required source identities.  GP
    and fiscal-year context may be supplied per row through known aliases or
    as constants in :class:`ProvenanceSpec`; at least one must resolve for
    every row.  The iterator raises before yielding the offending batch when
    a source cell, duplicate key, or relationship is invalid.
    """

    csv_path = Path(path)
    if not isinstance(start_nsap_id, numbers.Integral) or isinstance(start_nsap_id, bool) or start_nsap_id < 1:
        raise PLSchemaError("start_nsap_id must be a positive integer")
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise PLSchemaError("chunk_size must be a positive integer")
    if isinstance(date_formats, str) or not date_formats or any(
        not isinstance(value, str) or not value for value in date_formats
    ):
        raise PLSchemaError("date_formats must contain at least one non-empty format")
    provenance = _prepare_spec(
        csv_path,
        spec=spec,
        source_system=source_system,
        source_run_id=source_run_id,
        schema_version=schema_version,
        source_file=source_file,
        gp_code=gp_code,
        gp_name=gp_name,
        fiscal_year=fiscal_year,
    )
    chunks = read_csv_chunks(csv_path, dtype="string", chunksize=chunk_size)
    try:
        first = next(chunks)
    except StopIteration:
        header = _csv_header(csv_path)
        state = _PLState(provenance, header, tuple(date_formats), int(start_nsap_id))
        state.validate_header()
        state.finish()
        return
    header = tuple(str(column) for column in first.columns)
    state = _PLState(provenance, header, tuple(date_formats), int(start_nsap_id))
    state.validate_header()
    # Match the conventional CSV line number used by the other source
    # loaders: header is line 1, so the first logical data record is row 2.
    # Embedded newlines remain one logical record because the shared reader
    # parses CSV framing rather than counting physical newline characters.
    row_number = 2
    yield state.process_chunk(first, start_row_number=row_number)
    row_number += len(first)
    for chunk in chunks:
        yield state.process_chunk(chunk, start_row_number=row_number)
        row_number += len(chunk)
    state.finish()


def _materialize_empty_tables() -> dict[str, pd.DataFrame]:
    return {name: _empty_frame(_output_columns(columns)) for name, columns in TABLE_COLUMNS.items()}


def load_pl_csv(
    path: str | Path,
    *,
    spec: ProvenanceSpec | None = None,
    source_system: str | None = None,
    source_run_id: str | None = None,
    schema_version: str = "1",
    source_file: str | None = None,
    gp_code: str | None = None,
    gp_name: str | None = None,
    fiscal_year: str | None = None,
    chunk_size: int = 100_000,
    date_formats: Sequence[str] = (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y",
    ),
    start_nsap_id: int = 1,
) -> PLLoadResult:
    """Materialize all PL tables after streaming and global validation.

    This helper is intentionally separate from :func:`iter_pl_csv`: tests and
    small pilot extracts can use a convenient result object, while #50 can
    consume the bounded iterator and insert batches transactionally.
    """

    tables = _materialize_empty_tables()
    table_parts: dict[str, list[pd.DataFrame]] = {name: [] for name in TABLE_ORDER}
    extensions: list[pd.DataFrame] = []
    source_rows = 0
    for batch in iter_pl_csv(
        path,
        spec=spec,
        source_system=source_system,
        source_run_id=source_run_id,
        schema_version=schema_version,
        source_file=source_file,
        gp_code=gp_code,
        gp_name=gp_name,
        fiscal_year=fiscal_year,
        chunk_size=chunk_size,
        date_formats=date_formats,
        start_nsap_id=start_nsap_id,
    ):
        source_rows += len(batch.tables["planned_activity"])
        for name in TABLE_ORDER:
            if len(batch.tables[name]):
                table_parts[name].append(batch.tables[name])
        if len(batch.unmapped_extensions):
            extensions.append(batch.unmapped_extensions)

    if extensions:
        extension_frame = pd.concat(extensions, ignore_index=True)
    else:
        extension_frame = _empty_frame(EXTENSION_COLUMNS)
    for name in TABLE_ORDER:
        if table_parts[name]:
            tables[name] = pd.concat(table_parts[name], ignore_index=True)

    for name in TABLE_ORDER:
        frame = tables[name]
        if name == "plan":
            continue
        key = "nsap_id" if name == "activity_nsap" else "activity_code"
        if frame[key].duplicated().any():
            raise PLIntegrityError(f"{name}.{key} contains duplicate keys after materialization")
    activity_codes = set(tables["planned_activity"]["activity_code"].dropna())
    plan_codes = set(tables["plan"]["plan_code"].dropna())
    if not set(tables["planned_activity"]["plan_code"].dropna()) <= plan_codes:
        raise PLIntegrityError("planned_activity contains a plan_code absent from plan")
    for name in SATELLITE_TABLES:
        codes = set(tables[name]["activity_code"].dropna())
        if codes != activity_codes or len(tables[name]) != len(tables["planned_activity"]):
            raise PLIntegrityError(
                f"{name} must be one-to-one with planned_activity: "
                f"activities={len(activity_codes)}, satellite_rows={len(tables[name])}, "
                f"missing={sorted(activity_codes - codes)!r}, orphan={sorted(codes - activity_codes)!r}"
            )
    nsap_empty_asserted = len(tables["activity_nsap"]) == 0 and not any(
        bool(pd.notna(value)) and not _is_nullish(value)
        for value in tables["activity_nsap"]["beneficiary_count"]
    )
    return PLLoadResult(tables, extension_frame, source_rows, nsap_empty_asserted)


# Short aliases make the source-specific entry point easy to discover while
# retaining the explicit names used in documentation and tests.
load_pl = load_pl_csv
PLCSVLoader = PLLoader
PL_TABLE_COLUMNS = {name: _output_columns(columns) for name, columns in TABLE_COLUMNS.items()}
PL_BATCH_COLUMNS = PL_TABLE_COLUMNS


__all__ = [
    "ASSET_COLUMNS",
    "COMMUNITY_COLUMNS",
    "DELEGATION_COLUMNS",
    "EXTENSION_COLUMNS",
    "FIELD_ALIASES",
    "FUND_COLUMNS",
    "NSAP_COLUMNS",
    "PLAN_COLUMNS",
    "PLANNED_ACTIVITY_COLUMNS",
    "PLBatch",
    "PL_BATCH_COLUMNS",
    "PLCSVLoader",
    "PLDuplicateError",
    "PLIntegrityError",
    "PLLoadResult",
    "PLLoader",
    "PLLoaderError",
    "PLSchemaError",
    "PL_SOURCE_COLUMNS",
    "PL_TABLE_COLUMNS",
    "SATELLITE_TABLES",
    "TABLE_COLUMNS",
    "TABLE_ORDER",
    "TRAINING_COLUMNS",
    "iter_pl_csv",
    "load_pl",
    "load_pl_csv",
]
