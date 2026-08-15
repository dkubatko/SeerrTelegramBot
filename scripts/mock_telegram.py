"""A stand-in for the Telegram Bot API, for end-to-end tests without a token.

Point the bot at it with TELEGRAM_API_BASE. It records outgoing calls and lets
a test inject updates that the bot's getUpdates loop will pick up.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web


class MockTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self._next_message_id = 500
        self._next_update_id = 1
        self._new_update = asyncio.Event()

    def push_update(self, update: dict[str, Any]) -> None:
        update.setdefault("update_id", self._next_update_id)
        self._next_update_id = update["update_id"] + 1
        self._pending.append(update)
        self._new_update.set()

    def push_callback(
        self, data: str, chat_id: int, message_id: int, is_photo: bool = False
    ) -> None:
        message: dict[str, Any] = {"message_id": message_id, "chat": {"id": chat_id}}
        if is_photo:
            message["photo"] = [{"file_id": "mock"}]
            message["caption"] = "pending"
        else:
            message["text"] = "pending"
        self.push_update(
            {
                "callback_query": {
                    "id": f"cb{message_id}",
                    "data": data,
                    "message": message,
                    "from": {"id": chat_id, "username": "tester"},
                }
            }
        )

    def push_command(self, text: str, chat_id: int) -> None:
        self.push_update(
            {
                "message": {
                    "message_id": 1,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "username": "tester"},
                    "text": text,
                }
            }
        )

    async def wait_for_sent(self, count: int, timeout: float = 5.0) -> None:
        async def poll() -> None:
            while len(self.sent) < count:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(poll(), timeout)

    async def wait_for_edits(self, count: int, timeout: float = 5.0) -> None:
        async def poll() -> None:
            while len(self.edits) < count:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(poll(), timeout)

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._dispatch)
        # Inspection and injection helpers, for driving the mock over HTTP when
        # it runs as its own container rather than inside a test process.
        app.router.add_get("/_sent", self._http_sent)
        app.router.add_post("/_press", self._http_press)
        app.router.add_post("/_command", self._http_command)
        return app

    async def _http_sent(self, _request: web.Request) -> web.Response:
        return web.json_response({"sent": self.sent, "edits": self.edits,
                                  "answers": self.answers})

    async def _http_press(self, request: web.Request) -> web.Response:
        """POST /_press?data=approve:1&message_id=501&chat_id=4242"""
        query = request.query
        message_id = int(query.get("message_id", "0"))
        if not message_id:
            match = [m for m in self.sent if m["markup"]]
            if not match:
                return web.json_response({"error": "no message with buttons"},
                                         status=404)
            message_id = match[-1]["message_id"]
            is_photo = match[-1]["photo"]
        else:
            found = [m for m in self.sent if m["message_id"] == message_id]
            is_photo = bool(found and found[0]["photo"])

        self.push_callback(
            query.get("data", "approve:1"),
            int(query.get("chat_id", "4242")),
            message_id,
            is_photo,
        )
        return web.json_response({"pressed": query.get("data"), "message_id": message_id})

    async def _http_command(self, request: web.Request) -> web.Response:
        """POST /_command?text=/start&chat_id=4242"""
        text = request.query.get("text", "/start")
        self.push_command(text, int(request.query.get("chat_id", "4242")))
        return web.json_response({"sent": text})

    async def _dispatch(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params = await request.json()
        handler = getattr(self, f"_do_{method.lower()}", None)
        if handler is None:
            return web.json_response({"ok": False, "description": f"no {method}"})
        return web.json_response({"ok": True, "result": await handler(params)})

    # --- API surface -------------------------------------------------------

    async def _do_getme(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"id": 1, "is_bot": True, "username": "mock_bot"}

    async def _do_deletewebhook(self, _params: dict[str, Any]) -> bool:
        return True

    async def _do_sendmessage(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._record(params, params.get("text", ""), photo=False)

    async def _do_sendphoto(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._record(params, params.get("caption", ""), photo=True)

    def _record(self, params: dict[str, Any], text: str, photo: bool) -> dict[str, Any]:
        self._next_message_id += 1
        self.sent.append(
            {
                "message_id": self._next_message_id,
                "chat_id": params.get("chat_id"),
                "text": text,
                "markup": params.get("reply_markup"),
                "photo": photo,
            }
        )
        return {"message_id": self._next_message_id}

    async def _do_editmessagetext(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._record_edit(params, params.get("text", ""), caption=False)

    async def _do_editmessagecaption(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._record_edit(params, params.get("caption", ""), caption=True)

    def _record_edit(
        self, params: dict[str, Any], text: str, caption: bool
    ) -> dict[str, Any]:
        self.edits.append(
            {
                "message_id": params.get("message_id"),
                "text": text,
                "caption": caption,
                "markup": params.get("reply_markup"),
            }
        )
        return {"message_id": params.get("message_id")}

    async def _do_answercallbackquery(self, params: dict[str, Any]) -> bool:
        self.answers.append(
            {"text": params.get("text"), "alert": params.get("show_alert", False)}
        )
        return True

    async def _do_getupdates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        offset = params.get("offset")
        if offset is not None:
            self._pending = [u for u in self._pending if u["update_id"] >= offset]

        if not self._pending:
            self._new_update.clear()
            try:
                await asyncio.wait_for(
                    self._new_update.wait(), timeout=params.get("timeout", 1)
                )
            except (TimeoutError, asyncio.TimeoutError):
                return []

        batch, self._pending = self._pending, []
        return batch


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the mock Telegram API")
    parser.add_argument("--port", type=int, default=9111)
    args = parser.parse_args()

    mock = MockTelegram()
    print(f"mock telegram on :{args.port} - GET /_sent, POST /_press, /_command")
    web.run_app(mock.build_app(), port=args.port, print=None)


if __name__ == "__main__":
    main()
