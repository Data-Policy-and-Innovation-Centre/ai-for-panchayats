"""Build the panchayat DuckDB warehouse.

Publication sequence, enforced in this order:

    1. validate selected snapshots and manifests   (select.resolve_snapshots)
    2. create a temporary DuckDB                    (tempfile, not target)
    3. execute explicit DDL                         (schema.DDL)
    4. load canonical Parquet through pure transforms (transform.py, load.py)
    5. quarantine invalid rows                      (transform.Quarantine)
    6. validate PK/FK/counts/grains/provenance      (validate.run_checks)
    7. commit and close the temporary DB
    8. atomically replace the target DuckDB path    (os.replace)

A failed build (a rejected preflight, a Python exception mid-load, or a
failed post-load check) never touches the target path: everything happens in
a sibling temporary file that is removed on any exit other than a clean
``os.replace`` at the very end. The previous database, if any, is therefore
always left exactly as it was.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from . import transform
from .config import WarehouseSettings, load_settings
from .dimensions import dimension_frames
from .geography import gp_geography
from .views import materialize
from .load import DEFAULT_BATCH_SIZE, insert, read_table
from .schema import CREATE_ORDER, DDL, RESET_ORDER
from .select import SelectedSnapshot, resolve_snapshots
from .transform import FieldResolution, FieldResolutions, Quarantine
from .validate import Check, ValidationFailed, run_checks

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BuildResult:
    target: Path
    counts: dict[str, int]
    quarantine_count: int
    checks: tuple[Check, ...]
    consumed_tables: dict[str, tuple[str, ...]]
    unconsumed_tables: dict[str, tuple[str, ...]]
    field_resolutions: tuple[FieldResolution, ...]


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Drop children before parents, then create parents before children."""

    for table in RESET_ORDER:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    for table in CREATE_ORDER:
        con.execute(DDL[table])


def _child_tables(tables: dict[str, tuple[str, ...]], parent_kind: str, keyword: str) -> list[str]:
    """Direct children of ``parent_kind`` only -- never a grandchild.

    ``pl__fundlist__lineitems`` must not match here just because "fund" is a
    substring of "fundlist": a deeper nested array has its own, different
    row shape and would otherwise be silently folded into the wrong table
    (e.g. its rows have no ``schemeCode``/``amountTotal`` of their own,
    so they would load as spurious all-null fund rows). A table one level
    too deep is left for ``unconsumed_tables`` to report instead.
    """

    prefix = f"{parent_kind}__"
    return sorted(
        name for name in tables
        if name.startswith(prefix)
        and "__" not in name[len(prefix):]
        and keyword in name[len(prefix):]
    )


def _read(root: Path, tables: dict[str, tuple[str, ...]], name: str) -> pd.DataFrame:
    return read_table(root, tables.get(name, ()))


def _merge_gram_panchayat(
    con: duckdb.DuckDBPyConnection, frame: pd.DataFrame, quarantine: Quarantine,
    *, source_system: str, source_run_id: str, batch_size: int,
) -> int:
    """Insert new GP codes, quarantine ones that conflict with what is loaded.

    ``gram_panchayat`` is a conformed dimension with no per-source key (a GP
    is the same GP no matter which run observed it), so merging a second
    snapshot's contribution has to check what earlier snapshots in *this same
    build* already inserted, not just what is unique within this frame.
    """

    if frame.empty:
        return 0
    existing = con.execute("SELECT gp_lgd_code, gp_name FROM gram_panchayat").fetchdf()
    if existing.empty:
        new_rows = frame
    else:
        merged = frame.merge(existing, on="gp_lgd_code", how="left", suffixes=("", "_existing"))
        already_present = merged["gp_name_existing"].notna()
        conflicting = already_present & (merged["gp_name"] != merged["gp_name_existing"])
        if conflicting.any():
            quarantine.add(
                "gram_panchayat", "conflicting_duplicate_key",
                "gp_lgd_code already loaded with a different gp_name",
                "gp_lgd_code", frame.loc[conflicting.to_numpy(), "gp_lgd_code"],
                source_system=source_system, source_run_id=source_run_id,
            )
        new_rows = frame.loc[~already_present.to_numpy()]
    return insert(con, "gram_panchayat", new_rows, batch_size=batch_size)


def populate(
    con: duckdb.DuckDBPyConnection, selected: tuple[SelectedSnapshot, ...],
    *, batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[
    dict[str, int], Quarantine, dict[str, tuple[str, ...]], dict[str, tuple[str, ...]],
    FieldResolutions,
]:
    """Shape and load every table inside the caller's transaction."""

    quarantine = Quarantine()
    resolutions = FieldResolutions()
    counts: dict[str, int] = {}
    consumed: dict[str, tuple[str, ...]] = {}
    unconsumed: dict[str, tuple[str, ...]] = {}
    # activity_expenditure.expenditure_id and activity_nsap.nsap_id are
    # INTEGER surrogates with no source-field spelling (see
    # transform.activity_expenditure / transform.activity_nsap); each
    # counter is advanced by however many rows each snapshot contributes so
    # ids stay unique across every snapshot loaded into this one build.
    next_expenditure_id = 1
    next_nsap_id = 1
    # The LGD reference tree is static, so it is resolved once for the build
    # and shared by every snapshot's gram_panchayat contribution. Reading it
    # here rather than in transform keeps that module free of file system
    # access (see its docstring).
    geography = gp_geography()
    # Held back until every snapshot has contributed to gram_panchayat.
    # gp_profile has a FOREIGN KEY to it, so loading a profile snapshot
    # before the scrape snapshots that name the same GPs would quarantine
    # every row as an orphan and still finish green -- the order dependence
    # #161 is open about elsewhere in this function. Deferring is the whole
    # fix here: one row per GP, no per-snapshot state, nothing to interleave.
    profile_frames: list[tuple[str, str, pd.DataFrame]] = []
    # Deferred for exactly the reason profile_frames is: voucher has a
    # FOREIGN KEY to gram_panchayat, so loading an accounting snapshot ahead
    # of the scrape snapshots naming the same GPs would quarantine every
    # voucher as an orphan and still finish green (#161).
    voucher_frames: list[tuple[str, str, pd.DataFrame]] = []
    # Deferred for a further reason than voucher's: activity_voucher bridges
    # activity_expenditure to voucher, so it needs expenditure_id AND
    # voucher_pk to exist. Both are assigned after the loop, so the
    # expenditure source has to be held until they are.
    expenditure_frames: list[tuple[str, str, pd.DataFrame]] = []

    def add_count(table: str, n: int) -> None:
        counts[table] = counts.get(table, 0) + n

    # The code dictionaries are build-wide, not per-snapshot: they are the
    # same 717/17/12 rows whatever was scraped, so they are loaded once here
    # rather than inside the loop, where every snapshot after the first would
    # collide on dim_code's (variable, code) primary key.
    for table_name, frame in dimension_frames().items():
        add_count(table_name, insert(con, table_name, frame, batch_size=batch_size))

    for snapshot in selected:
        source_system, source_run_id = snapshot.spec.source, snapshot.spec.run_id
        tables = snapshot.tables
        root = snapshot.snapshot_root
        used: list[str] = []

        top_level = {}
        for kind in ("pl", "aa", "ta", "pp", "re"):
            frame = _read(root, tables, kind)
            top_level[kind] = frame
            # `re` is accounted for below: whether it is consumed depends on
            # what is in it, not on whether the snapshot declares it.
            if kind in tables and kind != "re":
                used.append(kind)
        pl, aa, ta, pp, re = (top_level[k] for k in ("pl", "aa", "ta", "pp", "re"))

        if "profile" in tables:
            profile_frames.append(
                (source_system, source_run_id, _read(root, tables, "profile"))
            )
            used.append("profile")

        if "voucher" in tables:
            voucher_frames.append(
                (source_system, source_run_id, _read(root, tables, "voucher"))
            )
            used.append("voucher")

        if "expenditure" in tables:
            expenditure_frames.append(
                (source_system, source_run_id, _read(root, tables, "expenditure"))
            )
            used.append("expenditure")

        # Every kind independently records its own GP via the same
        # folder-name parser, so the dimension is built from all of them,
        # not only from the planning payload.
        gp_frame = transform.gram_panchayat(
            [frame for frame in top_level.values() if not frame.empty], quarantine,
            source_system=source_system, source_run_id=source_run_id,
            geography=geography,
        )
        add_count("gram_panchayat", _merge_gram_panchayat(
            con, gp_frame, quarantine, source_system=source_system,
            source_run_id=source_run_id, batch_size=batch_size,
        ))

        if not pl.empty:
            plans = transform.plan(pl, quarantine, source_system=source_system, source_run_id=source_run_id)
            add_count("plan", insert(con, "plan", plans, batch_size=batch_size))

            activities = transform.planned_activity(
                pl, quarantine, source_system=source_system, source_run_id=source_run_id,
            )
            add_count("planned_activity", insert(con, "planned_activity", activities, batch_size=batch_size))
            activity_codes = set(activities["activity_code"].dropna())

            add_count("activity_delegation", insert(con, "activity_delegation", transform.activity_delegation(
                pl, activity_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
            ), batch_size=batch_size))
            add_count("activity_training", insert(con, "activity_training", transform.activity_training(
                pl, activity_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
            ), batch_size=batch_size))
            add_count("activity_community_service", insert(
                con, "activity_community_service", transform.activity_community_service(
                    pl, activity_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
                ), batch_size=batch_size))
            nsap_rows = transform.activity_nsap(
                pl, activity_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
                start_id=next_nsap_id,
            )
            add_count("activity_nsap", insert(con, "activity_nsap", nsap_rows, batch_size=batch_size))
            next_nsap_id += len(nsap_rows)

            asset_frames: list[pd.DataFrame] = []
            asset_children: list[str] = []
            fund_frames: list[pd.DataFrame] = []
            fund_children: list[str] = []
            # `pl` itself is an asset source, not only its child tables (#159).
            # `assetDetails` arrives as a MAPPING in the real payload, so
            # normalize folds it into this frame as `assetDetails_*` rather
            # than emitting a child table. Considered first so the ordering
            # matches the DDL comment's "one row per activity"; the signature
            # test below is what decides, not the position.
            #
            # Not added to `asset_children`: that list feeds `used`, which
            # tracks which canonical DATASETS were consumed, and `pl` is
            # already counted there. Adding it would double-count.
            if set(pl.columns) & set(transform.ASSET_CHILD_RENAMES):
                asset_frames.append(pl)
            for name in _child_tables(tables, "pl", ""):
                frame = _read(root, tables, name)
                if set(frame.columns) & set(transform.ASSET_CHILD_RENAMES):
                    asset_frames.append(frame)
                    asset_children.append(name)
                elif set(frame.columns) & set(transform.FUND_CHILD_RENAMES):
                    fund_frames.append(frame)
                    fund_children.append(name)

            asset_frame = pd.concat(asset_frames, ignore_index=True) if asset_frames else pd.DataFrame()
            add_count("activity_asset", insert(con, "activity_asset", transform.activity_asset(
                asset_frame, activity_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
            ), batch_size=batch_size))
            used.extend(asset_children)

            fund_frame = pd.concat(fund_frames, ignore_index=True) if fund_frames else pd.DataFrame()
            add_count("activity_fund", insert(con, "activity_fund", transform.activity_fund(
                fund_frame, activity_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
            ), batch_size=batch_size))
            used.extend(fund_children)
        else:
            activity_codes = set()

        gp_codes = set(con.execute("SELECT gp_lgd_code FROM gram_panchayat").fetchdf()["gp_lgd_code"])

        approvals = transform.admin_approval(
            aa, activity_codes, gp_codes, quarantine,
            source_system=source_system, source_run_id=source_run_id,
        )
        add_count("admin_approval", insert(con, "admin_approval", approvals, batch_size=batch_size))
        parent_row_ids = set(approvals["row_id"].dropna())

        # The scheme array's JSON key IS now verified -- it is
        # `admApprovalSchemeWebService`, the only child array key found in
        # 27,672 AA arrays across 250 random GPs (#163). Discovery is still by
        # prefix plus a field signature rather than by that literal, on
        # purpose: the survey proves what the portal emits today, not what it
        # will emit next year, and a signature match degrades to "found
        # nothing" where a hardcoded key would degrade to silently loading an
        # unrelated array. So candidates are found by prefix, and --
        # but a direct AA child is only kept if it actually carries a
        # recognized scheme field. Without this, ANY unrelated AA child array
        # (attachments, comments, ...) would match the empty keyword, get
        # loaded as all-null scheme rows, and be marked consumed instead of
        # reported unconsumed.
        scheme_frames: list[pd.DataFrame] = []
        scheme_children: list[str] = []
        for name in _child_tables(tables, "aa", ""):
            frame = _read(root, tables, name)
            if set(frame.columns) & set(transform.AA_SCHEME_RENAMES):
                scheme_frames.append(frame)
                scheme_children.append(name)
        scheme_frame = pd.concat(scheme_frames, ignore_index=True) if scheme_frames else pd.DataFrame()
        add_count("admin_approval_scheme", insert(con, "admin_approval_scheme", transform.admin_approval_scheme(
            scheme_frame, parent_row_ids, quarantine, source_system=source_system, source_run_id=source_run_id,
        ), batch_size=batch_size))
        used.extend(scheme_children)

        add_count("technical_approval", insert(con, "technical_approval", transform.technical_approval(
            ta, activity_codes, gp_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
        ), batch_size=batch_size))

        add_count("physical_progress", insert(con, "physical_progress", transform.physical_progress(
            pp, activity_codes, quarantine, source_system=source_system, source_run_id=source_run_id,
        ), batch_size=batch_size))

        # The scraped `re` kind is getLbAllocatedAmountData -- budgetary
        # allocation, not expenditure -- so it cannot fill this table, and
        # feeding it in only produces a RequiredFieldUnresolved for a field
        # the source never had. activity_expenditure's source is the separate
        # expenditure extract (#49), which has no loader yet; until it does,
        # this table stays empty the same way voucher and dim_code do.
        #
        # A frame that *does* carry expenditure spellings is still shaped and
        # still checked, so a renamed column in a real expenditure source
        # keeps failing loudly instead of silently loading nothing. Leaving
        # `re` out of `used` reports it as unconsumed rather than claiming a
        # kind was loaded that was not.
        expenditure_source = re if transform.is_expenditure_frame(re) else pd.DataFrame()
        if "re" in tables and (re.empty or not expenditure_source.empty):
            # An empty `re` dataset -- what normalization writes for a kind
            # that was requested but yielded nothing -- has no rows left
            # unloaded, so reporting it as unconsumed would be noise.
            used.append("re")
        expenditures = transform.activity_expenditure(
            expenditure_source, gp_codes, quarantine,
            source_system=source_system, source_run_id=source_run_id,
            resolutions=resolutions, start_id=next_expenditure_id,
        )
        add_count("activity_expenditure", insert(con, "activity_expenditure", expenditures, batch_size=batch_size))
        next_expenditure_id += len(expenditures)

        consumed[f"{source_system}/{source_run_id}"] = tuple(sorted(used))
        leftover = tuple(sorted(set(tables) - set(used) - {"quarantine"}))
        if leftover:
            unconsumed[f"{source_system}/{source_run_id}"] = leftover

    # After the loop, deliberately: see profile_frames above.
    all_gp_codes = set(con.execute("SELECT gp_lgd_code FROM gram_panchayat").fetchdf()["gp_lgd_code"])
    loaded_profile_keys: set[str] = set()
    for source_system, source_run_id, frame in profile_frames:
        profiles = transform.gp_profile(
            frame, all_gp_codes, quarantine,
            source_system=source_system, source_run_id=source_run_id,
            loaded_keys=loaded_profile_keys,
        )
        add_count("gp_profile", insert(con, "gp_profile", profiles, batch_size=batch_size))
        loaded_profile_keys.update(profiles["gp_lgd_code"])

    # voucher_pk advances across snapshots the same way expenditure_id and
    # nsap_id do inside the loop: the counter is the caller's, so two
    # accounting snapshots in one build cannot both claim id 1 and collide on
    # the INTEGER PRIMARY KEY.
    next_voucher_pk = 1
    loaded_voucher_keys: set[tuple[str, str, str]] = set()
    for source_system, source_run_id, frame in voucher_frames:
        vouchers = transform.voucher(
            frame, all_gp_codes, quarantine,
            source_system=source_system, source_run_id=source_run_id,
            start_id=next_voucher_pk, loaded_keys=loaded_voucher_keys,
        )
        add_count("voucher", insert(con, "voucher", vouchers, batch_size=batch_size))
        next_voucher_pk += len(vouchers)
        loaded_voucher_keys.update(zip(
            vouchers["gp_lgd_code"], vouchers["fiscal_year"], vouchers["voucher_no"],
            strict=True,
        ))

    # activity_expenditure and its bridge, after voucher so voucher_pk exists
    # to resolve against. The expenditure_id counter continues from the loop's
    # `re`-driven call above rather than restarting, so the two paths cannot
    # both claim id 1 -- the same rule voucher_pk follows across snapshots.
    for source_system, source_run_id, frame in expenditure_frames:
        expenditures = transform.activity_expenditure(
            frame, all_gp_codes, quarantine,
            source_system=source_system, source_run_id=source_run_id,
            resolutions=resolutions, start_id=next_expenditure_id,
        )
        add_count("activity_expenditure", insert(
            con, "activity_expenditure", expenditures, batch_size=batch_size,
        ))
        next_expenditure_id += len(expenditures)
        # Read back rather than held: voucher may span several snapshots, and
        # the bridge has to resolve against every voucher in the build, not
        # only the ones this snapshot happened to carry.
        voucher_keys = con.execute(
            "SELECT gp_lgd_code, fiscal_year, voucher_no, voucher_pk FROM voucher"
        ).fetchdf()
        add_count("activity_voucher", insert(con, "activity_voucher", transform.activity_voucher(
            frame, expenditures, voucher_keys, quarantine,
            source_system=source_system, source_run_id=source_run_id,
        ), batch_size=batch_size))

    add_count("quarantine", insert(con, "quarantine", quarantine.frame(), batch_size=batch_size))
    return counts, quarantine, consumed, unconsumed, resolutions


def build_into(
    path: Path, selected: tuple[SelectedSnapshot, ...], *, batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[
    dict[str, int], Quarantine, dict[str, tuple[str, ...]], dict[str, tuple[str, ...]],
    FieldResolutions,
]:
    """Create and populate a database at ``path``, in one transaction."""

    con = duckdb.connect(str(path))
    try:
        create_schema(con)
        con.execute("BEGIN TRANSACTION")
        try:
            result = populate(con, selected, batch_size=batch_size)
            # Inside the same transaction as the load: a warehouse that has
            # the facts but not the consumer relations is not consumable
            # (#51), so it must not be publishable either. Built after
            # populate because every one of them reads the tables it fills.
            for name, rows in materialize(con).items():
                result[0][name] = rows
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return result


def build(
    *,
    snapshot_ids: tuple[str, ...],
    target: Path | None = None,
    settings: WarehouseSettings | None = None,
    registry=None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    validate: bool = True,
) -> BuildResult:
    """Rebuild the warehouse, publishing it only if every check passes.

    ``target`` is replaced atomically: a failed or invalid build cannot leave
    the previous database missing or half-written, because every step before
    the final ``os.replace`` runs against a sibling temporary file.
    """

    settings = settings or load_settings()
    target = Path(target or settings.db_path)
    selected = resolve_snapshots(settings, snapshot_ids, registry=registry)

    target.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.stem}-build-"))
    staging = staging_dir / target.name
    try:
        counts, quarantine, consumed, unconsumed, resolutions = build_into(staging, selected, batch_size=batch_size)
        logger.info("Loaded %d table(s): %s", len(counts), ", ".join(f"{t}={n}" for t, n in counts.items()))
        if quarantine.total():
            logger.warning("Quarantined %d row(s); see the quarantine table", quarantine.total())
        if unconsumed:
            logger.warning("Declared but unconsumed canonical tables: %s", unconsumed)
        unresolved = resolutions.unresolved()
        if unresolved:
            logger.warning(
                "Optional field(s) resolved to null (no candidate column present): %s",
                ", ".join(f"{r.table}.{r.field}" for r in unresolved),
            )

        results: tuple[Check, ...] = ()
        if validate:
            con = duckdb.connect(str(staging), read_only=True)
            try:
                results = tuple(run_checks(con, counts, selected))
            finally:
                con.close()
            failures = [check for check in results if not check.passed]
            if failures:
                raise ValidationFailed(failures)

        os.replace(staging, target)
        logger.info("Published %s", target)
        return BuildResult(
            target, counts, quarantine.total(), results, consumed, unconsumed,
            tuple(resolutions.records),
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
