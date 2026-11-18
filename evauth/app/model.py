"""Plain data types shared by the engine, store and panel. No I/O here."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

UNKNOWN_ID = "unknown"
UNKNOWN_NAME = "Unknown"
SOURCES = ("lpr", "gps", "classifier", "nfc", "prompt", "default")
TIERS = ("confident", "non_confident")


@dataclass(frozen=True)
class Car:
    id: str
    name: str
    plate_patterns: tuple[str, ...] = ()
    frigate_class: str = ""
    gps_tracker: str = ""
    energy_ledger: str = ""
    opening_kwh: float = 0.0


@dataclass(frozen=True)
class User:
    id: str
    name: str
    phone_device_id: str = ""      # the HA mobile_app device that scans the tag
    notify_target: str = ""        # e.g. notify.mobile_app_<phone>
    default_car: str = ""          # car id
    allowed: bool = True
    platform: str = "ios"          # ios | android (picker style, phase 4)


@dataclass
class Latch:
    car: str | None = None         # car id, UNKNOWN_ID, or None for "no car"
    source: str | None = None
    tier: str | None = None
    set_at: float = 0.0            # epoch seconds
    session_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signal:
    """One input to the engine. `kind` is lpr | classifier | gps | occupancy | scan | pick | tick."""
    kind: str
    ts: float
    data: dict = field(default_factory=dict)


@dataclass
class Decision:
    ts: float
    signal: str                    # kind of the signal that caused it
    action: str                    # set | clear | keep | ignore
    car: str | None
    source: str | None
    tier: str | None
    rule: str                      # short machine id, e.g. "lpr.over_known"
    reason: str                    # human sentence for the log
    authorises: bool = False
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
