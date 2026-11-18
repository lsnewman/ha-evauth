"""Charging sessions: recorded read-only from the charger entities, or entered by hand.

Energy is booked per car, not per person: the car the latch resolves to at session
end owns it, even when charging began before a car was picked. A finished session
can be moved to another car; the per-car ledger is always derived from sessions
(plus each car's opening balance), so a move adjusts both cars by itself.
"""
from __future__ import annotations

from .model import UNKNOWN_ID

ACTIVE = {"Charging", "SuspendedEV", "SuspendedEVSE"}


class SessionManager:
    def __init__(self, store, latch_getter, clock):
        self.store = store
        self.latch = latch_getter          # () -> Latch
        self.clock = clock
        self.energy: float | None = None   # last session energy seen this session (kWh)
        self.last_car: str | None = None   # last car latched while the session ran
        self.status: str | None = None

    def on_latch(self):
        """Call whenever the latch changes; remembers the car in case it clears before session end."""
        s = self.store.open_session()
        if s and self.latch().car:
            self.last_car = self.latch().car

    def on_energy(self, kwh: float | None):
        if kwh is not None and (self.energy is None or kwh >= self.energy or kwh > 0):
            if kwh > 0:
                self.energy = kwh

    def on_status(self, status: str | None, tx: str | None = None) -> str | None:
        """Feed charger status changes. Returns a session id when one started or ended."""
        prev, self.status = self.status, status
        now = self.clock()
        was_active, is_active = prev in ACTIVE, status in ACTIVE
        if is_active and not was_active and not self.store.open_session():
            self.energy = None
            self.last_car = self.latch().car
            car = self.latch().car
            return self.store.create_session(now, car, origin="charger", source=self.latch().source, tx=tx)
        if was_active and not is_active and prev is not None:
            s = self.store.open_session()
            if s:
                return self._finish(s, now)
        return None

    def _finish(self, s: dict, now: float) -> str:
        car = self.latch().car or self.last_car or UNKNOWN_ID
        src = self.latch().source if self.latch().car else "last_known"
        self.store.update_session(s["id"], end=now, car_id=car, attribution_source=src, kwh=self.energy or 0.0)
        self.energy, self.last_car = None, None
        return s["id"]

    def allocate(self, sid: str, car_id: str, note: str = ""):
        s = self.store.get_session(sid)
        if not s:
            raise KeyError(sid)
        self.store.update_session(sid, car_id=car_id, attribution_source="manual",
                                  note=(note or s["note"] or ""))

    def ledger(self, cars: list[dict]) -> list[dict]:
        totals = self.store.totals()
        rows = [{"car": c["id"], "name": c["name"], "opening_kwh": c["opening_kwh"],
                 "session_kwh": round(totals.get(c["id"], 0.0), 3),
                 "total_kwh": round(c["opening_kwh"] + totals.get(c["id"], 0.0), 3)} for c in cars]
        rows.append({"car": UNKNOWN_ID, "name": "Unknown", "opening_kwh": 0.0,
                     "session_kwh": round(totals.get(UNKNOWN_ID, 0.0), 3),
                     "total_kwh": round(totals.get(UNKNOWN_ID, 0.0), 3)})
        return rows
