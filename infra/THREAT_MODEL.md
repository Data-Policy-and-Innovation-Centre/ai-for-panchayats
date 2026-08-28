# Threat model: DuckDB-backed Odisha_PRDW chatbot on AWS

Scope: the deployment described by `infra/terraform/snapshot` (the artifact
store), `infra/terraform/app` (the runtime), `docker/` (the image) and
`src/deploy/` (the startup verification). Written for #59; reviewed against
the deployment running as of this commit.

Each threat below lists the controls that exist, what would detect the attempt,
and the residual risk that is knowingly accepted. Residual risks are collected
again at the end so they can be read without the rest.

The single most important thing to know before reading further: **the
application is public and unauthenticated.** Every "who could do this" below
answers *anyone on the internet who has the URL* unless it says otherwise.

---

## 1. Artifact substitution

*Someone replaces the DuckDB snapshot with one that answers questions wrongly.*

The artifact is the answer. Nothing downstream re-derives it, so a substituted
snapshot is indistinguishable from a correct one at the point of use — it has
to be caught at the point of load.

**Controls**

- The bucket blocks all public access, is versioned, and is encrypted with a
  customer-managed KMS key. Its policy denies non-TLS access, denies any upload
  not encrypted with KMS, and denies uploads under any key but that CMK
  (`infra/terraform/snapshot/main.tf`).
- The manifest pins **content**, not location: object version id, byte size and
  SHA-256. It is `COPY`d into the image, so the pin travels with the code that
  uses it rather than being fetched from somewhere mutable.
- `fetch_snapshot` verifies the object version, the byte count and the SHA-256
  before the file is usable, runs a known-answer aggregate gate against it, and
  only then publishes atomically. `entrypoint.sh` runs it under `set -e` before
  `exec uvicorn`, so a task that cannot verify never opens its port.
- The task role can `GetObject` only under the deployment prefix and holds no
  write permission anywhere. `s3:PutObject` and `s3:DeleteObject` on the
  snapshot both evaluate to `implicitDeny` under `iam simulate-principal-policy`
  (matrix in #56).
- Versioning means an overwrite does not destroy the previous object, so the
  pinned version keeps resolving even after a bad upload.

**Detection**

CloudTrail S3 data events on the snapshot bucket (`infra/terraform/app/audit.tf`)
record every `PutObject`, `DeleteObject` and `GetObject` against it, with the
calling principal. This is the only place a substitution shows up: management
events would show the bucket policy being changed, not the object.

**Residual risk**

An operator credential with S3 write access can upload a new object *and* open
a manifest PR pinning it. Nothing distinguishes that from a legitimate
republish except review of the PR. The chain is only as strong as the human
reading the manifest diff.

---

## 2. Stale or mixed snapshots

*The app serves last month's data, or serves one relation from one snapshot and
another relation from a different one.*

**Controls**

- The snapshot is a single file fetched once at task start, opened `READ_ONLY`
  from an in-memory catalog. There is no partial refresh and no second source,
  so "mixed" cannot arise from the runtime — only from a manifest that pins
  parts inconsistently.
- The manifest carries a relation inventory alongside the digest, and the
  expectations object is separately version-pinned, so the known-answer gate is
  checked against the expectations that belong to that snapshot.
- 70 tests in `tests/test_deploy_{fetch,manifest,expectations}.py` cover a
  corrupted byte, a truncated file, a wrong object version, a deleted
  expectations version, and out-of-order multipart writes.

**Detection**

Weak, and this is the honest gap. Nothing running today reports *which*
snapshot a live task is serving — the identity is verified at startup and then
discarded. Answering "is production stale?" currently means reading the image
tag and correlating it by hand. Issue **#E2** in the continuous-delivery
milestone exists to fix exactly this: labels plus a build-info route that
states the commit, image and snapshot identity a task is actually running.

**Residual risk**

Staleness is silent. A snapshot can be months old and every health check still
passes, because "old" is not "unhealthy". Until the build-info route lands,
freshness is a manual check.

---

## 3. Credential compromise

*An attacker obtains AWS credentials, or the OpenAI key.*

**Controls**

- Execution role and task role are separate. The execution role reads the one
  secret and pulls the image; the running application can do neither. A
  compromise of the application process therefore does not yield the ability to
  pull arbitrary images or read other secrets.
- The OpenAI key lives in Secrets Manager and is injected by ECS at task start.
  **It is not in Terraform state** — it is set out of band by
  `scripts/set_openai_key.sh`, which never passes it in `argv` (visible in
  `ps auxww`) and writes it via a 0700 temp file.
- The task role's S3 grant is read-only and prefix-scoped; its KMS grant is
  `Decrypt` on one CMK.
- Terraform state lives in a private, encrypted, lock-enabled bucket.

**Detection**

CloudTrail records every **management** API call in the account, across all
regions, with log file validation enabled — so a deleted or edited log file is
detectable rather than merely unlikely. That covers role and policy changes, ECS
and Terraform activity, and anything an attacker does to widen their own access.

Its limit matters here: **data** events are recorded only for the snapshot
bucket. A read of the Terraform state bucket — the residual risk named just
below — is an S3 data event outside that selector and would leave no trace.
Widening the selector to the state bucket is a one-line change if that stops
being an acceptable blind spot.

**Residual risk**

- Terraform state **does** contain `random_password.origin_verify`, the shared
  secret CloudFront presents to the load balancer. Anyone who can read the state
  bucket can bypass CloudFront and reach the load balancer directly. That is a
  low-value bypass — the origin serves the same public content — but it is real,
  and it is why the state bucket is not merely a convenience store.
- The deployment is applied today from a long-lived IAM user (`user/DPIC`), not
  a federated or short-lived identity. Replacing that is the whole point of
  issues G and H in the continuous-delivery milestone; until they land, one
  static credential can do everything described in this document.
- There is no MFA condition and no permissions boundary on that user.

---

## 4. Expensive queries

*A user, or a script, drives cost or denial of service through the query path.*

Two budgets are at risk and they behave differently: **LLM spend**, which is
unbounded and metered per request, and **compute**, which is fixed at one task
and degrades rather than bills.

**Controls**

- The OpenAI client has an explicit timeout (`LLM_TIMEOUT_SECONDS`). Its absence
  was the root cause of #100: a hang is not an exception, so the `try/except`
  meant to degrade gracefully never fired and tasks blocked past the health
  check grace period forever.
- CloudFront's origin read timeout is 60s and the load balancer's idle timeout
  is 120s, so a slow answer is cut rather than held open indefinitely.
- The database is read-only and memory-mapped; a query cannot mutate it or
  outlive the request.
- An AWS Budget at $450/month notifies on account spend, and five CloudWatch
  alarms cover no-running-tasks, unhealthy hosts, target 5xx, ELB 5xx and slow
  responses.

**Detection**

`prdw-chatbot-request-flood` alarms on origin requests above 300 in a
five-minute period, sustained over two periods. It is measured at the load
balancer, so it counts what CloudFront could not serve from cache — which is
exactly the requests that reach the language model. The threshold is grounded in
measurement: the busiest observed five minutes carried 24 requests.

The latency alarm is the other half of the same picture, and it has already
proved itself. A burst of 33–39 second responses was recorded at the load
balancer on 2026-08-28 and did not reproduce minutes later at the same
questions. p95 over 15 seconds for two consecutive periods is the line, so one
slow period does not page anyone while a sustained one does.

Note what none of these do: the budget covers **AWS** spend only. OpenAI spend is
billed by OpenAI and is invisible to every alarm in this account.

**Residual risk**

**This is the largest accepted risk in the deployment.** There is no rate limit,
no WAF, no per-client quota and no authentication. A single script can issue
requests as fast as one task will serve them, and every one of them spends
OpenAI credit. The flood alarm says it is happening; it does not stop it, and it
fires after the requests have already been paid for. The only hard backstop is
the OpenAI account's own spending cap, which is set outside this repository. Mitigation is the authentication decision that
this issue leaves open.

---

## 5. Supply chain

*Malicious or merely unexpected code arrives through a dependency or a base
image.*

**Controls**

- ECR tags are immutable and scan-on-push is enabled, so a pushed tag cannot be
  silently repointed at different content.
- Build attestations are disabled (`--provenance=false --sbom=false`) because an
  attestation manifest can otherwise claim the immutable tag.
- The consumer application is built from a pinned commit of a specific
  repository, and the built image records that commit in its tag.

**Residual risk**

- `docker/requirements.txt` floats on `>=` with no lockfile, and the Vite build
  runs `npm ci` on the host with whatever Node happens to be installed.
  Rebuilding the same tag can therefore produce different content, which makes
  ECR immutability a weaker guarantee than it appears. Issue **E1** in the
  continuous-delivery milestone addresses this directly.
- Nothing acts on scan findings; scan-on-push records them, and no alarm reads
  them.
- The image is built on a developer laptop, not in CI. Issue **J** moves it.

---

## 6. Public application abuse

*The endpoint is used by people it was not meant for, or for purposes it was
not meant for.*

**Controls**

- Viewers reach CloudFront over HTTPS; HTTP is redirected. This is functional,
  not merely hygienic: the dashboard calls `crypto.randomUUID()` while mounting,
  which is undefined outside a secure context, so over plain HTTP the page
  renders blank.
- The load balancer is not a public entrance. Its security group admits only the
  `com.amazonaws.global.cloudfront.origin-facing` prefix list, and the listener's
  default action is a 403 — only requests carrying `X-Origin-Verify` are
  forwarded. Verified negatively: a direct request to the load balancer's own
  DNS name times out, while the CloudFront URL returns 200.
- Tasks accept inbound only from the load balancer's security group, by group
  membership rather than by CIDR. There is no database endpoint in the VPC.
- The data served is public.

**Detection**

VPC flow logs (`infra/terraform/app/audit.tf`) record accepted and rejected
traffic at the VPC edge, which is where the negative reachability claim above
becomes continuously checkable rather than a one-off curl. CloudFront access
logs are **not** enabled — see residual risk.

**Residual risk**

- **The application is unauthenticated.** Anyone with the URL can query it. The
  URL is not secret; it appears in issue comments in this public repository.
- No CloudFront access logs, so there is no record of *who* queried the
  application — only that traffic arrived. Flow logs see CloudFront's edge
  addresses, not the viewer's, and the flood alarm counts requests without
  attributing them. Enabling access logging would close this, at the cost of S3
  storage only.
- **CloudFront → load balancer is cleartext.** CloudFront requires a publicly
  trusted certificate on a custom origin, and a load balancer with no registered
  domain cannot obtain one. `X-Origin-Verify` authenticates the caller; it does
  not keep the hop confidential. Unresolvable until a real domain exists, at
  which point terminating TLS at the load balancer (`enable_cdn = false` with an
  ACM certificate) is the better answer anyway.

---

## 7. Disclosure of protected source data

*Proprietary inputs leak through logs, images, Git, issue text or Terraform
state.*

The deployed snapshot is a derived, publishable artifact; the DVC-backed inputs
it was built from are not.

**Controls**

- The snapshot is built locally and uploaded; the inputs never reach a runner,
  an image layer, or the repository.
- `scripts/scan_secrets.py` gates the repository, and this repository is public,
  so the assumption is that everything tracked in it is disclosed.
- The startup verification reports pass/fail and never echoes expectation SQL or
  aggregate values.
- The build-info route specified in **E2** is required to carry no credentials,
  no expectations SQL and no protected aggregate values.

**Residual risk**

Application logs are not systematically reviewed for content. Nothing prevents a
future code change from logging a query result into CloudWatch, where retention
is 30 days and access is IAM-controlled — contained, but not by design.

---

## Accepted risks, collected

| # | Risk | Why accepted | What would close it |
|---|---|---|---|
| 1 | Application is public and unauthenticated; unbounded LLM spend | Testing posture; users need frictionless access | The open decision on #59 |
| 2 | CloudFront → origin is cleartext | No domain obtainable, so no trusted origin certificate | A registered domain + ACM |
| 3 | Deployment runs from a long-lived IAM user | No CI identity exists yet | CD milestone, issues G and H |
| 4 | Python and Node dependencies unpinned | Predates the image build | CD milestone, issue E1 |
| 5 | No record of which snapshot a live task serves | Identity discarded after verification | CD milestone, issue E2 |
| 6 | No record of who queried the application | CloudFront access logging not enabled | Enable it; costs S3 storage only |
| 7 | Origin shared secret is readable from Terraform state | Inherent to `random_password` | Move to Secrets Manager, or accept |

## Review

This document is only true as of the deployment it was written against. Re-read
it when the authentication decision is made, when a domain is registered, and
when the continuous-delivery milestone replaces the manual deployment path —
each of those invalidates a row in the table above.
