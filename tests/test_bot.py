"""Unit tests covering payload parsing, routing, and decision handling."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.bot import SeerrTelegramBot  # noqa: E402
from app.config import Config, normalize_seerr_url  # noqa: E402
from app.formatting import CAPTION_LIMIT, RequestNotification  # noqa: E402
from app.telegram import is_valid_button_url  # noqa: E402
from app.seerr import SeerrError  # noqa: E402
from mock_seerr import REQUESTS, TEST_PAYLOAD, pending_payload  # noqa: E402


def make_config(**overrides: Any) -> Config:
    base = dict(
        telegram_bot_token="token",
        telegram_api_base="https://api.telegram.org",
        seerr_url="http://seerr:5055",
        seerr_public_url="http://seerr:5055",
        seerr_api_key="key",
        admin_chat_id=42,
        rejected_group_chat_id=None,
        webhook_auth_token=None,
        webhook_path="/webhook",
        port=8420,
        log_level="INFO",
        forward_other_notifications=False,
        notify_on_start=False,
        request_timeout=15.0,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self._next_id += 1
        self.sent.append(
            {"chat_id": chat_id, "text": text, "markup": reply_markup, "photo": False}
        )
        return {"message_id": self._next_id}

    async def send_photo(self, chat_id, photo, caption, reply_markup=None):
        self._next_id += 1
        self.sent.append(
            {"chat_id": chat_id, "text": caption, "markup": reply_markup, "photo": True}
        )
        return {"message_id": self._next_id}

    async def edit_text(self, chat_id, message_id, text, *, is_caption, reply_markup=None):
        self.edits.append({"message_id": message_id, "text": text, "caption": is_caption})
        return {}

    async def answer_callback(self, callback_id, text=None, alert=False):
        self.answers.append({"text": text, "alert": alert})
        return {}


class FakeSeerr:
    def __init__(self) -> None:
        self.records = {k: dict(v) for k, v in REQUESTS.items()}
        self.calls: list[tuple[str, str]] = []
        self.fail_with: SeerrError | None = None

    async def get_request(self, request_id):
        record = self.records.get(str(request_id))
        if record is None:
            raise SeerrError("not found", 404)
        return record

    async def set_request_status(self, request_id, decision):
        if self.fail_with:
            raise self.fail_with
        self.calls.append((str(request_id), decision))
        self.records[str(request_id)]["status"] = 2 if decision == "approve" else 3
        return self.records[str(request_id)]

    async def request_counts(self):
        return {"total": 2, "pending": 2, "approved": 0, "declined": 0}

    async def status(self):
        return {"version": "2.1.0"}

    async def pending_requests(self, take=10):
        return [r for r in self.records.values() if r["status"] == 1][:take]

    async def media_details(self, media_type, tmdb_id):
        return {"title": f"Title {tmdb_id}", "poster": None}


def build_bot(**config_overrides: Any):
    config = make_config(**config_overrides)
    telegram = FakeTelegram()
    seerr = FakeSeerr()
    return SeerrTelegramBot(config, telegram, seerr), telegram, seerr


def callback(
    data: str,
    chat_id: int = 42,
    message_id: int = 101,
    photo: bool = False,
    user_id: int | None = None,
):
    message: dict[str, Any] = {"message_id": message_id, "chat": {"id": chat_id}}
    if photo:
        message["photo"] = [{"file_id": "x"}]
        message["caption"] = "🎬 <b>The Dark Knight (2008)</b>"
    else:
        message["text"] = "🎬 The Dark Knight (2008)"
    return {
        "id": "cb1",
        "data": data,
        "message": message,
        "from": {"id": chat_id if user_id is None else user_id, "username": "dan"},
    }


class TestConfigLoading(unittest.TestCase):
    """ADMIN_CHAT_ID is refused at load time, not merely documented as private."""

    BASE_ENV = {
        "TELEGRAM_BOT_TOKEN": "token",
        "SEERR_URL": "http://seerr:5055",
        "SEERR_API_KEY": "key",
    }

    def _load(self, admin_chat_id: str | None) -> Config:
        env = dict(self.BASE_ENV)
        if admin_chat_id is not None:
            env["ADMIN_CHAT_ID"] = admin_chat_id
        with mock.patch.dict(os.environ, env, clear=True):
            return Config.from_env()

    def test_group_chat_id_is_rejected_at_load(self):
        for group_id in ("-100500", "-1001234567890"):
            config = self._load(group_id)
            self.assertIsNone(config.admin_chat_id, group_id)
            self.assertEqual(config.rejected_group_chat_id, int(group_id))

    def test_zero_is_rejected_at_load(self):
        config = self._load("0")
        self.assertIsNone(config.admin_chat_id)
        self.assertEqual(config.rejected_group_chat_id, 0)

    def test_private_chat_id_is_accepted(self):
        config = self._load("1101242859")
        self.assertEqual(config.admin_chat_id, 1101242859)
        self.assertIsNone(config.rejected_group_chat_id)

    def test_unset_is_neither_configured_nor_rejected(self):
        config = self._load(None)
        self.assertIsNone(config.admin_chat_id)
        self.assertIsNone(config.rejected_group_chat_id)


class TestButtonUrls(unittest.TestCase):
    """Telegram rejects the whole message over one bad button URL."""

    def test_domain_and_ip_hosts_are_usable(self):
        for url in (
            "http://192.168.1.196:5055/movie/155",
            "http://127.0.0.1:5055/movie/155",
            "https://seerr.example.com/movie/155",
            "http://seerr.lan:5055/tv/1396",
        ):
            self.assertTrue(is_valid_button_url(url), url)

    def test_bare_hostnames_are_not_usable(self):
        for url in (
            "http://jellyseerr:5055/movie/155",  # a Docker container name
            "http://mock-seerr:5055/movie/155",
            "http://localhost:5055/movie/155",
            "http://overseerr/movie/155",
        ):
            self.assertFalse(is_valid_button_url(url), url)

    def test_junk_is_not_usable(self):
        for url in (None, "", "not a url", "ftp://seerr.example.com", "://x"):
            self.assertFalse(is_valid_button_url(url), repr(url))


class TestUrlNormalization(unittest.TestCase):
    def test_variants_collapse_to_an_origin(self):
        for raw in [
            "http://host:5055",
            "http://host:5055/",
            "http://host:5055/api/v1",
            "http://host:5055/api/v1/",
            "host:5055",
        ]:
            self.assertEqual(normalize_seerr_url(raw), "http://host:5055", raw)

    def test_https_is_preserved(self):
        self.assertEqual(
            normalize_seerr_url("https://seerr.example.com/"), "https://seerr.example.com"
        )


class TestPayloadParsing(unittest.TestCase):
    def test_pending_payload_is_recognized(self):
        notification = RequestNotification(pending_payload(REQUESTS["1"]))
        self.assertTrue(notification.is_pending_request)
        self.assertEqual(notification.request_id, "1")
        self.assertEqual(notification.requested_by, "alice")
        self.assertEqual(notification.media_type, "movie")

    def test_test_payload_has_no_request(self):
        notification = RequestNotification(TEST_PAYLOAD)
        self.assertFalse(notification.is_pending_request)
        self.assertEqual(notification.notification_type, "TEST_NOTIFICATION")

    def test_null_media_and_request_do_not_crash(self):
        notification = RequestNotification({"notification_type": "MEDIA_PENDING"})
        self.assertFalse(notification.is_pending_request)
        self.assertEqual(notification.requested_by, "someone")

    def test_html_in_title_is_escaped(self):
        payload = pending_payload(REQUESTS["1"]) | {"subject": "<script>x</script>"}
        text = RequestNotification(payload).pending_text(CAPTION_LIMIT)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_caption_stays_within_telegram_limit(self):
        payload = pending_payload(REQUESTS["1"]) | {"message": "x" * 5000}
        text = RequestNotification(payload).pending_text(CAPTION_LIMIT)
        self.assertLessEqual(len(text), CAPTION_LIMIT)

    def test_card_orders_title_description_details_then_requester(self):
        text = RequestNotification(pending_payload(REQUESTS["3"])).pending_text(4096)
        paragraphs = text.split("\n\n")

        self.assertEqual(len(paragraphs), 3)
        self.assertIn("Breaking Bad", paragraphs[0].split("\n")[0])
        self.assertIn("<i>Walter White", paragraphs[0].split("\n")[1])
        self.assertIn("Requested Seasons", paragraphs[1])
        self.assertTrue(paragraphs[2].startswith("👤 Requested by"))

    def test_decision_line_follows_the_requester(self):
        notification = RequestNotification(pending_payload(REQUESTS["3"]))
        text = notification.resolved_text("approve", "@dan", 4096)
        lines = text.split("\n\n")[-1].split("\n")

        self.assertTrue(lines[0].startswith("👤 Requested by"))
        self.assertEqual(lines[1], "✅ Approved by <b>@dan</b>")
        self.assertNotIn("Approved", text.split("\n")[0])

    def test_denied_reads_as_one_line(self):
        notification = RequestNotification(pending_payload(REQUESTS["1"]))
        text = notification.resolved_text("decline", "the Seerr web UI", 4096)
        self.assertIn("🚫 Denied by <b>the Seerr web UI</b>", text)

    def test_paragraphs_collapse_when_there_is_nothing_to_show(self):
        text = RequestNotification(
            {"notification_type": "MEDIA_PENDING", "subject": "Bare"}
        ).pending_text(4096)
        self.assertEqual(text, "📺 <b>Bare</b>")

    def test_description_is_dropped_rather_than_crowding_a_caption(self):
        payload = pending_payload(REQUESTS["3"]) | {"message": "x" * 5000}
        text = RequestNotification(payload).pending_text(CAPTION_LIMIT)

        self.assertLessEqual(len(text), CAPTION_LIMIT)
        self.assertIn("Requested Seasons", text)
        self.assertTrue(text.split("\n\n")[-1].startswith("👤 Requested by"))

    def test_seasons_appear_in_the_message(self):
        text = RequestNotification(pending_payload(REQUESTS["2"])).pending_text(1024)
        self.assertIn("Requested Seasons", text)
        self.assertIn("1, 2", text)


class TestWebhookRouting(unittest.IsolatedAsyncioTestCase):
    async def test_pending_sends_message_with_buttons(self):
        bot, telegram, _ = build_bot()
        status, _ = await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        self.assertEqual(status, 200)
        self.assertEqual(len(telegram.sent), 1)
        sent = telegram.sent[0]
        self.assertEqual(sent["chat_id"], 42)
        buttons = sent["markup"]["inline_keyboard"][0]
        self.assertEqual(
            [b["callback_data"] for b in buttons], ["approve:1", "decline:1"]
        )

    async def test_unusable_seerr_url_still_delivers_the_card(self):
        """A container-name SEERR_URL must cost the link, never the card."""
        bot, telegram, _ = build_bot(seerr_public_url="http://jellyseerr:5055")
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        rows = telegram.sent[0]["markup"]["inline_keyboard"]
        self.assertEqual(len(rows), 1, "the link row should have been dropped")
        self.assertEqual(
            [b["callback_data"] for b in rows[0]], ["approve:1", "decline:1"]
        )

    async def test_usable_public_url_keeps_the_link(self):
        bot, telegram, _ = build_bot(seerr_public_url="http://192.168.1.10:5055")
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        rows = telegram.sent[0]["markup"]["inline_keyboard"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0]["url"], "http://192.168.1.10:5055/movie/155")

    async def test_test_notification_confirms_in_telegram(self):
        bot, telegram, _ = build_bot()
        status, _ = await bot.handle_webhook(TEST_PAYLOAD)

        self.assertEqual(status, 200)
        self.assertIn("Webhook test received", telegram.sent[0]["text"])

    async def test_missing_admin_chat_reports_failure_to_seerr(self):
        bot, telegram, _ = build_bot(admin_chat_id=None)
        status, message = await bot.handle_webhook(TEST_PAYLOAD)

        self.assertEqual(status, 503)
        self.assertIn("ADMIN_CHAT_ID", message)
        self.assertEqual(telegram.sent, [])

    async def test_unrelated_types_are_ignored_by_default(self):
        bot, telegram, _ = build_bot()
        status, message = await bot.handle_webhook(
            {"notification_type": "MEDIA_AVAILABLE", "subject": "Something"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(message, "ignored")
        self.assertEqual(telegram.sent, [])

    async def test_forwarding_can_be_enabled(self):
        bot, telegram, _ = build_bot(forward_other_notifications=True)
        await bot.handle_webhook(
            {"notification_type": "MEDIA_AVAILABLE", "subject": "Something", "event": "Now Available"}
        )
        self.assertIn("Now Available", telegram.sent[0]["text"])


class TestDecisions(unittest.IsolatedAsyncioTestCase):
    async def test_approve_calls_seerr_and_edits_the_message(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})

        self.assertEqual(seerr.calls, [("1", "approve")])
        self.assertIn("Approved", telegram.edits[0]["text"])
        self.assertIn("@dan", telegram.edits[0]["text"])
        self.assertEqual(telegram.answers[0]["text"], "Approved ✅")

    async def test_deny_calls_seerr_with_decline(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("decline:1")})

        self.assertEqual(seerr.calls, [("1", "decline")])
        self.assertIn("Denied", telegram.edits[0]["text"])

    async def test_photo_messages_are_edited_as_captions(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        self.assertTrue(telegram.sent[0]["photo"])

        await bot.handle_update({"callback_query": callback("approve:1", photo=True)})
        self.assertTrue(telegram.edits[0]["caption"])

    async def test_already_resolved_request_is_not_re_decided(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        seerr.records["1"]["status"] = 2  # approved in the web UI meanwhile

        await bot.handle_update({"callback_query": callback("approve:1")})

        self.assertEqual(seerr.calls, [])
        self.assertIn("Already approved", telegram.answers[0]["text"])
        self.assertTrue(telegram.answers[0]["alert"])

    async def test_button_from_another_chat_is_rejected(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        await bot.handle_update({"callback_query": callback("approve:1", chat_id=999)})

        self.assertEqual(seerr.calls, [])
        self.assertIn("not authorized", telegram.answers[0]["text"])

    async def test_group_admin_chat_id_refuses_button_presses(self):
        """The whole point of rejecting group IDs: no member inherits approval."""
        config = make_config(admin_chat_id=None, rejected_group_chat_id=-100500)
        bot = SeerrTelegramBot(config, FakeTelegram(), (seerr := FakeSeerr()))
        telegram = bot.telegram

        for presser in (-100500, 42, 999):
            await bot.handle_update(
                {
                    "callback_query": callback(
                        "approve:1", chat_id=-100500, user_id=presser
                    )
                }
            )

        self.assertEqual(seerr.calls, [])
        self.assertEqual(len(telegram.answers), 3)
        self.assertTrue(all(a["alert"] for a in telegram.answers))

    async def test_press_from_another_user_in_the_chat_is_rejected(self):
        """Identity comes from the sender, never from the chat alone."""
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        await bot.handle_update(
            {"callback_query": callback("approve:1", chat_id=42, user_id=999)}
        )

        self.assertEqual(seerr.calls, [])
        self.assertIn("not authorized", telegram.answers[0]["text"])

    async def test_seerr_failure_surfaces_as_an_alert(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        seerr.fail_with = SeerrError("Seerr rejected the API key (403).", 403)

        await bot.handle_update({"callback_query": callback("approve:1")})

        self.assertTrue(telegram.answers[0]["alert"])
        self.assertIn("403", telegram.answers[0]["text"])
        self.assertEqual(telegram.edits, [])

    async def test_decision_survives_a_restart(self):
        """Buttons keep working when the in-memory message index is empty."""
        bot, telegram, seerr = build_bot()
        await bot.handle_update({"callback_query": callback("approve:2")})

        self.assertEqual(seerr.calls, [("2", "approve")])
        self.assertIn("Approved", telegram.edits[0]["text"])

    async def test_external_decision_updates_the_pending_message(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        await bot.handle_webhook(
            {"notification_type": "MEDIA_DECLINED", "request": {"request_id": "1"}}
        )

        self.assertIn("Denied", telegram.edits[0]["text"])
        self.assertIn("Seerr web UI", telegram.edits[0]["text"])

    async def test_echo_of_our_own_decision_is_not_re_applied(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})
        edits_after_button = len(telegram.edits)

        await bot.handle_webhook(
            {"notification_type": "MEDIA_APPROVED", "request": {"request_id": "1"}}
        )

        self.assertEqual(len(telegram.edits), edits_after_button)


class TestCommands(unittest.IsolatedAsyncioTestCase):
    def _message(self, text: str, chat_id: int = 42) -> dict[str, Any]:
        return {
            "message": {
                "message_id": 1,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "username": "dan"},
                "text": text,
            }
        }

    async def test_start_reports_the_chat_id(self):
        bot, telegram, _ = build_bot(admin_chat_id=None)
        await bot.handle_update(self._message("/start", chat_id=777))

        text = telegram.sent[0]["text"]
        self.assertIn("<code>777</code>", text)
        self.assertIn("ADMIN_CHAT_ID", text)

    async def test_start_accepts_the_bot_handle_suffix(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/start@MySeerrBot"))
        self.assertIn("<code>42</code>", telegram.sent[0]["text"])

    async def test_start_is_answered_for_non_admins(self):
        """Anyone may learn their own ID; that is the point of the command."""
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/start", chat_id=999))
        self.assertIn("<code>999</code>", telegram.sent[0]["text"])

    async def test_other_commands_are_refused_for_non_admins(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/status", chat_id=999))
        self.assertIn("direct conversation", telegram.sent[0]["text"])

    async def test_test_command_reports_both_directions(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/test"))

        text = telegram.sent[0]["text"]
        self.assertIn("Reachable", text)
        self.assertIn("API key accepted", text)
        self.assertIn("2.1.0", text)

    async def test_pending_lists_open_requests_with_buttons(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/pending"))

        open_requests = len(REQUESTS)
        self.assertIn(f"{open_requests}</b> pending", telegram.sent[0]["text"])
        self.assertEqual(len(telegram.sent), open_requests + 1)  # header, then each
        self.assertEqual(
            telegram.sent[1]["markup"]["inline_keyboard"][0][0]["callback_data"],
            "approve:1",
        )

    async def test_every_admin_command_waits_for_admin_chat_id(self):
        """Pre-configuration there is nobody to authorize against, so /test
        must not leak the Seerr address, version, or request counts."""
        for command in ("/test", "/status", "/pending"):
            bot, telegram, seerr = build_bot(admin_chat_id=None)
            await bot.handle_update(self._message(command, chat_id=999))

            self.assertEqual(len(telegram.sent), 1, command)
            body = telegram.sent[0]["text"]
            self.assertIn("ADMIN_CHAT_ID", body, command)
            self.assertNotIn("http://seerr:5055", body, command)
            self.assertNotIn("2.1.0", body, command)

    async def test_another_user_cannot_run_admin_commands(self):
        bot, telegram, _ = build_bot()
        message = self._message("/pending", chat_id=42)
        message["message"]["from"] = {"id": 999, "username": "stranger"}

        await bot.handle_update(message)
        self.assertIn("direct conversation", telegram.sent[0]["text"])

    async def test_start_in_a_group_refuses_and_gives_no_id(self):
        bot, telegram, _ = build_bot()
        message = self._message("/start", chat_id=-100500)
        message["message"]["chat"]["type"] = "supergroup"

        await bot.handle_update(message)

        body = telegram.sent[0]["text"]
        self.assertIn("direct conversation", body)
        self.assertNotIn("-100500", body)

    async def test_a_group_admin_chat_id_is_treated_as_unconfigured(self):
        config = make_config(admin_chat_id=None, rejected_group_chat_id=-100500)
        bot = SeerrTelegramBot(config, FakeTelegram(), FakeSeerr())

        self.assertFalse(bot._is_admin(-100500, -100500))
        self.assertFalse(bot._is_admin(-100500, 42))
        status, _ = await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        self.assertEqual(status, 503)

    async def test_non_command_text_is_ignored(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("hello there"))
        self.assertEqual(telegram.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
