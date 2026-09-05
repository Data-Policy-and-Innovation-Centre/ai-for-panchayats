"""Each deployment assertion must fail on its own (#93).

The issue's last criterion is that every check turns the workflow red
individually. A verification script whose assertions can only be exercised
together is one where a broken assertion hides behind a passing neighbour.
"""

from __future__ import annotations

import pytest

from scripts.verify_deployment import (
    VerificationError,
    check_build_info,
    check_query,
    check_rollout,
)

TD = "arn:aws:ecs:ap-south-1:000000000000:task-definition/prdw-chatbot:12"
OLD_TD = "arn:aws:ecs:ap-south-1:000000000000:task-definition/prdw-chatbot:11"
TAG = "c48c7c2b8c5a-0f70811-arm64"
CONSUMER = "0f70811f7071f13e2d6620bef3f430375728284f"


def _service(**overrides):
    service = {
        "deployments": [{"status": "PRIMARY", "rolloutState": "COMPLETED", "taskDefinition": TD}],
        "runningCount": 1,
    }
    service.update(overrides)
    return service


def _payload(**overrides):
    payload = {
        "build": {"repo_commit": "a" * 40, "consumer_commit": CONSUMER, "image_tag": TAG},
        "snapshot": {
            "label": "full_state", "version_id": "VER1", "sha256": "b" * 64,
            "byte_size": 1, "aggregates_verified": True, "verified_at": "2026-09-06T00:00:00Z",
        },
    }
    payload.update(overrides)
    return payload


# --- the happy path, so the failures below mean something --------------------

def test_a_completed_rollout_of_the_expected_revision_passes():
    assert check_rollout(_service(), TD)


def test_matching_build_info_passes():
    assert check_build_info(_payload(), TAG, CONSUMER)


# --- rollout, the assertion the issue names explicitly -----------------------

def test_the_task_definition_assertion_fails_on_its_own():
    """The circuit-breaker rollback case: everything else is healthy, and the
    service is simply running the previous revision. `terraform apply` exits 0
    through exactly this state."""

    service = _service(deployments=[
        {"status": "PRIMARY", "rolloutState": "COMPLETED", "taskDefinition": OLD_TD},
    ])
    with pytest.raises(VerificationError, match="but this run registered"):
        check_rollout(service, TD)


def test_an_incomplete_rollout_fails_on_its_own():
    service = _service(deployments=[{
        "status": "PRIMARY", "rolloutState": "FAILED",
        "rolloutStateReason": "circuit breaker tripped", "taskDefinition": TD,
    }])
    with pytest.raises(VerificationError, match="rolloutState"):
        check_rollout(service, TD)


def test_a_service_scaled_to_zero_fails_on_its_own():
    with pytest.raises(VerificationError, match="runningCount"):
        check_rollout(_service(runningCount=0), TD)


def test_a_service_with_no_primary_deployment_fails():
    with pytest.raises(VerificationError, match="no PRIMARY"):
        check_rollout(_service(deployments=[]), TD)


# --- build info --------------------------------------------------------------

def test_a_stale_image_tag_fails_on_its_own():
    payload = _payload(build={"consumer_commit": CONSUMER, "image_tag": "an-older-tag-arm64"})
    with pytest.raises(VerificationError, match="image_tag"):
        check_build_info(payload, TAG, CONSUMER)


def test_a_mismatched_consumer_commit_fails_on_its_own():
    payload = _payload(build={"consumer_commit": "f" * 40, "image_tag": TAG})
    with pytest.raises(VerificationError, match="consumer_commit"):
        check_build_info(payload, TAG, CONSUMER)


def test_a_skipped_aggregate_gate_fails_on_its_own():
    """A task serving a database whose private aggregate gate never ran."""

    payload = _payload()
    payload["snapshot"] = dict(payload["snapshot"], aggregates_verified=False)
    with pytest.raises(VerificationError, match="aggregates=SKIPPED"):
        check_build_info(payload, TAG, CONSUMER)


def test_an_unavailable_snapshot_identity_fails_on_its_own():
    with pytest.raises(VerificationError, match="no snapshot identity"):
        check_build_info(_payload(snapshot="unavailable"), TAG, CONSUMER)


def test_html_instead_of_json_is_reported_as_a_shadowed_route():
    """The #85 failure mode: /deployment.json falling through to the static
    mount returns index.html with a 200, which looks like success."""

    with pytest.raises(VerificationError, match="build object"):
        check_build_info({"not": "a build payload"}, TAG, CONSUMER)


# --- query -------------------------------------------------------------------

def test_the_routers_clarification_branch_is_not_a_passing_round_trip():
    with pytest.raises(VerificationError, match="clarification"):
        check_query({"needs_clarification": True, "answer": "Which district?"})


def test_an_empty_answer_fails():
    with pytest.raises(VerificationError, match="no non-empty answer"):
        check_query({"answer": "   "})


def test_a_structurally_valid_answer_passes_whatever_it_says():
    """Structure only. Asserting on generated text produces a check that fails
    for reasons unrelated to the deployment."""

    assert check_query({"answer": "There are 6,794 gram panchayats."})
    assert check_query({"response": "anything at all, really"})
