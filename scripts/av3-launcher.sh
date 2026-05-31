#!/usr/bin/env bash
# Auto Applier v3 — one-click launcher (Phase 4 (6/M), spec section 11a).
#
# POSIX counterpart of av3-launcher.cmd. Same pattern: prefer the repo's
# .venv if present, otherwise fall through to ``python`` on PATH, then
# call ``av3 launch`` which spawns the server and opens the browser.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

echo "Starting Auto Applier v3 ..."
echo "(Ctrl-C to stop the server.)"
echo

exec "$PYTHON" -m av3.cli.main launch
