"""Локальный WebSocket-сервер (127.0.0.1), к которому подключается расширение Chrome."""
from __future__ import annotations

import json
import logging

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

EXTENSION_ID = "indgmclalpjobnolmodgimkjfbkiopcl"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"


class WsServer:
    def __init__(self, port: int, session_manager):
        self.port = port
        self.sm = session_manager
        self.server = None

    async def start(self) -> None:
        """Поднимает сервер; OSError при занятом порте — признак второго экземпляра."""
        self.server = await serve(self._handler, "127.0.0.1", self.port)
        log.info("WS-сервер слушает 127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handler(self, ws) -> None:
        origin = ws.request.headers.get("Origin", "") if ws.request else ""
        if origin != EXTENSION_ORIGIN:
            log.warning("Отклонено WS-подключение с Origin=%r", origin)
            await ws.close(code=4403, reason="forbidden origin")
            return
        log.info("Расширение подключилось (%s)", origin)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict):
                    try:
                        await self.sm.handle_message(msg)
                    except Exception:
                        log.exception("Ошибка обработки сообщения: %r", msg)
        except ConnectionClosed:
            pass
        finally:
            log.info("Расширение отключилось")
