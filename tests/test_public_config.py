"""No API response may contain a camera stream URL or its credentials.

Starts the real request handler on an ephemeral localhost port with a temp
config (ffmpeg start/stop stubbed out) and checks every read endpoint plus the
write endpoints that echo a camera back.

Run: docker run --rm --entrypoint sh -v "$PWD":/src:ro <cctv-viewer image> \
       -c 'cp -r /src /tmp/app && cd /tmp/app && CONFIG_PATH=/tmp/cfg/cameras.json python -m unittest -v tests.test_public_config'
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_PATH", os.path.join(tempfile.mkdtemp(), "cameras.json"))
import server  # noqa: E402

TOKEN = "tok3nZZsecretQQ"
PASSWORD = "hunter2pw"
CAM1 = "11111111-1111-4111-8111-111111111111"
CAM2 = "22222222-2222-4222-8222-222222222222"
FORBIDDEN = ("rtsp", TOKEN, PASSWORD, "enableSrtp")


def fixture_config():
    return {
        "cameras": [
            {"id": CAM1, "name": "Front", "url": f"rtsps://192.0.2.10:7441/{TOKEN}?enableSrtp",
             "x": 0, "y": 0, "w": 1, "h": 1},
            {"id": CAM2, "name": "Doorbell", "url": f"rtsp://admin:{PASSWORD}@192.0.2.11:554/s1",
             "hidden": True, "x": 1, "y": 0, "w": 1, "h": 1,
             "stream_token": TOKEN, "backup": f"rtsp://192.0.2.12/{TOKEN}"},
        ],
        "layout": {"columns": 2},
    }


class PublicConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        server.CONFIG_PATH = Path(cls.dir) / "cameras.json"
        server.RINGS_PATH = Path(cls.dir) / "doorbell-rings.jsonl"
        server.RINGS_PATH.write_text(json.dumps({"ts": 1.0, "camera": "Doorbell", "camera_id": CAM2}) + "\n")
        cls._orig = (server.start_stream, server.stop_stream)
        server.start_stream = lambda camera: None
        server.stop_stream = lambda cam_id: None
        cls.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.CCTVHandler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server.start_stream, server.stop_stream = cls._orig

    def setUp(self):
        server.save_config(fixture_config())

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()

    def assertClean(self, text):
        low = text.lower()
        for bad in FORBIDDEN:
            self.assertNotIn(bad.lower(), low)
        self.assertNotIn("://", text)

    def test_read_endpoints_are_clean(self):
        for path in ("/api/config", "/api/config/download", "/api/ping", "/api/version",
                     f"/api/cameras/{CAM1}/status", "/api/doorbell/rings?limit=5"):
            with self.subTest(path=path):
                status, text = self.call("GET", path)
                self.assertEqual(status, 200)
                self.assertClean(text)

    def test_config_keeps_client_fields(self):
        _, text = self.call("GET", "/api/config")
        cfg = json.loads(text)
        self.assertEqual(cfg["layout"], {"columns": 2})
        door = next(c for c in cfg["cameras"] if c["name"] == "Doorbell")
        self.assertEqual({k: door[k] for k in ("id", "name", "hidden", "x", "y", "w", "h")},
                         {"id": CAM2, "name": "Doorbell", "hidden": True, "x": 1, "y": 0, "w": 1, "h": 1})
        self.assertTrue(door["has_url"])
        self.assertNotIn("url", door)
        self.assertNotIn("stream_token", door)
        self.assertNotIn("backup", door)

    def test_add_and_edit_responses_are_clean(self):
        status, text = self.call("POST", "/api/cameras",
                                 {"name": "Side", "url": f"rtsps://192.0.2.13:7441/{TOKEN}"})
        self.assertEqual(status, 201)
        self.assertClean(text)
        status, text = self.call("PUT", f"/api/cameras/{CAM1}", {"name": "Front door"})
        self.assertEqual(status, 200)
        self.assertClean(text)
        stored = next(c for c in server.load_config()["cameras"] if c["id"] == CAM1)
        self.assertEqual(stored["name"], "Front door")
        self.assertTrue(stored["url"].startswith("rtsps://"))  # blank edit keeps the URL

    def test_export_import_round_trip_keeps_urls(self):
        _, exported = self.call("GET", "/api/config/download")
        before = {c["id"]: c["url"] for c in server.load_config()["cameras"]}
        status, text = self.call("POST", "/api/config/import", json.loads(exported))
        self.assertEqual(status, 200)
        self.assertClean(text)
        after = {c["id"]: c["url"] for c in server.load_config()["cameras"]}
        self.assertEqual(after, before)
        self.assertTrue(all("has_url" not in c for c in server.load_config()["cameras"]))

    def test_import_of_new_camera_without_url_is_rejected(self):
        cfg = {"cameras": [{"id": "33333333-3333-4333-8333-333333333333", "name": "New"}],
               "layout": {"columns": 1}}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("POST", "/api/config/import", cfg)
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(len(server.load_config()["cameras"]), 2)

    def test_sse_payload_is_ids_only(self):
        captured = []
        orig = server._broadcast_sse
        server._broadcast_sse = lambda event, data: captured.append((event, data))
        try:
            server.RINGS_PATH, saved = Path(self.dir) / "unused.jsonl", server.RINGS_PATH
            orig_record = server._record_ring_async
            server._record_ring_async = lambda entry: None  # no ring history write
            status, text = self.call("POST", "/api/doorbell/ring")
        finally:
            server._broadcast_sse = orig
            server._record_ring_async = orig_record
            server.RINGS_PATH = saved
        self.assertEqual(status, 200)
        self.assertEqual(captured, [("doorbell_ring", {"camera_id": CAM2})])
        self.assertClean(json.dumps(captured) + text)


if __name__ == "__main__":
    unittest.main()
