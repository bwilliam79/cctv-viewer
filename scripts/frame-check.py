#!/usr/bin/env python3
"""Distinguish a decode freeze from a compositor freeze: sample each grid
video's decoded pixels (canvas readback) twice and report whether the frame
content changes, alongside whether the currentTime clock advances."""
import json, socket, os, base64, struct
import urllib.request

PORT = 9222
tabs = json.loads(urllib.request.urlopen(f"http://localhost:{PORT}/json").read())
page = next((t for t in tabs if t.get("type") == "page"), None)
path = page["webSocketDebuggerUrl"].replace(f"ws://localhost:{PORT}", "")
sock = socket.create_connection(("localhost", PORT)); sock.settimeout(12.0)
key = base64.b64encode(os.urandom(16)).decode()
sock.send((f"GET {path} HTTP/1.1\r\nHost: localhost:{PORT}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
resp = b""
while b"\r\n\r\n" not in resp:
    resp += sock.recv(4096)
_buf = b""


def send(mid, method, params=None):
    msg = json.dumps({"id": mid, "method": method, "params": params or {}}).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
    n = len(msg)
    hdr = bytes([0x81, 0x80 | n]) + mask if n < 126 else bytes([0x81, 0xFE]) + struct.pack(">H", n) + mask
    sock.send(hdr + masked)


def recv(mid):
    global _buf
    while True:
        while len(_buf) >= 2:
            b1 = _buf[1] & 0x7F; off = 2
            if b1 == 126:
                if len(_buf) < 4: break
                ln = struct.unpack(">H", _buf[2:4])[0]; off = 4
            elif b1 == 127:
                if len(_buf) < 10: break
                ln = struct.unpack(">Q", _buf[2:10])[0]; off = 10
            else:
                ln = b1
            if len(_buf) < off + ln: break
            frame = _buf[off:off+ln].decode("utf-8", "replace"); _buf = _buf[off+ln:]
            try: m = json.loads(frame)
            except Exception: continue
            if m.get("id") == mid: return m
        _buf += sock.recv(65536)


send(1, "Runtime.enable"); recv(1)

EXPR = r"""(async () => {
  const vids = Array.from(document.querySelectorAll('video')).filter(v => /^video-[0-9a-f]{8}-/.test(v.id));
  const hash = (v) => {
    const c = document.createElement('canvas'); c.width=40; c.height=24;
    const ctx = c.getContext('2d', {willReadFrequently:true});
    try { ctx.drawImage(v, 0, 0, 40, 24); } catch(e){ return 'drawErr:'+e.name; }
    let d; try { d = ctx.getImageData(0,0,40,24).data; } catch(e){ return 'readErr:'+e.name; }
    let s=0; for (let i=0;i<d.length;i+=4){ s=(s*31 + d[i]+d[i+1]*7+d[i+2]*13)>>>0; }
    return s;
  };
  const s1 = vids.map(v => ({id:v.id.slice(6,14), t:v.currentTime, h:hash(v)}));
  await new Promise(r=>setTimeout(r,3000));
  const s2 = vids.map(v => ({id:v.id.slice(6,14), t:v.currentTime, h:hash(v)}));
  return JSON.stringify(s1.map((a,i)=>({
    id:a.id,
    clockAdvanced: Math.round((s2[i].t - a.t)*100)/100,
    frameChanged: a.h !== s2[i].h,
    h1:a.h, h2:s2[i].h
  })));
})()"""

send(2, "Runtime.evaluate", {"expression": EXPR, "returnByValue": True, "awaitPromise": True})
r = recv(2)
val = r.get("result", {}).get("result", {}).get("value")
print(val if val else json.dumps(r))
sock.close()
