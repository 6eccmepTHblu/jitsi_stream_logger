"""Иконка в трее (pystray) и уведомления Windows (winotify).

Состояние приложения показывается цветной точкой на иконке:
  без точки — ожидание; зелёная — созвон журналируется (без записи);
  красная — идёт запись; жёлтая — ожидание возврата (grace);
  оранжевая — сборка файлов; голубая — распознавание речи; серая — пауза.
"""
from __future__ import annotations

import logging

import pystray
from PIL import Image, ImageDraw
from winotify import Notification

from app.config import APP_TITLE

log = logging.getLogger(__name__)

_DOT_COLORS = {
    "logging": (76, 175, 80, 255),       # зелёный
    "recording": (230, 57, 53, 255),     # красный
    "grace": (255, 193, 7, 255),         # жёлтый
    "finalizing": (255, 152, 0, 255),    # оранжевый
    "transcribing": (3, 169, 244, 255),  # голубой
    "paused": (158, 158, 158, 255),      # серый
}


def _icon_image(state: str = "idle") -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Стилизованная камера.
    d.rounded_rectangle([6, 18, 42, 50], radius=8, fill=(42, 109, 245, 255))
    d.polygon([(42, 26), (58, 18), (58, 50), (42, 42)], fill=(42, 109, 245, 255))
    color = _DOT_COLORS.get(state)
    if color:
        d.ellipse([36, 0, 64, 28], fill=(255, 255, 255, 255))  # белая обводка
        d.ellipse([39, 3, 61, 25], fill=color)
    return img


class Tray:
    def __init__(self, app):
        self.app = app
        self._status = "Ожидание созвона"
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self._status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: self.app.record_action_text,
                             lambda icon, item: self.app.toggle_manual_record()),
            pystray.MenuItem("Настройки…",
                             lambda icon, item: self.app.open_settings(),
                             default=True),  # двойной клик по иконке
            pystray.MenuItem("Открыть папку записей",
                             lambda icon, item: self.app.open_records()),
            pystray.MenuItem("Пауза обнаружения",
                             lambda icon, item: self.app.toggle_pause(),
                             checked=lambda item: self.app.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda icon, item: self.app.request_quit()),
        )
        self.icon = pystray.Icon("jitsi_stream_logger", _icon_image("idle"),
                                 APP_TITLE, menu)

    def run(self) -> None:
        self.icon.run()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass

    def set_state(self, state: str, text: str) -> None:
        self._status = text
        try:
            self.icon.icon = _icon_image(state)
            self.icon.title = f"{APP_TITLE} — {text}"
            self.icon.update_menu()
        except Exception:
            log.debug("Не удалось обновить трей", exc_info=True)

    def notify(self, title: str, msg: str) -> None:
        try:
            Notification(app_id=APP_TITLE, title=title, msg=msg,
                         duration="short").show()
        except Exception:
            log.warning("Не удалось показать уведомление: %s — %s", title, msg,
                        exc_info=True)
