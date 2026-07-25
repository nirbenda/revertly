#!/bin/bash
# revertly uninstaller. Usage: ./uninstall.sh [--purge] [--yes]
#   --purge   also delete the session store (~/.revertly and all revert history)
#   --yes     don't prompt for confirmation
# Removes the shim + launcher and the PATH line it added. Your real `claude`
# is untouched and works normally again.
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
  PYTHONPATH="$REPO" python3 -m revertly uninstall "$@"
else
  # Fallback if python3 somehow vanished: remove the obvious bits by hand.
  echo "revertly: python3 not found; removing shim/launcher manually."
  rm -f "$HOME/.revertly/bin/claude" "$HOME/.revertly/bin/revertly"
  echo "Also remove the '>>> revertly >>>' block from your shell profile manually."
fi
