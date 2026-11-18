#!/usr/bin/env python3
"""Writes the synthetic saved payloads used by the acceptance tests.

Everything here is invented: fake plates, fake cars, fake coordinates. Real plates and
locations must never appear in this repository. Run: python scripts/make_fixtures.py
"""
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"
CAM, ZONE = "test_cam", "test_zone"


def event(plate=None, score=0.9, sub=None, before_plate=None, cls=None, cls_score=0.9, before_cls=None,
          camera=CAM, label="car", zones=(ZONE,), obj="1700000000.123-abc"):
    def side(p, c):
        d = {"id": obj, "camera": camera, "label": label, "current_zones": list(zones),
             "entered_zones": list(zones), "stationary": False}
        if p:
            d["recognized_license_plate"] = [p, score]
        if sub:
            d["sub_label"] = [sub, 0.9]
        if c:
            d["Car Identity"] = [c, cls_score]
        return d
    return {"type": "update", "before": side(before_plate, before_cls), "after": side(plate, cls)}


def gps(state, in_zones=(), accuracy=0):
    return {"entity_id": "device_tracker.alpha_location", "state": state,
            "attributes": {"in_zones": list(in_zones), "gps_accuracy": accuracy}}


FILES = {
    "frigate/lpr_alpha.json": event(plate="WBQ123"),
    "frigate/lpr_alpha_misread.json": event(plate="WBD123"),
    "frigate/lpr_alpha_crest.json": event(plate="W-X-B-Q-123"),
    "frigate/lpr_unmatched.json": event(plate="ZZZ999"),
    "frigate/lpr_bravo.json": event(plate="XYZ495"),
    "frigate/lpr_subonly.json": event(plate="ZZZ999", sub="Bravo"),
    "frigate/lpr_wrong_camera.json": event(plate="WBQ123", camera="other_cam"),
    "frigate/lpr_outside_zone.json": event(plate="WBQ123", zones=()),
    "frigate/lpr_person.json": event(plate="WBQ123", label="person"),
    "frigate/lpr_low_score.json": event(plate="WBQ123", score=0.4),
    "frigate/classifier_bravo.json": event(cls="Bravo", cls_score=0.9),
    "frigate/classifier_alpha.json": event(cls="Alpha", cls_score=0.9),
    "frigate/classifier_low.json": event(cls="Bravo", cls_score=0.5),
    "gps/alpha_home_in_zone.json": gps("home", ["zone.test_carport"]),
    "gps/alpha_home_outside_zone.json": gps("home", []),
    "gps/alpha_work.json": gps("Work", ["zone.work"]),
    "gps/alpha_not_home.json": gps("not_home", []),
    "gps/alpha_unavailable.json": gps("unavailable", []),
}

for rel, obj in FILES.items():
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
print(f"wrote {len(FILES)} fixtures to {OUT}")
