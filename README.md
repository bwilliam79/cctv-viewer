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

## Fullscreen / Kiosk Mode

The toolbar auto-hides and appears when you hover the top edge of the screen. It stays visible while in edit mode. A fullscreen button in the toolbar toggles the browser's Fullscreen API for a clean, edge-to-edge camera view.

For a dedicated kiosk display, launch Chrome with `--kiosk` to start in fullscreen:

```bash
google-chrome --kiosk --no-first-run --disable-session-crashed-bubble \
  --noerrdialogs --disable-infobars http://<your-server>:8090
```

The mouse cursor is hidden automatically in the viewer for a clean kiosk experience.

## Auto-Refresh

The frontend polls the server config every 3 seconds. When the configuration changes externally (e.g. cameras added/removed or layout updated via the API from another machine), the kiosk display reloads automatically — no manual refresh needed.

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
