"""HTTP transport for contract v1 (stdlib only: works on the Windows lab PC without extra packages).

One server per module (default 127.0.0.1:8781 exfoliation, 8782 stacking), plus the debug page server
(8780) that serves the page and forwards ``/m/<module>/api/...`` to the same service objects in-process.
Errors are ``{"error": "..."}`` with 400 / 403 / 404 / 409 / 415 / 503 (ADR 0001).
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import CLIENT_HEADER, ApiError, ContractService

CLIENT_RE = re.compile(r"^[\x21-\x7e][\x20-\x7e]{0,127}$")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
IMAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def handle_api(svc: ContractService, method: str, path: str, query: dict, headers, body: bytes,
               client_addr: str) -> tuple[int, dict | bytes, str]:
    """Route one contract request. Returns (status, json-or-bytes, content type)."""
    loopback = is_loopback(client_addr)
    if method == "GET":
        if path == "/api/describe":
            return 200, svc.describe(), "application/json"
        if path == "/api/state":
            return 200, svc.state(), "application/json"
        if path == "/api/events":
            try:
                since = int((query.get("since") or ["0"])[0])
            except ValueError:
                raise ApiError(400, "since must be an integer") from None
            return 200, svc.get_events(since), "application/json"
        m = re.match(r"^/api/images/([^/]+)$", path)
        if m:  # [Q] extension: fetch a captured image (debug page)
            image_id = m.group(1)
            if not IMAGE_ID_RE.match(image_id):
                raise ApiError(400, "bad image id")
            cap_dir = svc.module.config.runs_dir / "captures"
            for ext, ctype in (("jpg", "image/jpeg"), ("png", "image/png")):
                p = cap_dir / f"{image_id}.{ext}"
                if p.is_file():
                    return 200, p.read_bytes(), ctype
            raise ApiError(404, "no such image")
        raise ApiError(404, f"no route GET {path}")
    if method != "POST":
        raise ApiError(404, f"no route {method} {path}")
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise ApiError(415, "POST needs Content-Type: application/json")
    client = headers.get(CLIENT_HEADER) or ""
    if not CLIENT_RE.match(client):
        raise ApiError(400, f"missing or invalid {CLIENT_HEADER} header (1-128 printable ASCII)")
    try:
        payload = json.loads(body.decode("utf-8") or "{}") if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "body must be a JSON object")
    if path == "/api/command":
        return 202, svc.command(payload, client, loopback), "application/json"
    if path == "/api/stop":
        return 200, svc.stop(wait_s=float(payload.get("wait_s", 0) or 0), reason=f"api_stop by {client}",
                             heat_off=bool(payload.get("heat_off", False))), "application/json"
    if path == "/api/heartbeat":
        return 200, svc.heartbeat(client), "application/json"
    if path == "/api/renew":
        return 200, svc.renew(client), "application/json"
    if path == "/api/release":
        return 200, svc.release(client), "application/json"
    if path == "/api/unlock":
        return 200, svc.unlock(client, loopback), "application/json"
    raise ApiError(404, f"no route POST {path}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "qumus-lab/0.1"
    services: dict[str, ContractService] = {}
    single: ContractService | None = None
    serve_web = False

    def log_message(self, fmt, *args):  # quiet; events go to the journal
        pass

    def _send(self, status: int, payload, ctype: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("application/json", "text/")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            svc = self.single
            if svc is None:
                m = re.match(r"^/m/([a-z_]+)(/api/.*)$", path)
                if m and m.group(1) in self.services:
                    svc, path = self.services[m.group(1)], m.group(2)
                elif self.serve_web and method == "GET":
                    return self._static(path)
                else:
                    raise ApiError(404, f"no route {method} {path}")
            status, payload, ctype = handle_api(svc, method, path, query, self.headers, body, self.client_address[0])
            self._send(status, payload, ctype)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"internal: {type(exc).__name__}: {exc}"})

    def _static(self, path: str) -> None:
        if path == "/modules.json":
            return self._send(200, {"modules": sorted(self.services)})
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")


def make_server(host: str, port: int, *, service: ContractService | None = None,
                services: dict[str, ContractService] | None = None, web: bool = False) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"single": service, "services": dict(services or {}), "serve_web": web})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve_in_thread(srv: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=srv.serve_forever, daemon=True, name=f"http-{srv.server_address[1]}")
    t.start()
    return t
