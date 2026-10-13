#!/usr/bin/env bash
# Example --notify-cmd wrapper: forward the alert text to ntfy.sh.
# Usage: ocshepherd watch --notify-cmd /path/to/ntfy-notify.sh --quiet
set -euo pipefail
curl -fsS --max-time 15 -d @- "https://ntfy.sh/CHANGE-ME-TOPIC" >/dev/null
