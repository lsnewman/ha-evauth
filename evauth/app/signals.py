"""Build engine signals from Home Assistant state objects."""
from __future__ import annotations

from .model import Signal


def gps_signal(car_id: str, state_obj: dict, zone_entity: str, ts: float) -> Signal:
    attrs = state_obj.get("attributes") or {}
    return Signal("gps", ts, {"car": car_id, "state": state_obj.get("state"),
                              "in_zone": bool(zone_entity) and zone_entity in (attrs.get("in_zones") or [])})


def occupancy_signal(state: str, ts: float) -> Signal | None:
    """on/off become signals; unavailable/unknown are 'no change' (never trigger a clear)."""
    if state == "on":
        return Signal("occupancy", ts, {"on": True})
    if state == "off":
        return Signal("occupancy", ts, {"on": False})
    return None
