"""Canonical Parquet frames to warehouse-shaped frames.

Every function here is pure: frames in, frames out, no database and no file
system. A row that cannot be kept is returned as a reason-coded quarantine
record rather than dropped, following PR #9's convention
(``origin/Abhigyan_database:src/database/transform.py``), so a build can
always account for the difference between rows read and rows loaded.

Field-name mappings below are mined from PR #30
(``origin/Abhigyan_Schema_Tables:src/schema_tables/transform.py``), which
lists the raw eGramSwaraj webservice field names verbatim (its own docstrings
say so explicitly). Two confidence tiers:

* PL, AA, TA, PP renames are corroborated by *both* donor PRs independently
  and are used directly.
* RE (activity_expenditure, née recommended_expenditure) renames are
  inferred by analogy to the same API family (PR #30 only ever saw RE via an
  already-renamed CSV export, never the JSON webservice the gated normalizer
  consumes), so ``RE_CANDIDATES`` lists several plausible spellings per field
  and resolves the first one present. This is stated plainly rather than
  guessed silently: every field's resolution -- which candidate matched, or
  that none did -- is recorded in a :class:`FieldResolutions` log and
  surfaced on :class:`warehouse.build.BuildResult`. ``plan_code`` and
  ``s_no`` are part of the documented ``activity_expenditure`` identity
  ``(gp_lgd_code, plan_code, activity_code, s_no)``, so if *no* candidate
  spelling for either is present in the source frame, resolution raises
  :class:`RequiredFieldUnresolved` rather than silently producing an
  all-null identity column; every other RE field is genuinely optional and
  is allowed to resolve to null, but that outcome is still recorded. Every
  row surviving this identity check is also assigned an integer
  ``expenditure_id`` surrogate (the table's real primary key -- see
  ``schema.py``), via a caller-supplied ``start_id`` so ids stay unique
  across every snapshot loaded into one build (see ``build.populate``).

Where the normalizer turns a JSON list into a child table (``pl__fundlist``,
``pl__assetdetails``, ``aa__...``), the corresponding table here is keyed by
``activity_code`` and is strictly 1:1 with ``planned_activity``: the real
``activity_asset``/``activity_fund`` tables carry no per-row identity of
their own (no ``row_id`` column), unlike an earlier revision of this module
that modeled them one-to-many, keyed by an invented ``row_id``, on the
theory that a repeated funding scheme or asset line needed its own row. A
second line for the same activity is now treated the same as any other
conflicting duplicate: quarantined, not kept as a second row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

from .clean import strip_leading_zeros, to_code, to_datetime, to_decimal_money, to_int

# --------------------------------------------------------------------- quarantine


@dataclass
class Quarantine:
    """Rows that could not be loaded, and why."""

    records: list[dict] = field(default_factory=list)

    NULL_KEY = "<null>"

    def add(
        self,
        table: str,
        reason_code: str,
        reason: str,
        key_column: str,
        keys: pd.Series,
        *,
        source_system: str,
        source_run_id: str,
    ) -> None:
        if keys.empty:
            return
        # value_counts drops nulls by default; a row rejected *because* its
        # key is null would then vanish from the very count meant to track
        # it, so nulls are relabelled rather than dropped.
        safe_keys = keys.astype("string").fillna(self.NULL_KEY)
        for value, count in safe_keys.value_counts().items():
            self.records.append({
                "source_system": source_system,
                "source_run_id": source_run_id,
                "table_name": table,
                "reason_code": reason_code,
                "reason": reason,
                "key_column": key_column,
                "key_value": value,
                "row_count": int(count),
            })

    def frame(self) -> pd.DataFrame:
        columns = [
            "source_system", "source_run_id", "table_name", "reason_code",
            "reason", "key_column", "key_value", "row_count",
        ]
        if not self.records:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(self.records)[columns]

    def total(self, table: str | None = None) -> int:
        return sum(
            r["row_count"] for r in self.records
            if table is None or r["table_name"] == table
        )


class RequiredFieldUnresolved(ValueError):
    """A required canonical field matched none of its candidate spellings.

    Raised instead of silently filling the field with nulls, which would
    otherwise let a build succeed, pass validation, and publish a table
    whose supposedly-required column is entirely empty.
    """

    def __init__(
        self, *, table: str, field: str, candidates: tuple[str, ...], columns_present: list[str],
    ) -> None:
        self.table = table
        self.field = field
        self.candidates = candidates
        self.columns_present = columns_present
        super().__init__(
            f"{table}: required field {field!r} matches none of the candidate source "
            f"columns {candidates!r}; columns actually present in the source frame: "
            f"{sorted(columns_present)!r}"
        )


@dataclass(frozen=True, slots=True)
class FieldResolution:
    """Record of how one canonical field was resolved for one source run."""

    source_system: str
    source_run_id: str
    table: str
    field: str
    matched_candidate: str | None  # None means every candidate was absent (field is optional)


@dataclass
class FieldResolutions:
    """Log of alias-resolution outcomes for fields resolved via a candidate list.

    This is the safeguard the module docstring promises: rather than a
    candidate match (or non-match) disappearing silently into a frame
    assignment, every resolution is appended here so a build result can
    report exactly which spelling was used -- or that none was found --
    for every optional field.
    """

    records: list[FieldResolution] = field(default_factory=list)

    def add(
        self, table: str, field_name: str, matched_candidate: str | None,
        *, source_system: str, source_run_id: str,
    ) -> None:
        self.records.append(FieldResolution(
            source_system=source_system, source_run_id=source_run_id,
            table=table, field=field_name, matched_candidate=matched_candidate,
        ))

    def unresolved(self) -> tuple[FieldResolution, ...]:
        """Fields that resolved to null because no candidate was present."""

        return tuple(r for r in self.records if r.matched_candidate is None)


def _dedupe(frame: pd.DataFrame, keys: list[str], table: str, quarantine: Quarantine,
            *, source_system: str, source_run_id: str) -> pd.DataFrame:
    """Collapse to one row per composite key, quarantining genuine conflicts.

    Repeated identical rows are just the same fact re-observed and are kept
    silently. Two rows that share a key but disagree on their other values
    are a real conflict: keeping the first would pick a winner arbitrarily,
    so those rows are recorded and dropped instead.
    """

    duplicated = frame.duplicated(keys, keep="first")
    if not duplicated.any():
        return frame

    kept = frame.loc[~duplicated]
    identical = frame.merge(
        kept.drop_duplicates(keys), how="left", on=list(frame.columns), indicator=True,
    )
    conflicting = duplicated.to_numpy() & (identical["_merge"].to_numpy() == "left_only")

    if conflicting.any():
        label = "+".join(keys)
        values = (
            frame.loc[conflicting, keys].astype("string").agg("/".join, axis=1)
            if len(keys) > 1 else frame.loc[conflicting, keys[0]]
        )
        quarantine.add(
            table, "conflicting_duplicate_key", "rows share a key but disagree on other values",
            label, values, source_system=source_system, source_run_id=source_run_id,
        )
    return kept


def _restrict(
    frame: pd.DataFrame, table: str, column: str, allowed: set[str], quarantine: Quarantine,
    *, source_system: str, source_run_id: str, reason_code: str = "orphan_reference",
) -> pd.DataFrame:
    orphan = ~frame[column].isin(allowed)
    if orphan.any():
        quarantine.add(
            table, reason_code, f"{column} does not reference a loaded parent row",
            column, frame.loc[orphan, column], source_system=source_system, source_run_id=source_run_id,
        )
    return frame.loc[~orphan]


def _ensure_columns(frame: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        if column not in out.columns:
            out[column] = pd.NA
    return out


def _first_present(
    frame: pd.DataFrame, table: str, field_name: str, candidates: tuple[str, ...], *, required: bool,
) -> tuple[pd.Series, str | None]:
    """Resolve one canonical field to the first matching candidate column.

    Returns the resolved series and the candidate that matched (``None`` if
    none did). A required field with no match raises loudly rather than
    handing back a silent all-null column -- see :class:`RequiredFieldUnresolved`.
    """

    for candidate in candidates:
        if candidate in frame.columns:
            return frame[candidate], candidate
    if required:
        raise RequiredFieldUnresolved(
            table=table, field=field_name, candidates=candidates,
            columns_present=list(frame.columns),
        )
    return pd.Series(pd.NA, index=frame.index, dtype="object"), None


def _base_identity(frame: pd.DataFrame) -> pd.DataFrame:
    """Provenance columns every canonical row already carries.

    ``gp_code``/``gram_panchayat_name``/``fiscal_year`` come from the
    normalizer's own folder-name and filename parsing
    (``pipeline.normalize._gp_context``/``_file_context``), not from a
    guessed business-field spelling, so they are the most reliable source
    for these three values across every kind.
    """

    out = pd.DataFrame({
        "source_system": frame["source_system"],
        "source_run_id": frame["source_run_id"],
        "row_id": frame["row_id"],
        "activity_code": frame["business_id"],
        "gp_lgd_code": to_code(frame["gp_code"]),
        "gp_name": frame["gram_panchayat_name"],
        "fiscal_year": frame["fiscal_year"],
    })
    return out


# --------------------------------------------------------------------- gram_panchayat


def gram_panchayat(root_frames: list[pd.DataFrame], quarantine: Quarantine,
                    *, source_system: str, source_run_id: str) -> pd.DataFrame:
    """One row per LGD code, built from every kind's own provenance.

    Every kind independently records the GP it was scraped for; unioning
    them is the "proven mapping" -- the folder-name regex is the same
    parser for every kind, unlike a business field whose spelling can differ
    kind to kind.
    """

    parts = []
    for raw in root_frames:
        if raw.empty:
            continue
        parts.append(pd.DataFrame({
            "gp_lgd_code": to_code(raw["gp_code"]),
            "gp_name": raw["gram_panchayat_name"].astype("string"),
        }))
    if not parts:
        return pd.DataFrame(columns=["gp_lgd_code", "gp_name"])
    combined = pd.concat(parts, ignore_index=True).dropna(subset=["gp_lgd_code"])
    return _dedupe(
        combined, ["gp_lgd_code"], "gram_panchayat", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


# --------------------------------------------------------------------- PL: plan + planned_activity + satellites

PL_RENAMES = {
    "planCode": "plan_code",
    "planTyp": "plan_type", "plan_typ": "plan_type",
    "planCodeStts": "plan_code_status",
    "approvalDate": "approval_date",
    "activityType": "activity_type",
    "activityName": "activity_name",
    "activityDesc": "activity_desc",
    "focusArea": "focus_area",
    "activityFor": "activity_for",
    "workTyp": "work_type",
    "activityForCostlessFlag": "is_costless_activity",
    "totalCost": "total_cost",
    "operationType": "operation_type",
    "operationRemarks": "operation_remarks",
    "outputTyp": "output_type",
    "activityStts": "activity_status",
    "mainAstCtgry": "main_asset_category",
    "mainAstSubCtgry": "main_asset_subcategory",
    "mainAstUntTyp": "main_asset_unit_type",
    "mainAstNumOfUnt": "main_asset_unit_count",
    "dlagtdFlag": "is_delegated",
    "dlagtdPlnUntCd": "delegated_unit_code",
    "dlagtdPlnUntTyp": "delegated_unit_type",
    "dlagtdPlnUntLvl": "delegated_unit_level",
    "dlagtdPlnUntCat": "delegated_unit_category",
    "shareable": "is_shareable",
    "dlagtdPerentPlnUntCd": "delegated_parent_unit_code",
    "trainingCapacity_trngCatCd": "training_category_code",
    "trainingCapacity_trngOrgByCd": "training_organiser_code",
    "trainingCapacity_trngSubject": "training_subject",
    "trainingCapacity_totTrainees": "training_trainees_total",
    "trainingCapacity_totDurationDays": "training_duration_days",
    "communityService_serviCd": "community_service_code",
    "communityService_serviDuration": "community_service_duration",
    "communityService_totalexpBeneficiares": "community_beneficiaries_expected",
}

# variable, age band, gender -> renamed NSAP column name
NSAP_COLUMNS: dict[str, tuple[str, str, str]] = {
    "nsap_old_age_lt80_male": ("old_age", "lt80", "male"),
    "nsap_old_age_lt80_female": ("old_age", "lt80", "female"),
    "nsap_old_age_lt80_transgender": ("old_age", "lt80", "transgender"),
    "nsap_old_age_ge80_male": ("old_age", "ge80", "male"),
    "nsap_old_age_ge80_female": ("old_age", "ge80", "female"),
    "nsap_old_age_ge80_transgender": ("old_age", "ge80", "transgender"),
    "nsap_disabled_male": ("disabled", "na", "male"),
    "nsap_disabled_female": ("disabled", "na", "female"),
    "nsap_disabled_transgender": ("disabled", "na", "transgender"),
    "nsap_widow_male": ("widow", "na", "male"),
    "nsap_widow_female": ("widow", "na", "female"),
    "nsap_widow_transgender": ("widow", "na", "transgender"),
}
NSAP_RENAMES = {
    "activityNsap_old_age_below_eighty_male": "nsap_old_age_lt80_male",
    "activityNsap_old_age_below_eighty_female": "nsap_old_age_lt80_female",
    "activityNsap_old_age_below_eighty_transgender": "nsap_old_age_lt80_transgender",
    "activityNsap_old_age_greater_eighty_male": "nsap_old_age_ge80_male",
    "activityNsap_old_age_greater_eighty_female": "nsap_old_age_ge80_female",
    "activityNsap_old_age_greater_eighty_transgender": "nsap_old_age_ge80_transgender",
    "activityNsap_disabled_male": "nsap_disabled_male",
    "activityNsap_disabled_female": "nsap_disabled_female",
    "activityNsap_disabled_transgender": "nsap_disabled_transgender",
    "activityNsap_widow_male": "nsap_widow_male",
    "activityNsap_widow_female": "nsap_widow_female",
    # Source typo uses "window" rather than "widow" (per PR #30).
    "activityNsap_window_transgender": "nsap_widow_transgender",
}


def _clean_pl(pl: pd.DataFrame) -> pd.DataFrame:
    out = pl.rename(columns=PL_RENAMES).rename(columns=NSAP_RENAMES)
    identity = _base_identity(out)
    for column, series in identity.items():
        out[column] = series
    return out


def plan(pl: pd.DataFrame, quarantine: Quarantine,
         *, source_system: str, source_run_id: str) -> pd.DataFrame:
    out = _clean_pl(pl)
    out = _ensure_columns(out, ["plan_code", "plan_type", "plan_code_status", "approval_date"])
    frame = out[["source_system", "source_run_id", "plan_code", "gp_lgd_code",
                 "fiscal_year", "plan_type", "plan_code_status", "approval_date"]].copy()
    frame["plan_code"] = to_code(frame["plan_code"])
    frame["approval_date"] = to_datetime(frame["approval_date"])
    frame = frame.dropna(subset=["plan_code"])
    return _dedupe(
        frame, ["source_system", "source_run_id", "plan_code"], "plan", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


PLANNED_ACTIVITY_COLUMNS = [
    "source_system", "source_run_id", "activity_code", "plan_code", "gp_lgd_code",
    "fiscal_year", "source_file", "activity_type", "activity_name", "activity_desc",
    "focus_area", "activity_for", "work_type", "is_costless_activity", "total_cost",
    "operation_type", "operation_remarks", "output_type", "activity_status",
    "main_asset_category", "main_asset_subcategory", "main_asset_unit_type",
    "main_asset_unit_count",
]


def planned_activity(pl: pd.DataFrame, quarantine: Quarantine,
                      *, source_system: str, source_run_id: str) -> pd.DataFrame:
    out = _clean_pl(pl)
    out = _ensure_columns(out, PLANNED_ACTIVITY_COLUMNS)
    frame = out[PLANNED_ACTIVITY_COLUMNS].copy()
    frame["activity_code"] = to_code(frame["activity_code"])
    frame["plan_code"] = to_code(frame["plan_code"])
    frame["total_cost"] = to_decimal_money(frame["total_cost"])
    frame = frame.dropna(subset=["activity_code"])
    return _dedupe(
        frame, ["source_system", "source_run_id", "activity_code"], "planned_activity", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


def _satellite(pl: pd.DataFrame, columns: list[str], table: str, quarantine: Quarantine,
               *, source_system: str, source_run_id: str, activity_codes: set[str]) -> pd.DataFrame:
    out = _clean_pl(pl)
    keep = ["source_system", "source_run_id", "activity_code"] + columns
    out = _ensure_columns(out, keep)
    frame = out[keep].copy()
    frame["activity_code"] = to_code(frame["activity_code"])
    frame = frame.dropna(subset=["activity_code"])
    frame = _dedupe(
        frame, ["source_system", "source_run_id", "activity_code"], table, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    return _restrict(
        frame, table, "activity_code", activity_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


def activity_delegation(pl: pd.DataFrame, activity_codes: set[str], quarantine: Quarantine,
                         *, source_system: str, source_run_id: str) -> pd.DataFrame:
    return _satellite(
        pl, ["is_delegated", "delegated_unit_code", "delegated_unit_type",
             "delegated_unit_level", "delegated_unit_category", "is_shareable",
             "delegated_parent_unit_code"],
        "activity_delegation", quarantine, source_system=source_system,
        source_run_id=source_run_id, activity_codes=activity_codes,
    )


def activity_training(pl: pd.DataFrame, activity_codes: set[str], quarantine: Quarantine,
                       *, source_system: str, source_run_id: str) -> pd.DataFrame:
    return _satellite(
        pl, ["training_category_code", "training_organiser_code", "training_subject",
             "training_trainees_total", "training_duration_days"],
        "activity_training", quarantine, source_system=source_system,
        source_run_id=source_run_id, activity_codes=activity_codes,
    )


def activity_community_service(pl: pd.DataFrame, activity_codes: set[str], quarantine: Quarantine,
                                *, source_system: str, source_run_id: str) -> pd.DataFrame:
    return _satellite(
        pl, ["community_service_code", "community_service_duration",
             "community_beneficiaries_expected"],
        "activity_community_service", quarantine, source_system=source_system,
        source_run_id=source_run_id, activity_codes=activity_codes,
    )


def activity_nsap(pl: pd.DataFrame, activity_codes: set[str],
                   *, source_system: str, source_run_id: str, start_id: int = 1) -> pd.DataFrame:
    """Wide NSAP beneficiary columns to one row per non-zero category.

    ``nsap_id`` is a real, published column in the target schema (confirmed
    against the real table header) and the table's actual primary key --
    not an invented artifact the way activity_asset/activity_fund's old
    row_id was. It is assigned the same way
    ``transform.activity_expenditure`` assigns ``expenditure_id``: densely,
    starting at the caller-supplied ``start_id``, after every filtering
    step, so ids are 1:1 with the rows actually returned. The caller
    (``build.populate``) advances ``start_id`` by the number of rows
    returned before calling this again for the next snapshot.
    """

    columns = ["nsap_id", "source_system", "source_run_id", "activity_code", "category",
               "age_band", "gender", "beneficiary_count"]
    out = _clean_pl(pl)
    present = [c for c in NSAP_COLUMNS if c in out.columns]
    if not present:
        return pd.DataFrame(columns=columns)

    out = out[["source_system", "source_run_id", "activity_code"] + present].copy()
    out["activity_code"] = to_code(out["activity_code"])
    out = out[out["activity_code"].isin(activity_codes)]
    melted = out.melt(
        id_vars=["source_system", "source_run_id", "activity_code"],
        var_name="column", value_name="beneficiary_count",
    )
    # beneficiary_count is a COUNT, not money: parsed as a nullable integer
    # (see warehouse.schema's activity_nsap DDL), never routed through
    # decimal money parsing.
    melted["beneficiary_count"] = to_int(melted["beneficiary_count"])
    melted = melted[melted["beneficiary_count"].notna() & (melted["beneficiary_count"] != 0)]
    melted["category"] = melted["column"].map(lambda c: NSAP_COLUMNS[c][0])
    melted["age_band"] = melted["column"].map(lambda c: NSAP_COLUMNS[c][1])
    melted["gender"] = melted["column"].map(lambda c: NSAP_COLUMNS[c][2])
    frame = melted[[c for c in columns if c != "nsap_id"]].drop_duplicates(
        ["source_system", "source_run_id", "activity_code", "category", "age_band", "gender"],
    ).reset_index(drop=True)
    frame.insert(0, "nsap_id", range(start_id, start_id + len(frame)))
    return frame


# --------------------------------------------------------------------- PL children: asset, fund

ASSET_CHILD_RENAMES = {
    "astTyp": "asset_type", "astCtgry": "asset_category", "astSubCtgry": "asset_subcategory",
    "astCvrgCd": "asset_coverage_code", "astNm": "asset_name", "astUntTyp": "asset_unit_type",
    "astNumOfUnt": "asset_unit_count", "astUnitCost": "asset_unit_cost",
    "astParameterTyp": "asset_parameter_type",
    "assetLocationDetails_astLocCd": "asset_loc_code",
    "assetLocationDetails_astPlnUntCd": "asset_loc_unit_code",
    "assetLocationDetails_astPlnUntTyp": "asset_loc_unit_type",
    "assetLocationDetails_astNoOfUnt": "asset_loc_unit_count",
    "assetLocationDetails_astUnitCostTot": "asset_loc_unit_cost_total",
}

ACTIVITY_ASSET_COLUMNS = [
    "asset_type", "asset_category", "asset_subcategory", "asset_coverage_code",
    "asset_name", "asset_unit_type", "asset_unit_count", "asset_unit_cost",
    "asset_parameter_type", "asset_loc_code", "asset_loc_unit_code",
    "asset_loc_unit_type", "asset_loc_unit_count", "asset_loc_unit_cost_total",
]
ASSET_MONEY_COLUMNS = ["asset_unit_cost", "asset_loc_unit_cost_total"]

FUND_CHILD_RENAMES = {
    "schemeCode": "fund_scheme_code", "componentCode": "fund_component_code",
    "tiedAmountGen": "fund_tied_general", "tiedAmountSc": "fund_tied_sc",
    "tiedAmountSt": "fund_tied_st", "untiedAmountGen": "fund_untied_general",
    "untiedAmountSc": "fund_untied_sc", "untiedAmountSt": "fund_untied_st",
    "amountTotal": "fund_amount_total",
    "tiedAbundonAmountGen": "fund_tied_abandoned_general",
    "tiedAbundonAmountSc": "fund_tied_abandoned_sc",
    "tiedAbundonAmountSt": "fund_tied_abandoned_st",
    "untiedAbundonAmountGen": "fund_untied_abandoned_general",
    "untiedAbundonAmountSc": "fund_untied_abandoned_sc",
    "untiedAbundonAmountSt": "fund_untied_abandoned_st",
}
ACTIVITY_FUND_COLUMNS = [
    "fund_scheme_code", "fund_component_code", "fund_tied_general", "fund_tied_sc",
    "fund_tied_st", "fund_untied_general", "fund_untied_sc", "fund_untied_st",
    "fund_amount_total", "fund_tied_abandoned_general", "fund_tied_abandoned_sc",
    "fund_tied_abandoned_st", "fund_untied_abandoned_general",
    "fund_untied_abandoned_sc", "fund_untied_abandoned_st",
]
# Not every "fund_*" column is an amount: fund_scheme_code and
# fund_component_code are identifiers and must not be routed through decimal
# money parsing, which would silently null them out.
FUND_MONEY_COLUMNS = [
    c for c in ACTIVITY_FUND_COLUMNS
    if c.startswith("fund_") and c not in ("fund_scheme_code", "fund_component_code")
]


def _pl_child(
    child: pd.DataFrame, renames: dict[str, str], columns: list[str], money_columns: list[str],
    table: str, activity_codes: set[str], quarantine: Quarantine,
    *, source_system: str, source_run_id: str,
) -> pd.DataFrame:
    """Shape one activity_asset/activity_fund frame, keyed on activity_code.

    These tables are strictly 1:1 with planned_activity (see
    ``schema.DDL["activity_asset"]``'s comment): the real source gives no
    per-row identity of its own, so a second line for the same activity is
    a genuine conflicting duplicate, quarantined by ``_dedupe`` like any
    other -- not a second legitimate row keyed on an invented row_id.
    """

    keep = ["source_system", "source_run_id", "activity_code"] + columns
    if child.empty:
        return pd.DataFrame(columns=keep)
    out = child.rename(columns=renames)
    identity = _base_identity(out)
    for name, series in identity.items():
        out[name] = series
    out = _ensure_columns(out, keep)
    frame = out[keep].copy()
    frame["activity_code"] = to_code(frame["activity_code"])
    for column in money_columns:
        frame[column] = to_decimal_money(frame[column])
    frame = frame.dropna(subset=["activity_code"])
    frame = _dedupe(
        frame, ["source_system", "source_run_id", "activity_code"], table, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    return _restrict(
        frame, table, "activity_code", activity_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


def activity_asset(child: pd.DataFrame, activity_codes: set[str], quarantine: Quarantine,
                    *, source_system: str, source_run_id: str) -> pd.DataFrame:
    return _pl_child(
        child, ASSET_CHILD_RENAMES, ACTIVITY_ASSET_COLUMNS, ASSET_MONEY_COLUMNS,
        "activity_asset", activity_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


def activity_fund(child: pd.DataFrame, activity_codes: set[str], quarantine: Quarantine,
                   *, source_system: str, source_run_id: str) -> pd.DataFrame:
    return _pl_child(
        child, FUND_CHILD_RENAMES, ACTIVITY_FUND_COLUMNS, FUND_MONEY_COLUMNS,
        "activity_fund", activity_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


# --------------------------------------------------------------------- AA: admin_approval(+scheme)

AA_RENAMES = {
    "wrkPlnYr": "work_plan_year", "wrkAdmApprNo": "adm_approval_no",
    "wrkAdmApprSnctnOrdrDt": "adm_approval_sanction_date",
    "wrkProposedCost": "work_proposed_cost", "wrkAdmApprIssAuthrty": "adm_approval_authority",
}
ADMIN_APPROVAL_COLUMNS = [
    "source_system", "source_run_id", "row_id", "gp_lgd_code", "gp_name", "plan_year",
    "source_file", "activity_code", "work_plan_year", "adm_approval_no",
    "adm_approval_sanction_date", "work_proposed_cost", "adm_approval_authority",
]


def admin_approval(aa: pd.DataFrame, activity_codes: set[str], gp_codes: set[str],
                    quarantine: Quarantine, *, source_system: str, source_run_id: str) -> pd.DataFrame:
    if aa.empty:
        return pd.DataFrame(columns=ADMIN_APPROVAL_COLUMNS)
    out = aa.rename(columns=AA_RENAMES)
    identity = _base_identity(out)
    for name, series in identity.items():
        out[name] = series
    out["plan_year"] = out["fiscal_year"]
    out = _ensure_columns(out, ADMIN_APPROVAL_COLUMNS + ["source_file"])
    frame = out[ADMIN_APPROVAL_COLUMNS].copy()
    frame["activity_code"] = to_code(frame["activity_code"])
    frame["adm_approval_no"] = strip_leading_zeros(to_code(frame["adm_approval_no"]))
    frame["adm_approval_sanction_date"] = to_datetime(frame["adm_approval_sanction_date"])
    frame["work_proposed_cost"] = to_decimal_money(frame["work_proposed_cost"])
    frame = frame.dropna(subset=["row_id"])
    frame = _dedupe(
        frame, ["source_system", "source_run_id", "row_id"], "admin_approval", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    frame = _restrict(
        frame, "admin_approval", "activity_code", activity_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    return _restrict(
        frame, "admin_approval", "gp_lgd_code", gp_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id, reason_code="orphan_gp",
    )


# fndAllctnSchmTot appears in PR #30's rename dict for the *same* field that
# PR #9 spells wrkAdmApprFndSnctnTotal; the two donors disagree, so both
# aliases are accepted here and resolve to the same canonical column.
AA_SCHEME_RENAMES = {
    "wrkSchmCd": "scheme_code", "wrkSchmCmpntCd": "scheme_component_code",
    "wrkAdmApprFndSnctnGen": "fund_sanctioned_general",
    "wrkAdmApprFndSnctnSc": "fund_sanctioned_sc",
    "wrkAdmApprFndSnctnSt": "fund_sanctioned_st",
    "wrkAdmApprFndSnctnTotal": "fund_sanctioned_total",
    "fndAllctnSchmTot": "fund_sanctioned_total",
}
ADMIN_APPROVAL_SCHEME_COLUMNS = [
    "scheme_code", "scheme_component_code", "fund_sanctioned_general",
    "fund_sanctioned_sc", "fund_sanctioned_st", "fund_sanctioned_total",
]
AA_SCHEME_MONEY_COLUMNS = [c for c in ADMIN_APPROVAL_SCHEME_COLUMNS if c.startswith("fund_")]


def admin_approval_scheme(child: pd.DataFrame, parent_row_ids: set[str], quarantine: Quarantine,
                           *, source_system: str, source_run_id: str) -> pd.DataFrame:
    columns = ["source_system", "source_run_id", "row_id", "parent_row_id", "activity_code"] \
        + ADMIN_APPROVAL_SCHEME_COLUMNS
    if child.empty:
        return pd.DataFrame(columns=columns)
    out = child.rename(columns=AA_SCHEME_RENAMES)
    out["source_system"] = out["source_system"]
    out["source_run_id"] = out["source_run_id"]
    out["row_id"] = out["row_id"]
    out["parent_row_id"] = out["parent_row_id"]
    out["activity_code"] = out["business_id"]
    out = _ensure_columns(out, columns)
    frame = out[columns].copy()
    for column in AA_SCHEME_MONEY_COLUMNS:
        frame[column] = to_decimal_money(frame[column])
    frame = frame.dropna(subset=["row_id"])
    frame = _dedupe(
        frame, ["source_system", "source_run_id", "row_id"], "admin_approval_scheme", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    return _restrict(
        frame, "admin_approval_scheme", "parent_row_id", parent_row_ids, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


# --------------------------------------------------------------------- TA: technical_approval

TA_RENAMES = {
    "wrkTecApprReqFlg": "tec_approval_required", "wrkTecApprCost": "tec_approval_cost",
    "wrkTecApprIssAuthrty": "tec_approval_authority", "wrkTecApprOrdrNo": "tec_approval_order_no",
    "wrkTecApprOrdrDt": "tec_approval_order_date",
}
TECHNICAL_APPROVAL_COLUMNS = [
    "source_system", "source_run_id", "row_id", "gp_lgd_code", "gp_name", "plan_year",
    "source_file", "activity_code", "tec_approval_required", "tec_approval_cost",
    "tec_approval_authority", "tec_approval_order_no", "tec_approval_order_date",
]


def technical_approval(ta: pd.DataFrame, activity_codes: set[str], gp_codes: set[str],
                        quarantine: Quarantine, *, source_system: str, source_run_id: str) -> pd.DataFrame:
    if ta.empty:
        return pd.DataFrame(columns=TECHNICAL_APPROVAL_COLUMNS)
    out = ta.rename(columns=TA_RENAMES)
    identity = _base_identity(out)
    for name, series in identity.items():
        out[name] = series
    out["plan_year"] = out["fiscal_year"]
    out = _ensure_columns(out, TECHNICAL_APPROVAL_COLUMNS)
    frame = out[TECHNICAL_APPROVAL_COLUMNS].copy()
    frame["activity_code"] = to_code(frame["activity_code"])
    frame["tec_approval_order_no"] = strip_leading_zeros(to_code(frame["tec_approval_order_no"]))
    frame["tec_approval_order_date"] = to_datetime(frame["tec_approval_order_date"])
    frame["tec_approval_cost"] = to_decimal_money(frame["tec_approval_cost"])
    frame = frame.dropna(subset=["row_id"])
    frame = _dedupe(
        frame, ["source_system", "source_run_id", "row_id"], "technical_approval", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    frame = _restrict(
        frame, "technical_approval", "activity_code", activity_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    return _restrict(
        frame, "technical_approval", "gp_lgd_code", gp_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id, reason_code="orphan_gp",
    )


# --------------------------------------------------------------------- PP: physical_progress

PP_RENAMES = {"fileUploadId": "file_upload_id", "plnunttypecode": "plan_unit_type_code"}
PHYSICAL_PROGRESS_COLUMNS = [
    "source_system", "source_run_id", "row_id", "activity_code", "file_upload_id",
    "longitude", "latitude", "n_coords", "longitude_raw", "latitude_raw", "plan_unit_type_code",
]


def physical_progress(pp: pd.DataFrame, activity_codes: set[str], quarantine: Quarantine,
                       *, source_system: str, source_run_id: str) -> pd.DataFrame:
    if pp.empty:
        return pd.DataFrame(columns=PHYSICAL_PROGRESS_COLUMNS)
    out = pp.rename(columns=PP_RENAMES)
    identity = _base_identity(out)
    for name, series in identity.items():
        out[name] = series
    out = _ensure_columns(out, PHYSICAL_PROGRESS_COLUMNS + ["latitude", "longitude"])

    latitude_raw = out["latitude"].astype("string")
    longitude_raw = out["longitude"].astype("string")
    present = latitude_raw.notna() & (latitude_raw.str.strip() != "")
    n_coords = (latitude_raw.str.count(",").fillna(0) + 1).where(present, 0).astype("Int64")

    def first_coordinate(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series.astype("string").str.split(",").str[0].str.strip(), errors="coerce")

    frame = out[["source_system", "source_run_id", "row_id", "activity_code",
                 "file_upload_id", "plan_unit_type_code"]].copy()
    frame["activity_code"] = to_code(frame["activity_code"])
    frame["file_upload_id"] = to_code(frame["file_upload_id"])
    frame["latitude_raw"] = latitude_raw
    frame["longitude_raw"] = longitude_raw
    frame["n_coords"] = n_coords
    frame["latitude"] = first_coordinate(out["latitude"])
    frame["longitude"] = first_coordinate(out["longitude"])
    frame = frame[PHYSICAL_PROGRESS_COLUMNS]
    frame = frame.dropna(subset=["row_id"])
    frame = _dedupe(
        frame, ["source_system", "source_run_id", "row_id"], "physical_progress", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    return _restrict(
        frame, "physical_progress", "activity_code", activity_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )


# --------------------------------------------------------------------- RE: activity_expenditure

# Unverified against real data (see module docstring): several candidate
# spellings per field, first match wins.
RE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "plan_code": ("planCode", "plan_code"),
    "s_no": ("sNo", "s_no", "S.No.", "serialNo", "sno"),
    "scheme_name": ("schemeName", "scheme_name", "Scheme Name"),
    "approved_cost_action_plan": (
        "aprvdCstActnPln", "apprvdCstActnPln", "approved_cost_action_plan",
        "Approved Cost in Action Plan",
    ),
    "technical_approved_cost": (
        "tecApprvdCst", "technical_approved_cost", "Technical Approved Cost",
    ),
    "admin_approved_cost": ("admApprvdCst", "admin_approved_cost", "Admin Approved Cost"),
    "general": ("general", "General"),
    "sc": ("sc", "SC"),
    "st": ("st", "ST"),
    "total_expenditure": ("totalExpenditure", "total_expenditure", "Total Expenditure"),
}
# expenditure_id is prepended once the surrogate id is assigned, at the end
# of activity_expenditure() below -- not part of the RE_CANDIDATES-driven
# shaping, since it has no source-field spelling at all.
ACTIVITY_EXPENDITURE_COLUMNS = [
    "source_system", "source_run_id", "gp_lgd_code", "plan_code", "activity_code",
    "fiscal_year", "s_no", "scheme_name", "approved_cost_action_plan",
    "technical_approved_cost", "admin_approved_cost", "general", "sc", "st",
    "total_expenditure",
]
RE_MONEY_COLUMNS = [
    "approved_cost_action_plan", "technical_approved_cost", "admin_approved_cost",
    "general", "sc", "st", "total_expenditure",
]
# plan_code and s_no, together with gp_lgd_code and activity_code (both of
# which come from base identity, not from RE_CANDIDATES), make up the
# documented activity_expenditure business identity
# (gp_lgd_code, plan_code, activity_code, s_no) -- distinct from its actual
# primary key, the expenditure_id surrogate assigned below. An all-null
# identity component is definitionally unusable, so these two are the only
# RE_CANDIDATES fields required to resolve to a real column; every other
# field here is descriptive/financial detail that a row can legitimately
# lack.
RE_REQUIRED_FIELDS = frozenset({"plan_code", "s_no"})


def activity_expenditure(
    re: pd.DataFrame, gp_codes: set[str], quarantine: Quarantine,
    *, source_system: str, source_run_id: str,
    resolutions: FieldResolutions | None = None,
    start_id: int = 1,
) -> pd.DataFrame:
    """Shape one activity_expenditure frame and assign its expenditure_id.

    ``expenditure_id`` is the table's real primary key (an INTEGER
    surrogate -- see ``schema.py``): the source gives this table no row
    identity beyond the (gp_lgd_code, plan_code, activity_code, s_no)
    business tuple, which is too wide a composite to use as a foreign-key
    target from activity_voucher. Ids are assigned contiguously starting at
    ``start_id`` *after* every quarantine/dedupe/restrict step, so they are
    dense and 1:1 with the rows actually returned. The caller
    (``build.populate``) is responsible for advancing ``start_id`` by the
    number of rows returned before calling this again for the next
    snapshot, so ids stay unique across every snapshot loaded into one
    build.
    """

    columns = ["expenditure_id"] + ACTIVITY_EXPENDITURE_COLUMNS
    if re.empty:
        return pd.DataFrame(columns=columns)
    if resolutions is None:
        resolutions = FieldResolutions()
    identity = _base_identity(re)
    frame = pd.DataFrame({name: series for name, series in identity.items()
                          if name in ("source_system", "source_run_id", "gp_lgd_code",
                                      "activity_code", "fiscal_year")})
    for canonical, candidates in RE_CANDIDATES.items():
        series, matched = _first_present(
            re, "activity_expenditure", canonical, candidates,
            required=canonical in RE_REQUIRED_FIELDS,
        )
        frame[canonical] = series
        resolutions.add(
            "activity_expenditure", canonical, matched,
            source_system=source_system, source_run_id=source_run_id,
        )
    frame = frame[ACTIVITY_EXPENDITURE_COLUMNS]
    frame["plan_code"] = to_code(frame["plan_code"])
    frame["activity_code"] = to_code(frame["activity_code"])
    frame["s_no"] = to_code(frame["s_no"])
    for column in RE_MONEY_COLUMNS:
        frame[column] = to_decimal_money(frame[column])
    frame = frame.dropna(subset=["gp_lgd_code", "plan_code", "activity_code", "s_no"])
    frame = _dedupe(
        frame, ["source_system", "source_run_id", "gp_lgd_code", "plan_code", "activity_code", "s_no"],
        "activity_expenditure", quarantine, source_system=source_system, source_run_id=source_run_id,
    )
    frame = _restrict(
        frame, "activity_expenditure", "gp_lgd_code", gp_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id, reason_code="orphan_gp",
    )
    frame = frame.reset_index(drop=True)
    frame.insert(0, "expenditure_id", range(start_id, start_id + len(frame)))
    return frame
