"""Panel served through Home Assistant ingress: a JSON API plus one static page.

HA handles login and `panel_admin: true` limits the panel to admins; requests are only
accepted from the Supervisor's ingress proxy (unless EVAUTH_DEV is set).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import options as opts
from .model import UNKNOWN_ID
from .settings import ConfigError

log = logging.getLogger("evauth.web")
INGRESS_IPS = {"172.30.32.2"}
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
MAX_BODY = 2 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _ts(v, default=None):
    """Epoch seconds from a number or an ISO string."""
    if v in (None, ""):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ApiError(400, f"bad time {v!r}") from None


class Api:
    def __init__(self, app):
        self.app = app

    # settings
    def get_settings(self):
        return self.app.settings.get()

    def put_settings(self, body):
        try:
            self.app.settings.replace(body)
        except ConfigError as e:
            raise ApiError(400, str(e)) from None
        self.app.reload()
        return self.app.settings.get()

    def patch_settings(self, section, body):
        try:
            self.app.settings.update_section(section, body.get("value") if "value" in body else body)
        except ConfigError as e:
            raise ApiError(400, str(e)) from None
        self.app.reload()
        return self.app.settings.get()

    def import_settings(self, body):
        try:
            self.app.settings.import_doc(body)
        except ConfigError as e:
            raise ApiError(400, str(e)) from None
        self.app.reload()
        return self.app.settings.get()

    def entities(self, domains):
        if not self.app.ha:
            return {"entities": [], "error": "Home Assistant is not connected (dev mode or not running as an app)"}
        try:
            return {"entities": self.app.ha.list_entities(tuple(domains) if domains else None)}
        except Exception as e:      # noqa: BLE001 - shown to the user
            return {"entities": [], "error": str(e)}

    def options(self):
        with self.app.lock:
            seen = {k: set(v) for k, v in self.app.seen.items()}
        return opts.build(self.app.ha, seen, self.app.dev)

    # sessions
    def sessions(self, limit=200):
        return {"sessions": self.app.store.sessions(limit), "ledger": self.app.sessions.ledger(self.app.cfg["cars"]),
                "open": self.app.store.open_session()}

    def _car_ok(self, car_id):
        if car_id and car_id != UNKNOWN_ID and not any(c["id"] == car_id for c in self.app.cfg["cars"]):
            raise ApiError(400, f"no car {car_id!r}")

    def create_session(self, body):
        self._car_ok(body.get("car_id"))
        now = self.app.now()
        kwh = body.get("kwh")
        sid = self.app.store.create_session(_ts(body.get("start"), now), body.get("car_id") or None, "manual",
                                            "manual", note=str(body.get("note", "")),
                                            kwh=float(kwh) if kwh not in (None, "") else None,
                                            end=_ts(body.get("end")))
        return {"id": sid}

    def patch_session(self, sid, body):
        if not self.app.store.get_session(sid):
            raise ApiError(404, "no such session")
        if "car_id" in body:
            self._car_ok(body["car_id"])
            self.app.sessions.allocate(sid, body["car_id"], str(body.get("note", "")))
        fields = {}
        if "kwh" in body:
            fields["kwh"] = float(body["kwh"]) if body["kwh"] not in (None, "") else None
        if "note" in body and "car_id" not in body:
            fields["note"] = str(body["note"])
        if "end" in body:
            fields["end"] = _ts(body["end"])
        if fields:
            self.app.store.update_session(sid, **fields)
        return self.app.store.get_session(sid)

    def delete_session(self, sid):
        self.app.store.delete_session(sid)
        return {"ok": True}

    def pick(self, body):
        """The phase-1 picker: allocate the open (or latest) session to a car. Never touches the charger."""
        car = body.get("car_id")
        self._car_ok(car)
        if not car:
            raise ApiError(400, "choose a car")
        s = self.app.store.open_session() or next(iter(self.app.store.sessions(1)), None)
        if not s:
            raise ApiError(409, "there is no session to allocate yet")
        self.app.sessions.allocate(s["id"], car, "picked in the panel")
        self.app.store.add_note(self.app.now(), "pick", f"session {s['id']} allocated to {car} by the picker", "pick")
        return {"session": s["id"], "car_id": car}

    # logs
    def events(self, limit, action):
        return {"events": self.app.store.events(limit, action or None), "history": self.app.store.latch_history(50)}

    def disagreements(self, status):
        return {"disagreements": self.app.store.disagreements(status or None),
                "counts": self.app.store.disagreement_counts()}

    def annotate(self, did, body):
        try:
            self.app.store.annotate_disagreement(did, body.get("status", "open"), str(body.get("note", "")))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        return {"ok": True}

    # dev only
    def dev_frigate(self, body):
        self.app.on_frigate(body)
        return self.app.status()

    def dev_state(self, body):
        self.app.on_state(body["entity_id"], {"state": body.get("state"), "attributes": body.get("attributes") or {},
                                              "entity_id": body["entity_id"]})
        return self.app.status()

    def dev_advance(self, body):
        self.app.offset += float(body.get("seconds", 0))
        self.app.tick()
        return self.app.status()


ROUTES = [
    ("GET", r"/api/state", "state"), ("GET", r"/api/settings", "get_settings"),
    ("PUT", r"/api/settings", "put_settings"), ("PATCH", r"/api/settings/(?P<section>[a-z_]+)", "patch_settings"),
    ("GET", r"/api/settings/export", "export"), ("POST", r"/api/settings/import", "import_settings"),
    ("GET", r"/api/entities", "entities"), ("GET", r"/api/options", "options"),
    ("GET", r"/api/sessions", "sessions"), ("POST", r"/api/sessions", "create_session"),
    ("PATCH", r"/api/sessions/(?P<sid>[a-f0-9]{12})", "patch_session"),
    ("DELETE", r"/api/sessions/(?P<sid>[a-f0-9]{12})", "delete_session"), ("POST", r"/api/pick", "pick"),
    ("GET", r"/api/events", "events"), ("GET", r"/api/disagreements", "disagreements"),
    ("PATCH", r"/api/disagreements/(?P<did>\d+)", "annotate"),
    ("POST", r"/api/dev/frigate", "dev_frigate"), ("POST", r"/api/dev/state", "dev_state"),
    ("POST", r"/api/dev/advance", "dev_advance"),
]


def call(api: Api, name: str, kw: dict, query: dict, body: dict):
    q = lambda k, d=None: (query.get(k) or [d])[0]      # noqa: E731
    if name == "state":
        return api.app.status()
    if name == "get_settings":
        return api.get_settings()
    if name == "put_settings":
        return api.put_settings(body)
    if name == "patch_settings":
        return api.patch_settings(kw["section"], body)
    if name == "export":
        return api.app.settings.export()
    if name == "import_settings":
        return api.import_settings(body)
    if name == "entities":
        return api.entities([d for d in (q("domains", "") or "").split(",") if d])
    if name == "options":
        return api.options()
    if name == "sessions":
        return api.sessions(int(q("limit", 200)))
    if name == "create_session":
        return api.create_session(body)
    if name == "patch_session":
        return api.patch_session(kw["sid"], body)
    if name == "delete_session":
        return api.delete_session(kw["sid"])
    if name == "pick":
        return api.pick(body)
    if name == "events":
        return api.events(int(q("limit", 200)), q("action"))
    if name == "disagreements":
        return api.disagreements(q("status"))
    if name == "annotate":
        return api.annotate(int(kw["did"]), body)
    if name.startswith("dev_"):
        if not api.app.dev:
            raise ApiError(404, "not found")
        return getattr(api, name)(body)
    raise ApiError(404, "not found")


def make_server(api: Api, port: int, allow_all: bool = False) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "EVAuth"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.client_address[0], *args)

        def _send(self, status, body: bytes, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj, extra=None):
            self._send(status, json.dumps(obj).encode(), "application/json", extra)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "request too large")
            if not n:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                raise ApiError(400, "invalid JSON") from None
            if not isinstance(body, (dict, list)):
                raise ApiError(400, "expected JSON")
            return body

        def _dispatch(self, method):
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            if not allow_all and self.client_address[0] not in INGRESS_IPS:
                return self._send(403, b"forbidden", "text/plain")
            if method == "GET" and path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            for m, pattern, name in ROUTES:
                match = re.fullmatch(pattern, path)
                if m == method and match:
                    break
            else:
                return self._json(404, {"error": "not found"})
            try:
                body = self._body() if method in ("POST", "PUT", "PATCH") else {}
                out = call(api, name, match.groupdict(), query, body)
                extra = {"Content-Disposition": 'attachment; filename="evauth-settings.json"'} \
                    if name == "export" else None
                return self._json(200, out, extra)
            except ApiError as e:
                return self._json(e.status, {"error": str(e)})
            except KeyError as e:
                return self._json(400, {"error": f"missing {e}"})
            except Exception:       # noqa: BLE001
                log.exception("request failed: %s %s", method, path)
                return self._json(500, {"error": "internal error"})

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
        def do_PUT(self): self._dispatch("PUT")
        def do_PATCH(self): self._dispatch("PATCH")
        def do_DELETE(self): self._dispatch("DELETE")

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    return srv
