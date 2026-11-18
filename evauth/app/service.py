"""The running app: wires inputs (Frigate over MQTT, HA states) to the engine, store and shadow compare.

Phases 1 and 2 are observe-only: this class never calls a Home Assistant service and
never writes an HA entity. Decisions go to the app's own log and database.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime

from .engine import Engine
from .frigate import parse_event
from .model import Latch, Signal
from .sessions import SessionManager
from .settings import SettingsStore
from .shadow import Shadow
from .signals import gps_signal, occupancy_signal
from .store import Store

log = logging.getLogger("evauth")
VERSION = os.environ.get("EVAUTH_VERSION", "dev")


def _parse_ts(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class App:
    def __init__(self, data_dir: str, clock=time.time, dev: bool = False):
        os.makedirs(data_dir, exist_ok=True)
        self.dev = dev
        self._clock = clock
        self.offset = 0.0                       # dev only: simulated time passing
        self.lock = threading.RLock()
        self.settings = SettingsStore(os.path.join(data_dir, "config.json"))
        self.store = Store(os.path.join(data_dir, "evauth.db"))
        self.cfg = self.settings.get()
        saved = self.store.kv_get("latch")
        self.had_saved_latch = saved is not None
        self.engine = Engine(self.cfg, Latch(**saved) if saved else Latch())
        self.shadow = Shadow(self.store, lambda: self.cfg)
        self.sessions = SessionManager(self.store, lambda: self.engine.latch, self.now)
        self.ha = None
        self.mqtt = None
        self.ha_vals: dict[str, str | None] = {}
        self.seen = {"cameras": set(), "zones": set(), "classes": set()}   # learned from Frigate, for the pickers
        self.stats = {"frigate_seen": 0, "frigate_relevant": 0, "frigate_ignored": {}, "started": self.now()}
        self._stop = threading.Event()

    def now(self) -> float:
        return self._clock() + self.offset

    # ------------------------------------------------------------ lifecycle
    def start(self, ha=None, mqtt=None):
        self.ha, self.mqtt = ha, mqtt
        if self.ha:
            self.ha.set_watch(self.watched())
            self.ha.start()
        if self.mqtt:
            self.mqtt.start()
        threading.Thread(target=self._loop, name="engine-tick", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self.ha:
            self.ha.stop()
        if self.mqtt:
            self.mqtt.stop()

    def _loop(self):
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception:       # noqa: BLE001 - the loop must survive
                log.exception("tick failed")

    def watched(self) -> set[str]:
        e = self.cfg["entities"]
        out = {e["occupancy_sensor"], e["ha_latch_vehicle"], e["ha_latch_source"], e["ha_latch_at"],
               e["charger_status"], e["charger_energy"]}
        out |= {c["gps_tracker"] for c in self.cfg["cars"]}
        return {x for x in out if x}

    def reload(self):
        """Call after settings change."""
        with self.lock:
            old_mqtt = (self.cfg["mqtt"], self.cfg["frigate"]["topic"])
            self.cfg = self.settings.get()
            self.engine.set_config(self.cfg)
        if self.ha:
            self.ha.set_watch(self.watched())
        if self.mqtt and (self.cfg["mqtt"], self.cfg["frigate"]["topic"]) != old_mqtt:
            self.mqtt.restart()

    # ------------------------------------------------------------ inputs
    def on_frigate(self, raw: bytes | str | dict):
        payload = raw if isinstance(raw, dict) else json.loads(raw)
        now = self.now()
        with self.lock:
            self._learn(payload)
            self.stats["frigate_seen"] += 1
            signals, why = parse_event(payload, self.cfg["frigate"], now)
            if why:
                ig = self.stats["frigate_ignored"]
                ig[why] = ig.get(why, 0) + 1
                return
            if signals:
                self.stats["frigate_relevant"] += 1
            for s in signals:
                self._handle(s)

    def _learn(self, payload: dict):
        after = payload.get("after") if isinstance(payload, dict) else None
        if not isinstance(after, dict):
            return
        if after.get("camera"):
            self.seen["cameras"].add(str(after["camera"]))
        for z in (after.get("current_zones") or []) + (after.get("entered_zones") or []):
            self.seen["zones"].add(str(z))
        v = after.get(self.cfg["frigate"]["classifier_attribute"])
        if isinstance(v, (list, tuple)) and v:
            self.seen["classes"].add(str(v[0]))
        for k in self.seen:                      # bounded, so a noisy install cannot grow it forever
            if len(self.seen[k]) > 200:
                self.seen[k] = set(sorted(self.seen[k])[:200])

    def on_stats(self, raw: bytes | str | dict):
        data = raw if isinstance(raw, dict) else json.loads(raw)
        with self.lock:
            for cam in (data.get("cameras") or {}):
                self.seen["cameras"].add(str(cam))

    def on_state(self, entity_id: str, st: dict):
        now = self.now()
        e = self.cfg["entities"]
        state = st.get("state")
        with self.lock:
            if entity_id == e["occupancy_sensor"]:
                sig = occupancy_signal(state, now)
                if sig:
                    self._handle(sig)
            for car in self.cfg["cars"]:
                if car["gps_tracker"] == entity_id:
                    self._handle(gps_signal(car["id"], st, e["gps_zone"], now))
            if entity_id in (e["ha_latch_vehicle"], e["ha_latch_source"], e["ha_latch_at"]):
                self._ha_latch(entity_id, state, now)
            if entity_id == e["charger_status"]:
                self.sessions.on_status(state)
            if entity_id == e["charger_energy"]:
                try:
                    self.sessions.on_energy(float(state))
                except (TypeError, ValueError):
                    pass

    def _ha_latch(self, entity_id: str, state, now: float):
        e = self.cfg["entities"]
        key = {e["ha_latch_vehicle"]: "vehicle", e["ha_latch_source"]: "source", e["ha_latch_at"]: "at"}[entity_id]
        self.ha_vals[key] = state
        if not self.cfg["shadow"]["enabled"]:
            return
        if key == "at":
            return
        first = self.shadow.update_ha(self.ha_vals.get("vehicle"), self.ha_vals.get("source"), now)
        # No saved engine latch yet: adopt the HA one once so the comparison is meaningful.
        if first and not self.had_saved_latch and self.engine.latch.car is None and self.ha_vals.get("vehicle"):
            self._seed_from_ha(now)

    def _seed_from_ha(self, now: float):
        car, src = self.shadow._ha_pair()
        if car and not str(car).startswith("?") and src in (None, "lpr", "gps", "classifier", "nfc", "prompt", "default"):
            tier = self.cfg["sources"].get(src or "", {}).get("tier")
            self.engine.latch = Latch(car=car, source=src, tier=tier,
                                      set_at=_parse_ts(self.ha_vals.get("at")) or now)
            self.store.add_note(now, "startup", f"engine latch seeded from the HA helpers: {car} ({src})", "seed")
            self.store.kv_set("latch", self.engine.latch.to_dict())
            self.shadow.note_engine(self.engine.latch, None, now)

    # ------------------------------------------------------------ engine plumbing
    def _handle(self, sig: Signal):
        self._record(self.engine.handle(sig))

    def _record(self, decisions):
        for d in decisions:
            self.store.add_event(d)
            log.info("[%s] %s: %s", d.rule, d.action, d.reason)
            self.shadow.note_engine(self.engine.latch, d, d.ts)
            if d.action in ("set", "clear"):
                self.store.kv_set("latch", self.engine.latch.to_dict())
                self.sessions.on_latch()

    def tick(self):
        now = self.now()
        with self.lock:
            self._record(self.engine.tick(now))
            if self.cfg["shadow"]["enabled"]:
                for ev in self.shadow.poll(now):
                    if ev["event"] == "opened":
                        log.warning("shadow: new %s disagreement #%s", ev["kind"], ev["id"])

    # ------------------------------------------------------------ for the panel
    def status(self) -> dict:
        with self.lock:
            snap = self.engine.snapshot()
        ha = self.ha.status if self.ha else "dev (no Home Assistant)"
        mq = self.mqtt.status if self.mqtt else "dev (no MQTT)"
        return {"version": VERSION, "dev": self.dev, "now": self.now(), "engine": snap,
                "ha": ha, "mqtt": mq, "shadow": self.shadow.status(), "stats": self.stats,
                "ha_latch": dict(self.ha_vals), "warnings": self.settings.warnings,
                "setup_done": self.cfg["setup_done"], "mode": "shadow (observe only)"}
