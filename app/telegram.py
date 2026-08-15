"""Minimal async Telegram Bot API client plus a long-polling loop.

Only the handful of methods this shim needs are implemented, which keeps the
image small and the runtime dependency list at exactly one package.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 30
ALLOWED_UPDATES = ["message", "callback_query"]


class TelegramError(Exception):
    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class TelegramClient:
    def __init__(
        self,
        token: str,
        session: aiohttp.ClientSession,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        self._base = f"{api_base.rstrip('/')}/bot{token}"
        self._session = session

    async def call(self, method: str, http_timeout: float = 20.0, **params: Any) -> Any:
        # `http_timeout` is the socket deadline; Telegram's own long-poll
        # `timeout` travels in `params`, so the names must not collide.
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            async with self._session.post(
                f"{self._base}/{method}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=http_timeout),
            ) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise TelegramError(f"{method} failed: {exc}") from exc
        except TimeoutError as exc:
            raise TelegramError(f"{method} timed out") from exc

        if not data.get("ok"):
            raise TelegramError(
                f"{method} rejected: {data.get('description', 'unknown error')}",
                data.get("error_code"),
            )
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def delete_webhook(self) -> Any:
        # A previously configured push webhook would starve getUpdates.
        return await self.call("deleteWebhook", drop_pending_updates=False)

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": disable_preview},
        )

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.call(
            "sendPhoto",
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def edit_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        is_caption: bool,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        method = "editMessageCaption" if is_caption else "editMessageText"
        field = "caption" if is_caption else "text"
        return await self.call(
            method,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
            **{field: text},
        )

    async def answer_callback(
        self, callback_id: str, text: str | None = None, alert: bool = False
    ) -> Any:
        return await self.call(
            "answerCallbackQuery",
            callback_query_id=callback_id,
            text=text,
            show_alert=alert,
        )


async def poll_updates(
    client: TelegramClient,
    handler: Callable[[dict[str, Any]], Awaitable[None]],
    stop: asyncio.Event,
) -> None:
    """Long-poll getUpdates until `stop` is set, dispatching each update."""
    offset: int | None = None
    backoff = 1.0

    while not stop.is_set():
        try:
            updates = await client.call(
                "getUpdates",
                http_timeout=POLL_TIMEOUT + 15,
                offset=offset,
                allowed_updates=ALLOWED_UPDATES,
                timeout=POLL_TIMEOUT,
            )
            backoff = 1.0
        except TelegramError as exc:
            if exc.error_code == 409:
                logger.error(
                    "Telegram reports another instance polling with this token "
                    "(409). Stop the other copy of the bot. Retrying in %.0fs.",
                    backoff,
                )
            else:
                logger.warning("getUpdates failed (%s); retrying in %.0fs", exc, backoff)
            await sleep_or_stop(stop, backoff)
            backoff = min(backoff * 2, 60.0)
            continue

        for update in updates or []:
            offset = update["update_id"] + 1
            try:
                await handler(update)
            except Exception:  # a bad update must not kill the poll loop
                logger.exception("Unhandled error while processing update %s", offset)


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except (TimeoutError, asyncio.TimeoutError):
        pass


def keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}
