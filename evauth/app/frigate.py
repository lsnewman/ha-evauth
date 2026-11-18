"""Turn a frigate/events payload into engine signals.

Mirrors the HA automations' gates (camera, label, zone) but never keys on object
ids: Frigate churns them, so every payload is judged on its own content.
Assumption to verify against a real payload: the classifier result is the
object's own `Car Identity` [name, score] (top level of before/after).
"""
from __future__ import annotations

from .model import Signal


def _pair(v):
    """[value, score] -> (value, score); anything else -> (None, 0.0)."""
    if isinstance(v, (list, tuple)) and v:
        try:
            return str(v[0]), float(v[1]) if len(v) > 1 else 0.0
        except (TypeError, ValueError):
            return str(v[0]), 0.0
    if isinstance(v, str) and v:
        return v, 0.0
    return None, 0.0


def _gate(after: dict, fr: dict) -> str | None:
    """None if the object passes the camera/label/zone gate, else the reason it did not."""
    cams = {fr.get("camera")}
    if fr.get("accept_test_camera") and fr.get("test_camera"):
        cams.add(fr["test_camera"])
    cams.discard("")
    cams.discard(None)
    if after.get("camera") not in cams:
        return "camera"
    if after.get("label") != fr.get("label", "car"):
        return "label"
    if fr.get("zone") and fr["zone"] not in (after.get("current_zones") or []):
        return "zone"
    return None


def parse_event(payload: dict, fr: dict, ts: float) -> tuple[list[Signal], str | None]:
    """Returns (signals, ignored_reason). Signals may be empty with reason None (nothing new)."""
    if not isinstance(payload, dict):
        return [], "not an object"
    before = payload.get("before") or {}
    after = payload.get("after") or {}
    if not after:
        return [], "no after"
    why = _gate(after, fr)
    if why:
        return [], why
    out: list[Signal] = []

    plate, pscore = _pair(after.get("recognized_license_plate"))
    bplate, _ = _pair(before.get("recognized_license_plate"))
    sub, sscore = _pair(after.get("sub_label"))
    bsub, _ = _pair(before.get("sub_label"))
    # Same firing rule as automation 1: first plate, or the sub_label arriving after it.
    if plate and (not bplate or (not bsub and sub)):
        out.append(Signal("lpr", ts, {"plate": plate, "score": pscore, "sub_label": sub,
                                      "sub_score": sscore, "camera": after.get("camera")}))

    attr = fr.get("classifier_attribute", "Car Identity")
    name, cscore = _pair(after.get(attr))
    bname, bcscore = _pair(before.get(attr))
    if name and (name != bname or cscore != bcscore):
        out.append(Signal("classifier", ts, {"name": name, "score": cscore, "camera": after.get("camera")}))
    return out, None
