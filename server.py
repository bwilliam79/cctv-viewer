"""CCTV Viewer - Python backend server."""

import http.server
import json
import os
import signal
import subprocess
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config" / "cameras.json"))
STREAMS_DIR = BASE_DIR / "streams"
PUBLIC_DIR = BASE_DIR / "public"
PORT = int(os.environ.get("API_PORT", os.environ.get("PORT", 8091)))

# 256 KB is roughly 1000× the size of a typical camera-config JSON — easily
# enough headroom for a large import while bounding what a rogue client can
# make us buffer.
MAX_BODY_BYTES = 256 * 1024

# Camera URLs end up as CLI arguments to ffmpeg/ffprobe, which support many
# protocols beyond RTSP (file://, http://, srt://, concat:, etc.). An
# unvalidated URL could reach internal HTTP services, local files, or cloud
# metadata endpoints when the stream starts. We restrict to RTSP schemes —
# everything else is rejected at the config edge.
ALLOWED_URL_SCHEMES = ("rtsp", "rtsps")

# Ensure directories exist
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
STREAMS_DIR.mkdir(parents=True, exist_ok=True)

# Active ffmpeg processes: camera_id -> subprocess.Popen, or _STARTING while a
# spawn is in progress. The sentinel reserves the slot across the slow probe/
# detect work so concurrent start_stream calls don't both race past the guard
# and spawn duplicate ffmpegs writing to the same HLS output directory.
_STARTING = object()
ffmpeg_processes: dict = {}
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


def validate_camera_url(url: str) -> str | None:
    """Validate a camera URL. Returns error message, or None if OK."""
    if not url:
        return "URL is required"
    try:
        parsed = urlparse(url)
    except Exception:
        return "URL could not be parsed"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        return f"URL scheme must be one of: {', '.join(ALLOWED_URL_SCHEMES)}"
    if not parsed.hostname:
        return "URL must include a hostname"
    return None


def detect_vaapi(ffmpeg_bin: str) -> str | None:
    """Check if VAAPI H.264 encoding is available. Returns render device path or None."""
    for dev in ("/dev/dri/renderD128", "/dev/dri/renderD129"):
        if not Path(dev).exists():
            continue
        try:
            result = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-init_hw_device", f"vaapi=va:{dev}",
                 "-f", "lavfi", "-i", "color=black:s=128x128:d=1",
                 "-vf", "format=nv12,hwupload",
                 "-c:v", "h264_vaapi", "-frames:v", "1",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                print(f"  VAAPI available on {dev}")
                return dev
        except Exception:
            pass
    return None


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
        ffmpeg_processes[cam_id] = _STARTING

    proc = None
    try:
        proc = _spawn_ffmpeg(camera)
    finally:
        if proc is None:
            with process_lock:
                if ffmpeg_processes.get(cam_id) is _STARTING:
                    ffmpeg_processes.pop(cam_id, None)

    if proc is None:
        return

    # Claim the slot. If stop_stream popped our reservation while we were
    # spawning, the camera has been deleted / re-added — kill the proc
    # we just started rather than leaking it.
    with process_lock:
        if ffmpeg_processes.get(cam_id) is _STARTING:
            ffmpeg_processes[cam_id] = proc
            claimed = True
        else:
            claimed = False

    if not claimed:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return

    threading.Thread(target=_monitor_stream, args=(camera, proc), daemon=True).start()


def _spawn_ffmpeg(camera: dict):
    cam_id = camera["id"]
    output_dir = STREAMS_DIR / cam_id
    output_dir.mkdir(parents=True, exist_ok=True)

    url = camera["url"]
    is_rtsps = url.lower().startswith("rtsps://")

    paths = resolve_ffmpeg()
    if not paths:
        print("ERROR: ffmpeg not found. Install ffmpeg and ensure it's on your PATH.")
        return None
    ffmpeg_bin, ffprobe_bin = paths

    # Probe the codec to decide copy vs re-encode
    codec = probe_codec(url, ffprobe_bin)
    needs_reencode = codec and ("hevc" in codec or "h265" in codec)

    # Check for VAAPI hardware encoding support
    vaapi_dev = detect_vaapi(ffmpeg_bin) if needs_reencode else None

    args = [ffmpeg_bin, "-y"]

    if vaapi_dev:
        args += ["-init_hw_device", f"vaapi=va:{vaapi_dev}", "-hwaccel", "vaapi",
                 "-hwaccel_output_format", "vaapi", "-hwaccel_device", vaapi_dev]

    if is_rtsps:
        args += [
            "-rtsp_transport", "tcp",
            "-allowed_media_types", "video",
            "-tls_verify", "0",
        ]
    else:
        args += ["-rtsp_transport", "tcp"]

    args += ["-i", url]

    if needs_reencode and vaapi_dev:
        # VAAPI hardware encode — near-zero CPU usage
        args += [
            "-vf", "scale_vaapi=w=-2:h=720,format=nv12|vaapi",
            "-c:v", "h264_vaapi",
            "-g", "30",
            "-sc_threshold", "0",
        ]
        print(f"  HEVC detected — VAAPI hardware re-encode to H.264 720p")
    elif needs_reencode:
        # Software fallback: re-encode to H.264 at 720p
        args += [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-vf", "scale=-2:720",
            "-g", "30",
            "-sc_threshold", "0",
        ]
        print(f"  HEVC detected — software re-encoding to H.264 720p")
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
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("ERROR: ffmpeg not found. Install ffmpeg and ensure it's on your PATH.")
        return None


def _monitor_stream(camera: dict, proc: subprocess.Popen):
    cam_id = camera["id"]
    stderr_output = []
    for line in proc.stderr:
        decoded = line.decode(errors="replace").strip()
        if decoded:
            stderr_output.append(decoded)
            if "error" in decoded.lower() or "fatal" in decoded.lower():
                print(f'  [{camera["name"]}] {decoded}')
    proc.wait()
    code = proc.returncode
    print(f'Stream for "{camera["name"]}" exited with code {code}')
    if code != 0 and stderr_output:
        for line in stderr_output[-5:]:
            print(f'  [{camera["name"]}] {line}')

    # Only pop and restart if the tracked proc is still ours. The watchdog
    # may have force-replaced us (stuck-monitor recovery) — in that case,
    # leave the new entry alone and don't schedule a restart.
    with process_lock:
        if ffmpeg_processes.get(cam_id) is proc:
            ffmpeg_processes.pop(cam_id, None)
            should_restart = True
        else:
            should_restart = False

    if not should_restart:
        return

    config = load_config()
    still_exists = any(c["id"] == cam_id for c in config["cameras"])
    if still_exists:
        print(f'Restarting stream for "{camera["name"]}" in 5s...')
        threading.Timer(5.0, start_stream, args=[camera]).start()


def stop_stream(cam_id: str):
    with process_lock:
        entry = ffmpeg_processes.pop(cam_id, None)
    # If entry is _STARTING, an in-progress start_stream will see the missing
    # reservation when it tries to claim the slot and terminate its own proc.
    if entry is not None and entry is not _STARTING:
        entry.terminate()
        try:
            entry.wait(timeout=5)
        except subprocess.TimeoutExpired:
            entry.kill()
    # Clean up stream files
    output_dir = STREAMS_DIR / cam_id
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)


class CCTVHandler(http.server.BaseHTTPRequestHandler):
    """Handle API routes only. Static files and HLS served by nginx."""

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
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if body is None:
            return

        if path == "/api/cameras":
            data = json.loads(body)
            name = data.get("name", "").strip()
            url = data.get("url", "").strip()
            if not name:
                self._json_response({"error": "name is required"}, 400)
                return
            url_err = validate_camera_url(url)
            if url_err:
                self._json_response({"error": url_err}, 400)
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

            # Validate all camera URLs up front so a bad import can't partially
            # apply (stopping current streams and then leaving us in a weird
            # half-configured state).
            for cam in new_config["cameras"]:
                url_err = validate_camera_url((cam.get("url") or "").strip())
                if url_err:
                    self._json_response(
                        {"error": f"Invalid camera '{cam.get('name', '?')}': {url_err}"}, 400
                    )
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
        if body is None:
            return
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
                url_err = validate_camera_url((data["url"] or "").strip())
                if url_err:
                    self._json_response({"error": url_err}, 400)
                    return
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

    def _read_body(self):
        """Read request body, capped at MAX_BODY_BYTES.

        Returns the body bytes on success, or None if the request is
        malformed / oversized (a response has already been sent).
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._json_response({"error": "Invalid Content-Length"}, 400)
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._json_response({"error": f"Request body exceeds {MAX_BODY_BYTES} bytes"}, 413)
            return None
        return self.rfile.read(length)

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        super().log_message(format, *args)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def main():
    # Start streams for all saved cameras
    config = load_config()
    print(f"Loaded {len(config['cameras'])} camera(s) from config")
    for camera in config["cameras"]:
        start_stream(camera)

    def watchdog():
        """Detect ffmpeg processes that exited but whose monitor threads are stuck on a
        blocked pipe read (e.g. a VAAPI helper subprocess inherited the stderr fd).
        Runs every 30 s and force-restarts any dead stream."""
        while True:
            time.sleep(30)
            cfg = load_config()
            with process_lock:
                procs_snapshot = dict(ffmpeg_processes)
            for cam in cfg["cameras"]:
                cid = cam["id"]
                entry = procs_snapshot.get(cid)
                if entry is _STARTING:
                    # A spawn is in progress — don't interfere
                    continue
                if entry is not None and entry.poll() is not None:
                    # Process has exited; force-restart only if it's still the
                    # tracked entry (don't race with the monitor thread).
                    with process_lock:
                        if ffmpeg_processes.get(cid) is entry:
                            ffmpeg_processes.pop(cid, None)
                            do_restart = True
                        else:
                            do_restart = False
                    if do_restart:
                        print(f'Watchdog: "{cam["name"]}" exited (code {entry.poll()}), forcing restart')
                        threading.Thread(target=start_stream, args=[cam], daemon=True).start()
                elif entry is None:
                    # Not tracked at all — start it (start_stream's reservation
                    # protects against duplicate spawns from concurrent triggers).
                    threading.Thread(target=start_stream, args=[cam], daemon=True).start()

    threading.Thread(target=watchdog, daemon=True).start()

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
