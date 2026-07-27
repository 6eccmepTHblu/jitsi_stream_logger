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

# Пресеты масштабирования итогового видео — вторая группа на той же вкладке
# настроек. Ключ хранится в config ([video] scale) и ограничивает высоту кадра
# call.mp4; ширина пересчитывается пропорционально, кадр меньше предела не
# растягивается. Как и с кодеком, этот список — единственный источник правды
# о наборе вариантов (по нему рисуются радио-кнопки в GUI).
VIDEO_SCALE_PRESETS: list[dict] = [
    {"key": "original", "height": 0,
     "label": "Оригинал",
     "hint": "не менять разрешение записи"},
    {"key": "1440", "height": 1440,
     "label": "Не выше 1440p (2K)",
     "hint": "уменьшает только записи выше 2K"},
    {"key": "1080", "height": 1080,
     "label": "Не выше 1080p (Full HD)",
     "hint": "самый ходовой размер"},
    {"key": "720", "height": 720,
     "label": "Не выше 720p (HD)",
     "hint": "заметно меньше файл, текст мельче"},
    {"key": "480", "height": 480,
     "label": "Не выше 480p",
     "hint": "минимальный размер, мелкий текст почти не читается"},
]

VIDEO_SCALE_KEYS = frozenset(p["key"] for p in VIDEO_SCALE_PRESETS)

_SCALE_HEIGHTS = {p["key"]: int(p["height"]) for p in VIDEO_SCALE_PRESETS}


def scale_max_height(cfg: Config) -> int:
    """Предел высоты итогового видео в пикселях; 0 — не масштабировать.

    Неизвестный (или пустой) ключ трактуем как «Оригинал»: правка config.toml
    руками не должна незаметно менять разрешение записей.
    """
    return _SCALE_HEIGHTS.get(getattr(cfg, "video_scale", "") or "original", 0)


def scaled_dims(w: int, h: int, max_h: int) -> tuple[int, int]:
    """Размер кадра после ограничения высоты: пропорции сохранены, стороны чётные.

    Кадр ниже предела возвращается без изменений — вверх не растягиваем.
    """
    if max_h <= 0 or h <= 0 or h <= max_h:
        return w, h
    new_h = max_h - max_h % 2
    new_w = max(2, round(w * new_h / h / 2) * 2)
    return new_w, new_h


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
