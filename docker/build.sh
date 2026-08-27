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

echo "[build] building the dashboard for same-origin API calls"
( cd "$CTX/consumer/frontend/ab-dashboard-main"
  npm ci --no-audit --no-fund --silent
  VITE_API_BASE_URL="" npm run build --silent )

mkdir -p "$CTX/vendor"
cp -R "$CTX/consumer/Ask"                                   "$CTX/vendor/Ask"
cp -R "$CTX/consumer/frontend/ab-dashboard-main/dist"        "$CTX/vendor/static"
mkdir -p "$CTX/src" "$CTX/scripts" "$CTX/infra/snapshots" "$CTX/docker"
cp -R "$REPO_ROOT/src/deploy"                                "$CTX/src/deploy"
cp    "$REPO_ROOT/scripts/fetch_snapshot.py"                 "$CTX/scripts/"
cp    "$REPO_ROOT/infra/snapshots/full_state.json"           "$CTX/infra/snapshots/"
cp    "$REPO_ROOT"/docker/{Dockerfile,requirements.txt,serve.py,entrypoint.sh} "$CTX/docker/"
touch "$CTX/scripts/__init__.py" "$CTX/src/__init__.py"

echo "[build] docker build --platform $PLATFORM -t $IMAGE:$TAG"
docker build --platform "$PLATFORM" -f "$CTX/docker/Dockerfile" -t "$IMAGE:$TAG" "$CTX"
echo "$IMAGE:$TAG"
