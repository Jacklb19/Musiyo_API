"""Concurrent visitor sessions against an explicitly selected local API (RNF-05).

Each virtual visitor runs the catalogue, ficha (with a signed resource download whose SHA-256 is
checked), tour and guide/quiz flows at the same time as the others. The report keeps endpoint
names, status codes, timings and check outcomes only; no answers or resource bytes are stored.
This is HTTP load from one computer: it does not exercise Unity, GPUs, browsers or real networks.
"""
import argparse
import asyncio
import hashlib
import json
import random
import time
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

import httpx

from scripts.evaluate_guide import WORKSPACE


class Visitor:
    def __init__(self, client: httpx.AsyncClient, number: int, think: tuple[float, float], tour: str):
        self.client, self.number, self.think, self.tour = client, number, think, tour
        self.calls: list[dict] = []
        self.failures: list[str] = []

    async def call(self, name: str, method: str, url: str, expected: int = 200, **kwargs):
        started = time.monotonic()
        try:
            response = await self.client.request(method, url, **kwargs)
            status: int | str = response.status_code
        except httpx.HTTPError as error:
            response, status = None, type(error).__name__
        self.calls.append(dict(endpoint=name, status=status, seconds=round(time.monotonic() - started, 4)))
        if status != expected:
            self.failures.append(f"{name}: {status}")
            return None
        return response

    def check(self, condition: bool, label: str):
        if not condition:
            self.failures.append(label)

    async def pause(self):
        await asyncio.sleep(random.uniform(*self.think))

    async def run(self, slugs: list[str]):
        slug = slugs[self.number % len(slugs)]
        # Catalogue
        catalog = await self.call("catalog", "GET", "/catalog")
        if catalog:
            self.check(catalog.json()["total"] == len(slugs), "catalog: total changed")
        await self.call("categories", "GET", "/categories")
        await self.call("collections", "GET", "/collections")
        await self.call("catalog_search", "GET", "/catalog", params={"query": "máscara"})
        await self.pause()
        # Ficha and one signed resource
        element = await self.call("element", "GET", f"/elements/{slug}")
        if element:
            body = element.json()
            self.check(body["slug"] == slug and bool(body["blocks"]), "element: incomplete ficha")
            resources = sorted(body["resources"], key=lambda item: item["byte_count"])
            if resources:
                access = await self.call("resource_access", "POST", f"/resources/{resources[0]['id']}/access", json={})
                if access:
                    grant = access.json()
                    # The grant URL is API-relative ("/api/v1/..."); the client base already ends in /api/v1.
                    delivery = await self.call("resource_delivery", "GET", grant["url"].removeprefix("/api/v1"))
                    if delivery:
                        self.check(hashlib.sha256(delivery.content).hexdigest() == grant["sha256"]
                                   and len(delivery.content) == grant["byte_count"], "resource: content mismatch")
        await self.pause()
        # Tour
        tour = await self.call("tour", "GET", f"/tours/{self.tour}")
        if tour:
            rooms = tour.json()["rooms"]
            self.check(len(rooms) > 0 and all(room.get("points") for room in rooms), "tour: empty room")
        await self.pause()
        # Guide and quiz
        title = element.json()["title"] if element else slug
        answer = await self.call("guide_question", "POST", "/guide/questions",
                                 json={"schema_version": 1, "question": f"¿Qué es {title}?", "element_slug": None})
        if answer:
            self.check(answer.json()["status"] in {"answered", "abstained"}, "guide: provider unavailable")
        quiz = await self.call("quiz", "GET", "/guide/quiz", params={"tour_key": self.tour, "schema_version": 1})
        if quiz and quiz.json()["questions"]:
            question = quiz.json()["questions"][self.number % len(quiz.json()["questions"])]
            await self.call("quiz_answer", "POST", "/guide/quiz/answers",
                            json={"schema_version": 1, "id": question["id"], "option_id": question["options"][0]["id"]})


def summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return dict(samples=len(ordered), median_seconds=round(median(ordered), 4),
                p95_seconds=round(ordered[max(0, round(0.95 * len(ordered)) - 1)], 4),
                maximum_seconds=round(ordered[-1], 4))


async def run(base: str, sessions: int, think: tuple[float, float], database: Path | None,
              transport: httpx.AsyncBaseTransport | None = None, tour: str = "museum-main") -> dict:
    before = hashlib.sha256(database.read_bytes()).hexdigest() if database else None
    limits = httpx.Limits(max_connections=sessions * 2)
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=30, limits=limits, transport=transport) as setup:
        slugs = [item["slug"] for item in (await setup.get("/catalog", params={"limit": 100})).raise_for_status().json()["items"]]
    clients = [httpx.AsyncClient(base_url=base, trust_env=False, timeout=30, transport=transport) for _ in range(sessions)]
    visitors = [Visitor(client, number, think, tour) for number, client in enumerate(clients)]
    started = time.monotonic()
    try:
        await asyncio.gather(*(visitor.run(slugs) for visitor in visitors))
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))
    elapsed = round(time.monotonic() - started, 3)
    after = hashlib.sha256(database.read_bytes()).hexdigest() if database else None
    calls = [call for visitor in visitors for call in visitor.calls]
    endpoints = sorted({call["endpoint"] for call in calls})
    return dict(
        schema_version=1, measured_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), base=base, sessions=sessions,
        think_seconds=list(think), elapsed_seconds=elapsed, catalogue_size=len(slugs), requests=len(calls),
        sessions_without_failures=sum(not visitor.failures for visitor in visitors),
        failures=[dict(session=visitor.number, failures=visitor.failures) for visitor in visitors if visitor.failures],
        status_counts={status: sum(str(call["status"]) == status for call in calls) for status in sorted({str(c["status"]) for c in calls})},
        endpoints={name: summary([call["seconds"] for call in calls if call["endpoint"] == name]) for name in endpoints},
        database_sha256_before=before, database_sha256_after=after,
        database_unchanged=None if database is None else before == after,
        limits="HTTP sessions from one computer and one source address; not Unity/GPU, browsers, real networks or people.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Local API base, e.g. http://127.0.0.1:8021/api/v1")
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--tour", default="museum-main")
    parser.add_argument("--think-min", type=float, default=0.2)
    parser.add_argument("--think-max", type=float, default=0.8)
    parser.add_argument("--database", help="Selected SQLite file served by that API, hashed before and after")
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    url = urlsplit(arguments.base)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"} or not url.path.endswith("/api/v1"):
        raise ValueError("Select a local loopback API base ending in /api/v1")
    if not 1 <= arguments.sessions <= 50 or not 0 <= arguments.think_min <= arguments.think_max <= 5:
        raise ValueError("Select 1-50 sessions and a bounded think time")
    output = Path(arguments.output).resolve()
    if output.is_relative_to(WORKSPACE) or output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new output file in an existing external directory")
    database = Path(arguments.database).resolve(strict=True) if arguments.database else None
    report = asyncio.run(run(arguments.base.rstrip("/"), arguments.sessions, (arguments.think_min, arguments.think_max), database,
                             tour=arguments.tour))
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("sessions", "sessions_without_failures", "requests", "status_counts", "database_unchanged")}))
    return 0 if report["sessions_without_failures"] == report["sessions"] and report["database_unchanged"] is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
