"""Shadow mode: compare the engine's latch with the HA helpers' latch and log every disagreement.

Writes nothing to HA. After either side changes, wait `shadow_settle_s` (so the two
sides can finish reacting to the same input), then compare. One disagreement row is
opened per distinct (engine, HA) pair, and closed with a time when they agree again.
"""
from __future__ import annotations

from .model import SCAN_SOURCES, UNKNOWN_ID, UNKNOWN_NAME


class Shadow:
    def __init__(self, store, cfg_getter):
        self.store = store
        self.cfg = cfg_getter
        self.ha = {"vehicle": None, "source": None}
        self.ha_seen = False
        self.engine = (None, None)
        self.due: float | None = None
        self.open: dict | None = None      # {"id", "key"}
        self.last_decision = None
        self.last_ha_change: float | None = None

    # HA helper names -> (car id | None, source | None)
    def _ha_pair(self):
        veh = (self.ha.get("vehicle") or "").strip()
        src = (self.ha.get("source") or "").strip() or None
        if src == "none":
            src = None
        if veh.lower() in ("", "none", "unavailable", "unknown_state"):
            return None, src
        if veh == UNKNOWN_NAME:
            return UNKNOWN_ID, src
        for c in self.cfg()["cars"]:
            if c["name"].lower() == veh.lower():
                return c["id"], src
        return f"?{veh}", src            # a car the app does not know: always a disagreement

    def update_ha(self, vehicle, source, now: float):
        changed = (vehicle, source) != (self.ha["vehicle"], self.ha["source"])
        self.ha = {"vehicle": vehicle, "source": source}
        first = not self.ha_seen
        self.ha_seen = True
        if changed or first:
            self.last_ha_change = now
            self.due = now + self.cfg()["policy"]["shadow_settle_s"]
        return first

    def note_engine(self, latch, decision, now: float):
        pair = (latch.car, latch.source)
        if decision is not None:
            self.last_decision = decision
        if pair != self.engine or decision is not None and decision.action in ("set", "clear"):
            self.engine = pair
            self.due = now + self.cfg()["policy"]["shadow_settle_s"]

    def poll(self, now: float) -> list[dict]:
        """Returns what changed this call: [{"event": "opened"|"closed", ...}]. Call about once a second."""
        if self.due is None or now < self.due or not self.ha_seen:
            return []
        self.due = None
        ha = self._ha_pair()
        eng = self.engine
        out = []
        if ha[0] != eng[0]:
            kind = "car"
        elif ha[1] != eng[1] and eng[0] is not None:
            kind = "source"
        else:
            kind = None
        key = (kind, eng, ha)
        if kind is None:
            if self.open:
                self.store.close_disagreement(self.open["id"], now)
                out.append({"event": "closed", "id": self.open["id"]})
                self.open = None
            return out
        if self.open and self.open["key"] == key:
            return out
        if self.open:
            self.store.close_disagreement(self.open["id"], now)
            out.append({"event": "closed", "id": self.open["id"]})
        d = self.last_decision
        # The HA helpers never hear of the app's scan flow, so differences it causes need no review.
        scan = eng[1] in SCAN_SOURCES
        did = self.store.open_disagreement(
            now, kind, eng, ha, d.signal if d else "", d.rule if d else "",
            d.reason if d else "no engine decision yet",
            status="intended" if scan else "open", note="app scan flow" if scan else "")
        self.open = {"id": did, "key": key}
        out.append({"event": "opened", "id": did, "kind": kind})
        return out

    def status(self) -> dict:
        return {"enabled": True, "ha": dict(self.ha), "ha_seen": self.ha_seen, "engine": list(self.engine),
                "open_now": bool(self.open), "counts": self.store.disagreement_counts()}
