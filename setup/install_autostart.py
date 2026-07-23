"""Создание/удаление ярлыка автозапуска (обёртка над app.autostart).

Запуск:  .venv\\Scripts\\python setup\\install_autostart.py [--remove]
То же самое доступно из меню трея («Автозапуск при входе в Windows»)
и флагами приложения: --install-autostart / --remove-autostart.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import autostart  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true", help="удалить ярлык автозапуска")
    args = ap.parse_args()
    if args.remove:
        autostart.remove()
        print(f"Удалено: {autostart.lnk_path()}")
    else:
        print(f"Создан ярлык автозапуска: {autostart.install()}")


if __name__ == "__main__":
    main()
