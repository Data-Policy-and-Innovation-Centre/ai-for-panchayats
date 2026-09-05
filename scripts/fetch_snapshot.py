"""Container startup step: obtain the pinned snapshot or fail the task.

Exits non-zero on any missing, truncated, substituted or mismatched artifact,
so an ECS task that cannot prove its database never reports healthy.

    uv run python -m scripts.fetch_snapshot \
        infra/snapshots/full_state.json /var/task-db/database.duckdb
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Run directly (not via pytest, which sets pythonpath=["src", "."] itself, and
# not only via `python -m`), so the repository root needs to be on sys.path
# explicitly -- same convention as scripts/build_warehouse.py. `src.deploy`
# imports `src.warehouse`, so the repo root (not `src/`) must be the path
# added; otherwise the nested import fails with ModuleNotFoundError.
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.deploy.errors import SnapshotError  # noqa: E402
from src.deploy.fetch import fetch_snapshot, load_expectations  # noqa: E402
from src.deploy.identity import write_identity  # noqa: E402
from src.deploy.manifest import load_manifest  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, help="committed snapshot manifest JSON")
    parser.add_argument("destination", type=Path, help="task-local path to publish to")
    parser.add_argument("--region", default=None, help="AWS region; defaults to the environment")
    parser.add_argument(
        "--identity-out",
        type=Path,
        default=None,
        help=(
            "write the verified snapshot identity here as JSON, so the serving "
            "process can report what it is running (#85)"
        ),
    )
    parser.add_argument(
        "--skip-expectations",
        action="store_true",
        help="skip the private aggregate checks (benchmarking only, never production)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    import boto3

    client = boto3.client("s3", region_name=args.region)
    try:
        manifest = load_manifest(args.manifest)
        if manifest.expectations_key is None and not args.skip_expectations:
            print(
                f"{args.manifest} pins no expectations_key, so the aggregate gate would "
                "not run. Rebuild the manifest with --expectations-key, or pass "
                "--skip-expectations to accept an unverified publish.",
                file=sys.stderr,
            )
            return 1
        expectations = None if args.skip_expectations else load_expectations(client, manifest)
        identity = fetch_snapshot(
            manifest,
            args.destination,
            s3_client=client,
            expectations=expectations,
            allow_missing_expectations=args.skip_expectations,
        )
    except SnapshotError as exc:
        print(f"snapshot verification failed: {exc}", file=sys.stderr)
        return 1

    # After the fetch, never before: the file's existence has to mean the
    # database was proven, not that a fetch was attempted. A failure above
    # returns 1 without reaching here, and a stale file from a previous task
    # cannot survive because task-local storage starts empty.
    if args.identity_out is not None:
        try:
            write_identity(identity, args.identity_out)
        except OSError as exc:
            print(f"could not write {args.identity_out}: {exc}", file=sys.stderr)
            return 1

    print(identity.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
