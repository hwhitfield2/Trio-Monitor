"""Minimal Nightscout-compatible HTTP API.

Each configured user gets their own port running this server, so a Trio
instance can be pointed at ``http://<pi>:<port>`` as if it were a real
Nightscout site. Implements just enough of the v1 API for Trio's uploader:

    GET  /api/v1/status[.json]
    GET/POST  /api/v1/entries[.json], /api/v1/entries/sgv[.json]
    GET/POST/PUT  /api/v1/treatments[.json]
    DELETE /api/v1/treatments/<id>
    GET/POST  /api/v1/devicestatus[.json]
    GET/POST/PUT  /api/v1/profile[.json]   (accepted, stored in memory only)

Writes require the ``api-secret`` header (SHA-1 of the configured secret)
unless the user's secret is empty.
"""

import gzip
import hashlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, synclog
from .config import SCREEN_PNG
from .store import Store

log = logging.getLogger("trio_monitor.server")


class NightscoutServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, user: str, api_secret: str, store: Store):
        super().__init__((host, port), NightscoutHandler)
        self.user = user
        self.secret_hash = (
            hashlib.sha1(api_secret.encode()).hexdigest() if api_secret else None
        )
        self.store = store
        self.profile: list[dict] = []


class NightscoutHandler(BaseHTTPRequestHandler):
    server: NightscoutServer
    server_version = f"TrioMonitor/{__version__}"
    protocol_version = "HTTP/1.1"

    # ---- plumbing ----

    def log_message(self, fmt, *args):
        log.debug("[%s] %s", self.server.user, fmt % args)

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        if self.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        if not data:
            return None
        return json.loads(data)

    def _authorized(self) -> bool:
        expected = self.server.secret_hash
        if expected is None:
            return True
        provided = self.headers.get("api-secret", "")
        # Trio sends the SHA-1 hash; accept the plain secret hashed either way.
        return provided.lower() == expected or (
            hashlib.sha1(provided.encode()).hexdigest() == expected
        )

    def _route(self) -> tuple[str, dict]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path.endswith(".json"):
            path = path[: -len(".json")]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return path, query

    def _count(self, query: dict, default: int = 10) -> int:
        try:
            return max(1, min(int(query.get("count", default)), 1000))
        except ValueError:
            return default

    # ---- verbs ----

    def do_GET(self):
        path, query = self._route()
        store, user = self.server.store, self.server.user

        if path in ("/screen", "/screen.png"):
            try:
                with open(SCREEN_PNG, "rb") as f:
                    body = f.read()
            except OSError:
                self._send_json({"status": 404, "message": "No screenshot yet"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/api/v1/status", "/status"):
            self._send_json(
                {
                    "status": "ok",
                    "name": "trio-monitor",
                    "version": __version__,
                    "apiEnabled": True,
                    "careportalEnabled": True,
                    "settings": {"units": "mg/dl"},
                    "extendedSettings": {},
                }
            )
        elif path in ("/api/v1/entries", "/api/v1/entries/sgv"):
            self._send_json(store.get_entries(user, self._count(query)))
        elif path == "/api/v1/treatments":
            self._send_json(store.get_treatments(user, self._count(query)))
        elif path == "/api/v1/devicestatus":
            self._send_json(store.get_devicestatus(user, self._count(query)))
        elif path == "/api/v1/profile":
            self._send_json(self.server.profile)
        elif path in ("/api/v1/experiments/test", "/api/v1/verifyauth"):
            if self._authorized():
                self._send_json({"status": 200, "message": "OK"})
            else:
                self._send_json({"status": 401, "message": "Unauthorized"}, 401)
        else:
            self._send_json({"status": 404, "message": "Not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            self._send_json({"status": 401, "message": "Unauthorized"}, 401)
            return
        path, _ = self._route()
        store, user = self.server.store, self.server.user

        try:
            body = self._read_body()
        except (ValueError, OSError) as exc:
            self._send_json({"status": 400, "message": f"Bad body: {exc}"}, 400)
            return
        docs = body if isinstance(body, list) else [body] if body else []

        if path in ("/api/v1/entries", "/api/v1/entries/sgv"):
            stored = store.add_entries(user, docs)
            log.info("[%s] stored %d entries", user, len(stored))
            synclog.add("push", user, f"received {len(stored)} readings")
            self._send_json(stored)
        elif path == "/api/v1/treatments":
            stored = store.add_treatments(user, docs)
            log.info("[%s] stored %d treatments", user, len(stored))
            synclog.add("push", user, f"received {len(stored)} treatments")
            self._send_json(stored)
        elif path == "/api/v1/devicestatus":
            stored = store.add_devicestatus(user, docs)
            log.info("[%s] stored %d devicestatus", user, len(stored))
            synclog.add("push", user, f"received {len(stored)} statuses")
            self._send_json(stored)
        elif path == "/api/v1/profile":
            self.server.profile = docs
            self._send_json(docs)
        else:
            self._send_json({"status": 404, "message": "Not found"}, 404)

    def do_PUT(self):
        # Trio uses PUT for treatment/profile updates; upserts handle both.
        self.do_POST()

    def do_DELETE(self):
        if not self._authorized():
            self._send_json({"status": 401, "message": "Unauthorized"}, 401)
            return
        path, _ = self._route()
        prefix = "/api/v1/treatments/"
        if path.startswith(prefix):
            doc_id = path[len(prefix):]
            deleted = self.server.store.delete_treatment(self.server.user, doc_id)
            log.info("[%s] delete treatment %s -> %s", self.server.user, doc_id, deleted)
            self._send_json({"n": 1 if deleted else 0, "ok": 1})
        elif path == "/api/v1/treatments":
            # Bulk deletes by query are acknowledged but not applied.
            self._send_json({"n": 0, "ok": 1})
        else:
            self._send_json({"status": 404, "message": "Not found"}, 404)


def start_servers(users, store: Store, host: str = "0.0.0.0") -> list[NightscoutServer]:
    """Start one Nightscout server thread per configured user."""
    servers = []
    for user in users:
        server = NightscoutServer(host, user.port, user.name, user.api_secret, store)
        thread = threading.Thread(
            target=server.serve_forever, name=f"ns-{user.name}", daemon=True
        )
        thread.start()
        log.info("Listening for %s on port %d", user.name, user.port)
        servers.append(server)
    return servers


def stop_servers(servers) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()
