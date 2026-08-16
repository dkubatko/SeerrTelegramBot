"""Rendering of Telegram messages from Seerr webhook payloads."""

from __future__ import annotations

import re
from html import escape
from typing import Any

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

DECISIONS = {
    "approve": "✅ Approved",
    "decline": "🚫 Denied",
}

# Progress after approval, shown as the card's last paragraph.
STATUS_WAITING = "⏳ Waiting for download"
STATUS_AVAILABLE = "▶️ Available in Plex"

# Shortest description worth keeping; below this the line is just noise.
MIN_DESCRIPTION = 40


def esc(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=False)


def _truncate_escaped(text: str, limit: int) -> str:
    """Shorten already-escaped text without leaving a split HTML entity."""
    if len(text) <= limit:
        return text
    cut = re.sub(r"&[a-zA-Z]*$", "", text[: max(0, limit - 1)])
    return cut.rstrip() + "…"


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

    def pending_text(self, limit: int) -> str:
        return self._compose(limit)

    def resolved_text(
        self, decision: str, actor: str | None, limit: int, status: str | None = None
    ) -> str:
        """`actor` of None means Seerr approved it on its own."""
        verdict = DECISIONS.get(decision, "Updated")
        line = f"{verdict} by <b>{esc(actor)}</b>" if actor else f"{verdict} automatically"
        return self._compose(limit, line, status)

    def _compose(
        self, limit: int, decision: str | None = None, status: str | None = None
    ) -> str:
        """Title and description, then request details, then who is involved.

        Each of those is its own paragraph, and empty ones disappear rather
        than leaving a gap.
        """
        icon = "🎬" if self.media_type == "movie" else "📺"
        title = f"{icon} <b>{esc(self.subject)}</b>"

        details = "\n".join(
            f"{esc(name)}: <b>{esc(value)}</b>" for name, value in self.extra if value
        )

        people = []
        if self.has_requester:
            people.append(f"👤 Requested by <b>{esc(self.requested_by)}</b>")
        if decision:
            people.append(decision)

        paragraphs = [p for p in (details, "\n".join(people), status) if p]

        # Whatever the fixed parts do not use is available to the description.
        spent = len(title) + sum(len(p) + 2 for p in paragraphs)
        room = limit - spent - len("\n<i></i>")
        description = ""
        if self.overview and room >= MIN_DESCRIPTION:
            description = f"\n<i>{_truncate_escaped(esc(self.overview), room)}</i>"

        return "\n\n".join([title + description, *paragraphs])


def actor_name(user: dict[str, Any]) -> str:
    username = user.get("username")
    if username:
        return f"@{username}"
    name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    )
    return name or f"user {user.get('id')}"
