"""Local development commands; no cultural content is generated."""
import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.db import PointRecord, RoomRecord, make_session_factory
from app.migrations import upgrade_database


def import_test_data(database_url: str | None = None) -> dict[str, int]:
    manifest = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "test_data.json").read_text(encoding="utf-8"))
    factory = make_session_factory(database_url)
    inserted = {"rooms": 0, "points": 0}
    try:
        with factory.begin() as db:
            for room in manifest["rooms"]:
                existing = db.get(RoomRecord, room["key"])
                if existing is None:
                    if db.scalar(select(RoomRecord.id).where(RoomRecord.tour_id == manifest["tour_key"], RoomRecord.order == room["order"])):
                        raise ValueError("Room order already used; test import refused")
                    db.add(RoomRecord(id=room["key"], tour_id=manifest["tour_key"], order=room["order"]))
                    inserted["rooms"] += 1
                elif existing.tour_id != manifest["tour_key"] or existing.order != room["order"]:
                    raise ValueError("Existing room differs; test import refused")
                db.flush()
                for point in room["points"]:
                    existing_point = db.get(PointRecord, point["key"])
                    if existing_point is None:
                        if db.scalar(select(PointRecord.id).where(PointRecord.room_id == room["key"], PointRecord.order == point["order"])):
                            raise ValueError("Point order already used; test import refused")
                        db.add(PointRecord(id=point["key"], room_id=room["key"], order=point["order"]))
                        inserted["points"] += 1
                    elif existing_point.room_id != room["key"] or existing_point.order != point["order"]:
                        raise ValueError("Existing point differs; test import refused")
    finally:
        factory.kw["bind"].dispose()
    return inserted


def main():
    parser = argparse.ArgumentParser(prog="musiyo")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    importer = commands.add_parser("import")
    importer.add_argument("dataset", choices=["test-data"])
    args = parser.parse_args()
    if args.command == "migrate":
        upgrade_database()
    else:
        print(json.dumps(import_test_data()))


if __name__ == "__main__":
    main()
