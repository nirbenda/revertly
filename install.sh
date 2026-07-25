#!/bin/bash
# revertly installer. Usage: ./install.sh [--no-profile]
# Installs a `revertly` launcher and a `claude` shim into ~/.revertly/bin and adds
# that dir to your PATH (unless --no-profile). Fully reversible: ./uninstall.sh
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
PYTHONPATH="$REPO" python3 -m revertly doctor || true
echo
echo "Next: open a NEW terminal (so PATH refreshes), then run:  revertly doctor"
echo "Then just use \`claude\` as usual — revertly arms automatically."
