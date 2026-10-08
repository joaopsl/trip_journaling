"""Read the bits of photo metadata we care about: when, where, and with what."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

try:  # iPhone photos are usually HEIC; this teaches Pillow to open them.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - optional at runtime
    pass

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".webp"}

# EXIF tag ids (see the EXIF 2.32 spec).
_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_MAKE, _MODEL, _DATETIME = 271, 272, 306
_DATETIME_ORIGINAL, _OFFSET_TIME_ORIGINAL = 36867, 36881
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON, _GPS_ALT_REF, _GPS_ALT = 1, 2, 3, 4, 5, 6


@dataclass
class PhotoMeta:
    taken_at: datetime | None  # local wall-clock time where the photo was taken
    utc_offset: str | None  # e.g. "+09:00" when the camera recorded it
    lat: float | None
    lon: float | None
    altitude: float | None
    camera: str | None
    width: int
    height: int


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _to_degrees(dms, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    value = d + m / 60 + s / 3600
    if str(ref).strip().upper() in ("S", "W"):
        value = -value
    return value


def read_metadata(path: Path) -> PhotoMeta:
    with Image.open(path) as img:
        exif = img.getexif()
        width, height = img.size
        orientation = exif.get(0x0112)
        if orientation in (5, 6, 7, 8):  # rotated 90°, so the displayed size is swapped
            width, height = height, width

    sub = exif.get_ifd(_EXIF_IFD)
    gps = exif.get_ifd(_GPS_IFD)

    taken_at = _parse_datetime(sub.get(_DATETIME_ORIGINAL)) or _parse_datetime(exif.get(_DATETIME))
    offset = sub.get(_OFFSET_TIME_ORIGINAL)

    lat = lon = alt = None
    if _GPS_LAT in gps and _GPS_LON in gps:
        lat = _to_degrees(gps[_GPS_LAT], gps.get(_GPS_LAT_REF, "N"))
        lon = _to_degrees(gps[_GPS_LON], gps.get(_GPS_LON_REF, "E"))
        # Some phones write 0,0 when they had no fix.
        if lat is not None and lon is not None and abs(lat) < 1e-6 and abs(lon) < 1e-6:
            lat = lon = None
    if _GPS_ALT in gps:
        try:
            alt = float(gps[_GPS_ALT])
            if gps.get(_GPS_ALT_REF) in (1, b"\x01"):
                alt = -alt
        except (TypeError, ValueError, ZeroDivisionError):
            alt = None

    make = str(exif.get(_MAKE, "")).strip().rstrip("\x00")
    model = str(exif.get(_MODEL, "")).strip().rstrip("\x00")
    camera = model if make and model.startswith(make) else " ".join(p for p in (make, model) if p)

    return PhotoMeta(
        taken_at=taken_at,
        utc_offset=str(offset).strip() if offset else None,
        lat=lat,
        lon=lon,
        altitude=alt,
        camera=camera or None,
        width=width,
        height=height,
    )


def make_thumbnail(src: Path, dest: Path, max_side: int = 1280) -> None:
    """Write an upright JPEG thumbnail (used by the UI and sent to Claude)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_side, max_side))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(dest, "JPEG", quality=82)
