#!/usr/bin/env bash
# Shared macOS / Linux launcher used by start_pybravo.sh and
# start_vision_service.sh. Usage: _pybravo_launch.sh <module> [args...]
#
# Interpreter selection, in order:
#   1. $PYBRAVO_PYTHON        — explicit override, e.g. a conda python
#   2. uv                     — resolves Python 3.11+ and installs from uv.lock
#   3. ./.venv/bin/python     — a virtualenv in the repo
#   4. python3.13/3.12/3.11 on PATH
#
# The uv path re-syncs .venv against uv.lock on every launch, so a git pull
# that adds a dependency can't leave you with a stale environment. It also
# means optional extras get pruned unless you ask for them:
#   PYBRAVO_EXTRAS=llm scripts/start_pybravo.sh
#
# -B keeps Python from writing .pyc files, so stale bytecode can't survive a
# code change.
set -euo pipefail

module=$1
shift

cd "$(dirname "$0")/.."

start() {
  echo "Starting $module with: $*"
  exec "$@"
}

if [ -n "${PYBRAVO_PYTHON:-}" ]; then
  start "$PYBRAVO_PYTHON" -B -m "$module" "$@"
fi

if command -v uv >/dev/null 2>&1; then
  extras=()
  for extra in ${PYBRAVO_EXTRAS:-}; do
    extras+=(--extra "$extra")
  done
  # ${a[@]+"${a[@]}"} — plain "${a[@]}" on an empty array trips `set -u` in
  # the bash 3.2 that ships with macOS.
  start uv run --frozen ${extras[@]+"${extras[@]}"} python -B -m "$module" "$@"
fi

if [ -x .venv/bin/python ]; then
  start .venv/bin/python -B -m "$module" "$@"
fi

for exe in python3.13 python3.12 python3.11; do
  if command -v "$exe" >/dev/null 2>&1; then
    start "$exe" -B -m "$module" "$@"
  fi
done

cat >&2 <<'EOF'
No suitable Python found. PyBravo needs Python 3.11 or newer.

Easiest fix — install uv, which handles the interpreter and dependencies:
  curl -LsSf https://astral.sh/uv/install.sh | sh

Or use Homebrew and create a virtualenv in the repo:
  brew install python@3.12
  python3.12 -m venv .venv && .venv/bin/pip install -e .

Or point the launcher at an interpreter you already have:
  PYBRAVO_PYTHON=/path/to/python3.11 scripts/start_pybravo.sh
EOF
exit 1
