"""Запись звука: микрофон и системный звук (WASAPI loopback) через PyAudioWPatch.

Каждый источник пишется в сырой PCM s16le (без контейнера — файл валиден даже
после аварийного завершения). При ошибке устройства или смене устройства
вывода/ввода по умолчанию поток перезапускается новым сегментом; параметры
сегментов уходят в SegmentLog.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pyaudiowpatch as pyaudio

from app.recorder.segments import SegmentLog

log = logging.getLogger(__name__)

CHUNK_FRAMES = 1024
DEFAULT_DEVICE_POLL_S = 5.0

# PortAudio/WASAPI плохо переносит параллельную инициализацию и открытие
# устройств из разных потоков — сериализуем эти операции глобально
# (чтение открытых потоков остаётся параллельным).
_pa_lock = threading.Lock()


class AudioRecorder:
    """kind: "mic" (устройство ввода) или "speakers" (loopback вывода)."""

    def __init__(self, out_dir: Path, kind: str, seglog: SegmentLog, event_cb=None):
        assert kind in ("mic", "speakers")
        self.out_dir = out_dir
        self.kind = kind
        self.seglog = seglog
        self.event_cb = event_cb or (lambda etype, payload: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seg_idx = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"audio-{self.kind}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                log.error("Поток audio-%s не завершился за 10 с", self.kind)

    # ------------------------------------------------------------------

    def _resolve_device(self, p: pyaudio.PyAudio) -> dict:
        if self.kind == "speakers":
            return p.get_default_wasapi_loopback()
        try:
            return p.get_default_wasapi_device(d_in=True)
        except OSError:
            return p.get_default_input_device_info()

    def _run(self) -> None:
        with _pa_lock:
            p = pyaudio.PyAudio()
        fail_streak = 0
        reported_no_device = False
        try:
            while not self._stop.is_set():
                try:
                    with _pa_lock:
                        dev = self._resolve_device(p)
                except (OSError, LookupError) as e:
                    if not reported_no_device:
                        reported_no_device = True
                        log.error("Нет устройства для %s: %s", self.kind, e)
                        self.event_cb("audio_no_device", {"kind": self.kind, "error": str(e)})
                    self._stop.wait(3)
                    continue
                reported_no_device = False
                try:
                    self._record_segment(p, dev)
                    fail_streak = 0
                except Exception as e:
                    fail_streak += 1
                    log.warning("Сегмент %s прерван (%s); попытка №%d",
                                self.kind, e, fail_streak)
                    self.event_cb("audio_device_lost",
                                  {"kind": self.kind, "error": str(e)})
                    self._stop.wait(min(5, fail_streak))
        finally:
            with _pa_lock:
                p.terminate()

    def _open_stream(self, p: pyaudio.PyAudio, dev: dict):
        """Открывает поток, перебирая число каналов: устройства нередко
        сообщают maxInputChannels, с которым WASAPI открыть не может."""
        rate = int(dev["defaultSampleRate"])
        reported = max(1, int(dev["maxInputChannels"]))
        errors = []
        for ch in dict.fromkeys([min(reported, 2), 1, 2]):
            try:
                with _pa_lock:
                    stream = p.open(format=pyaudio.paInt16, channels=ch, rate=rate,
                                    input=True, input_device_index=int(dev["index"]),
                                    frames_per_buffer=CHUNK_FRAMES)
                return stream, rate, ch
            except OSError as e:
                errors.append(f"{ch}ch: {e}")
        raise OSError(f"не открылось ни с одним числом каналов ({'; '.join(errors)})")

    def _default_device_changed(self, p: pyaudio.PyAudio, dev_index: int) -> bool:
        """Смена устройства по умолчанию (переключили наушники в Windows):
        существующий поток продолжает писать старое устройство, поэтому
        периодически сверяемся и при смене начинаем новый сегмент."""
        now = time.monotonic()
        if now - self._last_dev_check < DEFAULT_DEVICE_POLL_S:
            return False
        self._last_dev_check = now
        try:
            with _pa_lock:
                current_index = int(self._resolve_device(p)["index"])
        except (OSError, LookupError):
            return False
        if current_index != dev_index:
            log.info("Устройство по умолчанию (%s) сменилось — новый сегмент", self.kind)
            self.event_cb("audio_device_changed", {"kind": self.kind})
            return True
        return False

    def _record_segment(self, p: pyaudio.PyAudio, dev: dict) -> None:
        dev_index = int(dev["index"])
        log.info("Открываю устройство %s: «%s»", self.kind, dev.get("name", "?"))
        stream, rate, channels = self._open_stream(p, dev)
        self._seg_idx += 1
        path = self.out_dir / f"{self.kind}_{self._seg_idx:02d}.pcm"
        bytes_per_frame = 2 * channels
        t0_written = False
        self._last_dev_check = time.monotonic()
        log.info("Запись %s: «%s» %d Гц %d кан. -> %s",
                 self.kind, dev.get("name", "?"), rate, channels, path.name)
        try:
            with open(path, "wb") as f:
                if self.kind == "speakers":
                    # WASAPI loopback не отдаёт пакеты, пока в системе ничего не
                    # воспроизводится — читаем неблокирующе и дозаписываем тишину,
                    # чтобы дорожка существовала всегда и не съезжала по времени.
                    t0 = time.time()
                    self.seglog.add_audio(kind=self.kind, path=path.name, t0=t0,
                                          rate=rate, channels=channels)
                    t0_written = True
                    written = 0  # кадров записано
                    zeros = b"\x00" * (CHUNK_FRAMES * bytes_per_frame)
                    while not self._stop.is_set():
                        if stream.get_read_available() >= CHUNK_FRAMES:
                            data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                            f.write(data)
                            written += len(data) // bytes_per_frame
                        else:
                            deficit = (time.time() - t0) * rate - written
                            if deficit > rate * 0.5:
                                n = int(deficit)
                                while n >= CHUNK_FRAMES:
                                    f.write(zeros)
                                    written += CHUNK_FRAMES
                                    n -= CHUNK_FRAMES
                            time.sleep(0.02)
                        if self._default_device_changed(p, dev_index):
                            break
                else:
                    while not self._stop.is_set():
                        data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                        if not t0_written:
                            t0 = time.time() - len(data) / bytes_per_frame / rate
                            self.seglog.add_audio(kind=self.kind, path=path.name, t0=t0,
                                                  rate=rate, channels=channels)
                            t0_written = True
                        f.write(data)
                        if self._default_device_changed(p, dev_index):
                            break
        finally:
            with _pa_lock:
                try:
                    stream.stop_stream()
                except OSError:
                    pass
                stream.close()
            if not t0_written:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
