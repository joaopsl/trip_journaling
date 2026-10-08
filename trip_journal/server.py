"""Local web app: map + day timeline + journal + memory chat."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai
from .db import connect, data_dir, now

STATIC = Path(__file__).parent / "static"


class DayUpdate(BaseModel):
    title: str = ""
    journal: str = ""


class ChatRequest(BaseModel):
    message: str = ""


def create_app(conn: sqlite3.Connection | None = None) -> FastAPI:
    conn = conn or connect()
    app = FastAPI(title="Trip Journal")
    thumbs = data_dir() / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    app.mount("/thumbs", StaticFiles(directory=thumbs), name="thumbs")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def trip_or_404(trip_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Trip not found")
        return row

    def day_or_404(trip_id: int, day: str) -> sqlite3.Row:
        trip_or_404(trip_id)
        row = conn.execute("SELECT * FROM days WHERE trip_id = ? AND day = ?", (trip_id, day)).fetchone()
        if not row:
            raise HTTPException(404, "No photos on that day")
        return row

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/trips")
    def list_trips():
        rows = conn.execute(
            """SELECT t.id, t.name, COUNT(p.id) AS photos, MIN(p.day) AS start, MAX(p.day) AS end
               FROM trips t LEFT JOIN photos p ON p.trip_id = t.id
               GROUP BY t.id ORDER BY start"""
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/trips/{trip_id}")
    def get_trip(trip_id: int):
        trip = trip_or_404(trip_id)
        days = conn.execute(
            """SELECT d.day, d.title, LENGTH(TRIM(d.journal)) > 0 AS has_journal,
                      COUNT(p.id) AS photos, AVG(p.lat) AS lat, AVG(p.lon) AS lon,
                      (SELECT p2.place FROM photos p2
                        WHERE p2.trip_id = d.trip_id AND p2.day = d.day AND p2.place IS NOT NULL
                        GROUP BY p2.place ORDER BY COUNT(*) DESC LIMIT 1) AS main_place
               FROM days d LEFT JOIN photos p ON p.trip_id = d.trip_id AND p.day = d.day
               WHERE d.trip_id = ? GROUP BY d.day ORDER BY d.day""",
            (trip_id,),
        ).fetchall()
        undated = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE trip_id = ? AND day IS NULL", (trip_id,)
        ).fetchone()[0]
        return {"id": trip["id"], "name": trip["name"], "days": [dict(d) for d in days], "undated": undated}

    @app.get("/api/trips/{trip_id}/photos")
    def trip_photos(trip_id: int):
        trip_or_404(trip_id)
        rows = conn.execute(
            """SELECT id, taken_at, day, lat, lon, place, location_estimated, camera, thumb
               FROM photos WHERE trip_id = ? AND day IS NOT NULL ORDER BY taken_at""",
            (trip_id,),
        ).fetchall()
        return [
            {**dict(r), "thumb": "/thumbs/" + Path(r["thumb"]).relative_to("thumbs").as_posix()}
            for r in rows
        ]

    @app.get("/api/trips/{trip_id}/days/{day}")
    def get_day(trip_id: int, day: str):
        row = day_or_404(trip_id, day)
        return {
            "day": day,
            "title": row["title"],
            "journal": row["journal"],
            "updated_at": row["updated_at"],
            "chat": ai.history(conn, trip_id, day),
        }

    @app.put("/api/trips/{trip_id}/days/{day}")
    def update_day(trip_id: int, day: str, body: DayUpdate):
        day_or_404(trip_id, day)
        conn.execute(
            "UPDATE days SET title = ?, journal = ?, updated_at = ? WHERE trip_id = ? AND day = ?",
            (body.title, body.journal, now(), trip_id, day),
        )
        conn.commit()
        return {"ok": True}

    @app.post("/api/trips/{trip_id}/days/{day}/chat")
    def chat(trip_id: int, day: str, body: ChatRequest):
        day_or_404(trip_id, day)
        return StreamingResponse(
            ai.stream_reply(conn, trip_id, day, body.message), media_type="text/plain; charset=utf-8"
        )

    @app.post("/api/trips/{trip_id}/days/{day}/draft")
    def draft(trip_id: int, day: str):
        day_or_404(trip_id, day)
        return StreamingResponse(
            ai.stream_reply(conn, trip_id, day, None, draft=True), media_type="text/plain; charset=utf-8"
        )

    @app.delete("/api/trips/{trip_id}/days/{day}/chat")
    def clear_chat(trip_id: int, day: str):
        day_or_404(trip_id, day)
        conn.execute("DELETE FROM chat_messages WHERE trip_id = ? AND day = ?", (trip_id, day))
        conn.commit()
        return {"ok": True}

    return app
