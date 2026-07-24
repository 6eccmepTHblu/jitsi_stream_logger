"""Оркестратор приложения: фоновый asyncio-цикл (WS-сервер, watchdog,
восстановление после сбоя, ретеншн) и мост к трею (другой поток)."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from app import records, summarize, transcribe
from app.config import Config, load_config
from app.records import CallLog
from app.recorder import mux
from app.recorder.segments import SegmentLog
from app.session import SessionManager
from app.ws_server import WsServer

log = logging.getLogger(__name__)


def _iso_to_ts(iso: str | None) -> float:
    if not iso:
        return time.time()
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return time.time()


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tray = None  # присваивается в main до tray.run()
        self.sm: SessionManager | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.fatal: str | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_evt: asyncio.Event | None = None
        self._settings_proc: subprocess.Popen | None = None
        self._queue_task: asyncio.Task | None = None
        self._postprocess_tasks: set[asyncio.Task] = set()
        self._retention_changed: asyncio.Event | None = None
        self._shutting_down = False

    # ------------------------------------------------- вызовы из потока трея

    @property
    def paused(self) -> bool:
        return bool(self.sm and self.sm.paused)

    def toggle_pause(self) -> None:
        if self.loop and self.sm:
            self.loop.call_soon_threadsafe(self.sm.set_paused, not self.sm.paused)

    @property
    def record_action_text(self) -> str:
        """Подпись кнопки записи в трее (читается из потока трея)."""
        sm = self.sm
        call = sm.call if sm is not None else None
        if call is None:
            return "Начать запись вручную"
        if call.manual:
            return "Остановить ручную запись"
        return "Остановить запись созвона"

    def toggle_manual_record(self) -> None:
        """Ручной старт/стоп записи из трея: coroutine — только через loop."""
        if self.loop is None or self.sm is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.sm.toggle_manual(), self.loop)
        except RuntimeError:
            log.warning("Ручная запись: фоновый цикл уже остановлен")

    def open_records(self) -> None:
        os.startfile(self.cfg.records_dir)  # noqa: S606

    def open_settings(self) -> None:
        """Окно настроек — всегда отдельный процесс: tkinter обязан жить
        в главном потоке своего процесса, из потока трея он вешает всё."""
        if self._settings_proc is not None and self._settings_proc.poll() is None:
            return  # окно уже открыто
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--settings"]
            cwd = str(Path(sys.executable).parent)
        else:
            cmd = [sys.executable, "-m", "app.main", "--settings"]
            cwd = str(Path(__file__).resolve().parents[1])
        try:
            self._settings_proc = subprocess.Popen(
                cmd, cwd=cwd, creationflags=0x08000000)  # CREATE_NO_WINDOW
        except OSError:
            log.exception("Не удалось открыть окно настроек")
            self.notify("Ошибка", "Не удалось открыть окно настроек (см. лог)")

    def request_quit(self) -> None:
        if self.loop and self._stop_evt:
            self.loop.call_soon_threadsafe(self._stop_evt.set)

    # ------------------------------------------------- колбэки для FSM

    def notify(self, title: str, msg: str) -> None:
        log.info("Уведомление: %s — %s", title, msg)
        if self.tray is not None:
            self.tray.notify(title, msg)

    def set_tray_state(self, state: str, text: str) -> None:
        if self.tray is not None:
            self.tray.set_state(state, text)

    # ------------------------------------------------- жизненный цикл

    def start_background(self) -> bool:
        self._thread = threading.Thread(target=self._loop_main, daemon=True,
                                        name="asyncio-loop")
        self._thread.start()
        self._ready.wait(timeout=15)
        return self.fatal is None

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop_main(self) -> None:
        try:
            asyncio.run(self._amain())
        except Exception:
            log.exception("Фоновый цикл упал")
            self.fatal = self.fatal or "Внутренняя ошибка (см. лог)"
            self._ready.set()
            if self.tray is not None:
                self.tray.stop()

    async def _amain(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop_evt = asyncio.Event()
        try:
            migrated, archived = transcribe.migrate_legacy_queue(
                (self.cfg.appdata_dir / "calls.db",
                 self.cfg.records_dir / "calls.db"))
            if migrated:
                log.info("Перенесено старых задач STT/резюме: %d", migrated)
            if archived:
                log.warning(
                    "Не удалось сопоставить %d старых задач; они сохранены в %s",
                    archived, transcribe.legacy_queue_path())
        except OSError:
            log.exception("Не удалось мигрировать старую очередь задач")
        # Снимок делаем до открытия WS: новые сессии этого запуска не должны
        # ошибочно считаться оборванными и попадать в recovery.
        stale_dirs = records.find_interrupted(self.cfg.records_dir)
        self.sm = SessionManager(self.cfg,
                                 notify=self.notify, set_tray=self.set_tray_state)
        self._retention_changed = asyncio.Event()
        ws = WsServer(self.cfg.ws_port, self.sm)
        try:
            await ws.start()
        except OSError:
            self.fatal = (f"Порт {self.cfg.ws_port} занят — похоже, приложение "
                          f"уже запущено.")
            log.error(self.fatal)
            self._ready.set()
            return
        reset_count = records.reset_interrupted_processing(self.cfg.records_dir)
        if reset_count:
            log.info("Освобождено оборванных задач STT/резюме: %d",
                     reset_count)
        self._ready.set()
        log.info("Приложение запущено. Записи: %s", self.cfg.records_dir)

        maintenance_tasks = [
            asyncio.create_task(self._watchdog(), name="watchdog"),
            asyncio.create_task(self._retention(), name="retention"),
        ]
        recovery_task = asyncio.create_task(
            self._startup_recovery(stale_dirs), name="recovery")

        await self._stop_evt.wait()
        log.info("Завершение работы…")
        self._shutting_down = True
        transcribe.request_stop()  # прерывает ожидание результата STT
        self.sm.begin_shutdown()
        await ws.stop()
        for t in maintenance_tasks:
            t.cancel()
        await asyncio.gather(*maintenance_tasks, return_exceptions=True)
        if self._queue_task is not None and not self._queue_task.done():
            self._queue_task.cancel()
            await asyncio.gather(self._queue_task, return_exceptions=True)
        results = await asyncio.gather(
            self.sm.shutdown(), recovery_task, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                log.error("Ошибка безопасного завершения",
                          exc_info=(type(result), result, result.__traceback__))
        for task in list(self._postprocess_tasks):
            task.cancel()
        if self._postprocess_tasks:
            await asyncio.gather(*list(self._postprocess_tasks),
                                 return_exceptions=True)
        log.info("Приложение остановлено")
        if self.tray is not None:
            self.tray.stop()

    # ------------------------------------------------- фоновые задачи

    async def _watchdog(self) -> None:
        cfg_path = self.cfg.appdata_dir / "config.toml"
        try:
            last_mtime = cfg_path.stat().st_mtime
        except OSError:
            last_mtime = 0.0
        while True:
            await asyncio.sleep(5)
            try:
                await self.sm.check_timeouts()
            except Exception:
                log.exception("Ошибка watchdog")
            # Окно настроек — отдельный процесс: подхватываем правки config.toml.
            try:
                mtime = cfg_path.stat().st_mtime
            except OSError:
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                try:
                    self._reload_config()
                except Exception:
                    log.exception("Не удалось перечитать настройки")
            # Ручные задачи «На транскрибацию» из окна настроек.
            if transcribe.queue_path().exists():
                self._ensure_queue_task()

    def _reload_config(self) -> None:
        new = load_config()
        if new.ws_port != self.cfg.ws_port:
            log.warning("ws_port изменён (%d -> %d) — нужен перезапуск приложения",
                        self.cfg.ws_port, new.ws_port)
        for f in ("records_dir", "allowed_domains", "auto_record", "ffmpeg_path",
                  "video_enabled", "video_mode", "video_monitor", "video_fps",
                  "video_crf", "video_preset", "video_encoder",
                  "video_quality_preset", "av1_preset", "av1_crf",
                  "respect_mic_mute", "mic_denoise", "echo_duck",
                  "tr_enabled", "tr_url", "tr_poll_s",
                  "tr_timeout_min", "sum_enabled", "sum_url", "sum_model",
                  "sum_temperature", "sum_timeout_min",
                  "keep_raw", "delete_stems", "delete_mux_log",
                  "retention_days"):
            setattr(self.cfg, f, getattr(new, f))
        if self._retention_changed is not None:
            self._retention_changed.set()
        log.info("Настройки перечитаны: записи=%s, видео=%s/%s, STT=%s",
                 self.cfg.records_dir,
                 self.cfg.video_mode if self.cfg.video_enabled else "off",
                 self.cfg.video_monitor, self.cfg.tr_enabled)
        self.sm._refresh_tray()

    async def _startup_recovery(self, stale_dirs: list[Path]) -> None:
        """Финализирует записи, оборванные падением приложения/системы."""
        await asyncio.sleep(2)
        for call_dir in stale_dirs:
            call = CallLog.from_dir(call_dir)
            if call is None:
                continue
            try:
                await self._recover_one(call)
            except Exception as e:
                log.exception("Не удалось восстановить запись %s", call_dir.name)
                call.set_status("error", error=f"recovery: {e}")
                call.write()
        if transcribe.queue_path().exists():
            self._ensure_queue_task()

    def _ensure_queue_task(self) -> None:
        """Запускает ровно одного обработчика файловой очереди."""
        if self._shutting_down:
            return
        if self._queue_task is not None and not self._queue_task.done():
            return
        task = asyncio.create_task(
            self._drain_transcribe_queue(), name="transcribe-queue")
        self._queue_task = task

        def done(finished: asyncio.Task) -> None:
            if self._queue_task is finished:
                self._queue_task = None
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                log.error("Обработчик очереди завершился с ошибкой",
                          exc_info=(type(exc), exc, exc.__traceback__))

        task.add_done_callback(done)

    async def _drain_transcribe_queue(self) -> None:
        """Обрабатывает задачи по одной и подтверждает только после попытки."""
        try:
            while not self._shutting_down:
                task = transcribe.peek_task()
                if task is None:
                    return
                try:
                    processed = await self._process_queue_task(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Ошибка ручной задачи: %r", task)
                    processed = True
                if not processed:
                    return
                transcribe.ack_task(task)
        finally:
            self.sm._refresh_tray()

    async def _process_queue_task(self, task: dict) -> bool:
        raw_dir, action = task.get("dir"), task["action"]
        if action not in ("stt", "summary", "stt_summary"):
            log.warning("Очередь задач: неизвестное действие %r — пропуск", action)
            return True
        call_dir = Path(raw_dir) if raw_dir else None
        call = CallLog.from_dir(call_dir) if call_dir else None
        if call is None or not call_dir.exists():
            log.warning("Очередь задач: запись %s без папки — пропуск", raw_dir)
            return True
        if call.status in ("recording", "finalizing",
                           "transcribing", "summarizing"):
            log.info("Очередь задач: запись «%s» ещё занята (%s) — отложено",
                     call.room, call.status)
            return False
        room = call.room
        tray = (self.set_tray_state if self.sm.call is None else None)
        log.info("Очередь задач: %s для записи «%s»", action, room)

        if action == "summary":
            tp = call_dir / "transcript.txt"
            if not tp.exists():
                self.notify("Резюме невозможно",
                            f"«{room}»: нет transcript.txt — сначала "
                            f"выполните транскрибацию")
                return True
            await summarize.maybe_summarize(
                self.cfg, call, self.notify, set_tray=tray, force=True)
            return True

        if tray:
            tray("transcribing", f"«{room}»: распознавание речи…")
        text = await transcribe.transcribe_call(self.cfg, call, self.notify)
        if text:
            await summarize.maybe_summarize(
                self.cfg, call, self.notify, set_tray=tray, transcript=text,
                force=(action == "stt_summary"))
        return True

    async def _recover_one(self, call: CallLog) -> None:
        call_dir = call.call_dir
        started_ts = _iso_to_ts(call.started_at)
        if not call.recorded or call_dir is None:
            end_ts = started_ts
            if call_dir is not None:
                try:
                    end_ts = max(started_ts,
                                 (call_dir / records.META_NAME).stat().st_mtime)
                except OSError:
                    pass
            if not call.ended_at:
                call.finish(end_ts, started_ts, "crash")
            call.close_open_participants(end_ts)
            call.set_status("done")
            call.add_event(time.time(), "recovered_after_crash", None)
            call.write()
            return
        seg = SegmentLog.load(call_dir / "segments.json")
        files = [call_dir / s["path"] for s in seg["audio"] + seg["video"]
                 if (call_dir / s["path"]).exists()]
        if not files:
            if not call.ended_at:
                call.finish(started_ts, started_ts, "crash")
            call.set_status("error", error="recovery: нет сегментов")
            call.write()
            return
        end_ts = max(f.stat().st_mtime for f in files)
        if not call.ended_at:
            call.finish(end_ts, started_ts, "crash")
        call.close_open_participants(end_ts)
        log.info("Восстанавливаю запись «%s» (%s)", call.room, call_dir.name)
        call.set_status("finalizing")
        call.write()
        mute = self._mute_intervals_from_events(call, end_ts)
        result = await mux.finalize_call(self.cfg, call_dir, seg,
                                         mute if self.cfg.respect_mic_mute else [])
        call.set_status("done")
        call.add_event(time.time(), "recovered_after_crash", None)
        call.write()
        self.notify("Запись восстановлена",
                    f"«{call.room}» — собрана после сбоя")
        if self.cfg.tr_enabled and result and not self._shutting_down:
            task = asyncio.create_task(
                self._postprocess_recovered(call),
                name=f"recovery-postprocess-{call_dir.name}")
            self._postprocess_tasks.add(task)

            def done(finished: asyncio.Task) -> None:
                self._postprocess_tasks.discard(finished)
                if finished.cancelled():
                    return
                exc = finished.exception()
                if exc is not None:
                    log.error("Постобработка восстановленной записи «%s» упала",
                              call.room,
                              exc_info=(type(exc), exc, exc.__traceback__))

            task.add_done_callback(done)

    async def _postprocess_recovered(self, call: CallLog) -> None:
        text = await transcribe.transcribe_call(self.cfg, call, self.notify)
        if text:
            await summarize.maybe_summarize(
                self.cfg, call, self.notify, transcript=text)

    @staticmethod
    def _mute_intervals_from_events(call: CallLog, end_ts: float) -> list:
        intervals = []
        open_ts: float | None = None
        for ev in call.events:
            if ev["type"] == "mic_muted":
                open_ts = _iso_to_ts(ev["ts"])
            elif ev["type"] == "mic_unmuted" and open_ts is not None:
                intervals.append((open_ts, _iso_to_ts(ev["ts"])))
                open_ts = None
        if open_ts is not None:
            intervals.append((open_ts, end_ts))
        return intervals

    async def _retention(self) -> None:
        while True:
            try:
                days = self.cfg.retention_days
                if days > 0:
                    self._run_retention(days)
            except Exception:
                log.exception("Ошибка ретеншна")
            try:
                assert self._retention_changed is not None
                await asyncio.wait_for(self._retention_changed.wait(),
                                       timeout=3600)
                self._retention_changed.clear()
            except TimeoutError:
                pass

    def _run_retention(self, days: int) -> None:
        """Удаляет завершённые папки записей старше N дней внутри records_dir."""
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        records_root = self.cfg.records_dir.resolve()
        active_dir = None
        if self.sm.call and self.sm.call.call_dir:
            active_dir = self.sm.call.call_dir.resolve()
        for raw_dir, _call in records.retention_candidates(self.cfg.records_dir):
            try:
                call_dir = raw_dir.resolve()
            except OSError:
                log.warning("Ретеншн: не удалось разрешить путь записи: %s", raw_dir)
                continue
            # Кандидаты — только прямые подпапки records_dir; проверка на всякий
            # случай не даёт удалить дерево вне текущей папки (симлинки и т.п.).
            if call_dir.parent != records_root:
                continue
            if active_dir is not None and call_dir == active_dir:
                continue
            # Захват держим до завершения удаления: GUI и постобработка
            # используют тот же межпроцессный файловый lock.
            if not records.try_acquire(call_dir):
                continue
            try:
                meta = records.read_meta(call_dir)
                if meta is None or not records.is_owned_meta(meta):
                    continue
                call = meta.get("call") or {}
                if str(call.get("status") or "") not in records.DELETABLE_STATUSES:
                    continue
                ended_ts = _iso_to_ts(
                    call.get("ended_at") or call.get("started_at"))
                if not ended_ts:
                    try:
                        ended_ts = call_dir.stat().st_mtime
                    except OSError:
                        continue
                if ended_ts >= cutoff or not call_dir.is_dir():
                    continue
                log.info("Ретеншн: удаляю старую запись: %s", call_dir.name)
                try:
                    shutil.rmtree(call_dir)
                except OSError:
                    log.exception("Ретеншн: не удалось удалить %s", call_dir)
            finally:
                records.release(call_dir)
