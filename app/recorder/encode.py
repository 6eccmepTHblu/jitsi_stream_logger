"""Аргументы ffmpeg для кодирования видео.

Общий модуль для живой записи сегментов (`recorder/video.py`) и финальной
пересборки (`recorder/mux.py`), чтобы выбранный в настройках кодек применялся
одинаково в обоих местах.
"""
from __future__ import annotations

from app.config import Config


def video_encode_args(cfg: Config, fps: int) -> list[str]:
    """Опции `-c:v …` для выбранного кодека (без -movflags и путей)."""
    g = str(max(1, fps * 2))
    enc = cfg.video_encoder
    if enc == "libsvtav1":
        # AV1 (SVT-AV1), программный кодек — заметно меньший размер записи экрана.
        # На современном многоядерном CPU держит реальное время на 15 fps.
        # tune=0 — субъективное качество; scm=2 — авто-режим экранного контента
        # (чёткий текст при меньшем битрейте).
        return ["-c:v", "libsvtav1", "-preset", str(cfg.av1_preset),
                "-crf", str(cfg.av1_crf), "-pix_fmt", "yuv420p", "-g", g,
                "-svtav1-params", "tune=0:scm=2"]
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", cfg.video_preset,
                "-crf", str(cfg.video_crf), "-pix_fmt", "yuv420p", "-g", g]
    # Аппаратные кодеры (h264_nvenc/qsv/amf и т.п.) — без x264/svt-специфичных флагов.
    return ["-c:v", enc, "-pix_fmt", "yuv420p", "-g", g]
