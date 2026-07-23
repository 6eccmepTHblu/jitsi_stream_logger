"""Точка входа: python -m app.main (для автозапуска — pythonw или exe).

Обычный запуск (двойной клик / автозапуск) — это ДВА процесса:
  • супервизор (верхний) — поднимает рабочий процесс заново после аварий;
  • рабочий процесс (--worker) — сам App + иконка в трее.
Нативные сбои C-модулей (windows_capture, PortAudio) убивают рабочий процесс
целиком; супервизор замечает это по коду выхода и перезапускает запись, чтобы
она не простаивала до конца встречи незамеченной. Режимы --smoke/--console/
--settings/--*-autostart работают напрямую, без супервизии.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

from app.config import APP_TITLE, load_config, setup_logging
from app.core import App

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
FAST_CRASH_S = 20.0        # падение раньше этого времени считаем «быстрым»
MAX_FAST_CRASHES = 5       # столько быстрых падений подряд — сдаёмся
RESTART_BACKOFF_S = 3.0    # пауза перед перезапуском (даём освободить порт 8765)


def _toast(title: str, msg: str) -> None:
    """Уведомление для случаев, когда консоли нет (exe/pythonw)."""
    try:
        from winotify import Notification

        Notification(app_id=APP_TITLE, title=title, msg=msg).show()
    except Exception:
        pass


def _worker_command() -> list[str]:
    """Команда запуска рабочего процесса (обычный App + трей)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker"]
    return [sys.executable, "-m", "app.main", "--worker"]


def _supervise() -> None:
    """Держит рабочий процесс живым, перезапуская его после аварийных выходов."""
    cmd = _worker_command()
    flags = CREATE_NO_WINDOW if getattr(sys, "frozen", False) else 0
    fast = 0
    log.info("Супервизор запущен (pid=%d). Рабочая команда: %s",
             os.getpid(), " ".join(cmd))
    while True:
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, creationflags=flags)
        except Exception:
            log.exception("Не удалось запустить рабочий процесс — супервизор выходит")
            _toast(APP_TITLE, "Не удалось запустить приложение (см. supervisor.log).")
            return
        code = proc.wait()
        lived = time.monotonic() - t0
        if code == 0:
            log.info("Рабочий процесс завершился штатно — супервизор выходит")
            return
        fast = fast + 1 if lived < FAST_CRASH_S else 0
        log.error("Рабочий процесс аварийно завершился (код %s, прожил %.0f c; "
                  "быстрых падений подряд: %d)", code, lived, fast)
        if fast >= MAX_FAST_CRASHES:
            log.critical("Слишком много падений подряд — автоперезапуск остановлен")
            _toast(APP_TITLE, "Приложение постоянно падает — автоперезапуск "
                              "остановлен. Загляните в crash.log.")
            return
        _toast(APP_TITLE, "Запись прервалась сбоем — перезапускаю…")
        time.sleep(RESTART_BACKOFF_S)


def _run_worker(cfg, args) -> None:
    """Собственно приложение: WS-сервер, запись, иконка в трее."""
    app = App(cfg)
    if not app.start_background():
        msg = app.fatal or "Не удалось запуститься (см. лог)"
        log.error(msg)
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
        _toast(APP_TITLE, msg)
        sys.exit(1)

    if args.smoke is not None:
        try:
            time.sleep(args.smoke)
        finally:
            app.request_quit()
            app.join(60)
        return

    from app.tray import Tray  # импорт здесь: в smoke-режиме pystray не нужен

    tray = Tray(app)
    app.tray = tray
    tray.run()      # блокируется до icon.stop() из фонового цикла
    app.join(120)   # дождаться финализации записи при выходе


def main() -> None:
    ap = argparse.ArgumentParser(prog="jitsi-stream-logger")
    ap.add_argument("--smoke", type=float, metavar="SEC",
                    help="служебный режим: запуск без трея на N секунд")
    ap.add_argument("--console", action="store_true",
                    help="дублировать лог в консоль (запуск без супервизора)")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--install-autostart", action="store_true",
                    help="включить тихий автозапуск при входе в Windows и выйти")
    ap.add_argument("--remove-autostart", action="store_true",
                    help="выключить автозапуск и выйти")
    ap.add_argument("--settings", action="store_true",
                    help="открыть окно настроек (отдельный процесс)")
    args = ap.parse_args()

    cfg = load_config()

    if args.settings:
        setup_logging(cfg, logname="settings.log", install_crash=False)
        from app.settings_gui import run_settings

        run_settings()
        return

    if args.install_autostart or args.remove_autostart:
        setup_logging(cfg, install_crash=False)
        from app import autostart

        try:
            if args.install_autostart:
                lnk = autostart.install()
                msg = f"Автозапуск включён ({lnk})"
            else:
                autostart.remove()
                msg = "Автозапуск выключен"
        except Exception as e:
            msg = f"Ошибка настройки автозапуска: {e}"
            log.exception(msg)
            _toast(APP_TITLE, msg)
            sys.exit(1)
        log.info(msg)
        if sys.stdout is not None:
            print(msg)
        _toast(APP_TITLE, msg)
        return

    # Обычный запуск без служебных флагов → верхний процесс становится
    # супервизором. --worker/--smoke/--console выполняют работу напрямую.
    if not (args.worker or args.smoke is not None or args.console):
        setup_logging(cfg, logname="supervisor.log", install_crash=False)
        try:
            _supervise()
        except Exception:
            log.exception("Супервизор аварийно завершился")
        return

    setup_logging(cfg, console=args.console or args.smoke is not None)
    _run_worker(cfg, args)


if __name__ == "__main__":
    main()
