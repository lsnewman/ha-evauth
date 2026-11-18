"""The inventory's acceptance table, one test per row, on saved synthetic payloads and a fake clock.

Car names are synthetic: Alpha stands in for the GPS-tracked car, Bravo for the car without GPS.
"""
from app.engine import Engine
from app.model import UNKNOWN_ID
from app.scan import ScanFlow, watchdog_expired
from rig import Rig


def test_lpr_known_car():
    r = Rig()
    r.frigate("lpr_alpha")
    assert (r.car, r.source) == ("alpha", "lpr")


def test_lpr_misread_matches_via_pattern():
    r = Rig()
    r.frigate("lpr_alpha_misread")
    assert (r.car, r.source) == ("alpha", "lpr")


def test_lpr_unknown_keeps_existing_latch():
    r = Rig()
    r.frigate("lpr_alpha")
    r.advance(1)
    d = r.frigate("lpr_unmatched")
    assert r.car == "alpha" and d[0].action == "keep"


def test_lpr_unknown_on_empty_latches_unknown():
    r = Rig()
    r.frigate("lpr_unmatched")
    assert (r.car, r.source) == (UNKNOWN_ID, "lpr")


def _arrive(r, car_state="alpha_home_in_zone"):
    r.gps("alpha_work")                 # away first, so home is a real arrival
    r.occupancy("on")
    r.advance(1)
    return r.gps(car_state)


def test_gps_arrival_on_empty_latch():
    r = Rig()
    _arrive(r)
    assert (r.car, r.source) == ("alpha", "gps")


def test_gps_does_not_override_fresh_other_car():
    r = Rig()
    r.apply("lpr", "bravo")
    r.advance(100)
    _arrive(r)
    assert r.car == "bravo"


def test_gps_overrides_stale_other_car():
    # default policy: a latch older than 300 s can be replaced by GPS (documented quirk 5)
    r = Rig()
    r.apply("lpr", "bravo")
    r.advance(301)
    _arrive(r)
    assert (r.car, r.source) == ("alpha", "gps")


def test_classifier_on_empty_fills_but_never_authorises():
    r = Rig()
    d = r.frigate("classifier_bravo")
    assert (r.car, r.source) == ("bravo", "classifier")
    assert d[0].authorises is False and r.engine.authorised() is False


def test_classifier_on_known_latch_unchanged():
    r = Rig()
    r.frigate("lpr_alpha")
    r.advance(1)
    r.frigate("classifier_bravo")
    assert (r.car, r.source) == ("alpha", "lpr")


def test_zone_empty_5_min_clears_non_gps_car():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    r.occupancy("off")
    r.advance(299)
    assert r.car == "bravo"
    r.advance(2)
    assert r.car is None


def test_zone_empty_keeps_gps_car_while_tracker_in_zone():
    r = Rig()
    _arrive(r)
    r.occupancy("off")
    r.advance(301)
    assert r.car == "alpha"


def test_gps_car_leaves_for_other_zone_clears_after_2_min():
    r = Rig()
    _arrive(r)
    r.advance(10)
    r.gps("alpha_work")
    r.advance(119)
    assert r.car == "alpha"
    r.advance(2)
    assert r.car is None


def test_scan_mapped_phone_no_latch_then_pick_sets_prompt():
    r = Rig()
    f = ScanFlow(r.engine)
    acts = f.scan(r.now, "dev_ann")
    assert {"type": "availability", "on": True} in acts
    assert any(a["type"] == "picker" for a in acts)
    f.pick(r.now + 5, "bravo")
    assert (r.car, r.source) == ("bravo", "prompt")


def test_scan_no_pick_in_90s_sets_default_car():
    r = Rig()
    f = ScanFlow(r.engine)
    f.scan(r.now, "dev_ann")
    f.tick(r.now + 89)
    assert r.car is None
    f.tick(r.now + 91)
    assert (r.car, r.source) == ("alpha", "default")


def test_scan_with_valid_latch_and_availability_on_changes_nothing():
    r = Rig()
    r.apply("prompt", "bravo")
    f = ScanFlow(r.engine)
    f.availability = True
    acts = f.scan(r.now + 1, "dev_ann")
    assert all(a["type"] == "log" for a in acts)
    assert r.car == "bravo" and f.pending is None


def test_scan_unmapped_phone_logs_notifies_owner_availability_unchanged():
    r = Rig()
    f = ScanFlow(r.engine)
    acts = f.scan(r.now, "dev_stranger")
    assert f.availability is False
    assert any(a["type"] == "notify" and a["target"] == "notify.mobile_app_ann" for a in acts)
    assert not any(a["type"] in ("availability", "picker") for a in acts)
    assert r.car is None


def test_scan_from_disallowed_user_authorises_nothing():
    r = Rig()
    f = ScanFlow(r.engine)
    acts = f.scan(r.now, "dev_ben")
    assert f.availability is False and not any(a["type"] == "picker" for a in acts)


def test_no_plugin_within_10_min_without_latch_turns_availability_off():
    # A user with no default car: the 90 s fallback cannot latch, so there is still no valid latch.
    r = Rig()
    r.cfg["users"][0]["default_car"] = ""
    f = ScanFlow(r.engine)
    f.scan(r.now, "dev_ann")
    f.tick(r.now + 599)
    assert f.availability is True
    acts = f.tick(r.now + 601)
    assert {"type": "availability", "on": False} in acts and f.availability is False


def test_valid_latch_is_never_disabled_by_the_no_plugin_rule():
    r = Rig()
    f = ScanFlow(r.engine)
    f.scan(r.now, "dev_ann")
    f.pick(r.now + 5, "alpha")
    acts = f.tick(r.now + 700)
    assert f.availability is True and not any(a["type"] == "availability" for a in acts)


def test_watchdog_turns_availability_off_when_app_goes_quiet():
    assert watchdog_expired(None, 1000, 120) is True
    assert watchdog_expired(900, 1000, 120) is False
    assert watchdog_expired(800, 1000, 120) is True


def test_default_fallback_that_latches_means_no_plugin_rule_does_not_apply():
    r = Rig()
    f = ScanFlow(r.engine)
    f.scan(r.now, "dev_ann")
    f.tick(r.now + 100)                       # default car latched at 90 s
    acts = f.tick(r.now + 700)
    assert r.car == "alpha" and f.availability is True and not acts
