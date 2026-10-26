"""The image tag, derived from the build inputs rather than from a commit (#90).

A tag derived from `git rev-parse --short HEAD` makes every commit on `main` a
new image even when nothing that goes into the image changed. Against an ECR
repository with IMMUTABLE tags that is not merely wasteful: it also means a
re-run of a workflow cannot be a harmless no-op, because the tag it computes
depends on which commit it happens to be running for.

Hashing the inputs instead gives three properties at once:

- two commits that change no build input produce the SAME tag, so the push is
  a no-op and the deploy is a no-op;
- any change to an input produces a different tag, so nothing is ever silently
  served from a stale image;
- the tag can be computed without a clone of the consumer, a docker daemon or
  any AWS call, which is what lets a deploy job name an image that a skipped
  build job never pushed.

    uv run python scripts/compute_image_tag.py
    uv run python scripts/compute_image_tag.py --platform linux/amd64
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Everything that ends up inside the image, or that decides what does.
#
# `infra/consumer/pin.json` is here because it selects the consumer commit the
# image is built from (#83); a pin bump must therefore mint a new tag. The
# manifest under `infra/snapshots/` is COPY'd into the image, so a snapshot
# re-pin is an image change even though no code moved.
BUILD_INPUTS: tuple[str, ...] = (
    "docker",
    "src/deploy",
    "scripts/fetch_snapshot.py",
    "infra/snapshots",
    "infra/consumer/pin.json",
)

# Never part of the image, and non-deterministic between machines.
EXCLUDED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})
EXCLUDED_SUFFIXES = (".pyc", ".pyo")

ARCH_SUFFIX = {"linux/arm64": "-arm64", "linux/amd64": ""}

DIGEST_CHARS = 12


class BuildInputError(RuntimeError):
    """A declared build input is missing, so the tag would be a lie."""


def _iter_files(root: Path, spec: str) -> list[Path]:
    target = root / spec
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(
            path
            for path in target.rglob("*")
            if path.is_file()
            and not path.name.endswith(EXCLUDED_SUFFIXES)
            and EXCLUDED_DIRS.isdisjoint(path.relative_to(root).parts)
        )
    raise BuildInputError(
        f"declared build input {spec!r} does not exist under {root}; "
        "a tag computed without it would not describe the image"
    )


def build_input_files(root: Path = ROOT) -> list[Path]:
    """Every file feeding the image, in a stable order."""

    files: list[Path] = []
    for spec in BUILD_INPUTS:
        files.extend(_iter_files(root, spec))
    # Sorted by POSIX-style relative path so the order cannot depend on the
    # filesystem's own ordering, which differs between macOS and Linux.
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def inputs_digest(root: Path = ROOT) -> str:
    """A hex digest over the content AND the layout of the build inputs.

    Both matter. Hashing only contents would give the same digest to a file
    renamed between two COPY'd directories, and hashing only names would miss
    every edit.
    """

    digest = hashlib.sha256()
    for path in build_input_files(root):
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        # Length-prefixed, so no combination of a path and its content can be
        # confused with a different split of the same bytes.
        digest.update(f"{len(relative)}:{relative}:{len(payload)}:".encode())
        digest.update(payload)
    return digest.hexdigest()


def consumer_short(root: Path = ROOT) -> str:
    """The pinned consumer commit, abbreviated for the human reading a tag."""

    pin = json.loads((root / "infra" / "consumer" / "pin.json").read_text(encoding="utf-8"))
    commit = pin.get("commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise BuildInputError("infra/consumer/pin.json carries no 40-hex commit")
    return commit[:7]


def compute_tag(root: Path = ROOT, *, platform: str = "linux/arm64") -> str:
    """The full tag, including the architecture suffix service.tf requires."""

    if platform not in ARCH_SUFFIX:
        raise BuildInputError(f"unsupported platform {platform!r}")
    return (
        f"{inputs_digest(root)[:DIGEST_CHARS]}-{consumer_short(root)}{ARCH_SUFFIX[platform]}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", default="linux/arm64", choices=sorted(ARCH_SUFFIX))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--list-inputs", action="store_true",
        help="print the files that feed the tag, for debugging a tag that changed unexpectedly",
    )
    args = parser.parse_args(argv)

    try:
        if args.list_inputs:
            for path in build_input_files(args.root):
                print(path.relative_to(args.root).as_posix())
            return 0
        print(compute_tag(args.root, platform=args.platform))
    except BuildInputError as error:
        print(f"cannot compute an image tag: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
