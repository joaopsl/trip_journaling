"""SQLite storage. One file, no server: everything lives under the data directory."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id                  INTEGER PRIMARY KEY,
    trip_id             INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    path                TEXT NOT NULL,
    thumb               TEXT NOT NULL,
    taken_at            TEXT,           -- local wall-clock time, ISO 8601
    day                 TEXT,           -- YYYY-MM-DD derived from taken_at
    utc_offset          TEXT,
    lat                 REAL,
    lon                 REAL,
    altitude            REAL,
    location_estimated  INTEGER NOT NULL DEFAULT 0,  -- 1 = borrowed from a nearby photo
    place               TEXT,
    camera              TEXT,
    width               INTEGER,
    height              INTEGER,
    UNIQUE (trip_id, path)
);
CREATE INDEX IF NOT EXISTS photos_by_day ON photos (trip_id, day, taken_at);

CREATE TABLE IF NOT EXISTS days (
    trip_id     INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    journal     TEXT NOT NULL DEFAULT '',
    updated_at  TEXT,
    PRIMARY KEY (trip_id, day)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY,
    trip_id     INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_by_day ON chat_messages (trip_id, day, id);

CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY,
    trip_id     INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS activities_by_day ON activities (trip_id, day);

CREATE TABLE IF NOT EXISTS place_cache (
    key   TEXT PRIMARY KEY,
    name  TEXT
);
"""


def data_dir() -> Path:
    return Path(os.environ.get("TRIP_JOURNAL_DATA", "data")).resolve()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return data_dir() / "journal.db"


def connect(path: Path | None = None, *, init: bool = True) -> sqlite3.Connection:
    """Open the database. `init` creates/migrates the schema (needed once per process).

    A connection must not be used by two threads at the same time, so the web
    server opens one per request rather than sharing one.
    """
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        conn.execute("PRAGMA journal_mode = WAL")  # the app can read while an import writes
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to existing databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    if "activity_id" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL")
    if "starred" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS photos_by_activity ON photos (activity_id)")
    conn.commit()


def get_or_create_trip(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM trips WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO trips (name, created_at) VALUES (?, ?)", (name, now()))
    conn.commit()
    return cur.lastrowid
