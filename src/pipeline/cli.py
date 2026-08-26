"""Thin command-line dispatch for the batch pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from enum import StrEnum
from pathlib import Path

from .manifest import ManifestError, RunPublisher, validate_run
from .normalize import normalize_egramswaraj


class Stage(StrEnum):
    NORMALIZE = "normalize"
    BUILD = "build"
    EXPORT_CSV = "export-csv"


class StageNotImplemented(RuntimeError):
    """Raised when a later serial stage has not been implemented yet."""


def dispatch(stage: Stage, **_: object) -> None:
    """Typed stage dispatch reserved for later serial pipeline work."""

    raise StageNotImplemented(
        f"stage {stage.value!r} is not implemented in the foundation; "
        "source adapters and canonicalization arrive in later stages"
    )


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-for-panchayats")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="publish a generic raw run")
    ingest.add_argument("--raw-root", required=True, type=Path)
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--run-id", required=True)
    ingest.add_argument("--schema-version", default="1")
    ingest.add_argument("--code-sha", default="unknown")
    ingest.add_argument("--config-hash", default="unknown")
    ingest.add_argument("--privacy-class", default="restricted")
    ingest.add_argument("--requested-scope", type=_json_object, default={})
    ingest.add_argument("--observed-scope", type=_json_object, default={})
    ingest.add_argument("--count", action="append", default=[], metavar="NAME=INTEGER")
    ingest.add_argument(
        "--payload", action="append", default=[], metavar="NAME=PATH",
        help="copy a local synthetic/test payload into payloads/",
    )
    ingest.add_argument("--terminal-state", default="complete")

    verify = commands.add_parser("validate-run", help="verify a published raw run")
    verify.add_argument("run_path", type=Path)

    normalize = commands.add_parser(Stage.NORMALIZE.value, help="normalize an eGramSwaraj raw run")
    normalize.add_argument("--run-path", required=True, type=Path)
    normalize.add_argument("--output-root", required=True, type=Path)
    normalize.add_argument("--chunk-size", type=int, default=100_000)
    normalize.add_argument("--kinds", default=",".join(sorted({"PL", "AA", "TA", "PP", "RE"})))
    for name in (Stage.BUILD.value, Stage.EXPORT_CSV.value):
        commands.add_parser(name, help="reserved later pipeline stage")
    return parser


def _counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"invalid --count {value!r}; expected NAME=INTEGER")
        name, raw_count = value.split("=", 1)
        if not name:
            raise ValueError(f"invalid --count {value!r}")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"invalid --count {value!r}; INTEGER required") from exc
        if count < 0:
            raise ValueError(f"invalid --count {value!r}; INTEGER must be non-negative")
        counts[name] = count
    return counts


def _ingest(args: argparse.Namespace) -> int:
    with RunPublisher(
        args.raw_root,
        args.source,
        args.run_id,
        schema_version=args.schema_version,
        code_sha=args.code_sha,
        config_hash=args.config_hash,
        requested_scope=args.requested_scope,
        privacy_class=args.privacy_class,
    ) as publisher:
        for specification in args.payload:
            if "=" not in specification:
                raise ValueError(f"invalid --payload {specification!r}; expected NAME=PATH")
            name, path = specification.split("=", 1)
            with Path(path).open("rb") as handle:
                publisher.write_payload(name, handle)
        publisher.append_audit({"event": "ingest", "source": args.source, "run_id": args.run_id})
        destination = publisher.publish(
            terminal_state=args.terminal_state,
            observed_scope=args.observed_scope,
            counts=_counts(args.count),
        )
    print(destination)
    return 0


def _normalize(args: argparse.Namespace) -> int:
    result = normalize_egramswaraj(
        args.run_path,
        args.output_root,
        chunk_size=args.chunk_size,
        kinds=(kind.strip() for kind in args.kinds.split(",") if kind.strip()),
    )
    print(f"published {result.output_root} ({len(result.tables)} tables; "
          f"{result.quarantine_count} quarantined)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ingest":
            return _ingest(args)
        if args.command == "validate-run":
            report = validate_run(args.run_path)
            print(f"valid: {report.run_path} ({report.checked_files} files)")
            return 0
        if args.command == Stage.NORMALIZE.value:
            return _normalize(args)
        dispatch(Stage(args.command))
    except (ManifestError, StageNotImplemented, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
