#!/bin/bash
set -euo pipefail

echo "=== DPIC Workspace Setup ==="

echo "[1/6] Checking prerequisites..."
command -v git    >/dev/null || (echo "Install Git: https://git-scm.com/downloads" && exit 1)
command -v uv     >/dev/null || (echo "Install uv: https://docs.astral.sh/uv/" && exit 1)
command -v rclone >/dev/null || (echo "Install rclone: https://rclone.org/install/" && exit 1)
command -v aws    >/dev/null || (echo "Install AWS CLI" && exit 1)

echo "[2/6] Initializing Git repository..."
if [ ! -e .git ]; then
    git init
fi

echo "[3/6] Setting up Python environment..."
uv sync

echo "[4/6] Configuring DVC credentials..."
source .venv/bin/activate
dvc remote modify --local s3remote profile default

echo "[5/6] Authenticating with Box (browser will open)..."
rclone config reconnect box:

echo "[6/6] Pulling latest data..."
make pull

nbstripout --install

echo ""
echo "=== Setup complete. Run 'make help' to see available commands. ==="
