#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT_DIR/apps/server"
DESKTOP_DIR="$ROOT_DIR/apps/desktop"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required. Install Python 3 first, then rerun this script." >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required. Install Node.js from https://nodejs.org or Homebrew first." >&2
  exit 1
fi

python3 -m venv "$SERVER_DIR/.venv"
"$SERVER_DIR/.venv/bin/python" -m pip install --upgrade pip
"$SERVER_DIR/.venv/bin/python" -m pip install -r "$SERVER_DIR/requirements.txt"

cd "$DESKTOP_DIR"
npm install

echo "macOS setup complete."
