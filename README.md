# CCTV Viewer

A self-hosted web interface for viewing RTSP/RTSPS camera feeds in a draggable, resizable grid layout. Built for UniFi Protect and other RTSP-compatible cameras.

## Features

- **Multi-camera grid** with drag-and-drop rearrangement and resizable tiles
- **HEVC/H.265 support** — automatically detects HEVC streams and re-encodes to browser-compatible H.264
- **VAAPI hardware encoding** — auto-detects AMD/Intel GPUs for near-zero CPU transcoding
- **RTSPS support** — handles TLS-encrypted RTSP streams (e.g. UniFi Protect)
- **Persistent config** — camera URLs and layout saved to a JSON file
- **Import/Export** — backup and restore your camera configuration
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
