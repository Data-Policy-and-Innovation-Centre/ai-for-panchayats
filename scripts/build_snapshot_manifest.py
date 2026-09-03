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
from src.deploy.manifest import ATTACH_CATALOG, PROVISIONAL_LABEL, attach_read_only, build_manifest
from src.warehouse.conformance import MIN_GP_COVERAGE
from src.warehouse.geography import GeographyError, gp_geography

# #61 is no longer here: gram_panchayat now carries state/district/block for
# every GP in the LGD reference tree, and assert_full_state below refuses to
# pin an artifact that is not the whole roster. Pinning the older externally
# built artifact, which does have blank geography, means passing
# --known-exception for it explicitly -- which is the right way round.
DEFAULT_EXCEPTIONS = (
    "expenditure/activity_voucher/plan lineage not independently reproduced (#43, #49)",
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
    exceptions = parser.add_mutually_exclusive_group()
    exceptions.add_argument(
        "--known-exception",
        action="append",
        dest="known_exceptions",
        help="repeatable; defaults to the open #43/#49 and #62 exceptions",
    )
    exceptions.add_argument(
        "--no-known-exceptions",
        action="store_true",
        help="record no caveats, for an artifact whose exceptions are all resolved",
    )
    parser.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    return parser.parse_args(argv)


def _known_exceptions(args: argparse.Namespace) -> tuple[str, ...]:
    """An empty tuple has to be reachable, or a corrected artifact keeps its caveats."""
    if args.no_known_exceptions:
        return ()
    if args.known_exceptions is None:
        return DEFAULT_EXCEPTIONS
    return tuple(args.known_exceptions)


def assert_full_state(artifact: Path) -> int:
    """Refuse to pin an artifact that is not the whole state.

    This is the only place a database becomes deployable: the deployment
    reads ``infra/snapshots/full_state.json``, and this script is what writes
    it. So this is where a sample has to be stopped.

    It has to be stopped somewhere, because a 20-GP smoke build is otherwise
    indistinguishable from a full one -- same 19 tables, same schema, same
    green conformance run -- and the result would be a chatbot answering
    statewide questions from 0.3% of the data. The prod/staging env files
    make the right thing easy; they are a convenience, and a convenience can
    be skipped. This cannot.

    The bound is coverage, not equality. ``gram_panchayat`` is built from GPs
    *observed* in the scrape, and a GP whose payloads are all empty produces
    no rows at all -- so demanding exactly the roster size would refuse a
    genuinely complete build. The gap this has to separate is three orders of
    magnitude wide (20 GPs versus ~6,800), so the threshold does not need to
    be precise.

    The roster size comes from the LGD tree rather than a literal here, but
    ``conformance.EXPECTED_GP_COUNT`` is a deliberately independent literal --
    conformance is written against the spec, not against our loader's inputs,
    so a corrupted reference tree cannot satisfy both. A test asserts they
    still agree.

    There is deliberately no --allow-partial override. An override exists to
    be used, and the only caller who needs one is the caller this guard is
    for.
    """

    expected = len(gp_geography())
    floor = int(expected * MIN_GP_COVERAGE)
    with attach_read_only(artifact) as conn:
        tables = conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = ?",
            [ATTACH_CATALOG],
        ).fetchall()
        if ("gram_panchayat",) not in tables:
            print(
                f"error: {artifact} has no gram_panchayat table; refusing to pin an "
                "artifact whose scope cannot be established",
                file=sys.stderr,
            )
            return 1
        actual = conn.execute(
            f"SELECT count(*) FROM {ATTACH_CATALOG}.gram_panchayat"
        ).fetchone()[0]
    if not floor <= actual <= expected:
        print(
            f"error: {artifact} holds {actual:,} gram_panchayat rows; a deployable "
            f"snapshot must hold between {floor:,} and {expected:,} (the LGD reference "
            "tree). A partial build must not be pinned; rebuild from a full-state "
            "snapshot, or refresh lgd_codes.json if Odisha's roster really changed.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        refusal = assert_full_state(args.artifact)
    except GeographyError as exc:
        # Blames the right file: a missing lgd_codes.json is not a problem
        # with the artifact being pinned.
        print(f"error: cannot read the LGD reference tree: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any read failure is a refusal
        print(f"error: cannot read {args.artifact}: {exc}", file=sys.stderr)
        return 1
    if refusal:
        return refusal
    try:
        manifest = build_manifest(
            args.artifact,
            bucket=args.bucket,
            key=args.key,
            version_id=args.version_id,
            label=args.label,
            expectations_key=args.expectations_key,
            expectations_version_id=args.expectations_version_id,
            # `or` would make an intentionally empty list unexpressible, so a
            # corrected artifact would keep claiming caveats it no longer has.
            known_exceptions=_known_exceptions(args),
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
