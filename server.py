"""CCTV Viewer - Python backend server."""

import http.server
import json
import os
import signal
import subprocess
import shutil
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config" / "cameras.json"))
STREAMS_DIR = BASE_DIR / "streams"
PUBLIC_DIR = BASE_DIR / "public"
PORT = int(os.environ.get("PORT", 8090))

# Ensure directories exist
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
STREAMS_DIR.mkdir(parents=True, exist_ok=True)

# Active ffmpeg processes: camera_id -> subprocess.Popen
ffmpeg_processes: dict[str, subprocess.Popen] = {}
process_lock = threading.Lock()


def load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to load config: {e}")
    return {"cameras": [], "layout": {"columns": 3}}


def save_config(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def resolve_ffmpeg() -> tuple[str, str] | None:
    """Find ffmpeg and ffprobe binaries. Returns (ffmpeg_path, ffprobe_path) or None."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        win_path = Path(r"C:\ffmpeg")
        candidates = list(win_path.glob("ffmpeg-*/bin/ffmpeg.exe"))
        if candidates:
            ffmpeg_bin = str(candidates[0])
        else:
            return None
    # ffprobe lives next to ffmpeg
    ffmpeg_dir = Path(ffmpeg_bin).parent
    ffprobe_bin = str(ffmpeg_dir / "ffprobe.exe") if os.name == "nt" else str(ffmpeg_dir / "ffprobe")
    if not Path(ffprobe_bin).exists():
        ffprobe_bin = shutil.which("ffprobe") or ffprobe_bin
    return ffmpeg_bin, ffprobe_bin


def probe_codec(url: str, ffprobe_bin: str) -> str | None:
    """Probe the video codec of a stream. Returns 'hevc', 'h264', or None."""
    is_rtsps = url.lower().startswith("rtsps://")
    args = [ffprobe_bin]
    if is_rtsps:
        args += ["-rtsp_transport", "tcp", "-tls_verify", "0"]
    args += [
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "csv=p=0",
        url,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        codec = result.stdout.strip().lower()
        if codec:
            print(f"  Probed codec: {codec}")
            return codec
    except Exception as e:
        print(f"  Probe failed: {e}")
    return None


def start_stream(camera: dict):
    cam_id = camera["id"]
    with process_lock:
        if cam_id in ffmpeg_processes:
            return

    output_dir = STREAMS_DIR / cam_id
    output_dir.mkdir(parents=True, exist_ok=True)

    url = camera["url"]
    is_rtsps = url.lower().startswith("rtsps://")

    paths = resolve_ffmpeg()
    if not paths:
        print("ERROR: ffmpeg not found. Install ffmpeg and ensure it's on your PATH.")
        return
    ffmpeg_bin, ffprobe_bin = paths

    # Probe the codec to decide copy vs re-encode
    codec = probe_codec(url, ffprobe_bin)
    needs_reencode = codec and ("hevc" in codec or "h265" in codec)

    args = [ffmpeg_bin, "-y"]

    if is_rtsps:
        args += [
            "-rtsp_transport", "tcp",
            "-allowed_media_types", "video",
            "-tls_verify", "0",
        ]
    else:
        args += ["-rtsp_transport", "tcp"]

    args += ["-i", url]

    if needs_reencode:
        # HEVC source: re-encode to H.264 at 720p to keep CPU low
        args += [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-vf", "scale=-2:720",
            "-g", "30",
            "-sc_threshold", "0",
        ]
        print(f"  HEVC detected — re-encoding to H.264 720p")
    else:
        # H.264 source: passthrough, no CPU cost
        args += [
            "-c:v", "copy",
            "-bsf:v", "h264_mp4toannexb",
        ]
        print(f"  H.264 detected — passthrough copy")

    args += [
        "-an",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "3",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(output_dir / "segment_%03d.ts"),
        str(output_dir / "stream.m3u8"),
    ]

    print(f'Starting stream for camera "{camera["name"]}" ({cam_id})')
    print(f'  Command: {" ".join(args[:6])}...')
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("ERROR: ffmpeg not found. Install ffmpeg and ensure it's on your PATH.")
        return

    with process_lock:
        ffmpeg_processes[cam_id] = proc

    # Monitor in background thread
    def monitor():
        # Read stderr for error reporting
        stderr_output = []
        for line in proc.stderr:
            decoded = line.decode(errors="replace").strip()
            if decoded:
                stderr_output.append(decoded)
                # Print errors immediately
                if "error" in decoded.lower() or "fatal" in decoded.lower():
                    print(f'  [{camera["name"]}] {decoded}')
        proc.wait()
        code = proc.returncode
        print(f'Stream for "{camera["name"]}" exited with code {code}')
        if code != 0 and stderr_output:
            # Print last few lines of stderr for debugging
            for line in stderr_output[-5:]:
                print(f'  [{camera["name"]}] {line}')
        with process_lock:
            ffmpeg_processes.pop(cam_id, None)
        # Auto-restart if unexpected exit
        config = load_config()
        still_exists = any(c["id"] == cam_id for c in config["cameras"])
        if still_exists and code != 0:
            print(f'Restarting stream for "{camera["name"]}" in 5s...')
            threading.Timer(5.0, start_stream, args=[camera]).start()

    threading.Thread(target=monitor, daemon=True).start()


def stop_stream(cam_id: str):
    with process_lock:
        proc = ffmpeg_processes.pop(cam_id, None)
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    # Clean up stream files
    output_dir = STREAMS_DIR / cam_id
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)


class CCTVHandler(http.server.SimpleHTTPRequestHandler):
    """Handle API routes and serve static files."""

    def __init__(self, *args, **kwargs):
        # Don't call super().__init__ here; it's called by the server
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            self._json_response(load_config())
        elif path == "/api/config/download":
            config = load_config()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="cctv-config.json"')
            body = json.dumps(config, indent=2).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/cameras/") and path.endswith("/status"):
            cam_id = path.split("/")[3]
            with process_lock:
                running = cam_id in ffmpeg_processes
            m3u8 = STREAMS_DIR / cam_id / "stream.m3u8"
            self._json_response({"running": running, "ready": m3u8.exists()})
        elif path.startswith("/streams/"):
            # Serve stream files from STREAMS_DIR
            rel = path[len("/streams/"):]
            file_path = STREAMS_DIR / rel
            try:
                data = file_path.read_bytes()
                self.send_response(200)
                if file_path.suffix == ".m3u8":
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                elif file_path.suffix == ".ts":
                    self.send_header("Content-Type", "video/mp2t")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (FileNotFoundError, OSError):
                self.send_error(404)
        else:
            # Serve static files from public/
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/cameras":
            data = json.loads(body)
            name = data.get("name", "").strip()
            url = data.get("url", "").strip()
            if not name or not url:
                self._json_response({"error": "name and url are required"}, 400)
                return

            config = load_config()
            camera = {
                "id": str(uuid.uuid4()),
                "name": name,
                "url": url,
                "x": 0,
                "y": 9999,
                "w": 1,
                "h": 1,
            }
            config["cameras"].append(camera)
            save_config(config)
            start_stream(camera)
            self._json_response(camera, 201)

        elif path == "/api/config/import":
            new_config = json.loads(body)
            if not isinstance(new_config.get("cameras"), list):
                self._json_response({"error": "Invalid config format"}, 400)
                return

            # Stop all current streams
            with process_lock:
                ids = list(ffmpeg_processes.keys())
            for cam_id in ids:
                stop_stream(cam_id)

            save_config(new_config)
            for camera in new_config["cameras"]:
                start_stream(camera)
            self._json_response({"ok": True})

        else:
            self.send_error(404)

    def do_PUT(self):
        path = urlparse(self.path).path
        body = self._read_body()
        data = json.loads(body)

        if path.startswith("/api/cameras/"):
            cam_id = path.split("/")[3]
            config = load_config()
            camera = next((c for c in config["cameras"] if c["id"] == cam_id), None)
            if not camera:
                self._json_response({"error": "Camera not found"}, 404)
                return

            for key in ("name", "x", "y", "w", "h"):
                if key in data:
                    camera[key] = data[key]

            if "url" in data and data["url"] != camera["url"]:
                camera["url"] = data["url"]
                stop_stream(cam_id)
                start_stream(camera)

            save_config(config)
            self._json_response(camera)

        elif path == "/api/layout":
            config = load_config()
            if "columns" in data:
                config["layout"]["columns"] = data["columns"]

            for item in data.get("items", []):
                camera = next((c for c in config["cameras"] if c["id"] == item["id"]), None)
                if camera:
                    for key in ("x", "y", "w", "h"):
                        if key in item:
                            camera[key] = item[key]

            save_config(config)
            self._json_response({"ok": True})

        else:
            self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/cameras/"):
            cam_id = path.split("/")[3]
            config = load_config()
            idx = next((i for i, c in enumerate(config["cameras"]) if c["id"] == cam_id), None)
            if idx is None:
                self._json_response({"error": "Camera not found"}, 404)
                return

            stop_stream(cam_id)
            config["cameras"].pop(idx)
            save_config(config)
            self._json_response({"ok": True})
        else:
            self.send_error(404)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quieter logging - only log errors
        if args and isinstance(args[0], str) and args[0].startswith("GET /streams/"):
            return  # Suppress noisy HLS segment requests
        super().log_message(format, *args)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def main():
    # Start streams for all saved cameras
    config = load_config()
    print(f"Loaded {len(config['cameras'])} camera(s) from config")
    for camera in config["cameras"]:
        start_stream(camera)

    server = ThreadedHTTPServer(("0.0.0.0", PORT), CCTVHandler)
    print(f"CCTV Viewer running on http://0.0.0.0:{PORT}")

    def shutdown(signum, frame):
        print("\nShutting down...")
        with process_lock:
            ids = list(ffmpeg_processes.keys())
        for cam_id in ids:
            stop_stream(cam_id)
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
