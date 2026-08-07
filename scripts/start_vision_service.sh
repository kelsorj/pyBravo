#!/usr/bin/env bash
# macOS / Linux launcher for the vision service — the equivalent of
# start_vision_service.bat.
set -euo pipefail
exec "$(dirname "$0")/_pybravo_launch.sh" pybravo.vision_service "$@"
