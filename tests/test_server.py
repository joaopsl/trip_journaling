import json
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trip_journal import ai
from trip_journal.db import connect
from trip_journal.importer import import_folder
from trip_journal.server import create_app

from .conftest import make_photo


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        yield from self.chunks

    def get_final_message(self):
        return SimpleNamespace(stop_reason="end_turn")


class FakeClient:
    """Stands in for anthropic.Anthropic and records what would have been sent."""

    def __init__(self, reply="", json_reply=None):
        self.calls = []
        self.reply = reply
        self.json_reply = json_reply
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream, create=self._create))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream([self.reply[:5], self.reply[5:]])

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.json_reply))]
        )


def images(message):
    return [b for b in message["content"] if b["type"] == "image"]


def texts(message):
    return "\n".join(b["text"] for b in message["content"] if b["type"] == "text")


@pytest.fixture
def client(data, kyoto_folder):
    conn = connect()
    import_folder(conn, "Japan 2019", kyoto_folder, geocode=False)
    return TestClient(create_app())


def test_trip_day_and_story_endpoints(client):
    trips = client.get("/api/trips").json()
    assert trips[0]["name"] == "Japan 2019" and trips[0]["photos"] == 5
    trip = client.get("/api/trips/1").json()
    assert [(d["day"], d["activities"]) for d in trip["days"]] == [("2019-04-02", 2), ("2019-04-03", 1)]
    assert trip["undated"] == 1
    photos = client.get("/api/trips/1/photos").json()
    assert len(photos) == 4 and all(p["activity_id"] for p in photos)
    assert client.get(photos[0]["thumb"]).status_code == 200

    url = "/api/trips/1/days/2019-04-02"
    assert client.put(url, json={"title": "Gates", "journal": "Woke up early."}).status_code == 200
    day = client.get(url).json()
    assert day["title"] == "Gates" and day["journal"] == "Woke up early."
    assert [len(a["photo_ids"]) for a in day["activities"]] == [2, 1]
    assert client.get("/api/trips/1/days/2020-01-01").status_code == 404

    act = day["activities"][0]["id"]
    client.put(f"/api/activities/{act}", json={"title": "Fushimi Inari", "notes": "So many stairs"})
    client.put(f"/api/photos/{photos[0]['id']}/star", json={"starred": True})
    story = client.get("/api/trips/1/story").json()
    assert story[0]["title"] == "Gates"
    assert story[0]["activities"][0]["title"] == "Fushimi Inari"
    assert story[0]["activities"][0]["notes"] == "So many stairs"
    assert client.get("/api/trips/1/photos").json()[0]["starred"] == 1
    assert client.get("/").status_code == 200


def test_merge_and_split_endpoints(client):
    day = client.get("/api/trips/1/days/2019-04-02").json()
    first, second = day["activities"]
    assert client.post(f"/api/activities/{first['id']}/merge-previous").status_code == 400
    merged = client.post(f"/api/activities/{second['id']}/merge-previous").json()["activities"]
    assert len(merged) == 1 and len(merged[0]["photo_ids"]) == 3
    split = client.post(f"/api/photos/{merged[0]['photo_ids'][1]}/split").json()["activities"]
    assert [len(a["photo_ids"]) for a in split] == [1, 2]


def test_chat_sends_activities_and_saves_history(client, monkeypatch):
    fake = FakeClient("Those orange gates - was it crowded?")
    monkeypatch.setattr(ai, "_client", lambda: fake)
    url = "/api/trips/1/days/2019-04-02"
    client.put(url, json={"title": "", "journal": "We went to a shrine."})
    act = client.get(url).json()["activities"][0]["id"]
    client.put(f"/api/activities/{act}", json={"title": "Inari", "notes": "Hundreds of steps"})

    res = client.post(url + "/chat", json={"message": "I remember a lot of stairs"})
    assert res.text == "Those orange gates - was it crowded?"

    sent = fake.calls[0]
    assert sent["model"] == ai.MODEL and sent["fallbacks"] == "default"
    first = sent["messages"][0]
    text = texts(first)
    assert "Day 1 of 2" in text and "We went to a shrine." in text
    assert 'Activity 1 "Inari" (09:15-09:40) - 2 photos' in text and "Hundreds of steps" in text
    assert "09:40" in text and "estimated" in text
    assert len(images(first)) == 3
    assert first["content"][-1]["text"] == "I remember a lot of stairs"

    chat = client.get(url).json()["chat"]
    assert [m["role"] for m in chat] == ["user", "assistant"]

    client.post(url + "/chat", json={"message": "Yes, very"})
    assert [m["role"] for m in fake.calls[1]["messages"]] == ["user", "assistant", "user"]

    # Drafts are streamed but not stored in the conversation.
    client.post(url + "/draft")
    assert fake.calls[2]["messages"][-1]["content"][-1]["text"] == ai.DRAFT_INSTRUCTION
    assert len(client.get(url).json()["chat"]) == 4

    client.delete(url + "/chat")
    assert client.get(url).json()["chat"] == []


def test_focused_chat_sees_more_of_that_activity(data, tmp_path, monkeypatch):
    folder = tmp_path / "museum"
    for i in range(60):  # a long museum visit
        make_photo(folder / f"m{i:02d}.jpg", datetime(2019, 4, 5, 10, 0, i * 30 % 60).replace(minute=i // 2),
                   35.7188, 139.7765)
    for i in range(10):  # dinner later
        make_photo(folder / f"d{i}.jpg", datetime(2019, 4, 5, 19, i), 35.7100, 139.8000)
    conn = connect()
    import_folder(conn, "Tokyo", folder, geocode=False)
    client = TestClient(create_app())
    monkeypatch.setattr(ai, "MAX_PHOTOS_PER_DAY", 20)
    monkeypatch.setattr(ai, "MAX_PHOTOS_FOCUSED", 30)
    fake = FakeClient("ok")
    monkeypatch.setattr(ai, "_client", lambda: fake)
    url = "/api/trips/1/days/2019-04-05"
    museum, dinner = client.get(url).json()["activities"]

    client.post(url + "/chat", json={"message": "hi"})
    assert len(images(fake.calls[0]["messages"][0])) == 20

    client.post(url + "/chat", json={"message": "what was in that room?", "activity_id": museum["id"]})
    ctx = fake.calls[1]["messages"][0]
    labels = [b["text"] for b in ctx["content"] if b["type"] == "text" and b["text"].startswith("[Activity")]
    assert sum(l.startswith("[Activity 1") for l in labels) == 30
    assert sum(l.startswith("[Activity 2") for l in labels) >= 1
    assert "[FOCUS]" in texts(ctx)
    assert client.get(url).json()["chat"][-2]["content"].startswith('(About Activity 1 "')


def test_suggest_titles_fills_only_empty_titles(client, monkeypatch):
    fake = FakeClient(json_reply={"activities": [{"number": 1, "title": "Fushimi Inari Shrine"},
                                                 {"number": 2, "title": "Evening in Gion"}]})
    monkeypatch.setattr(ai, "_client", lambda: fake)
    url = "/api/trips/1/days/2019-04-02"
    second = client.get(url).json()["activities"][1]["id"]
    client.put(f"/api/activities/{second}", json={"title": "Dinner", "notes": ""})
    acts = client.post(url + "/suggest-titles").json()["activities"]
    assert [a["title"] for a in acts] == ["Fushimi Inari Shrine", "Dinner"]
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_missing_api_key_is_explained(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_PROFILE", raising=False)
    res = client.post("/api/trips/1/days/2019-04-02/chat", json={"message": "hi"})
    assert res.status_code == 200 and "set ANTHROPIC_API_KEY" in res.text

    monkeypatch.setenv("ANTHROPIC_PROFILE", "missing-profile")
    res = client.post("/api/trips/1/days/2019-04-02/chat", json={"message": "hi"})
    assert "credentials problem" in res.text


def test_import_from_browser(data, tmp_path, monkeypatch):
    root = tmp_path / "library"
    make_photo(root / "Japan" / "Kyoto" / "a.jpg", datetime(2019, 4, 2, 9, 0), 34.9671, 135.7727)
    monkeypatch.setenv("TRIP_JOURNAL_PHOTOS", str(root))
    client = TestClient(create_app())

    assert client.get("/api/folders").json()["folders"] == ["Japan"]
    assert client.get("/api/folders", params={"path": "Japan"}).json()["folders"] == ["Kyoto"]
    assert client.get("/api/folders", params={"path": "../.."}).status_code == 400
    assert client.post("/api/imports", json={"trip": "x", "folder": "../"}).status_code == 400

    client.post("/api/imports", json={"trip": "Japan", "folder": "Japan", "geocode": False})
    for _ in range(50):
        status = client.get("/api/imports/current").json()
        if not status["running"]:
            break
        time.sleep(0.1)
    assert status["error"] is None and status["report"]["imported"] == 1
    assert client.get("/api/trips").json()[0]["photos"] == 1


def test_old_database_is_migrated(data, kyoto_folder):
    # A database from the first version: photos without activity_id / starred.
    data.mkdir(parents=True)
    old = sqlite3.connect(data / "journal.db")
    old.executescript("""
        CREATE TABLE trips (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE photos (id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL, path TEXT NOT NULL,
            thumb TEXT NOT NULL, taken_at TEXT, day TEXT, utc_offset TEXT, lat REAL, lon REAL,
            altitude REAL, location_estimated INTEGER NOT NULL DEFAULT 0, place TEXT, camera TEXT,
            width INTEGER, height INTEGER, UNIQUE (trip_id, path));
        CREATE TABLE days (trip_id INTEGER NOT NULL, day TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            journal TEXT NOT NULL DEFAULT '', updated_at TEXT, PRIMARY KEY (trip_id, day));
        INSERT INTO trips VALUES (1, 'Old', '2026-01-01');
        INSERT INTO photos (trip_id, path, thumb, taken_at, day, lat, lon)
            VALUES (1, '/x.jpg', 'thumbs/1/x.jpg', '2019-04-02T09:00:00', '2019-04-02', 35.0, 135.7);
        INSERT INTO days (trip_id, day, journal) VALUES (1, '2019-04-02', 'kept');
    """)
    old.commit()
    old.close()
    client = TestClient(create_app())
    day = client.get("/api/trips/1/days/2019-04-02").json()
    assert day["journal"] == "kept" and len(day["activities"]) == 1


def test_concurrent_requests(client):
    """The page fires several requests at once; each must get its own connection."""
    from concurrent.futures import ThreadPoolExecutor

    urls = ["/api/trips/1", "/api/trips/1/photos", "/api/trips/1/story", "/api/trips/1/days/2019-04-02"] * 25
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert set(codes) == {200}
