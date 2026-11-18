import json

from app import settings
from app.model import Latch
from app.sessions import SessionManager
from app.shadow import Shadow
from app.store import Store
from rig import make_cfg


def test_settings_defaults_roundtrip_and_backup(tmp_path):
    p = str(tmp_path / "config.json")
    s = settings.SettingsStore(p)
    assert s.get()["policy"]["stale_after_s"] == 300
    s.update_section("policy", {"stale_after_s": 120})
    s.update_section("policy", {"stale_after_s": 90})
    assert json.load(open(p))["policy"]["stale_after_s"] == 90
    assert json.load(open(p + ".bak"))["policy"]["stale_after_s"] == 120
    assert settings.SettingsStore(p).get()["policy"]["stale_after_s"] == 90


def test_bad_values_rejected_by_panel_but_never_stop_startup(tmp_path):
    p = tmp_path / "config.json"
    s = settings.SettingsStore(str(p))
    try:
        s.update_section("policy", {"stale_after_s": "abc"})
        assert False
    except settings.ConfigError as e:
        assert "stale_after_s" in str(e)
    p.write_text(json.dumps({"schema_version": 1, "policy": {"stale_after_s": "abc", "empty_clear_s": 77},
                             "cars": [{"name": "Good"}, {"name": ""}], "log_level": "loud"}))
    s2 = settings.SettingsStore(str(p))
    d = s2.get()
    assert d["policy"]["stale_after_s"] == 300 and d["policy"]["empty_clear_s"] == 77
    assert [c["name"] for c in d["cars"]] == ["Good"] and d["log_level"] == "info"
    assert s2.warnings


def test_corrupt_file_falls_back_to_backup_then_defaults(tmp_path):
    p = tmp_path / "config.json"
    s = settings.SettingsStore(str(p))
    s.update_section("policy", {"stale_after_s": 111})
    s.update_section("policy", {"stale_after_s": 112})
    p.write_text("{not json")
    s2 = settings.SettingsStore(str(p))
    assert s2.get()["policy"]["stale_after_s"] == 111 and s2.warnings
    (tmp_path / "config.json.bak").write_text("also bad")
    assert settings.SettingsStore(str(p)).get()["policy"]["stale_after_s"] == 300


def test_old_schema_document_is_migrated_not_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(settings.MIGRATIONS, 1, lambda d: {**d, "policy": {**d.get("policy", {}), "stale_after_s": 250}})
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"schema_version": 1, "policy": {"empty_clear_s": 60}}))
    d = settings.SettingsStore(str(p)).get()
    assert d["policy"]["stale_after_s"] == 250 and d["policy"]["empty_clear_s"] == 60


def test_cars_users_validation():
    import pytest
    with pytest.raises(settings.ConfigError):
        settings.validate_cars([{"name": "Unknown"}])
    with pytest.raises(settings.ConfigError):
        settings.validate_cars([{"name": "A"}, {"name": "a"}])
    cars = settings.validate_cars([{"name": "My Car", "plate_patterns": "ab-c 12*, x?y"}])
    assert cars[0]["id"] == "my_car" and cars[0]["plate_patterns"] == ["ABC12*", "X?Y"]
    with pytest.raises(settings.ConfigError):
        settings.validate_users([{"name": "U", "default_car": "nope"}], {"a"})


def test_export_hides_mqtt_password(tmp_path):
    s = settings.SettingsStore(str(tmp_path / "c.json"))
    s.update_section("mqtt", {"host": "h", "password": "secret"})
    assert s.export()["mqtt"]["password"] == "" and s.get()["mqtt"]["password"] == "secret"


def test_sessions_energy_booked_to_car_resolved_at_end_and_reallocation():
    st = Store()
    latch = Latch()
    t = [100.0]
    cars = make_cfg()["cars"]
    sm = SessionManager(st, lambda: latch, lambda: t[0])
    sm.on_status("Available")
    sm.on_status("Charging")                              # starts before any car is picked
    sm.on_energy(3.0)
    latch.car, latch.source = "bravo", "prompt"           # picked mid-session
    sm.on_latch()
    t[0] = 500
    sm.on_energy(7.5)
    sid = sm.on_status("Finishing")
    s = st.get_session(sid)
    assert s["car_id"] == "bravo" and s["kwh"] == 7.5 and s["end"] == 500
    assert {r["car"]: r["total_kwh"] for r in sm.ledger(cars)}["bravo"] == 7.5
    sm.allocate(sid, "alpha")
    led = {r["car"]: r["total_kwh"] for r in sm.ledger(cars)}
    assert led["alpha"] == 7.5 and led["bravo"] == 0.0


def test_session_uses_last_known_car_if_latch_cleared_and_unknown_if_none():
    st = Store()
    latch = Latch(car="alpha", source="gps")
    sm = SessionManager(st, lambda: latch, lambda: 1.0)
    sm.on_status("Charging")
    latch.car = None
    sid = sm.on_status("Available")
    assert st.get_session(sid)["car_id"] == "alpha"
    sm2 = SessionManager(Store(), lambda: Latch(), lambda: 1.0)
    sm2.on_status("Charging")
    assert sm2.store.get_session(sm2.on_status("Available"))["car_id"] == "unknown"


def test_shadow_agree_disagree_settle_and_close():
    st = Store()
    cfg = make_cfg()
    sh = Shadow(st, lambda: cfg)
    sh.update_ha("none", "none", 0)
    assert sh.poll(10) == []                              # both empty: agree
    sh.note_engine(Latch(car="alpha", source="lpr"), None, 20)
    assert sh.poll(22) == []                              # inside the settle window
    ev = sh.poll(26)
    assert ev and ev[0]["event"] == "opened" and ev[0]["kind"] == "car"
    sh.note_engine(Latch(car="alpha", source="lpr"), None, 30)
    assert sh.poll(40) == []                              # same pair: not duplicated
    sh.update_ha("Alpha", "lpr", 41)
    ev = sh.poll(50)
    assert ev and ev[0]["event"] == "closed"
    assert len(st.disagreements()) == 1 and st.disagreements()[0]["resolved_ts"] == 50


def test_shadow_source_only_difference_is_its_own_kind():
    st = Store()
    cfg = make_cfg()
    sh = Shadow(st, lambda: cfg)
    sh.update_ha("Alpha", "lpr", 0)
    sh.note_engine(Latch(car="alpha", source="gps"), None, 0)
    ev = sh.poll(10)
    assert ev[0]["kind"] == "source"


def test_shadow_unmapped_ha_car_is_a_disagreement():
    st = Store()
    sh = Shadow(st, lambda: make_cfg())
    sh.update_ha("Mystery", "lpr", 0)
    assert sh.poll(10)[0]["kind"] == "car"


def test_store_events_and_annotation():
    st = Store()
    from app.model import Decision
    st.add_event(Decision(1.0, "lpr", "set", "alpha", "lpr", "confident", "lpr.set", "why"))
    assert st.latch_history()[0]["car"] == "alpha" and st.events()[0]["rule"] == "lpr.set"
    did = st.open_disagreement(1, "car", ("a", "lpr"), (None, None), "lpr", "r", "x")
    st.annotate_disagreement(did, "intended", "expected")
    assert st.disagreements("intended")[0]["note"] == "expected"
