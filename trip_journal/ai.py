"""The memory companion: Claude looks at a day's photos and metadata and helps you remember.

Each request rebuilds the day's context (its activities with times, places and notes,
a selection of photos, your journal so far, neighbouring days) as the first user turn,
followed by the saved chat. The context sits at the front so prompt caching covers it
across a conversation.

Photo selection: a per-request budget is shared between the day's activities in
proportion to how many photos each has (every activity gets at least one). Starred
photos are picked first. When the chat is focused on one activity, that activity gets
a larger budget of its own.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterator

import anthropic
from PIL import Image

from . import activities as acts
from .db import data_dir, now

MODEL = os.environ.get("TRIP_JOURNAL_MODEL", "claude-opus-5-5")
MAX_PHOTOS_PER_DAY = int(os.environ.get("TRIP_JOURNAL_MAX_PHOTOS", "40"))
MAX_PHOTOS_FOCUSED = int(os.environ.get("TRIP_JOURNAL_MAX_FOCUS_PHOTOS", "30"))
AI_IMAGE_SIDE = 768  # plenty for recognising a temple or a bowl of ramen; keeps tokens down
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are a warm, curious travel companion helping someone reconstruct a journal of a trip \
they took, often years ago. You are given one day at a time. The day is split into \
activities (groups of photos close together in time and place - a temple visit, a lunch, \
a walk through a neighbourhood). For each you get the times, place names, the person's \
notes, and a selection of the photos. You also see what they've written for the day and a \
little about the days before and after.

Your job is to help them remember, not to remember for them.
- Ground everything in the evidence. Say what the photos actually show ("at 14:10 you were \
by a river in Fushimi; there's a long tunnel of orange gates") and be explicit when you are \
inferring or guessing ("this looks like it could be Fushimi Inari").
- Ask one or two specific, evocative questions at a time: sensory details, who they were \
with, what they ate, what surprised them, how they got from one activity to the next, gaps \
in the timeline ("nothing between 11:00 and 16:00 - what happened there?").
- Use your knowledge of the places to jog memories (local foods, landmarks nearby, what \
that neighbourhood is known for), but never present it as something they did.
- You only see a selection of each activity's photos; the counts tell you how many exist.
- If the message is about a specific activity, focus on it.
- Keep replies short and conversational. The person's own words and memories are what \
matter; the journal is theirs.

When asked to draft a journal entry, write in the first person in the person's voice, \
using only what they told you (in chat and notes) plus what the photos clearly show. Follow \
the day's activities in order. Mark anything uncertain with [?] so they can check it. No \
headings or bullet lists unless they ask - a few natural paragraphs."""

DRAFT_INSTRUCTION = (
    "Please draft the journal entry for this day now, based on our conversation, my notes, what "
    "I've already written, and the photos. Output only the entry text."
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
    """Evenly spaced sample that keeps the first and last item (the bookends)."""
    if k <= 0:
        return []
    if len(items) <= k:
        return items
    if k == 1:
        return [items[0]]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def allocate(counts: list[int], budget: int) -> list[int]:
    """Share a photo budget between activities: one each first, the rest by size."""
    alloc = [0] * len(counts)
    if budget >= sum(1 for c in counts if c):
        alloc = [min(1, c) for c in counts]
    remaining = budget - sum(alloc)
    while remaining > 0:
        open_ = [i for i, c in enumerate(counts) if alloc[i] < c]
        if not open_:
            break
        i = max(open_, key=lambda j: counts[j] / (alloc[j] + 1))
        alloc[i] += 1
        remaining -= 1
    return alloc


def pick_photos(photos: list[sqlite3.Row], k: int) -> list[sqlite3.Row]:
    """Starred photos first, then an even spread over the rest, returned in time order."""
    starred = [p for p in photos if p["starred"]]
    chosen = starred[:k]
    if len(chosen) < k:
        rest = [p for p in photos if not p["starred"]]
        chosen += _sample(rest, k - len(chosen))
    return sorted(chosen, key=lambda p: (p["taken_at"] or "", p["id"]))


def _time(p: sqlite3.Row) -> str:
    return p["taken_at"][11:16] if p["taken_at"] else "??:??"


def _photo_line(p: sqlite3.Row) -> str:
    where = p["place"] or (f"{p['lat']:.4f}, {p['lon']:.4f}" if p["lat"] is not None else "unknown location")
    if p["lat"] is not None and p["location_estimated"]:
        where += " (location estimated)"
    return f"{_time(p)} - {where}" + (" ★" if p["starred"] else "")


def activity_label(a: dict, number: int) -> str:
    name = a["title"] or a["place"] or "untitled"
    return f"Activity {number} \"{name}\" ({a['start'][11:16]}-{a['end'][11:16]})"


def _places_summary(conn: sqlite3.Connection, trip_id: int, day: str) -> str:
    rows = conn.execute(
        """SELECT place FROM photos WHERE trip_id = ? AND day = ? AND place IS NOT NULL
           GROUP BY place ORDER BY MIN(taken_at)""",
        (trip_id, day),
    ).fetchall()
    return "; ".join(r["place"] for r in rows) or "no named places"


def build_context(
    conn: sqlite3.Connection, trip_id: int, day: str, focus_activity: int | None = None
) -> list[dict]:
    trip = conn.execute("SELECT name FROM trips WHERE id = ?", (trip_id,)).fetchone()
    all_days = [
        r["day"] for r in conn.execute("SELECT day FROM days WHERE trip_id = ? ORDER BY day", (trip_id,))
    ]
    entry = conn.execute(
        "SELECT title, journal FROM days WHERE trip_id = ? AND day = ?", (trip_id, day)
    ).fetchone()
    day_acts = acts.list_for_day(conn, trip_id, day)
    photos_by_act = {
        a["id"]: conn.execute(
            "SELECT * FROM photos WHERE activity_id = ? ORDER BY taken_at, id", (a["id"],)
        ).fetchall()
        for a in day_acts
    }

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

    title = entry["title"].strip() if entry else ""
    journal = entry["journal"].strip() if entry else ""
    lines.append(f"\nTheir title for the day: {title or '(none yet)'}")
    lines.append(f"What they've written for the day so far:\n{journal or '(nothing yet)'}")

    total = sum(len(p) for p in photos_by_act.values())
    lines.append(f"\nThe day's {len(day_acts)} activities ({total} photos in all):")
    for n, a in enumerate(day_acts, 1):
        lines.append(f"\n{activity_label(a, n)} - {a['photos']} photos" + (" [FOCUS]" if a["id"] == focus_activity else ""))
        if a["notes"].strip():
            lines.append(f"  Their notes: {a['notes'].strip()}")
        lines.append("  Photo times: " + ", ".join(_photo_line(p) for p in photos_by_act[a["id"]]))

    # Decide how many photos of each activity Claude gets to see.
    counts = [len(photos_by_act[a["id"]]) for a in day_acts]
    if focus_activity in photos_by_act:
        fi = next(i for i, a in enumerate(day_acts) if a["id"] == focus_activity)
        focused = min(counts[fi], MAX_PHOTOS_FOCUSED)
        others = [0 if i == fi else c for i, c in enumerate(counts)]
        alloc = allocate(others, _others_budget(focused))
        alloc[fi] = focused
    else:
        alloc = allocate(counts, MAX_PHOTOS_PER_DAY)

    blocks: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    shown = sum(alloc)
    if shown:
        blocks.append({"type": "text", "text": f"\nA selection of {shown} of the day's photos:"})
    for n, (a, k) in enumerate(zip(day_acts, alloc), 1):
        for p in pick_photos(list(photos_by_act[a["id"]]), k):
            thumb = data_dir() / p["thumb"]
            if thumb.is_file():
                blocks.append({"type": "text", "text": f"[Activity {n} · {_photo_line(p)}]"})
                blocks.append(_image_block(thumb))
    return blocks


def _others_budget(focused: int) -> int:
    """Photos left for the other activities when one is focused."""
    return max(MAX_PHOTOS_PER_DAY - focused, MAX_PHOTOS_PER_DAY // 4)


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
    conn: sqlite3.Connection, trip_id: int, day: str, *, draft: bool = False,
    focus_activity: int | None = None,
) -> list[dict]:
    context = build_context(conn, trip_id, day, focus_activity)
    context.append({"type": "text", "text": KICKOFF})
    messages = [{"role": "user", "content": context}, *history(conn, trip_id, day)]
    if draft:
        messages.append({"role": "user", "content": DRAFT_INSTRUCTION})
    return _merge_roles(messages)


def _focus_prefix(conn: sqlite3.Connection, trip_id: int, day: str, activity_id: int | None) -> str:
    if activity_id is None:
        return ""
    for n, a in enumerate(acts.list_for_day(conn, trip_id, day), 1):
        if a["id"] == activity_id:
            return f"(About {activity_label(a, n)}) "
    return ""


def stream_reply(
    conn: sqlite3.Connection,
    trip_id: int,
    day: str,
    user_message: str | None,
    *,
    draft: bool = False,
    focus_activity: int | None = None,
    client: anthropic.Anthropic | None = None,
) -> Iterator[str]:
    """Stream Claude's reply as text chunks. Chat turns are saved; drafts are not."""
    text = (user_message or "").strip()
    if text or focus_activity is not None:
        prefix = _focus_prefix(conn, trip_id, day, focus_activity)
        message = prefix + (text or "Let's talk about this one.")
        save_message(conn, trip_id, day, "user", message)
    messages = build_messages(conn, trip_id, day, draft=draft, focus_activity=focus_activity)

    parts: list[str] = []
    try:
        client = client or _client()
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, retry on Anthropic's recommended fallback model.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            for chunk in stream.text_stream:
                parts.append(chunk)
                yield chunk
            final = stream.get_final_message()
    except Exception as exc:
        error = describe_error(exc)
        if error is None:
            raise
        yield f"\n[{error}]"
        return

    if final.stop_reason == "refusal":
        yield "\n[Claude declined to answer this one - try rephrasing.]"
        return
    reply = "".join(parts).strip()
    if reply and not draft:
        save_message(conn, trip_id, day, "assistant", reply)


def describe_error(exc: Exception) -> str | None:
    """A readable message for API problems, or None for genuine bugs."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude rejected the API key: check ANTHROPIC_API_KEY and restart the app."
    if isinstance(exc, anthropic.CredentialsError):
        return f"Claude credentials problem: {exc}"
    if isinstance(exc, TypeError) and "authentication" in str(exc):
        # The SDK raises this when no credentials are configured at all.
        return "Claude isn't configured: set ANTHROPIC_API_KEY and restart the app."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Claude API - wait a moment and try again."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API - check your connection."
    return None


SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "activities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"number": {"type": "integer"}, "title": {"type": "string"}},
                "required": ["number", "title"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["activities"],
    "additionalProperties": False,
}

SUGGEST_PROMPT = """\
Suggest a short title (2-5 words) for each activity of this travel day, the way someone \
would label it in their journal: the specific landmark, museum, temple, market or \
neighbourhood if the photos and place names make it clear (e.g. "Fushimi Inari Shrine", \
"Ramen lunch in Gion", "Night market in Shilin"), otherwise a plain description of what \
the photos show ("Train to Osaka", "Walk along the river"). Return one title per activity, \
using the activity numbers given."""


def suggest_titles(
    conn: sqlite3.Connection, trip_id: int, day: str, client: anthropic.Anthropic | None = None
) -> dict[int, str]:
    """Ask Claude to name the day's activities from their photos. Returns {activity_id: title}."""
    day_acts = acts.list_for_day(conn, trip_id, day)
    if not day_acts:
        return {}
    content: list[dict] = [{"type": "text", "text": SUGGEST_PROMPT}]
    for n, a in enumerate(day_acts, 1):
        photos = conn.execute(
            "SELECT * FROM photos WHERE activity_id = ? ORDER BY taken_at, id", (a["id"],)
        ).fetchall()
        content.append({
            "type": "text",
            "text": f"\nActivity {n}: {a['start'][11:16]}-{a['end'][11:16]}, "
                    f"{a['place'] or 'unknown place'}, {a['photos']} photos"
                    + (f"\nNotes: {a['notes'].strip()}" if a["notes"].strip() else ""),
        })
        for p in pick_photos(list(photos), 4):
            thumb = data_dir() / p["thumb"]
            if thumb.is_file():
                content.append(_image_block(thumb))

    client = client or _client()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SUGGEST_SCHEMA}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        return {}
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    by_number = {item["number"]: item["title"].strip() for item in json.loads(text).get("activities", [])}
    return {a["id"]: by_number[n] for n, a in enumerate(day_acts, 1) if by_number.get(n)}
