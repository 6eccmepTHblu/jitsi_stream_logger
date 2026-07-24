"""Аргументы ffmpeg для кодирования видео.

Общий модуль для живой записи сегментов (`recorder/video.py`) и финальной
пересборки (`recorder/mux.py`), чтобы выбранный в настройках кодек применялся
одинаково в обоих местах.
"""
from __future__ import annotations

from app.config import Config

# Пресеты качества для вкладки «Качество видео» в настройках. Ключ пресета
# хранится в config ([video] quality_preset) и, если задан, полностью
# определяет кодек и параметры (переопределяя encoder/crf/preset). Этот же
# список используется для отрисовки радио-кнопок в GUI — единственный источник
# правды о наборе пресетов.
VIDEO_QUALITY_PRESETS: list[dict] = [
    {"key": "h264_crf28",
     "codec": "H.264",
     "label": "H.264 — libx264, preset medium, CRF 28",
     "hint": "по умолчанию, максимальная совместимость"},
    {"key": "h265_crf30",
     "codec": "H.265/HEVC",
     "label": "H.265/HEVC — libx265, preset medium, CRF 30",
     "hint": "лучше качество при меньшем размере"},
    {"key": "h265_crf35",
     "codec": "H.265/HEVC",
     "label": "H.265/HEVC — libx265, preset medium, CRF 35",
     "hint": "заметно меньше файл"},
    {"key": "av1_crf50",
     "codec": "AV1",
     "label": "AV1 — libsvtav1, preset 8, CRF 50",
     "hint": "малый размер, выше нагрузка на CPU"},
    {"key": "av1_crf55",
     "codec": "AV1",
     "label": "AV1 — libsvtav1, preset 8, CRF 55",
     "hint": "минимальный размер, выше нагрузка на CPU"},
    {"key": "vp9_crf40",
     "codec": "VP9",
     "label": "VP9 — libvpx-vp9, CRF 40, b:v 0",
     "hint": "constant-quality VP9"},
]

VIDEO_QUALITY_KEYS = frozenset(p["key"] for p in VIDEO_QUALITY_PRESETS)


def _quality_preset_args(key: str, g: str) -> list[str] | None:
    """Аргументы `-c:v …` для пресета качества, либо None для неизвестного."""
    common = ["-pix_fmt", "yuv420p", "-g", g]
    # tag:v hvc1 — чтобы H.265 в mp4 проигрывался в QuickTime/Apple-плеерах.
    if key == "h264_crf28":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "28", *common]
    if key == "h265_crf30":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", "30",
                "-tag:v", "hvc1", *common]
    if key == "h265_crf35":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", "35",
                "-tag:v", "hvc1", *common]
    if key == "av1_crf50":
        return ["-c:v", "libsvtav1", "-preset", "8", "-crf", "50",
                *common, "-svtav1-params", "tune=0:scm=2"]
    if key == "av1_crf55":
        return ["-c:v", "libsvtav1", "-preset", "8", "-crf", "55",
                *common, "-svtav1-params", "tune=0:scm=2"]
    if key == "vp9_crf40":
        return ["-c:v", "libvpx-vp9", "-crf", "40", "-b:v", "0", *common]
    return None


def video_encode_args(cfg: Config, fps: int) -> list[str]:
    """Опции `-c:v …` для выбранного кодека (без -movflags и путей)."""
    g = str(max(1, fps * 2))
    # Пресет качества (вкладка «Качество видео») имеет приоритет над отдельными
    # полями encoder/crf/preset. Пусто = старое поведение по этим полям
    # (сохраняем совместимость со старыми config.toml без quality_preset).
    preset = getattr(cfg, "video_quality_preset", "")
    if preset:
        args = _quality_preset_args(preset, g)
        if args is not None:
            return args
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
