#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT_DIR/apps/server"
DESKTOP_DIR="${INTERVIEW_DESKTOP_DIR:-$ROOT_DIR/apps/desktop}"
DEFAULT_REMOTE_API_BASE_URL="https://interview.reachard.co"

if [ ! -x "$SERVER_DIR/.venv/bin/python" ]; then
  echo "Missing backend virtualenv. Run scripts/setup-macos.sh first." >&2
  exit 1
fi

if [ ! -d "$DESKTOP_DIR/node_modules" ]; then
  echo "Missing frontend dependencies. Run scripts/setup-macos.sh first." >&2
  exit 1
fi

if [ -z "${INTERVIEW_API_BASE_URL:-}" ] && [ -z "${VITE_API_BASE_URL:-}" ]; then
  export INTERVIEW_API_BASE_URL="$DEFAULT_REMOTE_API_BASE_URL"
fi

echo "Using Interview API: ${INTERVIEW_API_BASE_URL:-${VITE_API_BASE_URL:-$DEFAULT_REMOTE_API_BASE_URL}}"

cd "$DESKTOP_DIR"
npm run dev:desktop
