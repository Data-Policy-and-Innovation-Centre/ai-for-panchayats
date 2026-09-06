"""The consumer pin, and the ref validation that makes it meaningful (#83).

The image tag is built with the string slice ``${CONSUMER_REF:0:7}``. That
slice does not fail on a branch name -- it produces a tag ending ``-master``
naming an image nobody can trace back to a commit, which is the defect this
pin exists to close. So the interesting assertions are all about *rejection*.

``build.sh --print-consumer-ref`` exits immediately after resolution, before
the tool checks, the clone and ``npm ci``. That is what lets these run on a
machine with no docker daemon and no network.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_SH = ROOT / "docker" / "build.sh"
PIN = ROOT / "infra" / "consumer" / "pin.json"

PINNED_COMMIT = "0f70811f7071f13e2d6620bef3f430375728284f"


def _resolve(ref: str | None = None, *, pin_file: Path | None = None):
    """Run the resolution seam, returning the completed process."""

    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(ROOT)}
    if ref is not None:
        env["CONSUMER_REF"] = ref
    if pin_file is not None:
        env["PIN_FILE"] = str(pin_file)
    return subprocess.run(
        ["bash", str(BUILD_SH), "--print-consumer-ref"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60,
    )


# --- the pin itself --------------------------------------------------------

def test_the_pin_is_tracked_and_carries_all_four_fields():
    assert PIN.is_file(), f"{PIN} must be tracked"
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    assert set(pin) >= {"repo", "commit", "resolved_at", "subject"}
    assert all(isinstance(pin[k], str) and pin[k].strip() for k in pin)


def test_the_pinned_commit_is_a_full_lowercase_sha():
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", pin["commit"]), pin["commit"]
    assert pin["commit"] == PINNED_COMMIT


def test_the_pin_is_tracked_in_git():
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "infra/consumer/pin.json"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert tracked.returncode == 0, "pin.json is not tracked by git"


# --- resolution ------------------------------------------------------------

def test_an_unset_ref_resolves_to_the_pinned_commit():
    """The criterion behind 'CONSUMER_REF unset builds the pinned commit'.

    Note this is a deliberate behaviour *change*: build.sh previously used
    `${CONSUMER_REF:?...}` and refused to run at all when unset.
    """

    result = _resolve(None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == PINNED_COMMIT


def test_an_explicit_full_sha_is_accepted_and_wins_over_the_pin():
    other = "a" * 40
    result = _resolve(other)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == other


# --- rejection: the three shapes the issue names, plus one -----------------

@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("master", "a branch name"),
        ("0f70811", "a 7-hex abbreviation"),
        (PINNED_COMMIT[:39], "a 39-hex near-miss"),
        (PINNED_COMMIT.upper(), "an uppercase SHA"),
        ("v1.0.0", "a tag"),
        (PINNED_COMMIT + "f", "a 41-hex value"),
    ],
)
def test_a_ref_that_is_not_a_full_lowercase_sha_is_rejected(ref: str, why: str):
    result = _resolve(ref)
    assert result.returncode != 0, f"{why} was accepted: {result.stdout!r}"
    assert "40-character lowercase hex" in result.stderr, result.stderr
    # The tag slice must never have been reached.
    assert result.stdout.strip() == ""


def test_rejection_happens_before_any_clone_or_npm_work():
    """`--print-consumer-ref` exits before the tool checks, so a rejection
    cannot be a missing-docker error wearing the wrong message."""

    result = _resolve("master")
    assert "required tool not found" not in result.stderr
    assert "docker" not in result.stderr.lower()


def test_the_message_blames_the_environment_variable_not_the_pin():
    """A caller who set CONSUMER_REF must be told to fix CONSUMER_REF.

    The pin branch is entered whenever *either* CONSUMER_REF or CONSUMER_REPO
    is unset, so attributing the source to that branch blamed pin.json for a
    value the caller supplied.
    """

    result = _resolve("master")
    assert "CONSUMER_REF environment variable" in result.stderr
    assert "pin.json" not in result.stderr


def test_a_pin_missing_its_commit_is_refused_rather_than_defaulted(tmp_path: Path):
    broken = tmp_path / "pin.json"
    broken.write_text(json.dumps({"repo": "https://example.invalid/x.git"}), encoding="utf-8")
    result = _resolve(None, pin_file=broken)
    assert result.returncode != 0
    assert "not a usable consumer pin" in result.stderr


def test_a_missing_pin_is_refused_rather_than_defaulted(tmp_path: Path):
    result = _resolve(None, pin_file=tmp_path / "absent.json")
    assert result.returncode != 0
    assert "no CONSUMER_REF set and no pin" in result.stderr
