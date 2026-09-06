"""Every workflow job must be able to assume the role it names.

The subject GitHub mints is decided by the EVENT and the job's `environment:`,
not by the role. So a job can name a perfectly correct role and still be
refused, and nothing in Terraform or in the workflow says so.

That is not hypothetical. The first real deploy died here:

    role-to-assume: ai-for-panchayats-ci-tf-plan
    Could not assume role with OIDC: Not authorized to perform
    sts:AssumeRoleWithWebIdentity

`deploy.yml`'s preflight and verify assume the plan role from a `workflow_run`
on main, which mints `:ref:refs/heads/main`, while the role trusted only
`:pull_request`. preflight failed and apply, verify and record were skipped.
Three review rounds and a security review all read the trust policy in
isolation and none of them asked whether the workflow could actually use it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = ROOT / "infra" / "terraform" / "ci"

OWNER = "Data-Policy-and-Innovation-Centre"
REPO = "ai-for-panchayats"

# The repository variable each workflow names -> the Terraform trust document
# whose `sub` condition must cover it.
ROLE_VARS = {
    "AWS_TF_PLAN_ROLE_ARN": "plan_trust",
    "AWS_TF_APPLY_ROLE_ARN": "apply_trust",
    "AWS_ECR_PUSH_ROLE_ARN": "push_trust",
}


def _ci_source() -> str:
    return (CI / "roles.tf").read_text() + "\n" + (CI / "main.tf").read_text()


def _resolve(template: str) -> str:
    return (
        template.replace("${var.github_owner}", OWNER)
        .replace("${var.github_repository}", REPO)
        .replace("${var.deploy_branch}", "main")
    )


def _locals() -> dict[str, str]:
    return {
        name: _resolve(tpl)
        for name, tpl in re.findall(r"(\w*subject)\s*=\s*\"([^\"]+)\"", _ci_source())
    }


def _trusted_subjects(trust_doc: str) -> set[str]:
    """The subjects a trust document's `sub` condition actually lists.

    Read from the VALUES LIST, not from the locals. An earlier version of this
    test resolved every `*_subject` local in the module and passed even with the
    fix reverted -- the local still existed, it had simply stopped being
    referenced. A test that passes both with and without the fix pins nothing.
    """

    block = re.search(
        r"data\s+\"aws_iam_policy_document\"\s+\"" + trust_doc + r"\"\s*\{(.*?)\n\}",
        _ci_source(),
        re.S,
    )
    assert block, f"no trust document named {trust_doc}"

    sub = re.search(
        r"variable\s*=\s*\"token\.actions\.githubusercontent\.com:sub\"(.*?)\}",
        block.group(1),
        re.S,
    )
    assert sub, f"{trust_doc} has no sub condition"

    values = re.search(r"values\s*=\s*\[(.*?)\]", sub.group(1), re.S)
    assert values, f"{trust_doc}'s sub condition lists no values"

    names = re.findall(r"local\.(\w+)", values.group(1))
    table = _locals()
    resolved = {table[n] for n in names if n in table}
    literals = {_resolve(v) for v in re.findall(r"\"([^\"]+)\"", values.group(1))}
    return resolved | literals


def _triggers(doc: dict) -> dict:
    # PyYAML 1.1 parses the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True)) or {}


def _minted_subjects(doc: dict, job: dict, step_if: str = "") -> set[str]:
    """The subject(s) GitHub can mint for this job, honouring a step guard.

    The step guard matters: image.yml builds on pull_request AND push, but
    guards the credential step with `if: github.event_name == 'push'`, so a PR
    never assumes anything. Ignoring that would fail a correct workflow, and a
    test that cries wolf is a test someone deletes.
    """

    events = set(re.findall(r"github\.event_name\s*==\s*'([a-z_]+)'", step_if))
    env = job.get("environment")
    if env:
        name = env if isinstance(env, str) else env.get("name")
        if name:
            return {f"repo:{OWNER}/{REPO}:environment:{name}"}

    triggers = _triggers(doc)
    if events:
        triggers = {k: v for k, v in triggers.items() if k in events}

    subjects: set[str] = set()
    if "pull_request" in triggers:
        subjects.add(f"repo:{OWNER}/{REPO}:pull_request")
    for event in ("push", "workflow_run"):
        spec = triggers.get(event) or {}
        for branch in (spec.get("branches") if isinstance(spec, dict) else None) or []:
            subjects.add(f"repo:{OWNER}/{REPO}:ref:refs/heads/{branch}")
    if "workflow_dispatch" in triggers:
        # Dispatch runs on a ref; on this repo that is main.
        subjects.add(f"repo:{OWNER}/{REPO}:ref:refs/heads/main")
    return subjects


def _jobs_assuming_roles() -> list[tuple[str, str, str, set[str]]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                role = (step.get("with") or {}).get("role-to-assume", "")
                m = re.search(r"vars\.(\w+)", str(role))
                if m and m.group(1) in ROLE_VARS:
                    minted = _minted_subjects(doc, job, str(step.get("if", "")))
                    found.append((path.name, job_name, m.group(1), minted))
    assert found, "no workflow job assumes an OIDC role -- the parser is wrong"
    return found


@pytest.mark.parametrize(
    "workflow,job,role_var,minted",
    [pytest.param(*c, id=f"{c[0]}::{c[1]}") for c in _jobs_assuming_roles()],
)
def test_the_role_trusts_every_subject_this_job_can_mint(
    workflow: str, job: str, role_var: str, minted: set[str]
) -> None:
    trusted = _trusted_subjects(ROLE_VARS[role_var])
    assert trusted, f"no trusted subject resolved for {role_var}"
    assert minted, f"could not determine what subject {workflow}::{job} mints"

    missing = minted - trusted
    assert not missing, (
        f"{workflow} job {job!r} assumes {role_var} and can mint {sorted(missing)}, "
        f"which that role does not trust (it trusts {sorted(trusted)}). "
        f"sts:AssumeRoleWithWebIdentity would be refused at runtime."
    )
