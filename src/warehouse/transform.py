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

import re

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

import pandas as pd

from .clean import (
    strip_leading_zeros, to_code, to_datetime, to_decimal_money, to_int, ungroup_digits,
)
from .geography import GEOGRAPHY_COLUMNS

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


def _is_fractional(text: object) -> bool:
    """A count that is not a whole number is malformed, not roundable.

    ``clean.to_int`` ends in ``numeric.round()``, so "1.5" becomes 2 and looks
    like a clean parse to anything that only checks for null afterwards. A
    rounded count is an invented one.

    Module level rather than nested in one transform: activity_nsap's
    beneficiary counts and gp_profile's populations are the same question
    about the same kind of column, and two copies of this predicate would
    drift (#116, #123).
    """

    if pd.isna(text) or text == "":
        return False
    try:
        value = Decimal(text)
    except InvalidOperation:
        return False
    if not value.is_finite():
        return False
    return value != value.to_integral_value()


def _is_non_finite(text: object) -> bool:
    """NaN and Infinity: not fractional, and still not a count."""

    if pd.isna(text) or text == "":
        return False
    try:
        value = Decimal(text)
    except InvalidOperation:
        return False
    return not value.is_finite()


class EmptyRequiredColumn(ValueError):
    """A required column resolved by name, but none of its values survived.

    The sibling case ``RequiredFieldUnresolved`` does not cover, and the one
    that is easier to reach: the header is still there and the values are not.
    ``_first_present`` proves a column NAME exists; nothing proved the values
    parsed. A scrape emitting blanks -- or text where a count belongs -- would
    otherwise publish the right number of rows with entirely NULL measures,
    past every check there is, because the columns are nullable and no
    conformance rule reads their contents (#123).
    """

    def __init__(self, *, table: str, columns: tuple[str, ...], rows: int) -> None:
        self.table = table
        self.columns = columns
        self.rows = rows
        super().__init__(
            f"{table}: required column(s) {columns!r} are null in all {rows:,} loaded "
            "row(s); the source column is present but carries no readable values"
        )


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
                    *, source_system: str, source_run_id: str,
                    geography: Mapping[str, Mapping[str, str]] | None = None) -> pd.DataFrame:
    """One row per LGD code, built from every kind's own provenance.

    Every kind independently records the GP it was scraped for; unioning
    them is the "proven mapping" -- the folder-name regex is the same
    parser for every kind, unlike a business field whose spelling can differ
    kind to kind.

    ``geography`` is the ``{gp_lgd_code: {column: value}}`` lookup from
    :mod:`warehouse.geography`, left-joined on to fill the block/district/
    state columns the folder name cannot carry (#61). It is passed in rather
    than read here because every function in this module is pure -- see the
    module docstring. Omitting it leaves those columns null, which is what
    the callers that only care about the dimension's identity (tests, and
    any future caller building a code-only roster) want.

    The join is on ``gp_lgd_code`` and never on ``gp_name``: 505 GP names are
    shared by more than one GP, so a name join would quietly hand some GPs
    another GP's district.
    """

    parts = []
    for raw in root_frames:
        if raw.empty:
            continue
        parts.append(pd.DataFrame({
            "gp_lgd_code": to_code(raw["gp_code"]),
            "gp_name": raw["gram_panchayat_name"].astype("string"),
        }))
    columns = ["gp_lgd_code", "gp_name", *GEOGRAPHY_COLUMNS]
    if not parts:
        return pd.DataFrame(columns=columns)
    combined = pd.concat(parts, ignore_index=True).dropna(subset=["gp_lgd_code"])
    deduped = _dedupe(
        combined, ["gp_lgd_code"], "gram_panchayat", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    lookup = geography or {}
    for column in GEOGRAPHY_COLUMNS:
        deduped[column] = deduped["gp_lgd_code"].map(
            lambda code, _c=column: lookup.get(code, {}).get(_c)
        ).astype("string")
    return deduped


# --------------------------------------------------------------------- gp_profile

# The profile CSV's own header spelling -> the DDL column. Ten of its 99
# columns, written out rather than taken by prefix: the file's columns are
# whatever the scrape happened to see, so a pattern match would silently
# widen the table the next time the portal adds a field.
GP_PROFILE_KEY = "basic_info_lgd"
GP_PROFILE_RENAMES = {
    "demographic_details_total_gender_wise_population": "total_population",
    "demographic_details_male_population": "male_population",
    "demographic_details_female_population": "female_population",
    "demographic_details_transgender_population": "transgender_population",
    "demographic_details_children_population": "children_population",
    "demographic_details_sc_population": "sc_population",
    "demographic_details_st_population": "st_population",
    "demographic_details_obc_population": "obc_population",
    "demographic_details_general_population": "general_population",
    "general_no_of_households": "households",
}
GP_PROFILE_COLUMNS = [
    "source_system", "source_run_id", "gp_lgd_code", *GP_PROFILE_RENAMES.values(),
]


VOUCHER_COLUMNS: list[str] = [
    "gp_lgd_code", "fiscal_year", "voucher_no", "voucher_id",
    "direction", "type", "date", "month", "amount",
]

ACTIVITY_VOUCHER_COLUMNS: list[str] = [
    "expenditure_id", "voucher_pk", "gp_lgd_code", "fiscal_year",
    "voucher_no", "voucher_date", "voucher_cost",
]
# The three parallel pipe-delimited cells, and the column each becomes.
AV_PIPE_COLUMNS: dict[str, str] = {
    "Voucher No": "voucher_no",
    "Voucher Cost": "voucher_cost",
    "Voucher Date": "voucher_date",
}
# `XVFC/2021-22/P/2` -> 2021-22. The fiscal year of a voucher comes from the
# voucher number, never from the plan year: #49 records that vouchers are
# often paid in a later year than the plan they settle, and voucher.fiscal_year
# is half the key this table joins on.
VOUCHER_NO_YEAR_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?!\d)")


class MisalignedVoucherCells(ValueError):
    """The parallel voucher cells of one row split to different lengths."""


def _voucher_fiscal_year(voucher_no: object) -> str | None:
    """The `YYYY-YYYY` fiscal year named inside a voucher number."""

    if not isinstance(voucher_no, str):
        return None
    match = VOUCHER_NO_YEAR_RE.search(voucher_no)
    if match is None:
        return None
    start, end = match.group(1), match.group(2)
    # "2021-22" -> "2021-2022": the century comes from the start year, so the
    # 1999-00 rollover cannot produce "1999-1900".
    return f"{start}-{int(start) // 100 * 100 + int(end) + (100 if int(end) < int(start) % 100 else 0)}"


def activity_voucher(
    source: pd.DataFrame, expenditures: pd.DataFrame, voucher_keys: pd.DataFrame,
    quarantine: Quarantine, *, source_system: str, source_run_id: str,
    linked_expenditure_ids: set[int] | None = None,
) -> pd.DataFrame:
    """Explode the parallel voucher cells into one row per voucher reference (#49).

    The source packs a variable number of voucher references into three
    pipe-delimited cells that line up positionally::

        Voucher No:   XVFC/2023-24/P/7 | XVFC/2023-24/P/7 | XVFC/2023-24/P/7
        Voucher Cost: 1028.0           | 1143.0           | 116972.0
        Voucher Date: 05/07/2023       | 05/07/2023       | 05/07/2023

    **Different lengths raise rather than truncate**, which #49 names
    explicitly. `zip` would silently drop the tail of the longer cells, and
    the rows it dropped would be real payments -- money vanishing with no
    error and no quarantine row. A row whose cells disagree is corrupt, and
    the whole load should stop rather than publish a partial ledger.

    Note the same `voucher_no` legitimately repeats within one row, as above:
    three separate payments settled against one voucher. That is why
    `activity_voucher` has no primary key, and why nothing here deduplicates.

    ``voucher_pk`` is left NULL where the accounting extract does not reach
    the cited voucher. #49 records this as by design -- vouchers cited by the
    expenditure file but absent from accounting are legitimately unmatched,
    not invalid -- and #171 makes it much more common than the pilot's 488,
    since the accounting extract is missing 358 GPs entirely. The rows are
    kept either way; dropping them would lose the expenditure's own record of
    what it paid.
    """

    if source.empty:
        return pd.DataFrame(columns=ACTIVITY_VOUCHER_COLUMNS)

    keep = ["gp_lgd_code", "plan_code", "activity_code", "s_no"]
    identity = _base_identity(source)
    frame = pd.DataFrame({
        "gp_lgd_code": identity["gp_lgd_code"],
        "activity_code": to_code(identity["activity_code"]),
    })
    for canonical, candidates in (("plan_code", RE_CANDIDATES["plan_code"]),
                                  ("s_no", RE_CANDIDATES["s_no"])):
        series, _ = _first_present(source, "activity_voucher", canonical, candidates, required=True)
        frame[canonical] = to_code(series)
    for column, target in AV_PIPE_COLUMNS.items():
        series, _ = _first_present(
            source, "activity_voucher", target, (column, target), required=True,
        )
        frame[target] = series.astype("string")

    split = {
        target: frame[target].fillna("").map(lambda text: [p.strip() for p in text.split("|")])
        for target in AV_PIPE_COLUMNS.values()
    }
    lengths = pd.DataFrame(split).map(len)
    misaligned = (lengths.nunique(axis=1) > 1)
    if misaligned.any():
        offending = frame.loc[misaligned, keep].head(3).to_dict("records")
        raise MisalignedVoucherCells(
            f"{int(misaligned.sum())} row(s) have Voucher No/Cost/Date cells of "
            f"differing lengths; the first few are {offending}. Positional "
            f"recombination is only meaningful when the three agree, so this is "
            f"refused rather than zip-truncated (#49)."
        )

    exploded = frame.assign(**split).explode(list(AV_PIPE_COLUMNS.values()))
    def _blank(column: str) -> pd.Series:
        return exploded[column].astype("string").fillna("").str.strip() == ""

    # A position with nothing in any of the three cells is not a voucher
    # reference at all -- a row citing no vouchers explodes to one empty
    # position -- and is dropped silently.
    #
    # A position with a blank voucher_no but a populated cost or date is a
    # different thing entirely: a payment whose voucher number is missing.
    # Dropping it silently (as `str.len() > 0` alone did) would make
    # sum(voucher_cost) quietly fall below the expenditure row's own
    # total_expenditure, which is the one arithmetic anybody would use to
    # check this table. Quarantined instead, keyed by activity_code since the
    # voucher number is exactly what it lacks.
    all_blank = _blank("voucher_no") & _blank("voucher_cost") & _blank("voucher_date")
    partial = _blank("voucher_no") & ~all_blank
    if partial.any():
        quarantine.add(
            "activity_voucher", "partial_voucher_slot",
            "voucher position has a cost or date but no voucher number",
            "activity_code", exploded.loc[partial, "activity_code"],
            source_system=source_system, source_run_id=source_run_id,
        )
    exploded = exploded[~all_blank & ~partial]
    if exploded.empty:
        return pd.DataFrame(columns=ACTIVITY_VOUCHER_COLUMNS)

    exploded["fiscal_year"] = exploded["voucher_no"].map(_voucher_fiscal_year)
    exploded["voucher_cost"] = to_decimal_money(exploded["voucher_cost"])
    # dayfirst: the expenditure file writes DD/MM/YYYY here, same as the
    # accounting source, even though its other dates are ISO.
    exploded["voucher_date"] = to_datetime(exploded["voucher_date"], dayfirst=True)

    # `indicator`, not an index comparison: `merge` returns a fresh RangeIndex
    # while `explode` leaves repeated original labels, so `linked.index` and
    # `exploded.index` share no meaning and selecting the unmatched rows by
    # index membership names the wrong vouchers. Same device `_dedupe` uses.
    linked = exploded.merge(
        expenditures[["expenditure_id", *keep]], how="left", on=keep, indicator=True,
    )
    unmatched = linked["_merge"].to_numpy() == "left_only"
    if unmatched.any():
        # An expenditure line that did not survive its own quarantine cannot
        # have a bridge row: expenditure_id is a foreign key into it.
        quarantine.add(
            "activity_voucher", "orphan_expenditure",
            "voucher reference has no loaded activity_expenditure row",
            "voucher_no", linked.loc[unmatched, "voucher_no"],
            source_system=source_system, source_run_id=source_run_id,
        )
    linked = linked.loc[~unmatched].drop(columns="_merge")

    # An expenditure line that already has its bridge rows must not get them
    # again from a second snapshot restating the same line. This table is
    # legitimately 1:many -- three payments can settle against one voucher
    # number, so row-level dedupe would be wrong -- and the unit that must
    # not repeat is therefore the expenditure_id's whole set of references,
    # not an individual row.
    #
    # The case this exists for: two overlapping EXPENDITURE snapshots. The
    # second one's activity_expenditure rows are suppressed by the
    # cross-snapshot key guard, but its voucher references still RESOLVE --
    # against the surviving row -- so without this they would double the
    # bridge while the fact table stayed correct.
    if linked_expenditure_ids:
        repeated = linked["expenditure_id"].isin(linked_expenditure_ids).to_numpy()
        if repeated.any():
            quarantine.add(
                "activity_voucher", "cross_snapshot_duplicate_key",
                "this expenditure line already has its voucher references loaded",
                "voucher_no", linked.loc[repeated, "voucher_no"],
                source_system=source_system, source_run_id=source_run_id,
            )
        linked = linked.loc[~repeated]

    linked = linked.merge(
        voucher_keys, how="left", on=["gp_lgd_code", "fiscal_year", "voucher_no"],
    )
    out = _ensure_columns(linked, ACTIVITY_VOUCHER_COLUMNS)
    return out[ACTIVITY_VOUCHER_COLUMNS].reset_index(drop=True)


def voucher(
    frame: pd.DataFrame, gp_codes: set[str], quarantine: Quarantine,
    *, source_system: str, source_run_id: str, start_id: int = 1,
    loaded_keys: set[tuple[str, str, str]] | None = None,
) -> pd.DataFrame:
    """Shape one voucher frame and assign its voucher_pk (#46, #129).

    ``voucher_pk`` is an INTEGER surrogate assigned exactly the way
    ``activity_expenditure.expenditure_id`` is: contiguously from
    ``start_id``, *after* every quarantine step, so ids are dense and 1:1
    with the rows returned, and the caller advances ``start_id`` between
    snapshots. Mirrored rather than reinvented -- #129 asks for the
    deterministic assignment #69 built with a SQLite ``ROW_NUMBER``, and the
    property that actually matters (ids independent of chunk size and
    iteration order) already holds here, because the frame is fully
    materialised and sorted before ids are handed out.

    ``voucher_id`` is **not** the key and is never treated as one. #46
    verified it repeats across 116 distinct GP/year/direction combinations:
    it is a portal-internal sequence, unique only within a GP. The real
    identity is ``(gp_lgd_code, fiscal_year, voucher_no)``, which the DDL
    carries as a UNIQUE constraint, so a collision has to reach quarantine
    rather than fail the insert.

    Rows are sorted by that natural key before ids are assigned. Without it
    ``voucher_pk`` would follow the order files happened to be walked in, and
    two builds of the same snapshot could disagree about which voucher is 7 --
    which matters because ``activity_voucher.voucher_pk`` (#49) is a foreign
    key into this table.
    """

    columns = ["voucher_pk"] + VOUCHER_COLUMNS
    if frame.empty:
        return pd.DataFrame(columns=columns)

    out = pd.DataFrame({
        "source_system": frame["source_system"],
        "source_run_id": frame["source_run_id"],
        "gp_lgd_code": to_code(frame["gp_code"]),
        "fiscal_year": frame["fiscal_year"],
    })
    for column in ("voucher_no", "voucher_id", "direction", "type", "month"):
        out[column] = _ensure_columns(frame, [column])[column]
    out["amount"] = to_decimal_money(_ensure_columns(frame, ["amount"])["amount"])
    # dayfirst because the source writes DD/MM/YYYY (#46). Parsed without it,
    # 03/04/2022 silently becomes 4 March instead of 3 April -- a wrong answer
    # for every voucher before the 13th of a month, and no error anywhere.
    out["date"] = to_datetime(_ensure_columns(frame, ["date"])["date"], dayfirst=True)

    # No guard on `direction`'s spelling: the normalizer sets it from the
    # array a voucher came out of (`normalize.VOUCHER_DIRECTIONS`), so it is
    # 'receipt' or 'payment' by construction and a validity check here could
    # not be reached, let alone tested. An absent column is a different
    # matter and is caught below.
    missing = out["voucher_no"].isna() | out["gp_lgd_code"].isna() | out["direction"].isna()
    if missing.any():
        quarantine.add(
            "voucher", "missing_key",
            "voucher row lacks gp_lgd_code, voucher_no or direction",
            "voucher_no", out.loc[missing, "voucher_no"],
            source_system=source_system, source_run_id=source_run_id,
        )
    out = out[~missing]

    out = _dedupe(
        out, ["gp_lgd_code", "fiscal_year", "voucher_no"], "voucher", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    out = _restrict(
        out, "voucher", "gp_lgd_code", gp_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id, reason_code="orphan_gp",
    )
    # `_dedupe` above only sees this frame. Two accounting snapshots in one
    # build can each hold the same (gp, year, voucher_no) -- legitimately, if
    # a later run re-scrapes GPs the first one covered -- and each would
    # survive its own dedupe, so the second insert would hit the table's
    # UNIQUE constraint and abort the build. Checked against what earlier
    # snapshots already inserted, the way `_merge_gram_panchayat` does for the
    # conformed dimension, and passed in as a set the way `_restrict` takes
    # `gp_codes`. Filtered before ids are handed out, so voucher_pk stays
    # dense and activity_voucher's foreign key has no gaps to explain.
    if loaded_keys:
        already = pd.Series(
            [key in loaded_keys for key in zip(
                out["gp_lgd_code"], out["fiscal_year"], out["voucher_no"], strict=True,
            )],
            index=out.index,
        )
        if already.any():
            quarantine.add(
                "voucher", "cross_snapshot_duplicate_key",
                "an earlier snapshot in this build already loaded this voucher",
                "voucher_no", out.loc[already, "voucher_no"],
                source_system=source_system, source_run_id=source_run_id,
            )
        out = out[~already]
    out = out.sort_values(
        ["gp_lgd_code", "fiscal_year", "voucher_no"], kind="mergesort",
    ).reset_index(drop=True)
    out = _ensure_columns(out, VOUCHER_COLUMNS)
    result = out[VOUCHER_COLUMNS].copy()
    result.insert(0, "voucher_pk", range(start_id, start_id + len(result)))
    return result


def gp_profile(profile: pd.DataFrame, gp_codes: set[str], quarantine: Quarantine,
               *, source_system: str, source_run_id: str,
               loaded_keys: set[str] | None = None) -> pd.DataFrame:
    """One row per GP, from the panchayat profile extract (#123).

    Two rejections, kept apart because they mean different things:

    * **84 rows carry no LGD code at all** -- placeholders for GPs whose
      profile was never filled in. Loaded unfiltered, 83 of them collide on
      the empty string and fail the primary key. They are quarantined rather
      than filtered so the count stays visible; dropping them silently is how
      a shrinking source goes unnoticed. Note they are not blank rows: they
      still carry the scrape's own `param__*` request fields, which is why
      "the row is empty" is not a safe test for them.
    * **A keyed row whose GP is not in `gram_panchayat`** is an orphan, and
      goes to quarantine under the standard reason code.

    Every one of the ten measures is resolved with ``required=True``, and then
    checked again after conversion. The two catch different failures and only
    the pair is sufficient: ``required=True`` catches an upstream *rename*,
    while ``EmptyRequiredColumn`` catches the header surviving with no readable
    values behind it. Either way, loading 6,710 rows of all-NULL demographics
    would pass every other check this repo has -- right row count, no orphans,
    valid key -- and be wrong in the only way that matters.

    A row carrying a value that is present but unreadable is quarantined
    rather than loaded with a hole: nine good measures do not make a tenth
    trustworthy, and a silently-NULL cell is the thing this whole function is
    arranged to prevent. "Unreadable" includes values that parse *too*
    willingly: ``to_int`` rounds, so a population of "1.5" would otherwise be
    stored as 2 -- an invented number rather than a missing one -- and it
    carries a negative through untouched, though nobody ever counted -1
    people.
    """

    if profile.empty:
        return pd.DataFrame(columns=GP_PROFILE_COLUMNS)

    key, _ = _first_present(
        profile, "gp_profile", "gp_lgd_code", (GP_PROFILE_KEY,), required=True,
    )
    out = pd.DataFrame({
        "source_system": profile["source_system"],
        "source_run_id": profile["source_run_id"],
        "gp_lgd_code": to_code(key),
    })
    # A value that was present in the source and did NOT survive the cast is a
    # parse failure, not an absent measure, and `to_int` cannot tell them apart
    # -- it coerces both to NA. Tracked here, while the raw text is still in
    # reach, so the row can be quarantined rather than loaded with a hole.
    unreadable = pd.Series(False, index=profile.index)
    for source, target in GP_PROFILE_RENAMES.items():
        series, _ = _first_present(
            profile, "gp_profile", target, (source,), required=True,
        )
        converted = to_int(series)
        text = series.astype("string").str.strip()
        # `to_int` rounds, so a fractional count parses cleanly and a
        # null-check alone would accept an invented one. Same grouping cleanup
        # and same two predicates as activity_nsap, which asks this about the
        # same kind of column.
        cleaned = text.map(
            lambda value: ungroup_digits(value) if isinstance(value, str) else value
        ).astype("string")
        present = text.notna() & (text != "")
        # A negative population or household count is impossible, and `to_int`
        # carries it through untouched -- it is neither null, fractional nor
        # non-finite, so every other predicate here accepts it.
        unreadable |= present & (
            converted.isna()
            | cleaned.map(_is_fractional)
            | cleaned.map(_is_non_finite)
            | (converted < 0)
        )
        out[target] = converted

    unkeyed = out["gp_lgd_code"].isna()
    if unkeyed.any():
        quarantine.add(
            "gp_profile", "missing_key", "profile row carries no LGD code",
            "gp_lgd_code", out.loc[unkeyed, "gp_lgd_code"],
            source_system=source_system, source_run_id=source_run_id,
        )
        out = out.loc[~unkeyed]

    out = _dedupe(
        out, ["gp_lgd_code"], "gp_profile", quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    out = _restrict(
        out, "gp_profile", "gp_lgd_code", gp_codes, quarantine,
        source_system=source_system, source_run_id=source_run_id,
    )
    # The same cross-snapshot collision `voucher` guards against, and for the
    # same reason: `_dedupe` sees one frame, but two profile snapshots in one
    # build can both carry a GP and the second insert would hit gp_lgd_code's
    # PRIMARY KEY. Present since #123; found by reviewing the sibling.
    if loaded_keys:
        already = out["gp_lgd_code"].isin(loaded_keys)
        if already.any():
            quarantine.add(
                "gp_profile", "cross_snapshot_duplicate_key",
                "an earlier snapshot in this build already loaded this GP's profile",
                "gp_lgd_code", out.loc[already, "gp_lgd_code"],
                source_system=source_system, source_run_id=source_run_id,
            )
        out = out[~already]

    bad = unreadable.reindex(out.index, fill_value=False)
    if bad.any():
        quarantine.add(
            "gp_profile", "unreadable_measure",
            "a population or household value could not be read as a whole number",
            "gp_lgd_code", out.loc[bad, "gp_lgd_code"],
            source_system=source_system, source_run_id=source_run_id,
        )
        out = out.loc[~bad]

    # Last, on the rows actually being loaded. `required=True` above only
    # proves the column NAME survived upstream; this is what makes the
    # docstring's claim true rather than aspirational.
    if not out.empty:
        empty = tuple(
            column for column in GP_PROFILE_RENAMES.values() if out[column].isna().all()
        )
        if empty:
            raise EmptyRequiredColumn(table="gp_profile", columns=empty, rows=len(out))
    return out[GP_PROFILE_COLUMNS]


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


def activity_nsap(pl: pd.DataFrame, activity_codes: set[str], quarantine: Quarantine,
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

    Fractional counts are quarantined, not rounded.
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
    # Digit grouping is validated here, not stripped, for the same reason
    # clean.to_int validates it (#127): "1,2" is malformed, not twelve.
    # Checked in this function as well as in to_int because the detectors
    # below read `cleaned` -- and because a value that only became NA inside
    # to_int would be dropped by the notna() filter with no quarantine row,
    # which is the one outcome this function exists to prevent.
    raw = melted["beneficiary_count"].astype("string").str.strip()
    cleaned = raw.map(
        lambda text: ungroup_digits(text) if isinstance(text, str) else text
    ).astype("string")
    malformed = raw.notna() & (raw != "") & cleaned.isna()

    fractional = cleaned.map(_is_fractional)
    non_finite = cleaned.map(_is_non_finite)
    if fractional.any():
        # A fractional count (e.g. 1.5) is malformed, not roundable.
        quarantine.add(
            "activity_nsap", "fractional_beneficiary_count",
            "beneficiary_count is fractional",
            "activity_code", melted.loc[fractional, "activity_code"],
            source_system=source_system, source_run_id=source_run_id,
        )
    if malformed.any():
        quarantine.add(
            "activity_nsap", "malformed_beneficiary_count",
            "beneficiary_count has malformed digit grouping",
            "activity_code", melted.loc[malformed, "activity_code"],
            source_system=source_system, source_run_id=source_run_id,
        )
    # NaN/Infinity are non-fractional but still unparseable by to_int.
    melted = melted[~fractional & ~non_finite & ~malformed]
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

_ASSET_FIELDS = {
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

# Both spellings the same fields arrive under, because `assetDetails` is not
# always the same JSON type (#159).
#
# When it is a LIST, `normalize._child_rows` emits a `pl__*` child table whose
# columns are the bare names above. When it is a MAPPING -- which is what the
# full-state eGramSwaraj payload actually sends, `None` or `dict` and never a
# list across every record sampled -- `_child_rows` does not descend at all,
# and `_flatten_scalars` folds it into the parent `pl` frame prefixed
# `assetDetails_`.
#
# Only the bare spelling was recognised, so on the real source nothing matched
# the asset signature, `activity_asset` received an empty frame, and all
# 4,073,745 rows were 1:1 filler with every asset column NULL -- against
# 1,861,715 real rows in the externally built database. The data was never
# lost; it was sitting in `pl`, unread.
#
# The two key sets are disjoint, so one map handles both shapes and a source
# that changes type does not need a second code path. `pandas.rename` ignores
# keys a frame does not carry.
ASSET_CHILD_RENAMES = {
    **_ASSET_FIELDS,
    **{f"assetDetails_{source}": target for source, target in _ASSET_FIELDS.items()},
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

    Strictly 1:1 cuts both ways: an activity with no asset/fund child array
    element at all still needs a row here, synthesized all-null, exactly
    like activity_delegation/activity_training/activity_community_service
    (which get their one-row-per-activity guarantee for free, by reading
    straight off the ``pl`` frame instead of a separate child array).
    ``conformance.check_satellite_row_parity`` requires an exact
    row-per-planned_activity match across all five satellites; a childless
    activity silently dropped here would build successfully and then fail
    that check.
    """

    keep = ["source_system", "source_run_id", "activity_code"] + columns
    if child.empty:
        frame = pd.DataFrame(columns=keep)
    else:
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
        frame = _restrict(
            frame, table, "activity_code", activity_codes, quarantine,
            source_system=source_system, source_run_id=source_run_id,
        )

    missing = activity_codes - set(frame["activity_code"])
    if missing:
        filler = pd.DataFrame({"activity_code": sorted(missing)})
        filler["source_system"] = source_system
        filler["source_run_id"] = source_run_id
        filler = _ensure_columns(filler, keep)[keep]
        frame = pd.concat([frame, filler], ignore_index=True)
    return frame


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
    # Through to_code like every other activity_code column in this module
    # (eight of them). This was the one that assigned the raw provenance value
    # straight through, so the same activity could be spelled one way here and
    # another in planned_activity -- in a column whose only purpose is to join
    # them. Inert on today's data (every sampled activityCd is a JSON int, so
    # str() and to_code() agree), which is why it is a one-line alignment
    # rather than an issue.
    out["activity_code"] = to_code(out["business_id"])
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

# Every spelling activity_expenditure knows how to read. A frame carrying
# none of them is not an expenditure extract at all -- notably the `re`
# canonical kind, which is getLbAllocatedAmountData (budgetary allocation:
# planYear, planUnitCode, and a scheme-allocation child list), not
# expenditure. See src/ingest/egramSwaraj_API/config.py for the endpoint
# map. activity_expenditure's real source is the separate expenditure
# extract in #49, which has no loader yet.
RE_SOURCE_COLUMNS = frozenset(
    candidate for candidates in RE_CANDIDATES.values() for candidate in candidates
)


def is_expenditure_frame(frame: pd.DataFrame) -> bool:
    """Does this frame claim to be an expenditure extract at all?

    Distinguishes "wrong source wired in" from "right source, renamed
    column": the second must keep failing loudly through
    ``RE_REQUIRED_FIELDS``, so this asks only whether *any* expenditure
    spelling is present, never whether the required ones are.
    """

    return bool(RE_SOURCE_COLUMNS & set(frame.columns))


def activity_expenditure(
    re: pd.DataFrame, gp_codes: set[str], quarantine: Quarantine,
    *, source_system: str, source_run_id: str,
    resolutions: FieldResolutions | None = None,
    start_id: int = 1,
    loaded_keys: set[tuple[str, str, str, str]] | None = None,
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
    # The same cross-snapshot guard voucher and gp_profile carry, and the most
    # important of the three: those two collide on a UNIQUE/PRIMARY KEY and
    # abort the build, so a second overlapping snapshot fails loudly. This
    # table's only key is the `expenditure_id` surrogate assigned below, so
    # nothing would stop the duplicates -- `total_expenditure` would silently
    # double and the activity_voucher bridge would duplicate with it, on a
    # green build and a green conformance run.
    #
    # Note the `_dedupe` above cannot catch this even in principle: its key
    # includes source_run_id, so the same business row observed by two runs is
    # two rows to it. That is right within a frame and wrong across them.
    #
    # Bridge rows for the rows dropped here find no expenditure_id and are
    # counted by activity_voucher's own orphan_expenditure path, which is the
    # correct outcome -- they are duplicate references to an already-loaded
    # expenditure line.
    if loaded_keys:
        already = pd.Series(
            [key in loaded_keys for key in zip(
                frame["gp_lgd_code"], frame["plan_code"],
                frame["activity_code"], frame["s_no"], strict=True,
            )],
            index=frame.index,
        )
        if already.any():
            quarantine.add(
                "activity_expenditure", "cross_snapshot_duplicate_key",
                "an earlier snapshot in this build already loaded this expenditure line",
                "activity_code", frame.loc[already, "activity_code"],
                source_system=source_system, source_run_id=source_run_id,
            )
        frame = frame[~already]
    frame = frame.reset_index(drop=True)
    frame.insert(0, "expenditure_id", range(start_id, start_id + len(frame)))
    return frame
