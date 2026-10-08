from types import SimpleNamespace

from fastapi.testclient import TestClient

from trip_journal import ai
from trip_journal.db import connect
from trip_journal.importer import import_folder
from trip_journal.server import create_app


class FakeStream:
    def __init__(self, calls, text):
        self.calls, self.text = calls, text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        yield from self.text

    def get_final_message(self):
        return SimpleNamespace(stop_reason="end_turn")


class FakeClient:
    def __init__(self, reply):
        self.calls = []
        self.reply = reply
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.calls, [self.reply[:5], self.reply[5:]])


def setup(data, folder):
    conn = connect()
    import_folder(conn, "Japan 2019", folder, geocode=False)
    return conn, TestClient(create_app(conn))


def test_trip_and_day_endpoints(data, kyoto_folder):
    conn, client = setup(data, kyoto_folder)
    trips = client.get("/api/trips").json()
    assert trips[0]["name"] == "Japan 2019" and trips[0]["photos"] == 5
    trip = client.get(f"/api/trips/{trips[0]['id']}").json()
    assert [d["day"] for d in trip["days"]] == ["2019-04-02", "2019-04-03"]
    assert trip["undated"] == 1
    photos = client.get(f"/api/trips/{trips[0]['id']}/photos").json()
    assert len(photos) == 4
    assert client.get(photos[0]["thumb"]).status_code == 200

    url = f"/api/trips/{trips[0]['id']}/days/2019-04-02"
    assert client.put(url, json={"title": "Gates", "journal": "Woke up early."}).status_code == 200
    day = client.get(url).json()
    assert day["title"] == "Gates" and day["journal"] == "Woke up early."
    assert client.get(f"/api/trips/{trips[0]['id']}/days/2020-01-01").status_code == 404
    assert client.get("/").status_code == 200


def test_chat_sends_context_and_saves_history(data, kyoto_folder, monkeypatch):
    conn, client = setup(data, kyoto_folder)
    fake = FakeClient("Those orange gates - was it crowded?")
    monkeypatch.setattr(ai, "_client", lambda: fake)
    url = "/api/trips/1/days/2019-04-02"
    client.put(url, json={"title": "", "journal": "We went to a shrine."})

    res = client.post(url + "/chat", json={"message": "I remember a lot of stairs"})
    assert res.text == "Those orange gates - was it crowded?"

    sent = fake.calls[0]
    assert sent["model"] == ai.MODEL
    first = sent["messages"][0]
    assert first["role"] == "user"
    text = "\n".join(b["text"] for b in first["content"] if b["type"] == "text")
    assert "Day 1 of 2" in text and "We went to a shrine." in text
    assert "09:40" in text and "estimated" in text
    assert sum(b["type"] == "image" for b in first["content"]) == 3
    assert first["content"][-1]["text"] == "I remember a lot of stairs"

    chat = client.get(url).json()["chat"]
    assert [m["role"] for m in chat] == ["user", "assistant"]

    # A second turn alternates roles correctly.
    client.post(url + "/chat", json={"message": "Yes, very"})
    roles = [m["role"] for m in fake.calls[1]["messages"]]
    assert roles == ["user", "assistant", "user"]

    # Drafts are streamed but not stored in the conversation.
    client.post(url + "/draft")
    assert fake.calls[2]["messages"][-1]["content"][-1]["text"] == ai.DRAFT_INSTRUCTION
    assert len(client.get(url).json()["chat"]) == 4

    client.delete(url + "/chat")
    assert client.get(url).json()["chat"] == []


def test_sample_keeps_bookends():
    s = ai._sample(list(range(100)), 5)
    assert s[0] == 0 and s[-1] == 99 and len(s) == 5
