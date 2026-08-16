"""Bot logic: Seerr webhooks in, Telegram messages out, decisions back to Seerr."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Callable

from .config import Config
from .formatting import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    STATUS_AVAILABLE,
    STATUS_FAILED,
    STATUS_WAITING,
    RequestNotification,
    actor_name,
    esc,
)
from .seerr import (
    STATUS_APPROVED,
    STATUS_NAMES,
    STATUS_PENDING,
    SeerrClient,
    SeerrError,
)
from .state import MessageStore, SentMessage
from .telegram import TelegramClient, TelegramError, is_valid_button_url, keyboard

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "<b>Seerr approvals</b>\n\n"
    "/start — show this chat's Telegram ID (use it for ADMIN_CHAT_ID)\n"
    "/test — check the connection to Seerr without creating anything\n"
    "/status — pending and approved request counts\n"
    "/pending — list open requests with Approve / Deny buttons\n"
    "/help — this message"
)


def _status_for(decision: str) -> str | None:
    """An approved request goes on to download; a denied one goes nowhere."""
    return STATUS_WAITING if decision == "approve" else None


def _callback_rows_only(markup: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep only rows whose buttons carry callback data, dropping URL rows."""
    if not markup:
        return None
    rows = [
        row
        for row in markup["inline_keyboard"]
        if all("callback_data" in button for button in row)
    ]
    return keyboard(rows) if rows else None


class SeerrTelegramBot:
    def __init__(
        self, config: Config, telegram: TelegramClient, seerr: SeerrClient
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.seerr = seerr
        self.store = MessageStore(config.state_file)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ utils

    def _action_keyboard(self, notification: RequestNotification) -> dict[str, Any]:
        rows: list[list[dict[str, Any]]] = [
            [
                {
                    "text": "Approve",
                    "callback_data": f"approve:{notification.request_id}",
                },
                {
                    "text": "Deny",
                    "callback_data": f"decline:{notification.request_id}",
                },
            ]
        ]
        link = self._link_row(notification)
        if link:
            rows.append(link)
        return keyboard(rows)

    def _link_row(
        self, notification: RequestNotification
    ) -> list[dict[str, Any]] | None:
        link = notification.media_url(self.config.seerr_public_url)
        if not is_valid_button_url(link):
            return None
        return [{"text": "Open in Seerr", "url": link}]

    def _link_only_keyboard(
        self, notification: RequestNotification
    ) -> dict[str, Any] | None:
        row = self._link_row(notification)
        return keyboard([row]) if row else None

    # --------------------------------------------------------------- webhooks

    async def handle_webhook(self, payload: dict[str, Any]) -> tuple[int, str]:
        """Return an (http_status, message) pair for the Seerr-facing response."""
        notification = RequestNotification(payload)
        kind = notification.notification_type
        logger.info(
            "Webhook received: type=%s request=%s subject=%r",
            kind or "<none>",
            notification.request_id,
            notification.subject,
        )

        if kind == "TEST_NOTIFICATION":
            return await self._handle_test_notification()

        if self.config.admin_chat_id is None:
            logger.warning(
                "Dropping %s notification: ADMIN_CHAT_ID is not set. "
                "Send /start to the bot and set it.",
                kind,
            )
            return 503, "ADMIN_CHAT_ID is not configured on the bot"

        if notification.is_pending_request:
            await self.send_approval_request(
                self.config.target_chat_id, notification
            )
            return 200, "ok"

        if kind == "MEDIA_AUTO_APPROVED":
            await self._announce_auto_approved(notification)
            return 200, "ok"

        if kind in {"MEDIA_APPROVED", "MEDIA_DECLINED"}:
            await self._reflect_external_decision(notification)
            return 200, "ok"

        if kind == "MEDIA_AVAILABLE":
            await self._update_status(notification, STATUS_AVAILABLE)
            return 200, "ok"

        if kind == "MEDIA_FAILED":
            await self._update_status(notification, STATUS_FAILED)
            return 200, "ok"

        if self.config.forward_other_notifications:
            text = notification.pending_text(MESSAGE_LIMIT)
            header = f"<b>{esc(notification.event or kind)}</b>\n"
            await self.telegram.send_message(
                self.config.target_chat_id, header + text, disable_preview=True
            )
            return 200, "ok"

        logger.debug("Ignoring notification type %s", kind)
        return 200, "ignored"

    async def _handle_test_notification(self) -> tuple[int, str]:
        if self.config.admin_chat_id is None:
            logger.warning("Webhook test arrived but ADMIN_CHAT_ID is not set")
            return 503, (
                "Bot reached, but ADMIN_CHAT_ID is not configured. "
                "Send /start to the bot, then set ADMIN_CHAT_ID and restart."
            )
        await self.telegram.send_message(
            self.config.target_chat_id,
            "✅ <b>Webhook test received</b>\n"
            "Seerr can reach this bot. Pending requests will arrive here.",
        )
        return 200, "ok"

    async def _send_card(
        self,
        chat_id: int,
        notification: RequestNotification,
        render: Callable[[int], str],
        markup: dict[str, Any] | None,
    ) -> SentMessage:
        """Post a card as a poster where possible, as text otherwise."""
        if notification.image:
            try:
                message = await self.telegram.send_photo(
                    chat_id, notification.image, render(CAPTION_LIMIT), markup
                )
                return SentMessage(chat_id, message["message_id"], True, notification)
            except TelegramError as exc:
                logger.warning("Poster send failed (%s); falling back to text", exc)

        try:
            message = await self.telegram.send_message(
                chat_id, render(MESSAGE_LIMIT), markup
            )
        except TelegramError as exc:
            # Losing the card entirely would leave the request invisible, so
            # drop everything Telegram might object to and keep the buttons,
            # whose callback data is always valid.
            logger.warning("Card rejected (%s); retrying without links", exc)
            message = await self.telegram.send_message(
                chat_id, render(MESSAGE_LIMIT), _callback_rows_only(markup)
            )
        return SentMessage(chat_id, message["message_id"], False, notification)

    async def send_approval_request(
        self, chat_id: int, notification: RequestNotification
    ) -> None:
        sent = await self._send_card(
            chat_id,
            notification,
            notification.pending_text,
            self._action_keyboard(notification),
        )
        if notification.request_id:
            self.store.remember(str(notification.request_id), sent)

    async def _reflect_external_decision(
        self, notification: RequestNotification
    ) -> None:
        """Update a pending message that was resolved in the Seerr web UI."""
        request_id = str(notification.request_id or "")
        sent = self.store.get(request_id)
        if not sent:
            return
        if self.store.pop_decided(request_id):
            return  # this webhook is an echo of our own button press

        decision = (
            "approve" if notification.notification_type != "MEDIA_DECLINED" else "decline"
        )
        await self._finalize_message(
            sent, decision, "the Seerr web UI", _status_for(decision)
        )

    async def _announce_auto_approved(
        self, notification: RequestNotification
    ) -> None:
        """Post a card for a request Seerr approved without asking.

        There is nothing to decide, so the card carries no buttons; it exists
        to say what happened and to track the download that follows.
        """
        request_id = str(notification.request_id or "")
        existing = self.store.get(request_id) if request_id else None
        if existing is not None:
            await self._finalize_message(existing, "approve", None, STATUS_WAITING)
            return

        sent = await self._send_card(
            self.config.target_chat_id,
            notification,
            lambda limit: notification.resolved_text(
                "approve", None, limit, STATUS_WAITING
            ),
            self._link_only_keyboard(notification),
        )
        sent.decision = "approve"
        sent.actor = None
        if request_id:
            self.store.remember(request_id, sent)

    async def _update_status(
        self, notification: RequestNotification, status: str
    ) -> None:
        """Move an approved card to its next state, keeping who decided it.

        Only approved requests have a journey to report; a denied one is
        finished, and a pending one has not started.
        """
        sent = self.store.get(str(notification.request_id or ""))
        if sent is None:
            logger.debug(
                "No card tracked for request %s; nothing to update",
                notification.request_id,
            )
            return
        if sent.decision != "approve":
            return

        await self._finalize_message(sent, sent.decision, sent.actor, status)

    async def _finalize_message(
        self,
        sent: SentMessage,
        decision: str,
        actor: str | None,
        status: str | None = None,
    ) -> None:
        """Replace the card rather than editing it, so the outcome pings.

        An edit updates the message silently, which is easy to miss when the
        card is one of many in a group.
        """
        def render(limit: int) -> str:
            return sent.notification.resolved_text(decision, actor, limit, status)

        try:
            await self.telegram.delete_message(sent.chat_id, sent.message_id)
        except TelegramError as exc:
            # Telegram refuses to delete messages older than 48 hours. Strip
            # the buttons instead so the stale card cannot be tapped again.
            logger.warning("Could not delete message %s: %s", sent.message_id, exc)
            with contextlib.suppress(TelegramError):
                await self.telegram.edit_text(
                    sent.chat_id,
                    sent.message_id,
                    render(CAPTION_LIMIT if sent.is_photo else MESSAGE_LIMIT),
                    is_caption=sent.is_photo,
                    reply_markup=None,
                )
                return

        replacement = await self._send_card(
            sent.chat_id,
            sent.notification,
            render,
            self._link_only_keyboard(sent.notification),
        )
        replacement.decision = decision
        replacement.actor = actor
        request_id = sent.notification.request_id
        if request_id:
            self.store.remember(str(request_id), replacement)

    # ---------------------------------------------------------------- updates

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
        elif "message" in update:
            await self._handle_message(update["message"])

    def _is_admin(self, chat_id: Any, user_id: Any) -> bool:
        """The admin, in a chat this bot is meant to be used from.

        Authority comes from the user ID alone; a group's chat ID is shared by
        everyone in it and proves nothing. The chat is checked only so the bot
        stays inert in rooms it was not configured for.
        """
        admin = self.config.admin_chat_id
        if admin is None or user_id != admin:
            return False
        return chat_id in (self.config.target_chat_id, admin)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user_id = (message.get("from") or {}).get("id")
        if chat_id is None or not text.startswith("/"):
            return

        # Commands may be typed with the bot's handle, as /start@MySeerrBot.
        command = text.split()[0].lstrip("/").split("@")[0].lower()

        if command in {"start", "id"}:
            await self._cmd_start(chat_id, chat)
            return
        if command == "help":
            await self.telegram.send_message(chat_id, HELP_TEXT)
            return

        # Until ADMIN_CHAT_ID is set there is nobody to authorize against, so
        # every remaining command stays shut. They would otherwise expose the
        # Seerr address, version, and library contents to any passer-by who
        # found the bot.
        if self.config.admin_chat_id is None:
            await self.telegram.send_message(
                chat_id,
                "Set <code>ADMIN_CHAT_ID</code> and restart the bot before using "
                "this command. Check <code>docker logs</code> for the Seerr "
                "connection status in the meantime.",
            )
            return

        if not self._is_admin(chat_id, user_id):
            logger.info(
                "Ignoring /%s from chat %s / user %s", command, chat_id, user_id
            )
            await self.telegram.send_message(
                chat_id,
                "This bot only takes commands from its configured admin.",
            )
            return

        if command == "test":
            await self._cmd_test(chat_id)
        elif command == "status":
            await self._cmd_status(chat_id)
        elif command == "pending":
            await self._cmd_pending(chat_id)
        else:
            await self.telegram.send_message(chat_id, HELP_TEXT)

    async def _cmd_start(self, chat_id: int, chat: dict[str, Any]) -> None:
        if (chat.get("type") or "private") != "private":
            await self.telegram.send_message(
                chat_id,
                f"This chat's ID: <code>{chat_id}</code>\n\n"
                "Put it in <code>GROUP_CHAT_ID</code> to have request cards "
                "delivered here. Approvals stay restricted to the single user "
                "in <code>ADMIN_CHAT_ID</code>, which you get by sending "
                "/start to me privately.",
            )
            return

        lines = [
            "👋 <b>Seerr approval bot</b>",
            "",
            f"Your chat ID: <code>{chat_id}</code>",
            "",
        ]

        if self.config.admin_chat_id is None:
            lines += [
                "Set <code>ADMIN_CHAT_ID</code> to the chat ID above, then "
                "restart the container. Approval requests will then arrive here.",
            ]
        elif chat_id == self.config.admin_chat_id:
            where = (
                f"chat <code>{self.config.group_chat_id}</code>"
                if self.config.group_chat_id is not None
                else "here"
            )
            lines += [
                f"✅ You are the configured admin, and only you can approve. "
                f"Request cards are delivered to {where}.",
                "",
                "Use /test to verify the Seerr connection.",
            ]
        else:
            lines += [
                "ℹ️ Approvals go to a different user, so the buttons here would "
                "not work. The ID above is what you would put in Seerr's user "
                "notification settings.",
            ]
        await self.telegram.send_message(chat_id, "\n".join(lines))

    async def _cmd_test(self, chat_id: int) -> None:
        ok, report = await self.connection_report()
        await self.telegram.send_message(chat_id, report)
        logger.info("Connection test requested from chat %s: ok=%s", chat_id, ok)

    async def connection_report(self) -> tuple[bool, str]:
        """Read-only probe of Seerr. Creates nothing and changes nothing."""
        lines = ["<b>Seerr connection test</b>", ""]
        healthy = True

        try:
            status = await self.seerr.status()
            version = status.get("version", "unknown")
            lines.append(f"✅ Reachable at <code>{esc(self.config.seerr_url)}</code>")
            lines.append(f"   version <b>{esc(version)}</b>")
        except SeerrError as exc:
            return False, "\n".join(
                lines + [f"❌ {esc(exc)}", "", "Check SEERR_URL and that Seerr is up."]
            )

        try:
            counts = await self.seerr.request_counts()
            lines.append("✅ API key accepted (request counts readable)")
            lines.append(
                f"   pending <b>{counts.get('pending', 0)}</b>, "
                f"approved <b>{counts.get('approved', 0)}</b>, "
                f"total <b>{counts.get('total', 0)}</b>"
            )
        except SeerrError as exc:
            healthy = False
            lines.append(f"❌ {esc(exc)}")

        lines.append("")
        if self.config.admin_chat_id is None:
            lines.append("⚠️ ADMIN_CHAT_ID is not set — webhooks will be dropped.")
            healthy = False
        else:
            lines.append(
                f"✅ Cards go to <code>{self.config.target_chat_id}</code>, "
                f"approvable by <code>{self.config.admin_chat_id}</code>"
            )
        lines.append(
            "ℹ️ To test the other direction, press <b>Test</b> on Seerr's "
            "webhook notification settings page."
        )
        return healthy, "\n".join(lines)

    async def _cmd_status(self, chat_id: int) -> None:
        try:
            counts = await self.seerr.request_counts()
        except SeerrError as exc:
            await self.telegram.send_message(chat_id, f"❌ {esc(exc)}")
            return
        await self.telegram.send_message(
            chat_id,
            "<b>Seerr requests</b>\n"
            f"⏳ Pending: <b>{counts.get('pending', 0)}</b>\n"
            f"✅ Approved: <b>{counts.get('approved', 0)}</b>\n"
            f"🚫 Declined: <b>{counts.get('declined', 0)}</b>\n"
            f"📦 Total: <b>{counts.get('total', 0)}</b>",
        )

    async def _cmd_pending(self, chat_id: int) -> None:
        try:
            requests = await self.seerr.pending_requests(take=10)
        except SeerrError as exc:
            await self.telegram.send_message(chat_id, f"❌ {esc(exc)}")
            return

        if not requests:
            await self.telegram.send_message(chat_id, "✅ No pending requests.")
            return

        await self.telegram.send_message(
            chat_id, f"⏳ <b>{len(requests)}</b> pending request(s):"
        )
        for request in requests:
            notification = await self._notification_from_request(request)
            await self.send_approval_request(chat_id, notification)

    async def _notification_from_request(
        self, request: dict[str, Any]
    ) -> RequestNotification:
        """Build the webhook-shaped payload the renderer expects from API data."""
        media = request.get("media") or {}
        media_type = (media.get("mediaType") or "movie").lower()
        tmdb_id = media.get("tmdbId")
        details = await self.seerr.media_details(media_type, tmdb_id)

        requested_by = request.get("requestedBy") or {}
        extra: list[dict[str, str]] = []
        if request.get("is4k"):
            extra.append({"name": "Quality", "value": "4K"})
        seasons = [
            str(season.get("seasonNumber"))
            for season in (request.get("seasons") or [])
            if season.get("seasonNumber") is not None
        ]
        if seasons:
            extra.append({"name": "Requested Seasons", "value": ", ".join(seasons)})

        return RequestNotification(
            {
                "notification_type": "MEDIA_PENDING",
                "subject": details["title"] or f"TMDB ID {tmdb_id}",
                "message": "",
                "image": details["poster"],
                "media": {"media_type": media_type, "tmdbId": tmdb_id},
                "request": {
                    "request_id": str(request.get("id")),
                    "requestedBy_username": requested_by.get("displayName")
                    or requested_by.get("username")
                    or requested_by.get("email"),
                },
                "extra": extra,
            }
        )

    # -------------------------------------------------------------- callbacks

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")

        if ":" not in data:
            await self.telegram.answer_callback(callback_id)
            return

        decision, _, request_id = data.partition(":")
        if decision not in {"approve", "decline"} or not request_id:
            await self.telegram.answer_callback(callback_id)
            return

        if self.config.admin_chat_id is None:
            await self.telegram.answer_callback(
                callback_id,
                "ADMIN_CHAT_ID is not configured on the bot yet.",
                alert=True,
            )
            return

        user_id = (callback.get("from") or {}).get("id")
        if not self._is_admin(chat_id, user_id):
            logger.warning(
                "Rejected %s of request %s from chat %s / user %s",
                decision,
                request_id,
                chat_id,
                user_id,
            )
            await self.telegram.answer_callback(
                callback_id, "You are not authorized to decide this.", alert=True
            )
            return

        async with self._lock:
            await self._apply_decision(callback, decision, request_id)

    async def _apply_decision(
        self, callback: dict[str, Any], decision: str, request_id: str
    ) -> None:
        callback_id = callback["id"]
        message = callback["message"]
        chat_id = message["chat"]["id"]
        actor = actor_name(callback.get("from") or {})
        is_photo = "photo" in message

        sent = self.store.get(request_id)
        if sent is None:
            # Message predates a restart: rebuild enough context to edit it.
            sent = SentMessage(
                chat_id,
                message["message_id"],
                is_photo,
                RequestNotification({"subject": "", "request": {"request_id": request_id}}),
            )
            original = message.get("caption") or message.get("text") or ""
            sent.notification.subject = original.split("\n", 1)[0] or f"Request {request_id}"

        try:
            current = await self.seerr.get_request(request_id)
        except SeerrError as exc:
            logger.error("Could not load request %s: %s", request_id, exc)
            await self.telegram.answer_callback(callback_id, str(exc)[:190], alert=True)
            return

        status = current.get("status")
        if status != STATUS_PENDING:
            name = STATUS_NAMES.get(status, f"status {status}")
            await self.telegram.answer_callback(
                callback_id, f"Already {name} in Seerr.", alert=True
            )
            decided = "approve" if status == STATUS_APPROVED else "decline"
            await self._finalize_message(
                sent, decided, "the Seerr web UI", _status_for(decided)
            )
            return

        try:
            await self.seerr.set_request_status(request_id, decision)
        except SeerrError as exc:
            logger.error("Failed to %s request %s: %s", decision, request_id, exc)
            await self.telegram.answer_callback(callback_id, str(exc)[:190], alert=True)
            return

        self.store.mark_decided(request_id, decision)
        logger.info("Request %s %sd by %s", request_id, decision, actor)
        await self.telegram.answer_callback(
            callback_id, "Approved ✅" if decision == "approve" else "Denied 🚫"
        )
        await self._finalize_message(sent, decision, actor, _status_for(decision))

    # --------------------------------------------------------------- startup

    async def announce_start(self) -> None:
        if not self.config.notify_on_start or self.config.admin_chat_id is None:
            return
        try:
            await self.telegram.send_message(
                self.config.target_chat_id, "🔄 Seerr approval bot restarted."
            )
        except TelegramError as exc:
            logger.warning("Start-up notice failed: %s", exc)


__all__ = ["SeerrTelegramBot"]
