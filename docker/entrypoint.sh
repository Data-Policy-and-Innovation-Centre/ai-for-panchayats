#!/usr/bin/env bash
# Prove the database before serving anything.
set -euo pipefail

MANIFEST="${SNAPSHOT_MANIFEST:-/app/manifest/full_state.json}"
TARGET="${SNAPSHOT_PATH:-/var/snapshot/database.duckdb}"

echo "[entrypoint] fetching pinned snapshot"
cd /app
python -m scripts.fetch_snapshot "$MANIFEST" "$TARGET"

# fetch_snapshot exits non-zero on any missing, truncated, substituted or
# mismatched artifact, and `set -e` turns that into a task that never serves.
echo "[entrypoint] snapshot verified; starting API"

export DB_PATH="$TARGET"
cd /app/Ask
exec uvicorn serve:app --host 0.0.0.0 --port "${PORT:-8000}"
