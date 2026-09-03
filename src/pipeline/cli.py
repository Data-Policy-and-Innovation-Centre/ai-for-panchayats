"""Thin command-line dispatch for the batch pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from enum import StrEnum
from typing import Any

import yaml
from pathlib import Path

from .manifest import ManifestError, RunPublisher, validate_run
from .normalize import normalize_egramswaraj
from .settings import load_settings


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
    ingest.add_argument(
        "--payload-tree", action="append", default=[], type=Path, metavar="ROOT",
        help="copy every file under ROOT into payloads/, keyed by its path "
             "relative to ROOT; repeatable, and combinable with --payload",
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


PROGRESS_EVERY = 10_000


def _write_tree(publisher: RunPublisher, root: Path) -> tuple[int, list[str]]:
    """Copy every regular file under ``root`` into ``payloads/``.

    Keyed by the path relative to ``root``, so a scraper tree of
    ``LGD_<code>_<name>/<year>_<KIND>.json`` arrives with the folder names
    intact -- which matters, because ``normalize._gp_context`` recovers the
    GP code from exactly those parent directories.

    Symlinks are skipped rather than followed: a raw run's manifest is an
    inventory of real bytes with real hashes, and a link that resolves
    outside the tree is not something this run can honestly claim to have
    published. Skipped links are returned so the caller can record them --
    never dropped silently.
    """

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"invalid --payload-tree {root}; not a directory")
    written = 0
    skipped: list[str] = []
    # Sorted so two runs over the same tree publish in the same order, which
    # keeps the audit log and any progress output comparable between runs.
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            skipped.append(str(path.relative_to(root)))
            continue
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            publisher.write_payload(path.relative_to(root), handle)
        written += 1
        if written % PROGRESS_EVERY == 0:
            # A full-state run publishes ~204,000 files. Silence for that long
            # is indistinguishable from a hang.
            print(f"  {written:,} files copied from {root}", file=sys.stderr, flush=True)
    if not written:
        raise ValueError(f"invalid --payload-tree {root}; contains no files")
    return written, skipped


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
        tree_files = 0
        tree_symlinks: list[str] = []
        for root in args.payload_tree:
            written, skipped = _write_tree(publisher, root)
            tree_files += written
            tree_symlinks.extend(skipped)
        # The file count goes in the audit log rather than being inferred
        # later: a run that published 203,812 of an expected 203,820 files is
        # only answerable if the number it actually wrote was recorded.
        audit: dict[str, Any] = {
            "event": "ingest", "source": args.source, "run_id": args.run_id,
        }
        if args.payload_tree:
            audit["payload_trees"] = [str(root) for root in args.payload_tree]
            audit["payload_files"] = tree_files
        if tree_symlinks:
            # Never silent: a skipped symlink is data that did not make it in.
            audit["skipped_symlinks"] = sorted(tree_symlinks)
            print(
                f"warning: skipped {len(tree_symlinks)} symlink(s) under --payload-tree; "
                "a raw run inventories real files only (see audit.jsonl)",
                file=sys.stderr,
            )
        publisher.append_audit(audit)
        destination = publisher.publish(
            terminal_state=args.terminal_state,
            observed_scope=args.observed_scope,
            counts=_counts(args.count),
        )
    print(destination)
    return 0


def _registry_stanza(output_root: Path) -> str:
    """The `config/snapshots.yaml` entry for a snapshot that was just written.

    Printed rather than appended, deliberately. ``snapshots.py`` refuses any
    entry whose status is not ``approved``, so writing this file *is* the
    approval -- and approving one's own output automatically is how an
    unreviewed snapshot reaches a build. Pasting it is a person's decision;
    getting the fields right is not, which is the part this removes.

    (#95 asks for the publish-and-open-a-PR command. This is not it.)
    """

    # Same filename literal as normalize.validate_canonical_manifest reads;
    # the raw run's manifest.json is a different file in a different tree.
    manifest = json.loads(
        (output_root / "canonical_manifest.json").read_text(encoding="utf-8")
    )
    source, run_id = manifest["source"], manifest["run_id"]
    # Emitted by the YAML dumper rather than formatted by hand, because every
    # field here is required to be a *string* and several plausible values are
    # not strings in YAML: a run id of `2026-09-03` parses as a date, and one
    # of `20260903` as an integer. Both make the pasted entry fail
    # load_snapshot_registry with "has an empty field", a long way from the
    # cause.
    entry = {
        "id": f"{source.lower()}-{run_id}",
        "source": source,
        "run_id": run_id,
        "schema_version": str(manifest["schema_version"]),
        "status": "approved",
    }
    dumped = yaml.safe_dump([entry], sort_keys=False, default_flow_style=False)
    return "\n".join(f"  {line}" for line in dumped.rstrip().splitlines())


def _normalize(args: argparse.Namespace) -> int:
    result = normalize_egramswaraj(
        args.run_path,
        args.output_root,
        chunk_size=args.chunk_size,
        kinds=(kind.strip() for kind in args.kinds.split(",") if kind.strip()),
    )
    print(f"published {result.output_root} ({len(result.tables)} tables; "
          f"{result.quarantine_count} quarantined)")
    if result.quarantine_count:
        # Surfaced next to the stanza on purpose: approving a snapshot is the
        # moment to look at what it could not keep.
        print(f"\nRead the quarantine table before approving: "
              f"{result.quarantine_count} row(s) were not loaded.")
    # Built before the heading is printed, so a failure here cannot leave a
    # "paste this" banner with nothing under it.
    stanza = _registry_stanza(result.output_root)
    # The registry path, not a hardcoded one: PIPELINE_SNAPSHOTS points a
    # staging run at config/snapshots.staging.yaml, and telling that run to
    # paste into the production registry is how a sample gets approved for a
    # production build.
    registry_path = load_settings().snapshots_path
    print(f"\nTo build from this snapshot, add to {registry_path} "
          "under `snapshots:` --\n")
    print(stanza)
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
