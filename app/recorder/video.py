"""Запись видео окна Chrome: Windows Graphics Capture -> FFmpeg (MKV-сегменты).

WGC отдаёт кадры только при изменении содержимого, поэтому отдельный
"пейсер" пишет в ffmpeg последний полученный кадр со строго постоянной
частотой — так длительность видео совпадает с реальным временем.
Ресайз окна = новый сегмент (склейка при финализации). Если окно не найдено,
поиск повторяется каждые 5 секунд, звук при этом пишется независимо.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

import psutil
import win32api
import win32gui
import win32process
from windows_capture import WindowsCapture

from app.config import Config
from app.recorder.encode import video_encode_args
from app.recorder.segments import SegmentLog

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
FIND_RETRY_S = 5.0


def monitor_at_cursor() -> int:
    """Номер монитора под курсором (с 1, в порядке EnumDisplayMonitors —
    в этом же порядке мониторы нумерует windows-capture)."""
    try:
        x, y = win32api.GetCursorPos()
        for i, (_h, _dc, rect) in enumerate(win32api.EnumDisplayMonitors(), 1):
            left, top, right, bottom = rect
            if left <= x < right and top <= y < bottom:
                return i
    except Exception:
        pass
    return 1


def list_windows(needles: list[str], process_name: str | None) -> list[tuple[int, str]]:
    """Видимые top-level окна, чей заголовок содержит одну из подстрок.

    process_name=None — окна любых процессов (используется в selftest).
    """
    results: list[tuple[int, str]] = []
    needles_l = [n.lower() for n in needles if n]
    if not needles_l:
        return results

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or not any(n in title.lower() for n in needles_l):
            return
        if process_name is not None:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if psutil.Process(pid).name().lower() != process_name:
                    return
            except (psutil.Error, OSError):
                return
        results.append((hwnd, title))

    try:
        win32gui.EnumWindows(cb, None)
    except OSError:
        pass
    return results


class VideoRecorder:
    def __init__(self, out_dir: Path, cfg: Config, seglog: SegmentLog, event_cb=None,
                 process_name: str = "chrome.exe", mode: str | None = None):
        self.out_dir = out_dir
        self.cfg = cfg
        self.seglog = seglog
        self.event_cb = event_cb or (lambda etype, payload: None)
        self.process_name = process_name
        # mode=None — брать режим из конфига (и подхватывать его правки на лету);
        # заданный режим перекрывает конфиг на весь срок жизни рекордера.
        self._mode_override = mode
        self._needles: list[str] = []
        self._needles_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seg_idx = 0
        self._fflog = None

    def start(self, needles: list[str]) -> None:
        self.update_needles(needles)
        self._fflog = open(self.out_dir / "ffmpeg_video.log", "ab")
        self._thread = threading.Thread(target=self._run, daemon=True, name="video")
        self._thread.start()

    @property
    def mode(self) -> str:
        return self._mode_override or self.cfg.video_mode

    def update_needles(self, needles: list[str]) -> None:
        with self._needles_lock:
            self._needles = list(needles)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=20)
            if self._thread.is_alive():
                log.error("Видеопоток не завершился за 20 с")
        if self._fflog is not None:
            self._fflog.close()
            self._fflog = None

    # ------------------------------------------------------------------

    def _find_window(self) -> tuple[int, str] | None:
        with self._needles_lock:
            needles = list(self._needles)
        results = list_windows(needles, self.process_name)
        return results[0] if results else None

    def _run(self) -> None:
        if self.mode in ("monitor", "cursor"):
            # Режимы «монитор целиком» и «экран, где курсор» — окно не ищем.
            while not self._stop.is_set():
                idx = (monitor_at_cursor() if self.mode == "cursor"
                       else self.cfg.video_monitor)
                log.info("Захват монитора %d", idx)
                try:
                    switched = self._capture_session(None, monitor_index=idx)
                except Exception as e:
                    switched = False
                    log.exception("Сеанс захвата монитора прервался")
                    self.event_cb("video_error", {"error": str(e)})
                if not switched:  # после переключения курсора рестартуем сразу
                    self._stop.wait(FIND_RETRY_S)
            return
        warned = False
        while not self._stop.is_set():
            found = self._find_window()
            if found is None:
                if not warned:
                    warned = True
                    log.warning("Окно с активной вкладкой созвона не найдено — "
                                "видео на паузе, звук пишется")
                    self.event_cb("video_window_not_found", None)
                self._stop.wait(FIND_RETRY_S)
                continue
            hwnd, title = found
            if warned:
                warned = False
            log.info("Захват окна: «%s» (hwnd=%d)", title, hwnd)
            self.event_cb("video_window_found", {"title": title})
            try:
                self._capture_session(hwnd)
            except Exception as e:
                log.exception("Сеанс захвата окна прервался")
                self.event_cb("video_error", {"error": str(e)})
                self._stop.wait(FIND_RETRY_S)

    def _window_matches(self, hwnd: int) -> bool:
        """Активная вкладка окна всё ещё вкладка созвона (по заголовку)?"""
        with self._needles_lock:
            needles = [n.lower() for n in self._needles if n]
        try:
            title = win32gui.GetWindowText(hwnd)
        except OSError:
            return False
        if not title:
            return False
        tl = title.lower()
        return any(n in tl for n in needles) if needles else True

    def _capture_session(self, hwnd: int | None,
                         monitor_index: int | None = None) -> bool:
        """Возвращает True, если сеанс прерван переключением курсора на другой
        монитор (нужен немедленный рестарт на новом мониторе)."""
        fps = self.cfg.video_fps
        period = 1.0 / fps
        lock = threading.Lock()
        holder: dict = {"buf": None, "w": 0, "h": 0}
        closed = threading.Event()

        if hwnd is not None:
            # Окно могло исчезнуть между поиском и стартом захвата (быстрый F5,
            # закрытие вкладки). Передавать мёртвый HWND в WGC нельзя: нативный
            # модуль падает с access violation (0xC0000005) и уносит весь
            # процесс мимо Python. Проверяем — и уходим в обычный ретрут через
            # исключение, которое ловит _run.
            if not win32gui.IsWindow(hwnd):
                raise RuntimeError(f"окно hwnd={hwnd} исчезло до старта захвата")
            cap = WindowsCapture(
                cursor_capture=True,
                draw_border=False,
                minimum_update_interval=max(1, 1000 // fps),
                window_hwnd=hwnd,
            )
        else:
            cap = WindowsCapture(
                cursor_capture=True,
                draw_border=False,
                minimum_update_interval=max(1, 1000 // fps),
                monitor_index=monitor_index or self.cfg.video_monitor,
            )

        @cap.event
        def on_frame_arrived(frame, capture_control):
            if self._stop.is_set():
                capture_control.stop()
                return
            # Копия обязательна: буфер кадра живёт только внутри колбэка.
            data = frame.frame_buffer.tobytes()
            with lock:
                holder["buf"] = data
                holder["w"] = frame.width
                holder["h"] = frame.height

        @cap.event
        def on_closed():
            closed.set()

        control = cap.start_free_threaded()
        ffproc: subprocess.Popen | None = None
        dims: tuple[int, int] | None = None
        next_tick = time.monotonic() + period
        last_guard = time.monotonic()
        cursor_miss = 0
        switched = False
        try:
            while not (self._stop.is_set() or closed.is_set() or control.is_finished()):
                now = time.monotonic()
                if now < next_tick:
                    time.sleep(min(next_tick - now, 0.1))
                    continue
                next_tick += period
                if next_tick < now - 1.0:  # долгий лаг — не навёрстываем
                    next_tick = now + period
                # В оконном режиме следим, что активная вкладка — всё ещё созвон;
                # иначе ставим видео на паузу (не пишем посторонние вкладки).
                if hwnd is not None and now - last_guard >= 1.0:
                    last_guard = now
                    if not self._window_matches(hwnd):
                        log.info("Активная вкладка сменилась — видео на паузе")
                        self.event_cb("video_paused_tab_inactive", None)
                        break
                # В режиме «экран, где курсор» переключаемся вслед за курсором
                # (два подряд попадания на другой монитор ≈ 1 с стабильности).
                elif (hwnd is None and self.mode == "cursor"
                        and monitor_index is not None and now - last_guard >= 0.5):
                    last_guard = now
                    cur = monitor_at_cursor()
                    if cur != monitor_index:
                        cursor_miss += 1
                        if cursor_miss >= 2:
                            log.info("Курсор на мониторе %d — переключаю запись", cur)
                            self.event_cb("video_monitor_switch", {"to": cur})
                            switched = True
                            break
                    else:
                        cursor_miss = 0
                with lock:
                    buf = holder["buf"]
                    w, h = holder["w"], holder["h"]
                if buf is None:
                    continue
                if ffproc is None or (w, h) != dims:
                    self._close_ffmpeg(ffproc)
                    ffproc = self._open_segment(w, h)
                    dims = (w, h)
                try:
                    ffproc.stdin.write(buf)
                except OSError:
                    log.error("ffmpeg (видео) неожиданно завершился — сегмент прерван")
                    ffproc = None
                    dims = None
                    time.sleep(1)
        finally:
            try:
                control.stop()
            except Exception:
                pass
            self._close_ffmpeg(ffproc)
        if closed.is_set():
            log.info("Окно захвата закрылось")
            self.event_cb("video_window_closed", None)
        return switched

    def _open_segment(self, w: int, h: int) -> subprocess.Popen:
        self._seg_idx += 1
        path = self.out_dir / f"video_{self._seg_idx:02d}.mkv"
        fps = self.cfg.video_fps
        args = [
            self.cfg.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(fps),
            "-i", "pipe:0",
            "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
        ]
        args += video_encode_args(self.cfg, fps)
        args.append(str(path))
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=self._fflog, creationflags=CREATE_NO_WINDOW)
        self.seglog.add_video(path=path.name, t0=time.time(), width=w, height=h, fps=fps)
        log.info("Видеосегмент %s (%dx%d @ %d fps)", path.name, w, h, fps)
        return proc

    @staticmethod
    def _close_ffmpeg(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
