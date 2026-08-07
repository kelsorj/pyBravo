#!/usr/bin/env bash
# macOS / Linux launcher for the PyBravo web server — the equivalent of
# start_pybravo.bat. Serves the UI on http://localhost:8000.
set -euo pipefail
exec "$(dirname "$0")/_pybravo_launch.sh" pybravo.web.server "$@"
