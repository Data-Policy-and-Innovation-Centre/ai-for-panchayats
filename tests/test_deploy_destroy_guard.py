"""The deploy's destroy guard must block real removals and allow a new revision.

Both halves are load-bearing and they pull against each other.

The guard exists because a PR that deletes a resource block deletes its
`prevent_destroy` with it, so `terraform plan` succeeds and the PR check goes
green -- the apply is the last place to notice.

But an ECS task definition is IMMUTABLE: changing the image forces a new
revision, which Terraform models as delete+create. The first real deploy failed
with "this apply would remove aws_ecs_task_definition.app" -- the guard refusing
to let a deploy be a deploy. Exempting replacement too broadly would give back
the destroy protection entirely, so the exemption is one address AND requires a
paired create.

The guard is extracted from the workflow rather than copied, so the two cannot
drift.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


def _guard_source() -> str:
    src = WORKFLOW.read_text()
    match = re.search(r"python3 - <<'PYEOF'\n(.*?)\n\s+PYEOF", src, re.S)
    assert match, "the destroy guard's heredoc was not found in deploy.yml"
    body = textwrap.dedent(match.group(1))
    assert "resource_changes" in body, "extracted the wrong heredoc"
    return body


def _run(changes: list[dict], tmp_path: Path) -> int:
    plan = tmp_path / "tfplan.json"
    plan.write_text(json.dumps({"resource_changes": changes}))
    body = _guard_source().replace('"/tmp/tfplan.json"', repr(str(plan)))
    return subprocess.run([sys.executable, "-c", body], capture_output=True, text=True).returncode


TASKDEF = "aws_ecs_task_definition.app"


@pytest.mark.parametrize(
    "name,changes,expected",
    [
        # A deploy IS a task-definition replacement. Terraform emits the pair in
        # either order depending on create_before_destroy.
        ("replacement, create first", [{"address": TASKDEF, "change": {"actions": ["create", "delete"]}}], 0),
        ("replacement, delete first", [{"address": TASKDEF, "change": {"actions": ["delete", "create"]}}], 0),
        ("no destructive change", [{"address": "aws_ecs_service.app", "change": {"actions": ["update"]}}], 0),
        # A bare delete of the task definition is a removal, not a deploy.
        ("task definition removed outright", [{"address": TASKDEF, "change": {"actions": ["delete"]}}], 1),
        # The exemption must not generalise to anything else being replaced.
        ("distribution replaced", [{"address": "aws_cloudfront_distribution.cdn", "change": {"actions": ["delete", "create"]}}], 1),
        ("audit bucket destroyed", [{"address": "aws_s3_bucket.audit", "change": {"actions": ["delete"]}}], 1),
        ("snapshot bucket destroyed", [{"address": "aws_s3_bucket.snapshots", "change": {"actions": ["delete"]}}], 1),
    ],
)
def test_guard(name: str, changes: list[dict], expected: int, tmp_path: Path) -> None:
    got = _run(changes, tmp_path)
    verb = "allow" if expected == 0 else "block"
    assert got == expected, f"the guard should {verb} {name!r}, got exit {got}"


def test_a_real_deploy_and_a_real_destruction_are_told_apart(tmp_path: Path) -> None:
    """The distinction the guard exists for, in one assertion."""
    deploy = [
        {"address": TASKDEF, "change": {"actions": ["create", "delete"]}},
        {"address": "aws_ecs_service.app", "change": {"actions": ["update"]}},
    ]
    sabotage = [
        {"address": TASKDEF, "change": {"actions": ["create", "delete"]}},
        {"address": "aws_s3_bucket.snapshots", "change": {"actions": ["delete"]}},
    ]
    assert _run(deploy, tmp_path) == 0, "a routine image deploy must not be blocked"
    assert _run(sabotage, tmp_path) == 1, "a destroy riding alongside a deploy must still be caught"
