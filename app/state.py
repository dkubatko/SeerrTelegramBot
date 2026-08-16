"""Which Telegram message belongs to which Seerr request, kept across restarts.

Without this the link is lost on restart: buttons keep working, since the
request ID travels in their callback data, but a later webhook has no way to
find the card it should update.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .formatting import RequestNotification

logger = logging.getLogger(__name__)

TRACKED_LIMIT = 200


class SentMessage:
    """A card the bot posted, and the outcome it currently shows.

    `decision` and `actor` are kept so a later progress update can rebuild the
    same card without asking Seerr who decided it. An actor of None means the
    request was approved automatically, by nobody in particular.
    """

    __slots__ = (
        "chat_id",
        "message_id",
        "is_photo",
        "notification",
        "decision",
        "actor",
    )

    def __init__(
        self,
        chat_id: int,
        message_id: int,
        is_photo: bool,
        notification: RequestNotification,
        decision: str | None = None,
        actor: str | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.is_photo = is_photo
        self.notification = notification
        self.decision = decision
        self.actor = actor

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "is_photo": self.is_photo,
            "decision": self.decision,
            "actor": self.actor,
            "payload": self.notification.raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SentMessage":
        return cls(
            data["chat_id"],
            data["message_id"],
            bool(data.get("is_photo")),
            RequestNotification(data.get("payload") or {}),
            data.get("decision"),
            data.get("actor"),
        )


class MessageStore:
    """The request-to-message index, mirrored to a JSON file.

    A missing or unreadable file is not fatal: the bot starts with an empty
    index rather than refusing to run, since losing history is far cheaper
    than losing the ability to approve anything.
    """

    def __init__(self, path: Path | str | None, limit: int = TRACKED_LIMIT) -> None:
        self.path = Path(path) if path else None
        self.limit = limit
        self.messages: OrderedDict[str, SentMessage] = OrderedDict()
        self.decided: OrderedDict[str, str] = OrderedDict()
        self.preferences: dict[str, Any] = {}
        self._writable = True
        self._load()

    # --- reads -------------------------------------------------------------

    def get(self, request_id: str) -> SentMessage | None:
        return self.messages.get(request_id)

    # --- writes ------------------------------------------------------------

    def remember(self, request_id: str, sent: SentMessage) -> None:
        self.messages[request_id] = sent
        self.messages.move_to_end(request_id)
        while len(self.messages) > self.limit:
            self.messages.popitem(last=False)
        self.save()

    def mark_decided(self, request_id: str, decision: str) -> None:
        self.decided[request_id] = decision
        while len(self.decided) > self.limit:
            self.decided.popitem(last=False)
        self.save()

    def set_preference(self, key: str, value: Any) -> None:
        self.preferences[key] = value
        self.save()

    def pop_decided(self, request_id: str) -> str | None:
        decision = self.decided.pop(request_id, None)
        if decision is not None:
            self.save()
        return decision

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            for request_id, data in (raw.get("messages") or {}).items():
                self.messages[request_id] = SentMessage.from_dict(data)
            self.decided.update(raw.get("decided") or {})
            self.preferences.update(raw.get("preferences") or {})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "Ignoring unreadable state file %s (%s); starting empty",
                self.path,
                exc,
            )
            self.messages.clear()
            self.decided.clear()
            self.preferences.clear()
            return
        logger.info(
            "Restored %d tracked message(s) from %s", len(self.messages), self.path
        )

    def save(self) -> None:
        if not self.path or not self._writable:
            return
        payload = {
            "messages": {k: v.to_dict() for k, v in self.messages.items()},
            "decided": dict(self.decided),
            "preferences": self.preferences,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target and rename, so a crash mid-write cannot
            # leave a half-written file to be loaded next boot.
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                json.dump(payload, handle)
                temp = handle.name
            os.replace(temp, self.path)
        except OSError as exc:
            self._writable = False
            logger.warning(
                "Cannot write state to %s (%s); continuing in memory only. "
                "Message links will be lost on restart.",
                self.path,
                exc,
            )
