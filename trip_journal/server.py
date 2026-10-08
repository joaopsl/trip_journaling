"""Local web app: Write view (photos, activities, journal, chat) and Read view (the story)."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import activities as acts
from . import ai
from .db import connect, data_dir, db_path, now
from .importer import import_folder

STATIC = Path(__file__).parent / "static"


def photos_root() -> Path:
    """The folder the in-app importer may browse (in Docker: where photos are mounted)."""
    return Path(os.environ.get("TRIP_JOURNAL_PHOTOS", Path.home())).expanduser().resolve()


def get_db(request: Request):
    """One connection per request: SQLite connections can't be used by two threads at once."""
    conn = connect(request.app.state.db_path, init=False)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]


class DayUpdate(BaseModel):
    title: str = ""
    journal: str = ""


class ActivityUpdate(BaseModel):
    title: str = ""
    notes: str = ""


class StarUpdate(BaseModel):
    starred: bool


class ChatRequest(BaseModel):
    message: str = ""
    activity_id: int | None = None


class ImportRequest(BaseModel):
    trip: str
    folder: str
    geocode: bool = True


class ImportJob:
    """One background import at a time, with a log the UI polls."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.trip_id: int | None = None
        self.report: dict | None = None
        self.error: str | None = None

    def start(self, req: ImportRequest, folder: Path) -> None:
        with self.lock:
            if self.running:
                raise HTTPException(409, "An import is already running")
            self.running, self.log, self.report, self.error = True, [], None, None
        threading.Thread(target=self._run, args=(req, folder), daemon=True).start()

    def _run(self, req: ImportRequest, folder: Path) -> None:
        conn = connect(self.path, init=False)
        try:
            report = import_folder(conn, req.trip, folder, geocode=req.geocode, progress=self.log.append)
            self.report = asdict(report)
            self.trip_id = conn.execute("SELECT id FROM trips WHERE name = ?", (req.trip,)).fetchone()["id"]
            self.log.append(f"Done: {report.imported} new photos.")
        except Exception as exc:  # surfaced in the UI rather than lost in a thread
            self.error = str(exc)
        finally:
            conn.close()
            self.running = False

    def status(self) -> dict:
        return {"running": self.running, "log": self.log[-12:], "report": self.report,
                "error": self.error, "trip_id": self.trip_id}


def create_app(path: Path | None = None) -> FastAPI:
    path = path or db_path()
    with closing(connect(path)) as conn:
        for row in conn.execute("SELECT id FROM trips").fetchall():  # databases from before activities
            acts.assign_activities(conn, row["id"])

    def stream(**kwargs):
        """Streaming replies outlive the request's connection, so they get their own."""
        conn = connect(path, init=False)
        try:
            yield from ai.stream_reply(conn, **kwargs)
        finally:
            conn.close()

    app = FastAPI(title="Trip Journal")
    app.state.db_path = path
    thumbs = data_dir() / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    app.mount("/thumbs", StaticFiles(directory=thumbs), name="thumbs")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    job = ImportJob(path)

    def trip_or_404(conn: sqlite3.Connection, trip_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Trip not found")
        return row

    def day_or_404(conn: sqlite3.Connection, trip_id: int, day: str) -> sqlite3.Row:
        trip_or_404(conn, trip_id)
        row = conn.execute("SELECT * FROM days WHERE trip_id = ? AND day = ?", (trip_id, day)).fetchone()
        if not row:
            raise HTTPException(404, "No photos on that day")
        return row

    def activity_or_404(conn: sqlite3.Connection, activity_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Activity not found")
        return row

    def activities_with_photos(conn: sqlite3.Connection, trip_id: int, day: str) -> list[dict]:
        out = []
        for a in acts.list_for_day(conn, trip_id, day):
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM photos WHERE activity_id = ? ORDER BY taken_at, id", (a["id"],))]
            out.append({**a, "photo_ids": ids})
        return out

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---------- trips ----------
    @app.get("/api/trips")
    def list_trips(conn: Db):
        rows = conn.execute(
            """SELECT t.id, t.name, COUNT(p.id) AS photos, MIN(p.day) AS start, MAX(p.day) AS end
               FROM trips t LEFT JOIN photos p ON p.trip_id = t.id
               GROUP BY t.id ORDER BY start"""
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/trips/{trip_id}")
    def get_trip(trip_id: int, conn: Db):
        trip = trip_or_404(conn, trip_id)
        days = conn.execute(
            """SELECT d.day, d.title, LENGTH(TRIM(d.journal)) > 0 AS has_journal,
                      COUNT(p.id) AS photos, COUNT(DISTINCT p.activity_id) AS activities,
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
    def trip_photos(trip_id: int, conn: Db):
        trip_or_404(conn, trip_id)
        rows = conn.execute(
            """SELECT id, taken_at, day, lat, lon, place, location_estimated, camera, thumb,
                      activity_id, starred, width, height
               FROM photos WHERE trip_id = ? AND day IS NOT NULL ORDER BY taken_at, id""",
            (trip_id,),
        ).fetchall()
        return [
            {**dict(r), "thumb": "/thumbs/" + Path(r["thumb"]).relative_to("thumbs").as_posix()}
            for r in rows
        ]

    @app.get("/api/trips/{trip_id}/story")
    def story(trip_id: int, conn: Db):
        """Everything the Read view needs: each day with its text and activities."""
        trip_or_404(conn, trip_id)
        days = conn.execute(
            "SELECT day, title, journal FROM days WHERE trip_id = ? ORDER BY day", (trip_id,)
        ).fetchall()
        return [{**dict(d), "activities": activities_with_photos(conn, trip_id, d["day"])} for d in days]

    # ---------- days ----------
    @app.get("/api/trips/{trip_id}/days/{day}")
    def get_day(trip_id: int, day: str, conn: Db):
        row = day_or_404(conn, trip_id, day)
        return {
            "day": day,
            "title": row["title"],
            "journal": row["journal"],
            "updated_at": row["updated_at"],
            "activities": activities_with_photos(conn, trip_id, day),
            "chat": ai.history(conn, trip_id, day),
        }

    @app.put("/api/trips/{trip_id}/days/{day}")
    def update_day(trip_id: int, day: str, body: DayUpdate, conn: Db):
        day_or_404(conn, trip_id, day)
        conn.execute(
            "UPDATE days SET title = ?, journal = ?, updated_at = ? WHERE trip_id = ? AND day = ?",
            (body.title, body.journal, now(), trip_id, day),
        )
        conn.commit()
        return {"ok": True}

    @app.post("/api/trips/{trip_id}/days/{day}/suggest-titles")
    def suggest_titles(trip_id: int, day: str, conn: Db):
        """Name the day's untitled activities from their photos. Existing titles are kept."""
        day_or_404(conn, trip_id, day)
        try:
            titles = ai.suggest_titles(conn, trip_id, day)
        except Exception as exc:
            message = ai.describe_error(exc)
            if message is None:
                raise
            raise HTTPException(502, message) from exc
        for activity_id, title in titles.items():
            conn.execute(
                "UPDATE activities SET title = ?, updated_at = ? WHERE id = ? AND title = ''",
                (title, now(), activity_id),
            )
        conn.commit()
        return {"activities": activities_with_photos(conn, trip_id, day)}

    @app.post("/api/trips/{trip_id}/days/{day}/chat")
    def chat(trip_id: int, day: str, body: ChatRequest, conn: Db):
        day_or_404(conn, trip_id, day)
        return StreamingResponse(
            stream(trip_id=trip_id, day=day, user_message=body.message, focus_activity=body.activity_id),
            media_type="text/plain; charset=utf-8",
        )

    @app.post("/api/trips/{trip_id}/days/{day}/draft")
    def draft(trip_id: int, day: str, conn: Db):
        day_or_404(conn, trip_id, day)
        return StreamingResponse(
            stream(trip_id=trip_id, day=day, user_message=None, draft=True),
            media_type="text/plain; charset=utf-8",
        )

    @app.delete("/api/trips/{trip_id}/days/{day}/chat")
    def clear_chat(trip_id: int, day: str, conn: Db):
        day_or_404(conn, trip_id, day)
        conn.execute("DELETE FROM chat_messages WHERE trip_id = ? AND day = ?", (trip_id, day))
        conn.commit()
        return {"ok": True}

    # ---------- activities & photos ----------
    @app.put("/api/activities/{activity_id}")
    def update_activity(activity_id: int, body: ActivityUpdate, conn: Db):
        activity_or_404(conn, activity_id)
        conn.execute(
            "UPDATE activities SET title = ?, notes = ?, updated_at = ? WHERE id = ?",
            (body.title, body.notes, now(), activity_id),
        )
        conn.commit()
        return {"ok": True}

    @app.post("/api/activities/{activity_id}/merge-previous")
    def merge_previous(activity_id: int, conn: Db):
        act = activity_or_404(conn, activity_id)
        if acts.merge_into_previous(conn, activity_id) is None:
            raise HTTPException(400, "This is the first activity of the day")
        return {"activities": activities_with_photos(conn, act["trip_id"], act["day"])}

    @app.post("/api/photos/{photo_id}/split")
    def split(photo_id: int, conn: Db):
        photo = conn.execute("SELECT trip_id, day FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not photo:
            raise HTTPException(404, "Photo not found")
        if acts.split_at(conn, photo_id) is None:
            raise HTTPException(400, "That photo already starts its activity")
        return {"activities": activities_with_photos(conn, photo["trip_id"], photo["day"])}

    @app.put("/api/photos/{photo_id}/star")
    def star(photo_id: int, body: StarUpdate, conn: Db):
        cur = conn.execute("UPDATE photos SET starred = ? WHERE id = ?", (int(body.starred), photo_id))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "Photo not found")
        return {"ok": True}

    # ---------- importing ----------
    @app.get("/api/folders")
    def folders(path: str = ""):
        """Browse sub-folders of the photos root so the user can pick one to import."""
        root = photos_root()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_dir():
            raise HTTPException(400, "Not a folder inside the photos directory")
        subdirs = sorted(
            (d for d in target.iterdir() if d.is_dir() and not d.name.startswith(".")),
            key=lambda d: d.name.lower(),
        )
        return {
            "root": str(root),
            "path": target.relative_to(root).as_posix() if target != root else "",
            "folders": [d.name for d in subdirs],
        }

    @app.post("/api/imports")
    def start_import(body: ImportRequest):
        if not body.trip.strip():
            raise HTTPException(400, "Give the trip a name")
        root = photos_root()
        folder = (root / body.folder).resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise HTTPException(400, "Not a folder inside the photos directory")
        job.start(ImportRequest(trip=body.trip.strip(), folder=body.folder, geocode=body.geocode), folder)
        return job.status()

    @app.get("/api/imports/current")
    def import_status():
        return job.status()

    return app
