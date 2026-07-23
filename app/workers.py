"""Запуск блокирующей функции в daemon-потоке с ожиданием из asyncio.

В отличие от asyncio.to_thread, daemon-поток не блокирует выход из
приложения: незавершённый сетевой запрос (например, долгая генерация LLM)
не заставит процесс висеть при закрытии.
"""
from __future__ import annotations

import asyncio
import threading


async def run_daemon(func, /, *args):
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _set(result=None, exc: BaseException | None = None) -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def target() -> None:
        try:
            res = func(*args)
        except BaseException as e:  # noqa: BLE001 — пробрасываем всё в future
            try:
                loop.call_soon_threadsafe(_set, None, e)
            except RuntimeError:
                pass  # цикл уже закрыт
        else:
            try:
                loop.call_soon_threadsafe(_set, res)
            except RuntimeError:
                pass

    threading.Thread(target=target, daemon=True,
                     name=f"worker-{getattr(func, '__name__', 'fn')}").start()
    return await fut
