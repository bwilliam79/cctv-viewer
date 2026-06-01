#!/usr/bin/env python3
"""Reload the CCTV Viewer Chrome kiosk tab via the DevTools Protocol.

Usage:
    ssh bwwilliams@media-server "~/reload-chrome.sh"

Requires Chrome to be running with --remote-debugging-port=9222.
"""
import json, socket, os, base64
import urllib.request

PORT = 9222

tabs = json.loads(urllib.request.urlopen(f"http://localhost:{PORT}/json").read())
page = next((t for t in tabs if t.get("type") == "page"), None)
if not page:
    print("No Chrome page tab found — is Chrome running with --remote-debugging-port=9222?")
    exit(1)

path = page["webSocketDebuggerUrl"].replace(f"ws://localhost:{PORT}", "")
sock = socket.create_connection(("localhost", PORT))
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

msg = json.dumps({"id": 1, "method": "Page.reload", "params": {}}).encode()
mask = os.urandom(4)
masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
sock.send(bytes([0x81, 0x80 | len(msg)]) + mask + masked)
sock.close()
print("Chrome reloaded.")
