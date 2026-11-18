"""Read-only Home Assistant client through the Supervisor proxy.

One websocket: get_states once, then state_changed events, filtered to the entities
the app watches. It never calls a service or sets a state. Failures are logged and
retried; they never stop the app.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import requests

log = logging.getLogger("evauth.ha")
WS_URL = "ws://supervisor/core/websocket"
REST_URL = "http://supervisor/core/api"


class HAClient:
    def __init__(self, on_state, ws_url: str = WS_URL, rest_url: str = REST_URL, token: str | None = None):
        self.on_state = on_state                  # (entity_id, state_obj) -> None
        self.ws_url, self.rest_url = ws_url, rest_url
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self.watch: set[str] = set()
        self.status = "starting"
        self._stop = threading.Event()
        self._ws = None
        self._cache: tuple[float, list] | None = None

    def start(self):
        threading.Thread(target=self._run, name="ha-ws", daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:       # noqa: BLE001
            pass

    def set_watch(self, entities):
        new = {e for e in entities if e}
        added = new - self.watch
        self.watch = new
        if added:                                # prime newly watched entities
            threading.Thread(target=self._prime, args=(added,), daemon=True).start()

    def _prime(self, entities):
        try:
            for s in self.states():
                if s["entity_id"] in entities:
                    self.on_state(s["entity_id"], s)
        except Exception as e:      # noqa: BLE001
            log.debug("prime failed: %s", e)

    # ------------------------------------------------------------ REST (read only)
    def states(self) -> list[dict]:
        if self._cache and time.time() - self._cache[0] < 20:
            return self._cache[1]
        r = requests.get(f"{self.rest_url}/states", headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        self._cache = (time.time(), data)
        return data

    def list_entities(self, domains: tuple[str, ...] | None = None) -> list[dict]:
        out = []
        for s in self.states():
            dom = s["entity_id"].split(".")[0]
            if domains and dom not in domains:
                continue
            out.append({"entity_id": s["entity_id"], "state": s.get("state"),
                        "name": (s.get("attributes") or {}).get("friendly_name", "")})
        return sorted(out, key=lambda e: e["entity_id"])

    def services(self) -> list[dict]:
        r = requests.get(f"{self.rest_url}/services", headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
        r.raise_for_status()
        return r.json()

    def device_registry(self) -> list[dict]:
        """One read-only websocket command (config/device_registry/list)."""
        import websocket
        ws = websocket.create_connection(self.ws_url, timeout=15)
        try:
            ws.recv()
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("authentication refused")
            ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    break
            if not msg.get("success"):
                raise RuntimeError(f"device list refused ({(msg.get('error') or {}).get('code')})")
            return msg.get("result") or []
        finally:
            ws.close()

    # ------------------------------------------------------------ websocket loop
    def _run(self):
        backoff = 3
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 3
            except Exception as e:      # noqa: BLE001 - keep retrying
                self.status = f"disconnected: {e}"
                log.warning("HA connection lost: %s", e)
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 60)

    def _session(self):
        import websocket
        if not self.token:
            self.status = "no SUPERVISOR_TOKEN (not running inside Home Assistant)"
            self._stop.wait(30)
            return
        ws = websocket.create_connection(self.ws_url, timeout=30)
        self._ws = ws
        try:
            if json.loads(ws.recv()).get("type") != "auth_required":
                raise RuntimeError("unexpected greeting")
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("authentication refused")
            ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
            ws.send(json.dumps({"id": 2, "type": "get_states"}))
            self.status = "connected"
            ws.settimeout(90)
            while not self._stop.is_set():
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    ws.send(json.dumps({"id": 3, "type": "ping"}))
                    continue
                if msg.get("id") == 2 and msg.get("success"):
                    for s in msg.get("result") or []:
                        if s["entity_id"] in self.watch:
                            self.on_state(s["entity_id"], s)
                elif msg.get("type") == "event":
                    new = (msg.get("event", {}).get("data") or {}).get("new_state")
                    if new and new["entity_id"] in self.watch:
                        self.on_state(new["entity_id"], new)
        finally:
            self._ws = None
            ws.close()
