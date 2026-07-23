"""Имитатор расширения Chrome: сквозная проверка приложения без реального созвона.

Подключается к WS-серверу приложения и шлёт снапшоты, как это делает sw.js:
вход в конференцию, появление второго участника, мьют/анмьют в середине, выход.
Приложение при этом должно завести запись в БД, писать звук и (не найдя окна
Chrome) корректно собрать аудио-артефакты после grace-периода.

  python tools/fake_extension.py [--port 8765] [--duration 15] [--room smoketest]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

from websockets.asyncio.client import connect

from app.ws_server import EXTENSION_ORIGIN


async def run(port: int, duration: float, room: str) -> None:
    tab = 4242
    p_local = {"id": "local-1", "name": "Я (тест)", "local": True}
    p_remote = {"id": "remote-1", "name": "Собеседник (тест)", "local": False}

    def snap(joined: bool, parts, muted: bool | None) -> str:
        return json.dumps({
            "type": "snapshot", "tab_id": tab, "ts": int(time.time() * 1000),
            "joined": joined, "room": room, "participants": parts,
            "audioMuted": muted, "via": "app",
            "title": f"{room} | Jitsi Meet",
            "url": f"https://meet.jit.si/{room}",
        })

    async with connect(f"ws://127.0.0.1:{port}",
                       origin=EXTENSION_ORIGIN) as ws:
        print("Подключился к приложению; вход в конференцию…")
        await ws.send(snap(False, [p_local], False))
        await asyncio.sleep(1)
        await ws.send(snap(True, [p_local], False))

        t0 = time.time()
        mute_from = t0 + duration * 0.4
        mute_to = t0 + duration * 0.7
        while time.time() - t0 < duration:
            await asyncio.sleep(2)
            now = time.time()
            parts = [p_local, p_remote] if now - t0 > 3 else [p_local]
            muted = mute_from <= now < mute_to
            await ws.send(snap(True, parts, muted))
        print("Выход из конференции (дальше grace-период и финализация)…")
        await ws.send(snap(False, [p_local], False))
        await asyncio.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--duration", type=float, default=15)
    ap.add_argument("--room", default="smoketest")
    args = ap.parse_args()
    asyncio.run(run(args.port, args.duration, args.room))


if __name__ == "__main__":
    main()
