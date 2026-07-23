"""Отправка записи на корпоративный сервер распознавания речи (STT).

API (см. «эндпоинты.docx»):
  POST {url}/stt/queue-task            multipart/form-data, поле file
                                        -> {"task_id": "uuid"}
  GET  {url}/stt/finished/{task_id}    -> 200 {"status","result"} | 404 в работе
                                          (SSE-канал статуса не используем —
                                           опрос finished надёжнее и проще)

Из кода: await transcribe_call(...). Ручная проверка:
  python -m app.transcribe path\\to\\mix.ogg [--url http://host:8080]
"""
from __future__ import annotations

import json
import logging
import msvcrt
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from app import workers

log = logging.getLogger(__name__)

_stop = threading.Event()


class TranscribeError(RuntimeError):
    pass


def request_stop() -> None:
    """Прерывает ожидание результата (вызывается при завершении приложения)."""
    _stop.set()


def pick_audio(cfg, call_dir: Path) -> Path:
    """Выбирает общий микс в формате, заданном настройкой upload."""
    if str(cfg.tr_upload).lower() == "wav":
        return call_dir / "mix_16k_mono.wav"
    return call_dir / "mix.ogg"


# --- Очередь ручных задач (кнопки вкладки «Список записей») -----------------
# Окно настроек — отдельный процесс; задачи передаются через файл в APPDATA,
# основное приложение подхватывает их в watchdog (и при старте).
# Задача — папка записи (dir) + действие: "stt" — распознать (резюме по
# настройке), "summary" — резюме по готовому транскрипту, "stt_summary" —
# распознать и сделать резюме.

def queue_path() -> Path:
    from app.config import appdata_dir

    return appdata_dir() / "transcribe_queue.json"


def legacy_queue_path() -> Path:
    """Файл задач старого формата, которые не удалось сопоставить с папками."""
    return queue_path().with_name("transcribe_queue_legacy.json")


@contextmanager
def _queue_lock():
    """Сериализует изменения очереди между основным процессом и настройками."""
    lock_path = queue_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fp:
        fp.seek(0, 2)
        if fp.tell() == 0:
            fp.write(b"\0")
            fp.flush()
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)


def _normalize_task(item) -> dict | None:
    if isinstance(item, dict) and item.get("dir"):
        return {"dir": str(item["dir"]),
                "action": str(item.get("action", "stt"))}
    return None


def _read_queue_raw() -> list:
    qp = queue_path()
    if not qp.exists():
        return []
    try:
        raw = json.loads(qp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def _read_queue() -> list[dict]:
    out = []
    for item in _read_queue_raw():
        task = _normalize_task(item)
        if task is not None:
            out.append(task)
    return out


def _write_queue(tasks: list[dict]) -> None:
    qp = queue_path()
    if not tasks:
        qp.unlink(missing_ok=True)
        return
    tmp = qp.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, qp)
    finally:
        tmp.unlink(missing_ok=True)


def _archive_legacy_tasks(tasks: list) -> None:
    if not tasks:
        return
    path = legacy_queue_path()
    existing = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = raw
        except (OSError, json.JSONDecodeError):
            pass
    for task in tasks:
        if task not in existing:
            existing.append(task)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def migrate_legacy_queue(db_paths: Path | tuple[Path, ...]) -> tuple[int, int]:
    """Переводит сохранённые задачи `call_id` в новый формат с путём.

    Несопоставленные элементы не теряются: они уходят в отдельный архив,
    поэтому основной watchdog не запускает пустой обработчик бесконечно.
    """
    with _queue_lock():
        raw = _read_queue_raw()
        if not raw:
            return 0, 0
        current: list[dict] = []
        legacy: list[tuple[object, dict]] = []
        invalid: list = []
        for item in raw:
            normalized = _normalize_task(item)
            if normalized is not None:
                if normalized not in current:
                    current.append(normalized)
                continue
            if isinstance(item, dict) and "call_id" in item:
                try:
                    call_id = int(item["call_id"])
                except (TypeError, ValueError):
                    invalid.append(item)
                    continue
                legacy.append((call_id, item))
            else:
                invalid.append(item)

        directories: dict[int, str] = {}
        candidates = (db_paths,) if isinstance(db_paths, Path) else db_paths
        ids = sorted({int(call_id) for call_id, _ in legacy})
        for db_path in candidates:
            missing = [call_id for call_id in ids
                       if call_id not in directories]
            if not missing or not db_path.exists():
                continue
            try:
                uri = db_path.resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                try:
                    placeholders = ",".join("?" for _ in missing)
                    rows = conn.execute(
                        f"SELECT id, dir FROM calls WHERE id IN ({placeholders})",
                        missing).fetchall()
                    directories.update(
                        (int(call_id), str(call_dir))
                        for call_id, call_dir in rows if call_dir)
                finally:
                    conn.close()
            except sqlite3.Error:
                log.exception(
                    "Не удалось прочитать старую БД для миграции очереди: %s",
                    db_path)

        migrated = 0
        unresolved = list(invalid)
        for call_id, original in legacy:
            call_dir = directories.get(int(call_id))
            if not call_dir:
                unresolved.append(original)
                continue
            task = {
                "dir": call_dir,
                "action": str(original.get("action", "stt")),
            }
            if task not in current:
                current.append(task)
            migrated += 1

        _archive_legacy_tasks(unresolved)
        _write_queue(current)
        return migrated, len(unresolved)


def enqueue(call_dir: str, action: str = "stt") -> None:
    with _queue_lock():
        tasks = _read_queue()
        task = {"dir": str(call_dir), "action": action}
        if task not in tasks:
            tasks.append(task)
        _write_queue(tasks)


def peek_task() -> dict | None:
    """Возвращает первую задачу, не удаляя её до подтверждения выполнения."""
    with _queue_lock():
        tasks = _read_queue()
        return tasks[0] if tasks else None


def ack_task(task: dict) -> None:
    """Удаляет из очереди только уже обработанную задачу."""
    normalized = _normalize_task(task)
    if normalized is None:
        return
    with _queue_lock():
        tasks = _read_queue()
        for i, item in enumerate(tasks):
            if item == normalized:
                tasks.pop(i)
                _write_queue(tasks)
                return


def _upload(url: str, audio: Path) -> str:
    boundary = uuid.uuid4().hex
    body = (
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="'
        + audio.name.encode("utf-8") + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        + audio.read_bytes()
        + b"\r\n--" + boundary.encode() + b"--\r\n"
    )
    req = urllib.request.Request(
        url + "/stt/queue-task", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TranscribeError(
            f"Сервер отвечает, но эндпоинта /stt/queue-task на нём нет "
            f"(HTTP {e.code}) — проверьте адрес сервера STT в настройках") from e
    except urllib.error.URLError as e:
        raise TranscribeError(f"Сервер STT недоступен: {e.reason}") from e
    task_id = data.get("task_id")
    if not task_id:
        raise TranscribeError(f"Сервер не вернул task_id: {data!r}")
    return str(task_id)


def _poll(url: str, task_id: str, poll_s: float, timeout_min: float) -> str:
    deadline = time.monotonic() + timeout_min * 60
    unreachable_logged = False
    while time.monotonic() < deadline:
        if _stop.is_set():
            raise TranscribeError("Прервано завершением приложения")
        try:
            with urllib.request.urlopen(f"{url}/stt/finished/{task_id}",
                                        timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            status = str(data.get("status", ""))
            if status in ("error", "failure"):
                raise TranscribeError(f"Сервер сообщил об ошибке: {data!r}")
            result = data.get("result")
            if result is not None:
                return str(result)
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = задача ещё в работе
                raise TranscribeError(f"HTTP {e.code} от сервера STT") from e
        except urllib.error.URLError as e:
            if not unreachable_logged:
                unreachable_logged = True
                log.warning("STT недоступен (%s) — продолжаю опрос", e.reason)
        if _stop.wait(poll_s):
            raise TranscribeError("Прервано завершением приложения")
    raise TranscribeError(f"Результат не получен за {timeout_min:g} мин")


def run(cfg, audio: Path) -> str:
    """Синхронно (вызывать через asyncio.to_thread): загрузка + опрос."""
    if not audio.exists():
        raise TranscribeError(f"Нет файла для распознавания: {audio}")
    log.info("STT: загружаю %s (%.1f МБ) на %s",
             audio.name, audio.stat().st_size / 1e6, cfg.tr_url)
    task_id = _upload(cfg.tr_url, audio)
    log.info("STT: task_id=%s — жду результат (опрос каждые %d с)",
             task_id, cfg.tr_poll_s)
    text = _poll(cfg.tr_url, task_id, cfg.tr_poll_s, cfg.tr_timeout_min)
    log.info("STT: получен текст, %d символов", len(text))
    return text


async def transcribe_call(cfg, call, notify) -> str | None:
    """Полный цикл для созвона: аудио -> сервер -> transcript.txt + meta.json.

    `call` — CallLog записи. Возвращает текст транскрипта (для последующего
    резюме) или None при ошибке.
    """
    from app import records

    call_dir = call.call_dir
    room = call.room
    audio = pick_audio(cfg, call_dir)
    if not records.try_acquire(call_dir):
        log.info("STT для «%s» не запущен: запись уже обрабатывается", room)
        return None
    previous_status = call.status
    call.set_status("transcribing")
    call.write()
    try:
        call.add_event(time.time(), "transcribe_started", {"file": audio.name})
        call.write()
        try:
            text = await workers.run_daemon(run, cfg, audio)
            tpath = call_dir / "transcript.txt"
            tpath.write_text(text, encoding="utf-8")
            call.set_files(transcript_path=str(tpath))
            call.add_event(time.time(), "transcribe_done", {"chars": len(text)})
            notify("Транскрипция готова", f"«{room}» → transcript.txt")
            return text
        except Exception as e:
            log.exception("Ошибка транскрипции созвона «%s»", room)
            call.add_event(time.time(), "transcribe_failed", {"error": str(e)})
            notify("Ошибка транскрипции", f"«{room}»: {e}")
            return None
    finally:
        call.set_status(previous_status)
        call.write()
        records.release(call_dir)


def main() -> None:
    import argparse

    from app.config import load_config, setup_logging

    ap = argparse.ArgumentParser(description="Ручная отправка файла на STT")
    ap.add_argument("audio")
    ap.add_argument("--url", default=None)
    args = ap.parse_args()
    cfg = load_config()
    setup_logging(cfg, console=True)
    if args.url:
        cfg.tr_url = args.url.rstrip("/")
    print(run(cfg, Path(args.audio)))


if __name__ == "__main__":
    main()
