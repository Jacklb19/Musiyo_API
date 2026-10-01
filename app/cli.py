"""Local development commands; no cultural content is generated."""
import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.contracts import Tour
from app.db import PointRecord, RoomRecord, TourRecord, make_session_factory
from app.migrations import upgrade_database


def import_test_data(database_url: str | None = None, dataset: str = "test-data") -> dict[str, int]:
    filename = {"test-data": "test_data.json", "museum-test-data": "museum_test_data.json"}[dataset]
    manifest = Tour.model_validate_json((Path(__file__).resolve().parents[1] / "fixtures" / filename).read_text(encoding="utf-8"))
    factory = make_session_factory(database_url)
    inserted = {"rooms": 0, "points": 0}
    try:
        with factory.begin() as db:
            existing_tour = db.get(TourRecord, manifest.tour.key)
            if existing_tour is None:
                guide = manifest.guide
                db.add(TourRecord(id=manifest.tour.key, name=manifest.tour.name, published=True,
                                  guide_key=guide.key if guide else None,
                                  guide_name=guide.name if guide else None,
                                  guide_room_key=guide.room_key if guide else None,
                                  guide_available=guide.available if guide else False))
            elif existing_tour.name != manifest.tour.name:
                raise ValueError("Existing tour differs; test import refused")
            db.flush()
            for room in manifest.rooms:
                existing = db.get(RoomRecord, room.key)
                if existing is None:
                    if db.scalar(select(RoomRecord.id).where(RoomRecord.tour_id == manifest.tour.key, RoomRecord.order == room.order)):
                        raise ValueError("Room order already used; test import refused")
                    db.add(RoomRecord(id=room.key, name=room.name, short_description=room.short_description,
                                      tour_id=manifest.tour.key, order=room.order))
                    inserted["rooms"] += 1
                elif existing.tour_id != manifest.tour.key or existing.order != room.order:
                    raise ValueError("Existing room differs; test import refused")
                db.flush()
                for point in room.points:
                    existing_point = db.get(PointRecord, point.key)
                    if existing_point is None:
                        if db.scalar(select(PointRecord.id).where(PointRecord.room_id == room.key, PointRecord.order == point.order)):
                            raise ValueError("Point order already used; test import refused")
                        db.add(PointRecord(id=point.key, room_id=room.key, name=point.name,
                                           order=point.order, activation=point.activation))
                        inserted["points"] += 1
                    elif existing_point.room_id != room.key or existing_point.order != point.order:
                        raise ValueError("Existing point differs; test import refused")
    finally:
        factory.kw["bind"].dispose()
    return inserted


def main():
    parser = argparse.ArgumentParser(prog="musiyo")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    importer = commands.add_parser("import")
    importer.add_argument("dataset", choices=["test-data", "museum-test-data"])
    args = parser.parse_args()
    if args.command == "migrate":
        upgrade_database()
    else:
        print(json.dumps(import_test_data(dataset=args.dataset)))


if __name__ == "__main__":
    main()
