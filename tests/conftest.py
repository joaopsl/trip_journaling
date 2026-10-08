from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational


def _dms(value: float):
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = (value - d - m / 60) * 3600
    return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(round(s * 1000), 1000))


def make_photo(path: Path, when: datetime | None, lat: float | None = None, lon: float | None = None,
               color=(200, 120, 60)) -> Path:
    img = Image.new("RGB", (64, 48), color)
    exif = Image.Exif()
    exif[271] = "Apple"
    exif[272] = "iPhone 12"
    if when:
        exif.get_ifd(0x8769)[36867] = when.strftime("%Y:%m:%d %H:%M:%S")
    if lat is not None:
        exif.get_ifd(0x8825).update({
            1: "N" if lat >= 0 else "S", 2: _dms(lat),
            3: "E" if lon >= 0 else "W", 4: _dms(lon),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", exif=exif)
    return path


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIP_JOURNAL_DATA", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def kyoto_folder(tmp_path):
    folder = tmp_path / "photos"
    make_photo(folder / "a.jpg", datetime(2019, 4, 2, 9, 15), 34.9671, 135.7727)   # Fushimi Inari
    make_photo(folder / "b.jpg", datetime(2019, 4, 2, 9, 40))                       # camera, no GPS
    make_photo(folder / "c.jpg", datetime(2019, 4, 2, 15, 0), 35.0037, 135.7788)   # Gion
    make_photo(folder / "sub/d.jpg", datetime(2019, 4, 3, 10, 0), 35.0394, 135.7292)  # Kinkaku-ji
    make_photo(folder / "e.jpg", None)                                              # no metadata
    return folder
