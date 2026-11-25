"""Test rig: a synthetic two-car config, a fake clock and fixture playback."""
import json

from app import settings
from app.charging import ChargeController
from app.engine import Engine
from app.frigate import parse_event
from app.signals import gps_signal, occupancy_signal
from conftest import FIXTURES

ZONE_ENTITY = "zone.test_carport"


def make_cfg(**over):
    doc = settings.default_doc()
    doc["entities"].update(occupancy_sensor="binary_sensor.test_occupancy", gps_zone=ZONE_ENTITY)
    doc["frigate"].update(camera="test_cam", zone="test_zone")
    doc["cars"] = settings.validate_cars([
        {"name": "Alpha", "plate_patterns": ["W*B[QDO0]123", "WBQ123"], "frigate_class": "Alpha",
         "gps_tracker": "device_tracker.alpha_location"},
        {"name": "Bravo", "plate_patterns": ["XYZ49[5S]"], "frigate_class": "Bravo"},
    ])
    ids = {c["id"] for c in doc["cars"]}
    doc["users"] = settings.validate_users([
        {"name": "Ann", "phone_device_id": "dev_ann", "notify_target": "notify.mobile_app_ann",
         "default_car": "alpha", "platform": "ios"},
        {"name": "Ben", "phone_device_id": "dev_ben", "notify_target": "notify.mobile_app_ben",
         "default_car": "bravo", "allowed": False},
    ], ids)
    for k, v in over.items():
        doc[k] = {**doc[k], **v} if isinstance(doc[k], dict) else v
    return settings.validate_doc(doc)


class Rig:
    def __init__(self, **over):
        self.cfg = make_cfg(**over)
        self.engine = Engine(self.cfg)
        self.now = 1_000_000.0
        self.log = []

    def advance(self, s):
        self.now += s
        return self.tick()

    def tick(self):
        d = self.engine.tick(self.now)
        self.log += d
        return d

    def _run(self, signals):
        out = []
        for s in signals:
            out += self.engine.handle(s)
        self.log += out
        return out

    def frigate(self, name):
        payload = json.loads((FIXTURES / "frigate" / f"{name}.json").read_text())
        sigs, why = parse_event(payload, self.cfg["frigate"], self.now)
        return self._run(sigs)

    def gps(self, name, car="alpha"):
        obj = json.loads((FIXTURES / "gps" / f"{name}.json").read_text())
        return self._run([gps_signal(car, obj, ZONE_ENTITY, self.now)])

    def occupancy(self, state):
        s = occupancy_signal(state, self.now)
        return self._run([s] if s else [])

    def apply(self, source, car):
        d = self.engine.apply(source, car, self.now)
        self.log.append(d)
        return d

    @property
    def car(self):
        return self.engine.latch.car

    @property
    def source(self):
        return self.engine.latch.source


class Flow(Rig):
    """A Rig with the charging control on top. It plays the charger: real switch commands are
    reflected back as switch states unless `obey` is False."""

    def __init__(self, mode="live", switch="off", connector="Available", saved=None, connect=True,
                 latched=None, **over):
        super().__init__(control={"mode": mode}, **over)
        if latched:                                  # a latch restored from disk, as after a restart
            self.apply(*latched)
        self.charge = ChargeController(self.engine, saved, nonce=lambda: "n1")
        self.obey = True
        self.acts = []
        if connect:
            self.charge.reconnected(self.now)
        self.charge.connector_state(connector)
        if switch is not None:
            self.charge.switch_state(self.now, switch)
        self.step()

    def step(self, acts=()):
        acts = list(acts) + self.charge.update(self.now)
        self.acts += acts
        for a in acts:
            if a["type"] == "availability" and not a["dry"] and self.obey and self.charge.actual is not None:
                self.charge.switch_state(self.now, "on" if a["on"] else "off")
        return acts

    def advance(self, s):
        self.now += s
        self.tick()
        return self.step()

    def run(self, seconds, step=1):
        out = []
        for _ in range(int(seconds / step)):
            out += self.advance(step)
        return out

    def scan(self, device="dev_ann"):
        return self.step(self.charge.scan(self.now, device))

    def tap(self, car, nonce="n1"):
        return self.step(self.charge.notification_action(self.now, f"EVAUTH_PICK:{nonce}:{car}"))

    def switch(self, state):
        return self.step(self.charge.switch_state(self.now, state))

    def connector(self, state):
        self.charge.connector_state(state)
        return self.step()

    def seen(self, name):
        self.frigate(name)
        return self.step()


def switched(acts):
    """The on/off commands in a list of actions."""
    return [a["on"] for a in acts if a["type"] == "availability"]


def notices(acts):
    return [a for a in acts if a["type"] == "notify"]
