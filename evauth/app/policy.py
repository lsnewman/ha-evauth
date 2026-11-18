"""Plate matching, car lookup and the authorisation rule. Pure functions, no I/O."""
from __future__ import annotations

import fnmatch
import re

from .model import UNKNOWN_ID, Latch

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def normalise_plate(plate: str) -> str:
    """Strip every non-alphanumeric character and upper-case, as Frigate's replace_rules do."""
    return _NON_ALNUM.sub("", str(plate or "")).upper()


def plate_matches(plate: str, pattern: str) -> bool:
    """Pattern syntax: * any run, ? one character, [QDO0] one of a set (fnmatch)."""
    return fnmatch.fnmatchcase(normalise_plate(plate), pattern.upper())


def car_for_plate(plate: str, cars: list[dict]) -> str | None:
    for c in cars:
        for pat in c["plate_patterns"]:
            if plate_matches(plate, pat):
                return c["id"]
    return None


def car_for_name(name: str, cars: list[dict], by_class: bool = False) -> str | None:
    n = str(name or "").strip().lower()
    if not n:
        return None
    for c in cars:
        if c["name"].lower() == n or (by_class and c["frigate_class"].lower() == n):
            return c["id"]
    return None


def car_for_class(cls: str, cars: list[dict]) -> str | None:
    n = str(cls or "").strip().lower()
    if not n:
        return None
    for c in cars:
        if c["frigate_class"].lower() == n or c["name"].lower() == n:
            return c["id"]
    return None


def authorises(latch: Latch, sources: dict) -> bool:
    """True when the latch may authorise charging. A classifier-only latch never does."""
    if latch.car is None or latch.car == UNKNOWN_ID or not latch.source:
        return False
    if latch.source == "classifier":
        return False
    sp = sources.get(latch.source)
    return bool(sp and sp.get("authorises"))
