"""The running app: wires inputs (Frigate over MQTT, HA states and events) to the engine, the charging
control, the store and the shadow compare.

The only writes to Home Assistant are the charging control's actions (the Availability switch and
notifications, never while its mode is off) and the heartbeat published over MQTT.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime

from .charging import ChargeController, readiness
from .engine import Engine
from .frigate import parse_event
from .model import Latch, Signal
from .sessions import SessionManager
from .settings import SettingsStore
from .shadow import Shadow
from .signals import charger_signal, gps_signal, occupancy_signal
from .store import Store

log = logging.getLogger("evauth")
VERSION = os.environ.get("EVAUTH_VERSION", "dev")

HEARTBEAT_S = 30
DISCOVERY_TOPIC = "homeassistant/sensor/evauth/heartbeat/config"
STATE_TOPIC = "evauth/heartbeat"
ATTR_TOPIC = "evauth/attributes"
MODE_TEXT = {"off": "shadow only (observes, controls nothing)",
             "dry_run": "dry run (nothing is switched) + shadow",
             "live": "live (controls charger Availability) + shadow"}


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
        self.shadow.engine = (self.engine.latch.car, self.engine.latch.source)   # restored latch, not "none"
        self.sessions = SessionManager(self.store, lambda: self.engine.latch, self.now)
        saved_charge = self.store.kv_get("charging")
        self.charge = ChargeController(self.engine, saved_charge)
        self._saved_charge = saved_charge
        self.outbox: queue.Queue = queue.Queue()      # actions for Home Assistant, sent outside the lock
        self._hb_at = 0.0
        self._hb_discovery = None
        self.ha = None
        self.mqtt = None
        self.ha_vals: dict[str, str | None] = {}
        self.seen = {"cameras": set(), "zones": set(), "classes": set()}   # learned from Frigate, for the pickers
        self.stats = {"frigate_seen": 0, "frigate_relevant": 0, "frigate_ignored": {}, "started": self.now()}
        self._stop = threading.Event()
        if dev:
            self.charge.reconnected(self.now())

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
        threading.Thread(target=self._send_loop, name="actions", daemon=True).start()

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
               e["charger_status"], e["charger_energy"], e["charger_availability"],
               self.cfg["control"]["watchdog_automation"]}
        out |= {c["gps_tracker"] for c in self.cfg["cars"]}
        return {x for x in out if x}

    def reload(self):
        """Call after settings change."""
        with self.lock:
            old = self.cfg
            old_mqtt = (old["mqtt"], old["frigate"]["topic"])
            self.cfg = self.settings.get()
            self.engine.set_config(self.cfg)
            self._maybe_seed(self.now())
            acts = []
            if old["entities"]["charger_availability"] != self.cfg["entities"]["charger_availability"]:
                acts = self.charge.switch_state(self.now(), None)
            if old["entities"]["charger_status"] != self.cfg["entities"]["charger_status"]:
                self.charge.connector_state(None)
            if old["control"]["watchdog_automation"] != self.cfg["control"]["watchdog_automation"]:
                self.ha_vals.pop("watchdog", None)
        if self.ha:
            self.ha.set_watch(self.watched())
        with self.lock:
            self._drive(acts)
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
            if signals:
                self._drive()

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
            acts = []
            if entity_id == e["charger_status"]:
                self.sessions.on_status(state)
                sig = charger_signal(state, now)
                if sig:
                    self._handle(sig)
                self.charge.connector_state(state)
            if entity_id == e["charger_availability"]:
                acts = self.charge.switch_state(now, state)
            if entity_id == self.cfg["control"]["watchdog_automation"]:
                self.ha_vals["watchdog"] = state
            if entity_id == e["charger_energy"]:
                try:
                    self.sessions.on_energy(float(state))
                except (TypeError, ValueError):
                    pass
            self._drive(acts)

    def on_ha_event(self, event_type: str, data: dict):
        with self.lock:
            tag = self.cfg["control"]["tag_id"]
            acts = []
            if event_type == "tag_scanned" and tag and data.get("tag_id") == tag:
                acts = self.charge.scan(self.now(), data.get("device_id") or "")
            elif event_type == "mobile_app_notification_action":
                acts = self.charge.notification_action(self.now(), data.get("action"))
            if acts:
                self._drive(acts)

    def on_ha_connect(self):
        with self.lock:
            self.charge.reconnected(self.now())
            self._hb_at = 0.0                      # beat at once: the watchdog is counting
            self._drive()

    def on_ha_disconnect(self):
        with self.lock:
            self.charge.disconnected()

    def panel_pick(self, car_id: str) -> bool:
        """The panel picker answering a scan that is waiting. False if none is."""
        with self.lock:
            if not self.charge.pending:
                return False
            self._drive(self.charge.pick(self.now(), car_id))
            return True

    # ------------------------------------------------------------ charging control
    def _drive(self, acts=()):
        """Carry out the controller's actions, then let it re-check its rules. Call with the lock held."""
        now = self.now()
        todo = list(acts) + self.charge.update(now)
        todo.sort(key=lambda a: a["type"] == "notify")      # stable: a switch command never waits for a notification
        for a in todo:
            if a["type"] == "decision":
                self._record([a["decision"]])
            elif a["type"] == "log":
                self._charge_note(a["message"])
            else:
                self.outbox.put(a)
        state = self.charge.state()
        if state != self._saved_charge:
            self.store.kv_set("charging", state)
            self._saved_charge = state

    def _charge_note(self, message: str):
        self.store.add_note(self.now(), "charging", message, "charging")
        log.info("charging: %s", message)

    def _send_loop(self):
        while not self._stop.is_set():
            try:
                self._perform(self.outbox.get(timeout=1.0))
            except queue.Empty:
                continue

    def flush(self):
        """Send everything queued, on this thread (tests)."""
        while not self.outbox.empty():
            self._perform(self.outbox.get_nowait())

    def _perform(self, a: dict):
        if not self.ha or (a["type"] == "availability" and a["dry"]):
            return
        try:
            if a["type"] == "availability":
                entity = self.cfg["entities"]["charger_availability"]
                if not entity:
                    return self._charge_note("no Availability switch is selected; nothing switched")
                self.ha.call_service("switch", "turn_on" if a["on"] else "turn_off", {"entity_id": entity})
            elif a["type"] == "notify":
                domain, service = a["target"].split(".", 1)
                self.ha.call_service(domain, service,
                                     {"title": a["title"], "message": a["message"], "data": a["data"]})
        except Exception as e:      # noqa: BLE001 - the controller notices an unconfirmed switch and retries
            self._charge_note(f"{a['type']} failed: {e}")

    def readiness(self, cfg: dict | None = None) -> list[dict]:
        cfg = cfg or self.cfg
        entity = cfg["control"]["watchdog_automation"]
        state = self.ha_vals.get("watchdog") if entity == self.cfg["control"]["watchdog_automation"] else None
        if entity and state is None and self.ha:      # picked just now: look it up
            try:
                state = next((s.get("state") for s in self.ha.states() if s["entity_id"] == entity), None)
            except Exception as e:      # noqa: BLE001
                log.warning("could not read the watchdog automation: %s", e)
        ha_ok = self.dev or bool(self.ha and self.ha.fresh())
        mqtt_ok = self.dev or bool(self.mqtt and self.mqtt.connected)
        return readiness(cfg, ha_ok, mqtt_ok, state)

    def heartbeat(self):
        """Tell Home Assistant the app is alive and able to act; its watchdog turns Availability off when
        this stops. Once charging control has been used it carries on in "off" mode, so that leaving live
        does not set the watchdog off."""
        if not self.mqtt or not self.ha or not self.ha.fresh():
            return
        now = self.now()
        with self.lock:
            timeout = self.cfg["policy"]["watchdog_timeout_s"]
            mode = self.cfg["control"]["mode"]
            latch = self.engine.latch
            attrs = {"mode": mode, "car": self.engine.label(latch.car), "source": latch.source,
                     "authorised": self.engine.authorised(), "scan_pending": self.charge.pending is not None}
        if now - self._hb_at < min(HEARTBEAT_S, timeout // 3):
            return
        if mode == "off" and not self.store.kv_get("heartbeat_used"):
            return
        key = (self.mqtt.generation, timeout)
        if key != self._hb_discovery:
            config = {"name": "Heartbeat", "unique_id": "evauth_heartbeat",
                      "default_entity_id": "sensor.evauth_heartbeat", "state_topic": STATE_TOPIC,
                      "json_attributes_topic": ATTR_TOPIC, "expire_after": timeout, "icon": "mdi:heart-pulse",
                      "device": {"identifiers": ["evauth"], "name": "EVAuth"}}
            if not self.mqtt.publish(DISCOVERY_TOPIC, json.dumps(config), retain=True):
                return
            self._hb_discovery = key
        self.mqtt.publish(ATTR_TOPIC, json.dumps(attrs))
        if self.mqtt.publish(STATE_TOPIC, "alive"):
            self._hb_at = now
            self.store.kv_set("heartbeat_used", True)

    def _ha_latch(self, entity_id: str, state, now: float):
        e = self.cfg["entities"]
        key = {e["ha_latch_vehicle"]: "vehicle", e["ha_latch_source"]: "source", e["ha_latch_at"]: "at"}[entity_id]
        self.ha_vals[key] = state
        if not self.cfg["shadow"]["enabled"]:
            return
        if key == "at":
            return
        self.shadow.update_ha(self.ha_vals.get("vehicle"), self.ha_vals.get("source"), now)
        self._maybe_seed(now)

    def _maybe_seed(self, now: float):
        """Until the engine has made a decision of its own (nothing persisted), mirror the HA latch once it
        maps to a configured car. Retried on every HA change and settings change, so setup order does not matter."""
        if (self.cfg["shadow"]["enabled"] and self.shadow.ha_seen and self.engine.latch.car is None
                and self.store.kv_get("latch") is None and self.ha_vals.get("vehicle")):
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
            # repeated "already latched" confirmations are routine; keep them out of the INFO log
            log.log(logging.DEBUG if d.rule.endswith(".same") else logging.INFO, "[%s] %s: %s", d.rule, d.action, d.reason)
            self.shadow.note_engine(self.engine.latch, d, d.ts)
            if d.action in ("set", "clear"):
                self.store.kv_set("latch", self.engine.latch.to_dict())
                self.sessions.on_latch()

    def tick(self):
        now = self.now()
        with self.lock:
            self._record(self.engine.tick(now))
            self._drive()
            if self.cfg["shadow"]["enabled"]:
                for ev in self.shadow.poll(now):
                    if ev["event"] == "opened":
                        log.warning("shadow: new %s disagreement #%s", ev["kind"], ev["id"])
        self.heartbeat()

    # ------------------------------------------------------------ for the panel
    def status(self) -> dict:
        with self.lock:
            snap = self.engine.snapshot()
            charging = self.charge.snapshot(self.now())
        ha = self.ha.status if self.ha else "dev (no Home Assistant)"
        mq = self.mqtt.status if self.mqtt else "dev (no MQTT)"
        return {"version": VERSION, "dev": self.dev, "now": self.now(), "engine": snap,
                "ha": ha, "mqtt": mq, "shadow": self.shadow.status(), "stats": self.stats,
                "ha_latch": dict(self.ha_vals), "warnings": self.settings.warnings,
                "setup_done": self.cfg["setup_done"], "mode": MODE_TEXT[self.cfg["control"]["mode"]],
                "charging": {**charging, "readiness": self.readiness(),
                             "recent": self.store.notes("charging", 15)}}
