"""Split a day's photos into activities: "Fushimi Inari", "lunch", "Gion in the evening".

A new activity starts when there's a long pause between photos, or when you've
moved a fair distance since the last photo. Automatic splits are a starting point;
the user can rename, merge and split them in the app. Re-imports never redo the
split for a day that already has activities: new photos join the closest one.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

from .db import now

NEW_ACTIVITY_GAP = timedelta(minutes=45)
NEW_ACTIVITY_DISTANCE_KM = 0.8
MIN_GAP_FOR_DISTANCE_SPLIT = timedelta(minutes=5)  # photos from a moving train stay together
JOIN_EXISTING_WINDOW = timedelta(minutes=30)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def segment(photos: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Group time-ordered photos into activities."""
    groups: list[list[sqlite3.Row]] = []
    last_time: datetime | None = None
    last_loc: tuple[float, float] | None = None
    for p in photos:
        t = datetime.fromisoformat(p["taken_at"])
        loc = (p["lat"], p["lon"]) if p["lat"] is not None else None
        start_new = not groups
        if groups and last_time is not None:
            gap = t - last_time
            if gap >= NEW_ACTIVITY_GAP:
                start_new = True
            elif (
                loc and last_loc and gap >= MIN_GAP_FOR_DISTANCE_SPLIT
                and distance_km(*loc, *last_loc) >= NEW_ACTIVITY_DISTANCE_KM
            ):
                start_new = True
        if start_new:
            groups.append([])
        groups[-1].append(p)
        last_time = t
        if loc:
            last_loc = loc
    return groups


def _create(conn: sqlite3.Connection, trip_id: int, day: str, photo_ids: list[int]) -> int:
    cur = conn.execute(
        "INSERT INTO activities (trip_id, day, updated_at) VALUES (?, ?, ?)", (trip_id, day, now())
    )
    conn.executemany(
        "UPDATE photos SET activity_id = ? WHERE id = ?", [(cur.lastrowid, pid) for pid in photo_ids]
    )
    return cur.lastrowid


def assign_activities(conn: sqlite3.Connection, trip_id: int) -> None:
    """Give every dated photo of the trip an activity."""
    days = [
        r["day"]
        for r in conn.execute(
            "SELECT DISTINCT day FROM photos WHERE trip_id = ? AND day IS NOT NULL AND activity_id IS NULL",
            (trip_id,),
        )
    ]
    for day in days:
        loose = conn.execute(
            """SELECT * FROM photos WHERE trip_id = ? AND day = ? AND activity_id IS NULL
               ORDER BY taken_at""",
            (trip_id, day),
        ).fetchall()
        spans = activity_spans(conn, trip_id, day)
        if not spans:
            for group in segment(loose):
                _create(conn, trip_id, day, [p["id"] for p in group])
            continue
        leftovers = []
        for p in loose:
            t = datetime.fromisoformat(p["taken_at"])
            best = min(spans, key=lambda s: _distance_to_span(t, s))
            if _distance_to_span(t, best) <= JOIN_EXISTING_WINDOW:
                conn.execute("UPDATE photos SET activity_id = ? WHERE id = ?", (best[0], p["id"]))
            else:
                leftovers.append(p)
        for group in segment(leftovers):
            _create(conn, trip_id, day, [p["id"] for p in group])
    conn.commit()


def activity_spans(conn: sqlite3.Connection, trip_id: int, day: str) -> list[tuple[int, datetime, datetime]]:
    return [
        (r["activity_id"], datetime.fromisoformat(r["s"]), datetime.fromisoformat(r["e"]))
        for r in conn.execute(
            """SELECT activity_id, MIN(taken_at) s, MAX(taken_at) e FROM photos
               WHERE trip_id = ? AND day = ? AND activity_id IS NOT NULL GROUP BY activity_id""",
            (trip_id, day),
        )
    ]


def _distance_to_span(t: datetime, span: tuple[int, datetime, datetime]) -> timedelta:
    _, start, end = span
    if start <= t <= end:
        return timedelta(0)
    return min(abs(t - start), abs(t - end))


def list_for_day(conn: sqlite3.Connection, trip_id: int, day: str) -> list[dict]:
    """Activities in time order, with their time span, main place and photo count."""
    rows = conn.execute(
        """SELECT a.id, a.title, a.notes, MIN(p.taken_at) AS start, MAX(p.taken_at) AS end,
                  COUNT(p.id) AS photos, AVG(p.lat) AS lat, AVG(p.lon) AS lon,
                  (SELECT p2.place FROM photos p2 WHERE p2.activity_id = a.id AND p2.place IS NOT NULL
                    GROUP BY p2.place ORDER BY COUNT(*) DESC LIMIT 1) AS place
           FROM activities a JOIN photos p ON p.activity_id = a.id
           WHERE a.trip_id = ? AND a.day = ?
           GROUP BY a.id ORDER BY start""",
        (trip_id, day),
    ).fetchall()
    return [dict(r) for r in rows]


def merge_into_previous(conn: sqlite3.Connection, activity_id: int) -> int | None:
    """Fold an activity into the one before it; its notes are appended. Returns the survivor."""
    act = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if not act:
        return None
    ordered = list_for_day(conn, act["trip_id"], act["day"])
    idx = next(i for i, a in enumerate(ordered) if a["id"] == activity_id)
    if idx == 0:
        return None
    prev = ordered[idx - 1]
    notes = "\n\n".join(n for n in (prev["notes"].strip(), act["notes"].strip()) if n)
    title = prev["title"] or act["title"]
    conn.execute("UPDATE photos SET activity_id = ? WHERE activity_id = ?", (prev["id"], activity_id))
    conn.execute(
        "UPDATE activities SET notes = ?, title = ?, updated_at = ? WHERE id = ?",
        (notes, title, now(), prev["id"]),
    )
    conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
    conn.commit()
    return prev["id"]


def split_at(conn: sqlite3.Connection, photo_id: int) -> int | None:
    """Start a new activity at this photo: it and every later photo of its activity move over."""
    photo = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not photo or photo["activity_id"] is None:
        return None
    later = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM photos WHERE activity_id = ? AND (taken_at > ? OR (taken_at = ? AND id >= ?))",
            (photo["activity_id"], photo["taken_at"], photo["taken_at"], photo_id),
        )
    ]
    total = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE activity_id = ?", (photo["activity_id"],)
    ).fetchone()[0]
    if len(later) == total:  # already the first photo: nothing to split
        return None
    new_id = _create(conn, photo["trip_id"], photo["day"], later)
    conn.commit()
    return new_id
