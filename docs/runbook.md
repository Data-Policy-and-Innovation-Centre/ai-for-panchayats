# Deployment and incident runbook

Answers four questions you will have at the wrong time: what is running, how to
get off a bad image, how to get off a bad snapshot, and how to get Terraform
moving again after a lock or a rollback leaves state disagreeing with AWS.

Written for #97. Read the status section before you type anything: this
document describes a delivery path that is currently **half built**, and the
parts that do not exist yet are called out where they would otherwise read as
instructions.

---

## 0. Status of this document — READ FIRST

### Every command below is UNVERIFIED

Acceptance criterion 5 of #97 is *"Every command in it has been run once, by
someone other than its author."* **That criterion is not met.** No command in
this document has been executed by anyone. It was written by reading the
repository, and it was written with no AWS credentials and no access to the
account.

Treat every `aws`, `terraform` and `docker` invocation here as a **hypothesis
derived from the code**, not as a tested procedure. Specifically:

- Resource names, keys, buckets and regions are read out of committed
  Terraform and are cited to `path:line`. Those are reliable.
- The *shape* of each command — flag spellings, query expressions, what the
  output looks like — is not. `--query` expressions in particular have never
  been run against a real response.
- Anything the repository cannot know (the AWS account id, whether a resource
  actually exists in the account, what the console shows) is recorded as an
  **OPEN QUESTION** rather than guessed. There are no invented values in this
  document. If you find one, that is a defect, not a shortcut.

The first person to run these commands in anger should correct them in place
and delete this warning for each section they verified.

### What does not exist yet

#97 depends on #92 (deploy on merge to `main`, roll back on demand) and #93
(prove the deployed app actually works). **Both are OPEN as of this writing,
and neither has landed on any branch in this repository.** Confirmed by
inspection, not by reading the issue text:

- **There is no deploy workflow.** `.github/workflows/` holds
  `pipeline.yml`, `lint.yml`, `data-check.yml`, `secret-scan.yml` and
  `codex-review-gate.yml`, and nothing else. `git ls-tree` over
  `ci/pin-image-inputs`, `fix/rollout-postcondition` and
  `deploy/snapshot-packaging` finds no additional workflow either. Deployment
  today is a human running `terraform apply`, exactly as #92 describes.
- **There is no `production` environment gate.** No workflow in the tree uses
  `environment:` at all.
- **The image tag is not derived from input hashes.** It is
  `<this repo's short HEAD>-<consumer short ref><arch suffix>`
  (`docker/build.sh:70`). Anything that assumes a content hash is describing
  #90's intent, not the build that exists.
- **The ECS deployment circuit breaker is not enabled.**
  `aws_ecs_service.app` (`infra/terraform/app/service.tf:298-349`) declares no
  `deployment_circuit_breaker` block and no `deployment_controller`. Terraform
  does not enable the breaker by default, so **the failure mode in §4 cannot
  currently fire.** §4 is written for the configuration #92 is expected to
  introduce, and is marked as such.
- **There is no build-info route.** Nothing running today reports which
  snapshot a live task is serving — the identity is verified at startup and
  then discarded (`infra/THREAT_MODEL.md:84-88`). §1 works around this by
  reading the startup log line, which is the only surviving evidence.

---

## 1. What commit, image and snapshot are running right now

Three questions, and they chain: the service names a task definition revision,
the revision names an image tag, and the tag names a repo commit whose
committed manifest names the snapshot object version. There is no route that
answers this from the running process; you reconstruct it.

### Fixed names

Everything is prefixed by `var.name`, default `prdw-chatbot`
(`infra/terraform/app/variables.tf:6-10`), in `ap-south-1`
(`infra/terraform/app/variables.tf:1-4`).

| Thing | Name | Source |
|---|---|---|
| ECS cluster | `prdw-chatbot` | `service.tf:69-70` |
| ECS service | `prdw-chatbot` | `service.tf:298-299` |
| Task definition family | `prdw-chatbot` | `service.tf:84-85` |
| ECR repository | `prdw-chatbot` | `service.tf:29-30` |
| Log group | `/ecs/prdw-chatbot` | `service.tf:64-66` |
| Log stream prefix | `chatbot` | `service.tf:177` |
| Snapshot bucket | `dpic-prdw-snapshots` | `infra/terraform/snapshot/variables.tf:7-11` |
| App Terraform state | `s3://dpic-prdw-tfstate/prdw/app/terraform.tfstate` | `infra/terraform/app/versions.tf:16-22` |

### Ask the running tasks, not the PRIMARY deployment

**`PRIMARY` is the deployment Terraform most recently asked for. It is not
necessarily what users are getting**, and during exactly the incident that
brings you here it usually is not. `service.tf:122-132` spells out the case:
with `deployment_minimum_healthy_percent` at 100 the old task keeps serving
while a new revision that can never start sits in `ACTIVE`, and `apply`
reports success. A partial rollout can also serve *both* revisions at once.

So enumerate what is actually running. There may be more than one answer, and
if there is, that is the finding.

```bash
aws ecs list-tasks --cluster prdw-chatbot --service-name prdw-chatbot \
  --desired-status RUNNING --region ap-south-1 --query 'taskArns' --output text
```

```bash
aws ecs describe-tasks --cluster prdw-chatbot --region ap-south-1 \
  --tasks <arn> [<arn> ...] \
  --query 'tasks[].{task:taskArn,td:taskDefinitionArn,ip:attachments[0].details[?name==`privateIPv4Address`].value|[0],started:startedAt}'
```

**Do not read `healthStatus` here — it is `UNKNOWN`.** The task definition
declares no container `healthCheck` (`service.tf:74-181`; the
`start_period_seconds = 420` at line 81 is a local feeding the service's
`health_check_grace_period_seconds` at line 306, not a container probe). ECS
reports a task as `RUNNING` the moment the container starts, which is while
`docker/entrypoint.sh` is still downloading and hashing a ~1 GB snapshot.

**Serving is decided by the load balancer, so ask the load balancer.** A task
receives traffic only once its target passes two checks 30 s apart
(`service.tf:210-217`):

```bash
# The target group is named `prdw-chatbot`, same as everything else
# (`service.tf:199-200`). There is no Terraform output for its ARN, so look it
# up by name rather than by an output that does not exist.
TG=$(aws elbv2 describe-target-groups --names prdw-chatbot --region ap-south-1 \
       --query 'TargetGroups[0].TargetGroupArn' --output text)
aws elbv2 describe-target-health --region ap-south-1 --target-group-arn "$TG" \
  --query 'TargetHealthDescriptions[].{target:Target.Id,port:Target.Port,state:TargetHealth.State,reason:TargetHealth.Reason}'
```

Match `target` against each running task's private IP from the query above.
Only tasks whose target state is `healthy` are serving. A task that is
`RUNNING` with an `initial` or `unhealthy` target is starting up or failing,
and reporting its revision as "what is running" is exactly the mistake this
section exists to prevent.

**Two revisions with `healthy` targets means both are serving simultaneously**;
every step below has to be done for each of them, and a single answer to "what
is running" does not exist until the rollout settles.

Then, per revision:

```bash
aws ecs describe-task-definition --task-definition prdw-chatbot:<N> \
  --region ap-south-1 --query 'taskDefinition.containerDefinitions[0].image'
```

The image is `<account>.dkr.ecr.ap-south-1.amazonaws.com/prdw-chatbot:<tag>`
(`service.tf:153`). **The tag is the answer to "what image".** For what the
tag does and does not prove about a commit, see the caution below.

And per *task* — not "the newest log stream", which may belong to a task that
never became healthy, or to a replacement for one that died:

```
/ecs/prdw-chatbot  →  chatbot/chatbot/<task-id>
```

The stream name ends in the task id from `taskArn`
(`service.tf:174-178` sets the `awslogs-stream-prefix` to `chatbot`), so pick
the stream for each *running* task rather than the most recent one. Read its
first lines: the snapshot identity is printed exactly once, at startup, by
`src/deploy/fetch.py:288` and `scripts/fetch_snapshot.py:64`, in the format
from `src/deploy/fetch.py:80-85`:

```
provisional_full_state_snapshot s3://dpic-prdw-snapshots/duckdb/database_allgps.duckdb?versionId=<V> sha256=<H> bytes=<N> aggregates=verified
```

**`aggregates=SKIPPED` means the aggregate gate did not run.** That is a
benchmarking-only mode (`scripts/fetch_snapshot.py:27-31`) and must never
appear on a production task.

**If the startup lines are gone, read the image instead.** The log group keeps
30 days (`service.tf:66`) and the snapshot identity is printed exactly once, at
startup — so a task that has been up longer than that has no surviving record
of what it fetched. The manifest is baked into the image, which is immutable
(`service.tf:31`), so pull the exact tag and read it:

```bash
docker pull <account>.dkr.ecr.ap-south-1.amazonaws.com/prdw-chatbot:<tag>
docker run --rm --entrypoint cat <...>:<tag> /app/manifest/full_state.json | jq '{key, version_id, sha256}'
```

That answers what the task *was told* to fetch, which is the same thing while
the image is immutable and `entrypoint.sh` refuses to serve a mismatch. It does
not prove the fetch succeeded — but a task that is serving traffic got past
that check by definition.

> UNVERIFIED — no console session and no AWS call informed any of this. The
> `--query` expressions in particular have never been run. The console path is
> ECS → Clusters → `prdw-chatbot` → Services → `prdw-chatbot` → **Tasks** (not
> Deployments), then CloudWatch → Log groups → `/ecs/prdw-chatbot`.
>
> **OPEN QUESTION 8.** The exact log stream name format is inferred from the
> `awslogs-stream-prefix` and the awslogs driver's documented
> `<prefix>/<container>/<task-id>` shape. Confirm it against a real stream.

### Cross-check against the repository

Given a commit you inferred from a tag and confirmed resolves (see the
caution below):

```bash
git -C . show <repo-short-sha>:infra/snapshots/full_state.json | jq '{key, version_id, sha256, byte_size, expectations_version_id, known_exceptions}'
```

This is authoritative for the snapshot, because the manifest is baked into the
image: `docker/build.sh:115` copies `infra/snapshots/full_state.json` into the
build context and `docker/Dockerfile:36` `COPY`s it to
`/app/manifest/full_state.json`, which `docker/entrypoint.sh:5,10` reads at
startup. There is no runtime override — the manifest path is settable via
`SNAPSHOT_MANIFEST`, but `service.tf:158-164` sets no such variable.

Cross-check the two: the `version_id` and `sha256` in the committed manifest
must equal the ones in the startup log line. If they differ, the image was
built from a tree that does not match the commit its tag claims — see the
caution below.

### Caution: the tag does not prove the commit, and the commit does not prove the tree

Two separate reasons the tag is a hint rather than an answer, and the startup
log line is the only real evidence.

**The tag need not contain a commit at all.** `docker/build.sh:70` is
`TAG="${TAG:-...}"` — a supplied `TAG` is used verbatim, and lines 72-81
validate only the architecture suffix. Terraform enforces no commit-shaped
prefix either (`service.tf:133-141` checks the suffix and nothing else). So
splitting a tag on `-` and treating the first segment as a short SHA can name
a commit that does not exist, or worse, one that does and is not the right
one. Confirm any commit you infer this way — `git cat-file -e <sha>` at
minimum — and if it does not resolve, the tag was overridden and the
repository cannot tell you the commit. Take the snapshot identity from the
startup log line instead, and find the manifest by its `version_id` (§3).

**Even a real commit does not prove the tree.** `build.sh:70` derives the
default tag from `git rev-parse --short HEAD` and **does not check the working
tree is clean**. An image built with uncommitted changes to
`infra/snapshots/full_state.json` carries a different snapshot pin under a tag
that claims the committed one. ECR's immutable tags (`service.tf:31`) make the
*second* such push fail loudly; the first succeeds silently.

Both are why the identity check is "read the startup line, then match it
against the committed manifest" rather than "read the tag".

> **OPEN QUESTION 1.** The AWS account id is not in this repository. The ECR
> repository URL is available from the app module as
> `terraform -chdir=infra/terraform/app output ecr_repository_url`
> (`infra/terraform/app/outputs.tf:13-15`), or from the console. Fill it in
> here once known.
>
> **OPEN QUESTION 2.** No console URL is given anywhere in this document
> because none has been observed. Someone with console access should paste the
> deep links for the cluster, the task definition family and the log group.

---

## 2. Rolling back a bad image

### The mechanism you are relying on

`skip_destroy = true` on the task definition (`service.tf:87-102`) keeps
superseded revisions `ACTIVE`. Without it, the provider deregisters the old
revision on every replacement and there is nothing to roll back to — the
comment records that this was found by running the drill and getting
`ClientException: TaskDefinition is inactive` one minute after a deploy.

The ECR lifecycle policy expires **untagged images only**
(`service.tf:39-61`). Tagged images are never removed on a timer, deliberately,
so the previous image is still pullable. Tags are immutable
(`service.tf:31`), so a tag always means one image.

### 2a. Preferred: re-apply with the known-good tag

This is the same operation as a deploy with a different tag, which is why it is
not a separate mechanism. `image_tag` has no default and is validated non-empty
(`variables.tf:36-44`).

> **Every other variable must be passed too, exactly as production was applied.**
> `terraform apply -var image_tag=...` does not "change only the tag" — it
> re-evaluates *every* input, and any variable not supplied falls back to its
> default. **There is no committed `.tfvars` file in `infra/terraform/app/`**,
> so nothing is auto-loaded and whatever production was applied with lives
> outside this repository. A rollback that passes the tag alone would, for
> instance, silently revert an ALB-TLS deployment (`certificate_arn`,
> `public_domain`, `enable_cdn=false`) to the default CloudFront topology —
> turning an image rollback into a topology change, during an incident.
>
> **Before typing apply, run plan and read it.** The plan is the check: if it
> proposes anything beyond a new task definition revision and the service
> pointing at it, the variable set is wrong and applying it will make the
> outage worse.
>
> ```bash
> terraform -chdir=infra/terraform/app plan \
>   -var-file=<the production var file> -var image_tag=<known-good-tag>
> ```
>
> **OPEN QUESTION 9.** Where the production variable set actually lives — a
> var file held outside the repo, `TF_VAR_*` in someone's environment, or a
> series of `-var` flags in a runbook nobody wrote down. Until that is
> recorded, no one can perform this rollback correctly from this document
> alone, and that is the single biggest gap in it. It is also an argument for
> #92: a deploy workflow makes the variable set a committed artifact instead
> of an operator's shell history.

```bash
terraform -chdir=infra/terraform/app apply \
  -var-file=<the production var file> -var image_tag=<known-good-tag>
```

What this actually does: it registers a **new** task definition revision whose
container image is the old one, and points the service at that. It does not
reuse the old revision number. So after rolling back from `:8` to the image in
`:7`, the service runs `:9`. That is expected; do not go looking for `:7` in
the deployments list.

The arch-suffix precondition (`service.tf:133-141`) applies to the rollback tag
too. A tag without `-arm64` fails the plan while `cpu_architecture` is `ARM64`
(`variables.tf:166-180`), before any AWS mutation.

### 2b. Faster: point the service at the previous revision directly

Use this when Terraform is blocked (locked state, no plan role, §5 in progress)
and the site is down now.

```bash
aws ecs list-task-definitions --family-prefix prdw-chatbot --status ACTIVE \
  --sort DESC --region ap-south-1
aws ecs update-service --cluster prdw-chatbot --service prdw-chatbot \
  --task-definition prdw-chatbot:<N> --region ap-south-1
```

**This puts Terraform state out of date on purpose.** The next `terraform plan`
will show `aws_ecs_service.app.task_definition` drifting and will propose to
put the bad revision back. Resolve it the same way as §4 before anyone runs a
routine apply.

### How long to wait

- The health check grace period is **420 s** (`service.tf:81, 306`), sized for
  the ~1 GB download and SHA-256 at startup. A new task is not counted
  unhealthy inside that window, so a rollback does not visibly take effect for
  roughly two to seven minutes.
- Target health: `/health`, 30 s interval, 2 checks to pass, 5 to fail
  (`service.tf:210-217`).
- Draining the old task: 30 s (`service.tf:208`).

### You do not need a CloudFront invalidation

The default cache behaviour uses `Managed-CachingDisabled`
(`cdn.tf:34-36, 108`), so nothing dynamic — `index.html` included — is cached.
The only cached behaviour is `/assets/*` (`cdn.tf:127-135`), and Vite
fingerprints those filenames by content, so a rolled-back bundle is a different
URL (`cdn.tf:125-126`). Rolling back the image is sufficient.

---

## 3. Rolling back a bad snapshot — this **is** an image rollback

There is no separate snapshot rollback. Follow §2.

### Why

The manifest that names the snapshot object version is a file in this
repository, and it is `COPY`d into the image at build time:

- `docker/build.sh:115` — `cp "$REPO_ROOT/infra/snapshots/full_state.json" "$CTX/infra/snapshots/"`
- `docker/Dockerfile:36` — `COPY infra/snapshots/full_state.json /app/manifest/full_state.json`
- `docker/entrypoint.sh:5,10` — reads that path and refuses to serve if the
  artifact does not match it

Nothing in `service.tf:158-164` passes a snapshot identity as an environment
variable. The task definition names an image; the image names a snapshot. **A
running task's snapshot cannot be changed without changing its image.**

This is by construction, not by accident. `src/deploy/manifest.py:10-12`: *"A
manifest describes exactly one immutable object version. Republishing means
writing a new manifest, never editing this one, so a rollback is a manifest
revert rather than a mutation of an S3 object."* And
`infra/snapshots/README.md:46-51` says the same for the object side.

### Consequences during an incident

1. **Reverting the manifest in Git changes nothing until an image is built and
   pushed.** A revert commit is a prerequisite, not the fix.
2. **Deleting or overwriting the bad object in S3 does not help and makes
   things worse.** The old object version is what the *old* manifest pins.
   Snapshot objects are never mutated in place (`snapshot/main.tf:8-10`); a new
   artifact is a new version.
3. **The fastest path is almost always to redeploy an existing image tag whose
   baked-in manifest pins the good snapshot** — i.e. §2a with the tag from
   before the snapshot change. Building a fresh image takes a consumer clone
   and an `npm ci` (`build.sh:87-107`) and is minutes you may not have.

### Finding the last good manifest

```bash
git log --oneline -- infra/snapshots/full_state.json
git show <commit>:infra/snapshots/full_state.json | jq '{version_id, sha256}'
```

Then find the image tag built from that commit: for a tag built the default
way, the first segment is that commit's short SHA (`build.sh:70`), so list ECR
and match the prefix. **This fails silently for any image built with an
explicit `TAG` override**, which nothing enforces the shape of — see the
caution in §1. If no tag matches, do not conclude the image is gone; match on
the snapshot instead, by starting each candidate tag and reading its startup
line, or by `docker pull`ing it and reading `/app/manifest/full_state.json`
directly.

> **OPEN QUESTION 3.** There is no recorded mapping from image tag to
> deployment time. The only sources would be ECR push timestamps and, once #92
> lands, the GitHub Deployment records it is specified to create. Until then,
> "which tag was running last Tuesday" cannot be answered from this repository.

### The rollback window is 180 days

Superseded snapshot object versions expire after
`noncurrent_version_retention_days`, default **180**
(`infra/terraform/snapshot/variables.tf:19-27`). Past that, an old manifest
pins an object version that no longer exists and `fetch_snapshot` fails at
`head_object` with `SnapshotUnavailableError` (`src/deploy/fetch.py:98-107`) —
the task never becomes healthy, and there is no way back to that snapshot.

Also load-bearing: the KMS key has `prevent_destroy` (`snapshot/main.tf:12-24`)
precisely because destroying it would leave the whole 180-day window
undecryptable while the objects survive.

### What a bad snapshot looks like before it reaches you

`fetch_snapshot` is fail-closed and refuses to publish an artifact that does
not match: byte count and object version at `head_object`
(`src/deploy/fetch.py:108-119`), byte count and SHA-256 after download
(`fetch.py:259-269`), relation inventory and the private aggregate gate
(`fetch.py:271`). A "bad snapshot" that reaches production is therefore one
that is *internally consistent but wrong* — the gate proves identity, not
correctness. The manifest's `known_exceptions` list
(`infra/snapshots/full_state.json`) is where the known-wrong parts are
recorded.

---

## 4. When the circuit breaker rolls back

> **This section is contingent.** As established in §0, the service declares no
> `deployment_circuit_breaker` (`service.tf:298-349`), so the breaker is off
> and cannot roll anything back today. Written for the configuration #92 is
> expected to introduce. Verify the block exists before relying on any of it.

### The problem

The breaker acts on the ECS service directly. Terraform is not involved and
does not learn about it. So after a breaker rollback:

- **AWS**: the service's `taskDefinition` is the *previous* revision, and the
  failed deployment is marked `ROLLBACK_COMPLETED` (or similar) rather than
  `COMPLETED`.
- **Terraform state**: `aws_ecs_service.app.task_definition` still records the
  revision the apply registered — the bad one. The apply may well have exited
  0, because Terraform's `apply` returns once the service is updated, not once
  it is stable (there is no `wait_for_steady_state` in `service.tf`).

The two now disagree, and the disagreement is dangerous in a specific way:
**the next routine `terraform apply` will read the drift and put the bad
revision back.** Terraform sees the service pointing at the old revision,
compares it to its desired state, and re-deploys the thing the breaker just
rejected. That includes an apply someone runs for an unrelated change.

### What to do

1. **Establish that the breaker is what happened**, not a manual change:

   ```bash
   aws ecs describe-services --cluster prdw-chatbot --services prdw-chatbot \
     --region ap-south-1 \
     --query 'services[0].{taskDef:taskDefinition,deployments:deployments[].{id:id,status:status,rollout:rolloutState,reason:rolloutStateReason,td:taskDefinition,running:runningCount}}'
   ```

   `rolloutStateReason` carries the breaker's own message when it fires.

2. **Do not run `terraform apply` with the bad tag still in play.** Not even
   for something else. There is no partial apply here: the service resource is
   a single resource and it will be reconciled.

3. **Make Terraform's desired state match reality by choosing it
   deliberately** — apply with the *known-good* image tag:

   ```bash
   terraform -chdir=infra/terraform/app plan \
     -var-file=<the production var file> -var image_tag=<known-good-tag>
   terraform -chdir=infra/terraform/app apply \
     -var-file=<the production var file> -var image_tag=<known-good-tag>
   ```

   The same warning as §2a applies and matters more here, because you are
   already mid-incident: omitting the production variables re-evaluates every
   input from its default. Read the plan before applying.

   This registers a new revision with the good image and points the service at
   it. State and service agree again, and they agree on something you chose.
   This is preferred over `terraform refresh` or a state edit: refreshing
   records the rolled-back revision as desired state without registering
   anything, which leaves the *next* deploy computing its diff from a revision
   nobody deliberately picked.

4. **Read why the deployment failed before deploying anything else**, and read
   it in *two* places, because the two common causes surface differently.

   **Snapshot failures reach CloudWatch.** `fetch_snapshot` runs inside the
   container, so its `SnapshotError` subclasses (`src/deploy/errors.py`, raised
   from `src/deploy/fetch.py`) appear in `/ecs/prdw-chatbot`.

   **Secret-injection failures do not.** `OPENAI_API_KEY` is injected by the
   ECS agent from Secrets Manager (`service.tf:168-169`) *before* the container
   process starts, so a failure there produces no application log line at all —
   looking only at CloudWatch hides the cause entirely. The header comment at
   `service.tf:3-7` names this as a mode the service can sit in without
   stabilizing. Enumerate stopped tasks and read the reasons:

   ```bash
   aws ecs list-tasks --cluster prdw-chatbot --service-name prdw-chatbot \
     --desired-status STOPPED --region ap-south-1 --query 'taskArns' --output text
   aws ecs describe-tasks --cluster prdw-chatbot --region ap-south-1 --tasks <arn> ... \
     --query 'tasks[].{stopped:stoppedReason,code:stopCode,containers:containers[].{name:name,reason:reason,exit:exitCode}}'
   ```

   A `ResourceInitializationError` mentioning `secretsmanager` is this case. An
   empty CloudWatch stream for a task that stopped is itself the signal to look
   here.

5. **Check the alarm state.** `prdw-chatbot-no-running-tasks`
   (`alarms.tf:78-98`) needs five consecutive minutes at zero running tasks, so
   a breaker rollback that restored the old task **will not have paged anyone**.
   Silence is not evidence.

> **OPEN QUESTION 4.** The exact `rolloutState` value ECS reports after a
> breaker rollback, and whether Terraform's `apply` exits 0 or errors in that
> case, have not been observed. #92's acceptance criteria call for a
> demonstrated rollback; record the real strings here when that drill runs.

---

## 5. Terraform state-lock recovery

### How locking works here

Both modules use S3 native locking — `use_lockfile = true`
(`infra/terraform/app/versions.tf:21`,
`infra/terraform/snapshot/versions.tf:22`). **There is no DynamoDB table**;
locking is done with S3 conditional writes and needs none on Terraform >= 1.10
(`infra/terraform/snapshot/versions.tf:11`,
`infra/terraform/bootstrap_state_bucket.sh:5-6`). `required_version = ">= 1.10"`
(`versions.tf:2`) exists for this.

The consequence #92 names: a cancelled or failed apply can leave the lock
object behind, and every subsequent plan or apply then fails with
`Error acquiring the state lock`.

### Recovery

1. **Confirm nothing is actually running.** This is the whole risk: force
   unlocking a live apply lets a second apply write over the first, and the
   result is state that describes neither. Check for a running deploy, ask in
   the channel, and look at the lock's own metadata — the error message names
   the operation, who took it, and when.

2. **Read the lock ID from the error.** Terraform prints it as `ID:` in the
   `Error acquiring the state lock` block. Use that value verbatim; do not
   guess it.

3. **Unlock the module that is locked** — they have separate state keys
   (`prdw/app/terraform.tfstate` vs `prdw/snapshot/terraform.tfstate`) and
   therefore separate locks:

   ```bash
   terraform -chdir=infra/terraform/app force-unlock <LOCK_ID>
   ```

4. **Re-plan before re-applying.** A cancelled apply may have created real
   resources that state does not record. The plan after an unlock is not a
   formality.

> **OPEN QUESTION 5.** With `use_lockfile`, Terraform is documented to write
> the lock as an S3 object beside the state — expected to be
> `s3://dpic-prdw-tfstate/prdw/app/terraform.tfstate.tflock`. **This exact key
> has not been observed.** Confirm it before relying on
> `aws s3api head-object` / `delete-object` against it as a fallback for
> `force-unlock`, and record the real key here. Prefer `force-unlock`
> regardless; deleting the object by hand bypasses Terraform's own checks.
>
> **OPEN QUESTION 6.** Whether an operator's role is permitted `s3:DeleteObject`
> on the state bucket is not determinable from this repository — #89's
> plan/apply roles are not in the tree.

### If `init` itself fails against the wrong bucket

The backend blocks hard-code `bucket` and `region`, and a backend block cannot
read variables (`snapshot/versions.tf:12-16`). If someone bootstrapped with a
`TF_STATE_BUCKET` override, `init` silently targets the default bucket instead.
`infra/terraform/bootstrap_state_bucket.sh:221-245` prints the exact
`-reconfigure -backend-config=...` commands for that case, and warns that the
app module additionally reads the snapshot module's outputs through
`data.terraform_remote_state` (`infra/terraform/app/iam.tf:9-17`), so the
override has to be passed again as `-var snapshot_state_bucket=...`
`-var snapshot_state_region=...` or the app module reads stale outputs from the
default bucket.

---

## 6. Things that will bite you, collected

- **A wrong architecture is quiet.** With minimum healthy percent at 100 the
  old task keeps serving, the new one can never start, the
  `no-running-tasks` alarm stays green because a task *is* running, and
  `apply` reports success (`service.tf:122-132`).
- **`aggregates=SKIPPED` in the startup log** means the private aggregate gate
  did not run. Never acceptable in production
  (`scripts/fetch_snapshot.py:27-31`, `src/deploy/fetch.py:80-85`).
- **Staleness is silent.** A months-old snapshot passes every health check,
  because "old" is not "unhealthy" (`infra/THREAT_MODEL.md:93-95`).
- **The basic-auth password is in Terraform state, not in the repo.** Read it
  with `terraform -chdir=infra/terraform/app output -raw basic_auth_password`;
  rotate with `apply -replace='random_password.basic_auth[0]'` — quoted, or zsh
  treats the index as a glob (`outputs.tf:54-58`).
- **Basic auth silently requires the CDN.** `local.basic_auth_enabled` is
  `var.enable_basic_auth && var.enable_cdn` (`auth.tf:32`); a precondition
  turns the combination into an error rather than an unprotected deployment
  (`auth.tf:29-31, 37-44`).
- **`terraform destroy` is not a rollback.** `prevent_destroy` is set on the
  snapshot bucket and the KMS key (`snapshot/main.tf:12-24, 31-39`) for exactly
  this reason.

---

## Open questions, collected

1. The AWS account id, and therefore the full ECR repository URL. (§1)
2. Console deep links for the cluster, task definition family and log group;
   no console session informed this document. (§1)
3. Any mapping from image tag to deployment time. None exists in the
   repository today. (§3)
4. The `rolloutState` / `rolloutStateReason` strings ECS reports after a
   circuit-breaker rollback, and whether `terraform apply` exits 0 in that
   case. (§4)
5. The exact S3 key of the `use_lockfile` lock object. (§5)
6. Whether an operator role may delete objects in the state bucket. (§5)
7. **Whether the circuit breaker will be enabled at all**, and with what
   `rollback` setting. §4 is unusable until #92 answers this. (§0, §4)
8. The exact CloudWatch log stream name format, inferred from the
   `awslogs-stream-prefix` and the awslogs driver's documented
   `<prefix>/<container>/<task-id>` shape. (§1)
9. **Where the production Terraform variable set lives.** No `.tfvars` is
   committed, so nothing is auto-loaded and a rollback that passes only
   `-var image_tag` reverts every other input to its default. Nobody can
   perform §2a correctly from this document until this is recorded. (§2, §4)
