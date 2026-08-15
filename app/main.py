"""Entrypoint: wires the webhook server and the Telegram poll loop together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import aiohttp
from aiohttp import web

from .bot import SeerrTelegramBot
from .config import Config, ConfigError
from .seerr import SeerrClient, SeerrError
from .server import build_app
from .telegram import TelegramClient, TelegramError, poll_updates, sleep_or_stop

logger = logging.getLogger("seerrbot")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def connect_telegram(bot: SeerrTelegramBot, stop: asyncio.Event) -> bool:
    """Wait for Telegram to answer, distinguishing a bad token from bad network.

    A container can easily start before its network is ready, so connection
    failures are retried. Only a token Telegram actively rejects is fatal.
    """
    backoff = 2.0
    while not stop.is_set():
        try:
            me = await bot.telegram.get_me()
            logger.info("Telegram bot connected as @%s", me.get("username"))
            return True
        except TelegramError as exc:
            if exc.error_code in (401, 404):
                logger.error(
                    "Telegram rejected the bot token: %s. "
                    "Check TELEGRAM_BOT_TOKEN against @BotFather.",
                    exc,
                )
                return False
            logger.warning("Cannot reach Telegram (%s); retrying in %.0fs", exc, backoff)
            await sleep_or_stop(stop, backoff)
            backoff = min(backoff * 2, 30.0)
    return False


async def startup_checks(bot: SeerrTelegramBot, config: Config) -> None:
    """Log a clear verdict on the Seerr side of the integration at boot."""
    with contextlib.suppress(TelegramError):
        await bot.telegram.delete_webhook()

    try:
        status = await bot.seerr.status()
        logger.info("Seerr %s reachable at %s", status.get("version"), config.seerr_url)
    except SeerrError as exc:
        logger.error("Seerr unreachable: %s", exc)

    try:
        counts = await bot.seerr.request_counts()
        logger.info(
            "Seerr API key accepted (%s pending, %s total requests)",
            counts.get("pending", 0),
            counts.get("total", 0),
        )
    except SeerrError as exc:
        logger.error("Seerr API key check failed: %s", exc)

    if config.admin_chat_id is None:
        logger.warning(
            "ADMIN_CHAT_ID is not set. Send /start to the bot to get your chat ID, "
            "set the variable, and restart. Requests are dropped until then."
        )
    else:
        logger.info("Approvals will be sent to chat %s", config.admin_chat_id)


async def run() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO)
        logger.error("Configuration error: %s", exc)
        return 2

    configure_logging(config.log_level)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        telegram = TelegramClient(
            config.telegram_bot_token, session, config.telegram_api_base
        )
        seerr = SeerrClient(
            config.seerr_url, config.seerr_api_key, session, config.request_timeout
        )
        bot = SeerrTelegramBot(config, telegram, seerr)

        if not await connect_telegram(bot, stop):
            return 1
        await startup_checks(bot, config)

        runner = web.AppRunner(build_app(config, bot))
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", config.port)
        await site.start()
        logger.info(
            "Listening on http://0.0.0.0:%s%s", config.port, config.webhook_path
        )

        await bot.announce_start()

        poller = asyncio.create_task(poll_updates(telegram, bot.handle_update, stop))
        await stop.wait()

        logger.info("Shutting down")
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        await runner.cleanup()

    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
