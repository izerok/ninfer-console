#!/usr/bin/env python3
"""ninfer-console restarter — the smallest possible Docker lever.

This sidecar exists so the console panel can make engine config
changes take effect (the engine reads its configuration only at
startup) without the panel ever touching the Docker socket. Its
entire capability surface:

  GET  /healthz  -> is Docker reachable, does the target container
                    exist, and what state is it in?
  POST /restart  -> restart exactly ONE container, named by
                    RESTARTER_CONTAINER, resolved fresh by exact name
                    on every call. There is no other route, no other
                    verb, and no way to name a different container.

Auth: when RESTARTER_TOKEN is set, every request needs
"Authorization: Bearer <token>". The service is meant to live only
on a compose-internal network — never publish its port.

Stdlib only.
"""
import http.client
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCK = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
TARGET = os.environ.get("RESTARTER_CONTAINER", "")
TOKEN = os.environ.get("RESTARTER_TOKEN", "")
PORT = int(os.environ.get("RESTARTER_PORT", "8080"))
RESTART_TIMEOUT = os.environ.get("RESTARTER_TIMEOUT", "30")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker(method, path, timeout=15):
    conn = UnixHTTPConnection(SOCK, timeout=timeout)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def find_target():
    """(id, state) of the container named exactly TARGET, or
    (None, "") — the Docker name filter is loose, so the exact
    match is made here, in code, every time."""
    status, body = docker("GET", "/containers/json?all=1")
    if status != 200:
        raise RuntimeError(f"docker list returned {status}")
    for item in json.loads(body or "[]"):
        if f"/{TARGET}" in (item.get("Names") or []):
            return item.get("Id"), item.get("State") or ""
    return None, ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return not TOKEN or \
            self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path != "/healthz":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._auth_ok():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            cid, state = find_target()
            self._send(200, {"ok": True, "container": TARGET,
                             "found": bool(cid), "state": state})
        except Exception as exc:
            self._send(503, {"ok": False, "error": str(exc)})

    def do_POST(self):
        if self.path != "/restart":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._auth_ok():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            cid, _state = find_target()
            if not cid:
                self._send(404, {"ok": False, "container": TARGET,
                                 "error": "container not found"})
                return
            status, body = docker(
                "POST", f"/containers/{cid}/restart?t={RESTART_TIMEOUT}",
                timeout=120)
            if status == 204:
                self._send(200, {"ok": True, "container": TARGET,
                                 "restarted": True})
            else:
                self._send(502, {"ok": False,
                                 "error": f"docker restart returned "
                                          f"{status}: {body[:200]}"})
        except Exception as exc:
            self._send(502, {"ok": False, "error": str(exc)})


if __name__ == "__main__":
    if not TARGET:
        raise SystemExit("RESTARTER_CONTAINER is required")
    print(f"restarter: target={TARGET} port={PORT} "
          f"auth={'on' if TOKEN else 'off'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
