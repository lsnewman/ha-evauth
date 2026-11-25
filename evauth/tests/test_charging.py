"""Charger Availability and the scan flow: the controller's rules, then the service around it."""
import json

import pytest

from app.charging import PICK_PREFIX, readiness
from app.ha_client import HAClient
from app.service import ATTR_TOPIC, DISCOVERY_TOPIC, STATE_TOPIC, App
from app.signals import PRESENT_STATES
from app.web import Api, ApiError
from rig import Flow, make_cfg, notices, switched

SWITCH = "switch.test_availability"
CONNECTOR = "sensor.test_connector"
WATCHDOG = "automation.test_watchdog"


def idle(**kw):
    """A live flow that has settled with nothing to authorise: Availability off."""
    f = Flow(**kw)
    f.advance(60)
    return f


# ---------------------------------------------------------------- turning off

def test_idle_goes_off_only_after_the_settle_time():
    f = Flow(switch="on")
    assert switched(f.advance(29)) == []
    assert switched(f.advance(2)) == [False]
    assert switched(f.run(30)) == []


@pytest.mark.parametrize("state", PRESENT_STATES)
def test_never_off_while_a_car_is_plugged_in(state):
    f = Flow(switch="on", connector=state)
    assert switched(f.run(1200, 10)) == []


@pytest.mark.parametrize("state", [None, "unavailable", "unknown"])
def test_never_off_when_the_connector_cannot_be_read(state):
    f = Flow(switch="on", connector=state)
    assert switched(f.run(1200, 10)) == []


def test_nothing_is_sent_while_the_switch_is_unreadable():
    f = Flow(switch=None)
    assert switched(f.run(120)) == []
    f.apply("lpr", "alpha")
    assert switched(f.step()) == []
    assert switched(f.switch("off")) == [True]          # the pending "on" is applied once readable


def test_nothing_is_switched_before_connecting_or_after_a_disconnect():
    f = Flow(switch="on", connect=False)
    assert switched(f.run(120)) == []
    f = Flow(switch="on")
    f.charge.disconnected()
    assert switched(f.run(120)) == []


def test_unplugging_turns_off_once_nothing_authorises():
    f = Flow(switch="on", connector="Charging")
    f.advance(60)
    assert switched(f.connector("Available")) == [False]


def test_departure_turns_off_after_the_latch_clears():
    f = idle()
    f.occupancy("on")
    f.apply("lpr", "bravo")
    assert switched(f.step()) == [True]
    f.occupancy("off")
    assert switched(f.advance(299)) == []
    assert switched(f.advance(2)) == [False]
    assert f.car is None


# ---------------------------------------------------------------- turning on

def test_an_authorised_latch_turns_availability_on():
    f = idle()
    assert switched(f.seen("lpr_alpha")) == [True]


def test_the_classifier_never_turns_it_on():
    f = idle()
    assert switched(f.seen("classifier_bravo")) == []
    assert f.car == "bravo" and f.charge.switch is False


def test_an_unticked_source_does_not_turn_it_on():
    f = idle()
    f.cfg["sources"]["lpr"]["authorises"] = False
    assert switched(f.seen("lpr_alpha")) == []


def test_an_unknown_plate_does_not_turn_it_on():
    f = idle()
    assert switched(f.seen("lpr_unmatched")) == []


def test_manual_off_is_respected_until_the_next_event():
    f = idle()
    f.apply("lpr", "alpha")
    f.step()
    assert switched(f.switch("off")) == []
    f.apply("lpr", "alpha")                              # the same car seen again is not a new event
    assert switched(f.step()) == [] and switched(f.run(60)) == []
    assert switched(f.scan()) == [True]


def test_a_different_car_becoming_authorised_turns_it_on_again():
    f = idle()
    f.apply("lpr", "alpha")
    f.step()
    f.switch("off")
    f.apply("lpr", "bravo")
    assert switched(f.step()) == [True]


def test_manual_on_gets_the_plugin_window_then_goes_off():
    f = idle()
    assert switched(f.switch("on")) == []
    assert switched(f.advance(599)) == []
    assert switched(f.advance(2)) == [False]


def test_switch_returning_from_unavailable_is_not_manual_use():
    f = idle()
    f.switch("unavailable")
    assert switched(f.switch("on")) == [False]


# ---------------------------------------------------------------- the scan flow

def test_classifier_car_is_the_one_the_scan_authorises():
    f = idle()
    f.cfg["policy"]["upgrade_same_car"] = False          # a scan authorises whatever this says
    f.seen("classifier_bravo")
    acts = f.scan()
    assert "bravo" in notices(acts)[0]["message"].lower()
    f.advance(91)
    assert (f.car, f.source) == ("bravo", "nfc") and f.engine.authorised()


def test_default_car_replaces_an_unknown_latch():
    f = idle()
    f.seen("lpr_unmatched")
    f.scan()
    f.advance(91)
    assert (f.car, f.source) == ("alpha", "default")


def test_picking_the_classifier_car_authorises_it():
    f = idle()
    f.seen("classifier_bravo")
    f.scan()
    f.tap("bravo")
    assert (f.car, f.source) == ("bravo", "prompt") and f.engine.authorised()


def test_recognition_while_waiting_cancels_the_default():
    f = idle()
    f.scan()
    f.apply("lpr", "bravo")
    acts = f.step()
    assert f.charge.pending is None and "Recognised" in notices(acts)[0]["message"]
    f.advance(100)
    assert (f.car, f.source) == ("bravo", "lpr")


def test_old_or_foreign_taps_are_ignored():
    f = idle()
    f.scan()
    f.tap("bravo", nonce="old")
    assert f.car is None and f.charge.pending
    f.step(f.charge.notification_action(f.now, "SOMETHING_ELSE"))
    f.tap("bravo")
    f.tap("alpha")                                       # a second tap
    assert f.car == "bravo"


def test_an_alert_does_not_replace_a_waiting_picker():
    f = idle()
    picker = notices(f.scan())[0]
    alert = notices(f.scan("dev_stranger"))[0]
    assert alert["data"]["tag"] != picker["data"]["tag"] and f.charge.pending


def test_scan_while_off_asks_to_replug():
    f = idle()
    assert "plug it in again" in notices(f.scan())[0]["message"]


def test_android_picker_is_capped_default_first_and_urgent():
    f = idle()
    f.cfg["users"][0].update(platform="android", default_car="bravo")
    f.cfg["cars"] += [{**f.cfg["cars"][1], "id": f"c{i}", "name": f"C{i}"} for i in range(3)]
    data = notices(f.scan())[0]["data"]
    assert len(data["actions"]) == 3 and data["actions"][0]["action"] == f"{PICK_PREFIX}:n1:bravo"
    assert data["priority"] == "high" and data["ttl"] == 0


def test_a_plate_read_cannot_revoke_a_scan_when_lpr_does_not_authorise():
    f = idle()
    f.cfg["sources"]["lpr"]["authorises"] = False
    f.scan()
    f.tap("alpha")
    f.advance(10)
    f.seen("lpr_alpha")
    assert (f.car, f.source) == ("alpha", "prompt") and f.engine.authorised()
    assert f.engine.latch.set_at == f.now


# ---------------------------------------------------------------- restarts

def test_pending_scan_survives_a_restart():
    f = idle()
    f.scan()
    again = Flow(switch="on", saved=f.charge.state())
    again.now = f.now
    again.charge.reconnected(again.now)
    again.advance(91)
    assert (again.car, again.source) == ("alpha", "default")


@pytest.mark.parametrize("occupancy,expected", [("on", [True]), ("off", [])])
def test_after_a_restart_an_authorised_latch_needs_an_occupied_zone(occupancy, expected):
    saved = {"mode": "live", "auth_car": "alpha"}
    f = Flow(switch=None, saved=saved, connect=False, latched=("lpr", "alpha"))
    f.occupancy(occupancy)
    f.charge.switch_state(f.now, "off")                  # e.g. the watchdog turned it off while we were away
    f.charge.reconnected(f.now)
    assert switched(f.step()) == expected


def test_restart_while_charging_switches_nothing():
    f = Flow(switch="on", connector="Charging", saved={"mode": "live"})
    assert switched(f.run(1200, 10)) == []


# ---------------------------------------------------------------- failing commands

def test_unconfirmed_command_is_retried_then_reported_once():
    f = Flow(switch="on")
    f.obey = False
    acts = f.run(200)
    assert switched(acts) == [False, False, False]
    assert len(notices(acts)) == 1 and "did not turn" in notices(acts)[0]["message"]
    later = f.run(1200)
    assert switched(later) == [False, False] and not notices(later)      # quietly, every 10 minutes


def test_a_retry_is_dropped_when_no_longer_wanted():
    f = Flow(switch="on")
    f.obey = False
    f.advance(31)
    f.connector("Preparing")
    assert switched(f.run(120)) == []


# ---------------------------------------------------------------- modes

def test_dry_run_never_switches_but_simulates():
    f = Flow(mode="dry_run", switch="on")
    f.advance(60)
    assert f.charge.switch is False and f.charge.actual is True
    acts = f.scan()
    assert notices(acts)[0]["title"].startswith("[dry run]")
    f.run(1200, 10)
    assert all(a["dry"] for a in f.acts if a["type"] == "availability")


def test_off_mode_does_nothing_and_ignores_scans():
    f = Flow(mode="off", switch="on")
    assert switched(f.run(120)) == []
    assert switched(f.scan()) == [] and f.charge.pending is None


def test_leaving_live_turns_availability_back_on():
    f = idle()
    assert f.charge.switch is False
    f.cfg["control"]["mode"] = "off"
    assert switched(f.step()) == [True] and f.charge.actual is True


def test_dry_run_after_live_ignores_the_real_restore_command():
    f = idle()
    f.obey = False
    f.cfg["control"]["mode"] = "dry_run"
    acts = f.step() + f.run(200)
    assert [(a["on"], a["dry"]) for a in acts if a["type"] == "availability" and a["dry"]] == [(False, True)]


def test_going_live_turns_an_idle_charger_off():
    f = Flow(mode="off", switch="on")
    f.advance(60)
    f.cfg["control"]["mode"] = "live"
    assert switched(f.step()) == []
    assert switched(f.advance(31)) == [False]


def test_readiness_needs_every_item():
    cfg = make_cfg(entities={"charger_availability": SWITCH, "charger_status": CONNECTOR},
                   control={"tag_id": "t", "watchdog_automation": WATCHDOG})
    assert all(r["ok"] for r in readiness(cfg, True, True, "on"))
    assert not all(r["ok"] for r in readiness(cfg, True, True, "off"))
    assert not all(r["ok"] for r in readiness(cfg, False, True, "on"))
    cfg["users"][0]["default_car"] = ""
    assert not all(r["ok"] for r in readiness(cfg, True, True, "on"))


# ---------------------------------------------------------------- the service

class FakeHA:
    status = "connected"

    def __init__(self):
        self.calls, self.alive = [], True

    def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))

    def fresh(self):
        return self.alive

    def states(self):
        return [{"entity_id": WATCHDOG, "state": "on"}]

    def set_watch(self, entities):
        pass


class FakeMQTT:
    status, connected, generation = "connected", True, 1

    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))
        return True


class Svc:
    def __init__(self, path, mode="live"):
        self.t = [1_000_000.0]
        self.app = App(str(path), clock=lambda: self.t[0])
        self.app.settings.replace(make_cfg(
            entities={"charger_availability": SWITCH, "charger_status": CONNECTOR,
                      "ha_latch_vehicle": "input_select.v", "ha_latch_source": "input_select.s"},
            control={"mode": mode, "tag_id": "tag-1", "watchdog_automation": WATCHDOG}))
        self.ha, self.mqtt = FakeHA(), FakeMQTT()
        self.app.ha, self.app.mqtt = self.ha, self.mqtt
        self.app.reload()
        self.state(CONNECTOR, "Available")
        self.state(SWITCH, "off")
        self.app.on_ha_connect()

    def state(self, entity, value):
        self.app.on_state(entity, {"entity_id": entity, "state": value, "attributes": {}})
        self.app.flush()

    def scan(self, tag="tag-1"):
        self.app.on_ha_event("tag_scanned", {"tag_id": tag, "device_id": "dev_ann"})
        self.app.flush()

    def advance(self, s):
        self.t[0] += s
        self.app.tick()
        self.app.flush()

    def services(self):
        return [f"{d}.{s}" for d, s, _ in self.ha.calls]


def test_scan_event_switches_on_and_sends_the_picker(tmp_path):
    s = Svc(tmp_path)
    s.scan()
    assert s.services() == ["switch.turn_on", "notify.mobile_app_ann"]
    assert s.ha.calls[0][2] == {"entity_id": SWITCH}
    action = s.ha.calls[1][2]["data"]["actions"][0]["action"]
    s.app.on_ha_event("mobile_app_notification_action", {"action": action})
    assert s.app.engine.latch.source == "prompt"


def test_another_tag_is_ignored(tmp_path):
    s = Svc(tmp_path)
    s.scan("tag-2")
    assert s.ha.calls == []


def test_dry_run_notifies_but_never_calls_the_switch(tmp_path):
    s = Svc(tmp_path, mode="dry_run")
    s.scan()
    s.advance(700)
    assert s.services() and all(x.startswith("notify.") for x in s.services())


def test_panel_picker_answers_a_waiting_scan(tmp_path):
    s = Svc(tmp_path)
    s.scan()
    assert Api(s.app).pick({"car_id": "bravo"}) == {"scan": True, "car_id": "bravo"}
    assert s.app.engine.latch.car == "bravo"


def test_service_state_survives_a_restart(tmp_path):
    s = Svc(tmp_path)
    s.scan()
    again = App(str(tmp_path), clock=lambda: s.t[0])
    assert again.charge.pending and again.charge.mode == "live"


def test_scan_latches_are_pre_marked_intended_in_shadow(tmp_path):
    s = Svc(tmp_path)
    s.state("input_select.v", "none")
    s.state("input_select.s", "none")
    s.scan()
    s.advance(91)
    s.advance(10)
    d = s.app.store.disagreements()[0]
    assert (d["engine_source"], d["status"], d["note"]) == ("default", "intended", "app scan flow")


def test_heartbeat_announces_itself_then_stops_when_the_app_cannot_act(tmp_path):
    s = Svc(tmp_path)
    s.advance(1)
    topics = [t for t, _, _ in s.mqtt.sent]
    assert topics == [DISCOVERY_TOPIC, ATTR_TOPIC, STATE_TOPIC]
    config = json.loads(s.mqtt.sent[0][1])
    assert config["expire_after"] == 120 and config["default_entity_id"] == "sensor.evauth_heartbeat"
    assert s.mqtt.sent[0][2] is True and s.mqtt.sent[2][2] is False
    s.advance(31)
    assert [t for t, _, _ in s.mqtt.sent].count(STATE_TOPIC) == 2
    s.ha.alive = False
    s.advance(300)
    assert [t for t, _, _ in s.mqtt.sent].count(STATE_TOPIC) == 2


def test_no_heartbeat_until_charging_control_is_used_but_it_continues_afterwards(tmp_path):
    s = Svc(tmp_path, mode="off")
    s.advance(60)
    assert s.mqtt.sent == []
    s.app.settings.update_section("control", {"mode": "dry_run"})
    s.app.reload()
    s.advance(31)
    s.app.settings.update_section("control", {"mode": "off"})
    s.app.reload()
    before = len(s.mqtt.sent)
    s.advance(31)
    assert len(s.mqtt.sent) > before


def test_live_mode_is_refused_until_ready(tmp_path):
    s = Svc(tmp_path, mode="off")
    api = Api(s.app)
    s.state(WATCHDOG, "off")
    with pytest.raises(ApiError) as e:
        api.patch_settings("control", {"mode": "live"})
    assert e.value.status == 409 and "Watchdog" in str(e.value)
    with pytest.raises(ApiError):
        api.put_settings({**s.app.settings.get(), "control": {**s.app.cfg["control"], "mode": "live"}})
    s.state(WATCHDOG, "on")
    assert api.patch_settings("control", {"mode": "live"})["control"]["mode"] == "live"


def test_ha_client_routes_states_events_and_connect():
    seen = []
    ha = HAClient(lambda e, s: seen.append(("state", e)), token="x",
                  on_event=lambda k, d: seen.append(("event", k)), on_connect=lambda: seen.append(("connect",)))
    ha.watch = {"switch.a"}
    ha._dispatch({"id": 2, "type": "result", "success": True,
                  "result": [{"entity_id": "switch.a"}, {"entity_id": "switch.b"}]})
    ha._dispatch({"id": 3, "type": "result", "success": True})
    ha._dispatch({"type": "event", "event": {"event_type": "tag_scanned", "data": {"tag_id": "t"}}})
    ha._dispatch({"type": "event", "event": {"event_type": "state_changed",
                                             "data": {"new_state": {"entity_id": "switch.a"}}}})
    assert seen == [("state", "switch.a"), ("connect",), ("event", "tag_scanned"), ("state", "switch.a")]
