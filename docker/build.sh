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
TAG="${TAG:-$(git -C "$REPO_ROOT" rev-parse --short HEAD)-${CONSUMER_REF:0:7}}"
PLATFORM="${PLATFORM:-linux/amd64}"   # Fargate runs x86_64 unless configured otherwise

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
echo "$IMAGE:$TAG"
