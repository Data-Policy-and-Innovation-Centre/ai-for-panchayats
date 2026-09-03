#!/usr/bin/env python3
"""Build or validate the panchayat DuckDB warehouse from canonical Parquet.

    uv run python scripts/build_warehouse.py build --snapshot-id egramswaraj-2026-08
    uv run python scripts/build_warehouse.py build --snapshot-id a --snapshot-id b
    uv run python scripts/build_warehouse.py build --no-validate --snapshot-id a

The build is atomic: it runs into a temporary file next to the target and
replaces the target only after every table has loaded and every check has
passed. A failed run leaves the previous database exactly as it was.

Every ``--snapshot-id`` must already be marked ``approved`` in
``config/snapshots.yaml``; there is no "build everything on disk" mode.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Run directly (not via pytest, which sets pythonpath=["src", "."] itself),
# so the repository root needs to be on sys.path explicitly -- same
# convention as scripts/run_egram_scraper.py. `src.warehouse` itself imports
# `src.pipeline`, so the repo root (not `src/`) must be the one path added;
# otherwise `import src.pipeline` fails with ModuleNotFoundError.
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.warehouse.build import build  # noqa: E402
from src.warehouse.geography import GeographyError  # noqa: E402
from src.warehouse.select import SelectionError  # noqa: E402
from src.warehouse.validate import ValidationFailed  # noqa: E402

logger = logging.getLogger("build_warehouse")


def cmd_build(args: argparse.Namespace) -> int:
    try:
        result = build(
            snapshot_ids=tuple(args.snapshot_id),
            target=args.database,
            validate=not args.no_validate,
        )
    except (SelectionError, GeographyError) as exc:
        # Both are "this build cannot start" conditions raised before any
        # DuckDB file is touched: a bad snapshot selection, or an LGD
        # reference tree that is missing or malformed. Same controlled exit
        # rather than a traceback.
        logger.error("%s", exc)
        return 2
    except ValidationFailed as exc:
        logger.error("%s", exc)
        logger.error("The previous database was left untouched.")
        return 1

    width = max((len(t) for t in result.counts), default=0)
    for table, count in result.counts.items():
        print(f"  {table:<{width}}  {count:>8,}")
    if result.quarantine_count:
        print(f"\n{result.quarantine_count} row(s) quarantined; see the quarantine table.")
    if result.unconsumed_tables:
        print("\nDeclared but unconsumed canonical tables (not loaded, not lost):")
        for run, tables in result.unconsumed_tables.items():
            print(f"  {run}: {tables}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--database", type=Path, default=None,
                        help="database file (default: settings.db_path)")
    parser.add_argument("--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="rebuild and publish")
    build_parser.add_argument(
        "--snapshot-id", action="append", default=[], required=True,
        help="an approved snapshot id from config/snapshots.yaml (repeatable)",
    )
    build_parser.add_argument("--no-validate", action="store_true",
                              help="publish without running the post-load checks")
    build_parser.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
