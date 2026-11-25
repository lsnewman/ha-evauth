"""Charger Availability and the scan flow, as a pure state machine. No I/O and no real clock.

Inputs are scans, notification taps, switch and connector states and the time; outputs are
action dicts that the service carries out:
  {"type": "availability", "on": bool, "dry": bool}
  {"type": "notify", "target": ..., "title": ..., "message": ..., "data": {...}}
  {"type": "decision", "decision": Decision}      a latch change to record
  {"type": "log", "message": ...}

Availability goes ON only on an event (a scan, a latch becoming authorised, recovery after a
restart), so a manual "off" is respected until the next such event. It goes OFF on a condition:
nothing authorises, no window is open, and the connector is known to have nothing plugged in.
A plugged-in or running session is therefore never disabled.
"""
from __future__ import annotations

import secrets

from .engine import Engine
from .signals import PRESENT_STATES
from .model import UNKNOWN_ID

MODES = ("off", "dry_run", "live")
PICK_PREFIX = "EVAUTH_PICK"
NOTIFY_TAG = "evauth_scan"      # one scan's messages replace each other
ALERT_TAG = "evauth_alert"
ANDROID_MAX_ACTIONS = 3

CONFIRM_S = 30        # a switch command must be reflected in this long, or it is sent again
MAX_TRIES = 3         # then the owner is told, once
RETRY_AFTER_S = 600   # and from then on it is sent again this often
SETTLE_S = 30         # after connecting or changing mode, wait this long before switching off
RECOVER_S = 600       # how long after a restart an occupied zone may re-enable an authorised latch
UNREADABLE = (None, "", "unavailable", "unknown")


def readiness(cfg: dict, ha_ok: bool, mqtt_ok: bool, watchdog_state: str | None) -> list[dict]:
    """What must be true before live mode may be selected."""
    e, c = cfg["entities"], cfg["control"]
    user = any(u["allowed"] and u["phone_device_id"] and u["notify_target"] and u["default_car"] for u in cfg["users"])
    return [{"label": label, "ok": bool(ok)} for label, ok in (
        ("Charger Availability switch selected (Sources)", e["charger_availability"]),
        ("Charger connector status sensor selected (Sources)", e["charger_status"]),
        ("Charger tag selected (Settings)", c["tag_id"]),
        ("An allowed user with phone, notify target and default car (Users)", user),
        ("Home Assistant connected", ha_ok),
        ("MQTT connected (heartbeat)", mqtt_ok),
        ("Watchdog automation selected and on (Settings)", c["watchdog_automation"] and watchdog_state == "on"),
    )]


class ChargeController:
    def __init__(self, engine: Engine, state: dict | None = None, nonce=lambda: secrets.token_hex(4)):
        self.engine = engine
        self._nonce = nonce
        s = state or {}
        self.mode: str = s.get("mode", "off")
        self.pending: dict | None = s.get("pending")              # {"user", "nonce", "deadline"}
        self.window_until: float | None = s.get("window_until")   # on without a latch until then
        self.auth_car: str | None = s.get("auth_car")             # authorised car last seen, to spot changes
        self.sim: bool | None = s.get("sim")                      # dry run: the switch as it would be
        self.actual: bool | None = None                           # the real switch; None = unreadable
        self.connector: str | None = None
        self.cmd: dict | None = None                              # a real command not yet reflected
        self.want_on = False
        self.settle_until: float | None = None                    # None = not connected: never switch off
        self.recover_until: float | None = None

    # ------------------------------------------------------------ helpers
    @property
    def cfg(self) -> dict:
        return self.engine.cfg

    @property
    def switch(self) -> bool | None:
        return self.sim if self.mode == "dry_run" else self.actual

    @property
    def plugged_in(self) -> bool:
        return self.connector in PRESENT_STATES

    def _in_flight(self, on: bool) -> bool:
        """A live command to turn the switch `on` that is not yet reflected."""
        return self.mode == "live" and self.cmd is not None and self.cmd["on"] == on

    def _window_open(self, ts: float) -> bool:
        return self.window_until is not None and ts < self.window_until

    def _justified(self, ts: float) -> bool:
        return self.engine.authorised() or self.pending is not None or self._window_open(ts)

    def _off_allowed(self, ts: float) -> bool:
        return (self.settle_until is not None and ts >= self.settle_until and not self._justified(ts)
                and self.connector is not None and not self.plugged_in)

    def _user(self, uid: str) -> dict | None:
        return next((u for u in self.cfg["users"] if u["id"] == uid), None)

    def _user_for_device(self, device_id: str) -> dict | None:
        if not device_id:
            return None
        return next((u for u in self.cfg["users"] if u["phone_device_id"] == device_id), None)

    def _owner(self) -> dict | None:
        return next((u for u in self.cfg["users"] if u["notify_target"] and u["allowed"]), None)

    @staticmethod
    def _log(message: str) -> dict:
        return {"type": "log", "message": message}

    def _notify(self, user: dict | None, message: str, title: str = "EV charger", actions=None,
                tag: str = NOTIFY_TAG) -> list[dict]:
        if not user or not user["notify_target"]:
            return [self._log(f"no notify target for: {message}")]
        data: dict = {"tag": tag}
        if actions:
            data["actions"] = actions
            if user["platform"] == "android":
                data.update(ttl=0, priority="high")           # deliver at once, not on the next wake
        if self.mode == "dry_run":
            title = f"[dry run] {title}"
        return [{"type": "notify", "target": user["notify_target"], "title": title, "message": message, "data": data}]

    def _latched(self) -> str:
        latch = self.engine.latch
        return f"{self.engine.label(latch.car)} ({latch.source})"

    # ------------------------------------------------------------ mode
    def _sync(self, ts: float) -> list[dict]:
        new = self.cfg["control"]["mode"]
        if new == self.mode:
            return []
        old, self.mode = self.mode, new
        self.pending, self.window_until, self.cmd, self.want_on, self.recover_until = None, None, None, False, None
        self.sim = self.actual
        self.auth_car = self.engine.latch.car if self.engine.authorised() else None
        acts = [self._log(f"charging control: {old} -> {new}")]
        if old == "live":
            # Leaving live puts the charger back the way it was before this app controlled it.
            self.cmd = {"on": True, "ts": ts, "tries": 1, "gave_up": False, "restore": True}
            self.sim = True
            acts += [{"type": "availability", "on": True, "dry": False},
                     self._log("leaving live: Availability turned on")]
        if new != "off" and self.settle_until is not None:
            self.settle_until = ts + SETTLE_S
            self.recover_until = ts + RECOVER_S
        return acts

    # ------------------------------------------------------------ connection and states
    def reconnected(self, ts: float):
        """Home Assistant (re)connected, including at app start."""
        self.settle_until = ts + SETTLE_S
        self.recover_until = ts + RECOVER_S

    def disconnected(self):
        """States can no longer be trusted; act on nothing until they are read again."""
        self.actual, self.connector, self.settle_until = None, None, None

    def connector_state(self, state: str | None):
        self.connector = None if state in UNREADABLE else state

    def switch_state(self, ts: float, state: str | None) -> list[dict]:
        acts = self._sync(ts)
        new = {"on": True, "off": False}.get(state)
        prev, self.actual = self.actual, new
        ours = self.cmd is not None and self.cmd["on"] == new
        if ours:
            self.cmd = None
        if self.mode == "off" or new is None:
            return acts
        if self.mode == "dry_run" and new != prev and not ours:
            self.sim = new
        if prev is False and new is True and not ours:
            self.window_until = ts + self.cfg["policy"]["no_plugin_s"]
            acts.append(self._log("Availability turned on by someone else; kept on for the plug-in window"))
        return acts

    # ------------------------------------------------------------ scan and pick
    def scan(self, ts: float, device_id: str) -> list[dict]:
        acts = self._sync(ts)
        if self.mode == "off":
            return acts + [self._log("scan ignored: charging control is off")]
        user = self._user_for_device(device_id)
        if user is None or not user["allowed"]:
            acts.append(self._log(f"scan from unmapped or disallowed phone {device_id!r}; nothing authorised"))
            owner = self._owner()
            if owner:
                acts += self._notify(owner, "Charger scan from a phone that is not mapped to an allowed user; "
                                            "nothing was authorised.", tag=ALERT_TAG)
            return acts
        was_off = self.switch is not True
        self.window_until = ts + self.cfg["policy"]["no_plugin_s"]
        self.want_on = True
        hint = " If the car is already plugged in, unplug it and plug it in again." if was_off else ""
        if self.engine.authorised():
            self.pending = None
            acts.append(self._log(f"scan by {user['name']}: already authorised, {self._latched()}"))
            return acts + self._notify(user, f"Charging authorised for {self._latched()}.{hint}")
        timeout = self.cfg["policy"]["default_timeout_s"]
        nonce = self._nonce()
        self.pending = {"user": user["id"], "nonce": nonce, "deadline": ts + timeout}
        cars = sorted(self.cfg["cars"], key=lambda c: c["id"] != user["default_car"])
        if user["platform"] == "android":
            cars = cars[:ANDROID_MAX_ACTIONS]
        buttons = [{"action": f"{PICK_PREFIX}:{nonce}:{c['id']}", "title": c["name"]} for c in cars]
        standing = self.engine.latch.car
        if standing not in (None, UNKNOWN_ID):
            fallback = f"Looks like {self._latched()}. With no answer in {timeout} s, that car is authorised."
        elif user["default_car"]:
            fallback = f"With no answer in {timeout} s: {self.engine.label(user['default_car'])}."
        else:
            fallback = "You have no default car, so choose one."
        acts.append(self._log(f"scan by {user['name']}: picker sent"))
        return acts + self._notify(user, fallback + hint, "Charge which car?", buttons)

    def notification_action(self, ts: float, action: str) -> list[dict]:
        parts = str(action or "").split(":")
        if len(parts) != 3 or parts[0] != PICK_PREFIX:
            return []
        return self.pick(ts, parts[2], parts[1])

    def pick(self, ts: float, car_id: str, nonce: str | None = None) -> list[dict]:
        """A tap on the notification (with its nonce), or the panel picker (without)."""
        acts = self._sync(ts)
        if not self.pending or (nonce is not None and nonce != self.pending["nonce"]):
            return acts + [self._log("pick ignored: no scan is waiting for it")]
        if not any(c["id"] == car_id for c in self.cfg["cars"]):
            return acts + [self._log(f"pick ignored: {car_id!r} is not a configured car")]
        user = self._user(self.pending["user"])
        self.pending = None
        d = self.engine.apply("prompt", car_id, ts, "pick")
        acts.append({"type": "decision", "decision": d})
        return acts + self._result(user)

    def _result(self, user: dict | None) -> list[dict]:
        if self.engine.authorised():
            self.want_on = True
            return self._notify(user, f"Charging authorised for {self._latched()}.")
        return self._notify(user, f"Nothing is authorised (latch: {self._latched()}).")

    def _resolve_pending(self, ts: float) -> list[dict]:
        if not self.pending:
            return []
        user = self._user(self.pending["user"])
        if self.engine.authorised():
            self.pending = None
            return [self._log(f"recognised while waiting: {self._latched()}")] + \
                self._notify(user, f"Recognised {self._latched()}: charging authorised.")
        if ts < self.pending["deadline"]:
            return []
        self.pending = None
        standing = self.engine.latch.car
        if user is None or not user["allowed"]:
            return [self._log("picker timed out; the user is no longer allowed")]
        if standing not in (None, UNKNOWN_ID):                 # identified, only missing authority: the scan gives it
            d = self.engine.apply("nfc", standing, ts, "default_timeout")
        elif user["default_car"]:
            d = self.engine.apply("default", user["default_car"], ts, "default_timeout")
        else:
            return [self._log("picker timed out and the user has no default car")] + self._result(user)
        return [{"type": "decision", "decision": d}] + self._result(user)

    # ------------------------------------------------------------ the rules
    def update(self, ts: float) -> list[dict]:
        """Call after every input and about once a second."""
        acts = self._sync(ts)
        if self.mode == "off":
            return acts + self._retry(ts)
        acts += self._resolve_pending(ts)
        auth = self.engine.latch.car if self.engine.authorised() else None
        if auth and auth != self.auth_car:
            self.want_on = True
            acts.append(self._log(f"latch authorised: {self._latched()}"))
        self.auth_car = auth
        if self.recover_until is not None and self.settle_until is not None:
            if self.pending or self._window_open(ts) or (auth and self.engine.occupied is True):
                self.want_on, self.recover_until = True, None
            elif ts >= self.recover_until:
                self.recover_until = None
        if self.want_on:
            if not self._justified(ts):
                self.want_on = False
            elif self.switch is not None:
                self.want_on = False
                if self._in_flight(False) or (self.switch is False and not self._in_flight(True)):
                    acts += self._command(ts, True, "Availability on")
        if self.switch is not None and self._off_allowed(ts):
            if (self.switch is True or self._in_flight(True)) and not self._in_flight(False):
                acts += self._command(ts, False, "Availability off: nothing authorised and nothing plugged in")
        return acts + self._retry(ts)

    def _command(self, ts: float, on: bool, why: str) -> list[dict]:
        if self.mode == "dry_run":
            self.sim = on
            return [{"type": "availability", "on": on, "dry": True}, self._log(f"[dry run] would switch: {why}")]
        self.cmd = {"on": on, "ts": ts, "tries": 1, "gave_up": False, "restore": False}
        return [{"type": "availability", "on": on, "dry": False}, self._log(why)]

    def _retry(self, ts: float) -> list[dict]:
        c = self.cmd
        if c is None:
            return []
        if self.actual == c["on"]:
            self.cmd = None
            return []
        if self.actual is None or ts - c["ts"] < (RETRY_AFTER_S if c["gave_up"] else CONFIRM_S):
            return []
        still_wanted = c["restore"] or (self._justified(ts) if c["on"] else self._off_allowed(ts))
        if not still_wanted:
            self.cmd = None
            return []
        word = "on" if c["on"] else "off"
        if c["tries"] >= MAX_TRIES and not c["gave_up"]:
            c.update(gave_up=True, ts=ts)
            msg = f"The charger did not turn Availability {word} after {MAX_TRIES} tries."
            return [self._log(msg)] + self._notify(self._owner(), msg, tag=ALERT_TAG)
        c.update(ts=ts, tries=c["tries"] + 1)
        return [{"type": "availability", "on": c["on"], "dry": False},
                self._log(f"Availability {word} not confirmed; sent again")]

    # ------------------------------------------------------------ persistence and panel
    def state(self) -> dict:
        return {"mode": self.mode, "pending": self.pending, "window_until": self.window_until,
                "auth_car": self.auth_car, "sim": self.sim}

    def snapshot(self, ts: float) -> dict:
        user = self._user(self.pending["user"]) if self.pending else None
        return {"mode": self.mode, "actual": self.actual, "switch": self.switch, "connector": self.connector,
                "wanted": self._justified(ts) or self.plugged_in,
                "pending": {"user": user["name"] if user else "?", "seconds": max(0, round(self.pending["deadline"] - ts))}
                if self.pending else None,
                "window_seconds": round(self.window_until - ts) if self._window_open(ts) else 0,
                "command": dict(self.cmd) if self.cmd else None}
