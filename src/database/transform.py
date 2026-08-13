"""Source frames to table-shaped frames.

Every function here is pure: frames in, frames out, no database and no file
system. That is what lets the whole shaping layer be tested on small synthetic
fixtures instead of a 12,000-row extract.

Where a row cannot be kept, it is returned as a quarantine record with a reason
rather than dropped, so a build can always account for the difference between
rows read and rows loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .clean import (count_coordinates, first_coordinate, strip_leading_zeros,
                    to_code, to_fiscal_year, year_from_voucher_no)

CORE_COLUMNS = [
    "activity_code", "plan_code", "gp_lgd_code", "fiscal_year", "source_file",
    "activity_type", "activity_name", "activity_desc", "focus_area",
    "activity_for", "work_type", "is_costless_activity", "total_cost",
    "operation_type", "operation_remarks", "output_type", "activity_status",
]

SATELLITES: dict[str, list[str]] = {
    "activity_delegation": [
        "is_delegated", "delegated_unit_code", "delegated_unit_type",
        "delegated_unit_level", "delegated_unit_category", "is_shareable",
        "delegated_parent_unit_code"],
    "activity_asset": [
        "main_asset_category", "main_asset_subcategory", "main_asset_unit_type",
        "main_asset_unit_count", "asset_type", "asset_category",
        "asset_subcategory", "asset_coverage_code", "asset_name",
        "asset_unit_type", "asset_unit_count", "asset_unit_cost",
        "asset_parameter_type", "asset_details_raw", "asset_loc_code",
        "asset_loc_unit_code", "asset_loc_unit_type", "asset_loc_unit_count",
        "asset_loc_unit_cost_total", "asset_loc_overflow_json"],
    "activity_fund": [
        "fund_scheme_code", "fund_component_code", "fund_tied_general",
        "fund_tied_sc", "fund_tied_st", "fund_untied_general", "fund_untied_sc",
        "fund_untied_st", "fund_amount_total", "fund_tied_abandoned_general",
        "fund_tied_abandoned_sc", "fund_tied_abandoned_st",
        "fund_untied_abandoned_general", "fund_untied_abandoned_sc",
        "fund_untied_abandoned_st", "fund_overflow_json"],
    "activity_training": [
        "training_capacity_raw", "training_category_code",
        "training_organiser_code", "training_subject",
        "training_trainees_total", "training_duration_days"],
    "activity_community_service": [
        "community_service_raw", "community_service_code",
        "community_service_duration", "community_beneficiaries_expected"],
}

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

EXPENDITURE_RENAMES = {
    "planYear": "plan_year", "stateName": "state_name", "zpName": "zp_name",
    "blockName": "block_name", "gpName": "gp_name", "gpCode": "gp_lgd_code",
    "planType": "plan_type", "approvalDate": "approval_date",
    "planCode": "plan_code", "S.No.": "s_no", "Activity Code": "activity_code",
    "Activity Name": "activity_name", "Activity For": "activity_for",
    "Focus Area": "focus_area",
    "Approved Cost in Action Plan": "approved_cost_action_plan",
    "Technical Approved Cost": "technical_approved_cost",
    "Admin Approved Cost": "admin_approved_cost", "Scheme Name": "scheme_name",
    "General": "general", "SC": "sc", "ST": "st",
    "Total Expenditure": "total_expenditure",
    # _list columns are still pipe-delimited at this point.
    "Voucher Date": "voucher_date_list", "Voucher No": "voucher_no_list",
    "Voucher Cost": "voucher_cost_list",
}


@dataclass
class Quarantine:
    """Rows that could not be loaded, and why."""

    records: list[dict] = field(default_factory=list)

    NULL_KEY = "<null>"

    def add(self, table: str, reason: str, key_column: str,
            keys: pd.Series) -> None:
        # value_counts drops nulls by default, so rows rejected *because* their
        # key was null would be removed and never counted, understating the
        # totals a quarantine ceiling is meant to police.
        keys = keys.astype("string").fillna(self.NULL_KEY)
        for value, count in keys.value_counts().items():
            self.records.append({
                "table_name": table, "reason": reason,
                "key_column": key_column, "key_value": value,
                "row_count": int(count),
            })

    def frame(self) -> pd.DataFrame:
        columns = ["table_name", "reason", "key_column", "key_value", "row_count"]
        if not self.records:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(self.records)[columns]

    def total(self, table: str | None = None) -> int:
        return sum(r["row_count"] for r in self.records
                   if table is None or r["table_name"] == table)


def _dedupe(frame: pd.DataFrame, key, table: str,
            quarantine: Quarantine) -> pd.DataFrame:
    """Collapse to one row per business key, quarantining genuine conflicts.

    Repeated identical rows are just a fact table collapsing to a dimension's
    grain, which is expected and uninteresting. A conflict is different: two
    rows share a key but disagree on their values, so keeping the first picks a
    winner arbitrarily. Only those are recorded, which keeps the quarantine
    count meaningful as a data-quality signal instead of counting every
    collapse.
    """
    columns = [key] if isinstance(key, str) else list(key)
    duplicated = frame.duplicated(columns, keep="first")
    if not duplicated.any():
        return frame

    kept = frame.loc[~duplicated]
    # A dropped row conflicts if it is not identical to the row kept for its key.
    identical = frame.merge(kept.drop_duplicates(columns), how="left",
                            on=list(frame.columns), indicator=True)
    conflicting = duplicated & (identical["_merge"].values == "left_only")

    if conflicting.any():
        label = "+".join(columns)
        values = (frame.loc[conflicting, columns].astype("string")
                  .agg("/".join, axis=1) if len(columns) > 1
                  else frame.loc[conflicting, columns[0]])
        quarantine.add(table, "conflicting duplicate business key", label, values)
    return kept


# ---------------------------------------------------------------- cleaning


def clean_planning(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw.copy()
    for column in ["gp_lgd_code", "plan_code", "activity_code"]:
        frame[column] = to_code(frame[column])
    frame["fiscal_year"] = to_fiscal_year(frame["plan_year"])
    return frame


def clean_expenditure(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw.copy().rename(columns=EXPENDITURE_RENAMES)
    for column in ["gp_lgd_code", "plan_code", "activity_code"]:
        frame[column] = to_code(frame[column])
    frame["fiscal_year"] = to_fiscal_year(frame["plan_year"])
    frame["approval_date"] = pd.to_datetime(frame["approval_date"], errors="coerce")
    return frame


def clean_vouchers(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw.copy()
    for column in ["gp_lgd_code", "state", "district", "block", "voucher_id",
                   "voucher_no"]:
        frame[column] = to_code(frame[column])
    frame["date"] = pd.to_datetime(frame["date"], dayfirst=True, errors="coerce")
    return frame


# ---------------------------------------------------------------- dimensions


def gram_panchayat(vouchers: pd.DataFrame, expenditure: pd.DataFrame,
                   quarantine: Quarantine) -> pd.DataFrame:
    """Geography, one row per panchayat, from two fact-grain extracts.

    Each side collapses to its dimension grain before the join. Merging first
    would pair every voucher with every expenditure row for the same
    panchayat: on the documented extracts that is 7.2 million intermediate
    rows to produce twenty, and it multiplies any conflicting-geography count
    by the size of the opposite side.
    """
    codes = _dedupe(
        vouchers[["gp_lgd_code", "gp_name", "state", "district", "block"]]
        .rename(columns={"state": "state_code", "district": "district_code",
                         "block": "block_code"}),
        "gp_lgd_code", "gram_panchayat", quarantine)

    names = _dedupe(
        expenditure[["gp_lgd_code", "state_name", "zp_name", "block_name"]],
        "gp_lgd_code", "gram_panchayat", quarantine)

    frame = codes.merge(names, on="gp_lgd_code", how="outer")
    return frame[["gp_lgd_code", "gp_name", "state_code", "state_name",
                  "district_code", "zp_name", "block_code", "block_name"]]


def plan(expenditure: pd.DataFrame, quarantine: Quarantine) -> pd.DataFrame:
    frame = expenditure[["plan_code", "gp_lgd_code", "fiscal_year", "plan_type",
                         "approval_date"]].copy()
    frame["plan_code_status"] = pd.NA
    frame = frame.dropna(subset=["plan_code"])
    return _dedupe(frame, "plan_code", "plan", quarantine)


# ---------------------------------------------------------------- planning


def planned_activity(planning: pd.DataFrame, quarantine: Quarantine) -> pd.DataFrame:
    frame = planning[CORE_COLUMNS].copy()
    return _dedupe(frame, "activity_code", "planned_activity", quarantine)


def satellite(planning: pd.DataFrame, table: str,
              quarantine: Quarantine) -> pd.DataFrame:
    frame = planning[["activity_code"] + SATELLITES[table]].copy()
    return _dedupe(frame, "activity_code", table, quarantine)


def activity_nsap(planning: pd.DataFrame) -> pd.DataFrame:
    """Wide NSAP beneficiary columns to one row per non-zero category."""
    present = [c for c in NSAP_COLUMNS if c in planning.columns]
    if not present:
        return pd.DataFrame(columns=["nsap_id", "activity_code", "category",
                                     "age_band", "gender", "beneficiary_count"])

    frame = planning[["activity_code"] + present].melt(
        id_vars="activity_code", var_name="column",
        value_name="beneficiary_count")
    frame["category"] = frame["column"].map(lambda c: NSAP_COLUMNS[c][0])
    frame["age_band"] = frame["column"].map(lambda c: NSAP_COLUMNS[c][1])
    frame["gender"] = frame["column"].map(lambda c: NSAP_COLUMNS[c][2])

    frame = frame[frame["beneficiary_count"].notna()
                  & (frame["beneficiary_count"] != 0)]
    frame = frame[["activity_code", "category", "age_band", "gender",
                   "beneficiary_count"]].reset_index(drop=True)
    frame.insert(0, "nsap_id", range(1, len(frame) + 1))
    return frame


# ---------------------------------------------------------------- expenditure


def activity_expenditure(expenditure: pd.DataFrame, activity_codes: set[str],
                         quarantine: Quarantine) -> pd.DataFrame:
    """One row per expenditure record, keyed by a surrogate the bridge reuses.

    The surrogate is assigned before quarantining so an expenditure_id always
    refers to the same source row, whether or not it was loaded.
    """
    frame = expenditure[[
        "activity_code", "plan_code", "gp_lgd_code", "fiscal_year", "s_no",
        "scheme_name", "approved_cost_action_plan", "technical_approved_cost",
        "admin_approved_cost", "general", "sc", "st", "total_expenditure",
    ]].copy().reset_index(drop=True)
    frame.insert(0, "expenditure_id", range(1, len(frame) + 1))

    orphan = ~frame["activity_code"].isin(activity_codes)
    if orphan.any():
        quarantine.add("activity_expenditure", "activity_code not in planning",
                       "activity_code", frame.loc[orphan, "activity_code"])
    return frame.loc[~orphan]


def voucher(vouchers: pd.DataFrame, gp_codes: set[str],
            quarantine: Quarantine) -> pd.DataFrame:
    frame = vouchers[["gp_lgd_code", "fiscal_year", "voucher_no", "voucher_id",
                      "direction", "type", "date", "month", "amount"]].copy()
    frame = frame.reset_index(drop=True)
    frame.insert(0, "voucher_pk", range(1, len(frame) + 1))

    orphan = ~frame["gp_lgd_code"].isin(gp_codes)
    if orphan.any():
        quarantine.add("voucher", "gp_lgd_code not in gram_panchayat",
                       "gp_lgd_code", frame.loc[orphan, "gp_lgd_code"])
    frame = frame.loc[~orphan]

    # Uniqueness is the composite business key, not voucher_id: voucher_id
    # collides across panchayats, and keying on it dropped real vouchers.
    return _dedupe(frame, ["gp_lgd_code", "fiscal_year", "voucher_no"],
                   "voucher", quarantine)


def activity_voucher(expenditure: pd.DataFrame, loaded_expenditure: pd.DataFrame,
                     vouchers: pd.DataFrame) -> pd.DataFrame:
    """Explode the pipe-delimited voucher lists into a bridge table.

    Each voucher's fiscal year is parsed from its own number rather than
    inherited from the plan, which is what lifts the match rate from a
    minority of rows to nearly all of them.
    """
    source = expenditure[["voucher_no_list", "voucher_date_list",
                          "voucher_cost_list", "gp_lgd_code"]].copy()
    source = source.reset_index(drop=True)
    source["expenditure_id"] = range(1, len(source) + 1)
    source = source[source["expenditure_id"].isin(loaded_expenditure["expenditure_id"])]
    source = source.dropna(subset=["voucher_no_list"])

    rows = []
    for record in source.itertuples(index=False):
        numbers = str(record.voucher_no_list).split(" | ")
        dates = str(record.voucher_date_list).split(" | ")
        costs = str(record.voucher_cost_list).split(" | ")
        for position, number in enumerate(numbers):
            number = number.strip()
            date = dates[position] if position < len(dates) else None
            cost = costs[position] if position < len(costs) else None
            rows.append({
                "expenditure_id": record.expenditure_id,
                "gp_lgd_code": record.gp_lgd_code,
                "fiscal_year": year_from_voucher_no(number),
                "voucher_no": number,
                "voucher_date": pd.to_datetime(date, dayfirst=True,
                                               errors="coerce") if date else None,
                "voucher_cost": pd.to_numeric(cost, errors="coerce") if cost else None,
            })

    columns = ["expenditure_id", "voucher_pk", "gp_lgd_code", "fiscal_year",
               "voucher_no", "voucher_date", "voucher_cost"]
    if not rows:
        return pd.DataFrame(columns=columns)

    bridge = pd.DataFrame(rows).merge(
        vouchers[["voucher_pk", "gp_lgd_code", "fiscal_year", "voucher_no"]],
        on=["gp_lgd_code", "fiscal_year", "voucher_no"], how="left")
    return bridge[columns]


# ---------------------------------------------------------------- extensions


def _restrict(frame: pd.DataFrame, table: str, column: str,
              allowed: set[str], quarantine: Quarantine) -> pd.DataFrame:
    orphan = ~frame[column].isin(allowed)
    if orphan.any():
        quarantine.add(table, f"{column} not in parent", column,
                       frame.loc[orphan, column])
    return frame.loc[~orphan]


def admin_approval(raw: pd.DataFrame, activity_codes: set[str],
                   gp_codes: set[str], quarantine: Quarantine) -> pd.DataFrame:
    frame = raw.rename(columns={
        "lgd_code": "gp_lgd_code", "gram_panchayat_name": "gp_name",
        "activityCd": "activity_code", "wrkPlnYr": "work_plan_year",
        "wrkAdmApprNo": "adm_approval_no",
        "wrkAdmApprSnctnOrdrDt": "adm_approval_sanction_date",
        "wrkProposedCost": "work_proposed_cost",
        "wrkAdmApprIssAuthrty": "adm_approval_authority",
    })[["row_id", "gp_lgd_code", "gp_name", "plan_year", "doc_type",
        "source_file", "activity_code", "work_plan_year", "adm_approval_no",
        "adm_approval_sanction_date", "work_proposed_cost",
        "adm_approval_authority"]].copy()

    for column in ["row_id", "gp_lgd_code", "activity_code", "adm_approval_no"]:
        frame[column] = to_code(frame[column])
    # ISO dates. dayfirst=True would misread every one of them.
    frame["adm_approval_sanction_date"] = pd.to_datetime(
        frame["adm_approval_sanction_date"], errors="coerce", format="ISO8601")
    frame["adm_approval_no"] = strip_leading_zeros(frame["adm_approval_no"])

    frame = _dedupe(frame, "row_id", "admin_approval", quarantine)
    frame = _restrict(frame, "admin_approval", "activity_code", activity_codes,
                      quarantine)
    return _restrict(frame, "admin_approval", "gp_lgd_code", gp_codes, quarantine)


def admin_approval_scheme(raw: pd.DataFrame, parent_row_ids: set[str],
                          quarantine: Quarantine) -> pd.DataFrame:
    frame = raw.rename(columns={
        "activityCd": "activity_code", "wrkSchmCd": "scheme_code",
        "wrkSchmCmpntCd": "scheme_component_code",
        "wrkAdmApprFndSnctnGen": "fund_sanctioned_general",
        "wrkAdmApprFndSnctnSc": "fund_sanctioned_sc",
        "wrkAdmApprFndSnctnSt": "fund_sanctioned_st",
        "wrkAdmApprFndSnctnTotal": "fund_sanctioned_total",
    })[["row_id", "parent_row_id", "pos", "activity_code", "scheme_code",
        "scheme_component_code", "fund_sanctioned_general",
        "fund_sanctioned_sc", "fund_sanctioned_st",
        "fund_sanctioned_total"]].copy()

    for column in ["row_id", "parent_row_id", "activity_code", "scheme_code",
                   "scheme_component_code"]:
        frame[column] = to_code(frame[column])

    frame = _dedupe(frame, "row_id", "admin_approval_scheme", quarantine)
    return _restrict(frame, "admin_approval_scheme", "parent_row_id",
                     parent_row_ids, quarantine)


def technical_approval(raw: pd.DataFrame, activity_codes: set[str],
                       gp_codes: set[str], quarantine: Quarantine) -> pd.DataFrame:
    frame = raw.rename(columns={
        "lgd_code": "gp_lgd_code", "gram_panchayat_name": "gp_name",
        "activityCd": "activity_code",
        "wrkTecApprReqFlg": "tec_approval_required",
        "wrkTecApprCost": "tec_approval_cost",
        "wrkTecApprIssAuthrty": "tec_approval_authority",
        "wrkTecApprOrdrNo": "tec_approval_order_no",
        "wrkTecApprOrdrDt": "tec_approval_order_date",
    })[["row_id", "gp_lgd_code", "gp_name", "plan_year", "doc_type",
        "source_file", "activity_code", "tec_approval_required",
        "tec_approval_cost", "tec_approval_authority", "tec_approval_order_no",
        "tec_approval_order_date"]].copy()

    for column in ["row_id", "gp_lgd_code", "activity_code",
                   "tec_approval_order_no"]:
        frame[column] = to_code(frame[column])
    frame["tec_approval_order_date"] = pd.to_datetime(
        frame["tec_approval_order_date"], errors="coerce", format="ISO8601")
    frame["tec_approval_order_no"] = strip_leading_zeros(
        frame["tec_approval_order_no"])

    frame = _dedupe(frame, "row_id", "technical_approval", quarantine)
    frame = _restrict(frame, "technical_approval", "activity_code",
                      activity_codes, quarantine)
    return _restrict(frame, "technical_approval", "gp_lgd_code", gp_codes,
                     quarantine)


def physical_progress(raw: pd.DataFrame, activity_codes: set[str],
                      quarantine: Quarantine) -> pd.DataFrame:
    """One row per progress capture, keeping the raw coordinate strings.

    Some cells hold several comma-separated GPS captures. The first is used as
    the point; the original string and the capture count are kept so nothing is
    discarded and a multi-capture row stays identifiable.
    """
    frame = raw.rename(columns={
        "activityCd": "activity_code", "fileUploadId": "file_upload_id",
        "plnunttypecode": "plan_unit_type_code",
    })[["row_id", "parent_row_id", "pos", "activity_code", "file_upload_id",
        "longitude", "latitude", "plan_unit_type_code"]].copy()

    for column in ["row_id", "parent_row_id", "activity_code", "file_upload_id",
                   "plan_unit_type_code"]:
        frame[column] = to_code(frame[column])

    frame["latitude_raw"] = raw["latitude"].astype("string").values
    frame["longitude_raw"] = raw["longitude"].astype("string").values
    frame["n_coords"] = count_coordinates(raw["latitude"]).values
    frame["latitude"] = first_coordinate(raw["latitude"]).values
    frame["longitude"] = first_coordinate(raw["longitude"]).values

    frame = _dedupe(frame, "row_id", "physical_progress", quarantine)
    return _restrict(frame, "physical_progress", "activity_code",
                     activity_codes, quarantine)


# ---------------------------------------------------------------- lookups


def dim_code(raw: pd.DataFrame, quarantine: Quarantine) -> pd.DataFrame:
    frame = raw.rename(columns={
        "variabe_codes": "code",      # the typo is in the source workbook
        "codes_desc": "description",
    })[["variable", "code", "description", "source", "confidence"]].copy()

    frame["code"] = to_code(frame["code"])
    frame["variable"] = frame["variable"].astype("string").str.strip()
    frame = frame.dropna(subset=["variable", "code"])
    return _dedupe(frame, ["variable", "code"], "dim_code", quarantine)


def dim_welfare_scheme(raw: pd.DataFrame, quarantine: Quarantine) -> pd.DataFrame:
    frame = raw.dropna(subset=["scheme_code"]).copy()
    frame["scheme_code"] = to_code(frame["scheme_code"])
    frame = _dedupe(frame, "scheme_code", "dim_welfare_scheme", quarantine)
    return frame[["scheme_code", "scheme_name"]]


def dim_lsdg_theme(raw: pd.DataFrame) -> pd.DataFrame:
    return raw.rename(columns={
        "focus area": "focus_area_name",
        "dominant LSDG theme": "lsdg_theme",
        "distinct themes seen": "distinct_themes",
        "rows": "n_rows",
    }).dropna(subset=["focus_area_name"])[
        ["focus_area_name", "lsdg_theme", "distinct_themes", "n_rows"]]
