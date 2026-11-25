"""All EVAuth settings: one versioned document in /data/config.json, edited in the panel.

There are deliberately no app options (Configuration tab). Loading never fails: a
bad or missing value falls back to its default and is reported as a warning, so a
setting can never stop the app from starting. Saves are atomic and keep the
previous copy as config.json.bak. Old documents are upgraded by MIGRATIONS, so a
new version never needs user action.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading

from .charging import MODES
from .model import SOURCES, TIERS, UNKNOWN_ID, UNKNOWN_NAME

log = logging.getLogger("evauth.settings")

SCHEMA_VERSION = 1
LOG_LEVELS = ("debug", "info", "warning", "error")
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
SLUG_RE = re.compile(r"^[a-z0-9_]{1,40}$")


class ConfigError(ValueError):
    """A value the panel should show to the user."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:40]


# ---------------------------------------------------------------- defaults

def _src(tier, min_score, empty, unknown, fresh, stale, refresh, authorises):
    return {"tier": tier, "min_score": min_score, "over_empty": empty, "over_unknown": unknown,
            "over_known_fresh": fresh, "over_known_stale": stale, "refresh_same": refresh,
            "authorises": authorises}


# The inventory's override table, as data. `lpr` Unknown reads only ever fill an empty
# latch (fixed in code), whatever these say. `classifier` can never authorise (fixed).
# `authorises` means "counts as a valid latch": it turns Availability on and keeps it on.
DEFAULT_SOURCES = {
    "lpr":        _src("confident",     0.75, True, True, True,  True,  True,  True),
    "gps":        _src("confident",     0.0,  True, True, False, True,  False, True),
    "classifier": _src("non_confident", 0.8,  True, True, False, False, False, False),
    "nfc":        _src("confident",     0.0,  True, True, True,  True,  False, True),
    "prompt":     _src("confident",     0.0,  True, True, True,  True,  False, True),
    "default":    _src("non_confident", 0.0,  True, True, False, False, False, True),
}
SOURCE_KEYS = tuple(DEFAULT_SOURCES["lpr"])

# key: (type, min, max, default)
POLICY_SPEC = {
    "stale_after_s": (int, 0, 86400, 300),        # a latch older than this counts as stale
    "empty_clear_s": (int, 1, 86400, 300),        # zone empty this long -> clear
    "gps_leave_clear_s": (int, 1, 86400, 120),    # tracker away from home this long -> clear
    "default_timeout_s": (int, 5, 600, 90),       # picker unanswered -> default car
    "no_plugin_s": (int, 60, 7200, 600),          # after a scan, Availability stays on this long without a latch
    "restart_grace_s": (int, 0, 3600, 180),       # after a restart / lost sensor, ignore "empty" this long (Frigate re-detects), then the normal clear time applies
    "shadow_settle_s": (int, 0, 120, 5),          # wait before comparing with the HA latch
    "watchdog_timeout_s": (int, 30, 3600, 120),   # heartbeat silence before HA turns Availability off
    "charger_holds_latch": (bool, 0, 1, True),    # a car plugged in or charging keeps its latch
    "upgrade_same_car": (bool, 0, 1, True),       # a confident source confirming the latched car upgrades its source
}

DEFAULT_ENTITIES = {
    # HA entities (all optional; empty = not used)
    "occupancy_sensor": "", "gps_zone": "",
    "ha_latch_vehicle": "", "ha_latch_source": "", "ha_latch_at": "",   # shadow comparison, read only
    "charger_status": "", "charger_availability": "", "charger_energy": "",
    "charger_charge_control": "",
}
DEFAULT_FRIGATE = {
    "topic": "frigate/events", "camera": "", "test_camera": "", "accept_test_camera": False,
    "zone": "", "label": "car", "classifier_attribute": "Car Identity",
    "trust_sub_label": True,        # fall back to Frigate's own sub_label when no plate pattern matches
}
# mode: off = shadow only; dry_run = whole scan flow, switch commands only logged; live = switches Availability
DEFAULT_CONTROL = {"mode": "off", "tag_id": "", "watchdog_automation": ""}
DEFAULT_COLOURS = {"none": "off", "occupied_unknown": "red", "non_confident": "amber", "confident": "green"}


def default_doc() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "log_level": "info",
        "setup_done": False,
        "entities": dict(DEFAULT_ENTITIES),
        "frigate": dict(DEFAULT_FRIGATE),
        "mqtt": {"host": "", "port": 1883, "username": "", "password": ""},   # blank = Supervisor discovery
        "policy": {k: v[3] for k, v in POLICY_SPEC.items()},
        "sources": copy.deepcopy(DEFAULT_SOURCES),
        "colours": dict(DEFAULT_COLOURS),
        "cars": [],
        "users": [],
        "shadow": {"enabled": True},
        "control": dict(DEFAULT_CONTROL),
    }


# ---------------------------------------------------------------- validators (raise ConfigError)

def _num(key, value, typ, lo, hi):
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a number") from None
    if typ is int:
        if f != int(f):
            raise ConfigError(f"{key} must be a whole number")
        f = int(f)
    if not lo <= f <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return f


def _bool(key, value):
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{key} must be true or false")


def _entity(key, value):
    v = str(value or "").strip()
    if v and not ENTITY_RE.match(v):
        raise ConfigError(f"{key} must look like domain.name (for example sensor.foo)")
    return v


def validate_policy(p: dict) -> dict:
    if not isinstance(p, dict):
        raise ConfigError("policy must be an object")
    out = {}
    for k, (typ, lo, hi, d) in POLICY_SPEC.items():
        out[k] = _bool(k, p.get(k, d)) if typ is bool else _num(k, p.get(k, d), typ, lo, hi)
    return out


def validate_sources(s: dict) -> dict:
    if not isinstance(s, dict):
        raise ConfigError("sources must be an object")
    out = {}
    for name in SOURCES:
        cur = {**DEFAULT_SOURCES[name], **(s.get(name) or {})}
        tier = cur["tier"]
        if tier not in TIERS:
            raise ConfigError(f"{name}: tier must be confident or non_confident")
        o = {"tier": tier, "min_score": _num(f"{name}.min_score", cur["min_score"], float, 0.0, 1.0)}
        for k in SOURCE_KEYS[2:]:
            o[k] = _bool(f"{name}.{k}", cur[k])
        if name == "classifier":
            o["authorises"] = False         # fixed in code: the classifier never authorises
        out[name] = o
    return out


def validate_entities(e: dict) -> dict:
    if not isinstance(e, dict):
        raise ConfigError("entities must be an object")
    return {k: _entity(k, e.get(k, d)) for k, d in DEFAULT_ENTITIES.items()}


def validate_frigate(f: dict) -> dict:
    if not isinstance(f, dict):
        raise ConfigError("frigate must be an object")
    out = {}
    for k, d in DEFAULT_FRIGATE.items():
        v = f.get(k, d)
        if isinstance(d, bool):
            out[k] = _bool(k, v)
        else:
            v = str(v or "").strip()
            if k in ("topic", "label", "classifier_attribute") and not v:
                raise ConfigError(f"{k} cannot be empty")
            out[k] = v
    return out


def validate_mqtt(m: dict) -> dict:
    if not isinstance(m, dict):
        raise ConfigError("mqtt must be an object")
    return {"host": str(m.get("host", "") or "").strip(),
            "port": _num("mqtt.port", m.get("port", 1883) or 1883, int, 1, 65535),
            "username": str(m.get("username", "") or ""), "password": str(m.get("password", "") or "")}


def validate_control(c: dict) -> dict:
    if not isinstance(c, dict):
        raise ConfigError("control must be an object")
    mode = c.get("mode", "off")
    if mode not in MODES:
        raise ConfigError(f"control mode must be one of {', '.join(MODES)}")
    return {"mode": mode, "tag_id": str(c.get("tag_id", "") or "").strip(),
            "watchdog_automation": _entity("watchdog_automation", c.get("watchdog_automation", ""))}


def validate_colours(c: dict) -> dict:
    allowed = ("off", "red", "amber", "green", "blue", "white")
    out = {}
    for k, d in DEFAULT_COLOURS.items():
        v = (c or {}).get(k, d)
        if v not in allowed:
            raise ConfigError(f"colour for {k} must be one of {', '.join(allowed)}")
        out[k] = v
    return out


def _patterns(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    if not isinstance(value, (list, tuple)):
        raise ConfigError("plate patterns must be a list")
    out = []
    for p in value:
        p = re.sub(r"[^A-Za-z0-9*?\[\]]", "", str(p)).upper()
        if p:
            out.append(p)
    return out


def validate_cars(cars) -> list[dict]:
    if not isinstance(cars, list):
        raise ConfigError("cars must be a list")
    out, ids, names = [], set(), set()
    for c in cars:
        name = str(c.get("name", "")).strip()
        if not name:
            raise ConfigError("every car needs a name")
        if name.lower() in ("none", UNKNOWN_NAME.lower()):
            raise ConfigError(f"'{name}' is reserved")
        cid = c.get("id") or slugify(name)
        if not SLUG_RE.match(cid) or cid == UNKNOWN_ID:
            raise ConfigError(f"car id {cid!r} is not allowed")
        if cid in ids or name.lower() in names:
            raise ConfigError(f"car {name!r} appears twice")
        ids.add(cid); names.add(name.lower())
        out.append({"id": cid, "name": name, "plate_patterns": _patterns(c.get("plate_patterns", [])),
                    "frigate_class": str(c.get("frigate_class", "") or "").strip(),
                    "gps_tracker": _entity("gps_tracker", c.get("gps_tracker", "")),
                    "energy_ledger": _entity("energy_ledger", c.get("energy_ledger", "")),
                    "opening_kwh": _num("opening_kwh", c.get("opening_kwh", 0) or 0, float, 0, 1_000_000)})
    return out


def validate_users(users, car_ids: set[str]) -> list[dict]:
    if not isinstance(users, list):
        raise ConfigError("users must be a list")
    out, ids = [], set()
    for u in users:
        name = str(u.get("name", "")).strip()
        if not name:
            raise ConfigError("every user needs a name")
        uid = u.get("id") or slugify(name)
        if not SLUG_RE.match(uid) or uid in ids:
            raise ConfigError(f"user id {uid!r} is not allowed or repeated")
        ids.add(uid)
        dc = str(u.get("default_car", "") or "")
        if dc and dc not in car_ids:
            raise ConfigError(f"{name}: default car {dc!r} is not a defined car")
        plat = u.get("platform", "ios")
        if plat not in ("ios", "android"):
            raise ConfigError("platform must be ios or android")
        nt = str(u.get("notify_target", "") or "").strip()
        if nt and not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", nt):
            raise ConfigError("notify target must look like notify.mobile_app_name")
        out.append({"id": uid, "name": name, "phone_device_id": str(u.get("phone_device_id", "") or "").strip(),
                    "notify_target": nt, "default_car": dc,
                    "allowed": _bool("allowed", u.get("allowed", True)), "platform": plat})
    return out


def validate_doc(doc: dict) -> dict:
    """Strict: raise ConfigError on the first problem (used when the panel saves)."""
    out = default_doc()
    out["log_level"] = doc.get("log_level", "info")
    if out["log_level"] not in LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
    out["setup_done"] = bool(doc.get("setup_done", False))
    out["entities"] = validate_entities(doc.get("entities", {}))
    out["frigate"] = validate_frigate(doc.get("frigate", {}))
    out["mqtt"] = validate_mqtt(doc.get("mqtt", {}))
    out["policy"] = validate_policy(doc.get("policy", {}))
    out["sources"] = validate_sources(doc.get("sources", {}))
    out["colours"] = validate_colours(doc.get("colours", {}))
    out["control"] = validate_control(doc.get("control", {}))
    out["cars"] = validate_cars(doc.get("cars", []))
    out["users"] = validate_users(doc.get("users", []), {c["id"] for c in out["cars"]})
    out["shadow"] = {"enabled": _bool("shadow.enabled", (doc.get("shadow") or {}).get("enabled", True))}
    return out


def sanitise(doc) -> tuple[dict, list[str]]:
    """Lenient: keep what is valid, default the rest, and say what was dropped."""
    warnings: list[str] = []
    base = default_doc()
    if not isinstance(doc, dict):
        return base, ["config.json was not an object; defaults used"]
    out = base
    simple = {"log_level": lambda v: v if v in LOG_LEVELS else (_ for _ in ()).throw(ConfigError("bad log level")),
              "setup_done": bool}
    for k, fn in simple.items():
        if k in doc:
            try:
                out[k] = fn(doc[k])
            except Exception as e:      # noqa: BLE001
                warnings.append(f"{k}: {e}; default used")
    sections = {"entities": validate_entities, "frigate": validate_frigate, "mqtt": validate_mqtt,
                "policy": validate_policy, "sources": validate_sources, "colours": validate_colours,
                "control": validate_control}
    for k, fn in sections.items():
        try:
            out[k] = fn(doc.get(k, {}))
        except ConfigError as e:
            warnings.append(f"{k}: {e}; defaults used for the whole section")
            # keep valid individual keys where we can
            if isinstance(doc.get(k), dict):
                for kk, vv in doc[k].items():
                    try:
                        out[k] = fn({**out[k], kk: vv})
                    except ConfigError:
                        pass
    good_cars = []
    for c in doc.get("cars", []) if isinstance(doc.get("cars"), list) else []:
        try:
            good_cars = validate_cars(good_cars + [c])
        except (ConfigError, AttributeError) as e:
            warnings.append(f"car {c.get('name', '?') if isinstance(c, dict) else '?'} dropped: {e}")
    out["cars"] = good_cars
    good_users = []
    ids = {c["id"] for c in good_cars}
    for u in doc.get("users", []) if isinstance(doc.get("users"), list) else []:
        try:
            good_users = validate_users(good_users + [u], ids)
        except (ConfigError, AttributeError) as e:
            warnings.append(f"user {u.get('name', '?') if isinstance(u, dict) else '?'} dropped: {e}")
    out["users"] = good_users
    try:
        out["shadow"] = {"enabled": _bool("shadow.enabled", (doc.get("shadow") or {}).get("enabled", True))}
    except ConfigError:
        pass
    return out, warnings


# ---------------------------------------------------------------- migrations

def _m_none(doc):       # placeholder pattern: MIGRATIONS[n] upgrades a version-n doc to n+1
    return doc


MIGRATIONS: dict[int, callable] = {}


def migrate(doc: dict) -> dict:
    v = doc.get("schema_version", 1) if isinstance(doc, dict) else 1
    if not isinstance(v, int) or v < 1:
        v = 1
    while v < SCHEMA_VERSION:
        doc = MIGRATIONS[v](doc)
        v += 1
        doc["schema_version"] = v
    return doc


# ---------------------------------------------------------------- store

class SettingsStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.warnings: list[str] = []
        self.doc = default_doc()
        self.load()

    def load(self):
        with self._lock:
            raw = None
            for p in (self.path, self.path + ".bak"):
                try:
                    with open(p, encoding="utf-8") as f:
                        raw = json.load(f)
                    if p != self.path:
                        self.warnings.append("config.json was unreadable; restored from config.json.bak")
                    break
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as e:
                    self.warnings.append(f"{os.path.basename(p)} unreadable ({e})")
            if raw is None:
                self.doc = default_doc()
                return
            try:
                raw = migrate(raw)
            except Exception as e:      # noqa: BLE001 - never stop the app
                self.warnings.append(f"migration failed ({e}); defaults used")
                self.doc = default_doc()
                return
            self.doc, w = sanitise(raw)
            self.warnings += w
            for line in self.warnings:
                log.warning("settings: %s", line)

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.doc)

    def replace(self, doc: dict) -> dict:
        """Validate strictly, then save atomically. Raises ConfigError."""
        with self._lock:
            new = validate_doc(doc)
            self._write(new)
            self.doc = new
            return copy.deepcopy(new)

    def update_section(self, name: str, value) -> dict:
        with self._lock:
            if name not in self.doc or name == "schema_version":
                raise ConfigError(f"unknown settings section {name!r}")
            cur = copy.deepcopy(self.doc)
            if isinstance(cur[name], dict) and isinstance(value, dict):
                cur[name] = {**cur[name], **value}
            else:
                cur[name] = value
            return self.replace(cur)

    def _write(self, doc: dict):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            try:
                os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def export(self) -> dict:
        d = self.get()
        d["mqtt"]["password"] = ""
        return d

    def import_doc(self, doc: dict) -> dict:
        doc = migrate(doc)
        doc["mqtt"] = {**doc.get("mqtt", {}), "password": self.doc["mqtt"].get("password", "")}
        return self.replace(doc)
