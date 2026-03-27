#!/bin/sh
# Auto-detect the render device GID and add it to the current user
# so VAAPI hardware encoding works on any host without hardcoding GIDs.
if [ -e /dev/dri/renderD128 ]; then
    RENDER_GID=$(stat -c '%g' /dev/dri/renderD128)
    if ! id -G | grep -qw "$RENDER_GID"; then
        groupadd -g "$RENDER_GID" render 2>/dev/null || true
        usermod -aG render root 2>/dev/null || true
        echo "Added render group (GID $RENDER_GID) for VAAPI access"
    fi
fi

exec "$@"
