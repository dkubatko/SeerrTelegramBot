"""End-to-end test: mock Seerr -> real bot wiring -> mock Telegram and back.

Exercises the actual HTTP server, Telegram poll loop, and Seerr client that
run in production, with only the two remote services replaced.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import telegram as telegram_module  # noqa: E402
from app.bot import SeerrTelegramBot  # noqa: E402
from app.config import Config  # noqa: E402
from app.seerr import SeerrClient  # noqa: E402
from app.server import build_app  # noqa: E402
from app.telegram import TelegramClient, poll_updates  # noqa: E402
from mock_seerr import REQUESTS, SETTINGS  # noqa: E402
from mock_seerr import build_app as build_seerr_app  # noqa: E402
from mock_telegram import MockTelegram  # noqa: E402

# The production 30s long-poll would stall teardown between tests.
telegram_module.POLL_TIMEOUT = 1

ADMIN_CHAT = 4242
AUTH_TOKEN = "s3cret"


async def start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for record in REQUESTS.values():
            record["status"] = 1

        self.telegram_mock = MockTelegram()
        self.telegram_runner, telegram_port = await start(
            self.telegram_mock.build_app()
        )

        # Bot's own webhook listener, started before Seerr so we know its port.
        self.session = aiohttp.ClientSession()
        self.bot_runner: web.AppRunner | None = None

        seerr_runner, seerr_port = await start(build_seerr_app())
        self.seerr_runner = seerr_runner
        self.seerr_url = f"http://127.0.0.1:{seerr_port}"

        self.config = Config(
            telegram_bot_token="test-token",
            telegram_api_base=f"http://127.0.0.1:{telegram_port}",
            seerr_url=self.seerr_url,
            seerr_public_url=self.seerr_url,
            seerr_api_key="test-api-key",
            admin_chat_id=ADMIN_CHAT,
            group_chat_id=None,
            rejected_group_chat_id=None,
            webhook_auth_token=AUTH_TOKEN,
            webhook_path="/webhook",
            port=0,
            log_level="CRITICAL",
            forward_other_notifications=False,
            notify_on_start=False,
            state_file=None,
            request_timeout=10.0,
        )

        telegram = TelegramClient(
            self.config.telegram_bot_token, self.session, self.config.telegram_api_base
        )
        seerr = SeerrClient(
            self.seerr_url, "test-api-key", self.session, self.config.request_timeout
        )
        self.bot = SeerrTelegramBot(self.config, telegram, seerr)

        self.bot_runner, bot_port = await start(build_app(self.config, self.bot))
        self.webhook_url = f"http://127.0.0.1:{bot_port}/webhook"

        # Point the mock Seerr's trigger helper back at the bot.
        seerr_runner.app[SETTINGS].update(bot_url=self.webhook_url, auth=AUTH_TOKEN)

        self.stop = asyncio.Event()
        self.poller = asyncio.create_task(
            poll_updates(telegram, self.bot.handle_update, self.stop)
        )

    async def asyncTearDown(self) -> None:
        self.stop.set()
        self.poller.cancel()
        try:
            await self.poller
        except asyncio.CancelledError:
            pass
        await self.session.close()
        for runner in (self.bot_runner, self.seerr_runner, self.telegram_runner):
            if runner:
                await runner.cleanup()

    async def trigger(self, kind: str, request_id: str = "1") -> dict:
        url = f"{self.seerr_url}/trigger/{kind}?id={request_id}"
        async with self.session.post(url) as resp:
            return await resp.json()

    async def seerr_status_of(self, request_id: str) -> int:
        async with self.session.get(
            f"{self.seerr_url}/api/v1/request/{request_id}",
            headers={"X-Api-Key": "test-api-key"},
        ) as resp:
            return (await resp.json())["status"]

    # ----------------------------------------------------------------- tests

    async def test_seerr_test_button_reaches_telegram(self):
        result = await self.trigger("test")

        self.assertEqual(result["bot_status"], 200)
        await self.telegram_mock.wait_for_sent(1)
        self.assertIn("Webhook test received", self.telegram_mock.sent[0]["text"])

    async def test_pending_request_approved_from_telegram(self):
        result = await self.trigger("pending", "1")
        self.assertEqual(result["bot_status"], 200)

        await self.telegram_mock.wait_for_sent(1)
        message = self.telegram_mock.sent[0]
        self.assertEqual(message["chat_id"], ADMIN_CHAT)
        self.assertIn("The Dark Knight", message["text"])
        self.assertTrue(message["photo"], "poster should be sent as a photo")

        buttons = message["markup"]["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], "approve:1")

        self.assertEqual(await self.seerr_status_of("1"), 1)

        self.telegram_mock.push_callback(
            "approve:1", ADMIN_CHAT, message["message_id"], is_photo=True
        )
        await self.telegram_mock.wait_for_sent(2)

        self.assertEqual(await self.seerr_status_of("1"), 2)
        self.assertIn(message["message_id"], self.telegram_mock.deleted)

        replacement = self.telegram_mock.sent[-1]
        self.assertIn("Approved by <b>@tester</b>", replacement["text"])
        self.assertTrue(replacement["photo"], "the poster should survive")
        self.assertEqual(self.telegram_mock.answers[-1]["text"], "Approved ✅")

    async def test_pending_request_denied_from_telegram(self):
        await self.trigger("pending", "2")
        await self.telegram_mock.wait_for_sent(1)
        message = self.telegram_mock.sent[0]
        self.assertIn("Breaking Bad", message["text"])
        self.assertIn("Requested Seasons", message["text"])

        self.telegram_mock.push_callback(
            "decline:2", ADMIN_CHAT, message["message_id"], is_photo=True
        )
        await self.telegram_mock.wait_for_sent(2)

        self.assertEqual(await self.seerr_status_of("2"), 3)
        self.assertIn(message["message_id"], self.telegram_mock.deleted)
        self.assertIn("Denied by <b>@tester</b>", self.telegram_mock.sent[-1]["text"])

    async def test_webhook_rejects_a_bad_auth_header(self):
        async with self.session.post(
            self.webhook_url,
            json={"notification_type": "TEST_NOTIFICATION"},
            headers={"Authorization": "wrong"},
        ) as resp:
            self.assertEqual(resp.status, 401)
        self.assertEqual(self.telegram_mock.sent, [])

    async def test_start_command_returns_the_chat_id(self):
        self.telegram_mock.push_command("/start", 9001)
        await self.telegram_mock.wait_for_sent(1)
        self.assertIn("<code>9001</code>", self.telegram_mock.sent[0]["text"])

    async def test_test_command_probes_seerr(self):
        self.telegram_mock.push_command("/test", ADMIN_CHAT)
        await self.telegram_mock.wait_for_sent(1)

        text = self.telegram_mock.sent[0]["text"]
        self.assertIn("2.1.0-mock", text)
        self.assertIn("API key accepted", text)

    async def test_health_endpoint(self):
        base = self.webhook_url.rsplit("/", 1)[0]
        async with self.session.get(f"{base}/health") as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["admin_chat_configured"])

    async def test_unauthorized_chat_cannot_approve(self):
        await self.trigger("pending", "1")
        await self.telegram_mock.wait_for_sent(1)
        message = self.telegram_mock.sent[0]

        self.telegram_mock.push_callback("approve:1", 999, message["message_id"])

        async def wait_for_answer() -> None:
            while not self.telegram_mock.answers:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_for_answer(), 5)
        self.assertIn("not authorized", self.telegram_mock.answers[0]["text"])
        self.assertEqual(await self.seerr_status_of("1"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
