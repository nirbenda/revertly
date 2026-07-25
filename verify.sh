#!/bin/bash
# revertly verification loop. Run from repo root: ./verify.sh
# Exits nonzero on any failure. No third-party deps.
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
fail=0

echo "── 1/3  syntax gate (py_compile) ────────────────────────────"
if ! find revertly -name '*.py' -print0 | xargs -0 "$PY" -m py_compile; then
  echo "SYNTAX FAIL"; fail=1
fi

echo "── 2/3  unit suite (unittest discover) ──────────────────────"
if ! "$PY" -m unittest discover -s tests -p 'test_*.py' -v; then
  echo "UNIT FAIL"; fail=1
fi

echo "── 3/3  end-to-end smoke (test_e2e) ─────────────────────────"
if [ -f tests/test_e2e.py ]; then
  if ! "$PY" -m unittest tests.test_e2e -v; then
    echo "E2E FAIL"; fail=1
  fi
else
  echo "(no e2e yet — skipped)"
fi

echo "─────────────────────────────────────────────────────────────"
if [ "$fail" -eq 0 ]; then
  echo "PASS ✅"
else
  echo "FAIL ❌"
fi
exit $fail
