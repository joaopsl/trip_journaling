"""The memory companion: Claude looks at a day's photos and metadata and helps you remember.

Each request rebuilds the day's context (timeline, places, a sample of photos, your
journal so far, neighbouring days) as the first user turn, followed by the saved chat.
The context sits at the front so prompt caching covers it across a conversation.
"""

from __future__ import annotations

import base64
import io
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterator

import anthropic
from PIL import Image

from .db import data_dir, now

MODEL = os.environ.get("TRIP_JOURNAL_MODEL", "claude-opus-5-5")
MAX_PHOTOS_PER_REQUEST = int(os.environ.get("TRIP_JOURNAL_MAX_PHOTOS", "12"))
AI_IMAGE_SIDE = 768  # plenty for recognising a temple or a bowl of ramen; keeps tokens down

SYSTEM_PROMPT = """\
You are a warm, curious travel companion helping someone reconstruct a journal of a trip \
they took, often years ago. You are given one day at a time: the photos they took that day \
(with timestamps, camera GPS and place names), what they've written so far, and a little \
about the days before and after.

Your job is to help them remember, not to remember for them.
- Ground everything in the evidence. Say what the photos actually show ("at 14:10 you were \
by a river in Fushimi; there's a long tunnel of orange gates") and be explicit when you are \
inferring or guessing ("this looks like it could be Fushimi Inari").
- Ask one or two specific, evocative questions at a time: sensory details, who they were \
with, what they ate, what surprised them, how they got from one place to the next, gaps in \
the timeline ("nothing between 11:00 and 16:00 - what happened there?").
- Use your knowledge of the places to jog memories (local foods, landmarks nearby, what \
that neighbourhood is known for), but never present it as something they did.
- Keep replies short and conversational. The person's own words and memories are what \
matter; the journal is theirs.

When asked to draft a journal entry, write in the first person in the person's voice, \
using only what they told you plus what the photos clearly show. Mark anything uncertain \
with [?] so they can check it. No headings or bullet lists unless they ask - a few \
natural paragraphs."""

DRAFT_INSTRUCTION = (
    "Please draft the journal entry for this day now, based on our conversation, what I've "
    "already written, and the photos. Output only the entry text."
)

KICKOFF = "Here's the day I want to work on. Have a look and help me start remembering it."


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _image_block(thumb_path: Path) -> dict:
    with Image.open(thumb_path) as img:
        img.thumbnail((AI_IMAGE_SIDE, AI_IMAGE_SIDE))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(buf.getvalue()).decode(),
        },
    }


def _sample(items: list, k: int) -> list:
    """Evenly spaced sample that keeps the first and last item (the day's bookends)."""
    if len(items) <= k:
        return items
    if k == 1:
        return [items[0]]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def _photo_line(p: sqlite3.Row) -> str:
    time = p["taken_at"][11:16] if p["taken_at"] else "??:??"
    where = p["place"] or (f"{p['lat']:.4f}, {p['lon']:.4f}" if p["lat"] is not None else "unknown location")
    if p["lat"] is not None and p["location_estimated"]:
        where += " (location estimated from a nearby photo)"
    return f"{time} - {where}"


def _places_summary(conn: sqlite3.Connection, trip_id: int, day: str) -> str:
    rows = conn.execute(
        """SELECT place FROM photos WHERE trip_id = ? AND day = ? AND place IS NOT NULL
           GROUP BY place ORDER BY MIN(taken_at)""",
        (trip_id, day),
    ).fetchall()
    return "; ".join(r["place"] for r in rows) or "no named places"


def build_context(conn: sqlite3.Connection, trip_id: int, day: str) -> list[dict]:
    trip = conn.execute("SELECT name FROM trips WHERE id = ?", (trip_id,)).fetchone()
    all_days = [
        r["day"] for r in conn.execute("SELECT day FROM days WHERE trip_id = ? ORDER BY day", (trip_id,))
    ]
    entry = conn.execute(
        "SELECT title, journal FROM days WHERE trip_id = ? AND day = ?", (trip_id, day)
    ).fetchone()
    photos = conn.execute(
        "SELECT * FROM photos WHERE trip_id = ? AND day = ? ORDER BY taken_at", (trip_id, day)
    ).fetchall()

    idx = all_days.index(day) if day in all_days else -1
    weekday = date.fromisoformat(day).strftime("%A")
    lines = [
        f"Trip: {trip['name']}",
        f"Day {idx + 1} of {len(all_days)}: {weekday} {day}" if idx >= 0 else f"Date: {weekday} {day}",
    ]
    for label, other in (("Previous day", idx - 1), ("Next day", idx + 1)):
        if 0 <= other < len(all_days):
            d = all_days[other]
            row = conn.execute(
                "SELECT journal FROM days WHERE trip_id = ? AND day = ?", (trip_id, d)
            ).fetchone()
            summary = f"{label} ({d}): places - {_places_summary(conn, trip_id, d)}"
            if row and row["journal"].strip():
                summary += f"\n  their journal for it: {row['journal'].strip()[:600]}"
            lines.append(summary)

    lines.append(f"\nTimeline of all {len(photos)} photos this day:")
    lines.extend(f"  {_photo_line(p)}" for p in photos)
    title = entry["title"].strip() if entry else ""
    journal = entry["journal"].strip() if entry else ""
    lines.append(f"\nTheir title for the day: {title or '(none yet)'}")
    lines.append(f"What they've written so far:\n{journal or '(nothing yet)'}")

    blocks: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    sample = _sample(list(photos), MAX_PHOTOS_PER_REQUEST)
    if sample:
        blocks.append({"type": "text", "text": f"A sample of {len(sample)} of the day's photos:"})
    for p in sample:
        thumb = data_dir() / p["thumb"]
        if thumb.is_file():
            blocks.append({"type": "text", "text": f"Photo at {_photo_line(p)}"})
            blocks.append(_image_block(thumb))
    return blocks


def _merge_roles(messages: list[dict]) -> list[dict]:
    """The API wants alternating roles; fold consecutive same-role turns together."""
    merged: list[dict] = []
    for m in messages:
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(content)
        else:
            merged.append({"role": m["role"], "content": list(content)})
    return merged


def history(conn: sqlite3.Connection, trip_id: int, day: str) -> list[dict]:
    return [
        {"role": r["role"], "content": r["content"]}
        for r in conn.execute(
            "SELECT role, content FROM chat_messages WHERE trip_id = ? AND day = ? ORDER BY id",
            (trip_id, day),
        )
    ]


def save_message(conn: sqlite3.Connection, trip_id: int, day: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO chat_messages (trip_id, day, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (trip_id, day, role, content, now()),
    )
    conn.commit()


def build_messages(
    conn: sqlite3.Connection, trip_id: int, day: str, *, draft: bool = False
) -> list[dict]:
    context = build_context(conn, trip_id, day)
    context.append({"type": "text", "text": KICKOFF})
    messages = [{"role": "user", "content": context}, *history(conn, trip_id, day)]
    if draft:
        messages.append({"role": "user", "content": DRAFT_INSTRUCTION})
    return _merge_roles(messages)


def stream_reply(
    conn: sqlite3.Connection,
    trip_id: int,
    day: str,
    user_message: str | None,
    *,
    draft: bool = False,
    client: anthropic.Anthropic | None = None,
) -> Iterator[str]:
    """Stream Claude's reply as text chunks. Chat turns are saved; drafts are not."""
    if user_message and user_message.strip():
        save_message(conn, trip_id, day, "user", user_message.strip())
    messages = build_messages(conn, trip_id, day, draft=draft)

    client = client or _client()
    parts: list[str] = []
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, retry on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            for text in stream.text_stream:
                parts.append(text)
                yield text
            final = stream.get_final_message()
    except anthropic.AuthenticationError:
        yield "\n[Claude rejected the API key: check ANTHROPIC_API_KEY and restart the server.]"
        return
    except TypeError as exc:  # the SDK raises this when no credentials are configured at all
        if "authentication" not in str(exc):
            raise
        yield "\n[Claude isn't configured: set ANTHROPIC_API_KEY before starting the server.]"
        return
    except anthropic.RateLimitError:
        yield "\n[Rate limited by the Claude API - wait a moment and try again.]"
        return
    except anthropic.APIStatusError as exc:
        yield f"\n[Claude API error {exc.status_code}: {exc.message}]"
        return
    except anthropic.APIConnectionError:
        yield "\n[Couldn't reach the Claude API - check your connection.]"
        return

    if final.stop_reason == "refusal":
        yield "\n[Claude declined to answer this one - try rephrasing.]"
        return
    reply = "".join(parts).strip()
    if reply and not draft:
        save_message(conn, trip_id, day, "assistant", reply)
