#!/usr/bin/env python3
"""Verify CCTV feeds are actually playing by sampling each <video> element's
currentTime twice and confirming it advances.

A frozen feed throws no console errors, so absence of errors is not proof of
life. This checks the only thing that matters: are frames advancing?

Usage:
    python3 scripts/check-feeds.py [gap_seconds]   # default gap: 4s

Requires Chrome running with --remote-debugging-port=9222.
"""
import json, re, socket, os, base64, struct, time, sys
import urllib.request

# Only camera UUIDs — filters out overlay video elements like #doorbell-video
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")

PORT = 9222
GAP = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0

tabs = json.loads(urllib.request.urlopen(f"http://localhost:{PORT}/json").read())
page = next((t for t in tabs if t.get("type") == "page"), None)
if not page:
    print("No Chrome page tab found — is Chrome running with --remote-debugging-port=9222?")
    exit(1)

path = page["webSocketDebuggerUrl"].replace(f"ws://localhost:{PORT}", "")
sock = socket.create_connection(("localhost", PORT))
sock.settimeout(5.0)
key = base64.b64encode(os.urandom(16)).decode()
sock.send((
    f"GET {path} HTTP/1.1\r\n"
    f"Host: localhost:{PORT}\r\n"
    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\n\r\n"
).encode())
resp = b""
while b"\r\n\r\n" not in resp:
    resp += sock.recv(4096)

_buf = b""


def send(msg_id, method, params=None):
    msg = json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
    length = len(msg)
    if length < 126:
        header = bytes([0x81, 0x80 | length]) + mask
    else:
        header = bytes([0x81, 0xFE]) + struct.pack(">H", length) + mask
    sock.send(header + masked)


def recv_result(msg_id):
    """Block until the response with matching id arrives."""
    global _buf
    while True:
        while len(_buf) >= 2:
            b1 = _buf[1] & 0x7F
            offset = 2
            if b1 == 126:
                if len(_buf) < 4:
                    break
                length = struct.unpack(">H", _buf[2:4])[0]
                offset = 4
            elif b1 == 127:
                if len(_buf) < 10:
                    break
                length = struct.unpack(">Q", _buf[2:10])[0]
                offset = 10
            else:
                length = b1
            if len(_buf) < offset + length:
                break
            frame = _buf[offset:offset + length].decode("utf-8", errors="replace")
            _buf = _buf[offset + length:]
            try:
                msg = json.loads(frame)
            except Exception:
                continue
            if msg.get("id") == msg_id:
                return msg
        _buf += sock.recv(65536)


EXPR = """
JSON.stringify(Array.from(document.querySelectorAll('video')).map(v => ({
  id: v.id.replace('video-',''),
  t: Math.round(v.currentTime * 100) / 100,
  ready: v.readyState,
  paused: v.paused,
  w: v.videoWidth,
  h: v.videoHeight
})))
"""


def sample(msg_id):
    send(msg_id, "Runtime.evaluate", {"expression": EXPR, "returnByValue": True})
    res = recv_result(msg_id)
    return json.loads(res["result"]["result"]["value"])


send(1, "Runtime.enable")
recv_result(1)

s1 = {v["id"]: v for v in sample(2) if _UUID_RE.match(v["id"])}
time.sleep(GAP)
s2 = {v["id"]: v for v in sample(3)}
sock.close()

print(f"Sampled {len(s1)} video element(s), {GAP}s apart:\n")
all_live = True
for cid in s1:
    a, b = s1[cid], s2.get(cid, {})
    advanced = b.get("t", 0) > a.get("t", -1)
    live = advanced and not b.get("paused", True) and b.get("ready", 0) >= 2
    all_live = all_live and live
    flag = "LIVE " if live else "FROZEN"
    print(f"  [{flag}] {cid[:8]}  t:{a.get('t')}→{b.get('t')}  "
          f"ready={b.get('ready')} paused={b.get('paused')} "
          f"{b.get('w')}x{b.get('h')}")

print()
print("All feeds live." if all_live else "*** ONE OR MORE FEEDS FROZEN ***")
exit(0 if all_live else 1)
