#!/usr/bin/env bash
# Prove the database before serving anything.
set -euo pipefail

MANIFEST="${SNAPSHOT_MANIFEST:-/app/manifest/full_state.json}"
TARGET="${SNAPSHOT_PATH:-/var/snapshot/database.duckdb}"
# Task-local, beside the database it describes, so it cannot outlive the task
# that verified it (#85).
IDENTITY="${SNAPSHOT_IDENTITY_PATH:-/var/snapshot/identity.json}"

echo "[entrypoint] fetching pinned snapshot"
cd /app
# --identity-out writes only after verification succeeds. The command stays
# unguarded so `set -e` still turns any verification failure into a task that
# never serves -- adding the flag must not soften the fail-closed behaviour,
# which is the whole point of running this before uvicorn.
python -m scripts.fetch_snapshot "$MANIFEST" "$TARGET" --identity-out "$IDENTITY"

# fetch_snapshot exits non-zero on any missing, truncated, substituted or
# mismatched artifact, and `set -e` turns that into a task that never serves.
echo "[entrypoint] snapshot verified; starting API"

export DB_PATH="$TARGET"
export SNAPSHOT_IDENTITY_PATH="$IDENTITY"
cd /app/Ask
exec uvicorn serve:app --host 0.0.0.0 --port "${PORT:-8000}"
