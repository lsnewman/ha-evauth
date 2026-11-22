"""The latching engine: pure, deterministic, no I/O and no real clock.

A latch is (car, source, tier, set_at). Signals go in through `handle`, time passes
through `tick`; every call returns the Decisions it made, which the caller logs.
Fixed in code (not configurable): an Unknown read never overwrites a known latch,
the classifier never authorises.
"""
from __future__ import annotations

import copy

from .model import UNKNOWN_ID, Decision, Latch, Signal
from .policy import authorises, car_for_class, car_for_name, car_for_plate

HOME = "home"
UNAVAILABLE = (None, "unavailable", "unknown")


class _Tracker:
    __slots__ = ("state", "in_zone", "away_since", "away_fired")

    def __init__(self):
        self.state = None
        self.in_zone = False
        self.away_since = None
        self.away_fired = False


class Engine:
    def __init__(self, cfg: dict, latch: Latch | None = None):
        self.cfg = cfg
        self.latch = latch or Latch()
        self.occupied: bool | None = None
        self.empty_since: float | None = None
        self.empty_timeout: float = 300.0       # how long "empty" must last: normal after a real on->off, plus the restart grace when unconfirmed
        self.empty_checked = True
        self.trackers: dict[str, _Tracker] = {}
        self.charger_present = False            # a car is plugged in / charging (connector status)
        self._held_empty = False                # hold already logged, so the log is not spammed every tick
        self._held_gps: set[str] = set()

    # ------------------------------------------------------------ helpers
    @property
    def cars(self) -> list[dict]:
        return self.cfg["cars"]

    @property
    def sources(self) -> dict:
        return self.cfg["sources"]

    @property
    def policy(self) -> dict:
        return self.cfg["policy"]

    def set_config(self, cfg: dict):
        self.cfg = cfg

    def snapshot(self) -> dict:
        return {"latch": self.latch.to_dict(), "occupied": self.occupied, "charger_present": self.charger_present,
                "authorised": self.authorised(),
                "trackers": {k: {"state": t.state, "in_zone": t.in_zone} for k, t in self.trackers.items()}}

    def authorised(self) -> bool:
        return authorises(self.latch, self.sources)

    def _holds(self) -> bool:
        """A car that is plugged in or charging is certainly there: it keeps its latch."""
        return bool(self.policy.get("charger_holds_latch") and self.charger_present and self.latch.car is not None)

    def _tracker(self, car_id: str) -> _Tracker:
        return self.trackers.setdefault(car_id, _Tracker())

    def _car(self, car_id: str) -> dict | None:
        return next((c for c in self.cars if c["id"] == car_id), None)

    def _dec(self, sig: str, ts: float, action: str, rule: str, reason: str, **kw) -> Decision:
        return Decision(ts=ts, signal=sig, action=action, car=kw.pop("car", self.latch.car),
                        source=kw.pop("source", self.latch.source), tier=self.latch.tier, rule=rule,
                        reason=reason, authorises=self.authorised(), **kw)

    # ------------------------------------------------------------ latch writes
    def apply(self, source: str, car: str, ts: float, sig: str | None = None, session_id=None,
              detail: dict | None = None) -> Decision:
        """Try to latch `car` from `source`, honouring the policy matrix."""
        sig = sig or source
        sp = self.sources[source]
        cur = self.latch
        detail = detail or {}
        name = self._label(car)

        def keep(rule, why):
            return Decision(ts=ts, signal=sig, action="keep", car=cur.car, source=cur.source, tier=cur.tier,
                            rule=f"{source}.{rule}", reason=why, authorises=self.authorised(), detail=detail)

        def do_set(rule, why):
            self.latch = Latch(car=car, source=source, tier=sp["tier"], set_at=ts,
                               session_id=session_id if session_id else cur.session_id)
            if self.occupied is False:          # latched while the zone reads empty: the clear clock restarts
                self.empty_since, self.empty_timeout = ts, self.policy["empty_clear_s"]
                self.empty_checked = False
            return Decision(ts=ts, signal=sig, action="set", car=car, source=source, tier=sp["tier"],
                            rule=f"{source}.{rule}", reason=why, authorises=self.authorised(), detail=detail)

        if car == UNKNOWN_ID:
            if cur.car is None:
                return do_set("set_unknown", f"{source}: no known car matched; latched Unknown on an empty latch")
            return keep("unknown_kept", f"{source}: Unknown read ignored; latch stays {self._label(cur.car)}")

        if cur.car == car:
            if sp["refresh_same"]:
                return do_set("refresh", f"{source}: {name} seen again; latch time refreshed")
            if (self.policy.get("upgrade_same_car") and sp.get("authorises") and source != "classifier"
                    and not self.authorised()):
                return do_set("upgrade", f"{source}: confirmed {name}; latch upgraded from {cur.source}")
            return keep("same", f"{source}: {name} already latched")

        if cur.car is None:
            ok, cls = sp["over_empty"], "empty"
        elif cur.car == UNKNOWN_ID:
            ok, cls = sp["over_unknown"], "Unknown"
        elif ts - cur.set_at > self.policy["stale_after_s"]:
            ok, cls = sp["over_known_stale"], f"stale {self._label(cur.car)}"
        else:
            ok, cls = sp["over_known_fresh"], f"fresh {self._label(cur.car)}"
        if ok:
            return do_set("set", f"{source}: latched {name} over {cls}")
        return keep("blocked", f"{source}: {name} may not overwrite {cls}")

    def clear(self, ts: float, sig: str, rule: str, reason: str) -> Decision:
        was = self.latch.car
        self.latch = Latch()
        return Decision(ts=ts, signal=sig, action="clear", car=None, source=None, tier=None, rule=rule,
                        reason=reason, authorises=False, detail={"was": was})

    def _label(self, car_id) -> str:
        if car_id is None:
            return "none"
        if car_id == UNKNOWN_ID:
            return "Unknown"
        c = self._car(car_id)
        return c["name"] if c else str(car_id)

    # ------------------------------------------------------------ signals
    def handle(self, sig: Signal) -> list[Decision]:
        fn = getattr(self, f"_on_{sig.kind}", None)
        return fn(sig) if fn else []

    def _on_lpr(self, sig: Signal) -> list[Decision]:
        d = sig.data
        minimum = self.sources["lpr"]["min_score"]
        if d.get("score", 0.0) < minimum:
            return [self._dec("lpr", sig.ts, "ignore", "lpr.low_score",
                              f"lpr: plate score {d.get('score', 0):.2f} below {minimum:.2f}",
                              detail={"plate": d.get("plate")})]
        car = car_for_plate(d.get("plate", ""), self.cars)
        how = "plate pattern"
        if car is None and self.cfg["frigate"].get("trust_sub_label") and d.get("sub_label"):
            car = car_for_name(d["sub_label"], self.cars)
            how = "Frigate sub_label"
        if car is None:
            car, how = UNKNOWN_ID, "no pattern matched"
        return [self.apply("lpr", car, sig.ts, "lpr", detail={"plate": d.get("plate"), "matched_by": how,
                                                              "score": d.get("score")})]

    def _on_classifier(self, sig: Signal) -> list[Decision]:
        d = sig.data
        minimum = self.sources["classifier"]["min_score"]
        if d.get("score", 0.0) < minimum:
            return [self._dec("classifier", sig.ts, "ignore", "classifier.low_score",
                              f"classifier: score {d.get('score', 0):.2f} below {minimum:.2f}")]
        car = car_for_class(d.get("name", ""), self.cars)
        if car is None:
            return [self._dec("classifier", sig.ts, "ignore", "classifier.unmapped",
                              f"classifier: class {d.get('name')!r} is not a configured car")]
        return [self.apply("classifier", car, sig.ts, "classifier", detail={"score": d.get("score")})]

    def _on_charger(self, sig: Signal) -> list[Decision]:
        was, self.charger_present = self.charger_present, bool(sig.data.get("present"))
        if was and not self.charger_present:
            # Plug-in ended: give the car a fresh grace period before any clear that was being held.
            if self.occupied is not True and self.empty_since is not None:
                self.empty_since, self.empty_checked = sig.ts, False
            for t in self.trackers.values():
                if t.away_since is not None and not t.away_fired:
                    t.away_since = sig.ts
            self._held_empty = False
            self._held_gps.clear()
            return [self._dec("charger", sig.ts, "keep", "charger.released",
                              "charger no longer plugged in; clear timers restarted")]
        return []

    def _on_occupancy(self, sig: Signal) -> list[Decision]:
        on = sig.data.get("on")
        prev = self.occupied
        out: list[Decision] = []
        if on is None:
            # Sensor lost (HA or Frigate restarting): presence cannot be judged. Like the old automation, a
            # pending clear is cancelled; an unconfirmed latch gets the restart grace on top of the normal clear time.
            self.occupied = None
            self.empty_since, self.empty_checked = None, True
            if self.latch.car is not None:
                self.empty_since, self.empty_timeout = sig.ts, self.policy["restart_grace_s"] + self.policy["empty_clear_s"]
                self.empty_checked = False
            return out
        self.occupied = bool(on)
        if on:
            self.empty_since = None
            if prev is False:                       # off -> on is a GPS arrival trigger
                for car in self.cars:
                    out += self._gps_arrival(car["id"], sig.ts, "occupancy")
        elif prev is True:                          # a real on -> off reading
            self.empty_since, self.empty_timeout = sig.ts, self.policy["empty_clear_s"]
            self.empty_checked = False
        elif prev is None and self.latch.car is not None and (self.empty_since is None or self.empty_checked):
            # First reading after a restart (or after the sensor was lost) is not a departure; the old
            # automation never saw an on -> off here either. Keep the latch, but only for so long.
            self.empty_since, self.empty_timeout = sig.ts, self.policy["restart_grace_s"] + self.policy["empty_clear_s"]
            self.empty_checked = False
        return out

    def _on_gps(self, sig: Signal) -> list[Decision]:
        car_id = sig.data["car"]
        t = self._tracker(car_id)
        prev_state, prev_zone = t.state, t.in_zone
        t.state = sig.data.get("state")
        t.in_zone = bool(sig.data.get("in_zone"))
        # leaving-home timer
        if t.state == HOME:
            t.away_since, t.away_fired = None, False
        elif t.state in UNAVAILABLE:
            t.away_since = None                     # an unavailable blip cancels a pending clear
        elif t.away_since is None and not t.away_fired:
            t.away_since = sig.ts
        out: list[Decision] = []
        arrived = (prev_state is not None and prev_state != HOME and t.state == HOME) or \
                  (prev_state is not None and not prev_zone and t.in_zone)
        if arrived:
            out += self._gps_arrival(car_id, sig.ts, "gps")
        return out

    def _gps_arrival(self, car_id: str, ts: float, why: str) -> list[Decision]:
        t = self.trackers.get(car_id)
        car = self._car(car_id)
        if not t or not car or not car["gps_tracker"]:
            return []
        if t.state != HOME or not t.in_zone or self.occupied is not True:
            return []
        return [self.apply("gps", car_id, ts, "gps", detail={"trigger": why})]

    # ------------------------------------------------------------ time
    def tick(self, now: float) -> list[Decision]:
        out: list[Decision] = []
        p = self.policy
        # zone empty long enough
        if self.occupied is not True and self.empty_since is not None and not self.empty_checked \
                and now - self.empty_since >= self.empty_timeout:
            if self._holds():
                if not self._held_empty:
                    self._held_empty = True
                    out.append(self._dec("occupancy", now, "keep", "clear.empty_held_charging",
                                         "zone reads empty but the car is plugged in or charging; latch kept"))
            else:
                self.empty_checked = True
                self._held_empty = False
                out += self._clear_when_empty(now)
        # tracker away from home long enough
        for car_id, t in self.trackers.items():
            if t.away_since is not None and not t.away_fired and now - t.away_since >= p["gps_leave_clear_s"]:
                if self.latch.car == car_id and self._holds():
                    if car_id not in self._held_gps:
                        self._held_gps.add(car_id)
                        out.append(self._dec("gps", now, "keep", "clear.gps_held_charging",
                                             f"{self._label(car_id)} reads away from home but is plugged in or charging; latch kept"))
                    continue
                t.away_fired = True
                t.away_since = None
                if self.latch.car == car_id:
                    out.append(self.clear(now, "gps", "clear.gps_left_home",
                                          f"{self._label(car_id)} left home to {t.state!r}; latch cleared"))
                else:
                    out.append(self._dec("gps", now, "keep", "clear.gps_left_home_other",
                                         f"{self._label(car_id)} left home but is not the latched car"))
        return out

    def _clear_when_empty(self, now: float) -> list[Decision]:
        cur = self.latch
        if cur.car is None:
            return []
        if cur.car != UNKNOWN_ID:
            car = self._car(cur.car)
            if car and car["gps_tracker"]:
                t = self.trackers.get(cur.car)
                state = t.state if t else None
                if state in UNAVAILABLE or (t and t.in_zone):
                    why = "tracker state unknown" if state in UNAVAILABLE else "tracker still in the zone"
                    return [self._dec("occupancy", now, "keep", "clear.empty_protected",
                                      f"zone empty {int(self.empty_timeout)} s but latch kept: {why}")]
        return [self.clear(now, "occupancy", "clear.empty",
                           f"zone empty for {int(self.empty_timeout)} s; latch cleared")]

    def next_deadline(self) -> float | None:
        """Earliest time `tick` could act; lets the caller sleep precisely."""
        ds = []
        if self.occupied is not True and self.empty_since is not None and not self.empty_checked:
            ds.append(self.empty_since + self.empty_timeout)
        for t in self.trackers.values():
            if t.away_since is not None and not t.away_fired:
                ds.append(t.away_since + self.policy["gps_leave_clear_s"])
        return min(ds) if ds else None


def clone(e: Engine) -> Engine:
    return copy.deepcopy(e)
