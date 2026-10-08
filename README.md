# Trip Journal

Rebuild journals of past trips from your photos, then keep the habit going.

Point it at a folder of trip photos and it reads the metadata in each one (when it
was taken, GPS coordinates, camera). It groups the photos into **days**, then splits
each day into **activities**: the temple in the morning, lunch, the walk through Gion.
It also puts everything on a map.

There are two views:

- **Write** is where you add photos and work on the journal. Each day has a page for
  the day as a whole and a card for each activity, with its photos and your notes. On
  the side there's a small map and a chat with Claude. Claude looks at the day's
  photos, times and places and asks you questions to help you remember ("nothing
  between 11:00 and 16:00, what happened there?"). It can name your activities from
  their photos and draft the day's entry in your voice for you to edit.
- **Read** is the finished result: the trip as a story, with a route map, a chapter
  per day, a timeline of activities with your notes, and photo galleries.

Everything runs on your computer. Your photos are mounted read-only and never
modified. Journals live in a single SQLite file. The only things sent to Claude are
downscaled copies of a selection of photos for the day you're working on, plus that
day's text.

## Run it with Docker (recommended)

You need [Docker](https://docs.docker.com/get-docker/) and an
[Anthropic API key](https://console.anthropic.com/).

```bash
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY, and PHOTOS_DIR to the folder holding your trip folders
docker compose up -d --build
```

Open <http://localhost:8000>, click **+ Add photos**, pick a trip folder and give the
trip a name. Importing a folder again only adds the new photos, so you can also use it
to add a second camera's photos to the same trip.

Your journals are kept in the `journal-data` Docker volume, so they survive
`docker compose down` and rebuilds. `docker compose down -v` deletes them.
To back up:

```bash
docker compose cp trip-journal:/data ./journal-backup
```

## Run it without Docker

Requires Python 3.10+.

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
export TRIP_JOURNAL_PHOTOS=~/Pictures   # folder the in-app importer can browse
trip-journal serve                       # → http://127.0.0.1:8000

# or import from the terminal
trip-journal import "Japan 2019" ~/Pictures/Japan-2019
```

## Using it

**Activities.** A new activity starts when there's a gap of 45+ minutes between
photos, or when you've moved more than ~800 m (a quick run of photos from a moving
train stays in one). The split is only a starting point:

- **✂ new** on a photo starts a new activity from that photo.
- **⤒ Merge** folds an activity into the one before it. Notes are kept.
- **✨ Suggest names** asks Claude to name the untitled activities from their photos
  ("Fushimi Inari Shrine", "Ramen lunch in Gion"). Titles you've written are never changed.

**★ Star** your favourite photos. Starred photos are the first ones Claude sees, and
the Read view features them: the first starred photo of a day becomes its cover.

**Talking to Claude.** Press *Send* on an empty box and Claude opens with what it sees.
Claude gets up to 40 photos per message, shared across the day's activities by how
many photos each has. Click **💬 Ask** on an activity to focus the chat on it: that
activity gets up to 30 photos of its own, which helps for a long museum or temple
visit. **Draft entry** writes the day in your voice from your conversation and notes,
marking anything uncertain with `[?]`. You choose whether to add it.

## How the photo metadata is used

| Source | What we get |
|---|---|
| EXIF `DateTimeOriginal` | Local time where the photo was taken, which decides its day and activity |
| EXIF GPS | The point on the map |
| No GPS (e.g. a separate camera) | Borrows the location of the nearest-in-time phone photo within 2 hours, marked *estimated* |
| Google Photos Takeout `.json` sidecars | Used when the EXIF was stripped. Takeout only stores UTC, so local time is approximated from longitude |
| OpenStreetMap Nominatim | Place names like "Fushimi, Kyoto, Japan". Cached, limited to about 1 lookup/second. Optional |

HEIC (iPhone) photos are supported. Photos with no date are counted but left off the
timeline.

**Tip:** export originals. Copies shared through messaging apps, or downloaded from
some web galleries, often have the date and location stripped. For Google Photos use
Google Takeout; for Apple Photos use *File → Export → Export Unmodified Original*.

## Configuration

| Env var | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Needed for the chat, drafts and suggested names |
| `PHOTOS_DIR` | – | *(Docker only)* folder on your computer mounted read-only at `/photos` |
| `TRIP_JOURNAL_PHOTOS` | home folder (`/photos` in Docker) | Root folder the in-app importer can browse |
| `TRIP_JOURNAL_DATA` | `./data` (`/data` in Docker) | Database and thumbnails |
| `TRIP_JOURNAL_MODEL` | `claude-opus-5-5` | Claude model |
| `TRIP_JOURNAL_MAX_PHOTOS` | `40` | Photos per message, shared across a day's activities |
| `TRIP_JOURNAL_MAX_FOCUS_PHOTOS` | `30` | Photos of the focused activity when you use 💬 Ask |

## Development

```bash
pip install -e '.[dev]'
pytest
```

Layout: `trip_journal/exif.py` (metadata), `importer.py` (folder → days),
`activities.py` (days → activities, merge/split), `geocode.py` (place names),
`ai.py` (Claude context, photo selection, streaming, naming), `server.py` (FastAPI),
`static/` (UI; Leaflet is bundled so no CDN is needed).

## Ideas for next steps

- Export the Read view as a PDF/printable book.
- A chat about the whole trip, not one day at a time.
- A daily reminder on new trips: Claude asks about today's photos each evening.
- Pull in other traces: Google Maps Timeline exports, boarding passes, receipts.
- Let Claude ask to see specific photos itself (tool use) instead of a fixed selection.
