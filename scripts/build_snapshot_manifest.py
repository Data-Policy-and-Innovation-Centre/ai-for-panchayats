"""Pin a local DuckDB artifact as a deployable snapshot manifest.

Reads the artifact once to derive its SHA-256, byte size and relation
inventory, then writes the structural manifest that gets committed. Aggregate
expectations are never written here; they belong in the private S3 object the
manifest points at by key.

    uv run python -m scripts.build_snapshot_manifest \
        "$ARTIFACT" --bucket prdw-snapshots \
        --key duckdb/database_allgps.duckdb --version-id "$VERSION" \
        --out infra/snapshots/full_state.json

Pass --version-id placeholder before uploading, then re-run with the real
object version S3 returns.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.deploy.errors import SnapshotError
from src.deploy.manifest import PROVISIONAL_LABEL, build_manifest

DEFAULT_EXCEPTIONS = (
    "expenditure/activity_voucher/plan lineage not independently reproduced (#43, #49)",
    "full-state geography is blank for all gram_panchayat rows (#61)",
    "full-state reconciliation baseline not established (#62)",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifact", type=Path, help="path to the .duckdb file")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--version-id", required=True, help="S3 object version this pins")
    parser.add_argument("--label", default=PROVISIONAL_LABEL)
    parser.add_argument("--expectations-key", default=None, help="S3 key of the private aggregates")
    parser.add_argument(
        "--expectations-version-id",
        default=None,
        help="S3 object version of the aggregates; required with --expectations-key",
    )
    parser.add_argument(
        "--known-exception",
        action="append",
        dest="known_exceptions",
        help="repeatable; defaults to the open #43/#49, #61 and #62 exceptions",
    )
    parser.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = build_manifest(
            args.artifact,
            bucket=args.bucket,
            key=args.key,
            version_id=args.version_id,
            label=args.label,
            expectations_key=args.expectations_key,
            expectations_version_id=args.expectations_version_id,
            known_exceptions=tuple(args.known_exceptions or DEFAULT_EXCEPTIONS),
        )
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out is None:
        print(manifest.to_json(), end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(manifest.to_json(), encoding="utf-8")
        print(f"wrote {args.out}: {manifest.identity}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
