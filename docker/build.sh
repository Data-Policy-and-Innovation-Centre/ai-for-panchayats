#!/usr/bin/env bash
# Assemble the build context and build the chatbot image.
#
# The consumer application is NOT vendored into this repository. It is cloned
# at a pinned commit into a scratch directory at build time, so this repo never
# carries a copy of someone else's source that can silently drift.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- consumer pin (#83) -----------------------------------------------------
# Which commit of the consumer we build is a fact about this repository, so it
# lives in git rather than only inside an ECR tag string. This block runs
# FIRST, above the tool checks, because it does no work: it reads a file and
# matches a regex. Validating the ref before discovering there is no docker
# means `CONSUMER_REF=master` is reported as the ref problem it is, on any
# machine, instead of as a missing prerequisite.
PIN_FILE="${PIN_FILE:-$REPO_ROOT/infra/consumer/pin.json}"

# Recorded before the pin is consulted, so the error below names the thing the
# caller actually has to change. The pin branch is entered when EITHER value is
# missing, so deriving the source from that branch would blame the pin file for
# an env var the caller set.
if [[ -n "${CONSUMER_REF:-}" ]]; then
  CONSUMER_REF_SOURCE="the CONSUMER_REF environment variable"
else
  CONSUMER_REF_SOURCE="$PIN_FILE"
fi

if [[ -z "${CONSUMER_REF:-}" || -z "${CONSUMER_REPO:-}" ]]; then
  [[ -f "$PIN_FILE" ]] || {
    echo "[build] no CONSUMER_REF set and no pin at $PIN_FILE" >&2
    echo "[build] Set CONSUMER_REF to a 40-hex commit, or restore the pin." >&2
    exit 2; }
  command -v python3 >/dev/null 2>&1 || {
    echo "[build] python3 is required to read $PIN_FILE" >&2; exit 2; }
  # Read both fields in one pass. A pin missing either key, or holding a
  # non-string, is a broken pin and must not fall back to a default.
  PIN_VALUES="$(python3 -c '
import json, sys
pin = json.load(open(sys.argv[1]))
for key in ("repo", "commit"):
    value = pin.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"pin is missing a usable {key!r}")
    print(value.strip())
' "$PIN_FILE")" || {
    echo "[build] $PIN_FILE is not a usable consumer pin" >&2; exit 2; }
  CONSUMER_REPO="${CONSUMER_REPO:-$(printf '%s\n' "$PIN_VALUES" | sed -n 1p)}"
  CONSUMER_REF="${CONSUMER_REF:-$(printf '%s\n' "$PIN_VALUES" | sed -n 2p)}"
fi

# A full 40-hex SHA, lowercase, and nothing else. The tag is built with the
# string slice ${CONSUMER_REF:0:7}, so a branch name does not fail -- it
# produces a tag ending "-master" naming an image nobody can trace back to a
# commit. Git writes SHAs lowercase, so requiring lowercase costs nothing and
# keeps the pin comparison below a plain string equality.
if [[ ! "$CONSUMER_REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[build] CONSUMER_REF must be a full 40-character lowercase hex commit SHA." >&2
  echo "[build] got '$CONSUMER_REF' (${#CONSUMER_REF} chars), from $CONSUMER_REF_SOURCE." >&2
  echo "[build] A branch or short SHA cannot be traced back from the image tag." >&2
  exit 2
fi

# The seam the tests use: everything above is pure validation, so resolving
# the ref is observable without a clone, a network call or a docker daemon.
if [[ "${1:-}" == "--print-consumer-ref" ]]; then
  echo "$CONSUMER_REF"
  exit 0
fi

# Prerequisites, checked before anything else happens (#84). The clone and
# `npm ci` take minutes; discovering there is no docker afterwards wastes all
# of it. This sits above every other line that does work -- in particular above
# the TAG default, which shells out to git, so a missing git is reported by
# name here instead of dying at `command not found` two lines later.
for tool in git node npm docker; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "[build] required tool not found: $tool" >&2; exit 2; }
done

# The binary existing is not the same as the daemon running, and Docker Desktop
# installed-but-stopped is the common case. Without this the build still burns
# the clone and `npm ci` before failing at the first `docker build`.
docker info >/dev/null 2>&1 || {
  echo "[build] docker is installed but the daemon is not reachable; start Docker and retry." >&2; exit 2; }

# The frontend is built on the HOST, outside Docker, so the Node that happens
# to be installed is a build input as real as any pinned package -- and the
# consumer only declares `engines: {node: ">=20.0.0"}`, a floor, which permits
# any future major. docker/.node-version records the major that produced the
# image now in production.
NODE_VERSION_FILE="$REPO_ROOT/docker/.node-version"
[[ -f "$NODE_VERSION_FILE" ]] || { echo "[build] missing $NODE_VERSION_FILE" >&2; exit 2; }
# Compare majors on both sides. .node-version conventionally holds a full
# version ("24.9.1") as often as a major ("24"), and a whole-string compare
# against a full version would reject every Node that could ever be installed.
WANT_NODE="$(tr -d '[:space:]' < "$NODE_VERSION_FILE")"
WANT_NODE_MAJOR="${WANT_NODE#v}"; WANT_NODE_MAJOR="${WANT_NODE_MAJOR%%.*}"
HAVE_NODE="$(node --version)"            # e.g. v24.1.0
HAVE_NODE_MAJOR="${HAVE_NODE#v}"; HAVE_NODE_MAJOR="${HAVE_NODE_MAJOR%%.*}"
if [[ "$HAVE_NODE_MAJOR" != "$WANT_NODE_MAJOR" ]]; then
  echo "[build] node major $HAVE_NODE_MAJOR does not match the pinned major $WANT_NODE_MAJOR ($HAVE_NODE)." >&2
  echo "[build] The dashboard bundle is a build output; a different Node major can change it." >&2
  echo "[build] Install Node $WANT_NODE_MAJOR (nvm use $WANT_NODE_MAJOR / fnm use $WANT_NODE_MAJOR), or change" >&2
  echo "[build] $NODE_VERSION_FILE deliberately and rebuild the deployed image." >&2
  exit 2
fi
echo "[build] node $HAVE_NODE (major $WANT_NODE_MAJOR pinned), npm $(npm --version)"
IMAGE="${IMAGE:-odisha-prdw-chatbot}"
# ARM64 by default, because that is what the service runs: variables.tf
# defaults cpu_architecture to ARM64 (Graviton is 44% cheaper per vCPU-hour in
# ap-south-1). Defaulting to amd64 here meant a caller who omitted PLATFORM got
# an x86 image, and the only thing standing between that and a dead deployment
# was a tag SUFFIX -- which service.tf checks as a string. Terraform accepts
# the revision, then no task can start.
PLATFORM="${PLATFORM:-linux/arm64}"

# The suffix is derived from the platform, never supplied independently, so the
# two cannot disagree. This is the other half of the precondition in
# service.tf: that one refuses a tag whose suffix contradicts the task
# architecture, this one refuses to MINT such a tag.
case "$PLATFORM" in
  linux/arm64) ARCH_SUFFIX="-arm64" ;;
  linux/amd64) ARCH_SUFFIX="" ;;
  *) echo "[build] unsupported PLATFORM '$PLATFORM' (expected linux/arm64 or linux/amd64)" >&2; exit 2 ;;
esac

TAG="${TAG:-$(git -C "$REPO_ROOT" rev-parse --short=7 HEAD)-${CONSUMER_REF:0:7}${ARCH_SUFFIX}}"

# An explicitly supplied TAG still has to agree with the platform being built.
if [[ "$ARCH_SUFFIX" == "-arm64" && "$TAG" != *-arm64 ]]; then
  echo "[build] PLATFORM=$PLATFORM builds an arm64 image, but TAG='$TAG' does not end in -arm64." >&2
  echo "[build] service.tf requires the suffix to match the task architecture." >&2
  exit 2
fi
if [[ "$ARCH_SUFFIX" == "" && "$TAG" == *-arm64 ]]; then
  echo "[build] TAG='$TAG' claims arm64 but PLATFORM=$PLATFORM builds x86_64." >&2
  echo "[build] Terraform would accept this tag and then no task could start." >&2
  exit 2
fi

CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT

echo "[build] cloning consumer at $CONSUMER_REF (from $CONSUMER_REF_SOURCE)"
git clone --quiet "$CONSUMER_REPO" "$CTX/consumer"
git -C "$CTX/consumer" checkout --quiet "$CONSUMER_REF"

# What the clone resolved to, not what we asked for. `checkout <sha>` of a
# 40-hex ref should be an identity, so a mismatch means the ref named
# something other than a commit -- a tag or branch that happens to be 40 hex
# characters -- and the image would carry a provenance label that is a lie.
RESOLVED="$(git -C "$CTX/consumer" rev-parse HEAD)"
if [[ "$RESOLVED" != "$CONSUMER_REF" ]]; then
  echo "[build] $CONSUMER_REF_SOURCE names $CONSUMER_REF but the clone resolved to $RESOLVED." >&2
  exit 2
fi

# The dashboard reads `import.meta.env.VITE_API_BASE_URL || "http://localhost:8000"`.
# An empty string is FALSY, so building with "" does not select the current
# origin -- it ships the localhost fallback, and every user's browser then
# posts queries to their own machine. "." is the shortest truthy value that
# still resolves against the page: "." + "/query" is "./query", which the
# browser resolves to /query on the origin it loaded from. That keeps one
# image valid behind the load balancer, behind CloudFront, and behind whatever
# real domain replaces them, with no rebuild.
#
# Deliberately NOT overridable from the environment. The consumer repo's own
# .env.example tells developers to export VITE_API_BASE_URL=http://localhost:8000,
# so honouring an inherited value would let a developer's shell rebake exactly
# the bug this line fixes -- an image that sends every user's queries to their
# own machine.
( cd "$CTX/consumer/frontend/ab-dashboard-main"
  npm ci --no-audit --no-fund --silent
  VITE_API_BASE_URL="." npm run build --silent )

mkdir -p "$CTX/vendor"
cp -R "$CTX/consumer/Ask"                                   "$CTX/vendor/Ask"
cp -R "$CTX/consumer/frontend/ab-dashboard-main/dist"        "$CTX/vendor/static"
mkdir -p "$CTX/src" "$CTX/scripts" "$CTX/infra/snapshots" "$CTX/docker"
cp -R "$REPO_ROOT/src/deploy"                                "$CTX/src/deploy"
cp    "$REPO_ROOT/scripts/fetch_snapshot.py"                 "$CTX/scripts/"
cp    "$REPO_ROOT/infra/snapshots/full_state.json"           "$CTX/infra/snapshots/"
cp    "$REPO_ROOT"/docker/{Dockerfile,requirements.txt,serve.py,entrypoint.sh} "$CTX/docker/"
touch "$CTX/scripts/__init__.py" "$CTX/src/__init__.py"

# Attestations must be off. With them on, buildx produces a manifest LIST
# whose children include an attestation manifest, and pushing that to a
# repository with immutable tags races: the attestation can claim the tag
# first, after which the real image can never be pushed under it and ECS
# pulls an artifact that is not a runnable image. Observed exactly once,
# on tag 2b034fe-594e316.
#
# --provenance/--sbom are buildx-only flags, so they are passed only when
# buildx is present. The legacy builder cannot emit attestations at all, so
# omitting them there is not a downgrade -- and passing them would abort the
# build with "unknown flag" after the npm build has already run.
if docker buildx version >/dev/null 2>&1; then
  echo "[build] docker buildx build --platform $PLATFORM -t $IMAGE:$TAG"
  docker buildx build --provenance=false --sbom=false --load \
    --platform "$PLATFORM" -f "$CTX/docker/Dockerfile" -t "$IMAGE:$TAG" "$CTX"
else
  echo "[build] docker build --platform $PLATFORM -t $IMAGE:$TAG (no buildx)"
  docker build \
    --platform "$PLATFORM" -f "$CTX/docker/Dockerfile" -t "$IMAGE:$TAG" "$CTX"
fi
# Assert what was actually built, not what was asked for. The checks above
# constrain the tag; this one constrains the artifact -- a buildx builder
# without the requested emulation can quietly produce the host architecture,
# and the tag would still read correctly.
built="$(docker image inspect --format '{{.Architecture}}' "$IMAGE:$TAG")"
want="${PLATFORM#linux/}"
if [[ "$built" != "$want" ]]; then
  echo "[build] built image is $built but PLATFORM=$PLATFORM was requested." >&2
  echo "[build] Pushing this under $TAG would register a task definition that cannot start." >&2
  exit 1
fi
echo "[build] verified image architecture: $built"

echo "$IMAGE:$TAG"
