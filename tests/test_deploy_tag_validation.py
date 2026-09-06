"""The deploy workflow's tag guard must accept every tag we have ever shipped.

Two properties, and they pull against each other. The guard exists because
`inputs.image_tag` reaches a shell in a job that already holds AWS credentials,
so it has to be strict. But tightening it to the post-#90 shape alone silently
dropped the pre-#90 form -- which is what the image currently in production
carries, so the first content-tagged deploy could not have been rolled back.

The pattern is read out of the workflow rather than copied here, so a future
edit to one cannot pass while the other still describes the old behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"

# The production tag at the time #90 landed, recorded in docker/requirements.txt.
LEGACY_PRODUCTION_TAG = "7b4242d-0f70811-arm64"


def _guard() -> re.Pattern[str]:
    line = next(
        ln for ln in WORKFLOW.read_text().splitlines() if "grep -Eq" in ln and "arm64" in ln
    )
    pattern = re.search(r"'(\^.*\$)'", line).group(1)
    return re.compile(pattern)


@pytest.mark.parametrize(
    "tag",
    [
        "b9bfd01439a7-0f70811-arm64",  # minted by compute_image_tag.py
        LEGACY_PRODUCTION_TAG,  # minted by the pre-#90 build.sh
    ],
)
def test_accepts_every_shape_we_have_published(tag: str) -> None:
    assert _guard().match(tag), f"{tag!r} is a real tag; rejecting it strands a rollback"


@pytest.mark.parametrize(
    "tag",
    [
        "deadbeef'; aws s3 rm s3://x --recursive #-arm64",  # the injection this guards
        "b9bfd01439a7-0f70811",  # no arch suffix: service.tf would reject it
        "b9bfd01439a7-0f70811-amd64",  # wrong architecture
        "ZZZZZZZZZZZZ-0f70811-arm64",  # not hex
        "b9bfd01439a-0f70811-arm64",  # 11 hex: neither shape
        "",
    ],
)
def test_rejects_anything_we_would_not_mint(tag: str) -> None:
    assert not _guard().match(tag)


def test_the_guard_is_anchored_at_both_ends() -> None:
    # Without both anchors a crafted prefix or suffix rides along, which is the
    # whole point of validating before the value reaches a shell.
    src = _guard().pattern
    assert src.startswith("^") and src.endswith("$")
