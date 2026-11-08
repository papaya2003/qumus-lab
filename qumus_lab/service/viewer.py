"""View-only page for watching the bench from another computer (default 0.0.0.0:8790).

It serves the three live camera pictures and a read-only status summary, and nothing else:
- only GET is answered; every other method gets 405, so no command, stop, lease or unlock is reachable;
- the module APIs (``/m/<module>/api``) do not exist here; control stays on the loopback debug page (8780);
- every request needs the access key, from ``?key=...`` (then kept in a cookie) or the cookie. The key is
  generated once and kept in ``<runs_dir>/viewer-key.txt`` (not in git); delete the file to make a new one.

Plain HTTP: anyone on the same network who captures the traffic can see the key and the pictures. Use it
only on the university network or VPN.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import ContractService
from .http import CAM_RE, WEB_DIR, camera_frame, cameras_view

VIEWER_FILES = {"/": "viewer.html", "/viewer.js": "viewer.js", "/viewer.css": "viewer.css"}
COOKIE = "qumus_view"


def load_or_create_key(runs_dir: Path) -> str:
    path = Path(runs_dir) / "viewer-key.txt"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 16:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(18)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    return key


def lan_address() -> str:
    """This PC's address on the network that leads outside (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def status_view(services: dict[str, ContractService]) -> dict:
    """Read-only summary per module (no lease is taken, nothing is sent to an instrument)."""
    out = {}
    for name, svc in services.items():
        st = svc.state()
        out[name] = {k: st.get(k) for k in ("demo", "connected", "busy", "busy_action", "locked", "fault",
                                             "lease", "last_command", "devices", "zones", "ts_unix")}
    return {"modules": out}


class _ViewerHandler(BaseHTTPRequestHandler):
    server_version = "qumus-lab-viewer/0.1"
    services: dict[str, ContractService] = {}
    key = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, payload, ctype: str = "application/json", headers: dict | None = None) -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("application/json", "text/")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _cookie_key(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        given = (parse_qs(url.query).get("key") or [""])[0]
        if given and hmac.compare_digest(given, self.key):
            # keep the key in a cookie and drop it from the address bar
            return self._send(303, b"", "text/plain", {
                "Location": path or "/",
                "Set-Cookie": f"{COOKIE}={self.key}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"})
        if not hmac.compare_digest(self._cookie_key(), self.key):
            return self._send(403, b"Access key missing or wrong. Open the link with ?key=... that the lab PC "
                                   b"prints at start (also in runs\\real-lab\\viewer-url.txt).", "text/plain")
        try:
            if path in VIEWER_FILES:
                target = WEB_DIR / VIEWER_FILES[path]
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), ctype)
            if path == "/cameras.json":
                return self._send(200, cameras_view(self.services))
            if path == "/status.json":
                return self._send(200, status_view(self.services))
            m = CAM_RE.match(path)
            if m:
                return self._send(*camera_frame(self.services, m.group(1)))
            return self._send(404, {"error": "not found (this page is view-only)"})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"internal: {type(exc).__name__}: {exc}"})

    def _refuse(self):
        self._send(405, {"error": "view-only page: use the debug page on the lab PC to control instruments"},
                   headers={"Allow": "GET"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815


def make_viewer(host: str, port: int, services: dict[str, ContractService], key: str) -> ThreadingHTTPServer:
    if len(key) < 16:
        raise ValueError("viewer key too short")
    handler = type("ViewerHandler", (_ViewerHandler,), {"services": dict(services), "key": key})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv
