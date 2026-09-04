# Threat model: DuckDB-backed Odisha_PRDW chatbot on AWS

Scope: the deployment described by `infra/terraform/snapshot` (the artifact
store), `infra/terraform/app` (the runtime), `docker/` (the image) and
`src/deploy/` (the startup verification). Written for #59; reviewed against
the deployment running as of this commit.

Each threat below lists the controls that exist, what would detect the attempt,
and the residual risk that is knowingly accepted. Residual risks are collected
again at the end so they can be read without the rest.

The single most important thing to know before reading further: the application
is reachable by **anyone holding one shared password**, checked at the CloudFront
edge. Every "who could do this" below answers *anyone who has that credential*
unless it says otherwise. The credential is shared, so it identifies the pilot
group as a whole and never an individual.

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
- **The OpenAI account holds a $10 balance with auto-reload off.** This is the
  only control in this document that bounds the worst case absolutely rather
  than reporting on it: when the balance is gone the spending stops, whatever
  else has failed.
- HTTP Basic authentication runs as a CloudFront viewer-request function, so an
  unauthenticated request is rejected at the edge before the cache lookup and
  before the origin. Measured: an unauthenticated `POST /query` returns 401 in
  63 ms without reaching the load balancer, against 5.8 s for the authenticated
  one that actually answers.

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

Authentication removes the anonymous internet from this threat; it does not
remove the threat. There is still no rate limit, no WAF and no per-client quota,
so anyone **inside** the pilot — or anyone they pass the password to — can issue
requests as fast as one task will serve them, and every one spends OpenAI credit.
The flood alarm says it is happening; it does not stop it, and it fires after the
requests have been paid for.

A shared credential also cannot be revoked for one person. Rotating it locks out
everyone and requires redistributing a new password, which is the honest cost of
choosing one password over per-person identity.

What bounds the damage is the $10 balance with auto-reload off, not anything in
this account.

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
- Every input the image is built from is pinned (#84): all 32 Python packages
  are `==` including transitives, the base image is pinned by manifest digest
  rather than the moving `python:3.13-slim` tag, and the Node major used for
  the host-side dashboard build is recorded in `docker/.node-version` and
  enforced by `build.sh`. The image installs no apt packages at all: `curl`
  was the only one, it was unversioned, and the healthcheck now uses `urllib`
  from the standard library instead.
- Demonstrated rather than asserted, with the limit stated: two independent
  builds of the same commit produced byte-identical image IDs. That was
  measured on the image as it stood BEFORE the apt layer was removed, and the
  builds ran minutes apart -- so it shows the build is deterministic across
  invocations, not that it is stable across a Debian or PyPI change weeks
  later. Dropping the apt layer removes the input that made the second claim
  indefensible; it has not itself been re-measured over time.

**Residual risk**

- The pins are enforced only where `build.sh` runs. Nothing stops someone
  building the image by invoking `docker build` directly, which skips the Node
  gate entirely. Moving the build into CI (#90) is what makes the enforcement
  unavoidable rather than conventional.
- Only the Node MAJOR is enforced, and the npm version is printed rather than
  checked. Both participate in the host-side Vite build, so two builders on
  different 24.x patches remain a possible source of bundle drift. Enforcing
  exact versions locally would reject every developer whose toolchain differs
  by a patch; the place to pin exactly and for free is the CI build, where one
  runner controls the toolchain.
- The base image digest must now be bumped by hand, so a security patch in
  Debian or CPython no longer arrives on the next rebuild. That is the intended
  trade for reproducibility, but it is a standing obligation rather than a
  solved problem.
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
  DNS name times out, while the CloudFront URL answers — 200 with the shared
  credential, 401 without it.
- Tasks accept inbound only from the load balancer's security group, by group
  membership rather than by CIDR. There is no database endpoint in the VPC.
- Requests must carry the shared Basic credential, checked at the edge on
  **both** cache behaviors — the default one and `/assets/*`. Gating only the
  default would have left the ~1 MB JS bundle open to exactly the crawler this
  is meant to stop.
- The data served is public, so the credential protects the budget behind the
  application rather than the answers it gives.

**Detection**

VPC flow logs (`infra/terraform/app/audit.tf`) record accepted and rejected
traffic at the VPC edge, which is where the negative reachability claim above
becomes continuously checkable rather than a one-off curl. CloudFront access
logs are **not** enabled — see residual risk.

**Residual risk**

- **One shared password is not identity.** It cannot say which tester asked
  what, cannot be revoked for one person, and travels onward as easily as any
  other password. It is sized for a bounded pilot with a known group. Per-person
  auth means CloudFront signed URLs or an OIDC flow at the edge; both are
  recorded as options on #59.
- The password is readable by anyone with `cloudfront:GetFunction` in this
  account — it is compiled into the edge function body, because CloudFront
  Functions have no secret store to read at request time. That is a wider
  audience than the Terraform state it also sits in, and `GetFunction` is part
  of the AWS-managed `ReadOnlyAccess` policy.
- The URL itself is not secret and appears in issue comments in this public
  repository. That is now a non-issue rather than the exposure it was.
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
| 1 | One shared password, so no per-person identity or revocation | Sized for a bounded pilot with a known group | Signed URLs or edge OIDC |
| 2 | CloudFront → origin is cleartext | No domain obtainable, so no trusted origin certificate | A registered domain + ACM |
| 3 | Deployment runs from a long-lived IAM user | No CI identity exists yet | CD milestone, issues G and H |
| 4 | Base-image patches now require a manual digest bump | The price of a reproducible build | Automated base-bump PRs |
| 5 | No record of which snapshot a live task serves | Identity discarded after verification | CD milestone, issue E2 |
| 6 | No record of who queried the application | Access logging off, and a shared credential could not attribute it anyway | Access logging plus per-person credentials |
| 7 | Origin shared secret is readable from Terraform state | Inherent to `random_password` | Move to Secrets Manager, or accept |

## Review

This document is only true as of the deployment it was written against. Re-read
it when the authentication decision is made, when a domain is registered, and
when the continuous-delivery milestone replaces the manual deployment path —
each of those invalidates a row in the table above.
