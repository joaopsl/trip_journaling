"""Import a folder of photos into a trip: read metadata, make thumbnails, fill gaps."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .db import data_dir, get_or_create_trip
from .exif import IMAGE_SUFFIXES, PhotoMeta, make_thumbnail, read_metadata
from .geocode import Geocoder

# A photo with no GPS borrows the location of the closest photo taken within this window.
LOCATION_BORROW_WINDOW = timedelta(hours=2)


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    undated: int = 0
    no_location: int = 0
    estimated_location: int = 0


def _sidecar(path: Path) -> dict | None:
    """Google Photos Takeout puts metadata in JSON next to the image when EXIF was stripped."""
    for candidate in (
        path.with_name(path.name + ".json"),
        path.with_name(path.name + ".supplemental-metadata.json"),
        path.with_suffix(".json"),
    ):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def _apply_sidecar(meta: PhotoMeta, sidecar: dict) -> None:
    geo = sidecar.get("geoDataExif") or sidecar.get("geoData") or {}
    if meta.lat is None and geo.get("latitude") and geo.get("longitude"):
        meta.lat, meta.lon = float(geo["latitude"]), float(geo["longitude"])
    ts = (sidecar.get("photoTakenTime") or {}).get("timestamp")
    if meta.taken_at is None and ts:
        utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        # Takeout only gives UTC. Without a timezone database, approximate local
        # time from longitude (15° per hour) so the photo lands on the right day.
        hours = round(meta.lon / 15) if meta.lon is not None else 0
        meta.taken_at = (utc + timedelta(hours=hours)).replace(tzinfo=None)


def _thumb_name(path: Path) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:16] + ".jpg"


def import_folder(
    conn: sqlite3.Connection,
    trip_name: str,
    folder: Path,
    *,
    geocode: bool = True,
    progress: Callable[[str], None] = lambda _msg: None,
) -> ImportReport:
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise NotADirectoryError(folder)

    trip_id = get_or_create_trip(conn, trip_name)
    thumbs_dir = data_dir() / "thumbs" / str(trip_id)
    report = ImportReport()

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    progress(f"Found {len(files)} images in {folder}")

    for i, path in enumerate(files, 1):
        if conn.execute(
            "SELECT 1 FROM photos WHERE trip_id = ? AND path = ?", (trip_id, str(path))
        ).fetchone():
            report.skipped += 1
            continue
        try:
            meta = read_metadata(path)
            if meta.taken_at is None or meta.lat is None:
                sidecar = _sidecar(path)
                if sidecar:
                    _apply_sidecar(meta, sidecar)
            thumb = thumbs_dir / _thumb_name(path)
            make_thumbnail(path, thumb)
        except Exception as exc:  # corrupt file, unsupported codec, ...
            progress(f"  ! {path.name}: {exc}")
            report.failed += 1
            continue

        conn.execute(
            """INSERT INTO photos (trip_id, path, thumb, taken_at, day, utc_offset, lat, lon,
                                   altitude, camera, width, height)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trip_id,
                str(path),
                str(thumb.relative_to(data_dir())),
                meta.taken_at.isoformat() if meta.taken_at else None,
                meta.taken_at.date().isoformat() if meta.taken_at else None,
                meta.utc_offset,
                meta.lat,
                meta.lon,
                meta.altitude,
                meta.camera,
                meta.width,
                meta.height,
            ),
        )
        report.imported += 1
        if i % 50 == 0:
            conn.commit()
            progress(f"  {i}/{len(files)}")
    conn.commit()

    report.estimated_location = borrow_missing_locations(conn, trip_id)
    _ensure_days(conn, trip_id)
    if geocode:
        name_places(conn, trip_id, progress)

    report.undated = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE trip_id = ? AND taken_at IS NULL", (trip_id,)
    ).fetchone()[0]
    report.no_location = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE trip_id = ? AND lat IS NULL", (trip_id,)
    ).fetchone()[0]
    return report


def borrow_missing_locations(conn: sqlite3.Connection, trip_id: int) -> int:
    """Give GPS-less photos (e.g. from a camera) the location of the nearest photo in time."""
    located = [
        (datetime.fromisoformat(r["taken_at"]), r["lat"], r["lon"])
        for r in conn.execute(
            """SELECT taken_at, lat, lon FROM photos
               WHERE trip_id = ? AND lat IS NOT NULL AND taken_at IS NOT NULL
                     AND location_estimated = 0
               ORDER BY taken_at""",
            (trip_id,),
        )
    ]
    if not located:
        return 0
    count = 0
    for row in conn.execute(
        "SELECT id, taken_at FROM photos WHERE trip_id = ? AND lat IS NULL AND taken_at IS NOT NULL",
        (trip_id,),
    ).fetchall():
        t = datetime.fromisoformat(row["taken_at"])
        best = min(located, key=lambda loc: abs(loc[0] - t))
        if abs(best[0] - t) <= LOCATION_BORROW_WINDOW:
            conn.execute(
                "UPDATE photos SET lat = ?, lon = ?, location_estimated = 1 WHERE id = ?",
                (best[1], best[2], row["id"]),
            )
            count += 1
    conn.commit()
    return count


def _ensure_days(conn: sqlite3.Connection, trip_id: int) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO days (trip_id, day)
           SELECT DISTINCT trip_id, day FROM photos WHERE trip_id = ? AND day IS NOT NULL""",
        (trip_id,),
    )
    conn.commit()


def name_places(conn: sqlite3.Connection, trip_id: int, progress: Callable[[str], None]) -> None:
    rows = conn.execute(
        "SELECT id, lat, lon FROM photos WHERE trip_id = ? AND lat IS NOT NULL AND place IS NULL",
        (trip_id,),
    ).fetchall()
    if not rows:
        return
    progress(f"Naming places for {len(rows)} photos (cached, ~1 lookup/second)...")
    geocoder = Geocoder(conn)
    for row in rows:
        name = geocoder.lookup(row["lat"], row["lon"])
        if name:
            conn.execute("UPDATE photos SET place = ? WHERE id = ?", (name, row["id"]))
    conn.commit()
