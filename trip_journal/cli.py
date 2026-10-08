"""Command line entry point: `trip-journal import ...`, `trip-journal serve`."""

from __future__ import annotations

import argparse
from pathlib import Path

from .db import connect
from .importer import import_folder


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="trip-journal")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="import a folder of photos into a trip")
    imp.add_argument("trip", help='trip name, e.g. "Japan 2019"')
    imp.add_argument("folder", type=Path, help="folder with the trip's photos (searched recursively)")
    imp.add_argument("--no-geocode", action="store_true", help="skip looking up place names online")

    sub.add_parser("list", help="list imported trips")

    serve = sub.add_parser("serve", help="start the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    conn = connect()

    if args.command == "import":
        report = import_folder(conn, args.trip, args.folder, geocode=not args.no_geocode, progress=print)
        print(
            f"\nImported {report.imported} photos ({report.skipped} already imported, {report.failed} failed)."
        )
        if report.estimated_location:
            print(f"{report.estimated_location} photos without GPS got a location from a nearby photo.")
        if report.no_location:
            print(f"{report.no_location} photos still have no location.")
        if report.undated:
            print(f"{report.undated} photos have no date and won't appear on the timeline.")
    elif args.command == "list":
        for row in conn.execute(
            """SELECT t.name, COUNT(p.id) n, MIN(p.day) s, MAX(p.day) e
               FROM trips t LEFT JOIN photos p ON p.trip_id = t.id GROUP BY t.id ORDER BY s"""
        ):
            print(f"{row['name']}: {row['n']} photos, {row['s']} → {row['e']}")
    elif args.command == "serve":
        import uvicorn

        from .server import create_app

        print(f"Open http://{args.host}:{args.port}")
        uvicorn.run(create_app(conn), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
