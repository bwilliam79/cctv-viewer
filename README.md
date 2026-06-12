# CCTV Viewer

A self-hosted web interface for viewing RTSP/RTSPS camera feeds in a draggable, resizable grid layout. Built for UniFi Protect and other RTSP-compatible cameras.

## Features

- **Multi-camera grid** with drag-and-drop rearrangement and resizable tiles
- **HEVC/H.265 support** — automatically detects HEVC streams and re-encodes to browser-compatible H.264
- **VAAPI hardware encoding** — auto-detects AMD/Intel GPUs for near-zero CPU transcoding
- **RTSPS support** — handles TLS-encrypted RTSP streams (e.g. UniFi Protect)
- **Persistent config** — camera URLs and layout saved to a JSON file
- **Import/Export** — backup and restore your camera configuration
- **Fullscreen mode** — auto-hiding header with fullscreen toggle button
- **Auto-refresh** — detects external config changes and reloads the UI automatically
- **Docker deployment** with a single `docker compose up`

## Architecture

```
Browser <---> nginx (port 8090) <---> HLS files (.m3u8 / .ts)
                  |
                  +-- proxy --> Python API (port 8091) -- manages config & ffmpeg
                                    |
                                    +-- ffmpeg x N (VAAPI or software encode)
                                          |
                                          +-- RTSP camera feeds
```

- **nginx** serves the frontend and HLS stream segments directly (high throughput, near-zero CPU)
- **Python** handles the REST API (camera CRUD, layout persistence, stream lifecycle)
- **ffmpeg** transcodes HEVC to H.264 via VAAPI hardware encoding when available, with software fallback

## Quick Start

```bash
git clone https://github.com/bwilliam79/cctv-viewer.git
cd cctv-viewer
docker compose up -d
```

Open `http://<your-server>:8090` and add your camera RTSP URLs through the web UI.

## Hardware Encoding (VAAPI)

VAAPI hardware encoding is auto-detected at startup. If your host has an AMD or Intel GPU with `/dev/dri/renderD128`, the container will use it automatically — no configuration needed.

The entrypoint script detects the render device's group ID at runtime, so it works on any host regardless of the numeric GID.

To verify VAAPI is active, check the logs:

```bash
docker logs cctv-viewer 2>&1 | head -15
```

You should see:

```
Added render group (GID xxx) for VAAPI access
VAAPI available on /dev/dri/renderD128
HEVC detected — VAAPI hardware re-encode to H.264 720p
```

If you see `software re-encoding` instead, the GPU may not be accessible. Ensure `/dev/dri` exists on the host and the render device is readable.

## Auto-Refresh

The frontend polls the server config every 45 seconds. When the configuration changes externally (e.g. cameras added/removed or layout updated via the API from another machine), the kiosk display reloads automatically — no manual refresh needed.

The frontend also checks a `/api/version` endpoint every 30 seconds. When the server restarts (e.g. after a redeploy), the version changes and the kiosk reloads to pick up the latest build — useful for headless displays with no keyboard.

## Kiosk Management Scripts

The `scripts/` directory contains utilities for managing a headless kiosk Chrome session running the viewer. Both require Chrome to be started with `--remote-debugging-port=9222`.

### Suggested Chrome launch flags

```
google-chrome --kiosk --no-first-run --disable-session-crashed-bubble \
  --noerrdialogs --disable-infobars --disable-extensions \
  --remote-debugging-port=9222 \
  --user-data-dir=/home/<user>/.config/chrome-kiosk \
  --ozone-platform=wayland \
  http://localhost:8090
```

### reload-chrome.py

Triggers a page reload in Chrome without needing a keyboard, mouse, or server reboot:

```bash
python3 scripts/reload-chrome.py
```

### monitor-chrome.py

Streams Chrome's console logs, HTTP errors (with URLs), and page navigation events to your terminal. Useful for diagnosing feed restarts or unexpected reloads:

```bash
python3 scripts/monitor-chrome.py [seconds]   # default: 120
```

### check-feeds.py

Verifies feeds are actually decoding by sampling each `<video>` element's `currentTime` twice and confirming it advances. A frozen feed produces no console errors, so this is the definitive *playback* liveness check. Exits non-zero if any feed's clock is frozen:

```bash
python3 scripts/check-feeds.py [gap_seconds]   # default gap: 4s
```

### restart-chrome.sh

Full Chrome process restart (kill + relaunch). Use this — **not** `reload-chrome.py` — when feeds are frozen **on screen** but `check-feeds.py` reports them live. That signature means decode is fine but Chrome's GPU compositor / zero-copy video-overlay planes have wedged (typically after a display power/mode event). A page reload only re-creates the DOM video elements; it does not reset the GPU process or its overlay planes, so only a full restart clears it. Run on the host:

```bash
ssh <user>@<host> "~/restart-chrome.sh"
```

### frame-check.py

Distinguishes a **decode freeze** from a **compositor freeze**: draws each video to a canvas and reports whether the decoded pixels change, alongside whether the `currentTime` clock advances.

- clock advancing + frame changing → decode is live; an on-screen freeze is a compositor/overlay wedge → use `restart-chrome.sh`
- clock advancing + frame frozen → decode wedged → a player restart (handled in-page) or `restart-chrome.sh` applies

```bash
python3 scripts/frame-check.py
```

## REST API

All endpoints are served through nginx on port 8090 and proxied to the Python API internally.

### Cameras

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/cameras` | Add a camera. Body: `{"name": "...", "url": "rtsp://..."}` |
| `PUT` | `/api/cameras/:id` | Update a camera. Body: `{"name": "...", "url": "..."}` |
| `DELETE` | `/api/cameras/:id` | Remove a camera and stop its stream |
| `GET` | `/api/cameras/:id/status` | Stream status: `{"running": bool, "ready": bool}` |

### Config & Layout

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/config` | Full config (cameras + layout) |
| `GET` | `/api/config/download` | Download config as `cctv-config.json` attachment |
| `POST` | `/api/config/import` | Replace entire config. Body: full config JSON |
| `PUT` | `/api/layout` | Update grid layout. Body: `{"columns": N, "items": [...]}` |

#### Layout item format

```json
{"id": "camera-uuid", "x": 0, "y": 0, "w": 1, "h": 1}
```

## Configuration

Camera configuration is stored in `config/cameras.json` (mounted as a Docker volume). You can also use the Import/Export buttons in the web UI to back up or restore your setup.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `API_PORT` | `8091` | Internal port for the Python API |
| `CONFIG_PATH` | `/app/config/cameras.json` | Path to the camera config file |

### Docker Compose Options

The default `docker-compose.yml` passes `/dev/dri` into the container for GPU access. If your host has no GPU or you don't need hardware encoding, you can remove the `devices` section — ffmpeg will fall back to software encoding automatically.

## Without Docker

Requires Python 3.12+, ffmpeg, and nginx.

```bash
# Start nginx (configure to match nginx.conf)
# Then:
python server.py
```

## License

MIT
