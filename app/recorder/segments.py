"""Журнал медиасегментов записи (segments.json в папке созвона).

Пишется по ходу записи, атомарно (tmp + replace), чтобы после падения
приложения запись можно было досклеить: аудио хранится сырым PCM (s16le),
видео — MKV, оба формата читаемы без корректного "закрытия" файла.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class SegmentLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {"audio": [], "video": []}
        self._flush()

    def add_audio(self, *, kind: str, path: str, t0: float, rate: int, channels: int) -> None:
        with self._lock:
            self.data["audio"].append({
                "kind": kind, "path": path, "t0": t0,
                "rate": rate, "channels": channels, "format": "s16le",
            })
            self._flush()

    def add_video(self, *, path: str, t0: float, width: int, height: int, fps: int) -> None:
        with self._lock:
            self.data["video"].append({
                "path": path, "t0": t0, "width": width, "height": height, "fps": fps,
            })
            self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def load(path: Path) -> dict:
        """Возвращает данные сегментов ({"audio": [...], "video": [...]})."""
        if not path.exists():
            return {"audio": [], "video": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"audio": [], "video": []}
        data.setdefault("audio", [])
        data.setdefault("video", [])
        return data
