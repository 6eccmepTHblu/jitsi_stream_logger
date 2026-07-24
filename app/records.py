"""Журнал созвонов на файлах: единственный источник правды — meta.json в папке
записи. БД больше нет; список записей, восстановление после сбоя и ретеншн
получают данные, разбирая папку records_dir.

Активный созвон ведёт `CallLog` в памяти (события, участники, статусы) и по мере
изменений переписывает <call_dir>/meta.json. Ручные задачи и восстановление
после сбоя воссоздают `CallLog` из уже лежащего на диске meta.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import msvcrt
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

META_NAME = "meta.json"
META_APP_ID = "jitsi_stream_logger"
META_SCHEMA_VERSION = 1

# Статусы записи. Совпадают с прежними статусами БД для совместимости meta.json.
ACTIVE_STATUSES = ("recording", "finalizing")
PROCESSING_STATUSES = ("transcribing", "summarizing")
DELETABLE_STATUSES = frozenset({"done", "log_only", "error"})
KNOWN_STATUSES = frozenset(
    {*ACTIVE_STATUSES, *PROCESSING_STATUSES, *DELETABLE_STATUSES})


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ts_iso(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts).astimezone().isoformat(timespec="seconds")


def iso_to_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _write_json_atomic(path: Path, data: dict | list) -> None:
    """Атомарно заменяет JSON-файл, не оставляя обрезанную основную копию."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def is_owned_meta(meta: object) -> bool:
    """Проверяет явный маркер формата журнала этого приложения."""
    if not isinstance(meta, dict):
        return False
    fmt = meta.get("format")
    return (
        isinstance(fmt, dict)
        and fmt.get("app_id") == META_APP_ID
        and fmt.get("schema_version") == META_SCHEMA_VERSION
    )


def _is_legacy_meta(meta: object) -> bool:
    """Строго распознаёт старый журнал без маркера формата — по форме секций.

    Пути к файлам и папке (dir) в meta.json больше не хранятся и не проверяются;
    от чужого meta.json защищают обязательный набор полей секции call, известный
    статус и наличие списков participants/events.
    """
    if not isinstance(meta, dict):
        return False
    if "format" in meta:
        return False
    call = meta.get("call")
    participants = meta.get("participants")
    events = meta.get("events")
    if not isinstance(call, dict):
        return False
    if not isinstance(participants, list) or not isinstance(events, list):
        return False
    required = {
        "room", "url", "tab_id", "started_at", "ended_at", "duration_sec",
        "end_reason", "status", "recorded", "error",
    }
    if not required.issubset(call):
        return False
    if str(call.get("status") or "") not in KNOWN_STATUSES:
        return False
    return True


def _valid_owned_meta(meta: object) -> bool:
    if not is_owned_meta(meta):
        return False
    call = meta.get("call")
    if not isinstance(call, dict):
        return False
    if str(call.get("status") or "") not in KNOWN_STATUSES:
        return False
    return (
        isinstance(meta.get("participants"), list)
        and isinstance(meta.get("events"), list)
    )


# --------------------------------------------------------------- модель созвона

@dataclass
class CallLog:
    """Журнал одного созвона в памяти + сериализация в meta.json.

    `call_dir` указывает на папку журнала и для записываемого созвона, и для
    режима без медиа. Поэтому write() всегда сохраняет историю в meta.json.
    """
    room: str
    url: str = ""
    tab_id: int | None = None
    started_ts: float = 0.0
    call_dir: Path | None = None
    recorded: bool = False
    status: str = ""
    started_at: str = ""
    ended_at: str | None = None
    duration_sec: float | None = None
    end_reason: str | None = None
    error: str | None = None
    participants: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = ts_iso(self.started_ts) if self.started_ts else now_iso()
        if not self.status:
            self.status = "recording" if self.recorded else "log_only"
        if isinstance(self.call_dir, str):
            self.call_dir = Path(self.call_dir)

    # --- статусы и времена ---

    def set_status(self, status: str, error: str | None = None) -> None:
        self.status = status
        if error is not None:
            self.error = error

    def finish(self, ended_ts: float, started_ts: float, reason: str) -> None:
        self.ended_at = ts_iso(ended_ts)
        self.duration_sec = round(max(0.0, ended_ts - started_ts), 1)
        self.end_reason = reason

    # --- события ---

    def add_event(self, ts: float, etype: str, payload: dict | None = None) -> None:
        self.events.append({"ts": ts_iso(ts), "type": etype, "payload": payload})

    # --- участники ---

    def participant_joined(self, jitsi_id: str, name: str,
                           is_local: bool, ts: float) -> bool:
        for p in self.participants:
            if p["jitsi_id"] == jitsi_id and p.get("left_at") is None:
                if name and p.get("name") != name:
                    p["name"] = name
                    return True
                return False
        self.participants.append({
            "jitsi_id": jitsi_id, "name": name, "is_local": bool(is_local),
            "joined_at": ts_iso(ts), "left_at": None,
        })
        return True

    def participant_left(self, jitsi_id: str, ts: float) -> bool:
        for p in self.participants:
            if p["jitsi_id"] == jitsi_id and p.get("left_at") is None:
                p["left_at"] = ts_iso(ts)
                return True
        return False

    def close_open_participants(self, ts: float) -> None:
        stamp = ts_iso(ts)
        for p in self.participants:
            if p.get("left_at") is None:
                p["left_at"] = stamp

    # --- сериализация ---

    def call_dict(self) -> dict:
        return {
            "room": self.room,
            "url": self.url,
            "tab_id": self.tab_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_sec": self.duration_sec,
            "end_reason": self.end_reason,
            "status": self.status,
            "recorded": int(self.recorded),
            "error": self.error,
        }

    def to_meta(self) -> dict:
        return {
            "format": {
                "app_id": META_APP_ID,
                "schema_version": META_SCHEMA_VERSION,
            },
            "call": self.call_dict(),
            "participants": list(self.participants),
            "events": list(self.events),
        }

    def write(self) -> bool:
        """Сохраняет журнал атомарно и сообщает вызывающему об ошибке."""
        if self.call_dir is None:
            return False
        try:
            _write_json_atomic(self.call_dir / META_NAME, self.to_meta())
            return True
        except Exception:
            log.exception("Не удалось записать meta.json в %s", self.call_dir)
            return False

    @classmethod
    def from_meta(cls, meta: dict, call_dir: Path) -> "CallLog":
        call = meta.get("call") or {}
        log_obj = cls(
            room=str(call.get("room") or ""),
            url=str(call.get("url") or ""),
            tab_id=call.get("tab_id"),
            started_ts=iso_to_ts(call.get("started_at")),
            call_dir=call_dir,
            recorded=bool(call.get("recorded")),
            status=str(call.get("status") or "done"),
            started_at=str(call.get("started_at") or ""),
            ended_at=call.get("ended_at"),
            duration_sec=call.get("duration_sec"),
            end_reason=call.get("end_reason"),
            error=call.get("error"),
            participants=list(meta.get("participants") or []),
            events=list(meta.get("events") or []),
        )
        return log_obj

    @classmethod
    def from_dir(cls, call_dir: Path) -> "CallLog | None":
        meta = read_meta(call_dir, migrate_legacy=True)
        if meta is None:
            return None
        return cls.from_meta(meta, call_dir)


# --------------------------------------------------------------- разбор папки

def read_meta(call_dir: Path, *, migrate_legacy: bool = False) -> dict | None:
    """Читает только собственный журнал или строго распознанный старый формат."""
    path = call_dir / META_NAME
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if _valid_owned_meta(meta):
        return meta
    if not _is_legacy_meta(meta):
        return None
    if migrate_legacy:
        migrated = dict(meta)
        migrated["format"] = {
            "app_id": META_APP_ID,
            "schema_version": META_SCHEMA_VERSION,
        }
        try:
            _write_json_atomic(path, migrated)
            return migrated
        except OSError:
            log.exception("Не удалось обновить формат журнала в %s", call_dir)
    return meta


def _call_of(meta: dict | None) -> dict:
    return (meta.get("call") if isinstance(meta, dict) else None) or {}


def iter_record_dirs(records_dir: Path):
    """Папки записей — прямые подпапки records_dir."""
    try:
        entries = list(records_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            yield entry


def scan_records(records_dir: Path, limit: int = 300) -> list[dict]:
    """Список записей для окна настроек: по одной записи на папку с meta.json.

    Возвращает словари с полями start/room/dur/status/dir — всё разобрано из
    meta.json папки записи; dir — фактический путь папки, а не поле журнала.
    """
    out: list[dict] = []
    for call_dir in iter_record_dirs(records_dir):
        meta = read_meta(call_dir, migrate_legacy=True)
        if meta is None:
            continue
        call = _call_of(meta)
        started_at = str(call.get("started_at") or "")
        if not started_at:
            try:
                started_at = ts_iso(call_dir.stat().st_mtime)
            except OSError:
                started_at = ""
        out.append({
            "dir": str(call_dir),
            "room": str(call.get("room") or call_dir.name),
            "started_at": started_at,
            "ended_at": call.get("ended_at"),
            "duration_sec": call.get("duration_sec") or 0,
            "status": str(call.get("status") or ("done" if meta else "")),
            "owned": is_owned_meta(meta),
            "_sort": started_at or call_dir.name,
        })
    out.sort(key=lambda r: r["_sort"], reverse=True)
    return out[:limit]


def find_interrupted(records_dir: Path) -> list[Path]:
    """Папки записей, оборванных падением приложения/системы: статус в meta.json
    остался recording/finalizing. Снимок берётся на старте до открытия WS."""
    result = []
    for call_dir in iter_record_dirs(records_dir):
        meta = read_meta(call_dir, migrate_legacy=True)
        call = _call_of(meta)
        status = str(call.get("status") or "")
        if (status in ACTIVE_STATUSES
                or (status == "log_only" and not call.get("ended_at"))):
            result.append(call_dir)
    return result


def reset_interrupted_processing(records_dir: Path) -> int:
    """После рестарта освобождает записи, зависшие в transcribing/summarizing
    (сетевая задача оборвалась) — возвращает их в статус done."""
    count = 0
    for call_dir in iter_record_dirs(records_dir):
        meta = read_meta(call_dir, migrate_legacy=True)
        call = _call_of(meta)
        if str(call.get("status") or "") in PROCESSING_STATUSES:
            call["status"] = "done"
            try:
                _write_json_atomic(call_dir / META_NAME, meta)
                count += 1
            except OSError:
                log.warning("Не удалось сбросить статус записи %s", call_dir)
    return count


def retention_candidates(records_dir: Path) -> list[tuple[Path, dict]]:
    """Завершённые записи с папкой внутри records_dir — кандидаты на удаление.

    Учитываем только папки с meta.json нашего формата: посторонние каталоги без
    журнала ретеншн не трогает."""
    out = []
    for call_dir in iter_record_dirs(records_dir):
        meta = read_meta(call_dir)
        if meta is None or not is_owned_meta(meta):
            continue
        call = _call_of(meta)
        status = str(call.get("status") or "done")
        if status in DELETABLE_STATUSES and call.get("ended_at"):
            out.append((call_dir, call))
    return out


# --------------------- межпроцессная защита обработки и удаления ------------

_processing: dict[Path, BinaryIO] = {}
_processing_guard = threading.Lock()


def _processing_key(call_dir: Path) -> Path:
    try:
        return call_dir.resolve()
    except OSError:
        return call_dir.absolute()


def _processing_lock_path(key: Path) -> Path:
    digest = hashlib.sha256(
        str(key).casefold().encode("utf-8")).hexdigest()[:24]
    return key.parent / ".jitsi-locks" / f"{digest}.lock"


def try_acquire(call_dir: Path) -> bool:
    """Неблокирующе захватывает запись между процессами приложения и GUI."""
    key = _processing_key(call_dir)
    with _processing_guard:
        if key in _processing:
            return False
        lock_path = _processing_lock_path(key)
        fp: BinaryIO | None = None
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fp = open(lock_path, "a+b")
            fp.seek(0, 2)
            if fp.tell() == 0:
                fp.write(b"\0")
                fp.flush()
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if fp is not None:
                fp.close()
            return False
        _processing[key] = fp
        return True


def release(call_dir: Path) -> None:
    key = _processing_key(call_dir)
    with _processing_guard:
        fp = _processing.pop(key, None)
    if fp is None:
        return
    try:
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        fp.close()


def is_processing(call_dir: Path) -> bool:
    if not try_acquire(call_dir):
        return True
    release(call_dir)
    return False
