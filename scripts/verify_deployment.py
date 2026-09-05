"""Prove the thing that is running is the thing this run deployed (#93).

Reporting `/ -> 200` as deployment success was wrong once already: the page
rendered blank because `crypto.randomUUID()` is undefined outside a secure
context, and a status code only ever proved the transport.

The same mistake is available one layer up. With
`deployment_minimum_healthy_percent = 100` the previous task keeps serving
until the new one is healthy, so a check against the public URL can pass in
full while the deployment being verified never rolled at all. Only an
assertion about the ECS deployment itself is evidence that the thing under
test is the thing running -- and only that assertion catches the circuit
breaker having rolled back behind Terraform's back (see the comment on
deployment_circuit_breaker in service.tf).

    uv run python scripts/verify_deployment.py \
        --cluster prdw-chatbot --service prdw-chatbot \
        --task-definition arn:...:task-definition/prdw-chatbot:12 \
        --image-tag c48c7c2b8c5a-0f70811-arm64 \
        --consumer-commit 0f70811... --url https://example.cloudfront.net
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 30


class VerificationError(AssertionError):
    """A deployment assertion failed. Each one is independently fatal."""


def check_rollout(service: dict[str, Any], expected_task_definition: str) -> list[str]:
    """The primary deployment completed, and it is the revision we registered."""

    deployments = service.get("deployments", [])
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if primary is None:
        raise VerificationError("the service has no PRIMARY deployment")

    state = primary.get("rolloutState")
    if state != "COMPLETED":
        raise VerificationError(
            f"primary deployment rolloutState is {state!r}, not 'COMPLETED'"
            + (
                f" -- rolloutStateReason: {primary.get('rolloutStateReason')!r}"
                if primary.get("rolloutStateReason") else ""
            )
        )

    actual = primary.get("taskDefinition", "")
    if actual != expected_task_definition:
        raise VerificationError(
            f"the service is running {actual!r} but this run registered "
            f"{expected_task_definition!r}. A circuit-breaker rollback leaves exactly "
            "this state, and terraform apply exits 0 through it."
        )

    running = service.get("runningCount", 0)
    if running < 1:
        raise VerificationError(
            f"runningCount is {running}; a service left at desired_count = 0 would "
            "otherwise pass every other check here"
        )

    return [
        f"rollout      COMPLETED on {actual}",
        f"runningCount {running}",
    ]


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as error:
        raise VerificationError(f"could not reach {url}: {error}") from error
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        # The specific failure this guards: /deployment.json falling through to
        # the StaticFiles mount and returning index.html with a 200.
        raise VerificationError(
            f"{url} did not return JSON. If this is HTML, the route is being shadowed "
            f"by the static mount. First 120 chars: {body[:120]!r}"
        ) from error


def check_build_info(payload: Any, image_tag: str, consumer_commit: str) -> list[str]:
    """The running task reports the image and consumer commit we deployed."""

    if not isinstance(payload, dict):
        raise VerificationError(f"/deployment.json returned {type(payload).__name__}, not an object")

    build = payload.get("build")
    if not isinstance(build, dict):
        raise VerificationError("/deployment.json carries no build object")

    if build.get("image_tag") != image_tag:
        raise VerificationError(
            f"the task reports image_tag {build.get('image_tag')!r} but this run deployed "
            f"{image_tag!r}"
        )
    if build.get("consumer_commit") != consumer_commit:
        raise VerificationError(
            f"the task reports consumer_commit {build.get('consumer_commit')!r} but the pin "
            f"says {consumer_commit!r}"
        )

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise VerificationError(
            "the task reports no snapshot identity, so it cannot show its database was verified"
        )
    if snapshot.get("aggregates_verified") is not True:
        raise VerificationError(
            "the snapshot identity reads aggregates=SKIPPED. The task is serving a database "
            "whose private aggregate gate did not run."
        )

    return [
        f"image_tag    {build['image_tag']}",
        f"consumer     {build['consumer_commit']}",
        f"snapshot     {snapshot.get('label')} @ {snapshot.get('version_id')} aggregates=verified",
    ]


def check_query(payload: Any) -> list[str]:
    """A /query round-trip returns something structurally real.

    The assertion is on STRUCTURE, never on generated text -- asserting on a
    language model's wording produces a check that fails for reasons that have
    nothing to do with the deployment.
    """

    if not isinstance(payload, dict):
        raise VerificationError(f"/query returned {type(payload).__name__}, not an object")

    # The router's give-up branch is the thing to exclude: it returns a
    # clarification rather than an answer, and #98 records it as the slow path.
    if payload.get("needs_clarification") or payload.get("clarification"):
        raise VerificationError(
            "/query returned the router's clarification branch rather than an answer; "
            "that is the fallback path, not a working round-trip"
        )

    answer = payload.get("answer") or payload.get("response") or payload.get("result")
    if not isinstance(answer, str) or not answer.strip():
        raise VerificationError(
            f"/query returned no non-empty answer field; keys were {sorted(payload)!r}"
        )
    return [f"query        answered ({len(answer)} chars, structure only)"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--task-definition", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--consumer-commit", required=True)
    parser.add_argument("--url", required=True, help="public base URL, no trailing slash")
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--skip-query", action="store_true",
        help="skip the /query round-trip only; every other assertion still runs",
    )
    args = parser.parse_args(argv)

    import boto3

    lines: list[str] = []
    try:
        ecs = boto3.client("ecs", region_name=args.region)
        described = ecs.describe_services(cluster=args.cluster, services=[args.service])
        services = described.get("services", [])
        if not services:
            raise VerificationError(f"no service {args.service!r} in cluster {args.cluster!r}")
        lines += check_rollout(services[0], args.task_definition)

        lines += check_build_info(
            fetch_json(f"{args.url.rstrip('/')}/deployment.json"),
            args.image_tag, args.consumer_commit,
        )

        if not args.skip_query:
            request = urllib.request.Request(
                f"{args.url.rstrip('/')}/query",
                data=json.dumps({"question": "How many gram panchayats are there?"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    lines += check_query(json.loads(response.read().decode("utf-8")))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                raise VerificationError(f"/query round-trip failed: {error}") from error
    except VerificationError as error:
        print(f"::error title=Deployment verification failed::{error}", file=sys.stderr)
        return 1

    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
