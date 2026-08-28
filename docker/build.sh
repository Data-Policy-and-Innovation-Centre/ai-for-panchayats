#!/usr/bin/env bash
# Assemble the build context and build the chatbot image.
#
# The consumer application is NOT vendored into this repository. It is cloned
# at a pinned commit into a scratch directory at build time, so this repo never
# carries a copy of someone else's source that can silently drift.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONSUMER_REPO="${CONSUMER_REPO:-https://github.com/Data-Policy-and-Innovation-Centre/Odisha_PRDW.git}"
CONSUMER_REF="${CONSUMER_REF:?set CONSUMER_REF to the consumer commit to build}"
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

TAG="${TAG:-$(git -C "$REPO_ROOT" rev-parse --short HEAD)-${CONSUMER_REF:0:7}${ARCH_SUFFIX}}"

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

echo "[build] cloning consumer at $CONSUMER_REF"
git clone --quiet "$CONSUMER_REPO" "$CTX/consumer"
git -C "$CTX/consumer" checkout --quiet "$CONSUMER_REF"

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
