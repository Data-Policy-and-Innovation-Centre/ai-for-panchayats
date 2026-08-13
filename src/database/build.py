"""Build the panchayat read model.

The database is derived and disposable: it is always rebuilt from source, never
edited in place. The build runs into a temporary file and only replaces the
target once every table has loaded and validation has passed, so a failed run
leaves the last known-good database untouched rather than a half-populated one.
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

from . import config, transform
from .schema import CREATE_ORDER, DDL, FACT_TABLES, RESET_ORDER
from .transform import Quarantine

logger = logging.getLogger(__name__)


@dataclass
class Sources:
    """Every frame a build reads. Passing these in keeps build() testable."""

    planning: pd.DataFrame
    expenditure: pd.DataFrame
    vouchers: pd.DataFrame
    admin_approval: pd.DataFrame | None = None
    admin_approval_scheme: pd.DataFrame | None = None
    technical_approval: pd.DataFrame | None = None
    physical_progress: pd.DataFrame | None = None
    code_descriptions: pd.DataFrame | None = None
    welfare_schemes: pd.DataFrame | None = None
    lsdg_themes: pd.DataFrame | None = None


def load_sources() -> Sources:
    """Read every configured input, failing with the variable that moves it."""
    def read_csv(path: Path, env_var: str) -> pd.DataFrame:
        return pd.read_csv(config.require(path, env_var), low_memory=False)

    def read_optional(path: Path) -> pd.DataFrame | None:
        if not path.exists():
            logger.warning("Optional input absent, skipping: %s", path)
            return None
        return pd.read_csv(path, low_memory=False)

    sources = Sources(
        planning=read_csv(config.PLANNING_CSV, "PANCHAYAT_PLANNING_CSV"),
        expenditure=read_csv(config.EXPENDITURE_CSV, "PANCHAYAT_EXPENDITURE_CSV"),
        vouchers=read_csv(config.VOUCHERS_CSV, "PANCHAYAT_VOUCHERS_CSV"),
        admin_approval=read_optional(config.ADMIN_APPROVAL_CSV),
        admin_approval_scheme=read_optional(config.ADMIN_APPROVAL_SCHEME_CSV),
        technical_approval=read_optional(config.TECHNICAL_APPROVAL_CSV),
        physical_progress=read_optional(config.PHYSICAL_PROGRESS_CSV),
    )

    if config.CODE_LOOKUP_XLSX.exists():
        # Reading .xlsx needs openpyxl; xlsxwriter only writes.
        workbook = pd.ExcelFile(config.CODE_LOOKUP_XLSX)
        sources.code_descriptions = workbook.parse("Code Descriptions")
        sources.welfare_schemes = workbook.parse("Welfare Scheme Master")
        sources.lsdg_themes = workbook.parse("FocusArea to LSDG Theme")
    else:
        logger.warning("Code lookup workbook absent: %s", config.CODE_LOOKUP_XLSX)

    return sources


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Drop children before parents, then create parents before children."""
    for table in RESET_ORDER:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    for table in CREATE_ORDER:
        con.execute(DDL[table])


def _insert(con: duckdb.DuckDBPyConnection, table: str,
            frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    columns = [row[1] for row in con.execute(
        f"PRAGMA table_info('{table}')").fetchall()]
    payload = frame.reindex(columns=columns)     # noqa: F841 — used by DuckDB
    con.register("payload", payload)
    con.execute(f"INSERT INTO {table} SELECT * FROM payload")
    con.unregister("payload")
    return len(payload)


def populate(con: duckdb.DuckDBPyConnection,
             sources: Sources) -> tuple[dict[str, int], Quarantine]:
    """Shape and load every table inside the caller's transaction."""
    quarantine = Quarantine()
    counts: dict[str, int] = {}

    planning = transform.clean_planning(sources.planning)
    expenditure = transform.clean_expenditure(sources.expenditure)
    vouchers = transform.clean_vouchers(sources.vouchers)

    gps = transform.gram_panchayat(vouchers, expenditure, quarantine)
    counts["gram_panchayat"] = _insert(con, "gram_panchayat", gps)
    gp_codes = set(gps["gp_lgd_code"].dropna())

    plans = transform.plan(expenditure, quarantine)
    counts["plan"] = _insert(con, "plan", plans)

    activities = transform.planned_activity(planning, quarantine)
    counts["planned_activity"] = _insert(con, "planned_activity", activities)
    activity_codes = set(activities["activity_code"].dropna())

    for table in transform.SATELLITES:
        counts[table] = _insert(con, table,
                                transform.satellite(planning, table, quarantine))

    counts["activity_nsap"] = _insert(con, "activity_nsap",
                                      transform.activity_nsap(planning))

    spend = transform.activity_expenditure(expenditure, activity_codes, quarantine)
    counts["activity_expenditure"] = _insert(con, "activity_expenditure", spend)

    vouch = transform.voucher(vouchers, gp_codes, quarantine)
    counts["voucher"] = _insert(con, "voucher", vouch)

    bridge = transform.activity_voucher(expenditure, spend, vouch)
    counts["activity_voucher"] = _insert(con, "activity_voucher", bridge)

    # ---- extensions ----
    approvals = pd.DataFrame()
    if sources.admin_approval is not None:
        approvals = transform.admin_approval(
            sources.admin_approval, activity_codes, gp_codes, quarantine)
        counts["admin_approval"] = _insert(con, "admin_approval", approvals)

    if sources.admin_approval_scheme is not None:
        parents = set(approvals["row_id"]) if not approvals.empty else set()
        counts["admin_approval_scheme"] = _insert(
            con, "admin_approval_scheme",
            transform.admin_approval_scheme(sources.admin_approval_scheme,
                                            parents, quarantine))

    if sources.technical_approval is not None:
        counts["technical_approval"] = _insert(
            con, "technical_approval",
            transform.technical_approval(sources.technical_approval,
                                         activity_codes, gp_codes, quarantine))

    if sources.physical_progress is not None:
        counts["physical_progress"] = _insert(
            con, "physical_progress",
            transform.physical_progress(sources.physical_progress,
                                        activity_codes, quarantine))

    # ---- lookups ----
    if sources.code_descriptions is not None:
        counts["dim_code"] = _insert(
            con, "dim_code",
            transform.dim_code(sources.code_descriptions, quarantine))
    if sources.welfare_schemes is not None:
        counts["dim_welfare_scheme"] = _insert(
            con, "dim_welfare_scheme",
            transform.dim_welfare_scheme(sources.welfare_schemes, quarantine))
    if sources.lsdg_themes is not None:
        counts["dim_lsdg_theme"] = _insert(
            con, "dim_lsdg_theme",
            transform.dim_lsdg_theme(sources.lsdg_themes))

    _insert(con, "quarantine", quarantine.frame())
    return counts, quarantine


def build_into(path: Path, sources: Sources) -> tuple[dict[str, int], Quarantine]:
    """Create and populate a database at path, in one transaction."""
    con = duckdb.connect(str(path))
    try:
        create_schema(con)
        con.execute("BEGIN TRANSACTION")
        try:
            counts, quarantine = populate(con, sources)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return counts, quarantine


def build(target: Path | None = None, sources: Sources | None = None,
          validate: bool = True) -> dict[str, int]:
    """Rebuild the read model, publishing it only if it validates.

    The target is replaced atomically, so a failed or invalid build cannot
    leave the previous database missing or half-written.
    """
    from .validate import ValidationFailed, run_checks   # avoids a cycle

    target = Path(target or config.DB_PATH)
    sources = sources or load_sources()
    target.parent.mkdir(parents=True, exist_ok=True)

    staging_dir = Path(tempfile.mkdtemp(dir=target.parent,
                                        prefix=f".{target.stem}-build-"))
    staging = staging_dir / target.name
    try:
        counts, quarantine = build_into(staging, sources)
        logger.info("Loaded %d table(s): %s", len(counts),
                    ", ".join(f"{t}={n}" for t, n in counts.items()))
        if quarantine.total():
            logger.warning("Quarantined %d row(s); see the quarantine table",
                           quarantine.total())

        if validate:
            con = duckdb.connect(str(staging), read_only=True)
            try:
                results = run_checks(con, counts)
            finally:
                con.close()
            failures = [r for r in results if not r.passed]
            if failures:
                raise ValidationFailed(failures)

        os.replace(staging, target)
        logger.info("Published %s", target)
        return counts
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def table_counts(path: Path | None = None) -> dict[str, int]:
    """Row count per table in an existing database."""
    con = duckdb.connect(str(path or config.DB_PATH), read_only=True)
    try:
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in FACT_TABLES}
    finally:
        con.close()
