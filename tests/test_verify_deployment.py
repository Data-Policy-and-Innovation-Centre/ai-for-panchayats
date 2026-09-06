"""Each deployment assertion must fail on its own (#93).

The issue's last criterion is that every check turns the workflow red
individually. A verification script whose assertions can only be exercised
together is one where a broken assertion hides behind a passing neighbour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_deployment import (
    VerificationError,
    check_build_info,
    check_query,
    check_rollout,
)

ROOT = Path(__file__).resolve().parents[1]

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

# The router's real vocabulary, taken from scripts/benchmark_deployment.py.
# The first version of these tests asserted an invented schema
# (`needs_clarification`, a bare `answer`) and therefore passed against a
# check that would have accepted a fallback as a real answer.

@pytest.mark.parametrize("tier", ["clarify", "fallback"])
def test_a_non_answer_tier_is_not_a_passing_round_trip(tier: str):
    """Both deflection tiers return 200 and take about as long as a real
    answer, so neither the status code nor the latency distinguishes them."""

    with pytest.raises(VerificationError, match="deflecting"):
        check_query({"tier": tier, "answer": "Which district?", "query_id": "q1"})


def test_prose_without_a_query_id_fails():
    """An answer with no query_id means no query was executed -- the model
    said something, which is not the same as the database being reachable."""

    with pytest.raises(VerificationError, match="no query_id"):
        check_query({"tier": "sql", "answer": "There are 6,794 gram panchayats."})


def test_a_real_answer_passes_whatever_it_says():
    """Structure only. Asserting on generated text produces a check that fails
    for reasons unrelated to the deployment."""

    assert check_query({"tier": "sql", "query_id": "q-123", "answer": "6,794."})
    assert check_query({"tier": "rag", "query_id": "q-456", "answer": "anything at all"})


def test_the_tier_list_matches_the_established_client():
    """These two scripts talk to the same endpoint; they must not disagree."""

    from scripts.verify_deployment import NON_ANSWER_TIERS
    benchmark = (ROOT / "scripts" / "benchmark_deployment.py").read_text(encoding="utf-8")
    assert 'NON_ANSWER_TIERS = {"clarify", "fallback"}' in benchmark
    assert NON_ANSWER_TIERS == {"clarify", "fallback"}


# --- wait_for_rollout ------------------------------------------------------
#
# The deploy of the caveat fix applied cleanly, the service reached COMPLETED
# seconds later, and verify still failed:
#
#     primary deployment rolloutState is 'IN_PROGRESS', not 'COMPLETED'
#
# `wait_for_steady_state = true` returns when the service is stable, which is
# not the instant rolloutState flips. Asserting once made the check flaky, and a
# check that fails good deploys is the one people learn to ignore.
#
# Distinct helper names on purpose: this module already defines `_service` and
# `TD`, and a second definition would silently shadow them for every test above.

from scripts import verify_deployment as _vd  # noqa: E402

WAIT_TD = "arn:aws:ecs:ap-south-1:000000000000:task-definition/prdw-chatbot:14"


def _rollout_service(state, task_definition=WAIT_TD, running=1):
    return {
        "deployments": [
            {
                "status": "PRIMARY",
                "rolloutState": state,
                "rolloutStateReason": "because",
                "taskDefinition": task_definition,
            }
        ],
        "runningCount": running,
    }


class _FakeEcs:
    """Yields each queued service shape in turn, then repeats the last."""

    def __init__(self, shapes):
        self.shapes = list(shapes)
        self.calls = 0

    def describe_services(self, cluster, services):
        shape = self.shapes[min(self.calls, len(self.shapes) - 1)]
        self.calls += 1
        return {"services": [shape]}


def test_wait_polls_through_in_progress_then_succeeds():
    ecs = _FakeEcs([
        _rollout_service("IN_PROGRESS"),
        _rollout_service("IN_PROGRESS"),
        _rollout_service("COMPLETED"),
    ])
    slept: list[float] = []
    _service_out, lines = _vd.wait_for_rollout(
        ecs, "c", "s", WAIT_TD, sleep=slept.append, clock=lambda: 0.0
    )
    assert ecs.calls == 3
    assert slept == [_vd.ROLLOUT_POLL_SECONDS] * 2
    assert any("COMPLETED" in line for line in lines)


def test_wait_fails_immediately_on_a_circuit_breaker_trip():
    """FAILED is final; waiting on it would delay the report by five minutes."""
    ecs = _FakeEcs([_rollout_service("FAILED")])
    with pytest.raises(_vd.VerificationError) as caught:
        _vd.wait_for_rollout(ecs, "c", "s", WAIT_TD, sleep=lambda _: None, clock=lambda: 0.0)
    assert not isinstance(caught.value, _vd.RolloutInProgress)
    assert ecs.calls == 1


def test_wait_fails_immediately_on_a_rollback():
    """COMPLETED on the WRONG revision is exactly what a rollback leaves."""
    ecs = _FakeEcs([_rollout_service("COMPLETED", task_definition=WAIT_TD.replace(":14", ":11"))])
    with pytest.raises(_vd.VerificationError):
        _vd.wait_for_rollout(ecs, "c", "s", WAIT_TD, sleep=lambda _: None, clock=lambda: 0.0)
    assert ecs.calls == 1


def test_wait_gives_up_loudly_rather_than_hanging():
    ticks = iter([0.0, 0.0, 999.0, 999.0])
    ecs = _FakeEcs([_rollout_service("IN_PROGRESS")])
    with pytest.raises(_vd.VerificationError, match="still not COMPLETED"):
        _vd.wait_for_rollout(
            ecs, "c", "s", WAIT_TD, timeout=10, sleep=lambda _: None, clock=lambda: next(ticks)
        )
