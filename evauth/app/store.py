"""SQLite store (/data/evauth.db): event log, latch history, sessions, shadow disagreements."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, signal TEXT, action TEXT, car TEXT, source TEXT,
  tier TEXT, rule TEXT, reason TEXT, authorises INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS latch_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, action TEXT, car TEXT, source TEXT, tier TEXT, rule TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, start REAL, "end" REAL, car_id TEXT, orig_car_id TEXT, attribution_source TEXT,
  kwh REAL, charger_tx TEXT, note TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS disagreements(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, engine_car TEXT, engine_source TEXT,
  ha_car TEXT, ha_source TEXT, signal TEXT, rule TEXT, reason TEXT, status TEXT DEFAULT 'open',
  note TEXT DEFAULT '', resolved_ts REAL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""
EVENT_KEEP = 5000        # rows kept in the event log; older rows are trimmed


class Store:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _q(self, sql, args=(), one=False):
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            self._db.commit()
        return (rows[0] if rows else None) if one else rows

    def _x(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    # ---------------------------------------------------------- events
    def add_event(self, d) -> int:
        n = self._x("INSERT INTO events(ts,signal,action,car,source,tier,rule,reason,authorises,detail) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (d.ts, d.signal, d.action, d.car, d.source, d.tier, d.rule, d.reason,
                     int(d.authorises), json.dumps(d.detail)))
        if d.action in ("set", "clear"):
            self._x("INSERT INTO latch_history(ts,action,car,source,tier,rule) VALUES(?,?,?,?,?,?)",
                    (d.ts, d.action, d.car, d.source, d.tier, d.rule))
        if n % 500 == 0:
            self._x("DELETE FROM events WHERE id <= ?", (n - EVENT_KEEP,))
        return n

    def add_note(self, ts: float, signal: str, reason: str, rule: str = "note"):
        self._x("INSERT INTO events(ts,signal,action,rule,reason,authorises,detail) VALUES(?,?,?,?,?,0,'{}')",
                (ts, signal, "note", rule, reason))

    def events(self, limit: int = 200, action: str | None = None) -> list[dict]:
        if action:
            return self._q("SELECT * FROM events WHERE action=? ORDER BY id DESC LIMIT ?", (action, limit))
        return self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def notes(self, signal: str, limit: int = 20) -> list[dict]:
        return self._q("SELECT ts, reason FROM events WHERE signal=? AND action='note' ORDER BY id DESC LIMIT ?",
                       (signal, limit))

    def latch_history(self, limit: int = 100) -> list[dict]:
        return self._q("SELECT * FROM latch_history ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------- kv (persisted engine latch)
    def kv_set(self, key: str, value):
        self._x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    def kv_get(self, key: str, default=None):
        r = self._q("SELECT value FROM kv WHERE key=?", (key,), one=True)
        return json.loads(r["value"]) if r else default

    # ---------------------------------------------------------- sessions
    def create_session(self, start: float, car_id: str | None, origin: str = "manual", source: str | None = None,
                       tx: str | None = None, note: str = "", kwh: float | None = None,
                       end: float | None = None) -> str:
        sid = uuid.uuid4().hex[:12]
        self._x('INSERT INTO sessions(id,start,"end",car_id,orig_car_id,attribution_source,kwh,charger_tx,note,origin)'
                " VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, start, end, car_id, car_id, source, kwh, tx, note, origin))
        return sid

    def get_session(self, sid: str) -> dict | None:
        return self._q("SELECT * FROM sessions WHERE id=?", (sid,), one=True)

    def open_session(self) -> dict | None:
        return self._q('SELECT * FROM sessions WHERE "end" IS NULL ORDER BY start DESC LIMIT 1', one=True)

    def update_session(self, sid: str, **fields):
        allowed = {"start", "end", "car_id", "attribution_source", "kwh", "note", "charger_tx"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        cols = ", ".join(f'"{k}"=?' for k in fields)
        self._x(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))

    def sessions(self, limit: int = 200, car_id: str | None = None) -> list[dict]:
        if car_id:
            return self._q("SELECT * FROM sessions WHERE car_id=? ORDER BY start DESC LIMIT ?", (car_id, limit))
        return self._q("SELECT * FROM sessions ORDER BY start DESC LIMIT ?", (limit,))

    def delete_session(self, sid: str):
        self._x("DELETE FROM sessions WHERE id=?", (sid,))

    def totals(self) -> dict[str, float]:
        rows = self._q("SELECT car_id, SUM(kwh) AS kwh FROM sessions WHERE kwh IS NOT NULL AND car_id IS NOT NULL "
                       "GROUP BY car_id")
        return {r["car_id"]: r["kwh"] or 0.0 for r in rows}

    # ---------------------------------------------------------- disagreements
    def open_disagreement(self, ts, kind, engine, ha, signal, rule, reason, status="open", note="") -> int:
        return self._x("INSERT INTO disagreements(ts,kind,engine_car,engine_source,ha_car,ha_source,signal,rule,reason,"
                       "status,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (ts, kind, engine[0], engine[1], ha[0], ha[1], signal, rule, reason, status, note))

    def close_disagreement(self, did: int, ts: float):
        self._x("UPDATE disagreements SET resolved_ts=? WHERE id=? AND resolved_ts IS NULL", (ts, did))

    def disagreements(self, status: str | None = None, limit: int = 500) -> list[dict]:
        if status:
            return self._q("SELECT * FROM disagreements WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        return self._q("SELECT * FROM disagreements ORDER BY id DESC LIMIT ?", (limit,))

    def annotate_disagreement(self, did: int, status: str, note: str):
        if status not in ("open", "explained", "intended", "bug"):
            raise ValueError("status must be open, explained, intended or bug")
        self._x("UPDATE disagreements SET status=?, note=? WHERE id=?", (status, note, did))

    def disagreement_counts(self) -> dict:
        rows = self._q("SELECT status, COUNT(*) AS n FROM disagreements GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
