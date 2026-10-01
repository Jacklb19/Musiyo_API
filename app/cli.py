"""Local development commands; no cultural content is generated."""
import argparse
import getpass
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.content_operations import (
    create_validator,
    export_state,
    import_package,
    reindex,
    revoke,
    verify_validity,
    withdraw_tour,
)
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="musiyo")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    importer = commands.add_parser("import")
    importer.add_argument("package", help="test-data, museum-test-data, or a private manifest JSON path")
    importer.add_argument("--dry-run", action="store_true")
    revoker = commands.add_parser("revoke")
    targets = revoker.add_mutually_exclusive_group(required=True)
    targets.add_argument("--element")
    targets.add_argument("--resource")
    revoker.add_argument("--reason", required=True)
    withdrawal = commands.add_parser("withdraw")
    withdrawal.add_argument("--tour", required=True)
    commands.add_parser("verify-validity")
    indexer = commands.add_parser("reindex")
    indexer.add_argument("--element")
    validator = commands.add_parser("create-validator")
    validator.add_argument("--username", required=True)
    validator.add_argument("--name", required=True)
    exporter = commands.add_parser("export-state")
    exporter.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result: dict[str, Any]
    try:
        if args.command == "migrate":
            upgrade_database()
            result = {"migrated": True}
        elif args.command == "import":
            if args.package in ("test-data", "museum-test-data"):
                if args.dry_run:
                    parser.error("Test datasets do not support --dry-run; use a content package")
                result = import_test_data(dataset=args.package)
            else:
                result = import_package(Path(args.package), dry_run=args.dry_run)
        elif args.command == "revoke":
            result = revoke(args.element or args.resource, resource=bool(args.resource), reason=args.reason)
        elif args.command == "withdraw":
            result = withdraw_tour(args.tour)
        elif args.command == "verify-validity":
            result = verify_validity()
        elif args.command == "reindex":
            result = reindex(element_id=args.element)
        elif args.command == "create-validator":
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Confirm password: "):
                parser.error("Passwords differ")
            result = create_validator(args.username, args.name, password)
        else:
            result = export_state()
            if args.output:
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    except ValidationError as error:
        fields = [".".join(map(str, item["loc"])) for item in error.errors()]
        parser.exit(2, "Invalid manifest fields: " + ", ".join(fields) + "\n")
    except SQLAlchemyError:
        parser.exit(2, "Database operation failed; check migrations and relationships\n")
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
