"""Restarts: the engine must not treat 'first reading after a restart' as a car leaving."""
from app.engine import Engine
from app.model import Latch
from app.service import App
from rig import Rig, make_cfg


def restarted_rig(car="bravo", source="lpr"):
    r = Rig()
    r.engine = Engine(r.cfg, Latch(car=car, source=source, tier="confident", set_at=r.now))   # restored from disk
    return r


def test_first_reading_off_after_restart_does_not_start_the_5_minute_clear():
    r = restarted_rig()
    r.occupancy("off")
    r.advance(301)
    assert r.car == "bravo"                      # the old automation would not have cleared either
    r.advance(170)                               # 471 s: still inside grace (180) + clear time (300)
    assert r.car == "bravo"
    r.advance(10)                                # 481 s: Frigate had its settle time, still empty: clears
    assert r.car is None


def test_a_real_departure_after_restart_still_clears_in_5_minutes():
    r = restarted_rig()
    r.occupancy("on")
    r.occupancy("off")
    r.advance(301)
    assert r.car is None


def test_sensor_lost_and_back_off_is_not_a_departure():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    r.occupancy("unavailable")                   # HA / Frigate restarting
    r.occupancy("off")
    r.advance(301)
    assert r.car == "bravo"
    r.occupancy("on")
    r.occupancy("off")                           # a genuine departure
    r.advance(301)
    assert r.car is None


def test_sensor_back_on_cancels_the_unconfirmed_timer():
    r = restarted_rig()
    r.occupancy("off")
    r.advance(400)
    r.occupancy("on")
    r.advance(5000)
    assert r.car == "bravo"


def test_gps_protection_still_applies_to_an_unconfirmed_latch():
    r = Rig()
    r.apply("gps", "alpha")
    r.gps("alpha_work")
    r.gps("alpha_home_in_zone")
    r.occupancy("unavailable")
    r.occupancy("off")
    r.advance(4000)
    assert r.car == "alpha"


def test_charging_holds_an_unconfirmed_latch_too():
    from app.signals import charger_signal
    r = restarted_rig()
    r.occupancy("off")
    r._run([charger_signal("Charging", r.now)])
    r.advance(5000)
    assert r.car == "bravo"


def test_latched_while_zone_reads_empty_gets_a_fresh_clear_clock():
    r = Rig()
    r.occupancy("on")
    r.occupancy("off")
    r.advance(301)                               # nothing latched: nothing to clear
    r.apply("classifier", "bravo")               # the classifier sees a car the occupancy sensor does not
    r.advance(299)
    assert r.car == "bravo"
    r.advance(2)
    assert r.car is None


def test_no_occupancy_sensor_means_no_clear_timers_at_all():
    r = Rig()
    r.apply("lpr", "bravo")
    r.advance(100000)
    assert r.car == "bravo"


def test_shadow_starts_from_the_restored_latch_not_none(tmp_path):
    a = App(str(tmp_path), dev=True)
    a.settings.replace(make_cfg(entities={"ha_latch_vehicle": "input_select.v", "ha_latch_source": "input_select.s"}))
    a.reload()
    a.engine.apply("lpr", "alpha", a.now())
    a._record([a.engine.apply("lpr", "alpha", a.now())])        # persists the latch
    b = App(str(tmp_path), dev=True)                            # the app restarts
    assert b.engine.latch.car == "alpha" and b.shadow.engine == ("alpha", "lpr")
    b.on_state("input_select.v", {"state": "Alpha"})
    b.on_state("input_select.s", {"state": "lpr"})
    b.offset += 30
    b.tick()
    assert b.shadow.status()["open_now"] is False and b.store.disagreements() == []
