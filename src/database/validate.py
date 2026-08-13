"""Validation gate for a built read model.

Checks are executable assertions, not printed tables a reader has to eyeball.
Expected counts come from a versioned manifest rather than numbers typed into a
notebook cell, so when the source data legitimately changes, the manifest is
what gets reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from . import config
from .schema import FACT_TABLES


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


class ValidationFailed(RuntimeError):
    def __init__(self, failures: list[Check]) -> None:
        lines = "\n".join(f"  - {c.name}: {c.detail}" for c in failures)
        super().__init__(f"{len(failures)} validation check(s) failed:\n{lines}")
        self.failures = failures


def load_manifest(path: Path | None = None) -> dict:
    path = Path(path or config.MANIFEST_PATH)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _scalar(con: duckdb.DuckDBPyConnection, sql: str):
    return con.execute(sql).fetchone()[0]


def check_primary_keys(con: duckdb.DuckDBPyConnection) -> list[Check]:
    """No table may hold a duplicate primary key."""
    keys = {
        "gram_panchayat": "gp_lgd_code", "plan": "plan_code",
        "planned_activity": "activity_code",
        "activity_delegation": "activity_code", "activity_asset": "activity_code",
        "activity_fund": "activity_code", "activity_training": "activity_code",
        "activity_community_service": "activity_code",
        "activity_expenditure": "expenditure_id", "voucher": "voucher_pk",
        "admin_approval": "row_id", "admin_approval_scheme": "row_id",
        "technical_approval": "row_id", "physical_progress": "row_id",
    }
    checks = []
    for table, key in keys.items():
        duplicates = _scalar(
            con, f"SELECT count(*) - count(DISTINCT {key}) FROM {table}")
        checks.append(Check(f"unique {table}.{key}", duplicates == 0,
                            f"{duplicates} duplicate key(s)"))
    return checks


def check_orphans(con: duckdb.DuckDBPyConnection) -> list[Check]:
    """Every declared relationship must actually hold.

    The foreign keys enforce this at insert time; re-asserting it here catches
    a schema edit that quietly drops a constraint.
    """
    relations = [
        ("planned_activity", "gp_lgd_code", "gram_panchayat", "gp_lgd_code"),
        ("activity_expenditure", "activity_code", "planned_activity", "activity_code"),
        ("activity_voucher", "expenditure_id", "activity_expenditure", "expenditure_id"),
        ("activity_voucher", "voucher_pk", "voucher", "voucher_pk"),
        ("admin_approval", "activity_code", "planned_activity", "activity_code"),
        ("admin_approval_scheme", "parent_row_id", "admin_approval", "row_id"),
        ("technical_approval", "activity_code", "planned_activity", "activity_code"),
        ("physical_progress", "activity_code", "planned_activity", "activity_code"),
    ]
    checks = []
    for child, column, parent, key in relations:
        orphans = _scalar(con, f"""
            SELECT count(*) FROM {child} c
            WHERE c.{column} IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{key} = c.{column})
        """)
        checks.append(Check(f"{child}.{column} -> {parent}.{key}", orphans == 0,
                            f"{orphans} orphan row(s)"))
    return checks


def check_counts(counts: dict[str, int], manifest: dict) -> list[Check]:
    """Loaded rows must match the reviewed manifest, where one is declared."""
    expected = (manifest or {}).get("expected_rows") or {}
    checks = []
    for table, want in expected.items():
        got = counts.get(table, 0)
        checks.append(Check(f"rows in {table}", got == want,
                            f"expected {want}, loaded {got}"))
    return checks


def check_bridge_match_rate(con: duckdb.DuckDBPyConnection,
                            manifest: dict) -> list[Check]:
    """A voucher named in the expenditure feed should resolve to a voucher row.

    Some gap is expected where the accounting extract does not cover a
    GP-year, so this is a floor rather than an equality.
    """
    total = _scalar(con, "SELECT count(*) FROM activity_voucher")
    if not total:
        return [Check("bridge match rate", True, "no bridge rows")]

    matched = _scalar(
        con, "SELECT count(*) FROM activity_voucher WHERE voucher_pk IS NOT NULL")
    rate = matched / total
    floor = (manifest or {}).get("thresholds", {}).get("bridge_match_rate", 0.0)
    return [Check("bridge match rate", rate >= floor,
                  f"{rate:.1%} matched, floor {floor:.1%}")]


def check_no_negative_money(con: duckdb.DuckDBPyConnection) -> list[Check]:
    columns = [
        ("planned_activity", "total_cost"),
        ("activity_expenditure", "total_expenditure"),
        ("voucher", "amount"),
    ]
    checks = []
    for table, column in columns:
        negatives = _scalar(
            con, f"SELECT count(*) FROM {table} WHERE {column} < 0")
        checks.append(Check(f"{table}.{column} not negative", negatives == 0,
                            f"{negatives} negative value(s)"))
    return checks


def check_quarantine_budget(con: duckdb.DuckDBPyConnection,
                            manifest: dict) -> list[Check]:
    """Quarantined rows are acceptable up to a reviewed ceiling, not unbounded."""
    quarantined = _scalar(
        con, "SELECT coalesce(sum(row_count), 0) FROM quarantine")
    ceiling = (manifest or {}).get("thresholds", {}).get("max_quarantined_rows")
    if ceiling is None:
        return [Check("quarantined rows", True, f"{quarantined} row(s), no ceiling set")]
    return [Check("quarantined rows", quarantined <= ceiling,
                  f"{quarantined} row(s), ceiling {ceiling}")]


def run_checks(con: duckdb.DuckDBPyConnection, counts: dict[str, int],
               manifest: dict | None = None) -> list[Check]:
    manifest = load_manifest() if manifest is None else manifest
    return [
        *check_primary_keys(con),
        *check_orphans(con),
        *check_counts(counts, manifest),
        *check_bridge_match_rate(con, manifest),
        *check_no_negative_money(con),
        *check_quarantine_budget(con, manifest),
    ]


def validate_database(path: Path | None = None) -> list[Check]:
    """Run every check against an existing database."""
    path = Path(path or config.DB_PATH)
    con = duckdb.connect(str(path), read_only=True)
    try:
        counts = {t: _scalar(con, f"SELECT count(*) FROM {t}")
                  for t in FACT_TABLES}
        return run_checks(con, counts)
    finally:
        con.close()
