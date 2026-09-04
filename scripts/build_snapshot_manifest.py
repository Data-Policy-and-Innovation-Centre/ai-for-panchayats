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
from src.warehouse.conformance import MIN_GP_COVERAGE, check_geography_completeness
from src.warehouse.geography import GEOGRAPHY_COLUMNS, GeographyError, gp_geography

# #61 is no longer here: gram_panchayat now carries state/district/block for
# every GP in the LGD reference tree. Dropping the exception is only honest if
# something checks, so assert_full_state below verifies the geography is
# actually populated -- not merely that the row count is right -- and refuses
# the artifact otherwise. The older externally built artifact, whose geography
# is blank, is therefore refused rather than pinned with an exception: there
# is no --known-exception that makes a blank-geography database deployable.
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


MISMATCHES_SHOWN = 5


def _geography_mismatches(rows: list[tuple], lookup) -> dict[str, list[str]]:
    """GP code -> what disagrees with the LGD reference tree, per row.

    Keyed by row rather than returned flat so the caller can say how many
    *rows* are wrong: one swapped GP disagrees on four columns at once, and
    counting those separately would report a 6,794-row table as having
    40,764 bad rows.

    Cardinality is not correctness. ``check_geography_completeness`` proves
    every column is populated and that the artifact has ~30 districts and
    ~314 blocks -- all of which a build that joined on ``gp_name`` instead of
    ``gp_lgd_code`` would also satisfy, while handing 505 GPs that share a
    name to the wrong district (#136).

    This compares against the same tree the build read, so it establishes
    faithful *transfer*, not ground truth -- nothing here can tell you the
    LGD file itself is right. What it does catch is an artifact whose
    geography did not come from this tree by this code: a mis-keyed join, a
    column shifted by one, a build from a stale reference, or an externally
    produced database being pinned as though our pipeline had made it.
    """

    mismatches: dict[str, list[str]] = {}
    for code, *values in rows:
        expected = lookup.get(code)
        if expected is None:
            mismatches[code] = ["not in the LGD reference tree"]
            continue
        wrong = [
            f"{column} is {value!r}, tree says {expected[column]!r}"
            for column, value in zip(GEOGRAPHY_COLUMNS, values)
            if value != expected[column]
        ]
        if wrong:
            mismatches[code] = wrong
    return mismatches


def assert_full_state(artifact: Path) -> int:
    """Refuse to pin an artifact that is not the whole state.

    This is the only place a database becomes deployable: the deployment
    reads ``infra/snapshots/full_state.json``, and this script is what writes
    it. So this is where a sample has to be stopped.

    It has to be stopped somewhere, because a 20-GP smoke build is otherwise
    indistinguishable from a full one -- same tables, same schema, same
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
        # Row count alone would wave through the exact artifact #61 is named
        # for: 6,794 rows with every geography column NULL. Since this script
        # is what drops the #61 exception from the manifest, it has to be what
        # establishes the exception is really gone.
        #
        # Delegated to the conformance check that already defines "geography
        # is complete" -- columns present, every row populated, district and
        # block cardinality -- rather than restating a weaker version here.
        # USE makes its unqualified queries read the attached artifact.
        conn.execute(f"USE {ATTACH_CATALOG}")
        geography = check_geography_completeness(conn)
        # Cardinality says the shape is right; the row comparison below says
        # the rows are. Only reached once completeness has passed: selecting
        # these columns from an artifact that is missing one raises a binder
        # error, which would mask conformance's own geography.columns finding
        # with a stack-shaped message about the wrong thing.
        geography_rows = [] if geography else conn.execute(
            "SELECT gp_lgd_code, " + ", ".join(GEOGRAPHY_COLUMNS)
            + f" FROM {ATTACH_CATALOG}.gram_panchayat"
        ).fetchall()
        # PROFILE is deliberately not in schema.REQUIRED_KINDS -- an
        # independent reference extract must not make a rebuild of the scraped
        # data impossible (#123). That argument is only honest if completeness
        # is enforced where the artifact actually becomes deployable, which is
        # here. Without this a build selecting only the PL/AA/TA/PP/RE
        # snapshots publishes an empty gp_profile: the DDL creates the table
        # either way, and nothing downstream reads its row count.
        profile_rows = None if ("gp_profile",) not in tables else conn.execute(
            f"SELECT count(*) FROM {ATTACH_CATALOG}.gp_profile"
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
    if geography:
        print(
            f"error: {artifact} holds {actual:,} gram_panchayat rows but its geography "
            "is not complete; refusing to pin an artifact that would be published as "
            "having resolved #61:",
            file=sys.stderr,
        )
        for finding in geography:
            print(
                f"  {finding.check}: expected {finding.expected}, got {finding.actual}",
                file=sys.stderr,
            )
        return 1
    if profile_rows is None:
        print(
            f"error: {artifact} has no gp_profile table; a deployable snapshot must "
            "carry GP demographics (#123). Rebuild including the profile snapshot.",
            file=sys.stderr,
        )
        return 1
    # Against the same roster and the same floor as gram_panchayat above. The
    # ceiling is the roster, not equality: 84 of the 6,794 GPs have no profile
    # upstream at all, so demanding one per GP would refuse a complete build.
    if not floor <= profile_rows <= expected:
        print(
            f"error: {artifact} holds {profile_rows:,} gp_profile rows; a deployable "
            f"snapshot must hold between {floor:,} and {expected:,}. An empty or "
            "partial gp_profile means the profile snapshot was left out of the "
            "build, not that the demographics are missing upstream.",
            file=sys.stderr,
        )
        return 1
    mismatches = _geography_mismatches(geography_rows, gp_geography())
    if mismatches:
        print(
            f"error: {artifact} has {len(mismatches):,} gram_panchayat row(s) whose "
            "geography disagrees with the LGD reference tree; refusing to pin an "
            "artifact whose place mapping this pipeline did not produce:",
            file=sys.stderr,
        )
        for code in sorted(mismatches)[:MISMATCHES_SHOWN]:
            print(f"  {code}: {'; '.join(mismatches[code])}", file=sys.stderr)
        if len(mismatches) > MISMATCHES_SHOWN:
            print(f"  ... and {len(mismatches) - MISMATCHES_SHOWN:,} more", file=sys.stderr)
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
