#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(
    __file__
).resolve().parent


BUILDERS = [
    "build_planning_tables.py",
    "build_expenditure_tables.py",
    "build_voucher_tables.py",
    "build_admin_approval_tables.py",
    "build_technical_approval_table.py",
    "build_physical_progress_table.py",
    "build_dimension_tables.py",
]


def run_builder(
    script_name: str,
    *,
    planning_chunk_size: int,
) -> int:

    script_path = (
        SCRIPT_DIR
        / script_name
    )

    command = [
        sys.executable,
        str(script_path),
    ]

    if script_name == "build_planning_tables.py":

        command.extend(
            [
                "--chunk-size",
                str(
                    planning_chunk_size
                ),
            ]
        )

    print(
        "\n"
        + "=" * 70
    )

    print(
        f"Running: {script_name}"
    )

    print(
        "=" * 70
        + "\n"
    )

    result = subprocess.run(
        command,
        env=os.environ.copy(),
    )

    return result.returncode


def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Build all 19 schema tables "
            "using source-specific builders."
        )
    )

    parser.add_argument(
        "--planning-chunk-size",
        type=int,
        default=25_000,
        help=(
            "Chunk size for planning CSV. "
            "Default: 25000."
        ),
    )

    args = parser.parse_args()

    if args.planning_chunk_size <= 0:

        parser.error(
            "--planning-chunk-size must be greater than zero"
        )

    for script_name in BUILDERS:

        return_code = run_builder(
            script_name,
            planning_chunk_size=
            args.planning_chunk_size,
        )

        if return_code != 0:

            print(
                f"\nFAILED: {script_name}"
            )

            print(
                "Stopping build."
            )

            return return_code

        print(
            f"\nCOMPLETED: {script_name}"
        )

    print(
        "\n"
        + "=" * 70
    )

    print(
        "ALL SCHEMA TABLES COMPLETED SUCCESSFULLY"
    )

    print(
        "=" * 70
    )

    return 0


if __name__ == "__main__":

    raise SystemExit(
        main()
    )