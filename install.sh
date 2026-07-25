#!/bin/bash
# revertly installer. Usage: ./install.sh [--all] [--agents claude,aider] [--none] [--no-profile]
# Installs a `revertly` launcher into ~/.revertly/bin, DETECTS the agent CLIs on
# your PATH (Claude Code, Codex, Gemini, Aider, Cursor CLI, …) and offers to bind
# them, then adds the bin dir to PATH (unless --no-profile). Reversible: ./uninstall.sh
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "revertly: python3 is required but not found on PATH." >&2
  exit 1
fi

echo "revertly: installing from $REPO"
PYTHONPATH="$REPO" python3 -m revertly install "$@"

echo
echo "revertly: verifying…"
PYTHONPATH="$REPO" python3 -m revertly doctor --install || true
echo
echo "Next: open a NEW terminal (so PATH refreshes), then run:  revertly doctor"
echo "Then use your bound agent(s) as usual — revertly arms automatically."
echo "Bind more later with:  revertly bind <command>   ·   see all:  revertly agents"
