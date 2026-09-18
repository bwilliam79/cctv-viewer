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
LAST_RESTART_FILE="/tmp/cctv-watchdog.last-restart"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
LOG() { logger -t cctv-watchdog -- "$*"; }

# Don't restart Chrome more than once per RATE_LIMIT_SEC. If Chrome keeps
# dying we want a human to look, not a tight loop hiding the cause.
RATE_LIMIT_SEC=600

try_restart() {
  local reason="$1"
  if [ -f "$LAST_RESTART_FILE" ]; then
    local last_age=$(( $(date +%s) - $(stat -c %Y "$LAST_RESTART_FILE") ))
    if [ "$last_age" -lt "$RATE_LIMIT_SEC" ]; then
      LOG "$reason — but last restart was ${last_age}s ago (<${RATE_LIMIT_SEC}s rate limit); skipping"
      return 0
    fi
  fi
  LOG "$reason — running restart-chrome.sh"
  touch "$LAST_RESTART_FILE"
  "$SCRIPT_DIR/restart-chrome.sh" 2>&1 | logger -t cctv-watchdog
}

# Backend health gate — if the server's down, this is not a browser problem.
if ! curl -sf -m 3 http://localhost:8090/api/ping >/dev/null 2>&1; then
  LOG "backend /api/ping not reachable; skipping (server-side problem, not Chrome's)"
  rm -f "$STATE_FILE"
  exit 0
fi

# Chrome lifecycle:
#   - debug port up: Chrome is running, proceed to the freeze check.
#   - debug port down + chrome --kiosk process exists: Chrome is mid-startup or
#     mid-restart; wait for the next tick before doing anything.
#   - debug port down + no chrome --kiosk process: Chrome is DEAD. Restart it.
if ! curl -sf -m 3 http://localhost:9222/json/version >/dev/null 2>&1; then
  if pgrep -f '[c]hrome --kiosk' >/dev/null 2>&1; then
    LOG "Chrome debug port unreachable but process exists; assuming mid-startup, skipping"
    rm -f "$STATE_FILE"
    exit 0
  fi
  rm -f "$STATE_FILE"
  try_restart "Chrome process is dead"
  exit 0
fi

# Grid live? (check-feeds exit 0 = all live, non-zero = at least one frozen)
if python3 "$SCRIPT_DIR/check-feeds.py" 3 >/dev/null 2>&1; then
  rm -f "$STATE_FILE"
  exit 0
fi

# Frozen. One miss is tolerated; two in a row escalates.
if [ -f "$STATE_FILE" ]; then
  rm -f "$STATE_FILE"
  try_restart "feeds frozen on 2nd consecutive check"
else
  LOG "feeds frozen on 1st check; will recover if still frozen next run"
  : > "$STATE_FILE"
fi
