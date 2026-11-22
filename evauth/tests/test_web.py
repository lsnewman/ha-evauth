import json
import threading
import urllib.request

import pytest

from app.service import App
from app.web import Api, make_server
from rig import make_cfg


@pytest.fixture
def served(tmp_path):
    app = App(str(tmp_path), dev=True)
    app.settings.replace(make_cfg())
    app.reload()
    srv = make_server(Api(app), 0, allow_all=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(method, path, body=None):
        r = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield app, req
    srv.shutdown()


def test_state_and_settings_roundtrip(served):
    app, req = served
    code, st = req("GET", "/api/state")
    assert code == 200 and st["mode"].startswith("shadow") and st["engine"]["latch"]["car"] is None
    code, s = req("PATCH", "/api/settings/policy", {"stale_after_s": 200})
    assert code == 200 and s["policy"]["stale_after_s"] == 200 and app.cfg["policy"]["stale_after_s"] == 200
    code, err = req("PATCH", "/api/settings/policy", {"stale_after_s": -5})
    assert code == 400 and "stale_after_s" in err["error"]


def test_cars_and_users_edited_through_api(served):
    app, req = served
    # a car that users still name as their default cannot be removed
    code, err = req("PATCH", "/api/settings/cars", {"value": [{"name": "Zed", "plate_patterns": "ZZ*1"}]})
    assert code == 400 and "default car" in err["error"]
    assert req("PATCH", "/api/settings/users", {"value": []})[0] == 200
    code, s = req("PATCH", "/api/settings/cars", {"value": [{"name": "Zed", "plate_patterns": "ZZ*1"}]})
    assert code == 200 and s["cars"][0]["id"] == "zed"
    code, _ = req("PATCH", "/api/settings/users", {"value": [{"name": "U", "default_car": "nope"}]})
    assert code == 400


def test_sessions_allocate_and_ledger_through_api(served):
    app, req = served
    code, r = req("POST", "/api/sessions", {"car_id": "alpha", "kwh": 5, "start": 100, "end": 200})
    sid = r["id"]
    code, s = req("PATCH", f"/api/sessions/{sid}", {"car_id": "bravo"})
    assert s["car_id"] == "bravo"
    code, s = req("GET", "/api/sessions")
    led = {x["car"]: x["total_kwh"] for x in s["ledger"]}
    assert led["bravo"] == 5 and led["alpha"] == 0
    assert req("POST", "/api/sessions", {"car_id": "nope"})[0] == 400


def test_dev_injection_drives_engine_and_shadow(served):
    app, req = served
    req("PATCH", "/api/settings/entities", {"ha_latch_vehicle": "input_select.v", "ha_latch_source": "input_select.s"})
    req("POST", "/api/dev/state", {"entity_id": "input_select.v", "state": "none"})
    req("POST", "/api/dev/state", {"entity_id": "input_select.s", "state": "none"})
    payload = json.load(open(__import__("conftest").FIXTURES / "frigate" / "lpr_alpha.json"))
    code, st = req("POST", "/api/dev/frigate", payload)
    assert st["engine"]["latch"]["car"] == "alpha"
    code, st = req("POST", "/api/dev/advance", {"seconds": 10})
    code, d = req("GET", "/api/disagreements")
    assert d["disagreements"] and d["disagreements"][0]["kind"] == "car"
    code, _ = req("PATCH", f"/api/disagreements/{d['disagreements'][0]['id']}", {"status": "intended", "note": "ok"})
    assert code == 200
    assert req("GET", "/api/events")[1]["events"][0]["rule"]


def test_dev_routes_hidden_when_not_dev(tmp_path):
    app = App(str(tmp_path), dev=False)
    srv = make_server(Api(app), 0, allow_all=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    r = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/dev/advance", method="POST", data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 404
    srv.shutdown()


def test_options_dev_stub_and_learned_frigate_names(served):
    app, req = served
    payload = json.load(open(__import__("conftest").FIXTURES / "frigate" / "classifier_bravo.json"))
    req("POST", "/api/dev/frigate", payload)
    code, o = req("GET", "/api/options")
    assert code == 200
    assert [e["value"] for e in o["entities"]["device_tracker"]] == ["device_tracker.alpha_location"]
    assert all(e["value"].startswith("binary_sensor.") for e in o["entities"]["binary_sensor"])
    assert "test_cam" in o["frigate"]["cameras"] and "test_zone" in o["frigate"]["zones"]
    assert "Bravo" in o["frigate"]["classes"]
    assert o["notify"] and o["devices"]


def test_options_reports_failures_instead_of_raising():
    from app import options

    class Broken:
        def list_entities(self): raise RuntimeError("no states")
        def services(self): raise RuntimeError("no services")
        def device_registry(self): raise RuntimeError("no registry")
    o = options.build(Broken(), {"cameras": set(), "zones": set(), "classes": set()}, False)
    assert set(o["errors"]) == {"entities", "notify", "devices"} and o["entities"]["sensor"] == []


def test_notify_and_device_parsing():
    from app import options
    svc = [{"domain": "notify", "services": {"mobile_app_ann": {}, "persistent_notification": {}}},
           {"domain": "light", "services": {"turn_on": {}}}]
    assert [n["value"] for n in options.notify_from_services(svc)] == [
        "notify.mobile_app_ann", "notify.persistent_notification"]
    reg = [{"id": "abc", "name": "Ann's phone", "manufacturer": "Apple", "model": "iPhone",
            "identifiers": [["mobile_app", "x"]]}, {"id": "def", "name": "Hub", "identifiers": [["hue", "y"]]}]
    d = options.devices_from_registry(reg)
    assert d == [{"value": "abc", "label": "Ann's phone (Apple iPhone)"}]


def test_shadow_seed_works_whatever_order_you_configure_things(tmp_path):
    """Entities saved before any cars: the HA latch is adopted as soon as the car exists."""
    from app import settings
    app = App(str(tmp_path), dev=True)
    app.settings.update_section("entities", {"ha_latch_vehicle": "input_select.v", "ha_latch_source": "input_select.s"})
    app.reload()
    app.on_state("input_select.v", {"state": "Alpha"})
    app.on_state("input_select.s", {"state": "lpr"})
    assert app.engine.latch.car is None                       # no cars yet: cannot map "Alpha"
    app.settings.update_section("cars", [{"name": "Alpha"}])
    app.reload()
    assert (app.engine.latch.car, app.engine.latch.source) == ("alpha", "lpr")
    app.tick()
    assert app.shadow.status()["open_now"] is False


def test_seed_does_not_mask_a_real_clear(tmp_path):
    app = App(str(tmp_path), dev=True)
    app.settings.replace(make_cfg(entities={"ha_latch_vehicle": "input_select.v", "ha_latch_source": "input_select.s"}))
    app.reload()
    app.on_state("input_select.v", {"state": "none"})
    app.on_state("input_select.s", {"state": "none"})
    app.engine.apply("lpr", "alpha", app.now())
    app._record([app.engine.clear(app.now(), "occupancy", "clear.empty", "x")])
    app.on_state("input_select.v", {"state": "Alpha"})       # HA still says Alpha: that is a disagreement
    assert app.engine.latch.car is None


def test_manual_session_needs_start_and_end_and_can_be_backdated(served):
    app, req = served
    assert req("POST", "/api/sessions", {"car_id": "alpha", "kwh": 5})[0] == 400
    assert req("POST", "/api/sessions", {"car_id": "alpha", "start": 200, "end": 100})[0] == 400
    assert req("POST", "/api/sessions", {"car_id": "alpha", "start": 100, "end": 200, "kwh": "x"})[0] == 400
    code, r = req("POST", "/api/sessions", {"car_id": "alpha", "start": 1763682360, "end": 1763709240, "kwh": 51.3})
    assert code == 200
    code, s = req("PATCH", f"/api/sessions/{r['id']}", {"end": 1763709999})
    assert code == 200 and s["end"] == 1763709999
    assert req("PATCH", f"/api/sessions/{r['id']}", {"start": 1763710000})[0] == 400
    assert req("GET", "/api/sessions")[1]["open"] is None          # a manual session never blocks the charger's
