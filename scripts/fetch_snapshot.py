"""Container startup step: obtain the pinned snapshot or fail the task.

Exits non-zero on any missing, truncated, substituted or mismatched artifact,
so an ECS task that cannot prove its database never reports healthy.

    uv run python -m scripts.fetch_snapshot \
        infra/snapshots/full_state.json /var/task-db/database.duckdb
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.deploy.errors import SnapshotError
from src.deploy.fetch import fetch_snapshot, load_expectations
from src.deploy.manifest import load_manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, help="committed snapshot manifest JSON")
    parser.add_argument("destination", type=Path, help="task-local path to publish to")
    parser.add_argument("--region", default=None, help="AWS region; defaults to the environment")
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
        expectations = None if args.skip_expectations else load_expectations(client, manifest)
        identity = fetch_snapshot(
            manifest, args.destination, s3_client=client, expectations=expectations
        )
    except SnapshotError as exc:
        print(f"snapshot verification failed: {exc}", file=sys.stderr)
        return 1

    print(identity.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
