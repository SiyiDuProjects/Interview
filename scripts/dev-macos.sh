#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT_DIR/apps/server"
DESKTOP_DIR="$ROOT_DIR/apps/desktop"

if [ ! -x "$SERVER_DIR/.venv/bin/python" ]; then
  echo "Missing backend virtualenv. Run scripts/setup-macos.sh first." >&2
  exit 1
fi

if [ ! -d "$DESKTOP_DIR/node_modules" ]; then
  echo "Missing frontend dependencies. Run scripts/setup-macos.sh first." >&2
  exit 1
fi

export INTERVIEW_API_BASE_URL="${INTERVIEW_API_BASE_URL:-http://127.0.0.1:8000}"

cd "$DESKTOP_DIR"
npm run dev:desktop
