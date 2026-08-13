#!/usr/bin/env python3
"""Build or validate the panchayat DuckDB read model.

    uv run python scripts/build_panchayat_db.py build
    uv run python scripts/build_panchayat_db.py build --no-validate
    uv run python scripts/build_panchayat_db.py validate
    uv run python scripts/build_panchayat_db.py counts

The build is atomic: it runs into a temporary file and replaces the target only
after every table has loaded and validation has passed. A failed run leaves the
previous database exactly as it was.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from database import config
from database.build import build, table_counts
from database.validate import ValidationFailed, validate_database

logger = logging.getLogger("build_panchayat_db")


def cmd_build(args: argparse.Namespace) -> int:
    try:
        counts = build(target=args.database, validate=not args.no_validate)
    except config.MissingInput as exc:
        logger.error("%s", exc)
        return 2
    except ValidationFailed as exc:
        logger.error("%s", exc)
        logger.error("The previous database was left untouched.")
        return 1

    width = max(len(t) for t in counts) if counts else 0
    for table, count in counts.items():
        print(f"  {table:<{width}}  {count:>8,}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.database or config.DB_PATH)
    if not path.exists():
        logger.error("No database at %s. Run `build` first.", path)
        return 2

    checks = validate_database(path)
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"  {'PASS' if check.passed else 'FAIL'}  {check.name:<{width}}  {check.detail}")

    failed = [c for c in checks if not c.passed]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} check(s) passed")
    return 1 if failed else 0


def cmd_counts(args: argparse.Namespace) -> int:
    path = Path(args.database or config.DB_PATH)
    if not path.exists():
        logger.error("No database at %s. Run `build` first.", path)
        return 2
    counts = table_counts(path)
    width = max(len(t) for t in counts)
    for table, count in counts.items():
        print(f"  {table:<{width}}  {count:>8,}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", type=Path, default=None,
                        help=f"database file (default: {config.DB_PATH})")
    parser.add_argument("--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    build_parser = sub.add_parser("build", help="rebuild and publish")
    build_parser.add_argument("--no-validate", action="store_true",
                              help="publish without running the checks")
    build_parser.set_defaults(func=cmd_build)

    sub.add_parser("validate", help="run the checks against an existing build"
                   ).set_defaults(func=cmd_validate)
    sub.add_parser("counts", help="row count per table"
                   ).set_defaults(func=cmd_counts)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
