#!/usr/bin/env python3
"""Measure where the deployed chatbot's database time actually goes.

    uv run python scripts/profile_warehouse_queries.py DB --views create_views.sql
    uv run python scripts/profile_warehouse_queries.py DB --views v.sql --explain v_activity

#99 reports that complex questions time out on the full database, and #98 that
an *unanswerable* question took seconds against the 20-GP demo and minutes
against the full state -- which the reporter reasonably expected to be
independent of database size.

`scripts/benchmark_deployment.py` cannot answer either. It measures end-to-end
HTTP wall clock from outside AWS, so it cannot attribute time to a layer, and
its docstring still asserts the workload is "network-bound, not IO-bound"
(#72) -- a conclusion reached before the full-state snapshot existed.

WHY THIS REPRODUCES THE CONSUMER RATHER THAN JUST OPENING THE FILE
    The five views the chatbot queries are NOT in the shipped .duckdb. The
    consumer's `DuckDBFileAdapter` connects to `:memory:`, attaches the file
    READ_ONLY, and creates the views in the writable in-memory catalog, where
    their unqualified base-table references resolve through `search_path` into
    the attached file. They are plain `CREATE VIEW`, so nothing is saved: every
    reference re-executes the whole join graph.

    Building them as tables here instead would measure a system nobody runs.
    So this mirrors the adapter exactly, defaults included -- notably
    `memory_limit = 512MB` against a 1.01 GB database, and one connection.
    `--threads 1` matches the 1 vCPU Fargate task.

Prints wall-clock per probe. Nothing here is a data extract: only row counts
and timings, so a report is safe to attach to a public issue.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import duckdb

DATA_ALIAS = "analytics"

# Verbatim from the consumer's query_router/entity_validator.py `_DB_SOURCES`
# and `_RANKED_SOURCES` (Odisha_PRDW @ master). These run at startup to build
# the entity vocabulary the router matches user text against. Four of them read
# a view, so each one materialises the full join graph.
#
# Re-check these against the consumer if the router changes; they are copied,
# not imported, because that repo is not a dependency of this one.
ENTITY_PROBES: tuple[tuple[str, str], ...] = (
    ("entity.district", "SELECT DISTINCT zp_name FROM gram_panchayat WHERE zp_name IS NOT NULL"),
    ("entity.block", "SELECT DISTINCT block_name FROM gram_panchayat WHERE block_name IS NOT NULL"),
    ("entity.gp", "SELECT DISTINCT gp_name FROM gram_panchayat WHERE gp_name IS NOT NULL"),
    ("entity.fiscal_year",
     "SELECT DISTINCT fiscal_year FROM planned_activity WHERE fiscal_year IS NOT NULL"),
    ("entity.scheme",
     "SELECT DISTINCT scheme_name FROM activity_expenditure WHERE scheme_name IS NOT NULL"),
    ("entity.status [view]",
     "SELECT DISTINCT status_label FROM v_activity WHERE status_label IS NOT NULL"),
    ("entity.asset_category [view]",
     "SELECT DISTINCT asset_category_label FROM v_asset WHERE asset_category_label IS NOT NULL"),
    ("entity.asset_subcategory [view]",
     "SELECT DISTINCT asset_subcategory_label FROM v_asset "
     "WHERE asset_subcategory_label IS NOT NULL"),
    ("entity.focus_area_ranked [view]",
     "SELECT focus_area_name FROM v_activity WHERE focus_area_name IS NOT NULL "
     "GROUP BY focus_area_name ORDER BY COUNT(*) DESC"),
    ("roster.gp",
     "SELECT gp_lgd_code, gp_name, block_name, zp_name FROM gram_panchayat "
     "WHERE gp_lgd_code IS NOT NULL"),
)

# The view chain itself, and the same questions asked of base tables, so the
# cost of the views is separable from the cost of the data.
VIEW_PROBES: tuple[tuple[str, str], ...] = (
    ("view.v_activity count", "SELECT count(*) FROM v_activity"),
    ("view.v_asset count", "SELECT count(*) FROM v_asset"),
    ("view.v_progress count", "SELECT count(*) FROM v_progress"),
    ("view.v_plan count", "SELECT count(*) FROM v_plan"),
    ("base.planned_activity count", "SELECT count(*) FROM planned_activity"),
    ("base.activity_asset count", "SELECT count(*) FROM activity_asset"),
    # A representative catalogue shape: group by geography, which is what most
    # of the template catalogue does.
    ("query.spend by district [view]",
     "SELECT district_name, SUM(total_expenditure) FROM v_activity GROUP BY district_name"),
    ("query.spend by district [base]",
     "SELECT g.zp_name, SUM(e.total_expenditure) FROM planned_activity a "
     "JOIN gram_panchayat g ON g.gp_lgd_code = a.gp_lgd_code "
     "LEFT JOIN activity_expenditure e ON e.activity_code = a.activity_code "
     "GROUP BY g.zp_name"),
)


def _attach(con, path: Path, alias: str) -> None:
    literal = path.resolve().as_posix().replace("'", "''")
    con.execute(f"ATTACH '{literal}' AS {alias} (READ_ONLY)")


def connect(db_path: Path, views_sql: str | None, *, memory_limit: str,
            threads: int | None, materialized: Path | None = None):
    """Mirror the consumer's DuckDBFileAdapter, including its defaults.

    ``materialized`` attaches a second database holding the v_* relations as
    real tables and puts it ahead of the snapshot on the search path, so the
    same probes resolve to stored rows instead of re-running the join graph.
    That is what #51 would ship, and it is the only way to compare the two on
    identical hardware and identical data.
    """

    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit = '{memory_limit}'")
    if threads is not None:
        con.execute(f"SET threads = {threads}")
    _attach(con, db_path, DATA_ALIAS)
    if materialized is not None:
        _attach(con, materialized, "built")
        # `built` first: a stored v_activity shadows the view of the same name.
        con.execute(f"SET search_path = 'built.main,memory.main,{DATA_ALIAS}.main'")
        return con
    con.execute(f"SET search_path = 'memory.main,{DATA_ALIAS}.main'")
    if views_sql:
        # One multi-statement script, as ensure_views does: splitting a
        # 270-line view script on ';' is not safe.
        con.execute(views_sql)
    return con


def time_probe(con, sql: str, repeat: int) -> tuple[float | None, int | str]:
    timings: list[float] = []
    rows: int | str = 0
    for _ in range(repeat):
        started = time.perf_counter()
        try:
            result = con.execute(sql).fetchall()
        except Exception as exc:  # noqa: BLE001 - a probe that fails is a finding
            return None, type(exc).__name__
        timings.append(time.perf_counter() - started)
        rows = len(result)
    return statistics.median(timings), rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--views", type=Path, default=None,
                        help="the consumer's sql/create_views.sql; without it, only "
                             "base-table probes run")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--memory-limit", default="512MB",
                        help="the adapter's default; the deployed task has 4 GB total")
    parser.add_argument("--threads", type=int, default=None,
                        help="1 matches the deployed 1 vCPU Fargate task")
    parser.add_argument("--explain", default=None, metavar="SUBSTRING",
                        help="also print EXPLAIN ANALYZE for probes whose name matches")
    parser.add_argument("--materialized", type=Path, default=None,
                        help="a database holding the v_* relations as tables; probes then "
                             "read stored rows instead of re-running the join graph (#51)")
    parser.add_argument("--label", default=None, help="name for this database in the report")
    args = parser.parse_args(argv)

    if not args.database.exists():
        print(f"error: no database at {args.database}", file=sys.stderr)
        return 2

    views_sql = args.views.read_text(encoding="utf-8") if args.views else None
    size_mb = args.database.stat().st_size / 1024 / 1024
    label = args.label or args.database.name

    started = time.perf_counter()
    con = connect(args.database, views_sql, memory_limit=args.memory_limit,
                  threads=args.threads, materialized=args.materialized)
    setup = time.perf_counter() - started

    gp_count = con.execute("SELECT count(*) FROM gram_panchayat").fetchone()[0]
    print(f"\n{label}  --  {size_mb:,.0f} MB, {gp_count:,} GPs")
    mode = "materialized tables" if args.materialized else (
        "views (as deployed)" if views_sql else "base tables only")
    print(f"memory_limit={args.memory_limit} threads={args.threads or 'default'} "
          f"mode={mode} repeat={args.repeat}")
    print(f"attach + create views: {setup * 1000:,.0f} ms\n")

    probes = list(ENTITY_PROBES) + list(VIEW_PROBES)
    if not views_sql and args.materialized is None:
        probes = [p for p in probes if "[view]" not in p[0] and not p[0].startswith("view.")]

    width = max(len(name) for name, _ in probes)
    print(f"{'probe':<{width}}  {'median':>12}  {'rows':>10}")
    print("-" * (width + 26))
    total = 0.0
    for name, sql in probes:
        elapsed, rows = time_probe(con, sql, args.repeat)
        if elapsed is None:
            print(f"{name:<{width}}  {'FAILED':>12}  {rows:>10}")
            continue
        total += elapsed
        print(f"{name:<{width}}  {elapsed * 1000:>9,.0f} ms  {rows:>10,}")
    print("-" * (width + 26))
    print(f"{'total':<{width}}  {total * 1000:>9,.0f} ms")

    if args.explain:
        for name, sql in probes:
            if args.explain.lower() in name.lower():
                print(f"\n===== EXPLAIN ANALYZE: {name} =====")
                print(con.execute(f"EXPLAIN ANALYZE {sql}").fetchall()[0][1])
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
