"""Конфигурация: config.toml в %APPDATA%\\JitsiStreamLogger, создаётся с дефолтами."""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

APP_NAME = "JitsiStreamLogger"
APP_TITLE = "Jitsi Stream Logger"

DEFAULT_CONFIG_TOML = """\
# Конфигурация Jitsi Stream Logger.
# Файл читается при старте приложения.

[general]
# Папка, куда складываются записи созвонов (и общая БД calls.db).
records_dir = "~/Videos/JitsiCalls"
# Домены Jitsi, на которых реагируем. Для собственного сервера добавьте
# его домен сюда и в matches файла extension/manifest.json.
allowed_domains = ["meet.jit.si"]
# Порт локального WebSocket-сервера, к которому подключается расширение Chrome.
# Должен совпадать со значением WS_URL в extension/sw.js.
ws_port = 8765
# Автоматически записывать все созвоны (false = только журнал, без медиа).
auto_record = true
# Путь к ffmpeg (по умолчанию берётся из PATH).
ffmpeg_path = "ffmpeg"

[video]
enabled = true
# Что записывать: "window" — окно Chrome с созвоном (пауза, когда вкладка
# созвона неактивна); "monitor" — целиком монитор с номером monitor_index;
# "cursor" — тот монитор, где сейчас курсор (переключается автоматически).
mode = "window"
monitor_index = 1
fps = 15
crf = 26
preset = "veryfast"
# libx264 | libsvtav1 (AV1, меньше размер) | h264_nvenc | h264_qsv | h264_amf
# Аппаратный AV1 (av1_nvenc) требует NVIDIA RTX 40+; на старых картах — libsvtav1.
encoder = "libx264"
# Параметры AV1 (действуют при encoder = "libsvtav1"):
# preset 0(медленно/меньше файл)…13(быстро/больше), crf 0…63 (больше = меньше/хуже).
# Для записи экрана 7/38 — разумный баланс качества, размера и нагрузки на CPU.
av1_preset = 7
av1_crf = 38

[audio]
# Глушить в записи микрофона интервалы, когда микрофон был замьючен в Jitsi.
respect_mic_mute = true
# Шумоподавление дорожки микрофона при сборке (ffmpeg afftdn + highpass).
mic_denoise = true
# Приглушать микрофон, пока «говорят» динамики: убирает эхо от колонок,
# которое ловит микрофон (sidechain-компрессия по дорожке динамиков).
echo_duck = true

[transcribe]
# Отправлять готовую запись на сервер распознавания речи (STT) после созвона.
enabled = false
url = "http://127.0.0.1:8090"
# Что отправлять: "ogg" (mix.ogg, компактно) или "wav" (mix_16k_mono.wav).
upload = "ogg"
# Период опроса результата (сек) и общий таймаут ожидания (мин).
poll_interval_s = 10
timeout_min = 180

[summary]
# Делать резюме созвона по транскрипту через LLM (/v1/chat/completions).
# Работает после успешной транскрипции. Системный промпт редактируется
# на вкладке «Резюме» в настройках (файл summary_prompt.txt рядом с конфигом).
enabled = false
url = "http://127.0.0.1:8080"
model = "Qwen/Qwen3-32B"
temperature = 0.2
timeout_min = 30

[finalize]
# Хранить сырые WAV/MKV-сегменты после успешной сборки (для отладки).
keep_raw = false
# Удалять mic.ogg и speakers.ogg после сборки mix.ogg (экономия места;
# раздельные дорожки для диаризации при этом теряются).
delete_stems = false
# Удалять ffmpeg_mux.log после успешной сборки.
delete_mux_log = false

[retention]
# Автоматически удалять папки записей старше N дней. 0 = хранить вечно.
days = 0
"""


@dataclass
class Config:
    records_dir: Path
    allowed_domains: tuple[str, ...]
    ws_port: int
    auto_record: bool
    ffmpeg_path: str
    video_enabled: bool
    video_mode: str          # "window" | "monitor"
    video_monitor: int       # номер монитора (с 1) для mode="monitor"
    video_fps: int
    video_crf: int
    video_preset: str
    video_encoder: str
    av1_preset: int
    av1_crf: int
    respect_mic_mute: bool
    mic_denoise: bool
    echo_duck: bool
    tr_enabled: bool
    tr_url: str
    tr_upload: str          # "ogg" | "wav"
    tr_poll_s: int
    tr_timeout_min: int
    sum_enabled: bool
    sum_url: str
    sum_model: str
    sum_temperature: float
    sum_timeout_min: int
    keep_raw: bool
    delete_stems: bool
    delete_mux_log: bool
    retention_days: int
    appdata_dir: Path
    log_dir: Path
    db_path: Path
    # Тайминги FSM (в конфиг не выносим, но переопределяемы в тестах).
    heartbeat_timeout: float = 15.0
    grace_seconds: float = 20.0


def appdata_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME


def load_config() -> Config:
    adir = appdata_dir()
    adir.mkdir(parents=True, exist_ok=True)
    cfg_path = adir / "config.toml"
    if not cfg_path.exists():
        cfg_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)

    gen = raw.get("general", {})
    video = raw.get("video", {})
    audio = raw.get("audio", {})
    tr = raw.get("transcribe", {})
    summ = raw.get("summary", {})
    fin = raw.get("finalize", {})
    ret = raw.get("retention", {})

    records_dir = Path(str(gen.get("records_dir", "~/Videos/JitsiCalls"))).expanduser()
    records_dir.mkdir(parents=True, exist_ok=True)
    log_dir = adir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        records_dir=records_dir,
        allowed_domains=tuple(str(d).lower() for d in gen.get("allowed_domains", ["meet.jit.si"])),
        ws_port=int(gen.get("ws_port", 8765)),
        auto_record=bool(gen.get("auto_record", True)),
        ffmpeg_path=str(gen.get("ffmpeg_path", "ffmpeg")),
        video_enabled=bool(video.get("enabled", True)),
        video_mode=str(video.get("mode", "window")),
        video_monitor=int(video.get("monitor_index", 1)),
        video_fps=int(video.get("fps", 15)),
        video_crf=int(video.get("crf", 26)),
        video_preset=str(video.get("preset", "veryfast")),
        video_encoder=str(video.get("encoder", "libx264")),
        av1_preset=int(video.get("av1_preset", 7)),
        av1_crf=int(video.get("av1_crf", 38)),
        respect_mic_mute=bool(audio.get("respect_mic_mute", True)),
        mic_denoise=bool(audio.get("mic_denoise", True)),
        echo_duck=bool(audio.get("echo_duck", True)),
        tr_enabled=bool(tr.get("enabled", False)),
        tr_url=str(tr.get("url", "http://127.0.0.1:8090")).rstrip("/"),
        tr_upload=str(tr.get("upload", "ogg")),
        tr_poll_s=int(tr.get("poll_interval_s", 10)),
        tr_timeout_min=int(tr.get("timeout_min", 180)),
        sum_enabled=bool(summ.get("enabled", False)),
        sum_url=str(summ.get("url", "http://127.0.0.1:8080")).rstrip("/"),
        sum_model=str(summ.get("model", "Qwen/Qwen3-32B")),
        sum_temperature=float(summ.get("temperature", 0.2)),
        sum_timeout_min=int(summ.get("timeout_min", 30)),
        keep_raw=bool(fin.get("keep_raw", False)),
        delete_stems=bool(fin.get("delete_stems", False)),
        delete_mux_log=bool(fin.get("delete_mux_log", False)),
        retention_days=int(ret.get("days", 0)),
        appdata_dir=adir,
        log_dir=log_dir,
        # БД журнала лежит в APPDATA, а не в records_dir: смена папки записей
        # из меню не должна расщеплять журнал.
        db_path=adir / "calls.db",
    )


def set_config_value(section: str, key: str, value) -> None:
    """Точечно меняет ключ в config.toml, сохраняя комментарии и формат."""
    if isinstance(value, bool):
        rep = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rep = str(value)
    else:
        rep = '"' + str(value).replace("\\", "/").replace('"', '\\"') + '"'

    path = appdata_dir() / "config.toml"
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out: list[str] = []
    in_section = False
    done = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("["):
            if in_section and not done:
                out.append(f"{key} = {rep}")
                done = True
            in_section = stripped == f"[{section}]"
        elif in_section and not done and key_re.match(ln):
            out.append(f"{key} = {rep}")
            done = True
            continue
        out.append(ln)
    if not done:
        if not in_section:
            out += ["", f"[{section}]"]
        out.append(f"{key} = {rep}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# faulthandler держит файл открытым весь срок жизни процесса — сохраняем ссылку,
# чтобы его не собрал GC (иначе стек нативного краха писать будет некуда).
_crash_log_fp = None


def _install_crash_diagnostics(log_dir: Path) -> None:
    """Ловит то, что обычное логирование пропускает.

    Нативный сбой C-модуля (access violation 0xC0000005 в windows_capture.pyd
    или PortAudio) убивает процесс мимо Python — в app.log не попадает ничего,
    даже трейсбека. faulthandler успевает вывалить стек всех потоков в отдельный
    crash.log до смерти процесса. Плюс перехватываем неперехваченные исключения
    в главном и фоновых потоках, чтобы они гарантированно шли в лог.
    """
    global _crash_log_fp
    import faulthandler

    try:
        try:
            path = log_dir / "crash.log"
            _crash_log_fp = open(path, "a", buffering=1, encoding="utf-8")
        except OSError:
            # crash.log занят другим процессом — берём файл с суффиксом PID,
            # иначе стек нативного краха писать было бы некуда.
            path = log_dir / f"crash-{os.getpid()}.log"
            _crash_log_fp = open(path, "a", buffering=1, encoding="utf-8")
        _crash_log_fp.write(f"\n=== process {os.getpid()} started "
                            f"{datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        faulthandler.enable(file=_crash_log_fp, all_threads=True)
    except Exception:
        # Диагностика не должна мешать запуску: хотя бы stderr, если он есть.
        try:
            faulthandler.enable(all_threads=True)
        except Exception:
            pass

    root = logging.getLogger()

    def _excepthook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            root.critical("Неперехваченное исключение",
                          exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "?"
        root.critical("Неперехваченное исключение в потоке «%s»", name,
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_excepthook


def setup_logging(cfg: Config, console: bool = False, logname: str = "app.log",
                  install_crash: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(
            cfg.log_dir / logname, maxBytes=2_000_000, backupCount=5,
            encoding="utf-8",
        )
    except OSError:
        # Лог-файл занят другим процессом (Windows держит файл эксклюзивно) —
        # раньше хендлер молча отваливался и записи запуска терялись целиком.
        # Не теряем лог: пишем в файл с суффиксом PID.
        stem = Path(logname).stem
        fh = logging.handlers.RotatingFileHandler(
            cfg.log_dir / f"{stem}-{os.getpid()}.log", maxBytes=2_000_000,
            backupCount=2, encoding="utf-8",
        )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console and sys.stdout is not None:  # у windowed-exe (PyInstaller) stdout нет
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if install_crash:
        _install_crash_diagnostics(cfg.log_dir)
