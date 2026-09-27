"""Unit tests for doorbell ring persistence (no HTTP, no SSE, no real rings).

Run: docker run --rm --entrypoint sh -v "$PWD":/src:ro <cctv-viewer image> \
       -c 'cp -r /src /tmp/app && cd /tmp/app && CONFIG_PATH=/tmp/cfg/cameras.json python -m unittest -v tests.test_ring_log'
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_PATH", os.path.join(tempfile.mkdtemp(), "cameras.json"))
import server  # noqa: E402


class RingLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "rings.jsonl"

    def lines(self):
        return [json.loads(l) for l in self.path.read_text().splitlines()]

    def test_append_creates_file(self):
        server.append_ring({"ts": 1000.0, "camera": "Doorbell"}, path=self.path, now=1000.0)
        self.assertEqual(self.lines(), [{"ts": 1000.0, "camera": "Doorbell"}])

    def test_prunes_older_than_max_age(self):
        now = 10_000_000.0
        old = now - 31 * 86400
        recent = now - 29 * 86400
        self.path.write_text(json.dumps({"ts": old}) + "\n" + json.dumps({"ts": recent}) + "\n")
        server.append_ring({"ts": now}, path=self.path, now=now)
        self.assertEqual([e["ts"] for e in self.lines()], [recent, now])

    def test_line_cap(self):
        for i in range(12):
            server.append_ring({"ts": 100.0 + i}, path=self.path, now=200.0, max_lines=5)
        self.assertEqual([e["ts"] for e in self.lines()], [107.0, 108.0, 109.0, 110.0, 111.0])

    def test_garbage_lines_dropped(self):
        self.path.write_text("not json\n{\"no_ts\":1}\n")
        server.append_ring({"ts": 5.0}, path=self.path, now=5.0)
        self.assertEqual(self.lines(), [{"ts": 5.0}])

    def test_no_tmp_left_and_concurrent_writes(self):
        threads = [threading.Thread(target=server.append_ring, args=({"ts": 50.0 + i},),
                                    kwargs={"path": self.path, "now": 60.0}) for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(self.lines()), 20)
        self.assertFalse((Path(self.dir) / "rings.jsonl.tmp").exists())

    def test_read_rings_newest_first_and_limit(self):
        for i in range(5):
            server.append_ring({"ts": 10.0 + i}, path=self.path, now=20.0)
        self.assertEqual([e["ts"] for e in server.read_rings(2, path=self.path)], [14.0, 13.0])
        self.assertEqual(server.read_rings(5, path=Path(self.dir) / "missing.jsonl"), [])

    def test_payload_fields_whitelisted(self):
        payload = {"alarm": {"name": "Doorbell ring", "sources": [{"device": "AA"}],
                             "triggers": [{"key": "ring", "eventId": "evt123", "device": "AA:BB"}]},
                   "timestamp": 1}
        self.assertEqual(server._ring_fields_from_payload(payload),
                         {"alarm_name": "Doorbell ring", "trigger": "ring", "event_id": "evt123"})
        self.assertEqual(server._ring_fields_from_payload(None), {})
        self.assertEqual(server._ring_fields_from_payload({"alarm": "x"}), {})

    def test_async_write_swallows_errors(self):
        orig = server.RINGS_PATH
        try:
            server.RINGS_PATH = Path("/proc/definitely/not/writable.jsonl")
            server._record_ring_async({"ts": 1.0})  # must not raise
        finally:
            server.RINGS_PATH = orig


if __name__ == "__main__":
    unittest.main()
