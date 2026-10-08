from datetime import datetime

from trip_journal import activities as acts
from trip_journal.ai import allocate, pick_photos
from trip_journal.db import connect
from trip_journal.importer import import_folder

from .conftest import make_photo


def _titles(conn, day):
    return [(a["start"][11:16], a["photos"]) for a in acts.list_for_day(conn, 1, day)]


def test_segments_by_gap_and_distance(data, tmp_path):
    f = tmp_path / "p"
    # Temple: three photos close together
    make_photo(f / "1.jpg", datetime(2019, 4, 3, 9, 0), 34.9671, 135.7727)
    make_photo(f / "2.jpg", datetime(2019, 4, 3, 9, 20), 34.9680, 135.7730)
    make_photo(f / "3.jpg", datetime(2019, 4, 3, 9, 50), 34.9690, 135.7735)
    # 15 min later but 4 km away: a new place
    make_photo(f / "4.jpg", datetime(2019, 4, 3, 10, 5), 35.0037, 135.7788)
    # From a moving train: 2 min apart, far apart -> stays together
    make_photo(f / "5.jpg", datetime(2019, 4, 3, 10, 7), 35.0300, 135.7900)
    # Long gap: lunch
    make_photo(f / "6.jpg", datetime(2019, 4, 3, 12, 30), 35.0310, 135.7905)
    conn = connect()
    import_folder(conn, "Kyoto", f, geocode=False)
    assert _titles(conn, "2019-04-03") == [("09:00", 3), ("10:05", 2), ("12:30", 1)]


def test_reimport_joins_existing_activities(data, tmp_path):
    f = tmp_path / "p"
    make_photo(f / "1.jpg", datetime(2019, 4, 3, 9, 0), 34.9671, 135.7727)
    make_photo(f / "2.jpg", datetime(2019, 4, 3, 14, 0), 35.0037, 135.7788)
    conn = connect()
    import_folder(conn, "Kyoto", f, geocode=False)
    first = acts.list_for_day(conn, 1, "2019-04-03")[0]["id"]
    conn.execute("UPDATE activities SET title = 'Inari' WHERE id = ?", (first,))
    # A camera photo from the same morning, and one from the evening
    make_photo(f / "cam1.jpg", datetime(2019, 4, 3, 9, 10))
    make_photo(f / "cam2.jpg", datetime(2019, 4, 3, 19, 0))
    import_folder(conn, "Kyoto", f, geocode=False)
    day = acts.list_for_day(conn, 1, "2019-04-03")
    assert [(a["title"], a["photos"]) for a in day] == [("Inari", 2), ("", 1), ("", 1)]


def test_merge_and_split(data, kyoto_folder):
    conn = connect()
    import_folder(conn, "Japan", kyoto_folder, geocode=False)
    a, b = acts.list_for_day(conn, 1, "2019-04-02")
    conn.execute("UPDATE activities SET notes = 'stairs' WHERE id = ?", (a["id"],))
    conn.execute("UPDATE activities SET notes = 'tea', title = 'Gion' WHERE id = ?", (b["id"],))
    assert acts.merge_into_previous(conn, a["id"]) is None  # first of the day
    survivor = acts.merge_into_previous(conn, b["id"])
    (merged,) = acts.list_for_day(conn, 1, "2019-04-02")
    assert merged["id"] == survivor and merged["photos"] == 3
    assert merged["notes"] == "stairs\n\ntea" and merged["title"] == "Gion"

    c = conn.execute("SELECT id FROM photos WHERE path LIKE '%c.jpg'").fetchone()["id"]
    assert acts.split_at(conn, c) is not None
    assert [x["photos"] for x in acts.list_for_day(conn, 1, "2019-04-02")] == [2, 1]
    first = conn.execute("SELECT id FROM photos WHERE path LIKE '%a.jpg'").fetchone()["id"]
    assert acts.split_at(conn, first) is None


def test_allocate_shares_budget():
    alloc = allocate([100, 10, 2], 40)
    assert sum(alloc) == 40 and all(x >= 1 for x in alloc) and alloc[0] > alloc[1] > 0
    assert allocate([3, 2], 40) == [3, 2]
    assert sum(allocate([5, 5, 5], 2)) == 2


def test_pick_photos_prefers_starred():
    photos = [{"id": i, "taken_at": f"2019-04-02T10:{i:02d}:00", "starred": int(i in (7, 8))} for i in range(20)]
    picked = pick_photos(photos, 5)
    ids = [p["id"] for p in picked]
    assert 7 in ids and 8 in ids and len(ids) == 5 and ids == sorted(ids)
    assert 0 in ids and 19 in ids  # the rest still spans the activity
