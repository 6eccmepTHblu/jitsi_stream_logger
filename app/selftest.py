"""Самопроверка компонентов записи без реального созвона.

  python -m app.selftest devices              — устройства звука по умолчанию
  python -m app.selftest audio 5              — 5 с записи микрофона и динамиков + сборка
  python -m app.selftest window <подстрока>   — найти окна по заголовку (любой процесс)
  python -m app.selftest video <подстрока> 5  — 5 с записи найденного окна + сборка
  python -m app.selftest monitor 1 5          — 5 с записи монитора №1 + сборка
  python -m app.selftest cursor 5             — 5 с записи экрана, где курсор
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime

from app.config import load_config, setup_logging
from app.recorder import mux
from app.recorder.audio import AudioRecorder
from app.recorder.segments import SegmentLog
from app.recorder.video import VideoRecorder, list_windows


def _test_dir(cfg):
    d = cfg.records_dir / f"_selftest_{datetime.now():%Y%m%d_%H%M%S}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_devices() -> None:
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        try:
            dev = p.get_default_wasapi_device(d_in=True)
            print(f"Микрофон:  {dev['name']}  ({int(dev['defaultSampleRate'])} Гц, "
                  f"{dev['maxInputChannels']} кан.)")
        except OSError as e:
            print(f"Микрофон:  НЕ НАЙДЕН ({e})")
        try:
            dev = p.get_default_wasapi_loopback()
            print(f"Динамики (loopback):  {dev['name']}  "
                  f"({int(dev['defaultSampleRate'])} Гц, {dev['maxInputChannels']} кан.)")
        except (OSError, LookupError) as e:
            print(f"Динамики (loopback):  НЕ НАЙДЕНЫ ({e})")
    finally:
        p.terminate()


def cmd_audio(seconds: float) -> None:
    cfg = load_config()
    d = _test_dir(cfg)
    seglog = SegmentLog(d / "segments.json")
    mic = AudioRecorder(d, "mic", seglog)
    spk = AudioRecorder(d, "speakers", seglog)
    print(f"Пишу звук {seconds:.0f} с (говорите и включите любой звук)…")
    mic.start()
    spk.start()
    time.sleep(seconds)
    mic.stop()
    spk.stop()
    result = asyncio.run(mux.finalize_call(cfg, d, SegmentLog.load(d / "segments.json"), []))
    print("Готово:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    if not result:
        print(f"  (сегментов нет — проверьте устройства: python -m app.selftest devices)")


def cmd_window(needle: str) -> None:
    for hwnd, title in list_windows([needle], None):
        print(f"  hwnd={hwnd}  «{title}»")


def cmd_video(needle: str, seconds: float) -> None:
    cfg = load_config()
    cfg.video_mode = "window"
    wins = list_windows([needle], None)
    if not wins:
        sys.exit(f"Окно с «{needle}» в заголовке не найдено")
    print(f"Захватываю окно «{wins[0][1]}» {seconds:.0f} с…")
    d = _test_dir(cfg)
    seglog = SegmentLog(d / "segments.json")
    rec = VideoRecorder(d, cfg, seglog, process_name=None)
    rec.start([needle])
    time.sleep(seconds)
    rec.stop()
    result = asyncio.run(mux.finalize_call(cfg, d, SegmentLog.load(d / "segments.json"), []))
    print("Готово:")
    for k, v in result.items():
        print(f"  {k}: {v}")


def cmd_monitor(index: int, seconds: float, mode: str = "monitor") -> None:
    cfg = load_config()
    cfg.video_mode = mode
    cfg.video_monitor = index
    cfg.video_enabled = True
    print(f"Захватываю ({mode}) монитор {index} {seconds:.0f} с…")
    d = _test_dir(cfg)
    seglog = SegmentLog(d / "segments.json")
    rec = VideoRecorder(d, cfg, seglog)
    rec.start([])
    time.sleep(seconds)
    rec.stop()
    result = asyncio.run(mux.finalize_call(cfg, d, SegmentLog.load(d / "segments.json"), []))
    print("Готово:")
    for k, v in result.items():
        print(f"  {k}: {v}")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg, console=True)
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "devices":
        cmd_devices()
    elif cmd == "audio":
        cmd_audio(float(args[1]) if len(args) > 1 else 5.0)
    elif cmd == "window":
        cmd_window(args[1] if len(args) > 1 else "Chrome")
    elif cmd == "video":
        cmd_video(args[1], float(args[2]) if len(args) > 2 else 5.0)
    elif cmd == "monitor":
        cmd_monitor(int(args[1]) if len(args) > 1 else 1,
                    float(args[2]) if len(args) > 2 else 5.0)
    elif cmd == "cursor":
        from app.recorder.video import monitor_at_cursor

        cmd_monitor(monitor_at_cursor(),
                    float(args[1]) if len(args) > 1 else 5.0, mode="cursor")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
