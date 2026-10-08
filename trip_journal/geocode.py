"""Turn coordinates into human place names ("Fushimi, Kyoto, Japan").

Uses OpenStreetMap's free Nominatim service, which asks for at most one request
per second and an identifying User-Agent. Results are cached in the database and
coordinates are rounded to ~1 km, so a whole trip usually needs only a few dozen
lookups. Geocoding is optional: everything works without it, just with fewer names.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "trip-journal/0.1 (personal photo journal)"


def _cache_key(lat: float, lon: float) -> str:
    return f"{lat:.2f},{lon:.2f}"


def _format(address: dict) -> str | None:
    local = (
        address.get("suburb")
        or address.get("neighbourhood")
        or address.get("quarter")
        or address.get("village")
        or address.get("town")
    )
    city = address.get("city") or address.get("town") or address.get("county") or address.get("state")
    country = address.get("country")
    parts = []
    for p in (local, city, country):
        if p and p not in parts:
            parts.append(p)
    return ", ".join(parts) or None


class Geocoder:
    def __init__(self, conn: sqlite3.Connection, language: str = "en"):
        self.conn = conn
        self.language = language
        self._last_request = 0.0

    def lookup(self, lat: float, lon: float) -> str | None:
        key = _cache_key(lat, lon)
        row = self.conn.execute("SELECT name FROM place_cache WHERE key = ?", (key,)).fetchone()
        if row:
            return row["name"]

        wait = 1.1 - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

        params = urllib.parse.urlencode(
            {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "format": "jsonv2", "zoom": 14,
             "accept-language": self.language}
        )
        req = urllib.request.Request(f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.load(resp)
        except (OSError, ValueError):
            return None  # offline or rate limited: try again on the next import
        name = _format(payload.get("address", {}))
        self.conn.execute("INSERT OR REPLACE INTO place_cache (key, name) VALUES (?, ?)", (key, name))
        self.conn.commit()
        return name
