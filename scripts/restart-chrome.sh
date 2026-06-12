#!/usr/bin/env bash
# Full Chrome kiosk restart — kills and relaunches the browser process.
#
# Use this (NOT reload-chrome.py) when feeds are frozen ON SCREEN but
# check-feeds.py reports them live. That signature means the <video> elements
# are decoding fine but Chrome's GPU compositor / zero-copy video-overlay planes
# have wedged (typically after a display power/mode event). A page reload only
# re-creates the DOM video elements — it does NOT reset the GPU process or its
# overlay planes, so the on-screen freeze persists. Only a full process restart
# (fresh GPU process) clears it.
#
# Flags live in ~/.config/autostart/cctv-viewer.desktop (single source of truth);
# this script just replays that launch command.
set -e

UID_NUM=$(id -u)
export XDG_RUNTIME_DIR="/run/user/${UID_NUM}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${UID_NUM}/bus"
export WAYLAND_DISPLAY=wayland-0

# Bracket trick so this pattern never matches the script's own command line.
pkill -9 -f '[c]hrome --kiosk' || true
sleep 3
rm -f "${HOME}/.config/chrome-kiosk/Singleton"* 2>/dev/null || true

EXEC=$(grep '^Exec=' "${HOME}/.config/autostart/cctv-viewer.desktop" | head -1 | sed 's/^Exec=//')
if [ -z "${EXEC}" ]; then
  echo "Could not find Exec= line in autostart file" >&2
  exit 1
fi

setsid bash -c "${EXEC}" >/tmp/chrome.log 2>&1 < /dev/null &
echo "Chrome kiosk restarted (full process). Streams reconnect once the backend check passes."
