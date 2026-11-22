"""A car that is plugged in or charging keeps its latch (it is certainly there)."""
from app.model import Signal
from app.signals import charger_signal
from rig import Rig


def charger(r, state):
    s = charger_signal(state, r.now)
    return r._run([s] if s else [])


def test_charging_holds_latch_through_an_empty_zone_then_clears_after_a_fresh_grace_period():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    charger(r, "Preparing")
    charger(r, "Charging")
    r.occupancy("off")
    r.advance(301)
    assert r.car == "bravo"                              # held, even past the 5 min rule
    r.advance(3600)
    assert r.car == "bravo"
    charger(r, "Finishing")
    charger(r, "Available")                              # plug-in ends: a fresh 5 minutes starts now
    r.advance(299)
    assert r.car == "bravo"
    r.advance(2)
    assert r.car is None


def test_hold_is_logged_once_not_every_tick():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    charger(r, "Charging")
    r.occupancy("off")
    for _ in range(30):
        r.advance(30)
    assert len([d for d in r.log if d.rule == "clear.empty_held_charging"]) == 1


def test_setting_off_restores_the_old_behaviour():
    r = Rig(policy={"charger_holds_latch": False})
    r.occupancy("on")
    r.apply("lpr", "bravo")
    charger(r, "Charging")
    r.occupancy("off")
    r.advance(301)
    assert r.car is None


def test_charger_unavailable_is_no_change():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    charger(r, "Charging")
    charger(r, "unavailable")
    r.occupancy("off")
    r.advance(1000)
    assert r.car == "bravo"


def test_charging_also_holds_a_gps_car_that_reads_away():
    r = Rig()
    r.apply("gps", "alpha")
    r.gps("alpha_home_in_zone")
    charger(r, "Charging")
    r.gps("alpha_work")
    r.advance(500)
    assert r.car == "alpha"
    charger(r, "Available")
    r.advance(119)
    assert r.car == "alpha"
    r.advance(2)
    assert r.car is None


def test_nothing_plugged_in_changes_nothing():
    r = Rig()
    r.occupancy("on")
    r.apply("lpr", "bravo")
    charger(r, "Available")
    r.occupancy("off")
    r.advance(301)
    assert r.car is None


def test_service_wires_charger_status_to_the_engine(tmp_path):
    from app.service import App
    from rig import make_cfg
    app = App(str(tmp_path), dev=True)
    app.settings.replace(make_cfg(entities={"charger_status": "sensor.conn"}))
    app.reload()
    app.on_state("sensor.conn", {"state": "Charging"})
    assert app.status()["engine"]["charger_present"] is True
    app.on_state("sensor.conn", {"state": "Available"})
    assert app.status()["engine"]["charger_present"] is False
