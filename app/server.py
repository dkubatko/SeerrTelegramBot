"""HTTP surface: the Seerr webhook receiver and a health endpoint."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from aiohttp import web

from .bot import SeerrTelegramBot
from .config import Config

logger = logging.getLogger(__name__)


def build_app(config: Config, bot: SeerrTelegramBot) -> web.Application:
    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "admin_chat_configured": config.admin_chat_id is not None,
                "seerr_url": config.seerr_url,
            }
        )

    async def webhook(request: web.Request) -> web.Response:
        if not _authorized(config, request):
            logger.warning(
                "Rejected webhook with bad Authorization header from %s",
                request.remote,
            )
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload: Any = await request.json()
        except Exception:
            body = (await request.text())[:200]
            logger.warning("Webhook body was not JSON: %r", body)
            return web.json_response({"error": "expected a JSON body"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "expected a JSON object"}, status=400)

        try:
            status, message = await bot.handle_webhook(payload)
        except Exception as exc:
            logger.exception("Webhook handling failed")
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response({"status": message}, status=status)

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post(config.webhook_path, webhook)
    if config.webhook_path != "/":
        # Be forgiving about a webhook URL configured without the path.
        app.router.add_post("/", webhook)
    return app


def _authorized(config: Config, request: web.Request) -> bool:
    if not config.webhook_auth_token:
        return True
    provided = request.headers.get("Authorization", "")
    return hmac.compare_digest(provided, config.webhook_auth_token)
