"""Measure the deployed chatbot's query latency, error rate and answer parity.

#72 asks for a benchmark that is reproducible and committed, rather than a
number pasted into an issue once. This is that command.

Three things are measured, and the third is the one that needs explaining:

  latency   p50/p95/p99 wall-clock per query, from outside AWS, so it includes
            CloudFront, the load balancer and the router's own LLM calls. That
            is the number a user experiences; the database's share of it is
            small (see #72 -- the workload is network-bound, not IO-bound).

  errors    any non-200, counted separately from slow answers. A timeout is an
            error here even though CloudFront would render it a 504.

  parity    the same question asked repeatedly must produce the same answer.
            With more than one task behind the load balancer this is the check
            that catches a mixed-snapshot deployment: two tasks serving
            different databases return different numbers for the same
            question, and nothing else in the stack would notice.

Parity compares a SHA-256 of the answer-bearing fields, never the response
text, so a report from this script carries no data from the snapshot and is
safe to attach to a public issue.

It compares an explicit allow-list rather than everything-minus-a-deny-list.
That is not fastidiousness: the deny-list version was written first and
reported a parity failure on every question against a SINGLE task, because the
router picks its follow-up suggestion chips non-deterministically and stamps
each response with its own latency. A deny-list also fails open in the wrong
direction over time -- every new cosmetic field the application adds becomes a
false alarm, and a benchmark that cries wolf gets ignored exactly when it is
right.

Usage:

    export CHATBOT_URL=https://<distribution>.cloudfront.net
    export CHATBOT_USER=pilot
    export CHATBOT_PASSWORD="$(terraform -chdir=infra/terraform/app \\
                               output -raw basic_auth_password)"
    uv run python scripts/benchmark_deployment.py --repeat 3

The credential is read from the environment, never passed as an argument:
arguments are world-readable in `ps auxww` on a shared machine.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

# Every question here reaches tier2 and returns rows. Verified, not assumed --
# an earlier version of this list looked reasonable and clarified on all six,
# which would have reported the clarifier's latency as the database's.
#
# The year is why. Phrased without one, each of these dead-ends in a
# clarification; with "in 2024" the same question executes. That includes the
# router's OWN suggestion chips, which are emitted containing the literal words
# "in a year" and clarify again when sent back verbatim -- so a user who clicks
# a suggestion gets another clarification rather than an answer. Filed
# upstream; it is a router defect, not a benchmark problem, but it decides what
# can be measured here.
#
# Coverage is deliberately narrow for a second reason: with #61 unresolved,
# every district, block and GP question clarifies whatever the phrasing, so
# including one would benchmark the clarifier again. Widen this list when #61
# lands.
#
# Six distinct templates: EXP-025, IMP-002, PLU-007, STS-003, STS-006, PLN-012.
# Row counts range from 1 to 200, so the set spans a single aggregate through a
# result set large enough to serialise.
QUESTIONS = [
    "What is the total actual expenditure under each focus area in 2024?",
    "What is the total actual expenditure under each focus area in 2023-24?",
    "How many initiated activities have been completed in 2024?",
    "How many initiated activities have been completed in 2023-24?",
    "What is the cost-band split (below 500, 500-1000, above 1000) in 2024?",
    "How many activities are in WORK COMPLETED status for 2024?",
    "What percentage of taken-up activities are completed in 2024?",
    "What is the status of the GPDP in 2024?",
]

# The fields that constitute the answer, and nothing else.
#
#   result   the rows -- the only thing a different snapshot could change
#   query_id the template the router chose, e.g. EXP-025; a stable identifier,
#            so a change here means the router reached a different query
#   tier     which path answered (tier2, clarify, fallback). A run that starts
#            silently falling back is a failure worth catching, and the HTTP
#            status would still be 200.
#
# Deliberately excluded: `answer` and `query_description` (model prose),
# `suggestions` and `clarification` (chips, chosen non-deterministically),
# `latency_ms` and `session_id` (different by construction on every call).
ANSWER_KEYS = ("result", "query_id", "tier")


def answer_fields(document: dict) -> dict:
    """The comparable core of a response, with everything cosmetic dropped."""
    return {key: document.get(key) for key in ANSWER_KEYS}


def fingerprint(payload: bytes) -> str:
    """A stable digest of an answer's content, with no content in the output."""
    try:
        normalised = answer_fields(json.loads(payload))
    except (json.JSONDecodeError, AttributeError):
        # A non-JSON body is itself the finding; hash it as-is rather than
        # silently reporting a parity failure with no explanation.
        return "raw:" + hashlib.sha256(payload).hexdigest()[:16]
    canonical = json.dumps(normalised, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()[:16]


def ask(url: str, question: str, session: str, auth: str | None, timeout: float):
    """One query. Returns (seconds, http_status, fingerprint_or_None)."""
    request = urllib.request.Request(
        url.rstrip("/") + "/query",
        data=json.dumps({"message": question, "session_id": session}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if auth:
        request.add_header("Authorization", auth)

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return time.monotonic() - started, response.status, fingerprint(body)
    except urllib.error.HTTPError as exc:
        # Drain the body so the connection closes cleanly, but do not
        # fingerprint an error page as though it were an answer.
        exc.read()
        return time.monotonic() - started, exc.code, None
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"    transport failure: {exc}", file=sys.stderr)
        return time.monotonic() - started, 0, None


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Not statistics.quantiles: that interpolates, which invents a latency
    between two measured ones. At n=18 the invented value can sit outside
    anything actually observed, and a benchmark should only report numbers it
    saw.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[rank - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeat", type=int, default=3, help="passes over the question set (default 3)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="per-query timeout in seconds; matches CloudFront's origin read timeout (default 60)")
    parser.add_argument("--url", default=os.environ.get("CHATBOT_URL", ""),
                        help="base URL; defaults to $CHATBOT_URL")
    args = parser.parse_args()

    if not args.url:
        parser.error("no URL: pass --url or set CHATBOT_URL")

    user = os.environ.get("CHATBOT_USER", "")
    password = os.environ.get("CHATBOT_PASSWORD", "")
    if bool(user) != bool(password):
        parser.error("set both CHATBOT_USER and CHATBOT_PASSWORD, or neither")
    auth = None
    if user:
        auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    latencies: list[float] = []
    failures: list[tuple[str, int]] = []
    prints: dict[str, set[str]] = {q: set() for q in QUESTIONS}

    print(f"{args.url}  x{args.repeat} passes over {len(QUESTIONS)} questions\n")
    for pass_index in range(args.repeat):
        for question in QUESTIONS:
            # A fresh session per call. Reusing one would let the router answer
            # from conversation context and report a latency no first-time user
            # ever sees.
            session = f"bench-{pass_index}-{abs(hash(question)) % 10**6}"
            seconds, status, digest = ask(args.url, question, session, auth, args.timeout)
            if status == 200 and digest:
                latencies.append(seconds)
                prints[question].add(digest)
                print(f"  {seconds:7.3f}s  {digest}  {question[:58]}")
            else:
                failures.append((question, status))
                print(f"  {seconds:7.3f}s  HTTP {status:<3}  {question[:58]}")

    print()
    if latencies:
        print(f"n={len(latencies)}  p50={percentile(latencies, 0.50):.3f}s  "
              f"p95={percentile(latencies, 0.95):.3f}s  p99={percentile(latencies, 0.99):.3f}s  "
              f"min={min(latencies):.3f}s  max={max(latencies):.3f}s  "
              f"mean={statistics.fmean(latencies):.3f}s")
    print(f"errors: {len(failures)} of {len(QUESTIONS) * args.repeat}")
    for question, status in failures:
        print(f"  HTTP {status}  {question}")

    divergent = {q: d for q, d in prints.items() if len(d) > 1}
    if divergent:
        print("\nPARITY FAILURE — the same question returned different answers.")
        print("With more than one task running this is a mixed-snapshot deployment;")
        print("with one task it is non-determinism in the router. Either is a defect.")
        for question, digests in divergent.items():
            print(f"  {sorted(digests)}  {question}")
    elif args.repeat > 1:
        print(f"parity: OK — every question returned one distinct answer across {args.repeat} passes")

    return 1 if failures or divergent else 0


if __name__ == "__main__":
    sys.exit(main())
