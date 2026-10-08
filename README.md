# Trip Journal

Rebuild journals of past trips from your photos, then keep the habit going.

Point it at a folder of trip photos and it reads the metadata in each one (when it
was taken, GPS coordinates, camera), groups them into days, and puts them on a map.
For each day you get the route you walked, the photos in order, a journal page, and
a chat with Claude. Claude looks at that day's photos, times and places, then asks you
questions to jog your memory ("nothing between 11:00 and 16:00, what happened
there?"). When you've talked it through, it can draft the entry in your own voice for
you to edit.

Everything runs locally: photos stay where they are, and journals live in a single
SQLite file under `data/`. The only things sent to Claude are a small sample of
downscaled photos for the day you're working on, plus the text context.

## Setup

Requires Python 3.10+.

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...   # https://console.anthropic.com/
```

## Use

```bash
# 1. Import each trip (folders are searched recursively; re-running only adds new photos)
trip-journal import "Japan 2019" ~/Pictures/Japan-2019
trip-journal import "China 2017" ~/Pictures/China
trip-journal import "Southeast Asia 2018" ~/Pictures/SEA

# 2. Open the app
trip-journal serve        # → http://127.0.0.1:8000
```

In the app, pick a trip, then a day. Then:

- **Map**: grey is the whole trip; the selected day's route and photos are highlighted.
  Click any dot to jump to that day.
- **Journal**: type freely. It autosaves.
- **Remember with Claude**: press *Send* with an empty box and Claude opens with
  what it sees in the day, or start by telling it what you remember.
- **Draft entry**: Claude writes an entry from your conversation. Anything uncertain
  is marked `[?]`. *Add to journal* appends the draft to your journal so you can edit it.

## How the photo metadata is used

| Source | What we get |
|---|---|
| EXIF `DateTimeOriginal` | Local time where the photo was taken, which decides its day |
| EXIF GPS | The point on the map |
| No GPS (e.g. a separate camera) | Borrows the location of the nearest-in-time phone photo within 2 hours, marked *estimated* |
| Google Photos Takeout `.json` sidecars | Used when the EXIF was stripped. Takeout only stores UTC, so local time is approximated from longitude |
| OpenStreetMap Nominatim | Place names like "Fushimi, Kyoto, Japan". Cached, limited to about 1 lookup/second. Skip with `--no-geocode` |

HEIC (iPhone) photos are supported. Photos with no date are counted but left off the
timeline.

**Tip:** if your trip photos live in Google Photos or iCloud, export the originals
(Google Takeout, or "Export Unmodified Original" in Apple Photos). Shared or
downloaded copies often have the location stripped.

## Configuration

| Env var | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Needed for the chat |
| `TRIP_JOURNAL_DATA` | `./data` | Where the database and thumbnails go |
| `TRIP_JOURNAL_MODEL` | `claude-opus-5-5` | Claude model for the companion |
| `TRIP_JOURNAL_MAX_PHOTOS` | `12` | Photos per day sent to Claude (sampled evenly across the day) |

## Development

```bash
pip install -e '.[dev]'
pytest
```

Layout: `trip_journal/exif.py` (metadata), `importer.py` (folder → days),
`geocode.py` (place names), `ai.py` (Claude context + streaming), `server.py`
(FastAPI), `static/` (Leaflet map UI, Leaflet vendored).

## Ideas for next steps

- Whole-trip view: a chat across all days, plus an exported book (PDF/Markdown) of the journal.
- A daily reminder for new trips, where Claude asks about today's photos each evening.
- Pull in other traces: Google Maps Timeline exports, boarding passes, receipts.
- Let Claude see more photos on request (tool use), not just a fixed sample.
