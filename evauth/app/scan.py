"""Phase 4 scan flow as a pure state machine (designed and tested now, wired later).

It never talks to HA. Inputs are scans, picks and ticks; outputs are Action dicts
that a later phase will carry out: {"type": "availability", "on": bool},
{"type": "picker", "user": id}, {"type": "notify", "target": ..., "message": ...},
{"type": "log", "message": ...}.
Rules (from the invariants): availability goes on at scan; an unmapped or
disallowed phone authorises nothing; with no valid latch and no plug-in within
no_plugin_s, availability goes back off; a valid latch is never disabled.
"""
from __future__ import annotations

from .engine import Engine
from .model import Decision


def watchdog_expired(last_heartbeat: float | None, now: float, timeout_s: float) -> bool:
    """HA-side watchdog model: no heartbeat (ever, or within timeout_s) means turn Availability off."""
    return last_heartbeat is None or now - last_heartbeat > timeout_s


class ScanFlow:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.availability = False
        self.pending: dict | None = None          # {"user", "deadline"}
        self.no_plugin_deadline: float | None = None
        self.decisions: list[Decision] = []

    @property
    def cfg(self) -> dict:
        return self.engine.cfg

    def _user_for_device(self, device_id: str) -> dict | None:
        if not device_id:
            return None
        return next((u for u in self.cfg["users"] if u["phone_device_id"] == device_id), None)

    def _owner(self) -> dict | None:
        return next((u for u in self.cfg["users"] if u["notify_target"] and u["allowed"]), None)

    def scan(self, ts: float, device_id: str) -> list[dict]:
        user = self._user_for_device(device_id)
        if user is None or not user["allowed"]:
            acts = [{"type": "log", "message": f"scan from unmapped or disallowed phone {device_id!r}; nothing authorised"}]
            owner = self._owner()
            if owner:
                acts.append({"type": "notify", "target": owner["notify_target"],
                             "message": "Charger scan from a phone that is not mapped to a user; nothing was authorised."})
            return acts
        acts: list[dict] = []
        if not self.availability:
            self.availability = True
            acts.append({"type": "availability", "on": True})
        if self.engine.authorised():
            acts.append({"type": "log", "message": f"scan by {user['name']}: valid latch, no change"})
            return acts
        self.pending = {"user": user["id"], "deadline": ts + self.cfg["policy"]["default_timeout_s"]}
        self.no_plugin_deadline = ts + self.cfg["policy"]["no_plugin_s"]
        acts.append({"type": "picker", "user": user["id"], "platform": user["platform"]})
        return acts

    def pick(self, ts: float, car_id: str) -> list[dict]:
        if not self.pending:
            return [{"type": "log", "message": "pick ignored: no scan is waiting"}]
        self.pending = None
        d = self.engine.apply("prompt", car_id, ts, "pick")
        self.decisions.append(d)
        if self.engine.authorised():
            self.no_plugin_deadline = None
        return [{"type": "log", "message": d.reason}]

    def tick(self, ts: float, plugged_in: bool = False) -> list[dict]:
        acts: list[dict] = []
        if self.pending and ts >= self.pending["deadline"]:
            user = next((u for u in self.cfg["users"] if u["id"] == self.pending["user"]), None)
            self.pending = None
            car = user["default_car"] if user else ""
            if car:
                d = self.engine.apply("default", car, ts, "default_timeout")
                self.decisions.append(d)
                acts.append({"type": "log", "message": d.reason})
                if self.engine.authorised():
                    self.no_plugin_deadline = None
            else:
                acts.append({"type": "log", "message": "picker timed out and the user has no default car"})
        if self.no_plugin_deadline is not None:
            if plugged_in or self.engine.authorised():
                self.no_plugin_deadline = None
            elif ts >= self.no_plugin_deadline:
                self.no_plugin_deadline = None
                self.pending = None
                if self.availability:
                    self.availability = False
                    acts.append({"type": "availability", "on": False})
                    acts.append({"type": "log", "message": "no plug-in and no valid latch; Availability off"})
        return acts
