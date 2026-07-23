"""Резюме созвона по транскрипту через корпоративный LLM (vLLM, OpenAI-совместимый).

POST {url}/v1/chat/completions, payload по образцу из ТЗ:
  messages = [system: системный промпт, user: транскрипт],
  model, thinking: {"type": "disabled"}, temperature.

Системный промпт хранится в %APPDATA%\\JitsiStreamLogger\\summary_prompt.txt
(редактируется на вкладке «Резюме» окна настроек).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from app import workers
from app.config import appdata_dir

log = logging.getLogger(__name__)

PROMPT_FILE = "summary_prompt.txt"
MAX_TRANSCRIPT_CHARS = 120_000  # защита от сверхдлинных созвонов

DEFAULT_SYSTEM_PROMPT = """\
Ты — ассистент, который составляет резюме рабочих созвонов по транскрипту.
Транскрипт получен автоматическим распознаванием речи и может содержать
ошибки — аккуратно восстанавливай смысл по контексту.

Составь резюме на русском языке в таком виде:
1. Тема и цель встречи (1–2 предложения).
2. Ключевые обсуждённые вопросы — по пунктам.
3. Принятые решения.
4. Задачи и договорённости (кто, что, срок — если названы).

Пиши сжато и по делу, без воды. Если каких-то разделов в разговоре
не было, пропусти их.
"""


class SummarizeError(RuntimeError):
    pass


def prompt_path() -> Path:
    return appdata_dir() / PROMPT_FILE


def load_system_prompt() -> str:
    p = prompt_path()
    try:
        if p.exists():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    return DEFAULT_SYSTEM_PROMPT


def save_system_prompt(text: str) -> None:
    prompt_path().write_text(text.strip() + "\n", encoding="utf-8")


def _clip_transcript(text: str) -> str:
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    head, tail = MAX_TRANSCRIPT_CHARS * 2 // 3, MAX_TRANSCRIPT_CHARS // 3
    log.warning("Транскрипт длиннее %d символов — сокращаю для LLM",
                MAX_TRANSCRIPT_CHARS)
    return (text[:head] + "\n…[транскрипт сокращён из-за длины]…\n"
            + text[-tail:])


def run(cfg, transcript: str, room: str = "", started_at: str = "") -> str:
    """Синхронный запрос к LLM (вызывать через workers.run_daemon)."""
    header = f"Созвон «{room}»" if room else "Созвон"
    if started_at:
        header += f", {started_at}"
    payload = {
        "messages": [
            {"content": load_system_prompt(), "role": "system"},
            {"content": f"{header}.\n\nТранскрипт:\n{_clip_transcript(transcript)}",
             "role": "user"},
        ],
        "model": cfg.sum_model,
        "thinking": {"type": "disabled"},
        "temperature": cfg.sum_temperature,
    }
    req = urllib.request.Request(
        cfg.sum_url + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"})
    log.info("Резюме: запрос к %s (модель %s, %d символов транскрипта)",
             cfg.sum_url, cfg.sum_model, len(transcript))
    try:
        with urllib.request.urlopen(req, timeout=cfg.sum_timeout_min * 60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except OSError:
            pass
        raise SummarizeError(f"LLM ответил HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise SummarizeError(f"LLM-сервер недоступен: {e.reason}") from e
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise SummarizeError(f"Неожиданный ответ LLM: {str(data)[:300]}") from e
    if not str(text).strip():
        raise SummarizeError("LLM вернул пустой ответ")
    log.info("Резюме готово: %d символов", len(text))
    return str(text).strip() + "\n"


async def maybe_summarize(cfg, call, notify, set_tray=None,
                          transcript: str | None = None,
                          force: bool = False) -> None:
    """Резюме после транскрипции (если включено; force — по явному запросу
    пользователя, независимо от настройки). Ошибки не роняют пайплайн.

    `call` — CallLog записи."""
    from app import records

    if not (cfg.sum_enabled or force):
        return
    call_dir = call.call_dir
    room = call.room
    if transcript is None:
        tp = call_dir / "transcript.txt"
        if not tp.exists():
            return
        transcript = tp.read_text(encoding="utf-8")
    if not transcript.strip():
        return
    if not records.try_acquire(call_dir):
        log.info("Резюме для «%s» не запущено: запись уже обрабатывается", room)
        return
    previous_status = call.status
    call.set_status("summarizing")
    call.write()
    try:
        started = (call.started_at or "")[:16].replace("T", " ")
        if set_tray is not None:
            set_tray("transcribing", f"«{room}»: составление резюме…")
        call.add_event(time.time(), "summary_started", None)
        call.write()
        try:
            text = await workers.run_daemon(run, cfg, transcript, room, started)
            spath = call_dir / "summary.txt"
            spath.write_text(text, encoding="utf-8")
            call.set_files(summary_path=str(spath))
            call.add_event(time.time(), "summary_done", {"chars": len(text)})
            notify("Резюме готово", f"«{room}» → summary.txt")
        except Exception as e:
            log.exception("Ошибка резюме созвона «%s»", room)
            call.add_event(time.time(), "summary_failed", {"error": str(e)})
            notify("Ошибка резюме", f"«{room}»: {e}")
    finally:
        call.set_status(previous_status)
        call.write()
        records.release(call_dir)
