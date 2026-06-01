#!/usr/bin/env python3
"""Stream Chrome console, network errors, and navigation events via the DevTools Protocol.

Usage:
    ssh bwwilliams@media-server "python3 ~/scripts/monitor-chrome.py [seconds]"

Defaults to 120 seconds. Ctrl-C to stop early.
Requires Chrome to be running with --remote-debugging-port=9222.
"""
import json, socket, os, base64, struct, time, sys
import urllib.request

PORT = 9222
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 120

tabs = json.loads(urllib.request.urlopen(f"http://localhost:{PORT}/json").read())
page = next((t for t in tabs if t.get("type") == "page"), None)
if not page:
    print("No page tab found")
    exit(1)

path = page["webSocketDebuggerUrl"].replace(f"ws://localhost:{PORT}", "")
sock = socket.create_connection(("localhost", PORT))
sock.settimeout(1.0)

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


send(1, "Runtime.enable")
send(2, "Log.enable")
send(3, "Page.enable")
send(4, "Network.enable")

print(f"[monitor] Listening for {DURATION}s — Ctrl-C to stop\n", flush=True)
deadline = time.time() + DURATION
buf = b""

while time.time() < deadline:
    try:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        while len(buf) >= 2:
            b1 = buf[1] & 0x7F
            offset = 2
            if b1 == 126:
                if len(buf) < 4:
                    break
                length = struct.unpack(">H", buf[2:4])[0]
                offset = 4
            elif b1 == 127:
                if len(buf) < 10:
                    break
                length = struct.unpack(">Q", buf[2:10])[0]
                offset = 10
            else:
                length = b1
            if len(buf) < offset + length:
                break
            frame = buf[offset:offset + length].decode("utf-8", errors="replace")
            buf = buf[offset + length:]

            try:
                msg = json.loads(frame)
            except Exception:
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})
            ts = time.strftime("%H:%M:%S")

            if method == "Runtime.consoleAPICalled":
                level = params.get("type", "log").upper()
                args = params.get("args", [])
                text = " ".join(a.get("value", a.get("description", "")) for a in args)
                print(f"[{ts}] CONSOLE.{level}: {text}", flush=True)

            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                url = entry.get("url", "")
                text = entry.get("text", "")
                print(f"[{ts}] LOG.{entry.get('level','?').upper()}: {text} | url={url}", flush=True)

            elif method == "Network.responseReceived":
                r = params.get("response", {})
                status = r.get("status", 0)
                if status >= 400:
                    print(f"[{ts}] NETWORK.{status}: {r.get('url','')}", flush=True)

            elif method == "Network.loadingFailed":
                print(f"[{ts}] NETWORK.FAILED: {params.get('errorText','')} | url={params.get('requestId','')}", flush=True)

            elif method == "Page.frameNavigated":
                frame_data = params.get("frame", {})
                if frame_data.get("parentId") is None:
                    print(f"[{ts}] *** PAGE NAVIGATED (reload) → {frame_data.get('url','')}", flush=True)

            elif method == "Page.frameStartedLoading":
                print(f"[{ts}] *** FRAME STARTED LOADING", flush=True)

    except socket.timeout:
        continue
    except KeyboardInterrupt:
        break
    except Exception as e:
        print(f"[error] {e}", flush=True)
        break

sock.close()
print("\n[monitor] Done.", flush=True)
