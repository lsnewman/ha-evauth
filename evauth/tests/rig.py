"""Test rig: a synthetic two-car config, a fake clock and fixture playback."""
import json

from app import settings
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
