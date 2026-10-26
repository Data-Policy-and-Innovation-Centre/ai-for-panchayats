"""The Image workflow's paths filter must cover every hashed build input.

A PR that touches a build input but does not match the filter never runs the
Image job, so nothing rebuilds and nothing complains -- the tag simply stays
stale. That failure is silent by construction, which is exactly the kind #90's
review asked to be made loud. This couples the two lists so a change to
BUILD_INPUTS that forgets the workflow fails here instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "image.yml"

sys.path.insert(0, str(ROOT / "scripts"))
from compute_image_tag import BUILD_INPUTS  # noqa: E402


def _pr_paths() -> list[str]:
    # `on` is parsed by PyYAML 1.1 rules as the boolean True, not the string.
    doc = yaml.safe_load(WORKFLOW.read_text())
    triggers = doc.get("on", doc.get(True))
    return triggers["pull_request"]["paths"]


def test_pull_request_declares_a_paths_filter() -> None:
    assert _pr_paths(), "an unfiltered pull_request trigger rebuilds on every docs PR"


@pytest.mark.parametrize("declared", BUILD_INPUTS)
def test_every_build_input_is_covered_by_the_filter(declared: str) -> None:
    patterns = _pr_paths()
    covered = declared in patterns or f"{declared}/**" in patterns
    assert covered, (
        f"BUILD_INPUTS lists {declared!r} but .github/workflows/image.yml does not "
        f"match it, so a PR changing it would skip the image build entirely"
    )


def test_the_resolver_and_the_workflow_trigger_themselves() -> None:
    # Neither is a BUILD_INPUT -- they are not baked into the image -- but both
    # decide what tag gets minted, so a change to either must still build.
    patterns = _pr_paths()
    for extra in ("scripts/compute_image_tag.py", ".github/workflows/image.yml"):
        assert extra in patterns, f"{extra} changes the tag but would not trigger a build"
