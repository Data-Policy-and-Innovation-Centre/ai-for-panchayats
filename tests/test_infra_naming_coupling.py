"""The two name variables that must not drift apart (#89 review).

Every ARN pattern in `infra/terraform/ci/roles_policies.tf` scopes the apply
role to resources named from `infra/terraform/app`'s `var.name`. The CI module
cannot read the app module's variables -- they are separate root modules with
separate state -- so it carries its own `var.app_name_prefix` default and the
two are kept equal by hand.

Nothing in Terraform enforces that. If they drift, the failure is silent in
the dangerous direction: the apply role's grants stop matching the resources
it manages, and the apply fails partway with AccessDenied after some of the
stack is already built. This test is the enforcement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_VARS = ROOT / "infra" / "terraform" / "app" / "variables.tf"
CI_VARS = ROOT / "infra" / "terraform" / "ci" / "variables.tf"


def _default_of(path: Path, name: str) -> str:
    """The `default` of one variable block, read without a HCL parser."""

    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf'variable\s+"{re.escape(name)}"\s*\{{(.*?)\n\}}', text, re.DOTALL
    )
    assert match, f"no variable {name!r} in {path}"
    default = re.search(r'default\s*=\s*"([^"]*)"', match.group(1))
    assert default, f"variable {name!r} in {path} has no string default"
    return default.group(1)


def test_the_ci_prefix_matches_the_app_modules_name():
    app_name = _default_of(APP_VARS, "name")
    ci_prefix = _default_of(CI_VARS, "app_name_prefix")
    assert app_name == ci_prefix, (
        f"infra/terraform/app var.name is {app_name!r} but infra/terraform/ci "
        f"var.app_name_prefix is {ci_prefix!r}. Every ARN the apply role is scoped "
        "to is built from the CI value; if it stops matching what the app module "
        "actually names, the apply fails AccessDenied partway through."
    )


@pytest.mark.parametrize(
    "pattern",
    [
        # Each of these is an ARN scope that silently stops matching if the
        # prefix drifts. Listed so a new one added without a scope is visible.
        "role/${var.app_name_prefix}",
        "secret:${var.app_name_prefix}/",
        ":${var.app_name_prefix}*",
    ],
)
def test_the_scoping_patterns_are_built_from_the_variable_not_a_literal(pattern: str):
    """A hardcoded "prdw-chatbot" in a policy would pass the test above while
    ignoring the variable entirely."""

    policies = (ROOT / "infra" / "terraform" / "ci" / "roles_policies.tf").read_text()
    assert pattern in policies, f"expected {pattern!r} to be built from the variable"


def test_no_policy_hardcodes_the_app_name():
    policies = (ROOT / "infra" / "terraform" / "ci" / "roles_policies.tf").read_text()
    app_name = _default_of(APP_VARS, "name")
    # Comments may name it; policy strings may not.
    code = "\n".join(
        line for line in policies.splitlines() if not line.lstrip().startswith("#")
    )
    assert app_name not in code, (
        f"{app_name!r} appears literally in a policy expression; scope through "
        "var.app_name_prefix so the coupling test above can catch drift"
    )


def test_the_boundary_name_matches_between_the_two_modules() -> None:
    """app's boundary name must equal ci's "<github_repository>-ci-apply-boundary".

    The app module attaches the boundary by NAME, built into an ARN from the
    caller's account id, because it cannot read the ci module's variables. If
    the two drift the failure is loud but late: CreateRole fails at apply time
    against a policy ARN that does not exist, after part of the stack is built.

    This exists because the boundary stopped being optional. While it defaulted
    to "" a wrong name was harmless; now it is the only thing standing between
    the apply role and account admin, so it has to be pinned.
    """
    app_name = re.search(
        r'variable "iam_permissions_boundary_name".*?default\s*=\s*"([^"]+)"',
        (ROOT / "infra/terraform/app/variables.tf").read_text(),
        re.S,
    ).group(1)

    ci_repo = re.search(
        r'variable "github_repository".*?default\s*=\s*"([^"]+)"',
        (ROOT / "infra/terraform/ci/variables.tf").read_text(),
        re.S,
    ).group(1)

    ci_policy = re.search(
        r'resource "aws_iam_policy" "apply_boundary".*?name\s*=\s*"([^"]+)"',
        (ROOT / "infra/terraform/ci/roles.tf").read_text(),
        re.S,
    ).group(1)

    expected = ci_policy.replace("${var.github_repository}", ci_repo)
    assert app_name == expected, (
        f"app/variables.tf names the boundary {app_name!r} but ci/roles.tf creates "
        f"{expected!r}; CreateRole would fail against a non-existent policy ARN"
    )


def test_the_boundary_is_not_optional() -> None:
    """No role may be created without a ceiling.

    The regression this pins: `permissions_boundary = var.x != "" ? var.x : null`
    silently created unbounded roles, and nothing in the repository ever set the
    variable. Verified against the live roles at the time: all three carried
    PermissionsBoundary = None.
    """
    for rel in ("infra/terraform/app/iam.tf", "infra/terraform/app/audit.tf"):
        src = (ROOT / rel).read_text()
        for line in src.splitlines():
            if "permissions_boundary" in line and "=" in line and not line.strip().startswith("#"):
                assert "null" not in line, (
                    f"{rel} can still create a role with no boundary: {line.strip()!r}"
                )
