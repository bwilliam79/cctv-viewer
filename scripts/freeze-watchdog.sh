#!/usr/bin/env bash
# Detect feed freezes and auto-recover the kiosk Chrome.
#
# Designed to run on a short systemd timer (~5 min). Behavior:
#   * Skip cleanly if Chrome's debug port or the backend isn't reachable
#     (mid-restart / different problem class — not ours to fix).
#   * If check-feeds.py reports the grid live, clear state and exit.
#   * If frozen, require TWO consecutive frozen checks before escalating —
#     single transient hiccups don't trigger a recovery.
#   * On escalation, run restart-chrome.sh and log via syslog.
#
# State is stored in /tmp so it resets across reboots; that's fine — a fresh
# boot starts with a healthy Chrome.
#
# Limitations:
#   * check-feeds.py only inspects the four grid camera UUIDs, so a
#     doorbell-only freeze won't trigger this. The grid wedging is the loud
#     symptom; the doorbell freezing alone is rare and recoverable manually.
#   * A compositor-only freeze (decoded frames updating but not painted to
#     screen) cannot be detected from inside the page and won't trigger this.
#     Use restart-chrome.sh manually if you see that signature.

set -u

STATE_FILE="/tmp/cctv-watchdog.state"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
LOG() { logger -t cctv-watchdog -- "$*"; }

# Chrome debug port not up → likely mid-restart, don't escalate.
if ! curl -sf -m 3 http://localhost:9222/json/version >/dev/null 2>&1; then
  rm -f "$STATE_FILE"
  exit 0
fi

# Backend not healthy → not a browser problem, don't restart Chrome over it.
if ! curl -sf -m 3 http://localhost:8090/api/ping >/dev/null 2>&1; then
  rm -f "$STATE_FILE"
  exit 0
fi

# Grid live? (check-feeds exit 0 = all live, non-zero = at least one frozen)
if python3 "$SCRIPT_DIR/check-feeds.py" 3 >/dev/null 2>&1; then
  rm -f "$STATE_FILE"
  exit 0
fi

# Frozen. One miss is tolerated; two in a row escalates.
if [ -f "$STATE_FILE" ]; then
  LOG "feeds frozen on 2nd consecutive check; running restart-chrome.sh"
  rm -f "$STATE_FILE"
  "$SCRIPT_DIR/restart-chrome.sh" 2>&1 | logger -t cctv-watchdog
else
  LOG "feeds frozen on 1st check; will recover if still frozen next run"
  : > "$STATE_FILE"
fi
