"""Машина состояний созвона: снапшоты от расширения -> журнал + управление записью.

Состояния активного созвона:
  active     — пользователь в конференции, идёт запись (если auto_record);
  grace      — пользователь вышел/вкладка закрылась; ждём grace_seconds на случай
               перезагрузки страницы (F5), запись продолжается;
  finalizing — остановка рекордеров и сборка итоговых файлов.

Второй одновременный созвон не записывается — только журналируется (log_only).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from app import summarize, transcribe
from app.config import Config
from app.records import CallLog
from app.recorder import mux
from app.recorder.audio import AudioRecorder
from app.recorder.segments import SegmentLog
from app.recorder.video import VideoRecorder

log = logging.getLogger(__name__)

CHROME_TITLE_SUFFIXES = (" - Google Chrome", " – Google Chrome")


def strip_chrome_suffix(title: str) -> str:
    for s in CHROME_TITLE_SUFFIXES:
        if title.endswith(s):
            return title[: -len(s)]
    return title


def safe_name(s: str, max_len: int = 50) -> str:
    s = re.sub(r"[^\w\-. а-яА-ЯёЁ]", "_", s, flags=re.UNICODE).strip(" ._")
    return s[:max_len] or "room"


def create_call_dir(records_dir: Path, room: str, started_ts: float) -> Path:
    """Создаёт уникальную папку журнала, даже если медиа не записывается."""
    stamp = datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d_%H-%M-%S")
    base = f"{stamp}_{safe_name(room)}"
    for index in range(1000):
        suffix = "" if index == 0 else f"_{index:02d}"
        candidate = records_dir / f"{base}{suffix}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"Не удалось подобрать свободную папку для созвона «{room}»")


@dataclass
class TabState:
    snapshot: dict
    last_seen: float  # time.monotonic()


@dataclass
class LogOnlyCall:
    log: CallLog
    participants: dict[str, dict] = field(default_factory=dict)

    @property
    def room(self) -> str:
        return self.log.room

    @property
    def started_ts(self) -> float:
        return self.log.started_ts


@dataclass
class ActiveCall:
    call_id: int
    tab_id: int
    room: str
    url: str
    title: str
    started_ts: float
    call_dir: Path | None
    recorded: bool
    state: str = "active"  # active | grace | finalizing
    left_ts: float | None = None
    participants: dict[str, dict] = field(default_factory=dict)
    audio_muted: bool | None = None
    mute_open_ts: float | None = None
    mute_intervals: list[tuple[float, float]] = field(default_factory=list)
    seglog: SegmentLog | None = None
    rec_mic: AudioRecorder | None = None
    rec_spk: AudioRecorder | None = None
    rec_video: VideoRecorder | None = None
    log: CallLog | None = None

    def __post_init__(self) -> None:
        if self.log is None:
            self.log = CallLog(
                room=self.room, url=self.url, tab_id=self.tab_id,
                started_ts=self.started_ts, call_dir=self.call_dir,
                recorded=self.recorded)


class SessionManager:
    def __init__(self, cfg: Config, *, notify=None, set_tray=None):
        self.cfg = cfg
        self._seq = 0  # порядковый номер созвона за текущий запуск (для логов)
        # notify(title, msg); set_tray(state, text), state:
        # idle|paused|logging|recording|grace|finalizing|transcribing
        self.notify = notify or (lambda title, msg: None)
        self.set_tray = set_tray or (lambda state, text: None)
        self.paused = False
        self.tabs: dict[int, TabState] = {}
        self.call: ActiveCall | None = None
        self.log_only: dict[int, LogOnlyCall] = {}
        self._grace_task: asyncio.Task | None = None
        self._finalize_tasks: set[asyncio.Task] = set()
        self._postprocess_tasks: set[asyncio.Task] = set()
        self._ignored_hosts: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutting_down = False

    # ------------------------------------------------------------- входящие

    async def handle_message(self, msg: dict) -> None:
        if self._shutting_down:
            return
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        mtype = msg.get("type")
        now = time.time()
        if mtype == "snapshot":
            await self._on_snapshot(msg, now)
        elif mtype == "tab_closed":
            await self._on_tab_closed(int(msg.get("tab_id", -1)), now)

    async def _on_snapshot(self, msg: dict, now: float) -> None:
        try:
            tab_id = int(msg.get("tab_id"))
        except (TypeError, ValueError):
            return
        url = str(msg.get("url") or "")
        host = (urlparse(url).hostname or "").lower()
        if host not in self.cfg.allowed_domains:
            if host and host not in self._ignored_hosts:
                self._ignored_hosts.add(host)
                log.info("Снапшоты с домена %s игнорируются (нет в allowed_domains)", host)
            return

        snap = {
            "joined": bool(msg.get("joined")),
            "room": str(msg.get("room") or ""),
            "participants": msg.get("participants"),  # list | None
            "audioMuted": msg.get("audioMuted"),      # bool | None
            "title": str(msg.get("title") or ""),
            "url": url,
            "via": msg.get("via"),
        }
        prev = self.tabs.get(tab_id)
        prev_joined = bool(prev.snapshot.get("joined")) if prev else False
        self.tabs[tab_id] = TabState(snap, time.monotonic())

        if snap["joined"] and not prev_joined:
            await self._on_joined(tab_id, snap, now)
        elif not snap["joined"] and prev_joined:
            await self._on_unjoined(tab_id, now, reason="left")

        # Обновление деталей по активному созвону / log-only
        c = self.call
        if c and c.tab_id == tab_id and c.state in ("active", "grace") and snap["joined"]:
            self._update_call_details(c, snap, now)
        lo = self.log_only.get(tab_id)
        if lo and snap["joined"]:
            self._update_log_only(lo, snap, now)

    async def _on_tab_closed(self, tab_id: int, now: float) -> None:
        self.tabs.pop(tab_id, None)
        c = self.call
        if c and c.tab_id == tab_id and c.state == "active":
            await self._enter_grace(c, "tab_closed", now)
        lo = self.log_only.pop(tab_id, None)
        if lo:
            self._close_log_only(lo, now, "tab_closed")

    # ------------------------------------------------------------- переходы

    async def _on_joined(self, tab_id: int, snap: dict, now: float) -> None:
        room = snap["room"] or "room"
        c = self.call
        if c is not None:
            if c.state == "grace" and c.room == room:
                # Возврат после F5/переоткрытия — продолжаем ту же сессию.
                if self._grace_task:
                    self._grace_task.cancel()
                    self._grace_task = None
                c.tab_id = tab_id
                c.log.tab_id = tab_id
                c.state = "active"
                c.left_ts = None
                c.log.add_event(now, "rejoined", {"tab_id": tab_id})
                c.log.write()
                log.info("Возврат в созвон «%s» — сессия #%d продолжается", room, c.call_id)
                self._refresh_tray()
                return
            if c.tab_id == tab_id and c.state == "active":
                return  # дубль
            # Параллельный второй созвон — только журнал, но его meta.json
            # сохраняется так же надёжно, как журнал записываемого созвона.
            if tab_id not in self.log_only:
                call_dir = create_call_dir(self.cfg.records_dir, room, now)
                lo_log = CallLog(room=room, url=snap["url"], tab_id=tab_id,
                                 started_ts=now, call_dir=call_dir,
                                 recorded=False)
                lo_log.add_event(now, "concurrent_conference",
                                 {"recording_call_id": c.call_id})
                lo_log.write()
                self.log_only[tab_id] = LogOnlyCall(lo_log)
                log.warning("Второй одновременный созвон «%s» — только журнал", room)
                self.notify("Второй созвон не записывается",
                            f"Идёт запись «{c.room}»; «{room}» попадёт только в журнал")
            return

        if self.paused:
            log.info("Обнаружение на паузе — созвон «%s» пропущен", room)
            return

        # Новый созвон.
        recorded = self.cfg.auto_record
        call_dir = create_call_dir(self.cfg.records_dir, room, now)
        self._seq += 1
        call_id = self._seq
        c = ActiveCall(call_id=call_id, tab_id=tab_id, room=room, url=snap["url"],
                       title=snap["title"], started_ts=now, call_dir=call_dir,
                       recorded=recorded)
        self.call = c
        c.log.add_event(now, "conference_joined",
                        {"room": room, "url": snap["url"], "via": snap.get("via")})
        log.info("Созвон начался: «%s» (#%d), запись=%s", room, call_id, recorded)
        # Пишем сразу для любого режима, включая журнал без медиа.
        if not c.log.write():
            self.notify("Ошибка журнала",
                        f"«{room}»: не удалось сохранить meta.json")
        if recorded:
            try:
                await asyncio.to_thread(self._start_media, c)
            except Exception:
                log.exception("Не удалось запустить запись")
                c.log.add_event(time.time(), "record_start_failed", None)
                c.log.write()
            self.notify("Запись началась", f"Созвон «{room}»")
        else:
            self.notify("Созвон начался", f"«{room}» (журнал без записи)")
        self._refresh_tray()

    async def _on_unjoined(self, tab_id: int, now: float, reason: str) -> None:
        c = self.call
        if c and c.tab_id == tab_id and c.state == "active":
            await self._enter_grace(c, reason, now)
        lo = self.log_only.pop(tab_id, None)
        if lo:
            self._close_log_only(lo, now, reason)

    async def _enter_grace(self, c: ActiveCall, reason: str, now: float) -> None:
        c.state = "grace"
        c.left_ts = now
        c.log.add_event(now, "conference_left", {"reason": reason})
        c.log.write()
        log.info("Выход из созвона «%s» (%s) — grace %.0f с", c.room, reason,
                 self.cfg.grace_seconds)
        self._refresh_tray()
        if self._grace_task:
            self._grace_task.cancel()
        self._grace_task = asyncio.create_task(self._grace_timer(c, reason))

    async def _grace_timer(self, c: ActiveCall, reason: str) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(self.cfg.grace_seconds)
        except asyncio.CancelledError:
            return
        finally:
            if self._grace_task is current:
                self._grace_task = None
        if self.call is c and c.state == "grace":
            self._schedule_finalize(c, reason)

    # ------------------------------------------------------------- watchdog

    async def check_timeouts(self) -> None:
        """Вызывается периодически: закрывает сессии без heartbeat (Chrome убит)."""
        now_m = time.monotonic()
        now = time.time()
        c = self.call
        if c and c.state == "active":
            ts = self.tabs.get(c.tab_id)
            if ts is None or now_m - ts.last_seen > self.cfg.heartbeat_timeout:
                log.warning("Heartbeat от вкладки %d пропал — закрываю сессию", c.tab_id)
                await self._enter_grace(c, "timeout", now)
        for tab_id, lo in list(self.log_only.items()):
            ts = self.tabs.get(tab_id)
            if ts is None or now_m - ts.last_seen > self.cfg.heartbeat_timeout:
                self.log_only.pop(tab_id, None)
                self._close_log_only(lo, now, "timeout")
        # Подчистка давно молчащих вкладок.
        for tab_id in [t for t, ts in self.tabs.items()
                       if now_m - ts.last_seen > 10 * self.cfg.heartbeat_timeout]:
            self.tabs.pop(tab_id, None)

    # ------------------------------------------------------------- запись

    def _start_media(self, c: ActiveCall) -> None:
        """Запускает рекордеры (вызывается в отдельном потоке)."""
        assert c.call_dir is not None
        c.seglog = SegmentLog(c.call_dir / "segments.json")
        c.rec_mic = AudioRecorder(c.call_dir, "mic", c.seglog, self._thread_event(c))
        c.rec_spk = AudioRecorder(c.call_dir, "speakers", c.seglog, self._thread_event(c))
        c.rec_mic.start()
        c.rec_spk.start()
        if self.cfg.video_enabled:
            c.rec_video = VideoRecorder(c.call_dir, self.cfg, c.seglog,
                                        self._thread_event(c))
            c.rec_video.start(self._needles(c))

    def _needles(self, c: ActiveCall) -> list[str]:
        out = []
        t = strip_chrome_suffix(c.title).strip()
        if len(t) >= 4:
            out.append(t)
        if c.room and len(c.room) >= 4:
            out.append(c.room)
        return out or [c.room or "Jitsi Meet"]

    def _thread_event(self, c: ActiveCall):
        """Колбэк для рекордеров (из их потоков): событие в журнал через loop."""
        loop = self._loop

        def persist(etype: str, payload: dict | None) -> None:
            c.log.add_event(time.time(), etype, payload)
            c.log.write()

        def cb(etype: str, payload: dict | None) -> None:
            if loop is None or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(persist, etype, payload)
            except RuntimeError:
                pass  # loop уже закрыт

        return cb

    def _stop_media(self, c: ActiveCall) -> None:
        """Останавливает рекордеры (в отдельном потоке); сегменты уже в seglog."""
        for rec in (c.rec_video, c.rec_mic, c.rec_spk):
            if rec is not None:
                try:
                    rec.stop()
                except Exception:
                    log.exception("Ошибка остановки рекордера %r", rec)

    # ------------------------------------------------------------- детали

    def _update_call_details(self, c: ActiveCall, snap: dict, now: float) -> None:
        changed = False
        if snap["title"] and snap["title"] != c.title:
            c.title = snap["title"]
            if c.rec_video:
                c.rec_video.update_needles(self._needles(c))
        changed |= self._diff_participants(
            c.log, c.participants, snap.get("participants"), now)
        muted = snap.get("audioMuted")
        if muted is not None and muted != c.audio_muted:
            first_known = c.audio_muted is None
            c.audio_muted = muted
            changed = True
            if muted:
                c.mute_open_ts = now
                c.log.add_event(now, "mic_muted", None)
            elif not first_known:  # переход из «неизвестно» в False — не событие
                if c.mute_open_ts is not None:
                    c.mute_intervals.append((c.mute_open_ts, now))
                    c.mute_open_ts = None
                c.log.add_event(now, "mic_unmuted", None)
        if changed:
            c.log.write()

    def _update_log_only(self, lo: LogOnlyCall, snap: dict, now: float) -> None:
        if self._diff_participants(
                lo.log, lo.participants, snap.get("participants"), now):
            lo.log.write()

    def _diff_participants(self, clog: CallLog, current: dict[str, dict],
                           new_list, now: float) -> bool:
        if not isinstance(new_list, list):
            return False
        changed = False
        new = {}
        for p in new_list:
            if isinstance(p, dict) and p.get("id"):
                pid = str(p["id"])
                new[pid] = {"name": str(p.get("name") or ""),
                            "local": bool(p.get("local"))}
        for pid, info in new.items():
            old = current.get(pid)
            if old is None:
                changed |= clog.participant_joined(
                    pid, info["name"], info["local"], now)
                clog.add_event(now, "participant_joined",
                               {"id": pid, "name": info["name"],
                                "local": info["local"]})
                changed = True
            elif info["name"] and old.get("name") != info["name"]:
                changed |= clog.participant_joined(
                    pid, info["name"], info["local"], now)
        for pid in list(current.keys() - new.keys()):
            name = current[pid].get("name", "")
            changed |= clog.participant_left(pid, now)
            clog.add_event(now, "participant_left", {"id": pid, "name": name})
            changed = True
        current.clear()
        current.update(new)
        return changed

    def _close_log_only(self, lo: LogOnlyCall, now: float, reason: str) -> None:
        lo.log.close_open_participants(now)
        lo.log.finish(now, lo.started_ts, reason)
        lo.log.set_status("done")
        lo.log.write()

    # ------------------------------------------------------------- финализация

    @staticmethod
    def _track_task(task: asyncio.Task, registry: set[asyncio.Task],
                    label: str) -> asyncio.Task:
        """Держит сильную ссылку на задачу и забирает её исключение."""
        registry.add(task)

        def done(finished: asyncio.Task) -> None:
            registry.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                log.error("Фоновая задача «%s» завершилась с ошибкой",
                          label,
                          exc_info=(type(exc), exc, exc.__traceback__))

        task.add_done_callback(done)
        return task

    def _schedule_finalize(self, c: ActiveCall, reason: str) -> asyncio.Task | None:
        """Отделяет медиасборку от grace-таймера, чтобы её нельзя было отменить."""
        if c.state == "finalizing":
            return None
        c.state = "finalizing"
        if self.call is c:
            self.call = None
        task = asyncio.create_task(
            self._finalize(c, reason), name=f"finalize-{c.call_id}")
        return self._track_task(task, self._finalize_tasks,
                                f"финализация #{c.call_id}")

    def _schedule_postprocess(self, c: ActiveCall) -> None:
        if self._shutting_down:
            return
        task = asyncio.create_task(
            self._postprocess(c), name=f"postprocess-{c.call_id}")
        self._track_task(task, self._postprocess_tasks,
                         f"постобработка #{c.call_id}")

    async def _finalize(self, c: ActiveCall, reason: str) -> None:
        end_ts = c.left_ts or time.time()
        if c.mute_open_ts is not None:
            c.mute_intervals.append((c.mute_open_ts, end_ts))
            c.mute_open_ts = None
        c.log.close_open_participants(end_ts)
        c.log.finish(end_ts, c.started_ts, reason)
        dur = int(end_ts - c.started_ts)
        log.info("Созвон «%s» завершён (%s), длительность %d:%02d",
                 c.room, reason, dur // 60, dur % 60)
        self.set_tray("finalizing", f"«{c.room}»: сборка записи…")

        if c.recorded and c.call_dir is not None:
            c.log.set_status("finalizing")
            await asyncio.to_thread(self._stop_media, c)
            finalized = False
            try:
                mute = c.mute_intervals if self.cfg.respect_mic_mute else []
                result = await mux.finalize_call(self.cfg, c.call_dir,
                                                 SegmentLog.load(c.call_dir / "segments.json"),
                                                 mute)
                c.log.set_files(**result)
                c.log.set_status("done")
                finalized = True
                self.notify("Запись сохранена",
                            f"«{c.room}» · {dur // 60} мин {dur % 60} с")
            except Exception as e:
                log.exception("Ошибка финализации записи «%s»", c.room)
                c.log.set_status("error", error=str(e))
                self.notify("Ошибка сборки записи", f"«{c.room}»: {e}")
        else:
            c.log.set_status("done")
            self.notify("Созвон завершён", f"«{c.room}» · {dur // 60} мин {dur % 60} с")

        c.log.write()
        self._refresh_tray()
        # Долгую сетевую постобработку не держим внутри задачи медиасборки:
        # при выходе ждём только безопасного закрытия и сборки локальных файлов.
        if (c.recorded and c.call_dir is not None and finalized
                and self.cfg.tr_enabled and reason != "app_exit"):
            self._schedule_postprocess(c)

    async def _postprocess(self, c: ActiveCall) -> None:
        assert c.call_dir is not None
        try:
            if self.call is None:
                self.set_tray("transcribing",
                              f"«{c.room}»: распознавание речи…")
            text = await transcribe.transcribe_call(self.cfg, c.log, self.notify)
            if text:
                await summarize.maybe_summarize(
                    self.cfg, c.log, self.notify,
                    set_tray=self.set_tray if self.call is None else None,
                    transcript=text)
        finally:
            c.log.write()
            self._refresh_tray()

    # ------------------------------------------------------------- сервис

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        log.info("Пауза обнаружения: %s", paused)
        self._refresh_tray()

    def begin_shutdown(self) -> None:
        """Запрещает новым WS-сообщениям создавать сессии при остановке."""
        self._shutting_down = True

    async def shutdown(self) -> None:
        """Дожидается локальной медиасборки и отменяет только сетевые задачи."""
        self.begin_shutdown()
        if self._grace_task:
            grace_task = self._grace_task
            self._grace_task = None
            grace_task.cancel()
            await asyncio.gather(grace_task, return_exceptions=True)
        c = self.call
        if c is not None and c.state in ("active", "grace"):
            if c.left_ts is None:
                c.left_ts = time.time()
            self._schedule_finalize(c, "app_exit")
        for tab_id, lo in list(self.log_only.items()):
            self._close_log_only(lo, time.time(), "app_exit")
        self.log_only.clear()
        if self._finalize_tasks:
            await asyncio.gather(*list(self._finalize_tasks),
                                 return_exceptions=True)
        for task in list(self._postprocess_tasks):
            task.cancel()
        if self._postprocess_tasks:
            await asyncio.gather(*list(self._postprocess_tasks),
                                 return_exceptions=True)

    def _refresh_tray(self) -> None:
        c = self.call
        if c is None:
            if self.paused:
                self.set_tray("paused", "Пауза обнаружения")
            else:
                self.set_tray("idle", "Ожидание созвона")
        elif c.state == "grace":
            self.set_tray("grace", f"«{c.room}»: ожидание возврата…")
        elif c.state == "finalizing":
            self.set_tray("finalizing", f"«{c.room}»: сборка записи…")
        elif c.recorded:
            self.set_tray("recording", f"«{c.room}»: идёт запись")
        else:
            self.set_tray("logging", f"«{c.room}»: журнал (без записи)")
