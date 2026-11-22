"""Build engine signals from Home Assistant state objects."""
from __future__ import annotations

from .model import Signal


def gps_signal(car_id: str, state_obj: dict, zone_entity: str, ts: float) -> Signal:
    attrs = state_obj.get("attributes") or {}
    return Signal("gps", ts, {"car": car_id, "state": state_obj.get("state"),
                              "in_zone": bool(zone_entity) and zone_entity in (attrs.get("in_zones") or [])})


# Charger connector states in which a car is certainly there: plugged in, charging or just finished.
PRESENT_STATES = ("Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing")


def charger_signal(state, ts: float) -> Signal | None:
    """Connector status -> 'a car is plugged in'. unavailable/unknown are no change."""
    if state in (None, "", "unavailable", "unknown"):
        return None
    return Signal("charger", ts, {"present": state in PRESENT_STATES, "status": state})


def occupancy_signal(state: str, ts: float) -> Signal | None:
    """on/off are readings; unavailable/unknown mean the sensor cannot be judged (on=None)."""
    if state == "on":
        return Signal("occupancy", ts, {"on": True})
    if state == "off":
        return Signal("occupancy", ts, {"on": False})
    if state in ("unavailable", "unknown"):
        return Signal("occupancy", ts, {"on": None})
    return None
