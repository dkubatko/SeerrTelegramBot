"""Unit tests covering payload parsing, routing, and decision handling."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.bot import SeerrTelegramBot  # noqa: E402
from app.config import Config, normalize_seerr_url  # noqa: E402
from app.formatting import CAPTION_LIMIT, RequestNotification  # noqa: E402
from app.telegram import TelegramError, is_valid_button_url  # noqa: E402
from app.seerr import SeerrError  # noqa: E402
from app.state import MessageStore, SentMessage  # noqa: E402
from mock_seerr import REQUESTS, TEST_PAYLOAD, pending_payload  # noqa: E402


def make_config(**overrides: Any) -> Config:
    base = dict(
        telegram_bot_token="token",
        telegram_api_base="https://api.telegram.org",
        seerr_url="http://seerr:5055",
        seerr_public_url="http://seerr:5055",
        seerr_api_key="key",
        admin_chat_id=42,
        group_chat_id=None,
        rejected_group_chat_id=None,
        webhook_auth_token=None,
        webhook_path="/webhook",
        port=8420,
        log_level="INFO",
        forward_other_notifications=False,
        notify_on_start=False,
        state_file=None,  # tests stay in memory
        request_timeout=15.0,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self._next_id += 1
        self.sent.append(
            {
                "message_id": self._next_id,
                "chat_id": chat_id,
                "text": text,
                "markup": reply_markup,
                "photo": False,
            }
        )
        return {"message_id": self._next_id}

    async def send_photo(self, chat_id, photo, caption, reply_markup=None):
        self._next_id += 1
        self.sent.append(
            {
                "message_id": self._next_id,
                "chat_id": chat_id,
                "text": caption,
                "markup": reply_markup,
                "photo": True,
            }
        )
        return {"message_id": self._next_id}

    async def edit_text(self, chat_id, message_id, text, *, is_caption, reply_markup=None):
        self.edits.append(
            {
                "message_id": message_id,
                "text": text,
                "caption": is_caption,
                "markup": reply_markup,
            }
        )
        return {}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True

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


class TestDeliveryTarget(unittest.TestCase):
    """Delivery and authority are separate: a group can watch, one user acts."""

    def test_without_a_group_cards_go_to_the_admin(self):
        self.assertEqual(make_config().target_chat_id, 42)

    def test_a_group_takes_over_delivery_only(self):
        config = make_config(group_chat_id=-100500)
        self.assertEqual(config.target_chat_id, -100500)
        self.assertEqual(config.admin_chat_id, 42)

    def test_no_target_before_an_admin_is_configured(self):
        config = make_config(admin_chat_id=None, group_chat_id=-100500)
        self.assertIsNone(config.target_chat_id)

    def test_group_chat_id_is_read_from_the_environment(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "SEERR_URL": "http://seerr:5055",
            "SEERR_API_KEY": "key",
            "ADMIN_CHAT_ID": "42",
            "GROUP_CHAT_ID": "-100500",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.group_chat_id, -100500)
        self.assertEqual(config.target_chat_id, -100500)


class TestGroupDelivery(unittest.IsolatedAsyncioTestCase):
    GROUP = -100500

    def _bot(self):
        return build_bot(group_chat_id=self.GROUP)

    async def test_cards_are_posted_to_the_group(self):
        bot, telegram, _ = self._bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        self.assertEqual(telegram.sent[0]["chat_id"], self.GROUP)

    async def test_the_admin_can_approve_from_inside_the_group(self):
        bot, telegram, seerr = self._bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        await bot.handle_update(
            {"callback_query": callback("approve:1", chat_id=self.GROUP, user_id=42)}
        )

        self.assertEqual(seerr.calls, [("1", "approve")])
        self.assertEqual(telegram.sent[-1]["chat_id"], self.GROUP)

    async def test_other_group_members_cannot_approve(self):
        bot, telegram, seerr = self._bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        for member in (999, 1234, self.GROUP):
            await bot.handle_update(
                {
                    "callback_query": callback(
                        "approve:1", chat_id=self.GROUP, user_id=member
                    )
                }
            )

        self.assertEqual(seerr.calls, [])
        self.assertTrue(all("not authorized" in a["text"] for a in telegram.answers))

    async def test_the_admin_can_still_use_commands_in_their_dm(self):
        bot, telegram, _ = self._bot()
        await bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "username": "dan"},
                    "text": "/status",
                }
            }
        )
        self.assertIn("Pending", telegram.sent[0]["text"])

    async def test_the_bot_stays_inert_in_an_unrelated_group(self):
        bot, telegram, seerr = self._bot()
        await bot.handle_update(
            {"callback_query": callback("approve:1", chat_id=-999, user_id=42)}
        )
        self.assertEqual(seerr.calls, [])
        self.assertIn("not authorized", telegram.answers[0]["text"])


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
            {"notification_type": "ISSUE_CREATED", "subject": "Something"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(message, "ignored")
        self.assertEqual(telegram.sent, [])

    async def test_forwarding_can_be_enabled(self):
        bot, telegram, _ = build_bot(forward_other_notifications=True)
        await bot.handle_webhook(
            {"notification_type": "ISSUE_CREATED", "subject": "Something", "event": "Issue Reported"}
        )
        self.assertIn("Issue Reported", telegram.sent[0]["text"])


class TestDecisions(unittest.IsolatedAsyncioTestCase):
    async def test_approve_replaces_the_card_so_it_notifies(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        original = telegram.sent[0]

        await bot.handle_update({"callback_query": callback("approve:1")})

        self.assertEqual(seerr.calls, [("1", "approve")])
        self.assertEqual(telegram.deleted, [original["message_id"]])
        self.assertEqual(telegram.edits, [], "a silent edit would not notify")

        replacement = telegram.sent[-1]
        self.assertIn("Approved by <b>@dan</b>", replacement["text"])
        self.assertEqual(telegram.answers[0]["text"], "Approved ✅")

    async def test_replacement_card_drops_the_decision_buttons(self):
        bot, telegram, _ = build_bot(seerr_public_url="http://192.168.1.10:5055")
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})

        buttons = [
            b
            for row in telegram.sent[-1]["markup"]["inline_keyboard"]
            for b in row
        ]
        self.assertTrue(all("callback_data" not in b for b in buttons))

    async def test_replacement_is_tracked_for_later_updates(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})

        tracked = bot.store.get("1")
        self.assertEqual(tracked.message_id, telegram.sent[-1]["message_id"])

    async def test_deny_calls_seerr_with_decline(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("decline:1")})

        self.assertEqual(seerr.calls, [("1", "decline")])
        self.assertIn("Denied by <b>@dan</b>", telegram.sent[-1]["text"])

    async def test_replacement_keeps_the_poster(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        self.assertTrue(telegram.sent[0]["photo"])

        await bot.handle_update({"callback_query": callback("approve:1", photo=True)})
        self.assertTrue(telegram.sent[-1]["photo"])

    async def test_already_resolved_request_is_not_re_decided(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        seerr.records["1"]["status"] = 2  # approved in the web UI meanwhile

        await bot.handle_update({"callback_query": callback("approve:1")})

        self.assertEqual(seerr.calls, [])
        self.assertIn("Already approved", telegram.answers[0]["text"])
        self.assertTrue(telegram.answers[0]["alert"])
        self.assertIn("Approved", telegram.sent[-1]["text"])

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

    async def _decide_while_seerr_echoes(self, decision: str, echo_type: str):
        """Seerr fires its webhook the instant the status changes.

        The echo lands on the HTTP server while the button press is still
        being handled, which is exactly when a second card used to appear.
        """
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        echoes: list[asyncio.Task] = []
        original = seerr.set_request_status

        async def set_and_echo(request_id, dec):
            result = await original(request_id, dec)
            echoes.append(
                asyncio.create_task(
                    bot.handle_webhook(
                        {
                            "notification_type": echo_type,
                            "request": {"request_id": str(request_id)},
                        }
                    )
                )
            )
            await asyncio.sleep(0)  # let the echo reach the lock and wait
            return result

        seerr.set_request_status = set_and_echo
        await bot.handle_update({"callback_query": callback(f"{decision}:1")})
        await asyncio.gather(*echoes)
        return telegram

    async def test_seerr_echo_during_approval_does_not_duplicate(self):
        telegram = await self._decide_while_seerr_echoes("approve", "MEDIA_APPROVED")

        resolved = [m for m in telegram.sent if "Approved" in m["text"]]
        self.assertEqual(len(resolved), 1, "the echo must not post a second card")
        self.assertIn("Approved by <b>@dan</b>", resolved[0]["text"])
        self.assertNotIn("web UI", resolved[0]["text"])

    async def test_seerr_echo_during_denial_does_not_duplicate(self):
        telegram = await self._decide_while_seerr_echoes("decline", "MEDIA_DECLINED")

        resolved = [m for m in telegram.sent if "Denied" in m["text"]]
        self.assertEqual(len(resolved), 1)
        self.assertIn("Denied by <b>@dan</b>", resolved[0]["text"])

    async def test_a_failed_call_releases_the_claim(self):
        """A decision that never happened must not swallow a later web-UI one."""
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        seerr.fail_with = SeerrError("Seerr rejected the API key (403).", 403)

        await bot.handle_update({"callback_query": callback("approve:1")})
        seerr.fail_with = None

        await bot.handle_webhook(
            {"notification_type": "MEDIA_APPROVED", "request": {"request_id": "1"}}
        )

        self.assertIn("Approved by <b>the Seerr web UI</b>", telegram.sent[-1]["text"])

    async def test_a_vanished_message_is_not_replaced_twice(self):
        """If the card is already gone, editing fails too - post nothing new."""
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        before = len(telegram.sent)

        async def gone(*_args, **_kwargs):
            raise TelegramError("message to delete not found")

        telegram.delete_message = gone
        telegram.edit_text = gone

        await bot.handle_update({"callback_query": callback("approve:1")})

        self.assertEqual(len(telegram.sent), before, "no duplicate card")

    async def test_a_web_ui_decision_still_shows_when_nothing_was_claimed(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        await bot.handle_webhook(
            {"notification_type": "MEDIA_APPROVED", "request": {"request_id": "1"}}
        )

        self.assertIn("Approved by <b>the Seerr web UI</b>", telegram.sent[-1]["text"])

    async def test_a_stale_claim_cannot_swallow_the_opposite_decision(self):
        """Approve from chat, then decline in the web UI: the card must follow.

        If Seerr is not configured to send Approved notifications, the claim
        from the button press is never consumed, and it must not be mistaken
        for the later decline.
        """
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})

        await bot.handle_webhook(
            {"notification_type": "MEDIA_DECLINED", "request": {"request_id": "1"}}
        )

        self.assertIn("Denied by <b>the Seerr web UI</b>", telegram.sent[-1]["text"])

    async def test_a_repeated_notification_keeps_the_original_credit(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})

        for _ in range(3):  # the echo, then two replays
            await bot.handle_webhook(
                {"notification_type": "MEDIA_APPROVED", "request": {"request_id": "1"}}
            )

        cards = [m for m in telegram.sent if "Approved" in m["text"]]
        self.assertEqual(len(cards), 1)
        self.assertIn("@dan", cards[0]["text"])

    async def test_a_late_approval_echo_cannot_undo_available(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})
        await bot.handle_webhook(
            {"notification_type": "MEDIA_AVAILABLE", "request": {"request_id": "1"}}
        )

        await bot.handle_webhook(
            {"notification_type": "MEDIA_APPROVED", "request": {"request_id": "1"}}
        )

        self.assertTrue(telegram.sent[-1]["text"].endswith("▶️ Available in Plex"))

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
        self.assertIn("Approved", telegram.sent[-1]["text"])

    async def test_external_decision_updates_the_pending_message(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        await bot.handle_webhook(
            {"notification_type": "MEDIA_DECLINED", "request": {"request_id": "1"}}
        )

        self.assertIn("Denied by <b>the Seerr web UI</b>", telegram.sent[-1]["text"])
        self.assertEqual(len(telegram.deleted), 1)

    async def test_echo_of_our_own_decision_is_not_re_applied(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})
        sent_after_button = len(telegram.sent)

        await bot.handle_webhook(
            {"notification_type": "MEDIA_APPROVED", "request": {"request_id": "1"}}
        )

        self.assertEqual(len(telegram.sent), sent_after_button)


class TestProgressStatus(unittest.IsolatedAsyncioTestCase):
    """A card tracks the request onward: approved, then downloaded."""

    async def _approve(self):
        bot, telegram, seerr = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})
        return bot, telegram, seerr

    async def test_approval_says_it_is_waiting(self):
        _, telegram, _ = await self._approve()
        card = telegram.sent[-1]["text"]

        self.assertTrue(card.endswith("⏳ Waiting for download"))
        self.assertIn("Approved by <b>@dan</b>\n\n⏳", card)

    async def test_denial_has_nothing_to_wait_for(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("decline:1")})

        self.assertNotIn("Waiting for download", telegram.sent[-1]["text"])

    async def test_availability_promotes_the_card(self):
        bot, telegram, _ = await self._approve()
        before = len(telegram.sent)

        await bot.handle_webhook(
            {
                "notification_type": "MEDIA_AVAILABLE",
                "request": {"request_id": "1"},
            }
        )

        self.assertEqual(len(telegram.sent), before + 1, "should re-send, not edit")
        self.assertEqual(telegram.edits, [])
        card = telegram.sent[-1]["text"]
        self.assertTrue(card.endswith("▶️ Available in Plex"))
        self.assertNotIn("Waiting for download", card)
        self.assertIn("Approved by <b>@dan</b>", card, "the decider is preserved")

    async def test_failure_replaces_the_waiting_line(self):
        bot, telegram, _ = await self._approve()
        before = len(telegram.sent)

        await bot.handle_webhook(
            {"notification_type": "MEDIA_FAILED", "request": {"request_id": "1"}}
        )

        self.assertEqual(len(telegram.sent), before + 1, "should re-send, not edit")
        card = telegram.sent[-1]["text"]
        self.assertTrue(card.endswith("⚠️ Failed to process"))
        self.assertNotIn("Waiting for download", card)
        self.assertIn("Approved by <b>@dan</b>", card)

    async def test_a_failure_can_still_recover_to_available(self):
        bot, telegram, _ = await self._approve()
        for kind in ("MEDIA_FAILED", "MEDIA_AVAILABLE"):
            await bot.handle_webhook(
                {"notification_type": kind, "request": {"request_id": "1"}}
            )

        self.assertTrue(telegram.sent[-1]["text"].endswith("▶️ Available in Plex"))

    async def test_failure_for_an_untracked_request_is_ignored(self):
        bot, telegram, _ = build_bot()
        status, _ = await bot.handle_webhook(
            {"notification_type": "MEDIA_FAILED", "request": {"request_id": "77"}}
        )

        self.assertEqual(status, 200)
        self.assertEqual(telegram.sent, [])

    async def test_failure_does_not_touch_a_denied_card(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("decline:1")})
        after_denial = len(telegram.sent)

        await bot.handle_webhook(
            {"notification_type": "MEDIA_FAILED", "request": {"request_id": "1"}}
        )

        self.assertEqual(len(telegram.sent), after_denial)

    async def test_an_auto_approved_card_can_fail_too(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(
            pending_payload(REQUESTS["1"])
            | {"notification_type": "MEDIA_AUTO_APPROVED"}
        )

        await bot.handle_webhook(
            {"notification_type": "MEDIA_FAILED", "request": {"request_id": "1"}}
        )

        card = telegram.sent[-1]["text"]
        self.assertTrue(card.endswith("⚠️ Failed to process"))
        self.assertIn("Approved automatically", card)

    async def test_availability_for_an_untracked_request_is_ignored(self):
        bot, telegram, _ = build_bot()
        status, _ = await bot.handle_webhook(
            {"notification_type": "MEDIA_AVAILABLE", "request": {"request_id": "77"}}
        )

        self.assertEqual(status, 200)
        self.assertEqual(telegram.sent, [])

    async def test_availability_does_not_resurrect_a_denied_card(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("decline:1")})
        after_denial = len(telegram.sent)

        await bot.handle_webhook(
            {"notification_type": "MEDIA_AVAILABLE", "request": {"request_id": "1"}}
        )

        self.assertEqual(len(telegram.sent), after_denial)

    async def test_web_ui_approval_also_starts_waiting(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))

        await bot.handle_webhook(
            {"notification_type": "MEDIA_APPROVED", "request": {"request_id": "1"}}
        )

        self.assertTrue(telegram.sent[-1]["text"].endswith("⏳ Waiting for download"))


class TestAutoApproval(unittest.IsolatedAsyncioTestCase):
    """Seerr can approve without asking; the card says so and tracks it."""

    def _payload(self, request_id: str = "1") -> dict[str, Any]:
        return pending_payload(REQUESTS[request_id]) | {
            "notification_type": "MEDIA_AUTO_APPROVED",
            "event": "Movie Request Automatically Approved",
        }

    async def test_a_card_is_posted_with_no_decider(self):
        bot, telegram, _ = build_bot()
        status, _ = await bot.handle_webhook(self._payload())

        self.assertEqual(status, 200)
        card = telegram.sent[0]["text"]
        self.assertIn("✅ Approved automatically", card)
        self.assertNotIn("Approved by", card)
        self.assertTrue(card.endswith("⏳ Waiting for download"))

    async def test_the_card_carries_no_decision_buttons(self):
        bot, telegram, _ = build_bot(seerr_public_url="http://192.168.1.10:5055")
        await bot.handle_webhook(self._payload())

        buttons = [
            b for row in telegram.sent[0]["markup"]["inline_keyboard"] for b in row
        ]
        self.assertTrue(all("callback_data" not in b for b in buttons))

    async def test_it_becomes_available_like_any_other(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(self._payload())

        await bot.handle_webhook(
            {"notification_type": "MEDIA_AVAILABLE", "request": {"request_id": "1"}}
        )

        card = telegram.sent[-1]["text"]
        self.assertTrue(card.endswith("▶️ Available in Plex"))
        self.assertIn("Approved automatically", card)

    async def test_an_existing_card_is_replaced_not_duplicated(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        original = telegram.sent[0]

        await bot.handle_webhook(self._payload())

        self.assertEqual(telegram.deleted, [original["message_id"]])
        self.assertEqual(len(telegram.sent), 2)
        self.assertIn("Approved automatically", telegram.sent[-1]["text"])


class TestStatePersistence(unittest.TestCase):
    """The request-to-message link has to survive a restart."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "state.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _sample(self) -> SentMessage:
        return SentMessage(
            chat_id=-100500,
            message_id=777,
            is_photo=True,
            notification=RequestNotification(pending_payload(REQUESTS["3"])),
            decision="approve",
            actor="@dan",
        )

    def test_a_reopened_store_returns_what_was_written(self):
        MessageStore(self.path).remember("3", self._sample())

        restored = MessageStore(self.path).get("3")

        self.assertIsNotNone(restored)
        self.assertEqual(restored.chat_id, -100500)
        self.assertEqual(restored.message_id, 777)
        self.assertTrue(restored.is_photo)
        self.assertEqual(restored.decision, "approve")
        self.assertEqual(restored.actor, "@dan")
        self.assertIn("Breaking Bad", restored.notification.subject)
        self.assertIn("Requested Seasons", restored.notification.pending_text(1024))

    def test_an_automatic_approval_survives_with_no_actor(self):
        sent = self._sample()
        sent.actor = None
        MessageStore(self.path).remember("3", sent)

        restored = MessageStore(self.path).get("3")
        self.assertIsNone(restored.actor)
        self.assertIn(
            "Approved automatically",
            restored.notification.resolved_text("approve", None, 1024),
        )

    def test_echo_suppression_survives_too(self):
        MessageStore(self.path).mark_decided("3", "approve")

        store = MessageStore(self.path)
        self.assertEqual(store.pop_decided("3"), "approve")
        self.assertIsNone(MessageStore(self.path).pop_decided("3"))

    def test_the_oldest_entries_are_dropped(self):
        store = MessageStore(self.path, limit=3)
        for n in range(5):
            store.remember(str(n), self._sample())

        reopened = MessageStore(self.path, limit=3)
        self.assertIsNone(reopened.get("0"))
        self.assertIsNotNone(reopened.get("4"))

    def test_corrupt_state_is_ignored_rather_than_fatal(self):
        self.path.write_text("{ this is not json")

        store = MessageStore(self.path)

        self.assertIsNone(store.get("3"))
        store.remember("3", self._sample())  # and it recovers
        self.assertIsNotNone(MessageStore(self.path).get("3"))

    def test_an_unwritable_path_degrades_to_memory(self):
        store = MessageStore(Path(self.dir) / "nope" / "x" / "state.json")
        blocked = Path(self.dir) / "nope"
        blocked.write_text("I am a file, not a directory")

        store.remember("3", self._sample())  # must not raise

        self.assertIsNotNone(store.get("3"))

    def test_no_path_means_no_file(self):
        store = MessageStore(None)
        store.remember("3", self._sample())

        self.assertIsNotNone(store.get("3"))
        self.assertEqual(list(Path(self.dir).iterdir()), [])


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
        self.assertIn("configured admin", telegram.sent[0]["text"])

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
        self.assertIn("configured admin", telegram.sent[0]["text"])

    async def test_start_in_a_group_offers_the_id_for_delivery(self):
        bot, telegram, _ = build_bot()
        message = self._message("/start", chat_id=-100500)
        message["message"]["chat"]["type"] = "supergroup"

        await bot.handle_update(message)

        body = telegram.sent[0]["text"]
        self.assertIn("<code>-100500</code>", body)
        self.assertIn("GROUP_CHAT_ID", body)
        self.assertIn("ADMIN_CHAT_ID", body)

    async def test_a_group_admin_chat_id_is_treated_as_unconfigured(self):
        config = make_config(admin_chat_id=None, rejected_group_chat_id=-100500)
        bot = SeerrTelegramBot(config, FakeTelegram(), FakeSeerr())

        self.assertFalse(bot._is_admin(-100500, -100500))
        self.assertFalse(bot._is_admin(-100500, 42))
        status, _ = await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        self.assertEqual(status, 503)

    async def test_synopsis_reports_its_current_state(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/synopsis"))

        self.assertIn("Synopsis is <b>on</b>", telegram.sent[0]["text"])

    async def test_synopsis_off_hides_it_on_new_cards(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/synopsis off"))
        await bot.handle_webhook(pending_payload(REQUESTS["3"]))

        card = telegram.sent[-1]["text"]
        self.assertNotIn("Walter White", card)
        self.assertIn("Breaking Bad", card)
        self.assertIn("Requested Seasons", card)

    async def test_synopsis_on_restores_it(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/synopsis off"))
        await bot.handle_update(self._message("/synopsis ON"))
        await bot.handle_webhook(pending_payload(REQUESTS["3"]))

        self.assertIn("Walter White", telegram.sent[-1]["text"])

    async def test_the_setting_also_applies_to_resolved_cards(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["3"]))
        await bot.handle_update(self._message("/synopsis off"))
        await bot.handle_update({"callback_query": callback("approve:3")})

        card = telegram.sent[-1]["text"]
        self.assertNotIn("Walter White", card)
        self.assertIn("Approved by <b>@dan</b>", card)

    async def test_existing_cards_are_redrawn_in_place(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["3"]))
        card = telegram.sent[-1]
        sent_before = len(telegram.sent)

        await bot.handle_update(self._message("/synopsis off"))

        redraw = telegram.edits[0]
        self.assertEqual(redraw["message_id"], card["message_id"])
        self.assertNotIn("Walter White", redraw["text"])
        self.assertIn("Requested Seasons", redraw["text"])
        self.assertEqual(
            len(telegram.sent), sent_before + 1, "only the confirmation is posted"
        )

    async def test_redrawing_keeps_the_decision_buttons(self):
        bot, telegram, _ = build_bot(seerr_public_url="http://192.168.1.10:5055")
        await bot.handle_webhook(pending_payload(REQUESTS["3"]))

        await bot.handle_update(self._message("/synopsis off"))

        rows = telegram.edits[0]["markup"]["inline_keyboard"]
        self.assertEqual(
            [b["callback_data"] for b in rows[0]], ["approve:3", "decline:3"]
        )

    async def test_redrawing_preserves_each_card_state(self):
        """A redraw must not reset an available card to waiting."""
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["1"]))
        await bot.handle_update({"callback_query": callback("approve:1")})
        await bot.handle_webhook(
            {"notification_type": "MEDIA_AVAILABLE", "request": {"request_id": "1"}}
        )

        await bot.handle_update(self._message("/synopsis off"))

        redraw = telegram.edits[-1]
        self.assertIn("Approved by <b>@dan</b>", redraw["text"])
        self.assertTrue(redraw["text"].endswith("▶️ Available in Plex"))

    async def test_a_redraw_that_fails_does_not_stop_the_rest(self):
        bot, telegram, _ = build_bot()
        for request_id in ("1", "2", "3"):
            await bot.handle_webhook(pending_payload(REQUESTS[request_id]))

        calls = {"n": 0}
        real_edit = telegram.edit_text

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TelegramError("message to edit not found")
            return await real_edit(*args, **kwargs)

        telegram.edit_text = flaky
        await bot.handle_update(self._message("/synopsis off"))

        self.assertEqual(len(telegram.edits), 2, "the other two still redrew")
        self.assertIn("Synopsis is now <b>hidden</b>", telegram.sent[-1]["text"])

    async def test_setting_it_to_what_it_already_is_redraws_nothing(self):
        bot, telegram, _ = build_bot()
        await bot.handle_webhook(pending_payload(REQUESTS["3"]))

        await bot.handle_update(self._message("/synopsis on"))

        self.assertEqual(telegram.edits, [])
        self.assertIn("already <b>on</b>", telegram.sent[-1]["text"])

    async def test_a_bad_argument_explains_itself(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/synopsis maybe"))

        self.assertIn("/synopsis on", telegram.sent[0]["text"])
        self.assertTrue(bot._with_synopsis, "an unclear argument changes nothing")

    async def test_non_admins_cannot_change_it(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("/synopsis off", chat_id=999))

        self.assertTrue(bot._with_synopsis)
        self.assertIn("configured admin", telegram.sent[0]["text"])

    async def test_non_command_text_is_ignored(self):
        bot, telegram, _ = build_bot()
        await bot.handle_update(self._message("hello there"))
        self.assertEqual(telegram.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
