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

from warehouse.conformance import check_conformance, format_report, has_violations  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("database", type=Path, help="path to the built .duckdb file")
    parser.add_argument(
        "--skip-reconciliation", action="store_true",
        help="omit the exact reference-build reconciliation totals (use for synthetic fixtures)",
    )
    args = parser.parse_args(argv)

    if not args.database.exists():
        print(f"error: no such file: {args.database}", file=sys.stderr)
        return 2

    con = duckdb.connect(str(args.database), read_only=True)
    try:
        findings = check_conformance(con, skip_reconciliation=args.skip_reconciliation)
    finally:
        con.close()

    print(format_report(findings))
    return 1 if has_violations(findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
