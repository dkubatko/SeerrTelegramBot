"""A stand-in for Seerr, for testing the bot without a real instance.

Implements only the endpoints the bot calls, plus /trigger/* helpers that fire
webhooks at the bot the same way Seerr's notification agent would.

    python scripts/mock_seerr.py --port 5055 --bot-url http://127.0.0.1:8420/webhook
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

import aiohttp
from aiohttp import web

logger = logging.getLogger("mock-seerr")

# Where trigger/* sends webhooks. Held in a dict so tests can point it at a
# port that is only known after the bot has bound one.
SETTINGS: web.AppKey[dict[str, Any]] = web.AppKey("settings", dict)

API_KEY = "test-api-key"

POSTER = "https://image.tmdb.org/t/p/w600_and_h900_bestv2/qJ2tW6WMUDux911r6m7haRef0WH.jpg"

REQUESTS: dict[str, dict[str, Any]] = {
    "1": {
        "id": 1,
        "status": 1,
        "is4k": False,
        "media": {"id": 10, "mediaType": "movie", "tmdbId": 155, "status": 3},
        "requestedBy": {"id": 2, "displayName": "alice", "email": "alice@example.com"},
        "seasons": [],
    },
    "2": {
        "id": 2,
        "status": 1,
        "is4k": True,
        "media": {"id": 11, "mediaType": "tv", "tmdbId": 1396, "status": 3},
        "requestedBy": {"id": 3, "displayName": "bob", "email": "bob@example.com"},
        "seasons": [{"id": 1, "seasonNumber": 1}, {"id": 2, "seasonNumber": 2}],
    },
}

TITLES = {
    ("movie", "155"): {
        "title": "The Dark Knight",
        "releaseDate": "2008-07-16",
        "posterPath": "/qJ2tW6WMUDux911r6m7haRef0WH.jpg",
        "overview": "Batman raises the stakes in his war on crime.",
    },
    ("tv", "1396"): {
        "name": "Breaking Bad",
        "firstAirDate": "2008-01-20",
        "posterPath": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
        "overview": "A high school chemistry teacher turns to a life of crime.",
    },
}


def _check_key(request: web.Request) -> None:
    if request.headers.get("X-Api-Key") != API_KEY:
        raise web.HTTPForbidden(
            text=json.dumps({"error": "invalid api key"}),
            content_type="application/json",
        )


async def status(_request: web.Request) -> web.Response:
    return web.json_response({"version": "2.1.0-mock", "updateAvailable": False})


async def request_count(request: web.Request) -> web.Response:
    _check_key(request)
    values = REQUESTS.values()
    return web.json_response(
        {
            "total": len(REQUESTS),
            "pending": sum(1 for r in values if r["status"] == 1),
            "approved": sum(1 for r in values if r["status"] == 2),
            "declined": sum(1 for r in values if r["status"] == 3),
        }
    )


async def list_requests(request: web.Request) -> web.Response:
    _check_key(request)
    wanted = request.query.get("filter", "all")
    results = [
        r for r in REQUESTS.values() if wanted != "pending" or r["status"] == 1
    ]
    return web.json_response({"pageInfo": {"results": len(results)}, "results": results})


async def get_request(request: web.Request) -> web.Response:
    _check_key(request)
    record = REQUESTS.get(request.match_info["rid"])
    if not record:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "not found"}), content_type="application/json"
        )
    return web.json_response(record)


async def set_status(request: web.Request) -> web.Response:
    _check_key(request)
    record = REQUESTS.get(request.match_info["rid"])
    if not record:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "not found"}), content_type="application/json"
        )
    decision = request.match_info["decision"]
    record["status"] = 2 if decision == "approve" else 3
    logger.info("request %s -> %s", record["id"], decision.upper())
    return web.json_response(record)


async def media_details(request: web.Request) -> web.Response:
    _check_key(request)
    kind = "movie" if request.path.startswith("/api/v1/movie") else "tv"
    details = TITLES.get((kind, request.match_info["tid"]))
    if not details:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "not found"}), content_type="application/json"
        )
    return web.json_response(details)


# --- webhook triggers, mimicking Seerr's notification agent -----------------


def pending_payload(record: dict[str, Any]) -> dict[str, Any]:
    media = record["media"]
    kind = media["mediaType"]
    details = TITLES.get((kind, str(media["tmdbId"])), {})
    title = details.get("title") or details.get("name") or "Unknown"
    year = (details.get("releaseDate") or details.get("firstAirDate") or "")[:4]

    extra = []
    if record.get("is4k"):
        extra.append({"name": "Requested Quality", "value": "4K"})
    if record.get("seasons"):
        extra.append(
            {
                "name": "Requested Seasons",
                "value": ", ".join(str(s["seasonNumber"]) for s in record["seasons"]),
            }
        )

    return {
        "notification_type": "MEDIA_PENDING",
        "event": f"New {'Movie' if kind == 'movie' else 'Series'} Request",
        "subject": f"{title} ({year})" if year else title,
        "message": details.get("overview", ""),
        "image": POSTER if details.get("posterPath") else "",
        "media": {
            "media_type": kind,
            "tmdbId": str(media["tmdbId"]),
            "tvdbId": "",
            "status": "PENDING",
            "status4k": "UNKNOWN",
        },
        "request": {
            "request_id": str(record["id"]),
            "requestedBy_email": record["requestedBy"]["email"],
            "requestedBy_username": record["requestedBy"]["displayName"],
            "requestedBy_avatar": "",
        },
        "issue": None,
        "comment": None,
        "extra": extra,
    }


TEST_PAYLOAD = {
    "notification_type": "TEST_NOTIFICATION",
    "event": "Test Notification",
    "subject": "Test Notification",
    "message": "Check check, 1, 2, 3. Are we coming in clear?",
    "image": "",
    "media": None,
    "request": None,
    "issue": None,
    "comment": None,
    "extra": [],
}


async def trigger(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    bot_url = settings["bot_url"]
    auth = settings["auth"]
    kind = request.match_info["kind"]

    if kind == "test":
        payload = TEST_PAYLOAD
    else:
        rid = request.query.get("id", "1")
        record = REQUESTS.get(rid)
        if not record:
            return web.json_response({"error": f"no request {rid}"}, status=404)
        record["status"] = 1  # re-arm so the trigger is repeatable
        payload = pending_payload(record)

    headers = {"Authorization": auth} if auth else {}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(bot_url, json=payload, headers=headers) as resp:
                status_code = resp.status
                body = (await resp.text()).strip()
        except aiohttp.ClientError as exc:
            logger.error("could not reach bot at %s: %s", bot_url, exc)
            return web.json_response({"error": str(exc)}, status=502)

    logger.info("sent %s webhook -> HTTP %s %s", kind, status_code, body)
    return web.json_response(
        {"sent": kind, "bot_status": status_code, "bot_response": body}
    )


def build_app(bot_url: str = "", auth: str | None = None) -> web.Application:
    app = web.Application()
    app[SETTINGS] = {"bot_url": bot_url, "auth": auth}
    app.router.add_get("/api/v1/status", status)
    app.router.add_get("/api/v1/request/count", request_count)
    app.router.add_get("/api/v1/request", list_requests)
    app.router.add_get("/api/v1/request/{rid}", get_request)
    app.router.add_post("/api/v1/request/{rid}/{decision}", set_status)
    app.router.add_get("/api/v1/movie/{tid}", media_details)
    app.router.add_get("/api/v1/tv/{tid}", media_details)
    app.router.add_post("/trigger/{kind}", trigger)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[mock-seerr] %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--bot-url", default="http://127.0.0.1:8420/webhook")
    parser.add_argument("--auth", default=None, help="value for Authorization header")
    args = parser.parse_args()

    logger.info("API key is %r", API_KEY)
    logger.info("Fire a pending request:  curl -XPOST localhost:%s/trigger/pending?id=1", args.port)
    logger.info("Fire a test webhook:     curl -XPOST localhost:%s/trigger/test", args.port)
    web.run_app(
        build_app(args.bot_url, args.auth),
        port=args.port,
        print=None,
        access_log=None,
    )


if __name__ == "__main__":
    main()
