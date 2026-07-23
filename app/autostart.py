"""Тихий автозапуск при входе в Windows: ярлык в папке Startup пользователя.

Работает и из исходников (pythonw -m app.main), и из собранного exe
(sys.frozen): ярлык указывает на сам JitsiStreamLogger.exe.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

LNK_NAME = "Jitsi Stream Logger.lnk"


def _startup_dir() -> Path:
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" /
            "Start Menu" / "Programs" / "Startup")


def _target() -> tuple[str, str, str]:
    """(исполняемый файл, аргументы, рабочая папка)."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, "", str(Path(exe).parent)
    project = Path(__file__).resolve().parents[1]
    pythonw = project / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        raise FileNotFoundError(f"Не найден {pythonw} — создайте venv (см. README)")
    return str(pythonw), "-m app.main", str(project)


def lnk_path() -> Path:
    return _startup_dir() / LNK_NAME


def is_installed() -> bool:
    return lnk_path().exists()


def install() -> Path:
    import win32com.client

    target, args, workdir = _target()
    lnk = lnk_path()
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(str(lnk))
    sc.TargetPath = target
    sc.Arguments = args
    sc.WorkingDirectory = workdir
    sc.Description = "Jitsi Stream Logger"
    sc.IconLocation = f"{target},0"
    sc.save()
    return lnk


def remove() -> None:
    lnk_path().unlink(missing_ok=True)
