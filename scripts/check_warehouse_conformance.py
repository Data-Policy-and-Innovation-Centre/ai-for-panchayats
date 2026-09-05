#!/usr/bin/env python3
"""Check a built DuckDB warehouse against THE SPECIFICATION.

    uv run python scripts/check_warehouse_conformance.py path/to/warehouse.duckdb
    uv run python scripts/check_warehouse_conformance.py path/to/fixture.duckdb --skip-reconciliation

Exits 0 when no violation is found (informational notes do not fail the
run), non-zero when at least one violation is found. Opens the database
read-only: this tool never writes to the file it is checking.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Run directly (not via pytest, which sets pythonpath=["src", "."] itself),
# so `src` needs to be on sys.path explicitly -- same convention as
# scripts/build_warehouse.py.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

import duckdb  # noqa: E402

from warehouse.conformance import (  # noqa: E402
    EXEMPTABLE_RECONCILIATION_CHECKS,
    check_conformance,
    format_report,
    has_violations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("database", type=Path, help="path to the built .duckdb file")
    parser.add_argument(
        "--skip-reconciliation", action="store_true",
        help="omit the exact reference-build reconciliation totals (use for synthetic fixtures)",
    )
    parser.add_argument(
        "--exempt-reconciliation", action="append", default=[], metavar="CHECK",
        choices=sorted(EXEMPTABLE_RECONCILIATION_CHECKS),
        help="omit ONE named reconciliation target and check the rest. Prefer this to "
             "--skip-reconciliation on a real build: a total whose source is known to be "
             "incomplete is a reason to exempt that total, not to stop checking the "
             "others (#175). Repeatable. Each exemption is reported, not silent",
    )
    parser.add_argument(
        "--skip-geography", action="store_true",
        help="omit full-state geography coverage (use for synthetic fixtures). Separate "
             "from the reconciliation flags on purpose: folding the two together meant "
             "no real build ever checked its own scale, and a 20-GP build reported PASS",
    )
    parser.add_argument(
        "--skip-derived", action="store_true",
        help="omit the consumer relations v_activity/v_asset/... (#51), for synthetic "
             "fixtures. A real build should not skip this: the chatbot queries these "
             "and nothing else, so a warehouse without them is not consumable",
    )
    args = parser.parse_args(argv)

    if not args.database.exists():
        print(f"error: no such file: {args.database}", file=sys.stderr)
        return 2

    con = duckdb.connect(str(args.database), read_only=True)
    try:
        findings = check_conformance(
            con, skip_reconciliation=args.skip_reconciliation,
            skip_geography=args.skip_geography, skip_derived=args.skip_derived,
            exempt_reconciliation=frozenset(args.exempt_reconciliation),
        )
    finally:
        con.close()

    print(format_report(findings))
    return 1 if has_violations(findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
