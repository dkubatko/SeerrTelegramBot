"""Rendering of Telegram messages from Seerr webhook payloads."""

from __future__ import annotations

from html import escape
from typing import Any

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

DECISION_HEADERS = {
    "approve": "✅ <b>Approved</b>",
    "decline": "🚫 <b>Denied</b>",
}


def esc(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=False)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


class RequestNotification:
    """The subset of a Seerr webhook payload this bot cares about."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.raw = payload
        self.notification_type = str(payload.get("notification_type") or "").upper()
        self.event = payload.get("event") or ""
        self.subject = payload.get("subject") or "Unknown title"
        self.overview = payload.get("message") or ""
        self.image = payload.get("image") or None

        media = payload.get("media") or {}
        self.media_type = (media.get("media_type") or "").lower()
        self.tmdb_id = media.get("tmdbId") or None

        request = payload.get("request") or {}
        self.request_id = request.get("request_id") or None
        requester = request.get("requestedBy_username") or request.get(
            "requestedBy_email"
        )
        self.requested_by = requester or "someone"
        self.has_requester = bool(requester)

        self.extra = [
            (item.get("name"), item.get("value"))
            for item in (payload.get("extra") or [])
            if isinstance(item, dict) and item.get("name")
        ]

    @property
    def is_pending_request(self) -> bool:
        return self.notification_type == "MEDIA_PENDING" and bool(self.request_id)

    def media_url(self, seerr_url: str) -> str | None:
        if not self.tmdb_id:
            return f"{seerr_url}/requests"
        kind = "movie" if self.media_type == "movie" else "tv"
        return f"{seerr_url}/{kind}/{self.tmdb_id}"

    def _detail_lines(self) -> list[str]:
        lines = []
        if self.has_requester:
            lines.append(f"👤 Requested by <b>{esc(self.requested_by)}</b>")
        for name, value in self.extra:
            if value:
                lines.append(f"• {esc(name)}: {esc(value)}")
        return lines

    def pending_text(self, limit: int) -> str:
        icon = "🎬" if self.media_type == "movie" else "📺"
        head = [f"{icon} <b>{esc(self.subject)}</b>", *self._detail_lines()]
        header = "\n".join(head)
        if not self.overview:
            return _truncate(header, limit)
        # Reserve room for the header and the blank separator line.
        room = limit - len(header) - 2
        if room < 80:
            return _truncate(header, limit)
        return f"{header}\n\n<i>{esc(_truncate(self.overview, room - 8))}</i>"

    def resolved_text(self, decision: str, actor: str, note: str | None = None) -> str:
        header = DECISION_HEADERS.get(decision, "<b>Updated</b>")
        icon = "🎬" if self.media_type == "movie" else "📺"
        lines = [
            f"{header} — {icon} <b>{esc(self.subject)}</b>",
            *self._detail_lines(),
            f"🔨 Decided by {esc(actor)}",
        ]
        if note:
            lines.append(esc(note))
        return "\n".join(lines)


def actor_name(user: dict[str, Any]) -> str:
    username = user.get("username")
    if username:
        return f"@{username}"
    name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    )
    return name or f"user {user.get('id')}"
