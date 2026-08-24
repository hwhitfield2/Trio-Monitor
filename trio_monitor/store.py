"""SQLite persistence for glucose entries, treatments, and device status.

One store is shared by the per-user HTTP servers (writers, on their own
threads) and the display loop (reader), so all access goes through a lock.
"""

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    user      TEXT NOT NULL,
    date      INTEGER NOT NULL,          -- ms since epoch
    sgv       REAL,
    direction TEXT,
    raw       TEXT,
    UNIQUE(user, date) ON CONFLICT REPLACE
);
CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user, date DESC);

CREATE TABLE IF NOT EXISTS treatments (
    id         TEXT PRIMARY KEY,
    user       TEXT NOT NULL,
    created_at INTEGER NOT NULL,         -- ms since epoch
    event_type TEXT,
    carbs      REAL,
    insulin    REAL,
    raw        TEXT
);
CREATE INDEX IF NOT EXISTS idx_treatments_user_date ON treatments(user, created_at DESC);

CREATE TABLE IF NOT EXISTS devicestatus (
    user       TEXT NOT NULL,
    created_at INTEGER NOT NULL,         -- ms since epoch
    iob        REAL,
    cob        REAL,
    raw        TEXT
);
CREATE INDEX IF NOT EXISTS idx_devicestatus_user_date ON devicestatus(user, created_at DESC);
DELETE FROM devicestatus WHERE rowid NOT IN
    (SELECT MAX(rowid) FROM devicestatus GROUP BY user, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_devicestatus_user_date ON devicestatus(user, created_at);

CREATE TABLE IF NOT EXISTS params (
    user TEXT PRIMARY KEY,
    raw  TEXT
);
"""


def parse_time_ms(doc: dict, *keys: str) -> int:
    """Extract a timestamp in ms from the first usable key in the document.

    Accepts numeric epoch values (s or ms) and ISO-8601 strings.
    Falls back to the current time.
    """
    for key in keys:
        value = doc.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            # Heuristic: values before year ~2033 in seconds are < 2e9.
            return int(value if value > 1e11 else value * 1000)
        if isinstance(value, str):
            try:
                text = value.replace("Z", "+00:00")
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return int(time.time() * 1000)


def extract_iob_cob(doc: dict) -> tuple[float | None, float | None]:
    """Pull IOB/COB out of an openaps-style (Trio/oref) or Loop devicestatus."""
    iob = cob = None
    openaps = doc.get("openaps") or {}
    iob_doc = openaps.get("iob")
    if isinstance(iob_doc, dict):
        iob = iob_doc.get("iob")
    for key in ("suggested", "enacted"):
        section = openaps.get(key) or {}
        if isinstance(section, dict):
            if iob is None:
                iob = section.get("IOB")
            if cob is None:
                cob = section.get("COB")
    loop = doc.get("loop") or {}
    if iob is None and isinstance(loop.get("iob"), dict):
        iob = loop["iob"].get("iob")
    if cob is None and isinstance(loop.get("cob"), dict):
        cob = loop["cob"].get("cob")
    return iob, cob


@dataclass
class UserSnapshot:
    """Everything the display needs to draw one person's panel."""

    sgv: float | None = None
    sgv_date: int | None = None          # ms epoch
    direction: str | None = None
    delta: float | None = None
    iob: float | None = None
    cob: float | None = None
    status_date: int | None = None
    status_raw: dict | None = None       # full devicestatus doc (predictions live here)
    last_carbs: float | None = None
    last_carbs_date: int | None = None
    last_bolus: float | None = None
    last_bolus_date: int | None = None
    history: list[tuple[int, float]] = field(default_factory=list)
    boluses: list[tuple[int, float]] = field(default_factory=list)  # (ms, units)
    params: dict = field(default_factory=dict)   # therapy settings (isf/cr/dia)


class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- writes (called by the HTTP servers) ----

    def add_entries(self, user: str, docs: list[dict]) -> list[dict]:
        stored = []
        with self._lock:
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                sgv = doc.get("sgv", doc.get("glucose"))
                if sgv is None:
                    continue
                date = parse_time_ms(doc, "date", "dateString")
                self._db.execute(
                    "INSERT INTO entries (user, date, sgv, direction, raw) VALUES (?, ?, ?, ?, ?)",
                    (user, date, float(sgv), doc.get("direction"), json.dumps(doc)),
                )
                stored.append(doc)
            self._db.commit()
        return stored

    def add_treatments(self, user: str, docs: list[dict]) -> list[dict]:
        stored = []
        with self._lock:
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                doc_id = str(doc.get("_id") or doc.get("id") or uuid.uuid4().hex)
                doc = {**doc, "_id": doc_id}
                created_at = parse_time_ms(doc, "created_at", "timestamp", "date")
                self._db.execute(
                    "INSERT OR REPLACE INTO treatments"
                    " (id, user, created_at, event_type, carbs, insulin, raw)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        user,
                        created_at,
                        doc.get("eventType"),
                        doc.get("carbs"),
                        doc.get("insulin"),
                        json.dumps(doc),
                    ),
                )
                stored.append(doc)
            self._db.commit()
        return stored

    def delete_treatment(self, user: str, doc_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM treatments WHERE user = ? AND id = ?", (user, doc_id)
            )
            self._db.commit()
            return cur.rowcount > 0

    def add_devicestatus(self, user: str, docs: list[dict]) -> list[dict]:
        stored = []
        with self._lock:
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                created_at = parse_time_ms(doc, "created_at", "date")
                iob, cob = extract_iob_cob(doc)
                self._db.execute(
                    "INSERT OR REPLACE INTO devicestatus"
                    " (user, created_at, iob, cob, raw) VALUES (?, ?, ?, ?, ?)",
                    (user, created_at, iob, cob, json.dumps(doc)),
                )
                stored.append(doc)
            self._db.commit()
        return stored

    # ---- reads (HTTP GET endpoints) ----

    def get_entries(self, user: str, count: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT raw FROM entries WHERE user = ? ORDER BY date DESC LIMIT ?",
                (user, count),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_treatments(self, user: str, count: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT raw FROM treatments WHERE user = ? ORDER BY created_at DESC LIMIT ?",
                (user, count),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_devicestatus(self, user: str, count: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT raw FROM devicestatus WHERE user = ? ORDER BY created_at DESC LIMIT ?",
                (user, count),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---- therapy parameters (ISF/CR/DIA from profile sources) ----

    def set_params(self, user: str, params: dict) -> None:
        merged = {**self.get_params(user), **{k: v for k, v in params.items() if v}}
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO params (user, raw) VALUES (?, ?)",
                (user, json.dumps(merged)),
            )
            self._db.commit()

    def get_params(self, user: str) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT raw FROM params WHERE user = ?", (user,)
            ).fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except ValueError:
            return {}

    # ---- display snapshot ----

    def snapshot(self, user: str, history_minutes: int = 180) -> UserSnapshot:
        snap = UserSnapshot()
        now_ms = int(time.time() * 1000)
        with self._lock:
            rows = self._db.execute(
                "SELECT date, sgv, direction FROM entries"
                " WHERE user = ? ORDER BY date DESC LIMIT 2",
                (user,),
            ).fetchall()
            if rows:
                snap.sgv_date, snap.sgv, snap.direction = rows[0]
                # Only report a delta for consecutive readings (<15 min apart).
                if len(rows) > 1 and rows[0][0] - rows[1][0] < 15 * 60 * 1000:
                    snap.delta = rows[0][1] - rows[1][1]

            snap.history = [
                (date, sgv)
                for date, sgv in self._db.execute(
                    "SELECT date, sgv FROM entries WHERE user = ? AND date >= ?"
                    " ORDER BY date ASC",
                    (user, now_ms - history_minutes * 60 * 1000),
                ).fetchall()
            ]

            status = self._db.execute(
                "SELECT created_at, iob, cob, raw FROM devicestatus"
                " WHERE user = ? AND (iob IS NOT NULL OR cob IS NOT NULL)"
                " ORDER BY created_at DESC LIMIT 1",
                (user,),
            ).fetchone()
            if status:
                snap.status_date, snap.iob, snap.cob = status[:3]
                try:
                    snap.status_raw = json.loads(status[3])
                except (TypeError, ValueError):
                    pass

            carb = self._db.execute(
                "SELECT created_at, carbs FROM treatments"
                " WHERE user = ? AND carbs > 0 ORDER BY created_at DESC LIMIT 1",
                (user,),
            ).fetchone()
            if carb:
                snap.last_carbs_date, snap.last_carbs = carb

            bolus = self._db.execute(
                "SELECT created_at, insulin FROM treatments"
                " WHERE user = ? AND insulin > 0 ORDER BY created_at DESC LIMIT 1",
                (user,),
            ).fetchone()
            if bolus:
                snap.last_bolus_date, snap.last_bolus = bolus

            snap.boluses = self._db.execute(
                "SELECT created_at, insulin FROM treatments"
                " WHERE user = ? AND insulin > 0 AND created_at >= ?"
                " ORDER BY created_at ASC",
                (user, now_ms - 8 * 60 * 60 * 1000),
            ).fetchall()
        snap.params = self.get_params(user)
        return snap
