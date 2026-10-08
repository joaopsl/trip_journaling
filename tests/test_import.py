import json
from datetime import datetime

import pytest

from trip_journal.db import connect
from trip_journal.exif import read_metadata
from trip_journal.importer import import_folder

from .conftest import make_photo


def test_read_metadata_gps_and_time(tmp_path):
    p = make_photo(tmp_path / "x.jpg", datetime(2018, 10, 1, 8, 30, 5), -8.4095, 115.1889)
    meta = read_metadata(p)
    assert meta.taken_at == datetime(2018, 10, 1, 8, 30, 5)
    assert meta.lat == pytest.approx(-8.4095, abs=1e-4)
    assert meta.lon == pytest.approx(115.1889, abs=1e-4)
    assert meta.camera == "Apple iPhone 12"


def test_import_groups_days_and_borrows_locations(data, kyoto_folder):
    conn = connect()
    report = import_folder(conn, "Japan 2019", kyoto_folder, geocode=False)
    assert report.imported == 5
    assert report.estimated_location == 1  # b.jpg borrowed a.jpg's location
    assert report.undated == 1
    days = [r["day"] for r in conn.execute("SELECT day FROM days ORDER BY day")]
    assert days == ["2019-04-02", "2019-04-03"]
    b = conn.execute("SELECT * FROM photos WHERE path LIKE '%b.jpg'").fetchone()
    assert b["location_estimated"] == 1 and b["lat"] == pytest.approx(34.9671, abs=1e-4)
    assert (data / b["thumb"]).is_file()

    again = import_folder(conn, "Japan 2019", kyoto_folder, geocode=False)
    assert again.imported == 0 and again.skipped == 5


def test_takeout_sidecar_fills_missing_metadata(data, tmp_path):
    folder = tmp_path / "takeout"
    make_photo(folder / "IMG_1.jpg", None)
    (folder / "IMG_1.jpg.supplemental-metadata.json").write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1554163200"},  # 2019-04-02 00:00 UTC
        "geoData": {"latitude": 35.0116, "longitude": 135.7681},
    }))
    conn = connect()
    import_folder(conn, "Kyoto", folder, geocode=False)
    row = conn.execute("SELECT * FROM photos").fetchone()
    assert row["lat"] == pytest.approx(35.0116)
    assert row["taken_at"] == "2019-04-02T09:00:00"  # longitude 135° ≈ UTC+9
