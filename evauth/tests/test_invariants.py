"""Invariants from the spec, plus the plate matcher and the Frigate parser."""
import json

from app.frigate import parse_event
from app.model import UNKNOWN_ID, Latch
from app.policy import car_for_plate, normalise_plate, plate_matches
from conftest import FIXTURES
from rig import Rig, make_cfg


def test_unknown_never_downgrades_any_source_or_state():
    for src in ("lpr", "gps", "nfc", "prompt"):
        r = Rig()
        r.apply(src, "alpha")
        r.advance(1000)                                  # even a stale latch
        d = r.engine.apply("lpr", UNKNOWN_ID, r.now)
        assert d.action == "keep" and r.car == "alpha"


def test_classifier_never_authorises_even_if_config_says_so():
    r = Rig()
    r.cfg["sources"]["classifier"]["authorises"] = True  # a hand-edited config cannot enable it
    r.apply("classifier", "alpha")
    assert r.engine.authorised() is False


def test_lpr_authorises_by_default_and_can_be_switched_off():
    r = Rig()
    r.frigate("lpr_alpha")
    assert r.engine.authorised() is True
    r2 = Rig()
    r2.cfg["sources"]["lpr"]["authorises"] = False
    r2.frigate("lpr_alpha")
    assert r2.engine.authorised() is False


def test_unknown_latch_never_authorises():
    r = Rig()
    r.cfg["sources"]["lpr"]["authorises"] = True
    r.frigate("lpr_unmatched")
    assert r.car == UNKNOWN_ID and r.engine.authorised() is False


def test_same_car_confirmed_by_confident_source_upgrades_classifier_latch():
    r = Rig()
    r.frigate("classifier_alpha")
    assert r.source == "classifier" and not r.engine.authorised()
    r.apply("nfc", "alpha")
    assert r.source == "nfc" and r.engine.authorised()


def test_gps_leave_clear_applies_to_named_zone_not_just_not_home():
    r = Rig()
    r.apply("gps", "alpha")
    r.gps("alpha_home_in_zone")
    r.advance(5)
    r.gps("alpha_work")                      # a named zone, not "not_home"
    r.advance(121)
    assert r.car is None


def test_unavailable_tracker_blip_does_not_clear():
    r = Rig()
    r.apply("gps", "alpha")
    r.gps("alpha_home_in_zone")
    r.gps("alpha_work")
    r.advance(60)
    r.gps("alpha_unavailable")
    r.advance(200)
    assert r.car == "alpha"


def test_occupancy_unavailable_is_no_change():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    r.occupancy("unavailable")
    r.advance(1000)
    assert r.car == "bravo"


def test_occupancy_flap_does_not_clear():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    r.occupancy("off")
    r.advance(30)
    r.occupancy("on")
    r.advance(600)
    assert r.car == "bravo"


def test_lpr_other_car_overwrites_any_latch():
    r = Rig()
    r.apply("lpr", "alpha")
    r.advance(1)
    r.frigate("lpr_bravo")
    assert r.car == "bravo"


def test_lpr_refreshes_same_car_time():
    r = Rig()
    r.frigate("lpr_alpha")
    t0 = r.engine.latch.set_at
    r.advance(100)
    r.frigate("lpr_alpha")                   # new payload with a plate arriving
    assert r.engine.latch.set_at > t0


def test_lpr_low_score_ignored():
    r = Rig()
    r.frigate("lpr_low_score")
    assert r.car is None


def test_lpr_trusts_frigate_sub_label_when_no_pattern_matches():
    r = Rig()
    r.frigate("lpr_subonly")
    assert r.car == "bravo"
    r2 = Rig(frigate={"trust_sub_label": False})
    r2.frigate("lpr_subonly")
    assert r2.car == UNKNOWN_ID


def test_frigate_gates():
    cfg = make_cfg()
    for name in ("lpr_wrong_camera", "lpr_outside_zone", "lpr_person"):
        p = json.loads((FIXTURES / "frigate" / f"{name}.json").read_text())
        sigs, why = parse_event(p, cfg["frigate"], 0)
        assert sigs == [] and why


def test_test_camera_accepted_only_when_enabled():
    p = json.loads((FIXTURES / "frigate" / "lpr_wrong_camera.json").read_text())
    cfg = make_cfg(frigate={"test_camera": "other_cam", "accept_test_camera": True})
    assert parse_event(p, cfg["frigate"], 0)[0]
    cfg = make_cfg(frigate={"test_camera": "other_cam", "accept_test_camera": False})
    assert not parse_event(p, cfg["frigate"], 0)[0]


def test_lpr_signal_only_fires_when_plate_first_appears():
    cfg = make_cfg()
    p = json.loads((FIXTURES / "frigate" / "lpr_alpha.json").read_text())
    p["before"] = json.loads(json.dumps(p["after"]))        # plate already known: no new signal
    assert parse_event(p, cfg["frigate"], 0)[0] == []


def test_plate_matcher():
    assert normalise_plate("w-b q.123") == "WBQ123"
    assert plate_matches("WBD123", "W*B[QDO0]123")
    assert plate_matches("W-XB-O123", "W*B[QDO0]123")
    assert not plate_matches("WBZ123", "W*B[QDO0]123")
    cars = make_cfg()["cars"]
    assert car_for_plate("XYZ49S", cars) == "bravo" and car_for_plate("NOPE", cars) is None


def test_persisted_latch_round_trip():
    l = Latch(car="alpha", source="gps", tier="confident", set_at=5.0)
    assert Latch(**l.to_dict()) == l
