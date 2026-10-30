#!/usr/bin/env bash
# Kept for old instructions: the self-test is now Python so it also runs on Windows. Same arguments.
#   bash scripts/selftest.sh [--engine kokoro|edge|flite|auto] [--voice NAME] [--out DIR]
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
exec "$PY" "$HERE/selftest.py" "$@"
